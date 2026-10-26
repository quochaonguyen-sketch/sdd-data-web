from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sdd_data.pickup_cache import save_windows
from sdd_data.pickup_fetch import fetch_live_windows
from sdd_data.pickup_routes import PickupRouteClient
from sdd_data.pickup_realtime import RealtimeClient, RealtimeStore, sync_once


class PickupParallelTests(unittest.TestCase):
    def test_split_before_mutable_pagination_keeps_all_current_ids(self):
        client = RealtimeClient.__new__(RealtimeClient)
        client.settings = SimpleNamespace(inventory_workers=2)
        requests = []
        def get(path, params):
            requests.append(params)
            left, right = map(int, params["ctime"].split(","))
            rows = [{"id": str(i)} for i in range(left, right+1)]
            return {"total": len(rows), "count": 2, "list": rows[:2]}
        client.get = get
        rows = client.search_window("source", "ctime", 0, 9, page_size=2, max_window_records=2)
        self.assertEqual([row["id"] for row in rows], [str(i) for i in range(10)])
        self.assertTrue(all(p["pageno"] == 1 for p in requests))

    def test_windows_overlap_use_same_page_pool_and_keep_order(self):
        gate = threading.Barrier(4, timeout=3)
        pools = []
        client = SimpleNamespace(settings=SimpleNamespace(inventory_workers=8))
        def search(path, field, left, right, filters, fields, **options):
            pools.append(options["page_pool"])
            gate.wait()
            options["on_window"](left, right, 300)
            return [{"id": str(left)}]
        client.search_window = search
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "windows.json"
            save_windows(path, "station", [(i, i+99, 300) for i in range(0, 400, 100)])
            rows = fetch_live_windows(client, "source", "ctime", 0, 399,
                                      cache_path=path, namespace="station")
        self.assertEqual([r["id"] for r in rows], ["0", "100", "200", "300"])
        self.assertEqual(len({id(pool) for pool in pools}), 1)

    def test_http_request_budget_is_shared_across_nested_workers(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client._request_slots = threading.BoundedSemaphore(3)
        client._request_lock = threading.Lock()
        client.request_count = 0
        active, peak = 0, 0
        lock = threading.Lock()
        def get(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.01)
            with lock:
                active -= 1
            return SimpleNamespace(status_code=200, json=lambda: {"data": {}, "retcode": 0})
        client.session = lambda: SimpleNamespace(get=get)
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda i: client.get("source", {}), range(12)))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 3)
        self.assertEqual(client.request_count, 12)

    def test_geography_cache_invalidates_with_snapshot_and_rechecks_head(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RealtimeStore.__new__(RealtimeStore)
            store.settings = SimpleNamespace(station_id="station", supabase_url="https://project.example")
            store.cache_dir = Path(folder)
            head = {"snapshot_id": "a", "snapshot_at": "now"}
            store.request = Mock(side_effect=lambda *args: [dict(head)])
            store.all_rows = Mock(return_value=[{"shipment_id": "old"}])
            self.assertEqual(store.inventory()[1], [{"shipment_id": "old"}])
            self.assertFalse(store.inventory_cache_hit)
            store.inventory()
            self.assertTrue(store.inventory_cache_hit)
            self.assertEqual(store.all_rows.call_count, 1)
            self.assertEqual(store.request.call_count, 4)
            store.settings.supabase_url = "https://different-project.example"
            store.inventory()
            self.assertFalse(store.inventory_cache_hit)
            self.assertEqual(store.all_rows.call_count, 2)
            head["snapshot_id"] = "b"
            store.all_rows.return_value = [{"shipment_id": "new"}]
            self.assertEqual(store.inventory()[1], [{"shipment_id": "new"}])
            self.assertFalse(store.inventory_cache_hit)
            store.request.side_effect = [[head], [{**head, "snapshot_id": "c"}]]
            with self.assertRaisesRegex(RuntimeError, "đổi phiên"):
                store.inventory()

    def test_realtime_sources_overlap_and_dry_run_never_publishes(self):
        gate = threading.Barrier(4, timeout=3)
        def ready(result):
            def run(*args):
                gate.wait()
                return result
            return run
        store, client = Mock(inventory_cache_hit=False), Mock(request_count=3, request_counts={})
        store.all_rows.side_effect = ready([{"rider_code": "1", "kv": "KV1"}])
        store.inventory.return_value = ({}, [])
        client.tasks_today.side_effect = ready([{"driver_id": "1", "pickup_point_id": "p", "pickup_quantity": 3}])
        client.pending_orders.side_effect = ready([{"pickup_order_id": "o", "pickup_point_id": "p", "status": 2}])
        client.routes.side_effect = ready(({"p": "zone"}, 0))
        with tempfile.TemporaryDirectory() as folder, patch("sdd_data.pickup_realtime.RealtimeStore", return_value=store), patch("sdd_data.pickup_realtime.RealtimeClient", return_value=client):
            result = sync_once(SimpleNamespace(areas=("KV1",)), root=Path(folder), publish=False)
        self.assertEqual(result["pending_orders_read"], 1)
        self.assertEqual(result["areas"]["KV1"]["picked"], 3)
        store.request.assert_not_called()
        store.close.assert_called_once()
        client.close.assert_called_once()

    def test_failed_source_never_publishes_partial_realtime(self):
        store, client = Mock(), Mock()
        store.all_rows.return_value = []
        store.inventory.return_value = ({}, [])
        client.tasks_today.side_effect = RuntimeError("missing source page")
        with tempfile.TemporaryDirectory() as folder, patch("sdd_data.pickup_realtime.RealtimeStore", return_value=store), patch("sdd_data.pickup_realtime.RealtimeClient", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "missing source page"):
                sync_once(SimpleNamespace(areas=("KV1",)), root=Path(folder))
            self.assertEqual(list(Path(folder).glob("data/pickup-realtime-*.json")), [])
        store.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
