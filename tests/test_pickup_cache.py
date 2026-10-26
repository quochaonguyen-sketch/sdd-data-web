from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from sdd_data.pickup_cache import PickupRecordCache, cached_windows, save_windows
from sdd_data.pickup_inventory import PickupInventoryClient, build_pickup_inventory
from sdd_data.pickup_realtime import RealtimeClient
from tests.test_pickup_inventory import MAPPING, AREAS, sample_order


class PickupCacheTests(unittest.TestCase):
    def test_cached_partition_and_larger_pages_reduce_requests_without_losing_orders(self):
        rows = [{"pickup_order_id": str(i), "ctime": 9000 + i // 20, "status": 2} for i in range(20000)]
        client = PickupInventoryClient.__new__(PickupInventoryClient)
        client.settings = SimpleNamespace(inventory_workers=2)
        client.check_deadline = Mock()
        requests = []
        def get(path, params):
            requests.append(params)
            left, right = map(int, params["ctime"].split(","))
            selected = [row for row in rows if left <= row["ctime"] <= right]
            offset = (params["pageno"] - 1) * params["count"]
            return {"total": len(selected), "count": params["count"], "list": selected[offset:offset + params["count"]]}
        client.get = get
        args = ("fm_hub/pickup_order/search", "ctime", 0, 10000, {"status": 2}, ("pickup_order_id",))
        before = RealtimeClient.search_window(client, *args)
        old_requests = len(requests)
        requests.clear()
        observed = []
        first = client.search_window(*args, on_window=lambda a, b, total: observed.append((a, b, total)))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "windows.json"
            save_windows(path, "station", observed)
            requests.clear()
            result = []
            for left, right in cached_windows(path, "station", 10000):
                result.extend(client.search_window(args[0], args[1], left, right, args[4], args[5]))
        self.assertEqual(before, first)
        self.assertEqual(result, before)
        self.assertLess(len(requests), old_requests)

    def test_window_cache_keeps_complete_coverage_and_checks_empty_history_again(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "windows.json"
            save_windows(path, "station", [(0, 30, 0), (31, 60, 0), (61, 100, 3)])
            self.assertEqual(cached_windows(path, "station", 120), [(0, 60), (61, 100), (101, 120)])
            self.assertEqual(cached_windows(path, "other", 120), [(0, 120)])
            path.write_text('{"version":1,"station":"station","windows":[[2,100]]}')
            self.assertEqual(cached_windows(path, "station", 120), [(0, 120)])

    def test_cached_windows_fetch_current_status_and_new_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "windows.json"
            save_windows(path, "station", [(0, 100, 1), (101, 200, 0)])
            client = PickupInventoryClient.__new__(PickupInventoryClient)
            client.settings = SimpleNamespace(station_id="station", inventory_workers=2)
            client.order_window_cache = path
            client.check_deadline = Mock()
            calls = []
            def get(path, params):
                calls.append(params)
                left, right = map(int, params["ctime"].split(","))
                # Even a formerly empty window can acquire a new Assigned order.
                rows = [{"pickup_order_id": "old", "status": 2, "on_hold_reason": 132}] if left == 0 else (
                    [{"pickup_order_id": "newly-assigned", "status": 2}] if left == 101 else [])
                return {"list": rows, "total": len(rows), "count": params["count"]}
            client.get = get
            rows = client.fetch_all_pickup_orders()
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(p["count"] == 500 for p in calls))
            self.assertTrue(client.assigned_source_complete)

    def test_cached_order_is_reused_but_hold_removal_assignment_and_metadata_are_live(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            context = [MAPPING, AREAS]
            tasks = {"point_riders": {"PUP001": "rider-a"}}
            order = sample_order(status="2")
            def build(cache, orders, riders=tasks, groups=None):
                return build_pickup_inventory(orders, MAPPING, AREAS, {}, groups or {}, riders, "new", "now", cache)
            first = PickupRecordCache(path, context)
            build(first, [order])
            first.save()
            warm = PickupRecordCache(path, context)
            rows, stats = build(warm, [order])
            self.assertEqual(stats["orders_cached"], 1)
            self.assertEqual(rows[0]["snapshot_at"], "now")
            changed = PickupRecordCache(path, context)
            rows, stats = build(changed, [order], {"point_riders": {"PUP001": "rider-b"}})
            self.assertEqual(stats["orders_cached"], 0)
            self.assertEqual(rows[0]["assigned_riders_today"], "rider-b")
            for updated in ({**order, "status": "4"}, {**order, "driver_pickedup_time": 1792800000}):
                rows, _ = build(PickupRecordCache(path, context), [updated])
                self.assertEqual(rows, [])
            moved = PickupRecordCache(path, context)
            rows, stats = build(moved, [order], groups={"PUP001": "Q4_P8"})
            self.assertEqual(stats["orders_cached"], 0)
            self.assertEqual(rows[0]["zone_name"], "Q4_P8")
            held = PickupRecordCache(path, context)
            rows, _ = build(held, [{**order, "on_hold_reason": 132}])
            self.assertEqual(rows, [])
            held.save()
            self.assertEqual(PickupRecordCache(path, context).previous, {})

    def test_context_change_corrupt_cache_and_removed_orders_never_restore_old_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            cache = PickupRecordCache(path, "v1")
            cache.put("a", "signature", {"shipment_id": "a", "snapshot_at": "old"})
            cache.save()
            self.assertIsNone(PickupRecordCache(path, "v2").get("a", "signature"))
            fresh = PickupRecordCache(path, "v1")
            fresh.save()
            self.assertEqual(PickupRecordCache(path, "v1").previous, {})
            path.write_text("corrupt")
            self.assertEqual(PickupRecordCache(path, "v1").previous, {})


if __name__ == "__main__":
    unittest.main()
