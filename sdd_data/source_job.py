"""One isolated source execution. Parent maintains leases; writes have a safe stop boundary."""
import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID
from unittest.mock import patch
import requests

from .config import ROOT, Settings
from .source_control import Control, SOURCES
from .runner import RunLock, configure_logging
from .retry import RETRY_EXIT, ROLLED_BACK_EXIT, SourceBusy, retry_reason
from .transform import TZ


class Cancelled(BaseException):
    """Bypass broad source retry handlers; unwind safely before publication."""


class Boundary:
    def __init__(self, control, job, worker, cancel_file):
        self.control, self.job, self.worker = control, job, worker
        self.cancel_file = Path(cancel_file)
        self.publishing = False

    def check(self):
        if not self.publishing and self.cancel_file.exists():
            raise Cancelled()

    def begin(self):
        if self.publishing:
            return
        self.check()
        if not self.control.rpc('begin_data_source_publish', p_job=self.job, p_worker=self.worker):
            raise Cancelled()
        self.publishing = True


def execute(settings, source, day, boundary, *, root=ROOT):
    if source not in SOURCES:
        raise ValueError('Unknown source')
    original_send = requests.Session.send

    def guarded_send(session, *args, **kwargs):
        request = args[0] if args else kwargs.get('request')
        url = getattr(request, 'url', '')
        if not url.startswith(boundary.control.url + 'rpc/'):
            boundary.check()
        if urlsplit(url).hostname == 'spx.shopee.vn':
            timeout = kwargs.get('timeout')
            read = timeout[1] if isinstance(timeout, tuple) else timeout
            kwargs['timeout'] = (10, max(90, min(120, read or 90)))
        return original_send(session, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(requests.Session, 'send', guarded_send))
        if source in ('pickup_inventory', 'delivery_inventory'):
            stack.enter_context(RunLock(Path(root) / 'data' / f'{source.replace("_", "-")}-worker.lock'))
            legacy = boundary.control.rpc('begin_data_source_inventory', p_job=boundary.job, p_worker=boundary.worker)
            if not legacy:
                raise SourceBusy('Nguồn tồn đang được worker khác chạy; đợi hoàn tất rồi chạy lại.')
            if source == 'pickup_inventory':
                from .pickup_inventory import prepare_pickup_inventory
                rows, stats = prepare_pickup_inventory(settings, root=root)
                if not rows and not stats.get('source_complete'):
                    raise RuntimeError('Incomplete empty pickup snapshot')
                boundary.begin()
                stamp = datetime.now(TZ)
                boundary.control.rpc('publish_central_pickup', p_job=boundary.job, p_worker=boundary.worker,
                    p_snapshot=boundary.job, p_at=stamp.isoformat(), p_rows=rows, p_areas=list(settings.areas),
                    p_complete=bool(stats.get('source_complete')))
            else:
                from .delivery_inventory import prepare_inventory, InventoryStore
                rows, stats = prepare_inventory(settings, root=root)
                complete_empty = not rows and stats.get('source_complete') is True and not stats.get('unmapped')
                if not rows and not complete_empty:
                    raise RuntimeError('Incomplete empty delivery snapshot')
                boundary.begin()
                store = InventoryStore(settings)
                try:
                    store.rpc('publish_sdd_delivery_inventory', {'p_job_id': legacy['id'],
                        'p_worker_id': legacy['worker_id'], 'p_snapshot_id': boundary.job,
                        'p_snapshot_at': datetime.now(TZ).isoformat(), 'p_rows': rows, 'p_areas': list(settings.areas),
                        **({'p_complete': True} if complete_empty else {})})
                finally:
                    store.close()
        elif source == 'onhold_daily':
            from .onhold_daily import prepare_onhold_observations
            overnight = None
            if datetime.now(TZ).hour == 23 and datetime.now(TZ).minute < 15:
                if boundary.control.rpc('overnight_hold_capture_due'):
                    overnight = prepare_onhold_observations(settings, day, overnight=True)
            events, stats = prepare_onhold_observations(settings, day)
            boundary.begin()
            if overnight is not None:
                night_events, night_stats = overnight
                boundary.control.rpc('publish_overnight_hold', p_job=boundary.job, p_worker=boundary.worker,
                    p_rows=night_events, p_started=night_stats['scan_started_at'])
            boundary.control.rpc('record_central_onhold', p_job=boundary.job, p_worker=boundary.worker,
                p_events=events, p_input=stats['input'], p_excluded=stats['excluded'], p_started=stats['scan_started_at'])
        elif source == 'wh_tracking':
            from .wh_tracking import prepare_wh
            stack.enter_context(RunLock(Path(root) / 'data' / 'wh-tracking-worker.lock'))
            rows, stamp = prepare_wh(settings, root=root)
            boundary.begin()
            if settings.wh_archive_enabled is True:
                from .wh_archive import publish_wh_archive
                publish_wh_archive(settings, boundary.job, day, rows, stamp)
            else:
                boundary.control.rpc('publish_wh_tracking', p_job=boundary.job, p_worker=boundary.worker,
                    p_station=settings.station_id, p_rows=rows, p_at=stamp.isoformat())
        elif source == 'linehaul':
            from .linehaul import prepare_linehaul
            rows, stamp = prepare_linehaul(settings, root=root)
            boundary.begin()
            boundary.control.rpc('publish_linehaul', p_job=boundary.job, p_worker=boundary.worker,
                p_station=settings.station_id, p_rows=rows, p_at=stamp.isoformat())
        elif source == 'return_inventory':
            from .return_inventory import prepare_returns
            stack.enter_context(RunLock(Path(root) / 'data' / 'return-inventory-worker.lock'))
            rows, stats = prepare_returns(settings, root=root)
            if not stats.get('source_complete'):
                raise RuntimeError('Incomplete return snapshot')
            boundary.begin()
            boundary.control.rpc('publish_central_returns', p_job=boundary.job, p_worker=boundary.worker,
                p_rows=rows, p_complete=True, p_station=int(settings.station_id))
        elif source == 'delivery_realtime':
            from .delivery_realtime import prepare_delivery_realtime
            stack.enter_context(RunLock(Path(root) / 'data' / 'delivery-realtime-worker.lock'))
            rows, stamp = prepare_delivery_realtime(settings)
            boundary.begin()
            boundary.control.rpc('publish_central_delivery_realtime', p_job=boundary.job,
                p_worker=boundary.worker, p_rows=rows, p_at=stamp.isoformat())
        elif source == 'pickup_realtime':
            from .pickup_realtime import RealtimeStore, sync_once
            original = RealtimeStore.request

            def realtime_request(store, method, table, *args, **kwargs):
                if method == 'POST' and table == 'pickup_realtime_current':
                    boundary.begin()
                return original(store, method, table, *args, **kwargs)

            stack.enter_context(RunLock(Path(root) / 'data' / 'pickup-realtime-worker.lock'))
            stack.enter_context(patch.object(RealtimeStore, 'request', realtime_request))
            sync_once(settings, root=root)
        else:
            from .clients import WebsiteStore
            from .performance import PerformanceStore
            from .runner import run
            target = PerformanceStore if source == 'performance' else WebsiteStore
            original = target.commit_snapshot

            def publish(store, *args, **kwargs):
                boundary.begin()
                return original(store, *args, **kwargs)

            stack.enter_context(patch.object(target, 'commit_snapshot', publish))
            run(settings, {'pickup_volume': 'pickup', 'delivery_volume': 'delivery', 'performance': 'performance'}[source],
                day, root=root, allow_unmapped=True)
        if not boundary.publishing:
            raise RuntimeError('Source returned without publishing')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True, type=lambda s: str(UUID(s)))
    parser.add_argument('--worker', required=True)
    parser.add_argument('--source', required=True, choices=sorted(SOURCES))
    parser.add_argument('--day', required=True)
    parser.add_argument('--cancel-file', required=True, type=Path)
    parser.add_argument('--attempt', type=int)
    args = parser.parse_args()
    datetime.strptime(args.day, '%Y-%m-%d')
    configure_logging()
    configured = Settings.load()
    settings = replace(configured, workers=min(8, configured.workers), inventory_workers=min(6, configured.inventory_workers))
    control = Control(settings)
    lease_control = Control(settings)
    lease_stop = threading.Event()

    def heartbeat():
        last_renewal = time.monotonic()
        while not lease_stop.wait(10):
            try:
                pulse = lease_control.rpc('pulse_data_source_job', p_job=args.job, p_worker=args.worker)
                if not pulse or pulse.get('status') == 'CANCEL_REQUESTED':
                    args.cancel_file.touch(exist_ok=True)
                    return
                last_renewal = time.monotonic()
            except Exception:
                # A brief failure can recover while this attempt still owns its lease.
                if time.monotonic() - last_renewal >= 45:
                    with args.cancel_file.open('a', encoding='ascii') as marker:
                        if marker.tell() == 0:
                            marker.write('lease')
                    return

    lease_thread = threading.Thread(target=heartbeat, daemon=True)
    lease_thread.start()
    status, code = 'FAILED', 1
    reason = None
    boundary = Boundary(control, args.job, args.worker, args.cancel_file)
    try:
        pulse = control.rpc('pulse_data_source_job', p_job=args.job, p_worker=args.worker)
        if not pulse or pulse.get('status') == 'CANCEL_REQUESTED':
            raise Cancelled()
        execute(settings, args.source, args.day, boundary)
        status, code = 'SUCCEEDED', 0
    except Cancelled:
        marker = args.cancel_file.read_text(encoding='ascii') if args.cancel_file.exists() else ''
        if marker in ('timeout', 'lease'):
            status, code, reason = 'RETRY', RETRY_EXIT, marker
        else:
            logging.info('Cancelled before publication')
            status, code = 'CANCELLED', 2
    except Exception as error:
        reason = retry_reason(error, publishing=boundary.publishing)
        if reason:
            status, code = 'RETRY', ROLLED_BACK_EXIT if reason == 'rolled_back' else RETRY_EXIT
        logging.exception('Source failed')
    finally:
        # The child acknowledges its own outcome too, so a daemon restart cannot lose a completed job.
        try:
            if args.attempt is None:
                # A daemon started before the migration still invokes the older CLI.
                control.rpc('finish_data_source_job', p_job=args.job, p_worker=args.worker,
                            p_status='FAILED' if status == 'RETRY' else status)
            else:
                control.rpc('complete_data_source_attempt', p_job=args.job, p_worker=args.worker,
                            p_attempt=args.attempt, p_outcome=status, p_reason=reason)
        except Exception:
            logging.exception('Could not acknowledge source result; parent will retry acknowledgement')
        lease_stop.set()
        lease_thread.join(timeout=25)
        lease_control.close()
        control.close()
    return code


if __name__ == '__main__':
    sys.exit(main())
