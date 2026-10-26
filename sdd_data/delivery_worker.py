"""Delivery-only Supabase queue worker with leased jobs and atomic publication."""
from datetime import datetime
import logging
import socket
import threading
import time
from uuid import uuid4

from .config import ROOT
from .delivery_inventory import InventoryStore, prepare_inventory
from .runner import RunLock
from .transform import TZ

log = logging.getLogger(__name__)


def in_work_window(now=None):
    return 8 <= (now or datetime.now(TZ)).hour <= 23


def process_one(settings, owner, *, auto=False, root=ROOT, stop_event=None, lookback_days=14, allow_unmapped=True, job_timeout=600):
    stop_event = stop_event or threading.Event()
    store = InventoryStore(settings)
    heartbeat_stop, lease_lost, job_timed_out = threading.Event(), threading.Event(), threading.Event()
    try:
        job = store.rpc("claim_sdd_delivery_job", {"p_worker_id": owner, "p_auto": auto})
        if not job or (isinstance(job, dict) and not any(value is not None for value in job.values())):
            return False
        if not isinstance(job, dict) or not job.get("id") or job.get("worker_id") != owner:
            raise RuntimeError("RPC claim trả job không hợp lệ.")
        job_id = job["id"]
        args = {"p_job_id": job_id, "p_worker_id": owner}
        log.info("Worker nhận tồn Delivery: %s (timeout=%ss)", job_id, job_timeout)

        def on_job_timeout():
            log.warning("Job %s vượt %ss timeout; đánh dấu thất bại.", job_id, job_timeout)
            job_timed_out.set()
            lease_lost.set()

        def heartbeat():
            heartbeat_store = InventoryStore(settings)
            try:
                while not heartbeat_stop.wait(30):
                    if time.monotonic() > job_deadline:
                        on_job_timeout()
                        return
                    try:
                        if not heartbeat_store.rpc("heartbeat_sdd_delivery_job", args):
                            lease_lost.set()
                            return
                    except Exception:
                        lease_lost.set()
                        return
            finally:
                heartbeat_store.close()

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        job_deadline = time.monotonic() + job_timeout
        try:
            rows, stats = prepare_inventory(settings, root=root, stop_event=lease_lost, lookback_days=lookback_days)
            if stats.get("unmapped") and not allow_unmapped:
                raise RuntimeError(f"Có {stats['unmapped']} đơn chưa mapping Coverage SA; giữ nguyên tồn web. Xem báo cáo.")
            if stats.get("unmapped"):
                log.warning("Tồn Delivery: bỏ qua %s đơn chưa rõ địa bàn, không gán khu vực phỏng đoán. Zone: %s",
                            stats["unmapped"], stats.get("unmapped_zones"))
            complete_empty = not rows and stats.get("source_complete") is True and not stats.get("unmapped")
            if not rows and not complete_empty:
                raise RuntimeError("Không có tồn Delivery hợp lệ; giữ nguyên snapshot cũ.")
            if lease_lost.is_set() or not store.rpc("heartbeat_sdd_delivery_job", args):
                raise RuntimeError("Worker đã mất quyền giữ job; không ghi snapshot.")
            snapshot = "sdd-delivery-" + uuid4().hex
            count = store.rpc("publish_sdd_delivery_inventory", {**args, "p_snapshot_id": snapshot,
                "p_snapshot_at": datetime.now(TZ).isoformat(), "p_rows": rows, "p_areas": list(settings.areas),
                **({"p_complete": True} if complete_empty else {})})
            log.info("Tồn Delivery DONE: %s đơn, snapshot=%s", count, snapshot)
        except Exception as exc:
            # Never store arbitrary exception text: it can contain signed URLs or credentials.
            error_msg = (f"Job timeout sau {job_timeout}s; fetch tồn Delivery không hoàn thành kịp."
                         if job_timed_out.is_set()
                         else "Fetch/ghi tồn Delivery thất bại; xem log SDD Web Data trên máy worker.")
            store.rpc("fail_sdd_delivery_job", {**args, "p_error": error_msg})
            raise RuntimeError(f"Tồn Delivery thất bại: {exc}") from None
        finally:
            heartbeat_stop.set()
            thread.join(timeout=95)
        return True
    finally:
        store.close()


def run_worker(settings, *, stop_event=None, queue_only=False, once=False, interval=1800,
               poll_seconds=5, lookback_days=14, allow_unmapped=True, root=ROOT, job_timeout=600):
    if interval < 60 or poll_seconds < 1 or not 1 <= lookback_days <= 90:
        raise ValueError("Worker: interval >=60s, poll >=1s, lookback 1–90 ngày.")
    if job_timeout < 60:
        raise ValueError("Worker: job_timeout >=60s.")
    stop_event = stop_event or threading.Event()
    owner = f"sdd-delivery-{socket.gethostname()}-{uuid4().hex[:10]}"
    with RunLock(root / "data" / "delivery-inventory-worker.lock"):
        log.info("Worker tồn Delivery đang chạy | queue=lmhub_fetch_jobs | tự động=%s | 08:00–23:59 UTC+7", not queue_only)
        last_run = 0
        while not stop_event.is_set():
            try:
                auto = not queue_only and (once or in_work_window()) and (not last_run or time.monotonic()-last_run >= interval)
                handled = process_one(settings, owner, auto=auto, root=root, stop_event=stop_event,
                                      lookback_days=lookback_days, allow_unmapped=allow_unmapped,
                                      job_timeout=job_timeout)
                if handled:
                    last_run = time.monotonic()
                if once:
                    return handled
            except Exception as exc:
                log.error("Worker tồn Delivery: %s", exc)
                last_run = time.monotonic()  # Failed idle runs also back off for the full interval.
                if once:
                    raise
            stop_event.wait(poll_seconds)
        log.info("Đã dừng worker tồn Delivery.")
