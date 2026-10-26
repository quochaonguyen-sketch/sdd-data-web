"""Effective SPX pickup order state; never infer Assigned from point-level tasks."""
import re


def pickup_order_status(order):
    value = order.get("order_status")
    if value is None:
        value = order.get("status")
    token = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    if token in ("4", "picked", "pickedup", "fmhubpickuppicked", "fmhubpickupdone"):
        return "Picked up"
    if token in ("7", "inbound", "inbounded", "fmhubpickupparcelsreceived"):
        return "Inbound"
    if "onhold" in token or token in ("6", "37"):
        return "Pickup Onhold"
    if token in ("0", "created", "fmhubpickupcreated", "2", "assigned", "fmhubpickupassigned"):
        hold = any(order.get(key) not in (None, "", 0, "0") for key in ("pickup_on_hold_reason", "on_hold_reason"))
        hold = hold or bool(str(order.get("pickup_on_hold_reason_name") or "").strip())
        try:
            attempted = int(order.get("pickup_attempts") or 0) > 0
        except (TypeError, ValueError):
            return "Unknown"
        if hold or token in ("0", "created", "fmhubpickupcreated") and attempted:
            return "Pickup Onhold"
        try:
            if int(order.get("driver_pickedup_time") or 0) > 0:
                return "Picked up"
        except (TypeError, ValueError):
            return "Unknown"
        return "Assigned" if token in ("2", "assigned", "fmhubpickupassigned") else "Created"
    return "Unknown"
