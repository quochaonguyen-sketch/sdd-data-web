"""Full-fleet SPX pickup realtime, isolated from the legacy KV5 publisher."""
import argparse
from collections import Counter, defaultdict
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .config import ROOT, Settings
from .pickup_inventory import PickupStore
from .pickup_routes import PickupRouteClient
from .pickup_fetch import fetch_live_windows
from .pickup_cache import read_cache, write_cache, fingerprint
from .runner import RunLock, configure_logging
from .transform import TZ, normalized

log = logging.getLogger(__name__)
UNKNOWN = "Chưa xác định"


class RealtimeStore(PickupStore):
    tables = ("riders", "pickup_48h_no_api2", "pickup_realtime_current")

    def all_rows(self, table, columns, order, filters=None):
        rows, offset = [], 0
        while True:
            page = self.request("GET", table, {"select": columns, "order": order,
                                "offset": str(offset), "limit": "500", **(filters or {})})
            if not isinstance(page, list):
                raise RuntimeError("Dữ liệu phân trang realtime không hợp lệ.")
            rows.extend(page)
            if len(page) < 500:
                return rows
            offset += 500

    def inventory(self):
        def head():
            rows = self.request("GET", "pickup_48h_no_api2", {"select": "snapshot_id,snapshot_at",
                                "order": "snapshot_at.desc.nullslast", "limit": "1"})
            return rows[0] if rows else None
        first = head()
        self.inventory_cache_hit = False
        if not first or not first.get("snapshot_id"):
            # Geography is optional: realtime quantities come directly from SPX.
            return {"snapshot_id": "no-geography", "snapshot_at": datetime.now(TZ).isoformat()}, []
        cache_dir = getattr(self, "cache_dir", None)
        cache_path = cache_dir / f"pickup-realtime-geography-{self.settings.station_id}.json" if cache_dir else None
        context = fingerprint([self.settings.supabase_url, self.settings.station_id])
        cached = read_cache(cache_path) if cache_path else {}
        if (cached.get("context") == context and cached.get("head") == first
                and isinstance(cached.get("rows"), list) and all(isinstance(row, dict) for row in cached["rows"])):
            rows = cached["rows"]
            self.inventory_cache_hit = True
        else:
            rows = self.all_rows("pickup_48h_no_api2", "shipment_id,pickup_point_id,assigned_riders_today,zone_name,ward,area,status",
                                 "tracking_id.asc", {"snapshot_id": "eq." + first["snapshot_id"]})
        if head() != first:
            raise RuntimeError("Tồn pickup đổi phiên trong lúc đọc; giữ nguyên realtime cũ, thử lại lượt sau.")
        if cache_path and not self.inventory_cache_hit:
            write_cache(cache_path, {"context": context, "head": first, "rows": rows})
        return first, rows


class RealtimeClient(PickupRouteClient):
    def search_window(self, path, date_field, left, right, filters=None, id_fields=("id", "pickup_task_id"), *, page_size=100, on_window=None, page_pool=None, max_window_records=9999):
        """Never publish a partial result when the mutable SPX pagination moves rows."""
        deadline = getattr(self, "deadline", time.monotonic() + 480)
        def window(left, right):
            if time.monotonic() > deadline:
                raise RuntimeError("Pickup realtime quá 8 phút; giữ snapshot cũ.")
            params = {date_field: f"{left},{right}", "pageno": 1, "count": page_size, **(filters or {})}
            first = self.get(path, params)
            counter = "assigned_count" if (filters or {}).get("status") == 2 else "pending_assign_count"
            if not isinstance(first.get("list"), list) or ("total" not in first and not (path.endswith("pickup_order/search") and counter in first)):
                raise RuntimeError("SPX thiếu tổng/trang realtime.")
            total = int(first.get("total") or first.get(counter) or 0)
            if total >= 10000 or (total > max_window_records and left < right):
                if left >= right:
                    raise RuntimeError("Cửa sổ task vượt giới hạn SPX.")
                mid = (left + right) // 2
                return window(left, mid) + window(mid + 1, right)
            size = int(first.get("count") or page_size)
            if size <= 0:
                raise RuntimeError("SPX trả kích thước trang task không hợp lệ.")
            result = list(first["list"])
            pages = (total + size - 1) // size
            def page(number):
                data = self.get(path, {**params, "pageno": number})
                if not isinstance(data.get("list"), list):
                    raise RuntimeError("Thiếu trang task SPX.")
                return data["list"]
            with (nullcontext(page_pool) if page_pool is not None else ThreadPoolExecutor(max_workers=min(12, self.settings.inventory_workers))) as pool:
                for items in pool.map(page, range(2, pages + 1)):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Pickup realtime quá 8 phút; giữ snapshot cũ.")
                    result.extend(items)
            ids = [str(next((t.get(field) for field in id_fields if t.get(field)), "")) for t in result]
            if len(result) != total or any(not identity for identity in ids) or len(set(ids)) != total:
                # SPX sorts mutable task states; pagination can repeat/drop IDs.
                # Split the accepted-time window until every unique task is accounted for.
                if left < right:
                    mid = (left + right) // 2
                    return window(left, mid) + window(mid + 1, right)
                raise RuntimeError("Task SPX trùng hoặc thiếu ID trong một giây; giữ snapshot cũ.")
            if on_window:
                on_window(left, right, total)
            return result
        return window(left, right)

    def tasks_today(self, now):
        if not hasattr(self, "deadline"):
            self.deadline = time.monotonic() + 480
        end = int(now.timestamp())
        cursor = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        start, windows = cursor, []
        while cursor <= end:
            right = min(cursor + 14399, end)
            windows.append((cursor, right))
            cursor = right + 1
        cache_dir = getattr(self, "cache_dir", None)
        station = getattr(self.settings, "station_id", "")
        tasks = fetch_live_windows(self, "fm_hub/pickup_task/search", "accepted_time", start, end,
            cache_path=cache_dir / f"pickup-task-windows-{station}.json" if cache_dir else None,
            namespace=f"{station}:{now.date().isoformat()}", initial_windows=windows)
        ids = [str(t.get("id") or t.get("pickup_task_id") or "") for t in tasks]
        if len(ids) != len(set(ids)):
            raise RuntimeError("Task lặp qua cửa sổ thời gian; giữ snapshot cũ.")
        return tasks

    def pending_orders(self, now):
        rows = []
        for status in (0, 2):
            cache_dir = getattr(self, "cache_dir", None)
            station = getattr(self.settings, "station_id", "")
            filename = (f"pickup-inventory-windows-{station}.json" if status == 2 else f"pickup-created-windows-{station}.json")
            rows.extend(fetch_live_windows(self, "fm_hub/pickup_order/search", "ctime", 0, int(now.timestamp()),
                {"status": status, "fetch_total": 1, "fetch_list": 1}, ("pickup_order_id",),
                cache_path=cache_dir / filename if cache_dir else None, namespace=station))
        return rows


def pending_inventory(orders, tasks, geography, routes, now):
    """Mirror effective SPX hold status and task assignment; do not use old stock counts."""
    by_point, accepted, cot2 = defaultdict(set), {}, defaultdict(set)
    for task in tasks:
        pid, code = str(task.get("pickup_point_id") or "").strip(), str(task.get("driver_id") or "").strip()
        if not pid or not code:
            continue
        by_point[pid].add(code)
        ts = int(task.get("accepted_time") or task.get("start_time") or task.get("ctime") or 0)
        accepted[pid] = max(accepted.get(pid, 0), ts)
        dt = datetime.fromtimestamp(ts, TZ) if ts else now
        if dt.hour * 60 + dt.minute > 675:
            cot2[pid].add(code)
    point_info = {str(r.get("pickup_point_id") or ""): r for r in geography if r.get("pickup_point_id")}
    result = []
    for order in orders:
        pid = str(order.get("pickup_point_id") or "").strip()
        status = int(order.get("order_status", order.get("status", -1)))
        attempts = int(order.get("pickup_attempts") or 0)
        hold = any(order.get(k) not in (None, "", 0, "0") for k in ("pickup_on_hold_reason", "on_hold_reason")) or bool(order.get("pickup_on_hold_reason_name"))
        cts = int(order.get("ctime") or order.get("create_time") or 0)
        if status == 0 and attempts > 0 or status == 2 and hold or status == 37:
            effective = "Pickup Onhold"
        elif status == 2 or status == 0 and accepted.get(pid, 0) and (not cts or accepted[pid] >= cts - 120):
            effective = "Assigned"
        else:
            continue
        selected = by_point[pid]
        created = datetime.fromtimestamp(cts, TZ) if cts else None
        if created and created.date() == now.date() and 720 <= created.hour * 60 + created.minute <= 900 and cot2[pid]:
            selected = cot2[pid]
        point = point_info.get(pid, {})
        result.append({"shipment_id": str(order.get("pickup_order_id") or ""), "pickup_point_id": pid,
                       "assigned_riders_today": "; ".join(sorted(selected)), "status": effective,
                       "zone_name": routes.get(pid) or point.get("zone_name") or UNKNOWN,
                       "ward": point.get("ward") or UNKNOWN, "area": point.get("area")})
    return result


def build_realtime(profiles, inventory, tasks, routes, areas, geography=None):
    roster = {str(p.get("rider_code") or "").strip(): p for p in profiles if p.get("kv") in areas}
    names = defaultdict(set)
    for code, profile in roster.items():
        name = normalized(profile.get("full_name") or profile.get("name"))
        if name:
            names[name].add(code)
    point_info = {}
    for row in (geography if geography is not None else inventory):
        pid = str(row.get("pickup_point_id") or "").strip()
        if pid:
            point_info.setdefault(pid, (str(row.get("zone_name") or "").strip() or UNKNOWN,
                                        str(row.get("ward") or "").strip() or UNKNOWN, row.get("area")))
    groups = defaultdict(lambda: {"picked_orders": 0, "pending": 0, "onhold_orders": 0})
    active, seen = set(), set()
    for row in inventory:
        status = row.get("status")
        if status not in ("Assigned", "Pickup Onhold"):
            continue
        sid = str(row.get("shipment_id") or "").strip()
        if not sid:
            continue
        for part in re.split(r"[,;\n]+", str(row.get("assigned_riders_today") or "")):
            label = re.sub(r"^\s*\[COT[12]\]\s*", "", part, flags=re.I).strip()
            match = re.match(r"^([0-9]+)\s*[-–:]", label)
            if match:
                code = match[1]
            elif label.isdigit():
                code = label
            else:
                matches = names.get(normalized(label), set())
                code = next(iter(matches)) if len(matches) == 1 else ""
            if code not in roster or (code, sid) in seen:
                continue
            seen.add((code, sid))
            key = (code, str(row.get("zone_name") or "").strip() or UNKNOWN,
                   str(row.get("ward") or "").strip() or UNKNOWN, row.get("area"))
            groups[key]["pending" if status == "Assigned" else "onhold_orders"] += 1
            active.add(code)
    task_counts, task_points, unmapped = Counter(), defaultdict(set), 0
    for task in tasks:
        code = str(task.get("driver_id") or "").strip()
        if code not in roster:
            unmapped += 1
            continue
        active.add(code)
        qty = int(task.get("pickup_quantity") or 0)
        if qty < 0:
            raise RuntimeError("SPX trả sản lượng pickup âm.")
        pid = str(task.get("pickup_point_id") or "").strip()
        task_counts[code] += 1
        if pid:
            task_points[code].add(pid)
        zone, ward, area = point_info.get(pid, (UNKNOWN, UNKNOWN, None))
        zone = routes.get(pid) or zone
        groups[(code, zone, ward, area)]["picked_orders"] += qty
    records, details = [], []
    for (code, zone, ward, area), counts in groups.items():
        picked, pending, onhold = counts["picked_orders"], counts["pending"], counts["onhold_orders"]
        details.append({"driver_id": code, "khu_vuc": zone, "ward": ward, "ward_area": area,
                        "assigned_orders": picked + pending + onhold, "picked_orders": picked,
                        "onhold_orders": onhold})
    by_rider = defaultdict(list)
    for row in details:
        by_rider[row["driver_id"]].append(row)
    covered = active | {code for code, profile in roster.items() if profile.get("status") != "inactive"}
    for code in sorted(covered):
        profile = roster[code]
        rows = by_rider[code]
        records.append({"driver_id": code, "driver_name": profile.get("full_name") or profile.get("name") or code,
                        "area": profile["kv"], "zones": ", ".join(sorted({r["khu_vuc"] for r in rows if r["khu_vuc"] != UNKNOWN})),
                        "assigned_orders": sum(r["assigned_orders"] for r in rows),
                        "total_pickup_quantity": sum(r["picked_orders"] for r in rows),
                        "onhold_orders": sum(r["onhold_orders"] for r in rows),
                        "pickup_point_count": len(task_points[code]), "task_count": task_counts[code]})
    counts = {area: {"riders": sum(r["area"] == area for r in records),
                     "active_riders": sum(roster[code]["kv"] == area for code in active),
                     "picked": sum(r["total_pickup_quantity"] for r in records if r["area"] == area)} for area in areas}
    return records, details, {"tasks": len(tasks), "tasks_outside_roster": unmapped, "areas": counts}


def sync_once(settings, root=ROOT, *, publish=True):
    store = RealtimeStore(settings)
    client = RealtimeClient(settings)
    client.cache_dir = Path(root) / "data"
    client.deadline = time.monotonic() + 480
    store.cache_dir = client.cache_dir
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        now = datetime.now(TZ)
        started, timings = time.monotonic(), {}
        def measured(name, work, *args):
            begin = time.monotonic()
            try:
                return work(*args)
            finally:
                timings[name + "_ms"] = round((time.monotonic() - begin) * 1000)
        def references():
            # The DB session stays on one thread; SPX uses thread-local sessions.
            profiles = store.all_rows("riders", "rider_code,full_name,name,kv,cot,status", "id.asc")
            return profiles, store.inventory()
        with ThreadPoolExecutor(max_workers=4) as pool:
            reference_future = pool.submit(measured, "references", references)
            task_future = pool.submit(measured, "tasks", client.tasks_today, now)
            pending_future = pool.submit(measured, "orders", client.pending_orders, now)
            route_future = pool.submit(measured, "routes", client.routes)
            profiles, (inventory_head, inventory) = reference_future.result()
            tasks = task_future.result()
            pending_orders = pending_future.result()
            routes, _ = route_future.result()
        log.info("Pickup Realtime: đã kiểm đủ %s task SPX, đang đối chiếu mã tuyến.", len(tasks))
        if not tasks:
            raise RuntimeError("Không có task SPX trong ngày; giữ snapshot realtime cũ.")
        transform_started = time.monotonic()
        live_pending = pending_inventory(pending_orders, tasks, inventory, routes, now)
        riders, groups, stats = build_realtime(profiles, live_pending, tasks, routes, settings.areas, geography=inventory)
        stats["pending_orders_read"] = len(pending_orders)
        stats["onhold_orders"] = sum(row["status"] == "Pickup Onhold" for row in live_pending)
        stats.update(timings)
        stats["transform_ms"] = round((time.monotonic() - transform_started) * 1000)
        stats["read_transform_ms"] = round((time.monotonic() - started) * 1000)
        stats["spx_requests"] = client.request_count
        stats["spx_requests_by_endpoint"] = dict(client.request_counts)
        stats["geography_cache_hit"] = store.inventory_cache_hit
        if not riders:
            raise RuntimeError("Không có rider trong phạm vi; giữ snapshot realtime cũ.")
        stats["source_complete"] = True
        stamp = datetime.now(TZ)
        payload = {"id": 1, "snapshot_id": "sdd-realtime-" + uuid4().hex,
                   "snapshot_at": stamp.isoformat(), "source_date": now.date().isoformat(),
                   "inventory_snapshot_at": now.isoformat(),
                   "inventory_snapshot_id": "spx-orders-" + now.strftime("%Y%m%dT%H%M%S"),
                   "task_count": len(tasks), "riders": riders, "groups": groups}
        artifact = Path(root) / "data" / f"pickup-realtime-{stamp.strftime('%Y%m%d-%H%M%S')}.json"
        artifact.write_text(json.dumps({"stats": stats, "snapshot": payload}, ensure_ascii=False), encoding="utf-8")
        if publish:
            store.request("POST", "pickup_realtime_current", {"on_conflict": "id"}, [payload])
        log.info("Pickup Realtime %s: %s", "đã đồng bộ" if publish else "dry-run, chưa ghi database", stats)
        return stats
    finally:
        store.close()
        client.close()


def run_worker(settings, *, stop_event=None, interval=300, root=ROOT):
    if interval < 60:
        raise ValueError("Interval tối thiểu 60 giây.")
    stop_event = stop_event or threading.Event()
    try:
        lock = RunLock(Path(root) / "data" / "pickup-realtime-worker.lock")
        lock.__enter__()
    except RuntimeError:
        log.info("Pickup Realtime: một worker khác đang chạy; bỏ qua bản trùng.")
        return
    try:
        log.info("Pickup Realtime KV1–KV5: tự cập nhật mỗi %ss từ cookie sdd-web-data.", interval)
        if not stop_event.is_set():
            peek = RealtimeStore(settings)
            try:
                rows = peek.request("GET", "pickup_realtime_current", {"id": "eq.1", "select": "snapshot_at", "limit": "1"})
                if rows:
                    age = (datetime.now(TZ) - datetime.fromisoformat(rows[0]["snapshot_at"])).total_seconds()
                    if 0 <= age < interval:
                        stop_event.wait(interval - age)
            except Exception:
                pass
            finally:
                peek.close()
        while not stop_event.is_set():
            if 8 <= datetime.now(TZ).hour <= 23:
                try:
                    sync_once(settings, root=root)
                except Exception as exc:
                    log.error("Pickup Realtime giữ snapshot cũ: %s", exc)
            stop_event.wait(interval)
    finally:
        lock.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Read and validate without publishing; requires --once")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if args.dry_run and not args.once:
        parser.error("--dry-run cần --once")
    if args.interval < 60:
        raise ValueError("Interval tối thiểu 60 giây.")
    configure_logging()
    if args.once:
        with RunLock(ROOT / "data" / "pickup-realtime-worker.lock"):
            print(json.dumps(sync_once(Settings.load(), publish=not args.dry_run), ensure_ascii=False), flush=True)
    else:
        run_worker(Settings.load(), interval=args.interval)


if __name__ == "__main__":
    main()
