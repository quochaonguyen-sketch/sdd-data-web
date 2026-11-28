"""Fill Delivery Backlog parcel measurements and archive them in Cloudflare D1."""

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import logging
from math import isfinite
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic

from parcel_measurements import add_measurements

JOB_DIR = Path(__file__).resolve().parent
ROOT = JOB_DIR.parent


def d1(*args, query=False):
    result = subprocess.run(
        ["node", str(JOB_DIR / "node_modules/wrangler/bin/wrangler.js"), "d1", "execute",
         "BACKLOG_HISTORY", "--config", str(JOB_DIR / "wrangler.jsonc"), "--remote", *args],
        cwd=ROOT, check=True, stdout=subprocess.PIPE if query else None,
        encoding="utf-8", timeout=120,
    )
    if query:
        return json.loads(result.stdout)[0]["results"]


def read(sql):
    return d1("--json", "--command", sql, query=True)


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def checkpoint_ids(day):
    rows = read(f"""select s.scheduled_for, s.cutoff, s.order_count, c.payload
                    from delivery_checkpoint_snapshots s
                    left join delivery_checkpoint_chunks c using (scheduled_for)
                    where s.work_date={quote(day)} order by s.scheduled_for, c.chunk_no""")
    covered = {}
    for row in rows:
        manifest = covered.setdefault(row["cutoff"], (row["scheduled_for"], row["order_count"], []))
        if row["payload"]:
            manifest[2].extend(order["shipment_id"] for order in json.loads(row["payload"]))
    if set(covered) != {"13:30", "17:30"}:
        raise RuntimeError(f"Delivery {day} cần đủ hai snapshot 13:30 và 17:30 trên Cloudflare.")
    for _, count, ids in covered.values():
        if len(ids) != count or len(set(ids)) != count or any(not identity for identity in ids):
            raise RuntimeError("Snapshot Delivery có mã trùng hoặc thiếu.")
    observed = datetime.fromisoformat(covered["17:30"][0].replace("Z", "+00:00"))
    return sorted(set(covered["13:30"][2]) | set(covered["17:30"][2])), observed


def live_ids(settings):
    from sdd_data.delivery_inventory import InventoryStore

    db = InventoryStore(settings)
    try:
        def head():
            return db.request("GET", "lmhub_inventory_rows", {
                "select": "snapshot_id,snapshot_at", "order": "snapshot_at.desc", "limit": "1",
            })

        latest = head()
        if not latest:
            raise RuntimeError("Delivery Backlog chưa có snapshot hiện tại.")
        ids = []
        while True:
            batch = db.request("GET", "lmhub_inventory_rows", {
                "select": "shipment_id", "snapshot_id": "eq." + latest[0]["snapshot_id"],
                "order": "id.asc", "limit": "500", "offset": str(len(ids)),
            })
            ids.extend(row["shipment_id"] for row in batch)
            if len(batch) < 500:
                break
        if latest != head() or not ids or len(set(ids)) != len(ids) or any(not identity for identity in ids):
            raise RuntimeError("Snapshot Delivery hiện tại đã đổi hoặc thiếu mã, hãy thử lại.")
        return ids, datetime.fromisoformat(latest[0]["snapshot_at"])
    finally:
        db.close()


def save(scope, observed, orders):
    stamp = observed.isoformat()
    key = quote(scope)
    statements = [
        f"delete from delivery_measurement_runs where scope={key} and observed_at<={quote(stamp)};",
        f"insert into delivery_measurement_runs values ({key},{quote(stamp)},{len(orders)}) on conflict do nothing;",
    ]
    for index in range(0, len(orders), 300):
        values = []
        for row in orders[index:index + 300]:
            weight, length = (str(row.get(field) or "null") for field in ("weight_kg", "length_cm"))
            values.append(f"({key},{quote(row['order_id'])},{weight},{length})")
        statements.append(f"""insert into delivery_measurements
            select * from (values {','.join(values)})
            where exists(select 1 from delivery_measurement_runs
                         where scope={key} and observed_at={quote(stamp)})
            on conflict do nothing;""")
    with TemporaryDirectory(prefix="sdd-delivery-sizes-") as folder:
        file = Path(folder) / "measurements.sql"
        file.write_text("\n".join(statements), encoding="utf-8")
        d1("--file", str(file), "--yes")
    [stored] = read(f"""select r.order_count, count(m.shipment_id) as checked,
         count(m.weight_kg) as weight
         from delivery_measurement_runs r left join delivery_measurements m using(scope)
         where r.scope={key} group by r.scope""")
    if stored["checked"] != stored["order_count"]:
        raise RuntimeError("Cloudflare chưa lưu đủ mã đo Delivery.")
    logging.info("Đã xác minh Cloudflare Delivery %s: %s.", scope, stored)


def save_live_cache(path, orders, observed):
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
        staged = Path(target.name)
        json.dump({"observed_at": observed.isoformat(), "orders": orders}, target, ensure_ascii=False)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def read_live_list_weights(settings, cache_path, ids):
    from sdd_data.clients import SpxClient
    from sdd_data.delivery_inventory import InventoryClient

    class WeightClient(InventoryClient):
        def post(self, payload):
            if monotonic() > self.deadline:
                raise RuntimeError("Đọc số đo từ danh sách Delivery đã quá thời gian.")
            self.session().headers["af-ac-enc-dat"] = ""
            result = SpxClient.post(self, payload)
            result["list"] = [{key: row.get(key) for key in ("shipment_id", "input_weight")}
                              for row in result["list"]]
            return result

    client = WeightClient(replace(settings, inventory_workers=min(8, settings.delivery_workers)))
    try:
        rows = client.fetch_inventory(cache_path=cache_path)
        weights = {}
        for row in rows:
            weight = row["input_weight"]
            if row["shipment_id"] in ids and type(weight) in (int, float) and isfinite(weight) and weight > 0:
                weights[row["shipment_id"]] = weight
        return weights
    finally:
        client.close()


def capture(settings, scope, ids, observed, live_cache=None, refresh=False):
    from sdd_data.pickup_realtime import RealtimeClient

    orders = [{"order_id": identity} for identity in ids]
    to_read = orders
    if live_cache and live_cache.is_file():
        saved = json.loads(live_cache.read_text(encoding="utf-8"))
        previous = {row["order_id"]: row for row in saved["orders"]}
        to_read = []
        for order in orders:
            cached = previous.get(order["order_id"])
            if cached:
                order.update(cached)
                checked_at = datetime.fromisoformat(cached["checked_at"])
                if not refresh and (cached.get("weight_kg") or timedelta() <= observed - checked_at < timedelta(hours=2)):
                    continue
            to_read.append(order)
        if not to_read and set(previous) == set(ids):
            logging.info("Delivery hiện tại đã có số đo cập nhật cho %s mã.", len(ids))
            return
    if live_cache:
        if to_read:
            weights = read_live_list_weights(settings, live_cache.with_name("delivery-size-window-plan.json"),
                                             {order["order_id"] for order in to_read})
            for order in to_read:
                if order["order_id"] in weights:
                    order["weight_kg"] = weights[order["order_id"]]
            logging.info("Delivery: trọng lượng có sẵn trên API danh sách cho %s/%s mã cần cập nhật.",
                         len(weights), len(to_read))
            if not any(order.get("weight_kg") for order in orders):
                raise RuntimeError("API danh sách không trả cân nặng cho phiên Delivery hiện tại.")
    else:
        previous = {row["shipment_id"]: row for row in read(f"""select shipment_id, weight_kg, length_cm
                    from delivery_measurements where scope={quote(scope)}""")}
        for order in orders:
            saved = previous.get(order["order_id"])
            if saved:
                order.update(weight_kg=saved["weight_kg"], length_cm=saved["length_cm"])
        to_read = [order for order in orders if order.get("weight_kg") is None]
        if to_read:
            client = RealtimeClient(settings)
            try:
                outcomes = add_measurements(client, to_read, observed, max(1, min(6, settings.inventory_workers)),
                                            include_length=False)
            finally:
                client.close()
            all_denied = outcomes["http_403"] + outcomes["permission"] == len(to_read)
            if (all_denied and not previous) or any(outcomes[key] for key in ("session", "deadline", "retry_exhausted", "invalid")):
                raise RuntimeError(f"Lượt đọc Delivery {scope} chưa hoàn tất, có thể chạy lại.")
    save(scope, observed, orders)
    if live_cache:
        for order in to_read:
            order["checked_at"] = observed.isoformat()
        save_live_cache(live_cache, orders, observed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-dir", type=Path, default=ROOT)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--only", choices=("live", "history"))
    parser.add_argument("--scheduled-live", action="store_true")
    parser.add_argument("--refresh-live", action="store_true")
    parser.add_argument("--all-history", action="store_true")
    args = parser.parse_args()
    worker_dir = args.worker_dir.resolve(strict=True)
    sys.path.insert(0, str(worker_dir))
    from sdd_data.config import Settings
    from sdd_data.runner import RunLock, configure_logging
    from sdd_data.transform import TZ

    configure_logging(worker_dir)
    settings = Settings.load(worker_dir)
    today = datetime.now(TZ)
    day = args.date or today.date()
    if day > today.date() or (args.all_history and args.date):
        parser.error("Ngày không hợp lệ.")
    if args.only == "live" and args.scheduled_live and not 6 <= today.hour < 23:
        return
    with RunLock(worker_dir / "data/delivery-backlog-measurements.lock"):
        if args.only != "live":
            days = [day.isoformat()]
            if args.all_history:
                days = [row["work_date"] for row in read("""select work_date from delivery_checkpoint_snapshots
                         group by work_date having count(*) = 2 order by work_date""")]
            for report_day in days:
                capture(settings, report_day, *checkpoint_ids(report_day))
        if args.only != "history":
            capture(settings, "live", *live_ids(settings), live_cache=worker_dir / "data/delivery-measurements-live.json",
                    refresh=args.refresh_live)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.error("Delivery Backlog: %s", error)
        sys.exit(1)
