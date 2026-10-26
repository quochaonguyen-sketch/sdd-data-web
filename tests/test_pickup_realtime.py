from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from sdd_data.pickup_realtime import RealtimeClient, build_realtime, pending_inventory
from sdd_data.transform import TZ

AREAS = ("KV1", "KV2", "KV3", "KV4", "KV5")


class PickupRealtimeTests(unittest.TestCase):
    def test_full_fleet_and_shared_riders_keep_actual_quantities(self):
        profiles = [{"rider_code": str(i), "kv": f"KV{i}", "name": f"Rider {i}"} for i in range(1, 6)]
        rows = [{"shipment_id": "order-a", "pickup_point_id": "p1", "assigned_riders_today": "[COT1] 1-A; [COT2] 2-B; [COT1] 1-A", "area": "KV1", "ward": "Ward A", "zone_name": "ZONE-A", "status": "Assigned"},
                {"shipment_id": "order-b", "pickup_point_id": "p2", "assigned_riders_today": "[COT1] 2-B", "area": "KV2", "ward": "Ward B", "zone_name": "ZONE-B", "status": "Pickup Onhold"}]
        tasks = [{"driver_id": str(i), "pickup_point_id": f"p{i}", "pickup_quantity": i * 10} for i in range(1, 6)]
        records, groups, stats = build_realtime(profiles, rows, tasks, {f"p{i}": f"ZONE-{i}" for i in range(1, 6)}, AREAS)
        by_id = {r["driver_id"]: r for r in records}
        self.assertEqual(len(records), 5)
        self.assertEqual(by_id["1"]["assigned_orders"], 11)
        self.assertEqual(by_id["2"]["assigned_orders"], 22)
        self.assertEqual(by_id["2"]["onhold_orders"], 1)
        self.assertEqual(stats["areas"]["KV4"]["picked"], 40)
        for record in records:
            detail = [g for g in groups if g["driver_id"] == record["driver_id"]]
            self.assertEqual(sum(g["picked_orders"] for g in detail), record["total_pickup_quantity"])
            self.assertEqual(sum(g["assigned_orders"] for g in detail), record["assigned_orders"])
        self.assertIsNone(next(g for g in groups if g["driver_id"] == "5")["ward_area"])

    def test_unknown_id_is_not_matched_by_name_and_negative_quantity_rejected(self):
        profiles = [{"rider_code": "1", "name": "Same", "kv": "KV1"}]
        records, _, stats = build_realtime(profiles, [], [{"driver_id": "other", "driver_name": "Same", "pickup_quantity": 100}], {}, AREAS)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["total_pickup_quantity"], 0)
        self.assertEqual(records[0]["task_count"], 0)
        self.assertEqual(stats["tasks_outside_roster"], 1)
        with self.assertRaises(RuntimeError):
            build_realtime(profiles, [], [{"driver_id": "1", "pickup_quantity": -1}], {}, AREAS)

    def test_onhold_is_derived_from_live_order_flags_and_new_cot2_uses_cot2_rider(self):
        now = datetime(2026, 10, 24, 15, tzinfo=TZ)
        at = lambda hour: int(datetime(2026, 10, 24, hour, tzinfo=TZ).timestamp())
        tasks = [{"driver_id": "1", "pickup_point_id": "p", "accepted_time": at(10)},
                 {"driver_id": "2", "pickup_point_id": "p", "accepted_time": at(13)}]
        orders = [{"pickup_order_id": "hold-a", "pickup_point_id": "p", "status": 0, "pickup_attempts": 1, "ctime": at(13)},
                  {"pickup_order_id": "hold-b", "pickup_point_id": "p", "status": 2, "on_hold_reason": 132, "ctime": at(13)},
                  {"pickup_order_id": "pending", "pickup_point_id": "p", "status": 2, "ctime": at(10)},
                  {"pickup_order_id": "future", "pickup_point_id": "p", "status": 0, "ctime": at(15)}]
        result = pending_inventory(orders, tasks, [], {"p": "ZONE-A"}, now)
        self.assertEqual(len(result), 3)
        self.assertEqual([row["status"] for row in result], ["Pickup Onhold", "Pickup Onhold", "Assigned"])
        self.assertEqual(result[0]["assigned_riders_today"], "2")
        self.assertEqual(result[1]["assigned_riders_today"], "2")
        self.assertEqual(result[2]["assigned_riders_today"], "1; 2")
        profiles = [{"rider_code": "1", "kv": "KV1"}, {"rider_code": "2", "kv": "KV2"}]
        records, _, _ = build_realtime(profiles, result, [], {}, AREAS)
        self.assertEqual(next(r for r in records if r["driver_id"] == "2")["onhold_orders"], 2)

    def test_orders_use_pickup_order_identity_for_complete_pagination(self):
        client = self.client()
        client.get = lambda path, params: {"total": 1, "count": 100, "list": [{"pickup_order_id": str(params["status"])}]}
        rows = client.pending_orders(datetime(2026, 10, 24, 15, tzinfo=TZ))
        self.assertEqual([row["pickup_order_id"] for row in rows], ["0", "2"])

    def test_empty_order_window_without_total_uses_explicit_status_count(self):
        client = self.client()
        client.get = lambda path, params: {"pending_assign_count": 0, "assigned_count": 0, "count": 100, "list": []}
        self.assertEqual(client.pending_orders(datetime(2026, 10, 24, 15, tzinfo=TZ)), [])

    def client(self):
        client = RealtimeClient.__new__(RealtimeClient)
        client.settings = SimpleNamespace(inventory_workers=2)
        return client

    def test_strict_pagination(self):
        client = self.client()
        client.get = lambda path, params: {"total": 3, "count": 2, "list": [{"id": 1}, {"id": 2}] if params["pageno"] == 1 else [{"id": 3}]}
        rows = client.tasks_today(datetime(2026, 10, 24, 1, tzinfo=TZ))
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])

    def test_mutable_pagination_splits_window_to_recover_missing_task(self):
        client = self.client()
        def get(path, params):
            left, right = map(int, params["accepted_time"].split(","))
            if right - left > 1800:
                return {"total": 3, "count": 2, "list": [{"id": 1}, {"id": 2}] if params["pageno"] == 1 else [{"id": 2}]}
            midnight = int(datetime(2026, 10, 24, tzinfo=TZ).timestamp())
            return {"total": 2 if left == midnight else 1, "count": 100, "list": [{"id": 1}, {"id": 2}] if left == midnight else [{"id": 3}]}
        client.get = get
        self.assertEqual(len(client.tasks_today(datetime(2026, 10, 24, 1, tzinfo=TZ))), 3)

    def test_missing_api_page_is_not_treated_as_zero(self):
        client = self.client()
        def get(path, params):
            if params["pageno"] > 1:
                return {"total": 3}
            return {"total": 3, "count": 2, "list": [{"id": 1}, {"id": 2}]}
        client.get = get
        with self.assertRaises(RuntimeError):
            client.tasks_today(datetime(2026, 10, 24, 1, tzinfo=TZ))


if __name__ == "__main__":
    unittest.main()
