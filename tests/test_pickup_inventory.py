from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sdd_data.config import Settings
from sdd_data.pickup_inventory import (
    PickupStore,
    build_pickup_inventory,
    parse_pickup_ward,
    pickup_cot_group,
    prepare_pickup_inventory,
)
from sdd_data.pickup_worker import in_work_window, process_one, run_worker
from sdd_data.transform import TZ, coverage_map

AREAS = ("KV1", "KV2", "KV3", "KV4", "KV5")
MAPPING = coverage_map([
    ["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
    ["route-a", "KV1", "Phường 1", "Phường 1", "Quận 10 - Phường 1"],
    ["Q4_P8", "KV2", "Phường Khánh Hội", "Phường 8", "Quận 4 - Phường 8"],
    ["route-b", "KV5", "Phường Hiệp Bình Chánh", "Phường Hiệp Bình Chánh", "Thành phố Thủ Đức"],
])


def settings(root):
    return Settings(root, root, root / "cookie", root / "sa", "https://example.supabase.co", "test-only")


def sample_order(sid="VN123456", pid="PUP001", status="0", hour=10, **extra):
    dt = datetime(2026, 10, 24, hour, 0, tzinfo=TZ)
    stamp = int(dt.timestamp())
    return {
        "shipment_id": sid,
        "sls_tracking_number": sid,
        "pickup_point_id": pid,
        "pickup_point_name": "Shop ABC",
        "status": status,
        "ctime": stamp,
        "create_time": dt.strftime("%d/%m/%Y %H:%M:%S"),
        **extra,
    }


class PickupTransformTests(unittest.TestCase):
    def test_pipeline_overlaps_sources_and_preserves_assigned_filter(self):
        gate = threading.Barrier(3, timeout=3)
        client = Mock(assigned_source_complete=True)
        def orders(*args):
            gate.wait()
            return [sample_order(sid='assigned', status='2'), sample_order(sid='held', status='2', on_hold_reason=132)]
        def tasks():
            gate.wait()
            return {'point_riders': {}, 'accepted_pids': set()}
        def routes():
            gate.wait()
            return {'PUP001': 'Q4_P8'}, 0
        client.fetch_all_pickup_orders.side_effect = orders
        client.fetch_today_tasks.side_effect = tasks
        client.routes.side_effect = routes
        client.fetch_point_details.return_value = {}
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.pickup_inventory.PickupInventoryClient', return_value=client), patch('sdd_data.pickup_inventory.load_coverage', return_value=MAPPING):
            root = Path(folder)
            rows, stats = prepare_pickup_inventory(settings(root), root=root)
        self.assertEqual([row['shipment_id'] for row in rows], ['assigned'])
        self.assertEqual(stats['excluded_pickup_onhold'], 1)
        self.assertTrue(stats['source_complete'])
        client.close.assert_called_once()

    def test_pipeline_failure_does_not_produce_partial_artifact(self):
        client = Mock()
        client.fetch_all_pickup_orders.return_value = [sample_order(status='2')]
        client.fetch_today_tasks.side_effect = RuntimeError('incomplete tasks')
        client.routes.return_value = ({}, 0)
        client.fetch_point_details.return_value = {}
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.pickup_inventory.PickupInventoryClient', return_value=client), patch('sdd_data.pickup_inventory.load_coverage', return_value=MAPPING):
            root = Path(folder)
            with self.assertRaisesRegex(RuntimeError, 'incomplete tasks'):
                prepare_pickup_inventory(settings(root), root=root)
            self.assertFalse(list(root.glob('data/pickup-inventory-*.json')))
        client.close.assert_called_once()

    def test_parse_pickup_ward(self):
        self.assertIn("Khánh Hội", parse_pickup_ward("Thành phố Hồ Chí Minh,Phường Khánh Hội", "145 Hoàng Diệu"))
        self.assertIn("Tân Thới Hiệp", parse_pickup_ward("Quận 12", "Đường Nguyễn Ảnh Thủ, Phường Tân Thới Hiệp"))
        self.assertEqual(parse_pickup_ward("", ""), "")

    def test_cot_group_boundaries(self):
        d0 = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)
        d1 = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
        d2 = datetime(2026, 10, 24, 16, 0, tzinfo=TZ)
        self.assertTrue(pickup_cot_group(d0).endswith("COT 1"))
        self.assertTrue(pickup_cot_group(d1).endswith("COT 2"))
        self.assertTrue(pickup_cot_group(d2).endswith("COT 1"))

    def test_build_inventory_normal(self):
        order = sample_order(sid="VN111", pid="PUP001", status="2")
        details = {"PUP001": {"district": "Quận 4", "full_address": "123 Đoàn Văn Bơ, Phường Khánh Hội"}}
        groups = {"PUP001": "Q4_P8"}
        tasks = {"point_riders": {"PUP001": "[COT1] 123-Rider A"}, "accepted_pids": {"PUP001"}}

        rows, stats = build_pickup_inventory([order], MAPPING, AREAS, details, groups, tasks, "snap1", "2026-10-24T12:00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tracking_id"], "VN111")
        self.assertEqual(rows[0]["status"], "Assigned")
        self.assertEqual(rows[0]["area"], "KV2")
        self.assertEqual(rows[0]["district"], "Quận 4")
        self.assertEqual(rows[0]["zone_name"], "Q4_P8")
        self.assertEqual(rows[0]["assigned_riders_today"], "[COT1] 123-Rider A")
        self.assertEqual(stats["included"], 1)

    def test_history_district_uses_route_but_does_not_guess_ambiguous_ward(self):
        mapping = {"route-a": {"district":"Quận 4","ward":"Phường A","old_ward":"Phường A","area":"KV1"},
                   "route-b": {"district":"Quận 5","ward":"Phường A","old_ward":"Phường A","area":"KV1"}}
        details = {"PUP001":{"district":"Thành phố Hồ Chí Minh,Phường A"}}
        order = sample_order(status="2")
        rows, _ = build_pickup_inventory([order],mapping,AREAS,details,{}, {},"test","now")
        self.assertEqual(rows[0]["district"], "")
        rows, _ = build_pickup_inventory([order],mapping,AREAS,details,{"PUP001":"route-b"}, {},"test","now")
        self.assertEqual(rows[0]["district"], "Quận 5")
        rows, _ = build_pickup_inventory([order],mapping,AREAS,details,{"PUP001":"Q4_new_route"}, {},"test","now")
        self.assertEqual(rows[0]["district"], "Quận 4")

    def test_dry_run_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("sdd_data.pickup_inventory.load_coverage", return_value=MAPPING):
                rows, stats = prepare_pickup_inventory(settings(root), root=root, orders=[sample_order(status="2")])
            self.assertEqual(stats["included"], 1)
            files = list((root / "data").glob("pickup-inventory-*.json"))
            self.assertTrue(len(files) > 0)

    def test_only_explicit_assigned_without_hold_or_picked_is_included(self):
        orders = [sample_order(sid="assigned", status="2"),
                  sample_order(sid="held", status="2", on_hold_reason=132),
                  sample_order(sid="held-alt", status="2", pickup_on_hold_reason=1),
                  sample_order(sid="attempted", status="0", pickup_attempts=1),
                  sample_order(sid="created", status="0"),
                  sample_order(sid="picked", status="4"),
                  sample_order(sid="inbound", status="7"),
                  sample_order(sid="picked-time", status="2", driver_pickedup_time=1792800000),
                  sample_order(sid="unknown", status="999")]
        tasks = {"point_riders": {"PUP001": "[COT1] 123-Rider"}, "accepted_pids": {"PUP001"}}
        rows, stats = build_pickup_inventory(orders, MAPPING, AREAS, {}, {"PUP001":"route-a"}, tasks, "test", "2026-10-24T12:00:00+07:00")
        self.assertEqual([r["shipment_id"] for r in rows], ["assigned"])
        self.assertEqual(stats["excluded_pickup_onhold"], 3)
        self.assertEqual(stats["excluded_created"], 1)
        self.assertEqual(stats["excluded_picked_up"], 2)

    def test_store_rejects_non_assigned_and_unverified_empty_snapshots(self):
        store = PickupStore.__new__(PickupStore)
        store.request = Mock()
        with self.assertRaises(ValueError):
            store.publish_pickup_inventory("job","worker","snap","now",[{"status":"Pickup Onhold"}],AREAS)
        with self.assertRaises(ValueError):
            store.publish_pickup_inventory("job","worker","snap","now",[],AREAS)
        store.request.assert_not_called()
        self.assertEqual(store.publish_pickup_inventory("job","worker","snap","now",[],AREAS,allow_empty=True),0)
        self.assertEqual(store.request.call_args_list[0].args[0],"DELETE")


class PickupQueueTests(unittest.TestCase):
    def test_in_work_window(self):
        self.assertTrue(in_work_window(datetime(2026, 10, 24, 10, 0, tzinfo=TZ)))
        self.assertFalse(in_work_window(datetime(2026, 10, 24, 2, 0, tzinfo=TZ)))

    def test_empty_queue_does_not_fetch(self):
        store = Mock()
        store.claim_job.return_value = None
        with patch("sdd_data.pickup_worker.PickupStore", return_value=store):
            res = process_one(settings(Path(".")), "sdd-pickup-test", root=Path("."))
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
