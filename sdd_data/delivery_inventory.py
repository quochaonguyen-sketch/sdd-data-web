"""SPX LMHub_Received -> delivery inventory, using the website's Supabase only."""
from collections import Counter
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
import json
import logging
import math
from pathlib import Path
import threading
import time as clock

from .clients import SpxClient, WebsiteStore, load_coverage, load_seller_wards
from .pickup_cache import read_cache, write_cache, fingerprint
from .transform import TZ, as_datetime, match_zone, location_key

log = logging.getLogger(__name__)
FIELDS = {"shipment_id", "create_time", "current_station_received_time", "order_status",
          "zone_id", "receiver_zone_id", "buyer_zone_id", "pickup_station_name",
          "driver_id", "rider_id", "driverId", "driver_name", "rider_name", "driverName",
          "receiver_ward_name", "buyer_ward_name", "receiver_district_name", "buyer_district_name",
          "lowest_buyer_address_id", "lowest_buyer_address_name", "ward_name", "district_name"}


class InventoryClient(SpxClient):
    def __init__(self, settings, stop_event=None):
        super().__init__(settings)
        self.stop_event = stop_event or threading.Event()
        self.deadline = clock.monotonic() + 600

    def payload(self, kind, start, end, page=1):
        return {"count": 500, "order_status": "1", "create_time": f"{start},{end}",
                "current_station_ids": self.settings.station_id, "page_no": page}

    def post(self, payload):
        if self.stop_event.is_set():
            raise RuntimeError("Worker đã yêu cầu dừng hoặc mất quyền giữ job.")
        if clock.monotonic() > self.deadline:
            raise RuntimeError("Lấy tồn Delivery quá 10 phút; giữ nguyên snapshot cũ.")
        self.session().headers["af-ac-enc-dat"] = ""
        data = super().post(payload)
        data["list"] = [{key: value for key, value in row.items() if key in FIELDS} for row in data["list"]]
        return data

    def fetch_inventory(self, lookback_days=14, *, cache_path=None):
        self.source_complete = False
        now = datetime.now(TZ)
        start = int((now - timedelta(days=lookback_days)).timestamp())
        end = int(now.timestamp()) - 2
        namespace = fingerprint([self.settings.supabase_url, self.settings.station_id, lookback_days, 500])
        windows = inventory_windows(cache_path, namespace, start, end)
        log.info("Tồn Delivery: %s khoảng thích ứng, lookback=%s ngày, workers=%s.",
                 len(windows), lookback_days, self.settings.inventory_workers)
        rows, observed = [], []
        probes = 0
        with ThreadPoolExecutor(max_workers=self.settings.inventory_workers) as executor:
            pending = {executor.submit(self.post, self.payload("inventory", s, e)): (s, e)
                       for s, e in windows}
            while pending:
                future = next(as_completed(pending))
                s, e = pending.pop(future)
                data = future.result()
                probes += 1
                total, page_rows = int(data["total"]), data["list"]
                if total < 0:
                    raise RuntimeError("SPX trả total âm.")
                if total > len(page_rows):
                    if s == e:
                        # Single-second window exceeds one page; paginate fully.
                        complete = self.fetch_window("inventory", s, e)
                        rows.extend(complete)
                        observed.append((s, e, len(complete)))
                    else:
                        seconds = e-s+1
                        parts = min(seconds, max(2, math.ceil(total/400)))
                        for index in range(parts):
                            left, right = s+index*seconds//parts, s+(index+1)*seconds//parts-1
                            pending[executor.submit(self.post, self.payload("inventory", left, right))] = (left, right)
                else:
                    if total and not page_rows:
                        raise RuntimeError("SPX báo có đơn nhưng trang đầu trống; hãy chạy lại.")
                    if len(page_rows) != total or len({str(row.get("shipment_id") or "") for row in page_rows}) != total:
                        raise RuntimeError("SPX trả total không khớp danh sách hoặc đơn trùng trong một trang.")
                    rows.extend(page_rows)
                    observed.append((s, e, total))
                if probes % 50 == 0 or not pending:
                    log.info("inventory: %s đơn | %s probe | %s khoảng còn lại.",
                             len(rows), probes, len(pending))
        ids = [str(row.get("shipment_id") or "").strip() for row in rows]
        if any(not sid for sid in ids) or len(ids) != len(set(ids)):
            raise RuntimeError("Snapshot tồn Delivery thiếu/trùng mã đơn giữa các khoảng; chưa ghi web.")
        log.info("Đã đọc đủ tồn Delivery: %s đơn, lookback=%s ngày.", len(rows), lookback_days)
        self.source_complete = True
        self.fetch_stats = {"requests":probes,"initial_windows":len(windows),"final_windows":len(observed)}
        if cache_path:
            save_inventory_windows(cache_path, namespace, observed)
        return rows


def inventory_windows(path, namespace, start, end):
    """Cache only the partition plan. Every interval is read live on every run."""
    body = read_cache(path) if path else {}
    previous = body.get("windows", [])
    if body.get("namespace") == namespace and isinstance(previous, list) and previous:
        valid = all(isinstance(w,list) and len(w)==2 and all(type(n) is int for n in w) and w[0]<=w[1] for w in previous)
        if valid and all(previous[i-1][1]+1==previous[i][0] for i in range(1,len(previous))):
            clipped = [(max(start,left),min(end,right)) for left,right in previous if right>=start and left<=end]
            if clipped:
                if clipped[0][0]>start: clipped.insert(0,(start,clipped[0][0]-1))
                if clipped[-1][1]<end: clipped.append((clipped[-1][1]+1,end))
                return clipped
    return [(cursor,min(cursor+86399,end)) for cursor in range(start,end+1,86400)]


def save_inventory_windows(path, namespace, observed):
    merged=[]
    for left,right,total in sorted(observed):
        if merged and merged[-1][1]+1==left and (not total and not merged[-1][2] or total>0 and merged[-1][2]>0 and total+merged[-1][2]<=400):
            merged[-1][1]=right
            merged[-1][2]+=total
        else:
            merged.append([left,right,total])
    write_cache(path,{"namespace":namespace,"windows":[[left,right] for left,right,_ in merged]})


def inventory_cot(kind, received):
    cutoff = time(12) if kind == "WH" else time(12, 30)
    cot = 1 if received.time() < cutoff else 2
    day = received.date()
    if kind == "MP" and received.time() >= time(23):
        day += timedelta(days=1)
        cot = 1
    return f"{day.isoformat()} COT {cot}"


def build_inventory(orders, mapping, areas, ward_catalog=None):
    stats = Counter(input=len(orders))
    rows, seen, unmapped_zones = [], set(), Counter()
    ward_catalog = ward_catalog or {}
    location_cache = {}
    ward_locations = {}
    for info in mapping.values():
        candidate = tuple(sorted((key,info.get(key)) for key in ("area","district","old_ward","ward")))
        for key in {location_key(info.get("ward")),location_key(info.get("old_ward"))} - {""}:
            ward_locations.setdefault(key,set()).add(candidate)
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("Tồn Delivery: mỗi đơn phải là JSON object.")
        sid = str(order.get("shipment_id") or "").strip()
        if not sid or sid in seen:
            raise ValueError("Tồn Delivery thiếu/trùng mã đơn.")
        seen.add(sid)
        if str(order.get("order_status")) != "1":
            raise ValueError("API tồn Delivery trả trạng thái ngoài LMHub_Received.")
        assigned = any(str(order.get(key) or "").strip() not in ("", "0", "-1") for key in
                       ("driver_id", "rider_id", "driverId", "driver_name", "rider_name", "driverName"))
        if assigned:
            stats["assigned_skipped"] += 1
            continue
        received = as_datetime(order.get("current_station_received_time"))
        created = as_datetime(order.get("create_time"))
        if not received or not created:
            raise ValueError("Tồn Delivery thiếu thời gian tạo/nhận hợp lệ; chưa ghi web.")
        zone = next((str(order[key]).strip() for key in ("zone_id", "receiver_zone_id", "buyer_zone_id") if order.get(key)), "")
        ward_name = (order.get("receiver_ward_name") or order.get("buyer_ward_name")
                     or order.get("lowest_buyer_address_name") or order.get("ward_name")
                     or ward_catalog.get(str(order.get("lowest_buyer_address_id") or "")))
        district_name = order.get("receiver_district_name") or order.get("buyer_district_name") or order.get("district_name")
        location_signature = (zone, str(ward_name or ""), str(district_name or ""))
        cached_location = location_cache.get(location_signature)
        merged_ward = False
        if cached_location is not None:
            info, merged_ward = cached_location
        else:
            _, info = match_zone(zone, mapping, ward_hint=ward_name, district_hint=district_name)
        if cached_location is None and not info:
            # Only accept a unique buyer location from Coverage SA, never a seller zone.
            ward = location_key(ward_name)
            district = location_key(district_name)
            candidates = {value for value in ward_locations.get(ward,())
                          if not district or district == location_key(dict(value).get("district"))}
            if len(candidates) == 1:
                info = dict(candidates.pop())
            elif candidates:
                # Several old wards may form one new ward. Keep the observed new name
                # only when every matching route agrees on the district and area.
                locations = {(dict(value)["area"], dict(value)["district"]) for value in candidates}
                if len(locations) == 1:
                    area, district_name = locations.pop()
                    info = {"area": area, "district": district_name, "ward": str(ward_name), "old_ward": ""}
                    merged_ward = True
        location_cache[location_signature] = (info, merged_ward)
        if merged_ward:
            stats["new_ward_matched"] += 1
        if not info:
            stats["unmapped"] += 1
            unmapped_zones[zone or "(trống)"] += 1
            continue
        if not info.get("area"):
            stats["unmapped"] += 1
            unmapped_zones[zone or "(trống)"] += 1
            continue
        if info.get("area") not in areas:
            stats["outside_scope"] += 1
            continue
        kind = "WH" if "51-HCM Warehouse Inbound" in str(order.get("pickup_station_name") or "") else "MP"
        rows.append({"shipment_id": sid, "create_time": created.isoformat(), "received_time": received.isoformat(),
                     "report_date": received.date().isoformat(), "ward": info.get("old_ward") or info.get("ward") or "",
                     "district": info.get("district") or "", "area": info["area"], "zone_id": zone,
                     "status": "LMHub_Received", "order_type": kind, "cot_group": inventory_cot(kind, received)})
    stats["included"] = len(rows)
    return rows, {**dict(stats), "unmapped_zones": dict(unmapped_zones)}


class InventoryStore(WebsiteStore):
    tables = ("lmhub_inventory_rows", "lmhub_fetch_jobs")
    RPCS = {"claim_sdd_delivery_job", "heartbeat_sdd_delivery_job", "fail_sdd_delivery_job",
            "publish_sdd_delivery_inventory"}

    def rpc(self, name, payload):
        if name not in self.RPCS:
            raise ValueError("RPC không thuộc worker tồn Delivery.")
        import requests
        if name == "publish_sdd_delivery_inventory" and "p_rows" in payload:
            payload = compact_inventory_payload(payload)
            endpoint = "publish_sdd_delivery_compact"
        else:
            endpoint = name
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # Claims must never retry automatically: a lost response may already own a job.
        retries = 1 if name in ("claim_sdd_delivery_job", "heartbeat_sdd_delivery_job") else 3
        for attempt in range(retries):
            try:
                response = self.session.post(f"{self.settings.supabase_url}/rest/v1/rpc/{endpoint}",
                                             data=encoded, timeout=90 if name == "publish_sdd_delivery_inventory" else 20, allow_redirects=False)
            except requests.RequestException:
                if attempt + 1 < retries:
                    clock.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Không kết nối được RPC {name}; kiểm tra hàng đợi trước khi chạy lại.") from None
            if response.status_code >= 500 and attempt + 1 < retries:
                clock.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                try:
                    code = response.json().get("code", "unknown")
                except (ValueError, AttributeError):
                    code = "unknown"
                raise RuntimeError(f"RPC {name}: HTTP {response.status_code}, mã {code}.")
            return response.json()

    def check(self):
        self.request("GET", "lmhub_inventory_rows", {"select": "snapshot_id,snapshot_at,shipment_id,received_time,area,ward,district,zone_id,status,order_type,cot_group", "limit": "0"})
        self.request("GET", "lmhub_fetch_jobs", {"select": "id,status,worker_id,params,updated_at,snapshot_id,row_count", "limit": "0"})


def compact_inventory_payload(payload):
    """Dictionary encoding removes repeated field names and geography, without losing rows."""
    groups, indices, records = [], {}, []
    for row in payload["p_rows"]:
        if row.get("status") != "LMHub_Received":
            raise ValueError("Invalid delivery status")
        group = tuple(row.get(key) for key in ("report_date","ward","district","area","zone_id","order_type","cot_group"))
        if group not in indices:
            indices[group] = len(groups)
            groups.append(group)
        records.append([row.get("shipment_id"),row.get("create_time"),row.get("received_time"),indices[group]])
    return {**{key:value for key,value in payload.items() if key not in ("p_rows","p_complete")},
            "p_records":records,"p_groups":groups,"p_complete":payload.get("p_complete",False)}


def prepare_inventory(settings, *, lookback_days=14, root, stop_event=None, mapping_csv=None, orders=None):
    if not 1 <= lookback_days <= 90:
        raise ValueError("Lookback tồn Delivery phải từ 1 đến 90 ngày.")
    source_complete = False
    started = clock.perf_counter()
    fetch_ms, fetch_stats = 0, {}
    # Fire Google Sheets I/O in background; they complete while SPX fetch runs.
    with ThreadPoolExecutor(max_workers=2) as io_pool:
        coverage_future = io_pool.submit(load_coverage, settings, mapping_csv, cache_dir=Path(root) / 'data')
        ward_future = io_pool.submit(load_seller_wards, settings) if orders is None else None
        if orders is None:
            client = InventoryClient(replace(settings, inventory_workers=settings.delivery_workers), stop_event)
            try:
                orders = client.fetch_inventory(lookback_days, cache_path=Path(root)/"data"/"delivery-window-plan.json")
                source_complete = getattr(client, "source_complete", False) is True
                fetch_ms = round((clock.perf_counter()-started)*1000)
                fetch_stats = getattr(client,"fetch_stats",{})
            finally:
                client.close()
        mapping = coverage_future.result()
        ward_catalog = ward_future.result() if ward_future else {}
    if not isinstance(orders, list):
        raise ValueError("Inventory fixture phải là danh sách đơn.")
    transform_started = clock.perf_counter()
    rows, stats = build_inventory(orders, mapping, settings.areas, ward_catalog)
    stats["source_complete"] = source_complete
    stats["fetch"] = fetch_stats
    stats["timings_ms"] = {"fetch":fetch_ms,"transform":round((clock.perf_counter()-transform_started)*1000),"prepare":round((clock.perf_counter()-started)*1000)}
    artifact = Path(root) / "data" / f"delivery-inventory-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps({"job": "delivery-inventory", "lookback_days": lookback_days,
                                  "stats": stats, "records": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Tồn Delivery: %s. Báo cáo: %s", stats, artifact)
    return rows, stats
