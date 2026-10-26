from datetime import datetime
import unittest

from sdd_data.cot_classifier import classify_rider_cots,classify_pickup_order_cot,compute_rider_cot_breakdown
from sdd_data.transform import TZ


def make_ts(hour, minute):
    return int(datetime(2026, 10, 24, hour, minute, tzinfo=TZ).timestamp())


class CotClassifierTest(unittest.TestCase):
    def test_pickup_uses_actual_picked_time_at_noon_boundary_in_vietnam(self):
        noon=make_ts(12,0)
        for stamp,expected in ((noon-1,"COT 1"),(noon,"COT 2"),(noon+1,"COT 2"),(noon*1000,"COT 2")):
            self.assertEqual(classify_pickup_order_cot({"pickup_time":stamp,"create_time":make_ts(8,0),"delivering_time":make_ts(8,0)}),expected)
        self.assertEqual(classify_pickup_order_cot({"pickup_time":make_ts(11,0),"create_time":make_ts(15,0),"delivered_time":make_ts(16,0)}),"COT 1")

    def test_missing_picked_time_does_not_fall_back_to_creation_or_delivery(self):
        for value in (None,0,"0","invalid"):
            order={"driver_id":"A","pickup_time":value,"create_time":make_ts(8,0),"delivering_time":make_ts(14,0),"delivered_time":make_ts(16,0),"order_status":"4"}
            self.assertEqual(classify_pickup_order_cot(order),"")
            self.assertEqual(compute_rider_cot_breakdown(pickup_orders=[order]),{})

    def test_delivery_cot1_1_before_10h(self):
        # Rider giao luc 8h30 (< 10h) -> COT 1
        orders = [{"driver_id": "R001", "delivering_time": make_ts(8, 30)}]
        res = classify_rider_cots(delivery_orders=orders)
        self.assertEqual(res.get("R001"), "COT 1")

    def test_delivery_cot1_2_from_10h(self):
        # Rider giao luc 10h30 (>= 10h va < 14h) -> COT 1
        orders = [{"driver_id": "R002", "delivering_time": make_ts(10, 30)}]
        res = classify_rider_cots(delivery_orders=orders)
        self.assertEqual(res.get("R002"), "COT 1")

    def test_delivery_cot1_2_midday(self):
        # Rider giao luc 12h30 -> COT 1
        orders = [{"driver_id": "R003", "delivering_time": make_ts(12, 30)}]
        res = classify_rider_cots(delivery_orders=orders)
        self.assertEqual(res.get("R003"), "COT 1")

    def test_delivery_cot2_afternoon(self):
        # Rider di giao sau 14h -> COT 2
        orders = [{"driver_id": "R004", "delivering_time": make_ts(15, 0)}]
        res = classify_rider_cots(delivery_orders=orders)
        self.assertEqual(res.get("R004"), "COT 2")

    def test_pickup_cot1_before_12h(self):
        # Pick truoc 12h -> COT 1
        orders = [{"driver_id": "R005", "pickup_time": make_ts(11, 30)}]
        res = classify_rider_cots(pickup_orders=orders)
        self.assertEqual(res.get("R005"), "COT 1")

    def test_pickup_cot2_from_12h(self):
        # Pick tu 12h tro di -> COT 2
        orders = [{"driver_id": "R006", "pickup_time": make_ts(12, 0)}]
        res = classify_rider_cots(pickup_orders=orders)
        self.assertEqual(res.get("R006"), "COT 2")

    def test_fallback_to_catalog_cot(self):
        # Rider khong co don volume -> fallback catalog
        res = classify_rider_cots(catalog_cots={"R009": "COT2"})
        self.assertEqual(res.get("R009"), "COT2")


if __name__ == "__main__":
    unittest.main()
