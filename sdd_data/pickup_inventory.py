"""SPX Pickup Order / Task / Point Detail -> Pickup Inventory (pickup_48h_no_api2)."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
import json
import logging
from pathlib import Path
import re
import threading
import time as clock

from .clients import SpxClient, WebsiteStore, load_coverage
from .pickup_routes import PickupRouteClient, group_routes
from .pickup_status import pickup_order_status
from .pickup_cache import PickupRecordCache, fingerprint
from .pickup_fetch import fetch_live_windows
from .transform import TZ, as_datetime, location_key, normalized, operating_area, match_zone, confirmed_pickup_location

log = logging.getLogger(__name__)

PICKUP_FIELDS = {
    "tracking_id", "shipment_id", "sls_tracking_number", "pickup_station_id",
    "pickup_station_name", "pickup_point_id", "pickup_point_name",
    "pickup_point_district", "district", "full_address", "create_time", "status",
    "cot_group", "zone_name", "assigned_riders_today", "area", "ward",
    "snapshot_id", "snapshot_at",
}


def parse_pickup_ward(district: str, full_address: str, point_address: str = "") -> str:
    """Extract ward/commune from SPX pickup point address text."""
    for text in (district, full_address, point_address):
        if not text:
            continue
        for part in str(text).split(","):
            p = part.strip()
            low = normalized(p)
            for prefix in ("phuong ", "p. ", "p.", "xa ", "x. ", "x."):
                if low.startswith(prefix):
                    return p
    for text in (point_address, full_address):
        if not text:
            continue
        m = re.search(
            r"(?:phường|phuong|xã|xa|p\.|x\.)\s*\.?\s*([^,]+)",
            str(text),
            re.IGNORECASE,
        )
        if m:
            return m.group(0).strip()
    return ""


def pickup_cot_group(dt: datetime) -> str:
    """COT group for pickup orders:
    00:00 - 11:15 -> COT 1 (D0)
    11:15 - 15:00 -> COT 2 (D0)
    >= 15:00      -> COT 1 (D+1)
    """
    minutes = dt.hour * 60 + dt.minute
    d0 = dt.strftime("%d/%m/%Y")
    d1 = (dt + timedelta(days=1)).strftime("%d/%m/%Y")
    if minutes <= 675:
        return f"{d0} COT 1"
    if minutes <= 900:
        return f"{d0} COT 2"
    return f"{d1} COT 1"


class PickupInventoryClient(PickupRouteClient):
    """Client for fetching SPX pickup orders, tasks, and pickup point details."""

    def __init__(self, settings, stop_event=None):
        super().__init__(settings)
        self.stop_event = stop_event or threading.Event()
        self.deadline = clock.monotonic() + 600

    def check_deadline(self):
        if self.stop_event.is_set():
            raise RuntimeError("Worker đã yêu cầu dừng hoặc mất quyền giữ job.")
        if clock.monotonic() > self.deadline:
            raise RuntimeError("Lấy tồn Pickup quá 10 phút; giữ nguyên snapshot cũ.")

    def fetch_orders_by_status(self, status: int, start_ts: int, end_ts: int) -> list[dict]:
        """Fetch pickup orders for a specific status with auto-pagination and splitting."""
        self.check_deadline()
        # Initial probe: count records
        params = {
            "status": status,
            "ctime": f"{start_ts},{end_ts}",
            "pageno": 1,
            "count": 200,
            "fetch_total": 1,
            "fetch_list": 1,
        }
        first = self.get("fm_hub/pickup_order/search", params)
        if not isinstance(first, dict):
            return []
        
        # In SPX API: when status=0, total is in pending_assign_count; status=2 in assigned_count
        total = int(first.get("total") or 0)
        if not total:
            if status == 0:
                total = int(first.get("pending_assign_count") or 0)
            elif status == 2:
                total = int(first.get("assigned_count") or 0)

        rows = list(first.get("list") or [])
        if total <= len(rows):
            return rows

        # Split window if total is too large
        if total >= 10000 and start_ts < end_ts:
            mid = (start_ts + end_ts) // 2
            return self.fetch_orders_by_status(status, start_ts, mid) + self.fetch_orders_by_status(status, mid + 1, end_ts)

        size = int(first.get("count") or 200)
        if size <= 0:
            size = 200
        total_pages = max(1, (total + size - 1) // size)
        
        pages = range(2, total_pages + 1)
        with ThreadPoolExecutor(max_workers=self.settings.inventory_workers) as executor:
            future_to_page = {
                executor.submit(self.get, "fm_hub/pickup_order/search", {**params, "pageno": p}): p
                for p in pages
            }
            for fut in as_completed(future_to_page):
                self.check_deadline()
                res = fut.result()
                if isinstance(res, dict) and isinstance(res.get("list"), list):
                    rows.extend(res["list"])

        return rows

    def search_window(self, *args, **kwargs):
        from .pickup_realtime import RealtimeClient
        self.check_deadline()
        kwargs.setdefault("page_size", 200)
        return RealtimeClient.search_window(self, *args, **kwargs)

    def fetch_all_pickup_orders(self, lookback_days: int = 7) -> list[dict]:
        """Fetch explicit status=2 across all pending backlog; flags are checked again below."""
        self.check_deadline()
        now = datetime.now(TZ)
        end = int(now.timestamp())
        path = getattr(self, "order_window_cache", None)
        rows = fetch_live_windows(self, "fm_hub/pickup_order/search", "ctime", 0, end,
            {"status": 2, "fetch_total": 1, "fetch_list": 1}, ("pickup_order_id",),
            cache_path=path, namespace=self.settings.station_id)
        self.check_deadline()
        self.assigned_source_complete = True
        log.info("Pickup inventory: đọc đủ %s bản ghi SPX status=2, kiểm tiếp cờ Onhold/Picked.", len(rows))
        return rows

    def fetch_today_tasks(self) -> dict:
        """Fetch today's pickup tasks to map pickup points to assigned riders."""
        now = datetime.now(TZ)
        start_day = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end_now = int(now.timestamp())

        from .pickup_realtime import RealtimeClient
        task_list = RealtimeClient.tasks_today(self, now)
        self.check_deadline()

        point_riders = {}
        accepted_pids = set()
        for task in task_list:
            if not isinstance(task, dict):
                continue
            pid = str(task.get("pickup_point_id") or "").strip()
            if not pid:
                continue
            did = str(task.get("driver_id") or "").strip()
            dname = str(task.get("driver_name") or "").strip()
            label = f"{did}-{dname}" if did and dname else did or dname
            
            # Record accept time
            accept_ts = int(task.get("accepted_time") or 0)
            if accept_ts > 0:
                accepted_pids.add(pid)
            
            # Simple COT
            start_ts = int(task.get("start_time") or task.get("assigned_time") or accept_ts)
            dt_start = datetime.fromtimestamp(start_ts, TZ) if start_ts > 0 else now
            cot = "COT1" if dt_start.hour * 60 + dt_start.minute <= 675 else "COT2"

            if label:
                rider_entry = f"[{cot}] {label}"
                entries = point_riders.setdefault(pid, set())
                entries.add(rider_entry)

        return {
            "point_riders": {pid: "; ".join(sorted(entries)) for pid, entries in point_riders.items()},
            "accepted_pids": accepted_pids,
        }

    def fetch_point_details(self, point_ids: set[str], cache_dir: Path) -> dict[str, dict]:
        """Fetch detail info (district, address) for pickup points with disk caching."""
        cache_file = cache_dir / "pickup_points_catalog.json"
        cache = {}
        if cache_file.is_file():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                cache = {}

        needed = [pid for pid in point_ids if pid and pid not in cache]
        if needed:
            log.info("Cần tải thông tin chi tiết cho %s bưu cục/điểm lấy hàng mới...", len(needed))
            def fetch_one(pid):
                try:
                    data = self.get("pickup_point/detail", {"pickup_point_id": pid, "is_edit": "true"})
                    extra = (data.get("extra") if isinstance(data, dict) else {}) or {}
                    return pid, {
                        "district": str(extra.get("pickup_point_district") or "").strip(),
                        "full_address": str(extra.get("full_address") or "").strip(),
                        "point_address": str(extra.get("pickup_point_address") or "").strip(),
                    }
                except Exception:
                    return pid, {"district": "", "full_address": "", "point_address": ""}

            with ThreadPoolExecutor(max_workers=self.settings.inventory_workers) as executor:
                for pid, detail in executor.map(fetch_one, needed):
                    cache[pid] = detail

            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                log.warning("Không lưu được cache pickup points: %s", exc)

        return {pid: cache.get(pid, {"district": "", "full_address": "", "point_address": ""}) for pid in point_ids}


def build_pickup_inventory(orders: list[dict], mapping: dict, areas: tuple[str, ...],
                           point_details: dict[str, dict], point_groups: dict[str, str],
                           task_info: dict, snapshot_id: str, snapshot_at: str, record_cache=None) -> tuple[list[dict], dict]:
    """Map raw SPX pickup orders into standardized pickup_48h_no_api2 rows."""
    stats = Counter(input=len(orders))
    rows = []
    seen = set()
    point_riders = task_info.get("point_riders", {})

    # Pre-index mapping for O(1) lookups
    ward_lookup = {}
    dist_ward_lookup = {}
    district_names, district_candidates = set(), {}
    for info in mapping.values():
        d = location_key(info.get("district"))
        if info.get("district"):
            district_names.add(info["district"])
            district_candidates.setdefault(d, []).append(info)
        w = location_key(info.get("ward"))
        ow = location_key(info.get("old_ward"))
        if w:
            ward_lookup.setdefault(w, []).append(info)
            dist_ward_lookup.setdefault((d, w), []).append(info)
        if ow and ow != w:
            ward_lookup.setdefault(ow, []).append(info)
            dist_ward_lookup.setdefault((d, ow), []).append(info)

    for order in orders:
        if not isinstance(order, dict):
            continue
        sid = str(order.get("shipment_id") or order.get("pickup_order_id") or "").strip()
        tracking = str(order.get("sls_tracking_number") or sid).strip()
        if not tracking or tracking in seen:
            stats["duplicates"] += 1
            continue
        seen.add(tracking)

        pid = str(order.get("pickup_point_id") or "").strip()
        point_name = str(order.get("pickup_point_name") or "").strip()
        detail = point_details.get(pid, {})
        district = detail.get("district") or str(order.get("pickup_point_district") or "").strip()
        full_address = detail.get("full_address") or str(order.get("full_address") or "").strip()
        point_address = detail.get("point_address") or ""

        # Only explicit, effective Assigned belongs in this inventory.
        status = pickup_order_status(order)
        if status != "Assigned":
            stats["excluded_" + status.lower().replace(" ", "_")] += 1
            continue

        signature = None
        if record_cache is not None:
            signature = fingerprint([order, detail, point_groups.get(pid), point_riders.get(pid, "")])
            cached = record_cache.get(tracking, signature)
            if cached is not None:
                cached.update(snapshot_id=snapshot_id, snapshot_at=snapshot_at)
                rows.append(cached)
                stats["included"] += 1
                continue

        # Timestamp & COT
        cts = order.get("create_time") or order.get("ctime")
        parsed_dt = as_datetime(cts) or as_datetime(order.get("ctime"))
        dt = parsed_dt or datetime.now(TZ)
        create_str = dt.strftime("%d/%m/%Y %H:%M:%S")
        cot_group = pickup_cot_group(dt)

        # Route / Zone
        zone_name = point_groups.get(pid) or str(order.get("zone_name") or order.get("pickup_zone_name") or "").strip()

        # Ward & Area resolution (O(1) indexed lookup)
        ward_text = parse_pickup_ward(district, full_address, point_address)
        _, route_info = match_zone(zone_name, mapping, kind="pickup", ward_hint=ward_text)
        resolved_district = str(route_info.get("district") or "").strip()
        if not resolved_district:
            confirmed = confirmed_pickup_location(zone_name, ward_text, mapping, district_names, district_candidates)
            resolved_district = str((confirmed or {}).get("district") or "").strip()
        area = None
        matched_ward = ward_text
        if ward_text:
            nw = location_key(ward_text)
            nd = location_key(district) if district else ""
            candidates = dist_ward_lookup.get((nd, nw)) or ward_lookup.get(nw) or []
            if candidates:
                first_info = candidates[0]
                candidate_districts = {str(info.get("district") or "").strip() for info in candidates}
                if not resolved_district and len(candidate_districts) == 1:
                    resolved_district = next(iter(candidate_districts))
                matched_ward = first_info.get("ward") or ward_text
                area = operating_area(first_info.get("district"), first_info.get("old_ward"), first_info.get("ward"), first_info.get("area"))

        # Fallback zone route matching if area not determined
        if not area and zone_name:
            matched_key, info = next(((k, v) for k, v in mapping.items() if zone_name.startswith(k)), ("", {}))
            if info:
                area = info.get("area")
                matched_ward = matched_ward or info.get("ward") or ""

        area = area or "KV5"  # default fallback if not mapped
        if area not in areas:
            stats["outside_scope"] += 1
            continue

        assigned_riders = point_riders.get(pid, "")

        record = {
            "tracking_id": tracking,
            "shipment_id": sid or tracking,
            "sls_tracking_number": tracking,
            "pickup_station_id": str(order.get("pickup_station_id") or "4232").strip(),
            "pickup_station_name": str(order.get("pickup_station_name") or "52-HCM SDD-01 Hub").strip(),
            "pickup_point_id": pid,
            "pickup_point_name": point_name,
            "pickup_point_district": district,
            "district": resolved_district,
            "full_address": full_address,
            "create_time": create_str,
            "status": status,
            "cot_group": cot_group,
            "zone_name": zone_name,
            "assigned_riders_today": assigned_riders,
            "area": area,
            "ward": matched_ward or "Chưa xác định",
            "snapshot_id": snapshot_id,
            "snapshot_at": snapshot_at,
        }
        rows.append(record)
        if record_cache is not None and parsed_dt is not None:
            record_cache.put(tracking, signature, record)
        stats["included"] += 1

    if record_cache is not None:
        stats["orders_cached"] = record_cache.hits
        stats["orders_rebuilt"] = stats["included"] - record_cache.hits
    return rows, dict(stats)


class PickupStore(WebsiteStore):
    """Store interface for claiming, heartbeat, failing, and publishing pickup inventory."""

    tables = ("pickup_48h_no_api2", "lmhub_fetch_jobs")

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def request(self, method, table, params, rows=None, return_body=False):
        if table not in self.tables:
            raise ValueError("Bảng không thuộc phạm vi đồng bộ của project.")
        import requests
        headers = {"Prefer": "return=representation" if return_body else "resolution=merge-duplicates,return=minimal"}
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, f"{self.settings.supabase_url}/rest/v1/{table}",
                    params=params, json=rows, headers=headers,
                    timeout=60, allow_redirects=False,
                )
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError(f"Không kết nối được bảng {table} sau 3 lần thử.") from None
                clock.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    clock.sleep(2 ** attempt)
                    continue
            if not response.ok or 300 <= response.status_code < 400:
                try:
                    error_code = str(response.json().get("code", "unknown"))
                except (ValueError, AttributeError):
                    error_code = "unknown"
                raise RuntimeError(f"Bảng {table}: HTTP {response.status_code}, mã {error_code}.")
            if method == "GET" or return_body:
                try:
                    return response.json()
                except Exception:
                    return None
            return None
        raise RuntimeError("Không kết nối được Supabase.")

    def claim_job(self, worker_id: str, auto: bool = False) -> dict | None:
        """Claim a pickup job from lmhub_fetch_jobs queue."""
        # 1. Clean stale jobs first (> 5 minutes without heartbeat)
        stale_cutoff = (datetime.now(TZ) - timedelta(minutes=5)).isoformat()
        try:
            self.request("PATCH", "lmhub_fetch_jobs",
                         {"status": "eq.RUNNING", "updated_at": f"lt.{stale_cutoff}",
                          "worker_id": "like.sdd-pickup-%"},
                         {"status": "ERROR", "error_message": "Pickup worker lost heartbeat",
                          "finished_at": datetime.now(TZ).isoformat(), "updated_at": datetime.now(TZ).isoformat()})
        except Exception:
            pass

        # 2. Check if another worker is running
        running = self.request("GET", "lmhub_fetch_jobs", {"status": "eq.RUNNING", "limit": "1"})
        if running:
            return None

        # 3. Find pending pickup job
        jobs = self.request("GET", "lmhub_fetch_jobs",
                            {"status": "eq.PENDING", "order": "requested_at.asc", "limit": "5"})
        pickup_job = None
        for j in jobs or []:
            kind = str((j.get("params") or {}).get("kind") or "").strip().lower()
            if kind in ("pickup", "ton_pickup", "all", "both"):
                pickup_job = j
                break

        if not pickup_job:
            if not auto:
                return None
            # Insert auto job if requested
            inserted = self.request("POST", "lmhub_fetch_jobs",
                                    {"select": "*"},
                                    [{"status": "PENDING", "message": "SDD auto-fetch tồn Pickup",
                                      "params": {"kind": "pickup", "source": "sdd_web_data_auto"}}],
                                    return_body=True)
            pickup_job = inserted[0] if isinstance(inserted, list) and inserted else None

        if not pickup_job or not pickup_job.get("id"):
            return None

        job_id = pickup_job["id"]
        # Claim atomically
        claimed = self.request("PATCH", "lmhub_fetch_jobs",
                               {"id": f"eq.{job_id}", "status": "eq.PENDING", "select": "*"},
                               {"status": "RUNNING", "worker_id": worker_id,
                                "started_at": datetime.now(TZ).isoformat(),
                                "updated_at": datetime.now(TZ).isoformat(),
                                "error_message": None},
                               return_body=True)
        return claimed[0] if isinstance(claimed, list) and claimed else None

    def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        """Update job heartbeat."""
        res = self.request("PATCH", "lmhub_fetch_jobs",
                           {"id": f"eq.{job_id}", "status": "eq.RUNNING", "worker_id": f"eq.{worker_id}"},
                           {"updated_at": datetime.now(TZ).isoformat()})
        return True

    def fail_job(self, job_id: str, worker_id: str, error: str) -> bool:
        """Mark job as failed."""
        self.request("PATCH", "lmhub_fetch_jobs",
                     {"id": f"eq.{job_id}", "status": "eq.RUNNING", "worker_id": f"eq.{worker_id}"},
                     {"status": "ERROR", "error_message": str(error)[:500],
                      "finished_at": datetime.now(TZ).isoformat(),
                      "updated_at": datetime.now(TZ).isoformat()})
        return True

    def publish_pickup_inventory(self, job_id: str, worker_id: str, snapshot_id: str,
                                 snapshot_at: str, rows: list[dict], areas: tuple[str, ...], *, allow_empty=False) -> int:
        """Upsert rows into pickup_48h_no_api2 and clean up stale snapshots."""
        if not rows and not allow_empty:
            raise ValueError("Không ghi snapshot pickup rỗng khi chưa kiểm đủ nguồn.")
        if any(row.get("status") != "Assigned" for row in rows):
            raise ValueError("Tồn pickup chỉ được ghi Assigned, chưa Onhold/Picked.")

        for r in rows:
            r["snapshot_id"] = snapshot_id
            r["snapshot_at"] = snapshot_at

        table = "pickup_48h_no_api2"
        size = 200
        # 1. Upsert all rows in batches
        for start in range(0, len(rows), size):
            batch = rows[start:start + size]
            self.request("POST", table, {"on_conflict": "tracking_id"}, batch)

        # 2. Delete rows belonging to previous snapshots in the scoped areas
        area_filter = "in.(" + ",".join(areas) + ")"
        self.request("DELETE", table,
                     {"area": area_filter, "snapshot_id": f"neq.{snapshot_id}"})

        # 3. Mark job as DONE
        msg = f"Pickup · {len(rows)} đơn · snapshot {snapshot_id}"
        self.request("PATCH", "lmhub_fetch_jobs",
                     {"id": f"eq.{job_id}", "status": "eq.RUNNING", "worker_id": f"eq.{worker_id}"},
                     {"status": "DONE", "snapshot_id": snapshot_id, "row_count": len(rows),
                      "message": msg, "error_message": None,
                      "finished_at": datetime.now(TZ).isoformat(),
                      "updated_at": datetime.now(TZ).isoformat()})
        return len(rows)


def prepare_pickup_inventory(settings, *, root, stop_event=None, lookback_days=7, orders=None):
    """Full execution pipeline for fetching and preparing pickup inventory."""
    mapping = load_coverage(settings)
    snapshot_id = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    snapshot_at = datetime.now(TZ).isoformat()
    cache_dir = Path(root) / "data"

    source_complete = False
    timings = {}
    excluded_states = Counter()
    source_count = None
    if orders is None:
        client = PickupInventoryClient(settings, stop_event)
        client.cache_dir = cache_dir
        client.order_window_cache = cache_dir / f"pickup-inventory-windows-{settings.station_id}.json"
        try:
            started = clock.monotonic()
            def measured(name, work, *args):
                step_start = clock.monotonic()
                try:
                    return work(*args)
                finally:
                    timings[name + "_ms"] = round((clock.monotonic() - step_start) * 1000)
            # Independent SPX reads overlap. Thread-local sessions isolate concurrent requests.
            with ThreadPoolExecutor(max_workers=3) as pool:
                task_future = pool.submit(measured, "tasks", client.fetch_today_tasks)
                route_future = pool.submit(measured, "routes", client.routes)
                order_future = pool.submit(measured, "orders", client.fetch_all_pickup_orders, lookback_days)
                raw_orders = order_future.result()
                source_count = len(raw_orders)
                orders = []
                for order in raw_orders:
                    state = pickup_order_status(order)
                    if state == "Assigned":
                        orders.append(order)
                    else:
                        excluded_states["excluded_" + state.lower().replace(" ", "_")] += 1
                source_complete = bool(getattr(client, "assigned_source_complete", False))
                point_ids = {str(r.get("pickup_point_id") or "").strip() for r in orders if r.get("pickup_point_id")}
                point_details = measured("point_details", client.fetch_point_details, point_ids, cache_dir)
                routes, _ = route_future.result()
                task_info = task_future.result()
            log.info("Tồn Pickup: đọc đơn, tuyến và task song song hoàn tất trong %.1fs.", clock.monotonic() - started)
        finally:
            client.close()
    else:
        point_details = {}
        routes = {}
        task_info = {"point_riders": {}, "accepted_pids": set()}

    record_cache = PickupRecordCache(cache_dir / f"pickup-inventory-records-{settings.station_id}.json",
                                     [mapping, settings.areas, settings.station_id, "v3-confirmed-district"])
    transform_started = clock.monotonic()
    rows, stats = build_pickup_inventory(orders, mapping, settings.areas, point_details,
                                         routes, task_info, snapshot_id, snapshot_at, record_cache)
    if source_complete:
        record_cache.save()
    stats["transform_ms"] = round((clock.monotonic() - transform_started) * 1000)
    stats["source_complete"] = source_complete
    stats.update(timings)
    if source_count is not None:
        request_count = getattr(client, "request_count", 0)
        stats["spx_requests"] = request_count if isinstance(request_count, int) else 0
        request_counts = getattr(client, "request_counts", {})
        stats["spx_requests_by_endpoint"] = dict(request_counts) if isinstance(request_counts, dict) else {}
    stats.update(excluded_states)
    if source_count is not None:
        stats["input"] = source_count

    artifact = cache_dir / f"pickup-inventory-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"job": "pickup-inventory", "lookback_days": lookback_days,
                                    "scope": "effective Assigned, all pending backlog", "stats": stats, "records": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Tồn Pickup: %s. Báo cáo: %s", stats, artifact)
    return rows, stats
