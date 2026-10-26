"""Unified Supabase queue worker for BOTH Delivery and Pickup inventories."""
from datetime import datetime
import logging
import socket
import threading
import time
from uuid import uuid4

from .config import ROOT
from .delivery_worker import in_work_window, process_one as process_delivery
from .pickup_inventory import PickupStore
from .pickup_worker import process_one as process_pickup
from .runner import RunLock
from .transform import TZ

log = logging.getLogger(__name__)


def run_worker(
    settings,
    *,
    stop_event=None,
    queue_only=False,
    once=False,
    enable_delivery=True,
    enable_pickup=True,
    interval=1800,
    poll_seconds=5,
    delivery_lookback=14,
    pickup_lookback=7,
    allow_unmapped=True,
    root=ROOT,
    job_timeout=600,
    enable_realtime=True,
):
    """Run unified worker processing both Delivery and Pickup jobs from lmhub_fetch_jobs queue."""
    if interval < 60 or poll_seconds < 1:
        raise ValueError("Worker: interval >=60s, poll >=1s.")
    if job_timeout < 60:
        raise ValueError("Worker: job_timeout >=60s.")

    stop_event = stop_event or threading.Event()
    hostname = socket.gethostname()
    delivery_owner = f"sdd-delivery-{hostname}-{uuid4().hex[:10]}"
    pickup_owner = f"sdd-pickup-{hostname}-{uuid4().hex[:10]}"

    with RunLock(root / "data" / "delivery-inventory-worker.lock"):
        with RunLock(root / "data" / "pickup-inventory-worker.lock"):
            log.info(
                "Worker tồn TỔNG HỢP (Deli + Pickup) đang chạy | queue=lmhub_fetch_jobs | tự động=%s | 08:00–23:59 UTC+7",
                not queue_only,
            )
            last_delivery = 0.0
            last_pickup = 0.0

            if once:
                log.info("Chế độ chạy một lần (--worker-once): fetch cả 2 tồn...")
                d_ok = False
                p_ok = False
                if enable_delivery:
                    try:
                        log.info(">> Đang fetch tồn Delivery...")
                        d_ok = process_delivery(
                            settings,
                            delivery_owner,
                            auto=True,
                            root=root,
                            stop_event=stop_event,
                            lookback_days=delivery_lookback,
                            allow_unmapped=allow_unmapped,
                            job_timeout=job_timeout,
                        )
                    except Exception as exc:
                        log.error("Tồn Delivery thất bại: %s", exc)
                if enable_pickup:
                    try:
                        log.info(">> Đang fetch tồn Pickup...")
                        p_ok = process_pickup(
                            settings,
                            pickup_owner,
                            auto=True,
                            root=root,
                            stop_event=stop_event,
                            lookback_days=pickup_lookback,
                            job_timeout=job_timeout,
                        )
                    except Exception as exc:
                        log.error("Tồn Pickup thất bại: %s", exc)
                log.info("Hoàn tất chạy một lần: Deli=%s, Pickup=%s", d_ok, p_ok)
                return d_ok or p_ok

            if enable_pickup and enable_realtime and not queue_only:
                from .pickup_realtime import run_worker as run_realtime
                threading.Thread(target=run_realtime, kwargs={"settings": settings, "stop_event": stop_event,
                    "root": root, "interval": 300}, daemon=True, name="pickup-realtime").start()
            store = PickupStore(settings)
            try:
                while not stop_event.is_set():
                    now = time.monotonic()
                    handled = False

                    # 1. Check pending jobs from queue
                    try:
                        pending = store.request(
                            "GET",
                            "lmhub_fetch_jobs",
                            {"status": "eq.PENDING", "order": "requested_at.asc", "limit": "5"},
                        )
                    except Exception as exc:
                        log.debug("Không đọc được hàng đợi jobs: %s", exc)
                        pending = []

                    pending_job = None
                    job_kind = None
                    for j in pending or []:
                        k = str((j.get("params") or {}).get("kind") or "").strip().lower()
                        if k in ("pickup", "ton_pickup") and enable_pickup:
                            pending_job = j
                            job_kind = "pickup"
                            break
                        elif k in ("delivery", "ton_delivery", "") and enable_delivery:
                            pending_job = j
                            job_kind = "delivery"
                            break
                        elif k in ("all", "both"):
                            pending_job = j
                            job_kind = "all"
                            break

                    if pending_job:
                        log.info("Hàng đợi nhận job: %s (kind=%s)", pending_job.get("id"), job_kind)
                        try:
                            if job_kind == "pickup":
                                handled = process_pickup(
                                    settings,
                                    pickup_owner,
                                    auto=False,
                                    root=root,
                                    stop_event=stop_event,
                                    lookback_days=pickup_lookback,
                                    job_timeout=job_timeout,
                                )
                                if handled:
                                    last_pickup = time.monotonic()
                            elif job_kind == "delivery":
                                handled = process_delivery(
                                    settings,
                                    delivery_owner,
                                    auto=False,
                                    root=root,
                                    stop_event=stop_event,
                                    lookback_days=delivery_lookback,
                                    allow_unmapped=allow_unmapped,
                                    job_timeout=job_timeout,
                                )
                                if handled:
                                    last_delivery = time.monotonic()
                            elif job_kind == "all":
                                if enable_pickup:
                                    process_pickup(
                                        settings,
                                        pickup_owner,
                                        auto=False,
                                        root=root,
                                        stop_event=stop_event,
                                        lookback_days=pickup_lookback,
                                        job_timeout=job_timeout,
                                    )
                                    last_pickup = time.monotonic()
                                if enable_delivery:
                                    process_delivery(
                                        settings,
                                        delivery_owner,
                                        auto=False,
                                        root=root,
                                        stop_event=stop_event,
                                        lookback_days=delivery_lookback,
                                        allow_unmapped=allow_unmapped,
                                        job_timeout=job_timeout,
                                    )
                                    last_delivery = time.monotonic()
                                handled = True
                        except Exception as exc:
                            log.error("Lỗi khi xử lý job %s: %s", pending_job.get("id"), exc)

                    # 2. Check idle auto-refresh (every `interval` seconds, 08:00–23:59)
                    if not handled and not queue_only and in_work_window():
                        # Delivery auto-refresh
                        if enable_delivery and (not last_delivery or now - last_delivery >= interval):
                            try:
                                log.info("Tự động fetch tồn Delivery định kỳ...")
                                if process_delivery(
                                    settings,
                                    delivery_owner,
                                    auto=True,
                                    root=root,
                                    stop_event=stop_event,
                                    lookback_days=delivery_lookback,
                                    allow_unmapped=allow_unmapped,
                                    job_timeout=job_timeout,
                                ):
                                    last_delivery = time.monotonic()
                                    handled = True
                            except Exception as exc:
                                log.error("Auto tồn Delivery thất bại: %s", exc)
                                last_delivery = time.monotonic()

                        # Pickup auto-refresh
                        if not stop_event.is_set() and enable_pickup and (not last_pickup or now - last_pickup >= interval):
                            try:
                                log.info("Tự động fetch tồn Pickup định kỳ...")
                                if process_pickup(
                                    settings,
                                    pickup_owner,
                                    auto=True,
                                    root=root,
                                    stop_event=stop_event,
                                    lookback_days=pickup_lookback,
                                    job_timeout=job_timeout,
                                ):
                                    last_pickup = time.monotonic()
                                    handled = True
                            except Exception as exc:
                                log.error("Auto tồn Pickup thất bại: %s", exc)
                                last_pickup = time.monotonic()

                    stop_event.wait(poll_seconds)
            finally:
                store.close()
            log.info("Đã dừng worker tồn TỔNG HỢP.")
