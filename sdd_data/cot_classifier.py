from datetime import datetime, time
import logging

from .transform import TZ, as_datetime

log = logging.getLogger(__name__)


def classify_delivery_order_cot(order: dict) -> str:
    """Phân loại COT cho một đơn delivery:
    - delivering_time trước 10:00: COT 1.1
    - delivering_time từ 10:00 đến 14:00: COT 1.2
    - delivering_time từ 14:00 trở đi: COT 2
    """
    dt = as_datetime(order.get("delivering_time") or order.get("delivered_time"))
    if dt is not None:
        t = dt.time()
        if t < time(10, 0):
            return "COT 1.1"
        if time(10, 0) <= t < time(14, 0):
            return "COT 1.2"
        return "COT 2"
    
    recv_dt = as_datetime(order.get("current_station_received_time"))
    if recv_dt is not None:
        t = recv_dt.time()
        if t < time(10, 0):
            return "COT 1.1"
        if t < time(14, 0):
            return "COT 1.2"
        return "COT 2"
    return ""


def classify_pickup_order_cot(order: dict) -> str:
    """Phân loại COT cho một đơn pickup dựa trên mốc 12:00:
    - Trước 12:00: COT 1
    - Từ 12:00 trở đi: COT 2
    """
    pick_dt = as_datetime(order.get("pickup_time"))
    if pick_dt is not None:
        return "COT 1" if pick_dt.time() < time(12, 0) else "COT 2"
    return ""


def compute_rider_cot_breakdown(delivery_orders: list[dict] | None = None,
                                pickup_orders: list[dict] | None = None,
                                catalog_cots: dict[str, str] | None = None) -> dict[str, dict]:
    """Bóc tách chính xác sản lượng theo từng COT (COT 1 vs COT 2) cho từng rider từ đơn hàng Volume:
    - delivery_assigned_cot1 / delivery_delivered_cot1
    - delivery_assigned_cot2 / delivery_delivered_cot2
    - pickup_assigned_cot1 / pickup_picked_cot1
    - pickup_assigned_cot2 / pickup_picked_cot2
    - cot: Phân loại COT tổng thể của rider
    - cot_breakdown: Chi tiết từng nhóm COT (COT 1.1, COT 1.2, COT 1, COT 2)
    """
    delivery_orders = delivery_orders or []
    pickup_orders = pickup_orders or []
    catalog_cots = catalog_cots or {}

    stats: dict[str, dict] = {}

    def get_rider_stat(driver_id: str):
        if driver_id not in stats:
            stats[driver_id] = {
                "cot": catalog_cots.get(driver_id, ""),
                "delivery_assigned_cot1": 0,
                "delivery_delivered_cot1": 0,
                "delivery_assigned_cot2": 0,
                "delivery_delivered_cot2": 0,
                "pickup_assigned_cot1": 0,
                "pickup_picked_cot1": 0,
                "pickup_assigned_cot2": 0,
                "pickup_picked_cot2": 0,
                "earliest_delivery_dt": None,
                "earliest_delivery_cot": "",
                "earliest_pickup_dt": None,
                "earliest_pickup_cot": "",
                "breakdown": {
                    "delivery": {},
                    "pickup": {},
                }
            }
        return stats[driver_id]

    # Xử lý Delivery
    for order in delivery_orders:
        driver_id = str(order.get("driver_id") or "").strip()
        if not driver_id:
            continue
        cot = classify_delivery_order_cot(order)
        if not cot:
            continue
        item = get_rider_stat(driver_id)
        dt = as_datetime(order.get("delivering_time") or order.get("delivered_time") or
                         order.get("current_station_received_time"))
        if dt:
            if item["earliest_delivery_dt"] is None or dt < item["earliest_delivery_dt"]:
                item["earliest_delivery_dt"] = dt
                item["earliest_delivery_cot"] = cot

        is_delivered = bool(order.get("delivered_time") or order.get("order_status") in (4, "4"))
        detail = item["breakdown"]["delivery"].setdefault(cot, {"assigned": 0, "delivered": 0})
        detail["assigned"] += 1
        if is_delivered:
            detail["delivered"] += 1

        if cot in ("COT 1.1", "COT 1.2", "COT 1"):
            item["delivery_assigned_cot1"] += 1
            if is_delivered:
                item["delivery_delivered_cot1"] += 1
        elif cot == "COT 2":
            item["delivery_assigned_cot2"] += 1
            if is_delivered:
                item["delivery_delivered_cot2"] += 1

    # Xử lý Pickup
    for order in pickup_orders:
        driver_id = str(order.get("driver_id") or "").strip()
        if not driver_id:
            continue
        cot = classify_pickup_order_cot(order)
        if not cot:
            continue
        item = get_rider_stat(driver_id)
        dt = as_datetime(order.get("pickup_time"))
        if dt:
            if item["earliest_pickup_dt"] is None or dt < item["earliest_pickup_dt"]:
                item["earliest_pickup_dt"] = dt
                item["earliest_pickup_cot"] = cot

        is_picked = dt is not None
        detail = item["breakdown"]["pickup"].setdefault(cot, {"assigned": 0, "picked": 0})
        detail["assigned"] += 1
        if is_picked:
            detail["picked"] += 1

        if cot == "COT 1":
            item["pickup_assigned_cot1"] += 1
            if is_picked:
                item["pickup_picked_cot1"] += 1
        elif cot == "COT 2":
            item["pickup_assigned_cot2"] += 1
            if is_picked:
                item["pickup_picked_cot2"] += 1

    # Xác định COT phân loại chính cho mỗi rider
    for driver_id, default_cot in catalog_cots.items():
        if driver_id not in stats:
            get_rider_stat(driver_id)

    for driver_id, item in stats.items():
        if catalog_cots.get(driver_id):
            item["cot"] = catalog_cots[driver_id]
        elif item["earliest_delivery_dt"]:
            t = item["earliest_delivery_dt"].time()
            if t < time(14, 0):
                item["cot"] = "COT 1"
            else:
                item["cot"] = "COT 2"
        elif item["earliest_pickup_cot"]:
            item["cot"] = item["earliest_pickup_cot"]
        elif not item["cot"]:
            item["cot"] = catalog_cots.get(driver_id, "")

    return stats


def classify_rider_cots(delivery_orders: list[dict] | None = None,
                        pickup_orders: list[dict] | None = None,
                        catalog_cots: dict[str, str] | None = None) -> dict[str, str]:
    """Helper đơn giản trả về {driver_id: cot_label}."""
    breakdown = compute_rider_cot_breakdown(delivery_orders, pickup_orders, catalog_cots)
    return {driver_id: data["cot"] for driver_id, data in breakdown.items() if data.get("cot")}
