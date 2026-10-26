"""Daily rider performance, matching launcher2's SPX report and website schema."""
from datetime import date, datetime
import json
import logging
import re

from .clients import SpxClient, WebsiteStore
from .transform import TZ

log = logging.getLogger(__name__)
TABLE = "driver_performance_daily"
METRICS = {
    0: {"delivery_assigned": "DAILY_VN_NUMBER_OF_PARCEL_ASSIGNED",
        "delivery_delivered": "DAILY_VN_NUMBER_OF_PARCEL_DELIVERED"},
    2: {"pickup_assigned": "DAILY_VN_NUMBER_OF_PICKUP_PARCEL_ASSIGNED",
        "pickup_picked": "DAILY_VN_NUMBER_OF_PICKUP_PARCEL_PICKED_UP"},
}
COUNTERS = tuple(key for fields in METRICS.values() for key in fields)
COT_COUNTERS = (
    "delivery_assigned_cot1", "delivery_delivered_cot1",
    "delivery_assigned_cot2", "delivery_delivered_cot2",
    "pickup_assigned_cot1", "pickup_picked_cot1",
    "pickup_assigned_cot2", "pickup_picked_cot2",
)
COLUMNS = ("performance_id", "report_date", "driver_id", "driver_name", "contract_type_name", "cot",
           *COUNTERS, *COT_COUNTERS, "delivery_success_rate", "pickup_success_rate", "cot_breakdown", "fetched_at")


def nonnegative_int(value, label):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"Performance: {label} phải là số nguyên không âm; chưa ghi dữ liệu.")
    return int(value)


class PerformanceClient(SpxClient):
    api_url = "https://spx.shopee.vn/api/driverservice/admin/performance/pickup/report/list"

    def session(self):
        session = super().session()
        session.headers["af-ac-enc-dat"] = ""
        return session

    def fetch_report(self, report_date, function_type):
        if function_type not in METRICS:
            raise ValueError("Performance function_type phải là 0 hoặc 2.")
        date.fromisoformat(report_date)
        payload = {"count": 100, "frequency": 1, "start_date": report_date,
                   "function_type": function_type, "pageno": 1}
        rows, seen, total, page_size = [], set(), None, None
        while True:
            data = self.post(payload)
            current_total = nonnegative_int(data["total"], "total")
            current_size = nonnegative_int(data.get("count", 100), "count")
            if current_size == 0:
                raise RuntimeError("Performance: kích thước trang bằng 0.")
            if total is None:
                total, page_size = current_total, current_size
            if current_total != total or current_size != page_size:
                raise RuntimeError("Performance thay đổi trong lúc phân trang; chạy lại, chưa ghi dữ liệu.")
            items = data["list"]
            expected = min(page_size, total - len(rows))
            if len(items) != expected:
                raise RuntimeError("Performance thiếu/thừa dòng phân trang; chưa ghi dữ liệu.")
            for item in items:
                driver_id = str(item.get("driver_id") or "").strip()
                if not driver_id or driver_id in seen:
                    raise RuntimeError("Performance thiếu/trùng mã rider; chưa ghi dữ liệu.")
                seen.add(driver_id)
                # Discard unrelated report fields before retaining data in memory/artifacts.
                rows.append({key: item.get(key) for key in
                             ("driver_id", "driver_name", "contract_type_name", "period_metric")})
            log.info("Performance type=%s trang=%s: %s/%s rider.", function_type, payload["pageno"], len(rows), total)
            if len(rows) == total:
                return rows
            payload["pageno"] += 1


def build_records(report_date, delivery, pickup, rider_cot_info=None, rider_cots=None):
    date.fromisoformat(report_date)
    rider_cot_info = rider_cot_info or rider_cots or {}
    merged = {}
    for function_type, items in ((0, delivery), (2, pickup)):
        if not isinstance(items, list):
            raise ValueError("Performance phải có hai danh sách delivery và pickup.")
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Performance rider phải là JSON object.")
            driver_id = str(item.get("driver_id") or "").strip()
            if not driver_id or driver_id in seen:
                raise ValueError("Performance thiếu/trùng mã rider.")
            seen.add(driver_id)
            metric = item.get("period_metric")
            if not isinstance(metric, dict):
                raise ValueError("Performance thiếu period_metric; chưa ghi dữ liệu.")
            info = rider_cot_info.get(driver_id)
            cot_label = info.get("cot", "") if isinstance(info, dict) else str(info or "")
            row = merged.setdefault(driver_id, {
                "performance_id": f"{report_date}|{driver_id}", "report_date": report_date,
                "driver_id": driver_id, "driver_name": "", "contract_type_name": "",
                "cot": cot_label,
                **dict.fromkeys(COUNTERS, 0),
                **dict.fromkeys(COT_COUNTERS, 0),
                "cot_breakdown": None,
            })
            for key in ("driver_name", "contract_type_name"):
                row[key] = str(item.get(key) or row[key]).strip()
            for key, source in METRICS[function_type].items():
                # A missing counter is malformed, not an observed zero.
                row[key] = nonnegative_int(metric.get(source), source)
    fetched_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    for row in merged.values():
        driver_id = str(row["driver_id"]).strip()
        info = rider_cot_info.get(driver_id)
        if isinstance(info, dict):
            row["cot"] = info.get("cot") or row.get("cot") or ""
            for k in COT_COUNTERS:
                row[k] = info.get(k, 0)
            breakdown = info.get("breakdown") or {}
            row["cot_breakdown"] = {kind:dict(breakdown.get(kind,{})) for kind in ("delivery","pickup")}
            quality = {}
            for kind, assigned, completed in (("delivery","delivery_assigned","delivery_delivered"),
                                               ("pickup","pickup_assigned","pickup_picked")):
                a1,a2 = row[f"{assigned}_cot1"],row[f"{assigned}_cot2"]
                c1,c2 = row[f"{completed}_cot1"],row[f"{completed}_cot2"]
                if a1+a2>row[assigned] or c1+c2>row[completed] or c1>a1 or c2>a2:
                    # Current order ownership and a daily rider report may cover different
                    # assignments. Never fabricate a proportional split to make them fit.
                    for key in (assigned,completed):
                        row[f"{key}_cot1"] = row[f"{key}_cot2"] = 0
                    row["cot_breakdown"][kind] = {}
                    quality[kind] = "source_scope_mismatch"
            if quality:
                row["cot_breakdown"]["quality"] = quality

        elif info:
            row["cot"] = str(info)
        row["delivery_success_rate"] = round(row["delivery_delivered"] / row["delivery_assigned"], 4) if row["delivery_assigned"] else None
        row["pickup_success_rate"] = round(row["pickup_picked"] / row["pickup_assigned"], 4) if row["pickup_assigned"] else None
        row["fetched_at"] = fetched_at
    return sorted(merged.values(), key=lambda row: row["driver_id"])


class PerformanceStore(WebsiteStore):
    tables = (TABLE,)

    def check(self):
        self.request("GET", TABLE, {"select": ",".join(COLUMNS), "limit": "0"})
        log.info("Đọc cấu trúc bảng %s: OK.", TABLE)

    def commit_snapshot(self, day, rows):
        if not rows:
            raise ValueError("Performance rỗng: giữ nguyên dữ liệu website.")
        ids = set()
        for row in rows:
            driver = str(row.get("driver_id") or "").strip()
            key = row.get("performance_id")
            if not driver or key != f"{day}|{driver}" or row.get("report_date") != day or key in ids:
                raise ValueError("Performance có ID/ngày không hợp lệ hoặc trùng rider.")
            for field in COUNTERS:
                nonnegative_int(row.get(field), field)
            ids.add(key)
        self.check()
        old_ids, offset = set(), 0
        scope = {"report_date": f"eq.{day}"}
        while True:
            batch = self.request("GET", TABLE, {**scope, "select": "performance_id", "order": "performance_id.asc",
                                                "limit": "500", "offset": str(offset)})
            old_ids.update(row["performance_id"] for row in batch)
            if len(batch) < 500:
                break
            offset += len(batch)
        for index in range(0, len(rows), self.settings.batch_size):
            self.request("POST", TABLE, {"on_conflict": "performance_id"}, rows[index:index + self.settings.batch_size])
        stale = sorted(old_ids - ids)
        for index in range(0, len(stale), 30):
            escaped = [json.dumps(key) for key in stale[index:index + 30]]
            self.request("DELETE", TABLE, {**scope, "performance_id": "in.(" + ",".join(escaped) + ")"})
        return {"upserted": len(rows), "stale_removed": len(stale)}


def run_performance(settings, day, *, dry_run, root, fixture=None, rider_cots=None, rider_orders=None):
    if fixture is None:
        client = PerformanceClient(settings)
        try:
            delivery = client.fetch_report(day, 0)
            pickup = client.fetch_report(day, 2)
        finally:
            client.close()
    else:
        if not dry_run:
            raise ValueError("Performance fixture chỉ được dùng với dry-run.")
        if not isinstance(fixture, dict):
            raise ValueError("Fixture performance phải có delivery và pickup.")
        delivery, pickup = fixture.get("delivery"), fixture.get("pickup")
    if fixture is None:
        from .performance_cot_cache import load_cots
        rider_cots = load_cots(settings, day, root, supplied_orders=rider_orders)
    elif rider_orders:
        from .cot_classifier import compute_rider_cot_breakdown
        rider_cots = compute_rider_cot_breakdown(rider_orders.get("delivery"),rider_orders.get("pickup"))
    rows = build_records(day, delivery, pickup, rider_cots=rider_cots)
    result = {"job": "performance", "date": day, "table": TABLE, "dry_run": dry_run,
              "stats": {"delivery_riders": len(delivery), "pickup_riders": len(pickup), "riders": len(rows),
                        **{key: sum(row[key] for row in rows) for key in COUNTERS}},
              "records": rows, "status": "prepared"}
    artifact = root / "data" / f"performance-{day}-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    def save():
        artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    if not dry_run:
        store = PerformanceStore(settings)
        try:
            result["sync"] = store.commit_snapshot(day, rows)
        finally:
            store.close()
    result["status"] = "dry_run" if dry_run else "synced"
    save()
    log.info("Performance hoàn tất: %s. Báo cáo: %s", result["stats"], artifact)
    return result
