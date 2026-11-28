"""Chốt các đơn Pickup đang OH và xếp hạng theo rider thực hiện OH."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import json
import logging
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from time import monotonic

from parcel_measurements import add_measurements, parcel_measurements


VN = timezone(timedelta(hours=7))
CUTOFF = {"COT1": time(13), "COT2": time(17)}


def bounds(day, cot):
    if cot == "COT1":
        return (datetime.combine(day - timedelta(days=1), time(15), VN),
                datetime.combine(day, time(10, 30), VN))
    return datetime.combine(day, time(10, 30), VN), datetime.combine(day, time(15), VN)


def field(row, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() not in ("", "0"):
            return str(value).strip()
    return ""


def is_pickup_hold(row):
    status = str(row.get("order_status", row.get("status")))
    code = field(row, "pickup_on_hold_reason", "on_hold_reason")
    label = field(row, "pickup_on_hold_reason_name", "on_hold_reason_name")
    attempted = field(row, "pickup_attempts")
    return status == "37" or status in ("0", "2") and bool(code or label or attempted.isdecimal())


def summarize(histories, cot, observed, parse_time):
    start, end = bounds(observed.date(), cot)
    selected, names = [], {}
    missing_history = 0
    for order_id, history in histories:
        latest = None
        has_hold = False
        for action in history:
            if action.get("pickup_result") != "pickup on_hold":
                continue
            held_at = parse_time(action.get("ctime"))
            if held_at is None:
                raise RuntimeError("Thao tác OH trên SPX thiếu giờ hợp lệ.")
            has_hold = True
            if not start <= held_at < end:
                continue
            rider_id = field(action, "pickup_driver") or None
            name = field(action, "pickup_driver_name")
            if rider_id and name:
                names[rider_id] = name
            event = {"order_id": order_id, "onhold_at": held_at.isoformat(), "rider_id": rider_id,
                     "reason": field(action, "on_hold_reason") or "Chưa rõ lý do"}
            if latest and latest[0] == held_at and latest[1] != event:
                raise RuntimeError("SPX trả thông tin OH khác nhau cho cùng đơn và thời điểm.")
            if latest is None or latest[0] < held_at:
                latest = held_at, event
        missing_history += not has_hold
        if latest:
            selected.append(latest[1])
    selected.sort(key=lambda event: (event["onhold_at"], event["order_id"]))
    by_rider = defaultdict(Counter)
    for event in selected:
        by_rider[event["rider_id"]][event["reason"]] += 1
    ranking = [
        {"rider_id": rider, "rider_name": names.get(rider) or rider,
         "onhold_orders": counts.total(), "reasons": dict(counts.most_common())}
        for rider, counts in by_rider.items() if rider is not None
    ]
    ranking.sort(key=lambda rider: (-rider["onhold_orders"], rider["rider_id"]))
    return {
        "date": observed.date().isoformat(),
        "cot": cot,
        "cutoff_at": datetime.combine(observed.date(), CUTOFF[cot], VN).isoformat(),
        "source_started_at": observed.isoformat(),
        "onhold_from": start.isoformat(),
        "onhold_before": end.isoformat(),
        "onhold_orders": len(selected),
        "source_orders_missing_onhold_history": missing_history,
        "orders_missing_rider": by_rider[None].total(),
        "reasons": dict(Counter(event["reason"] for event in selected).most_common()),
        "top_riders": [rider for rider in ranking if rider["onhold_orders"] == ranking[0]["onhold_orders"]],
        "riders": ranking,
        "orders": selected,
    }


def read_report(worker_dir, cot, now):
    from sdd_data.config import Settings
    from sdd_data.pickup_realtime import RealtimeClient
    from sdd_data.transform import as_datetime

    settings = Settings.load(worker_dir)
    client = RealtimeClient(settings)
    try:
        client.cache_dir = worker_dir / "data"
        client.deadline = monotonic() + 480
        rows = client.pending_orders(now)
        order_ids = {field(row, "pickup_order_id") for row in rows if is_pickup_hold(row)}
        if "" in order_ids:
            raise RuntimeError("Đơn OH thiếu mã Pickup.")
        logging.info("Pickup On Hold %s: đã đọc %s dòng, kiểm tra lịch sử %s đơn.", cot, len(rows), len(order_ids))

        def history(order_id):
            detail = client.get("order/detail/pickup_info", {"shipment_id": order_id, "station_type": 12})
            info = detail.get("pickup_info_list")
            if detail.get("shipment_id") != order_id or not isinstance(info, list):
                raise RuntimeError("SPX trả lịch sử Pickup không hợp lệ.")
            events = [event for item in info for event in (item.get("pickup_order_tracking") or [])]
            return order_id, events

        with ThreadPoolExecutor(max_workers=min(6, settings.inventory_workers)) as pool:
            report = summarize(pool.map(history, sorted(order_ids)), cot, now, as_datetime)
        if order_ids and report["source_orders_missing_onhold_history"] == len(order_ids):
            raise RuntimeError("SPX có đơn OH nhưng chưa trả được thao tác OH trong lịch sử Pickup.")
        add_measurements(client, report["orders"], now, max(1, min(6, settings.inventory_workers)))
        return report
    finally:
        client.close()


def save_report(path, report):
    staged = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            staged = Path(output.name)
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def queue_saved_reports(worker_dir, folder):
    import requests
    from sdd_data.config import Settings

    pending = [path for path in sorted(folder.glob("*-COT[12].json")) if not path.with_suffix(".queued").exists()]
    if not pending:
        return
    settings = Settings.load(worker_dir)
    key = settings.supabase_key
    if not settings.supabase_url or not key:
        raise RuntimeError("Thiếu cấu hình Supabase để lưu Pickup On Hold lên Cloudflare.")
    headers = {"apikey": key, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    with requests.Session() as session:
        for path in pending:
            report = json.loads(path.read_text(encoding="utf-8"))
            orders = report.pop("orders")
            if path.stem != f"{report['date']}-{report['cot']}" or len(orders) != report["onhold_orders"]:
                raise RuntimeError(f"Báo cáo Pickup OH không khớp ngày, COT hoặc số đơn: {path.name}")
            payload = {"cutoff_at": report["cutoff_at"], "report_date": report["date"], "cot": report["cot"],
                       "order_count": len(orders), "summary": report,
                       "chunks": [json.dumps(orders[i:i + 200], ensure_ascii=False, separators=(",", ":"))
                                  for i in range(0, len(orders), 200)]}
            response = session.post(f"{settings.supabase_url}/rest/v1/pickup_onhold_outbox",
                                    params={"on_conflict": "cutoff_at"}, headers=headers, json=payload,
                                    timeout=40, allow_redirects=False)
            if response.status_code not in (200, 201, 204):
                raise RuntimeError(f"Hàng đợi lưu Pickup OH trả HTTP {response.status_code}.")
            path.with_suffix(".queued").touch()
            logging.info("Đã xếp hàng lưu Cloudflare: %s.", path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cot", choices=CUTOFF)
    parser.add_argument("--worker-dir", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Đọc SPX và in số liệu kiểm tra, không lưu báo cáo.")
    mode.add_argument("--archive-only", action="store_true", help="Xếp hàng lưu các file báo cáo đã có lên Cloudflare.")
    mode.add_argument("--measurements-only", type=date.fromisoformat, metavar="YYYY-MM-DD",
                      help="Bổ sung số đo SPX cho đúng tập mã đơn đã chốt, theo thời điểm bắt đầu phiên.")
    args = parser.parse_args()
    if not args.archive_only and args.cot is None:
        parser.error("Cần --cot để đọc SPX.")
    worker_dir = args.worker_dir.resolve(strict=True)
    sys.path.insert(0, str(worker_dir))
    from sdd_data.runner import RunLock, configure_logging

    configure_logging(worker_dir)
    folder = worker_dir / "data" / "pickup-onhold"
    if args.archive_only:
        queue_saved_reports(worker_dir, folder)
        return

    now = datetime.now(VN)
    if args.measurements_only:
        from sdd_data.config import Settings
        from sdd_data.pickup_realtime import RealtimeClient

        path = folder / f"{args.measurements_only}-{args.cot}.json"
        with RunLock(path.with_suffix(".lock")):
            report = json.loads(path.read_text(encoding="utf-8"))
            observed = datetime.fromisoformat(report["source_started_at"])
            settings = Settings.load(worker_dir)
            client = RealtimeClient(settings)
            try:
                add_measurements(client, report["orders"], observed, max(1, min(6, settings.inventory_workers)))
            finally:
                client.close()
            save_report(path, report)
        return

    cutoff = datetime.combine(now.date(), CUTOFF[args.cot], VN)
    if not args.dry_run and not cutoff <= now < cutoff + timedelta(hours=1):
        parser.error("Chỉ lưu báo cáo trong một giờ kể từ mốc chạy. Dùng --dry-run để kiểm tra nguồn.")

    path = folder / f"{now.date().isoformat()}-{args.cot}.json"
    if args.dry_run:
        report = read_report(worker_dir, args.cot, now)
    else:
        with RunLock(path.with_suffix(".lock")):
            if path.exists():
                report = json.loads(path.read_text(encoding="utf-8"))
            else:
                report = read_report(worker_dir, args.cot, now)
                save_report(path, report)
        queue_saved_reports(worker_dir, folder)
        print(path)
    print(json.dumps({key: report[key] for key in
                      ("date", "cot", "onhold_orders", "source_orders_missing_onhold_history", "orders_missing_rider")},
                     ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.error("Pickup On Hold: %s", error)
        sys.exit(1)
