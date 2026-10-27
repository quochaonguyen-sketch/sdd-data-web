from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import date, datetime
import importlib.util
import json
import logging
from pathlib import Path

from .clients import SpxClient, WebsiteStore, load_coverage, load_seller_wards, read_cookies
from .config import ROOT, Settings
from .transform import JOBS, TZ, summarize
from .performance import PerformanceClient, PerformanceStore, run_performance
from .pickup_routes import enrich_pickup_routes

log = logging.getLogger(__name__)


class RunLock(AbstractContextManager):
    """OS lock, automatically released if a process exits or crashes."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if not self.handle.tell():
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            if self.path.name == "daemon.lock" and self.path.parent.name == "source-worker":
                raise RuntimeError("Worker trung tâm đã chạy nền trên máy này. Mở web → Excel & dữ liệu → Nguồn dữ liệu để điều khiển; không cần bật thêm worker.") from None
            if self.path.name == "delivery-inventory-worker.lock":
                raise RuntimeError("Worker tồn Delivery đã chạy trên máy này. Dừng tiến trình hiện tại trước khi bật lại.") from None
            raise RuntimeError("Job này đang chạy cho cùng ngày. Đợi lượt chạy trước hoàn tất.") from None
        return self

    def __exit__(self, *args):
        self.handle.seek(0)
        if __import__("os").name == "nt":
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


def configure_logging(root=ROOT):
    folder = root / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(folder / f"run-{stamp}.log", encoding="utf-8"),
    ])
    # Third-party HTTP libraries must not print signed URLs or authorization headers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google.auth").setLevel(logging.WARNING)


def validate_day(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("Ngày phải có dạng YYYY-MM-DD.")
    return value


def check(settings: Settings, online=False, report_date=None):
    for module in ("requests", "google.auth"):
        if importlib.util.find_spec(module) is None:
            raise RuntimeError("Thiếu thư viện. Chạy setup.cmd trước.")
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("Thiếu cấu hình Supabase của website.")
    log.info("Đích duy nhất: %s", settings.supabase_url)
    log.info("Bảng: pickup_volume, delivery_order, driver_performance_daily | Phạm vi web: %s", ", ".join(settings.areas))
    if not settings.service_account_file.is_file():
        raise RuntimeError("Không tìm thấy Google service account. Cấu hình GOOGLE_SERVICE_ACCOUNT_FILE.")
    if online:
        store = WebsiteStore(settings)
        try:
            store.check()
        finally:
            store.close()
        performance_store = PerformanceStore(settings)
        try:
            performance_store.check()
        finally:
            performance_store.close()
        mapping = load_coverage(settings)
        log.info("Coverage SA: %s tuyến mapping.", len(mapping))
    from .auth import CookieManager
    CookieManager(settings).cookies()
    log.info("Cookie SPX: cấu trúc và hạn phiên hợp lệ (không hiển thị giá trị cookie).")
    if online:
        day = validate_day(report_date or datetime.now(TZ).date().isoformat())
        client = SpxClient(settings)
        start = int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp())
        try:
            for kind in JOBS:
                result = client.post({**client.payload(kind, start, start + 3599), "count": 1})
                log.info("Đọc API %s: OK (total=%s trong giờ đầu ngày).", kind, result["total"])
        finally:
            client.close()
        performance_client = PerformanceClient(settings)
        try:
            for function_type in (0, 2):
                result = performance_client.post({"count": 1, "frequency": 1, "start_date": day,
                                                  "function_type": function_type, "pageno": 1})
                log.info("Đọc API Performance type=%s: OK (total=%s).", function_type, result["total"])
        finally:
            performance_client.close()
    log.info("Kiểm tra hoàn tất. Không ghi/xoá dữ liệu.")


def run(settings: Settings, kind: str, day: str, *, dry_run=False, mapping_csv=None,
        orders_json=None, allow_unmapped=True, root=ROOT):
    validate_day(day)
    jobs = list(JOBS) if kind == "both" else [*JOBS, "performance"] if kind == "all" else kind.split(",")
    if not jobs or len(set(jobs)) != len(jobs) or any(job not in (*JOBS, "performance") for job in jobs):
        raise ValueError("Job phải là pickup, delivery, performance, both hoặc all.")
    if orders_json and not dry_run:
        raise ValueError("Dữ liệu --orders-json chỉ được dùng với --dry-run để tránh đẩy dữ liệu mẫu lên web.")
    volume_jobs = [job for job in jobs if job in JOBS]
    mapping = {}
    seller_wards = {}
    if volume_jobs:
        if mapping_csv or orders_json:
            mapping = load_coverage(settings, mapping_csv)
            seller_wards = {} if orders_json else load_seller_wards(settings)
        else:
            with ThreadPoolExecutor(max_workers=2) as init_pool:
                f_map = init_pool.submit(load_coverage, settings, mapping_csv)
                f_sel = init_pool.submit(load_seller_wards, settings)
                mapping = f_map.result()
                seller_wards = f_sel.result()
    fixture = json.loads(Path(orders_json).read_text(encoding="utf-8-sig")) if orders_json else None
    client = SpxClient(settings) if not orders_json and volume_jobs else None
    volume_orders = {}

    def _execute_job(job):
        with RunLock(root / "data" / f"{job}-{day}.lock"):
            log.info("Bắt đầu %s | %s | %s", job, day, "DRY RUN - không ghi" if dry_run else "đồng bộ website")
            if job == "performance":
                if fixture is not None and (not isinstance(fixture, dict) or "performance" not in fixture):
                    raise ValueError("File orders-json phải có object performance gồm delivery và pickup.")
                return run_performance(settings, day, dry_run=dry_run, root=root,
                                       fixture=fixture["performance"] if fixture is not None else None,
                                       rider_orders=volume_orders)
            orders = fixture.get(job) if isinstance(fixture, dict) else fixture
            if fixture is not None and not isinstance(orders, list):
                raise ValueError(f"File orders-json phải có danh sách {job}.")
            if fixture is None:
                orders = client.fetch(job, day)
            if any(not isinstance(order, dict) for order in orders):
                raise ValueError("Mỗi đơn phải là một JSON object.")
            volume_orders[job] = orders
            if fixture is None:
                from .performance_cot_cache import save_cots
                save_cots(settings,day,job,orders,root)
            route_stats = enrich_pickup_routes(settings, orders, day, root) if job == "pickup" and fixture is None else {}
            unresolved_orders = []
            rows, stats = summarize(job, orders, mapping, day, settings.areas, seller_wards=seller_wards, unresolved_orders=unresolved_orders)
            stats.update(route_stats)
            stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S-%f")
            artifact = root / "data" / f"{job}-{day}-{stamp}.json"
            result = {"job": job, "date": day, "table": JOBS[job], "dry_run": dry_run,
                      "stats": stats, "records": rows, "status": "prepared"}
            if job == "pickup":
                retry_file = root / "data" / f"pickup-unresolved-{day}-{stamp}.json"
                retry_file.write_text(json.dumps({"job": job, "date": day, "basis": "seller",
                    "count": len(unresolved_orders), "orders": unresolved_orders}, ensure_ascii=False, indent=2), encoding="utf-8")
                result["unresolved_file"] = str(retry_file)
                retry_ids = retry_file.with_suffix(".txt")
                retry_ids.write_text("\n".join(order["shipment_id"] for order in unresolved_orders), encoding="utf-8")
                result["unresolved_ids_file"] = str(retry_ids)
            elif unresolved_orders:
                retry_file = root / "data" / f"delivery-unmapped-{day}-{stamp}.json"
                retry_file.write_text(json.dumps({"job": job, "date": day, "basis": "receiver", "count": len(unresolved_orders), "orders": unresolved_orders}, ensure_ascii=False, indent=2), encoding="utf-8")
                result["unresolved_file"] = str(retry_file)
            artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("Báo cáo chuẩn bị: %s", artifact)
            log.info("%s: %s", job, stats)
            if job == "pickup":
                log.info("Pickup theo SELLER: %s đơn tra địa chỉ seller; %s đơn chưa có mã tuyến seller, không dùng tuyến buyer thay thế.",
                         stats.get("seller_address_matched", 0), stats.get("missing_seller_route", 0))
            if stats.get("unmapped", 0) and allow_unmapped:
                if job == "pickup":
                    log.warning("Pickup: giữ %s đơn trong nhóm Chưa rõ địa bàn seller. Danh sách fetch lại: %s", stats["unmapped"], result["unresolved_file"])
                else:
                    log.warning("%s: bỏ qua %s đơn chưa xác định được địa bàn; chỉ đồng bộ các đơn hợp lệ.", job, stats["unmapped"])
                    result["skipped_unmapped"] = stats["unmapped"]
            if not dry_run:
                invalid = sum(stats.get(key, 0) for key in ("invalid_id", "invalid_time", "outside_date"))
                if invalid or (stats.get("unmapped", 0) and not allow_unmapped):
                    raise RuntimeError("Có đơn không hợp lệ/chưa có mapping. Xem báo cáo dry-run; chưa ghi/xoá web.")
                if not rows:
                    raise RuntimeError("Không có dòng phù hợp. Giữ nguyên dữ liệu web; không tự xoá ngày rỗng.")
                store = WebsiteStore(settings)
                try:
                    result["sync"] = store.commit_snapshot(job, day, rows)
                    if job == "pickup":
                        from .pickup_points import publish_daily
                        result["shop_volume"] = publish_daily(settings, day, orders)
                finally:
                    store.close()
                result["status"] = "synced"
            else:
                result["status"] = "dry_run"
            artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("%s hoàn tất. Báo cáo: %s", job, artifact)
            return result

    all_results, errors = [], []
    try:
        # Nếu có cả volume jobs và performance, chạy volume trước để lấy thông tin đơn hàng phân loại COT
        v_jobs = [j for j in jobs if j in JOBS]
        has_perf = "performance" in jobs
        job_results = {}

        if len(v_jobs) > 1:
            with ThreadPoolExecutor(max_workers=min(len(v_jobs), 2)) as job_pool:
                futures = {j: job_pool.submit(_execute_job, j) for j in v_jobs}
                for j in v_jobs:
                    try:
                        job_results[j] = futures[j].result()
                    except Exception as exc:
                        log.error("%s thất bại: %s", j, exc)
                        errors.append(f"{j}: {exc}")
        elif len(v_jobs) == 1:
            j = v_jobs[0]
            try:
                job_results[j] = _execute_job(j)
            except Exception as exc:
                log.error("%s thất bại: %s", j, exc)
                errors.append(f"{j}: {exc}")

        if has_perf:
            try:
                job_results["performance"] = _execute_job("performance")
            except Exception as exc:
                log.error("performance thất bại: %s", exc)
                errors.append(f"performance: {exc}")

        for j in jobs:
            if j in job_results:
                all_results.append(job_results[j])
    finally:
        if client:
            client.close()

    if errors:
        raise RuntimeError(" | ".join(errors))
    return all_results
