"""Read SPX shipment measurements as of a snapshot timestamp."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import logging
from math import isfinite
from time import monotonic

SPX_MEASUREMENTS = "https://spx.shopee.vn/api/fleet_order/order/detail/get_measurement_list"


def parcel_measurements(records, observed, *, include_length=True):
    def latest_value(key, divisor):
        for stamp, record in ordered:
            value = record.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0:
                return value / divisor
        return None

    ordered = sorted(
        ((row["operate_time"], row) for row in records if isinstance(row, dict)
         and type(row.get("operate_time")) is int and 0 < row["operate_time"] <= observed.timestamp()),
        key=lambda entry: entry[0], reverse=True,
    )
    result = {"weight_kg": latest_value("weight", 1000)}
    if include_length:
        result["length_cm"] = latest_value("length", 1)
    return result


def add_measurements(client, orders, observed, workers, *, include_length=True):
    import requests
    from threading import Event
    from time import sleep

    session_expired = Event()
    deadline = monotonic() + 600

    def load(order):
        if order.get("weight_kg") is not None and (not include_length or order.get("length_cm") is not None):
            return "cached", {}
        order_id = order["order_id"]
        for attempt in range(3):
            if session_expired.is_set():
                return "session", {}
            if monotonic() >= deadline:
                return "deadline", {}
            try:
                response = client.session().get(SPX_MEASUREMENTS, params={"shipment_ids": order_id, "station_type": 12},
                                                timeout=10, allow_redirects=False)
                if response.status_code == 401:
                    session_expired.set()
                    return "session", {}
                if response.status_code == 403 or response.is_redirect:
                    return f"http_{response.status_code}", {}
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < 2:
                        sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    return "invalid", {}
                code = str(body.get("retcode"))
                if code in ("401", "403") or body.get("is_login") is False:
                    session_expired.set()
                    return "session", {}
                if code == "90309999":
                    if attempt < 2:
                        sleep(2 ** attempt)
                    continue
                if code == "200301006":
                    return "permission", {}
                data = body.get("data")
                if code != "0" or not isinstance(data, dict) or not isinstance(data.get("list"), list):
                    return "invalid", {}
                for row in data["list"]:
                    if isinstance(row, dict) and row.get("shipment_id") == order_id and isinstance(row.get("measure_infos"), list):
                        values = parcel_measurements(row["measure_infos"], observed, include_length=include_length)
                        return ("measured" if any(values.values()) else "empty"), values
                return "empty", {}
            except (requests.RequestException, ValueError):
                if attempt < 2:
                    sleep(2 ** attempt)
        return "retry_exhausted", {}

    outcomes = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (order, (outcome, values)) in enumerate(zip(orders, pool.map(load, orders)), 1):
            outcomes[outcome] += 1
            for key, value in values.items():
                if order.get(key) is None and value is not None:
                    order[key] = value
            if index % 250 == 0:
                logging.info("SPX số đo: %s/%s mã đã kiểm.", index, len(orders))
    logging.info("SPX kiểm tra số đo: %s.", dict(outcomes))
    logging.info("SPX: có trọng lượng cho %s/%s đơn.",
                 sum(order.get("weight_kg") is not None for order in orders), len(orders))
    if include_length:
        logging.info("SPX: có chiều dài cho %s/%s đơn.",
                     sum(order.get("length_cm") is not None for order in orders), len(orders))
    return outcomes
