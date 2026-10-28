"""Persistent background launcher, with bounded subprocess concurrency and private DB queue."""
import argparse
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .config import ROOT, Settings
from .runner import RunLock, configure_logging
from .source_control import Control, SOURCES
from .session_control import SessionControl

log = logging.getLogger(__name__)


@dataclass
class Child:
    process: subprocess.Popen
    output: object
    cancel: Path
    started: float
    stop_at: float = 0
    finish_status: str | None = None


def child_command(job, worker, cancel):
    if job['source_id'] not in SOURCES:
        raise ValueError('Source not allowed')
    return [sys.executable, '-u', '-m', 'sdd_data.source_job', '--job', job['id'],
            '--worker', worker, '--source', job['source_id'], '--day', job['report_date'],
            '--cancel-file', str(cancel)]


def stop_child(child):
    """Only the process tree created and owned by this daemon."""
    if child.process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(child.process.pid), '/T', '/F'],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    else:
        os.killpg(child.process.pid, signal.SIGTERM)


def bridge_legacy(control):
    control.rpc('bridge_combined_inventory')
    for job in control.pending_inventory():
        params = job.get('params') or {}
        kind = str(params.get('kind') or params.get('job') or params.get('type') or 'delivery').lower()
        source = {'pickup': 'pickup_inventory', 'ton_pickup': 'pickup_inventory',
                  'delivery': 'delivery_inventory', 'ton_delivery': 'delivery_inventory'}.get(kind)
        if source:
            from datetime import datetime
            from .transform import TZ
            control.rpc('control_data_source', p_source=source, p_action='run',
                        p_day=datetime.now(TZ).date().isoformat(), p_actor=job.get('requested_by'))


def run_worker(settings, *, stop_event=None, concurrency=4, root=ROOT, job_timeout=1800):
    if not 1 <= concurrency <= 4:
        raise ValueError('Concurrency 1..4')
    stop = stop_event or threading.Event()
    worker = f'{socket.gethostname()}-{uuid4().hex[:12]}'
    control = Control(settings)
    session_control = SessionControl(settings)
    children = {}
    work = Path(root) / 'data' / 'source-worker'
    work.mkdir(parents=True, exist_ok=True)
    stop_request = work / 'stop.request'
    (Path(root) / 'logs').mkdir(exist_ok=True)
    with RunLock(work / 'daemon.lock'):
        stop_request.unlink(missing_ok=True)
        from .overnight_status import run_worker as run_overnight_status
        overnight_thread = threading.Thread(target=run_overnight_status, kwargs={'settings': settings, 'stop_event': stop},
                                            daemon=True, name='overnight-status')
        overnight_thread.start()
        log.info('Central worker online: %s, max parallel=%s', worker, concurrency)
        try:
            while not stop.is_set() or children:
                if stop_request.exists():
                    stop.set()
                try:
                    control.rpc('pulse_data_source_worker', p_worker=worker, p_hostname=socket.gethostname(),
                                p_version='1.5-lh-dashboard-lh-unseal-pick13-overnight-status', p_concurrency=concurrency)
                    try:
                        session_control.tick(control, worker)
                    except Exception:
                        # Cookie control-plane rollout must not cancel data-source jobs.
                        log.warning('SPX session status unavailable; check session-control SQL configuration.')
                    for job_id, child in list(children.items()):
                        pulse = control.rpc('pulse_data_source_job', p_job=job_id, p_worker=worker)
                        if not pulse:
                            # Ownership was lost: do not start new work; stop reads immediately.
                            child.cancel.touch(exist_ok=True)
                            child.stop_at = child.stop_at or time.monotonic()
                        publishing = bool(pulse and pulse.get('phase') == 'publishing')
                        cancelled = bool(pulse and pulse.get('status') == 'CANCEL_REQUESTED')
                        if stop.is_set() or cancelled or time.monotonic() - child.started > job_timeout:
                            child.cancel.touch(exist_ok=True)
                            child.stop_at = child.stop_at or time.monotonic()
                        if child.stop_at and not publishing and time.monotonic() - child.stop_at > 90:
                            stop_child(child)
                        code = child.process.poll()
                        if code is not None:
                            child.finish_status = child.finish_status or ('SUCCEEDED' if code == 0 else 'CANCELLED' if code == 2 or child.stop_at else 'FAILED')
                            control.rpc('finish_data_source_job', p_job=job_id, p_worker=worker, p_status=child.finish_status)
                            child.output.close()
                            child.cancel.unlink(missing_ok=True)
                            del children[job_id]
                    if not stop.is_set():
                        control.rpc('finalize_onhold_days')
                        control.rpc('enqueue_due_data_sources')
                        bridge_legacy(control)
                        while len(children) < concurrency:
                            job = control.rpc('claim_data_source_job', p_worker=worker)
                            if not job:
                                break
                            cancel = work / f'{job["id"]}.cancel'
                            cancel.unlink(missing_ok=True)
                            output = (Path(root) / 'logs' / f'source-{job["id"]}.log').open('ab')
                            try:
                                process = subprocess.Popen(child_command(job, worker, cancel), cwd=root,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                    start_new_session=os.name != 'nt', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
                                children[job['id']] = Child(process, output, cancel, time.monotonic())
                            except Exception:
                                output.close()
                                control.rpc('finish_data_source_job', p_job=job['id'], p_worker=worker, p_status='FAILED')
                                raise
                except Exception:
                    log.exception('Control connection failed; running children will stop before publication')
                    for child in children.values():
                        child.cancel.touch(exist_ok=True)
                # Use an event wait so shutdown remains responsive; continue heartbeats while draining.
                if stop.is_set():
                    time.sleep(3)
                else:
                    stop.wait(5)
        finally:
            stop.set()
            overnight_thread.join(timeout=5)
            session_control.close()
            control.close()
            stop_request.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='SDD central source worker')
    parser.add_argument('--concurrency', type=int, choices=range(1, 5), default=4)
    parser.add_argument('--stop', action='store_true', help='Yêu cầu worker nền dừng an toàn, không nhận thêm tác vụ.')
    args = parser.parse_args()
    if args.stop:
        request = ROOT / 'data' / 'source-worker' / 'stop.request'
        request.parent.mkdir(parents=True, exist_ok=True)
        request.touch(exist_ok=True)
        print('Đã gửi yêu cầu dừng worker. Tác vụ đang ghi sẽ hoàn tất trước khi tắt.')
        return 0
    configure_logging()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        run_worker(Settings.load(), stop_event=stop, concurrency=args.concurrency)
    except RuntimeError as error:
        log.error('%s', error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
