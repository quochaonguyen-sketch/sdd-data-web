"""Capture SPX pickup orders by creation day and publish one complete snapshot."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import json
import logging
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from time import monotonic


def normalize_orders(raw, day, observed, station, backlog, routes, mapping):
    from sdd_data.pickup_status import pickup_order_status
    from sdd_data.transform import as_datetime, confirmed_pickup_location, location_key, match_zone

    unknown = "Chưa xác định"

    def clean(value):
        value = str(value or "").strip()
        return "" if not value or value.startswith("Chưa ") else value

    geography = defaultdict(set)
    for row in backlog:
        if row["pickup_point_id"]:
            geography[str(row["pickup_point_id"])].add(tuple(clean(row.get(key)) for key in ("zone_name", "ward", "area")))

    wards = defaultdict(set)
    for value in mapping.values():
        for info in value.get("zone_candidates", [value]):
            for key in ("ward", "old_ward"):
                if info.get(key):
                    wards[location_key(info[key])].add((info.get("ward") or "", info.get("area") or ""))

    states = {"Assigned": "pending", "Pickup Onhold": "hold", "Picked up": "picked", "Inbound": "picked"}
    result, ids, excluded = [], set(), Counter()
    for row in raw:
        created_at = as_datetime(row.get("ctime"))
        if created_at is None or created_at.date() != day or created_at > observed:
            raise RuntimeError("SPX trả mã đơn ngoài khoảng thời gian tạo được yêu cầu.")
        if str(row.get("pickup_station_id")) != station:
            continue
        shipment = str(row.get("pickup_order_id") or "").strip().upper()
        if not shipment or shipment in ids:
            raise RuntimeError("SPX trả mã đơn trùng hoặc trống.")
        ids.add(shipment)
        state = "Picked up" if as_datetime(row.get("driver_pickedup_time")) else pickup_order_status(row)
        if state not in states:
            excluded[f"{state}:{row.get('status')}"] += 1
            continue
        point = str(row.get("pickup_point_id") or "").strip()
        point_zones, point_wards, point_areas = (
            {entry[index] for entry in geography[point] if entry[index]} for index in range(3)
        )
        zone = routes.get(point) or (next(iter(point_zones)) if len(point_zones) == 1 else "")
        town = clean(row.get("uptown_name"))
        _, route = match_zone(zone, mapping, kind="pickup", ward_hint=town)
        ward_matches = wards.get(location_key(town), set())
        ward, area = next(iter(ward_matches)) if len(ward_matches) == 1 else ("", "")
        ward = ward or (next(iter(point_wards)) if len(point_wards) == 1 else "") or town
        area = (next(iter(point_areas)) if len(point_areas) == 1 else "") or route.get("area") or area
        if zone and (not area or not ward):
            confirmed = confirmed_pickup_location(zone, ward, mapping) or {}
            ward = ward or confirmed.get("ward")
            area = area or confirmed.get("area")
        result.append({
            "shipmentId": shipment, "status": states[state], "zone": zone or unknown,
            "ward": clean(ward) or clean(route.get("ward")) or unknown,
            "area": area if area in {"KV1", "KV2", "KV3", "KV4", "KV5"} else unknown,
        })

    logging.info("SPX đã kiểm %s mã tại trạm. Trạng thái ngoài báo cáo %s.", len(ids), dict(excluded))
    return sorted(result, key=lambda row: row["shipmentId"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--publish", action="store_true", help="Publish the complete snapshot to Supabase.")
    args = parser.parse_args()
    worker_dir = args.worker_dir.resolve()
    sys.path.insert(0, str(worker_dir))
    from sdd_data.config import Settings
    from sdd_data.pickup_realtime import RealtimeClient, RealtimeStore
    from sdd_data.pickup_fetch import fetch_live_windows
    from sdd_data.clients import load_coverage
    from sdd_data.runner import RunLock
    from sdd_data.transform import TZ

    class DailyOrderStore(RealtimeStore):
        tables = (*RealtimeStore.tables, "rpc/publish_pickup_daily_orders")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.load(worker_dir)
    observed = datetime.now(TZ).replace(microsecond=0)
    day = args.date or observed.date()
    if day > observed.date():
        parser.error("--date phải là hôm nay hoặc một ngày đã qua.")
    start = int(datetime.combine(day, time(), TZ).timestamp())
    stop = min(int(observed.timestamp()), start + 86399)
    local_dir = worker_dir / "data" / "pickup-daily"
    local_dir.mkdir(parents=True, exist_ok=True)
    path = local_dir / f"{day}.json"

    with RunLock(path.with_suffix(".lock")):
        client, store = RealtimeClient(settings), DailyOrderStore(settings)
        try:
            client.deadline = monotonic() + 600
            store.cache_dir = worker_dir / "data"
            with ThreadPoolExecutor(max_workers=4) as pool:
                inventory = pool.submit(store.inventory)
                groups = pool.submit(client.routes)
                coverage = pool.submit(load_coverage, settings, cache_dir=store.cache_dir)
                raw = fetch_live_windows(client, "fm_hub/pickup_order/search", "ctime", start, stop,
                    {"fetch_total": 1, "fetch_list": 1}, ("pickup_order_id",),
                    initial_windows=[(left, min(left + 3599, stop)) for left in range(start, stop + 1, 3600)])
                orders = normalize_orders(raw, day, observed, settings.station_id, inventory.result()[1],
                                          groups.result()[0], coverage.result())
            payload = {"report_date": day.isoformat(), "observed_at": observed.isoformat(), "orders": orders}
            if args.publish:
                store.request("POST", "rpc/publish_pickup_daily_orders", {}, {"p_snapshot": payload})
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=local_dir, delete=False) as file:
                temporary = Path(file.name)
                json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
            temporary.replace(path)
            logging.info("Nguồn ngày %s: %s đơn, %s.", day, len(orders), dict(Counter(row["status"] for row in orders)))
        finally:
            client.close()
            store.close()


if __name__ == "__main__":
    main()
