"""Resolve pickup routes from SPX pickup points, never from destination zones."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import time
import threading

from .clients import AuthenticationError, SpxClient
from .transform import TZ

log = logging.getLogger(__name__)
BASE = "https://spx.shopee.vn/api/admin/pickup/"


def pickup_point(data):
    items = data.get("pickup_info_list")
    if not isinstance(items, list):
        raise RuntimeError("SPX thiếu pickup_info_list; chưa đồng bộ dữ liệu.")
    def stamp(item):
        for field in ("pickedup_time", "driver_pickedup_time", "assigned_time"):
            try:
                value = int(item.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value // 1000 if value > 10_000_000_000 else value
        return 0
    valid = [item for item in items if isinstance(item, dict) and str(item.get("pickup_point_id") or "").strip()]
    if not valid:
        return ""
    latest = max(stamp(item) for item in valid)
    points = {str(item["pickup_point_id"]).strip() for item in valid if stamp(item) == latest}
    # Equal-time events belonging to different points are ambiguous.
    return next(iter(points)) if len(points) == 1 else ""


def group_routes(groups):
    candidates = {}
    for group in groups:
        route = str(group.get("group_name") or "").strip()
        if not route or route.casefold() in {"all", "no group", "default", "ungrouped"}:
            continue
        raw = group.get("pickup_point_id_list") or ""
        points = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw))
        for point in points:
            point = str(point).strip()
            if point:
                candidates.setdefault(point, set()).add(route)
    return {point: next(iter(routes)) for point, routes in candidates.items() if len(routes) == 1}, sum(len(routes) > 1 for routes in candidates.values())


class PickupRouteClient(SpxClient):
    def __init__(self, settings):
        super().__init__(settings)
        self._request_slots = threading.BoundedSemaphore(max(1, min(12, settings.inventory_workers)))
        self._request_lock = threading.Lock()
        self.request_count = 0
        self.request_counts = Counter()

    def get(self, path, params):
        import requests
        for attempt in range(3):
            try:
                with getattr(self, "_request_slots", None) or nullcontext():
                    if hasattr(self, "_request_lock"):
                        with self._request_lock:
                            self.request_count += 1
                            if hasattr(self, "request_counts"):
                                self.request_counts[path] += 1
                    response = self.session().get(BASE + path, params=params, timeout=30, allow_redirects=False)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("Không đọc được pickup point sau 3 lần thử; chưa đồng bộ.") from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise AuthenticationError("SPX từ chối phiên đọc pickup point. Bấm Đăng nhập SPX trong SDD Web Data.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                raise RuntimeError(f"Pickup point: HTTP {response.status_code}; chưa đồng bộ.")
            try:
                body = response.json()
            except ValueError:
                raise AuthenticationError("Pickup point trả trang không phải JSON.") from None
            if not isinstance(body, dict):
                raise RuntimeError("Pickup point trả JSON không hợp lệ.")
            code = body.get("retcode", body.get("error", 0))
            if body.get("is_login") is False or code in (401, 403, 90309999, "401", "403", "90309999"):
                raise AuthenticationError("Phiên đọc pickup point hết hạn.")
            if code not in (None, 0, "0") or not isinstance(body.get("data"), dict):
                raise RuntimeError(f"SPX báo lỗi pickup point (mã {code}); chưa đồng bộ.")
            return body["data"]
        raise RuntimeError("Không đọc được pickup point.")

    def routes(self):
        groups, seen = [], set()
        expected = None
        page = 1
        while True:
            data = self.get("pickup_point_group/search", {"pageno": page, "count": 200,
                "group_type": 4, "station_id": self.settings.station_id, "is_filter": 1})
            if not isinstance(data.get("list"), list) or "total" not in data:
                raise RuntimeError("SPX thiếu trang danh mục tuyến pickup.")
            total = int(data["total"])
            if expected is None:
                expected = total
            if total != expected:
                raise RuntimeError("Danh mục tuyến pickup thay đổi khi lấy trang; chạy lại.")
            for group in data["list"]:
                # SPX repeats these virtual rows on every page and counts
                # filtered/empty groups in total even when not returned.
                if str(group.get("group_name") or "").strip().casefold() in {"all", "no group"}:
                    continue
                identity = group.get("id")
                if identity is None or identity in seen:
                    raise RuntimeError("Trang danh mục tuyến pickup trùng/thiếu ID.")
                seen.add(identity)
                groups.append(group)
            if not data["list"]:
                raise RuntimeError("Thiếu trang danh mục tuyến pickup.")
            if page * 200 >= total:
                break
            page += 1
        routes, conflicts = group_routes(groups)
        log.info("Danh mục pickup: %s nhóm, %s điểm có tuyến duy nhất, %s điểm nhiều tuyến (giữ chưa rõ).", len(groups), len(routes), conflicts)
        return routes, conflicts

    def bulk_points(self, day, shipment_ids):
        """Read an index in sub-10k time windows; detail-read any missed IDs.

        Tracking remains the authoritative volume set. Never add orders from
        this endpoint to volume or infer a seller's point for a different ID.
        """
        begin = datetime.fromisoformat(day).replace(tzinfo=TZ)
        start, finish = int(begin.timestamp()), int((begin + timedelta(days=1)).timestamp()) - 1
        if begin.date() == datetime.now(TZ).date():
            finish = min(finish, int(datetime.now(TZ).timestamp()) - 2)
        points, conflicts = {}, set()
        def compact(rows):
            return [(str(row.get("pickup_order_id") or "").strip(),
                     str(row.get("pickup_point_id") or "").strip()) for row in rows
                    if str(row.get("pickup_order_id") or "").strip() in shipment_ids
                    and str(row.get("pickup_station_id") or "") == self.settings.station_id]
        def window(bounds):
            left, right = bounds
            params = {"ctime": f"{left},{right}", "pageno": 1, "count": 200, "fetch_total": 1, "fetch_list": 1}
            first = self.get("fm_hub/pickup_order/search", params)
            if first.get('total') in (0, '0') and first.get('list') is None:
                return []
            if not isinstance(first.get("list"), list) or "total" not in first:
                raise RuntimeError("SPX thiếu danh sách pickup point theo vận đơn.")
            total, size = int(first["total"]), int(first.get("count") or 200)
            if total < 0 or size <= 0:
                raise RuntimeError("Tổng/kích thước trang pickup không hợp lệ.")
            if total >= 10000:
                if left == right:
                    # Bulk-created second: leave its IDs for individual details.
                    return []
                middle = (left + right) // 2
                return window((left, middle)) + window((middle + 1, right))
            result = compact(first["list"])
            for page in range(2, (total + size - 1) // size + 1):
                data = self.get("fm_hub/pickup_order/search", {**params, "pageno": page})
                # This index can change for today's orders. It does not
                # determine volume: missing source IDs get a detail read.
                if not isinstance(data.get("list"), list):
                    raise RuntimeError("SPX thiếu danh sách pickup point; chưa đồng bộ.")
                result.extend(compact(data["list"]))
            return result
        windows = [(left, min(left + 3599, finish)) for left in range(start, finish + 1, 3600)]
        with ThreadPoolExecutor(max_workers=self.settings.workers) as pool:
            pending = [pool.submit(window, bounds) for bounds in windows]
            for index, future in enumerate(as_completed(pending), 1):
                try:
                    entries = future.result()
                except AuthenticationError:
                    raise
                except RuntimeError:
                    # Tracking is authoritative; resolve missing source IDs via details.
                    log.warning('Một khoảng tra pickup point thiếu dữ liệu; tra chi tiết các mã chưa khớp.')
                    entries = []
                for sid, point in entries:
                    if not point:
                        continue
                    if sid in points and points[sid] != point:
                        conflicts.add(sid)
                    points[sid] = point
                log.info("Danh sách pickup point: %s/%s khoảng giờ, khớp %s vận đơn nguồn.", index, len(windows), len(points))
        for sid in conflicts:
            points.pop(sid, None)
        return points

def enrich_pickup_routes(settings, orders, day, root):
    """Cache only positive point IDs for this day/station; routes are refreshed each run."""
    cache_path = Path(root) / "data" / f"pickup-points-{settings.station_id}-{day}.json"
    cache = {}
    if cache_path.is_file():
        body = json.loads(cache_path.read_text(encoding="utf-8"))
        if body.get("station_id") == settings.station_id and body.get("date") == day:
            cache = {str(k): str(v).strip() for k, v in body.get("points", {}).items() if str(v).strip()}
    ids = list(dict.fromkeys(str(row.get("shipment_id") or "").strip() for row in orders if row.get("shipment_id")))
    missing = [sid for sid in ids if sid not in cache]
    stats = Counter(pickup_point_cached=len(ids) - len(missing))
    client = PickupRouteClient(settings)
    def save():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pending = cache_path.with_suffix(".pending")
        pending.write_text(json.dumps({"date": day, "station_id": settings.station_id, "points": cache}), encoding="utf-8")
        os.replace(pending, cache_path)
    def resolve(sid):
        data = client.get("order/detail/pickup_info", {"shipment_id": sid, "station_type": 12})
        if str(data.get("shipment_id") or "") != sid:
            raise RuntimeError("SPX trả pickup point của vận đơn khác; dừng đồng bộ.")
        return sid, pickup_point(data)
    try:
        routes, conflicts = client.routes()
        stats["pickup_point_route_conflicts"] = conflicts
        if len(missing) > 200:
            bulk = client.bulk_points(day, set(missing))
            cache.update(bulk)
            stats["pickup_point_bulk_found"] = len(bulk)
            missing = [sid for sid in missing if sid not in cache]
            save()
        log.info("Tra pickup point: %s vận đơn, cache %s, cần gọi SPX %s.", len(ids), len(ids)-len(missing), len(missing))
        with ThreadPoolExecutor(max_workers=settings.workers) as pool:
            for start in range(0, len(missing), 200):
                for sid, point in pool.map(resolve, missing[start:start+200]):
                    if point:
                        cache[sid] = point
                save()
                log.info("Pickup point: %s/%s yêu cầu hoàn tất.", min(start+200,len(missing)), len(missing))
        for order in orders:
            point = cache.get(str(order.get("shipment_id") or "").strip(), "")
            route = routes.get(point, "")
            order["pickup_point_id"] = point
            order["pickup_route_name"] = route
            stats["pickup_point_found"] += bool(point)
            stats["pickup_route_found"] += bool(route)
            stats["pickup_route_missing"] += not bool(route)
        return dict(stats)
    finally:
        save()
        client.close()
