from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests
from sdd_data.config import Settings
from sdd_data.delivery_inventory import InventoryClient, InventoryStore, build_inventory, inventory_cot, prepare_inventory, inventory_windows, save_inventory_windows, compact_inventory_payload
from sdd_data.delivery_worker import in_work_window, process_one, run_worker
from sdd_data.transform import TZ, coverage_map

AREAS = ("KV1", "KV2", "KV3", "KV4", "KV5")
MAPPING = coverage_map([["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
                        ["route-a", "KV1", "Phường A mới", "Phường A", "Quận 10 - Phường A"],
                        ["route-b", "KV6", "Phường B mới", "Phường Hiệp Bình Chánh", "Thành phố Thủ Đức"]])


def settings(root):
    return Settings(root, root, root / "cookie", root / "sa", "https://example.supabase.co", "test-only")


def order(sid="A", hour=10, minute=0, **extra):
    stamp = int(datetime(2026, 10, 23, hour, minute, tzinfo=TZ).timestamp())
    return {"shipment_id": sid, "order_status": 1, "zone_id": "route-a", "create_time": stamp-3600,
            "current_station_received_time": stamp, **extra}


class InventoryTransformTests(unittest.TestCase):
    def test_delivery_concurrency_is_configurable_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value, expected in [('48',48),('999',48),('12',12),('0',1)]:
                with patch.dict(os.environ, {'DELIVERY_WORKERS':value}):
                    self.assertEqual(Settings.load(Path(tmp)).delivery_workers,expected)

    def test_snapshot_uses_buyer_mapping_and_true_received_date(self):
        rows, stats = build_inventory([order(seller_zone_id="route-b")], MAPPING, AREAS)
        self.assertEqual(rows[0]["area"], "KV1")
        self.assertEqual(rows[0]["ward"], "Phường A")
        self.assertEqual(rows[0]["report_date"], "2026-10-23")
        self.assertTrue(rows[0]["received_time"].endswith("+07:00"))
        self.assertEqual(stats["included"], 1)

    def test_cot_matches_original_inventory_boundaries(self):
        def at(hour, minute=0):
            return datetime(2026, 10, 23, hour, minute, tzinfo=TZ)
        for kind, dt, expected in (("MP", at(12,29), "2026-10-23 COT 1"),
                                   ("MP", at(12,30), "2026-10-23 COT 2"),
                                   ("MP", at(23), "2026-10-24 COT 1"),
                                   ("WH", at(11,59), "2026-10-23 COT 1"),
                                   ("WH", at(12), "2026-10-23 COT 2")):
            self.assertEqual(inventory_cot(kind, dt), expected)

    def test_excludes_assigned_riders_even_if_only_id_is_present(self):
        for key in ("driver_id", "driver_name", "rider_id", "rider_name", "driverId"):
            rows, stats = build_inventory([order(**{key: "assigned"})], MAPPING, AREAS)
            self.assertEqual(rows, [])
            self.assertEqual(stats["assigned_skipped"], 1)
        self.assertEqual(len(build_inventory([order(driver_id=0)], MAPPING, AREAS)[0]), 1)

    def test_invalid_status_time_or_duplicate_id_blocks_snapshot(self):
        for orders in ([order(order_status=2)], [order(current_station_received_time=0)],
                       [order(create_time="bad")], [order(), order()], [order(shipment_id="")]):
            with self.subTest(orders=orders), self.assertRaises(ValueError):
                build_inventory(orders, MAPPING, AREAS)

    def test_unmapped_is_counted_and_no_seller_zone_is_substituted(self):
        rows, stats = build_inventory([order(zone_id="missing", seller_zone_id="route-a")], MAPPING, AREAS)
        self.assertEqual(rows, [])
        self.assertEqual(stats["unmapped"], 1)

    def test_new_ward_catalog_can_resolve_multiple_old_wards_in_one_district(self):
        mapping = {"a": {"area": "KV5", "district": "Quận 12", "ward": "Phường Thới An", "old_ward": "Phường A"},
                   "b": {"area": "KV5", "district": "Quận 12", "ward": "Phường Thới An", "old_ward": "Phường B"}}
        rows, stats = build_inventory([order(zone_id="new-route", lowest_buyer_address_id=123)], mapping, AREAS,
                                      {"123": "Phường Thới An"})
        self.assertEqual(rows[0]["ward"], "Phường Thới An")
        self.assertEqual(rows[0]["area"], "KV5")
        self.assertEqual(stats["new_ward_matched"], 1)
        mapping["b"]["district"] = "Quận 7"
        self.assertEqual(build_inventory([order(zone_id="new-route", lowest_buyer_address_id=123)], mapping, AREAS,
                                        {"123": "Phường Thới An"})[0], [])

    def test_legacy_area_is_remapped_to_web_five_areas(self):
        rows, _ = build_inventory([order(zone_id="route-b")], MAPPING, AREAS)
        self.assertEqual(rows[0]["area"], "KV5")
        self.assertEqual(build_inventory([order(zone_id="route-b")], MAPPING, ("KV1",))[0], [])

    def test_warehouse_and_artifact_do_not_include_raw_personal_fields(self):
        rows, _ = build_inventory([order(pickup_station_name="51-HCM Warehouse Inbound",
                                         buyer_phone="private", buyer_address="private")], MAPPING, AREAS)
        self.assertEqual(rows[0]["order_type"], "WH")
        self.assertNotIn("private", json.dumps(rows))
        self.assertNotIn("driver_id", rows[0])

    def test_inventory_payload_matches_original_without_volume_order_account(self):
        client = InventoryClient.__new__(InventoryClient)
        client.settings = settings(Path("."))
        self.assertEqual(client.payload("inventory", 1, 10, 2),
                         {"count": 500, "order_status": "1", "create_time": "1,10",
                          "current_station_ids": "4232", "page_no": 2})

    def test_dry_run_writes_only_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("sdd_data.delivery_inventory.load_coverage", return_value=MAPPING), \
                 patch("sdd_data.delivery_inventory.InventoryClient", side_effect=AssertionError("No SPX")), \
                 patch("sdd_data.delivery_inventory.InventoryStore", side_effect=AssertionError("No DB")):
                rows, stats = prepare_inventory(settings(root), root=root, orders=[order()])
            self.assertEqual(stats["included"], 1)
            self.assertEqual(json.loads(next((root/"data").glob("*.json")).read_text(encoding="utf-8"))["records"], rows)

    def test_cached_location_keeps_mapping_and_per_order_statistics(self):
        mapping={"a":{"area":"KV1","district":"Quận 10","ward":"Merged","old_ward":"A"},
                 "b":{"area":"KV1","district":"Quận 10","ward":"Merged","old_ward":"B"}}
        with patch("sdd_data.delivery_inventory.match_zone", wraps=__import__('sdd_data.delivery_inventory',fromlist=['match_zone']).match_zone) as lookup:
            rows,stats=build_inventory([order(str(i),zone_id="new",receiver_ward_name="Merged") for i in range(10)],mapping,AREAS)
        self.assertEqual(len(rows),10)
        self.assertEqual(stats["new_ward_matched"],10)
        self.assertEqual(lookup.call_count,1)

    def test_partition_cache_clips_rolling_range_without_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"plan.json"
            save_inventory_windows(path,"station",[(0,9,0),(10,19,0),(20,29,300),(30,39,200)])
            self.assertEqual(inventory_windows(path,"station",5,45),[(5,19),(20,29),(30,39),(40,45)])
            self.assertEqual(inventory_windows(path,"other-station",5,45),[(5,45)])
            path.write_text('{"namespace":"station","windows":[[0,10],[12,20]]}')
            self.assertEqual(inventory_windows(path,"station",5,45),[(5,45)])

    def test_complete_live_fetch_splits_large_windows_and_rechecks_cached_empty_ranges(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            client=InventoryClient.__new__(InventoryClient)
            client.settings=settings(root)
            created=int(time.time())-1000
            records=[{"shipment_id":str(i),"create_time":created+i//200} for i in range(1000)]
            calls=[]
            def post(payload):
                calls.append(payload)
                left,right=map(int,payload["create_time"].split(","))
                current=[r for r in records if left<=r["create_time"]<=right]
                return {"total":len(current),"count":500,"list":current[:500]}
            client.post=post
            path=root/"plan.json"
            self.assertEqual(len(client.fetch_inventory(1,cache_path=path)),1000)
            self.assertTrue(client.source_complete)
            self.assertTrue(all(c["count"]==500 for c in calls))
            records.clear();calls.clear()
            self.assertEqual(client.fetch_inventory(1,cache_path=path),[])
            self.assertTrue(calls)  # Cached partitions never replace live status reads.
            client.post=lambda payload:{"total":2,"list":[{"shipment_id":"duplicate"},{"shipment_id":"duplicate"}]}
            original=path.read_bytes()
            with self.assertRaises(RuntimeError):client.fetch_inventory(1,cache_path=path)
            self.assertFalse(client.source_complete)
            self.assertEqual(path.read_bytes(),original)


class InventoryQueueTests(unittest.TestCase):
    def test_compact_transport_preserves_every_field_and_unicode(self):
        rows,_=build_inventory([order("A"),order("B")],MAPPING,AREAS)
        compact=compact_inventory_payload({"p_rows":rows,"p_job_id":"job","p_complete":True})
        self.assertEqual(len(compact["p_groups"]),1)
        keys=("report_date","ward","district","area","zone_id","order_type","cot_group")
        decoded=[]
        for sid,created,received,index in compact["p_records"]:
            decoded.append({"shipment_id":sid,"create_time":created,"received_time":received,"status":"LMHub_Received",**dict(zip(keys,compact["p_groups"][index]))})
        self.assertEqual(decoded,rows)
        self.assertEqual(compact["p_job_id"],"job")
        self.assertTrue(compact["p_complete"])
        store=InventoryStore.__new__(InventoryStore);store.settings=settings(Path("."));store.session=Mock()
        store.session.post.return_value.status_code=200
        store.session.post.return_value.json.return_value=2
        self.assertEqual(store.rpc("publish_sdd_delivery_inventory",{"p_rows":rows}),2)
        self.assertTrue(store.session.post.call_args.args[0].endswith("/publish_sdd_delivery_compact"))
        self.assertIn("Phường A".encode(),store.session.post.call_args.kwargs["data"])

    def test_claim_is_never_retried_after_uncertain_network_result(self):
        store = InventoryStore.__new__(InventoryStore)
        store.settings = settings(Path("."))
        store.session = Mock()
        store.session.post.side_effect = requests.ConnectionError("secret should not be logged")
        with self.assertRaises(RuntimeError) as raised:
            store.rpc("claim_sdd_delivery_job", {"p_worker_id": "sdd-delivery-test"})
        self.assertEqual(store.session.post.call_count, 1)
        self.assertNotIn("secret", str(raised.exception))

    def process(self, store, prepare, root):
        with patch("sdd_data.delivery_worker.InventoryStore", return_value=store), \
             patch("sdd_data.delivery_worker.prepare_inventory", prepare), \
             patch("sdd_data.delivery_worker.threading.Thread"):
            return process_one(settings(root), "sdd-delivery-test", root=root)

    def test_empty_queue_does_not_fetch_or_publish(self):
        store, prepare = Mock(), Mock()
        store.rpc.return_value = None
        self.assertFalse(self.process(store, prepare, Path(".")))
        prepare.assert_not_called()
        self.assertEqual(store.rpc.call_count, 1)

    def test_null_composite_response_is_an_empty_queue(self):
        store, prepare = Mock(), Mock()
        store.rpc.return_value = {"id": None, "params": None, "status": None}
        self.assertFalse(self.process(store, prepare, Path(".")))
        prepare.assert_not_called()

    def test_complete_source_is_published_once_with_job_ownership(self):
        store = Mock()
        store.rpc.side_effect = [{"id": "job", "worker_id": "sdd-delivery-test"}, True, 1]
        prepare = Mock(return_value=build_inventory([order()], MAPPING, AREAS))
        self.assertTrue(self.process(store, prepare, Path(".")))
        call = store.rpc.call_args
        self.assertEqual(call.args[0], "publish_sdd_delivery_inventory")
        self.assertEqual(call.args[1]["p_job_id"], "job")
        self.assertEqual(call.args[1]["p_areas"], list(AREAS))

    def test_unmapped_empty_or_fetch_error_never_publishes(self):
        for result in (([], {"unmapped": 1}), ([], {}), RuntimeError("source incomplete")):
            store = Mock()
            store.rpc.return_value = {"id": "job", "worker_id": "sdd-delivery-test"}
            prepare = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            with self.assertRaises(RuntimeError):
                self.process(store, prepare, Path("."))
            names = [call.args[0] for call in store.rpc.call_args_list]
            self.assertNotIn("publish_sdd_delivery_inventory", names)
            self.assertIn("fail_sdd_delivery_job", names)

    def test_verified_empty_source_publishes_zero_with_explicit_complete_flag(self):
        store = Mock()
        store.rpc.side_effect = [{"id":"job","worker_id":"sdd-delivery-test"},True,0]
        prepare = Mock(return_value=([],{"source_complete":True,"input":0}))
        self.assertTrue(self.process(store,prepare,Path(".")))
        self.assertTrue(store.rpc.call_args.args[1]["p_complete"])
        self.assertEqual(store.rpc.call_args.args[1]["p_rows"],[])

    def test_lost_lease_prevents_publication(self):
        store = Mock()
        store.rpc.side_effect = [{"id": "job", "worker_id": "sdd-delivery-test"}, False, True]
        with self.assertRaisesRegex(RuntimeError, "mất quyền"):
            self.process(store, Mock(return_value=build_inventory([order()], MAPPING, AREAS)), Path("."))
        self.assertNotIn("publish_sdd_delivery_inventory", [call.args[0] for call in store.rpc.call_args_list])

    def test_daily_window_and_queue_only_mode(self):
        for hour, expected in ((0, False), (7, False), (8, True), (23, True)):
            self.assertEqual(in_work_window(datetime(2026,10,24,hour,tzinfo=TZ)), expected)
        with tempfile.TemporaryDirectory() as tmp, patch("sdd_data.delivery_worker.process_one", return_value=False) as process:
            run_worker(settings(Path(tmp)), queue_only=True, once=True, root=Path(tmp))
            self.assertFalse(process.call_args.kwargs["auto"])

    def test_once_mode_fetches_even_outside_auto_window(self):
        with tempfile.TemporaryDirectory() as tmp, patch("sdd_data.delivery_worker.process_one", return_value=True) as process:
            run_worker(settings(Path(tmp)), once=True, root=Path(tmp))
            self.assertTrue(process.call_args.kwargs["auto"])


if __name__ == "__main__":
    unittest.main()
