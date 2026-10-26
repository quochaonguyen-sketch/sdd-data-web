"""Pickup-only Supabase queue worker with leased jobs and atomic publication."""
from datetime import datetime
import logging
import socket
import threading
import time
from uuid import uuid4

from .config import ROOT
from .pickup_inventory import PickupStore, prepare_pickup_inventory
from .runner import RunLock
from .transform import TZ

log = logging.getLogger(__name__)


def in_work_window(now=None):
    return 8 <= (now or datetime.now(TZ)).hour <= 23


def process_one(settings, owner, *, auto=False, root=ROOT, stop_event=None, lookback_days=7, allow_unmapped=True, job_timeout=600):
    from .pickup_inventory import prepare_pickup_inventory
    stop_event = stop_event or threading.Event()
    store = PickupStore(settings)
    heartbeat_stop, lease_lost, job_timed_out = threading.Event(), threading.Event(), threading.Event()
    try:
        job = store.claim_job(owner, auto=auto)
        if not job or not job.get("id"):
            return False
        if job.get("worker_id") != owner:
            raise RuntimeError("Claim trả job không thuộc về worker này.")
        job_id = job["id"]
        log.info("Worker nhận tồn Pickup: %s (timeout=%ss)", job_id, job_timeout)

        job_deadline = time.monotonic() + job_timeout

        def on_job_timeout():
            log.warning("Job tồn Pickup %s vượt %ss timeout; đánh dấu thất bại.", job_id, job_timeout)
            job_timed_out.set()
            lease_lost.set()

        def heartbeat():
            heartbeat_store = PickupStore(settings)
            try:
                while not heartbeat_stop.wait(30):
                    if time.monotonic() > job_deadline:
                        on_job_timeout()
                        return
                    try:
                        if not heartbeat_store.heartbeat_job(job_id, owner):
                            lease_lost.set()
                            return
                    except Exception:
                        lease_lost.set()
                        return
            finally:
                heartbeat_store.close()

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            rows, stats = prepare_pickup_inventory(settings, root=root, stop_event=lease_lost, lookback_days=lookback_days)
            if not rows and not stats.get("source_complete"):
                raise RuntimeError("Chưa xác nhận nguồn tồn Pickup đầy đủ; giữ nguyên snapshot cũ.")
            if lease_lost.is_set():
                raise RuntimeError("Worker đã mất quyền giữ job; không ghi snapshot.")
            snapshot = "sdd-pickup-" + uuid4().hex
            count = store.publish_pickup_inventory(job_id, owner, snapshot,
                                                   datetime.now(TZ).isoformat(),
                                                   rows, settings.areas, allow_empty=bool(stats.get("source_complete")))
            log.info("Tồn Pickup DONE: %s đơn, snapshot=%s", count, snapshot)
        except Exception as exc:
            error_msg = (f"Job timeout sau {job_timeout}s; fetch tồn Pickup không hoàn thành kịp."
                         if job_timed_out.is_set()
                         else f"Fetch/ghi tồn Pickup thất bại: {exc}")
            try:
                store.fail_job(job_id, owner, error_msg)
            except Exception:
                pass
            raise RuntimeError(f"Tồn Pickup thất bại: {exc}") from None
        finally:
            heartbeat_stop.set()
            thread.join(timeout=95)
        return True
    finally:
        store.close()


def run_worker(settings, *, stop_event=None, queue_only=False, once=False, interval=1800,
               poll_seconds=5, lookback_days=7, allow_unmapped=True, root=ROOT, job_timeout=600):
    if interval < 60 or poll_seconds < 1 or not 1 <= lookback_days <= 90:
        raise ValueError("Worker: interval >=60s, poll >=1s, lookback 1–90 ngày.")
    if job_timeout < 60:
        raise ValueError("Worker: job_timeout >=60s.")
    stop_event = stop_event or threading.Event()
    owner = f"sdd-pickup-{socket.gethostname()}-{uuid4().hex[:10]}"
    with RunLock(root / "data" / "pickup-inventory-worker.lock"):
        log.info("Worker tồn Pickup đang chạy | queue=lmhub_fetch_jobs | tự động=%s | 08:00–23:59 UTC+7", not queue_only)
        last_run = 0
        while not stop_event.is_set():
            try:
                auto = not queue_only and (once or in_work_window()) and (not last_run or time.monotonic() - last_run >= interval)
                handled = process_one(settings, owner, auto=auto, root=root, stop_event=stop_event,
                                      lookback_days=lookback_days, allow_unmapped=allow_unmapped,
                                      job_timeout=job_timeout)
                if handled:
                    last_run = time.monotonic()
                if once:
                    return handled
            except Exception as exc:
                log.error("Worker tồn Pickup: %s", exc)
                last_run = time.monotonic()
                if once:
                    raise
            stop_event.wait(poll_seconds)
        log.info("Đã dừng worker tồn Pickup.")
