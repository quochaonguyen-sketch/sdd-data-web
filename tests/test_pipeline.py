from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from sdd_data.clients import AuthenticationError, SpxClient, WebsiteStore, read_cookies, split_window, volume_fields
from sdd_data.config import Settings
from sdd_data.runner import RunLock, run
from sdd_data.transform import TZ, area_code, cot_label, coverage_map, match_zone, operating_area, summarize

DAY = "2026-10-23"
MAPPING = coverage_map([
    ["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
    ["route-a", "Khu vực 1", "Phường A", "Phường cũ A", "Quận A - Phường cũ A"],
    ["route-b", "KV5", "Phường B", "Phường cũ B", "Quận B - Phường cũ B"],
    ["route-c", "Khu vực 6", "Phường C", "Phường cũ C", "Quận C"],
])


def stamp(hour=7, minute=0):
    return int(datetime(2026, 10, 23, hour, minute, tzinfo=TZ).timestamp())


def settings(root):
    return Settings(root, root, root / "cookies.json", root / "sa.json",
                    "https://example.supabase.co", "test-only-key")


class TransformTests(unittest.TestCase):
    def test_pickup_never_uses_buyer_zone_as_seller_route(self):
        orders = [{"shipment_id": "seller", "seller_zone_id": "route-a", "zone_id": "route-b", "create_time": stamp()},
                  {"shipment_id": "buyer-only", "zone_id": "route-b", "buyer_zone_id": "route-b", "create_time": stamp()}]
        rows, stats = summarize("pickup", orders, MAPPING, DAY, ("KV1", "KV5"))
        self.assertEqual(sum(row["total_orders"] for row in rows), 2)
        known = next(row for row in rows if row["area"] == "KV1")
        self.assertEqual(known["ma_tuyen"], "route-a")
        unknown = next(row for row in rows if row["area"] == "UNRESOLVED")
        self.assertIsNone(unknown["ma_tuyen"])
        self.assertEqual(stats["unmapped"], 1)

    def test_seller_address_maps_volume_without_fabricating_an_exact_route(self):
        orders = [{"shipment_id": "seller", "lowest_seller_address_id": 123,
                   "zone_id": "route-b", "create_time": stamp()}]
        rows, stats = summarize("pickup", orders, MAPPING, DAY, ("KV1", "KV5"), seller_wards={"123": "Phường A"})
        self.assertEqual(rows[0]["area"], "KV1")
        self.assertEqual(rows[0]["new_ward"], "Phường A")
        self.assertIsNone(rows[0]["ma_tuyen"])
        self.assertEqual(stats["missing_seller_route"], 1)
        # Delivery must continue to use the destination zone even when seller data exists.
        delivery = [{**orders[0], "current_station_received_time": stamp()}]
        self.assertEqual(summarize("delivery", delivery, MAPPING, DAY, ("KV1", "KV5"))[0][0]["area"], "KV5")

    def test_fetch_keeps_seller_locality_but_discards_street_addresses(self):
        fields = volume_fields({"shipment_id": "A", "lowest_seller_address_id": 100932,
                                "seller_address": "123 private street, Phường Trung Mỹ Tây, Quận 12, Hồ Chí Minh",
                                "buyer_address": "private", "seller_phone": "private"})
        self.assertEqual(fields["seller_district_name"], "Quận 12")
        self.assertEqual(fields["lowest_seller_address_id"], 100932)
        self.assertNotIn("seller_address", fields)
        self.assertNotIn("buyer_address", fields)
        self.assertNotIn("seller_phone", fields)

    def test_new_seller_ward_does_not_match_a_former_ward_with_the_same_name(self):
        mapping = coverage_map([["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
                                ["old-name", "KV2", "Phường Tân Mỹ", "Phường Tân Phú", "Quận 7"],
                                ["new-name", "KV3", "Phường Tân Phú", "Phường Hòa Thạnh", "Quận Tân Phú"]])
        orders = [{"shipment_id": "A", "lowest_seller_address_id": 1, "create_time": stamp()}]
        rows, _ = summarize("pickup", orders, mapping, DAY, ("KV2", "KV3"), seller_wards={"1": "Phường Tân Phú"})
        self.assertEqual(rows[0]["district"], "Quận Tân Phú")

    def test_ambiguous_merged_seller_ward_is_not_guessed_from_buyer_route(self):
        mapping = coverage_map([["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
                                ["a", "KV1", "Phường Bình Phú", "Phường 10", "Quận 6"],
                                ["b", "KV1", "Phường Bình Phú", "Phường 16", "Quận 8"]])
        orders = [{"shipment_id": "A", "lowest_seller_address_id": 1, "zone_id": "a", "create_time": stamp()}]
        rows, stats = summarize("pickup", orders, mapping, DAY, ("KV1",), seller_wards={"1": "Phường Bình Phú"})
        self.assertEqual(rows[0]["area"], "KV1")
        self.assertEqual(rows[0]["district"], "Chưa rõ quận cũ")
        self.assertEqual(stats["seller_area_matched"], 1)

    def test_merged_ward_spanning_operating_areas_stays_unresolved(self):
        mapping = coverage_map([["Zone ID", "Zone", "Phường mới", "Phường cũ", "Quận & phường cũ"],
                                ["a", "KV1", "Phường Chung", "Phường 10", "Quận 6"],
                                ["b", "KV3", "Phường Chung", "Phường 1", "Quận Bình Tân"]])
        rows, _ = summarize("pickup", [{"shipment_id": "A", "lowest_seller_address_id": 1,
            "create_time": stamp()}], mapping, DAY, ("KV1", "KV3"), seller_wards={"1": "Phường Chung"})
        self.assertEqual(rows[0]["area"], "UNRESOLVED")

    def test_user_approved_geography_overrides_legacy_coverage_areas(self):
        cases = [("Quận Gò Vấp", "Phường 15", "KV6", "KV5"),
                 ("Quận 12", "Thạnh Lộc", "KV6", "KV5"),
                 ("Huyện Hóc Môn", "Bà Điểm", "KV6", "KV5"),
                 ("Quận 10", "Phường 1", "KV4", "KV1"),
                 ("Quận 4", "Phường 1", "KV4", "KV2"),
                 ("Quận 7", "Phường 1", "KV4", "KV2"),
                 ("Quận Bình Thạnh", "Phường 1", "KV5", "KV2"),
                 ("Quận 3", "Phường 1", "KV5", "KV4"),
                 ("Quận Tân Bình", "Phường 1", "KV2", "KV4"),
                 ("Quận Phú Nhuận", "Phường 1", "KV2", "KV4"),
                 ("Huyện Bình Chánh", "Xã Bình Hưng", "KV3", "KV1"),
                 ("Huyện Bình Chánh", "Xã Phong Phú", "KV3", "KV1"),
                 ("Huyện Bình Chánh", "Xã Vĩnh Lộc B", "KV1", "KV3"),
                 ("Thành phố Thủ Đức", "Phường Hiệp Bình Chánh", "KV2", "KV5"),
                 ("TP. Thủ Đức", "Phường An Phú", "KV5", "KV2"),
                 ("Thành phố Thủ Đức", "Phường Phước Long B", "KV5", "KV2")]
        for district, ward, source, expected in cases:
            with self.subTest(district=district, ward=ward):
                self.assertEqual(operating_area(district, ward, "", source), expected)
        self.assertIsNone(operating_area("Thành phố Thủ Đức", "", "Phường mới chưa rõ", "KV2"))

    def test_legacy_kv6_orders_are_included_in_both_volume_jobs(self):
        mapping = coverage_map([["Zone ID", "Zone", "Phường cũ", "Quận & phường cũ"],
                                ["gv", "KV6", "Phường 15", "Quận Gò Vấp"],
                                ["q12", "KV6", "Thạnh Lộc", "Quận 12"],
                                ["hm", "KV6", "Bà Điểm", "Huyện Hóc Môn"]])
        for kind, field in [("pickup", "create_time"), ("delivery", "current_station_received_time")]:
            orders = [{"shipment_id": zone, "seller_zone_id" if kind == "pickup" else "zone_id": zone, field: stamp()} for zone in mapping]
            rows, stats = summarize(kind, orders, mapping, DAY, ("KV1", "KV2", "KV3", "KV4", "KV5"))
            self.assertEqual(sum(r["total_orders"] for r in rows), 3)
            self.assertEqual({r["area"] for r in rows}, {"KV5"})
            self.assertEqual(stats["remapped_area"], 3)
            self.assertNotIn("outside_area", stats)

    def test_operating_area_normalization(self):
        for value in ("Khu vực 1", "KV1", "Area 1", "1"):
            self.assertEqual(area_code(value), "KV1")

    def test_pickup_cot_rollover_and_boundaries(self):
        for hour, minute, expected in [(11, 14, "2026-10-23 COT 1"),
                (11, 15, "2026-10-23 COT 2"), (15, 29, "2026-10-23 COT 2"),
                (15, 30, "2026-10-24 COT 1")]:
            self.assertEqual(cot_label("pickup", datetime.fromtimestamp(stamp(hour, minute), TZ)), expected)

    def test_delivery_uses_its_own_cot_boundaries(self):
        expected = [(7, 59, "COT 1.1"), (8, 0, "COT 1.2"), (12, 59, "COT 1.2"), (13, 0, "COT 2")]
        for hour, minute, label in expected:
            self.assertEqual(cot_label("delivery", datetime.fromtimestamp(stamp(hour, minute), TZ)), f"{DAY} {label}")

    def test_pickup_counts_unique_shipments_in_web_scope_and_keeps_routes(self):
        rows = [
            {"shipment_id": "A", "pickup_zone_id": "route-a", "create_time": stamp()},
            {"shipment_id": "A", "pickup_zone_id": "route-a", "create_time": stamp()},
            {"shipment_id": "B", "pickup_zone_id": "route-a-x", "create_time": stamp() * 1000},
            {"shipment_id": "C", "pickup_zone_id": "route-c", "create_time": stamp()},
        ]
        summaries, stats = summarize("pickup", rows, MAPPING, DAY, ("KV1", "KV2", "KV3", "KV4", "KV5"))
        self.assertEqual(sum(row["total_orders"] for row in summaries), 2)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["outside_area"], 1)
        self.assertTrue(all(row["area"] == "KV1" for row in summaries))
        self.assertTrue(all("new_ward" in row and "old_ward" not in row for row in summaries))

    def test_delivery_uses_received_time_and_old_ward(self):
        rows = [{"shipment_id": "D", "zone_id": "route-b", "create_time": stamp() - 86400,
                 "current_station_received_time": stamp(13)}]
        summaries, _ = summarize("delivery", rows, MAPPING, DAY, ("KV5",))
        self.assertEqual(summaries[0]["old_ward"], "Phường cũ B")
        self.assertEqual(summaries[0]["cot"], DAY + " COT 2")
        self.assertNotIn("shipment_id", summaries[0])

    def test_summary_ids_are_stable_across_input_order(self):
        rows = [{"shipment_id": str(i), "zone_id": "route-a", "current_station_received_time": stamp()} for i in range(3)]
        self.assertEqual(summarize("delivery", rows, MAPPING, DAY, ("KV1",))[0],
                         summarize("delivery", list(reversed(rows)), MAPPING, DAY, ("KV1",))[0])

    def test_invalid_rows_are_visible_in_report(self):
        rows = [{"shipment_id": "A", "zone_id": "missing", "current_station_received_time": stamp()},
                {"shipment_id": "B", "zone_id": "route-a", "current_station_received_time": 0},
                {"shipment_id": "C", "zone_id": "route-a", "current_station_received_time": stamp() - 86400}]
        summaries, stats = summarize("delivery", rows, MAPPING, DAY, ("KV1",))
        self.assertEqual(summaries, [])
        self.assertEqual([stats[x] for x in ("unmapped", "invalid_time", "outside_date")], [1, 1, 1])
        self.assertEqual(stats["unmapped_zones"], {"missing": 1})

    def test_zone_matching_does_not_assign_arbitrary_similar_prefix(self):
        self.assertEqual(match_zone("route-a-detail", MAPPING)[0], "route-a")
        self.assertEqual(match_zone("route", MAPPING), ("", {}))
        self.assertEqual(match_zone("route-ab", MAPPING), ("", {}))

    def test_conflicting_zone_mapping_fails(self):
        with self.assertRaises(ValueError):
            coverage_map([["Zone ID", "Zone"], ["A", "KV1"], ["A", "KV2"]], duplicate_policy="error")

    def test_mapping_default_matches_launcher2_last_row_policy(self):
        result = coverage_map([["Zone ID", "Zone"], ["A", "KV1"], ["A", "KV2"]])
        self.assertEqual(result["a"]["area"], "KV2")


class StoreTests(unittest.TestCase):
    def test_unknown_pickup_bucket_is_written_and_included_in_snapshot_cleanup(self):
        store = self.make_store()
        records = self.records()
        records[0]["area"] = "UNRESOLVED"
        store.commit_snapshot("pickup", DAY, records)
        self.assertIn("UNRESOLVED", store.calls[-1][2]["area"])
        self.assertTrue(all(call[1] == "pickup_volume" for call in store.calls))
        self.assertNotIn("UNRESOLVED", store.scope(DAY, "delivery")["area"])

    def make_store(self, fail=False):
        store = WebsiteStore.__new__(WebsiteStore)
        store.settings = replace(settings(Path(".")), batch_size=1)
        store.calls = []
        store.existing_ids = lambda table, day: {"stale-row"}

        def request(method, table, params, rows=None):
            store.calls.append((method, table, params, rows))
            if fail and method == "POST" and len(store.calls) == 2:
                raise RuntimeError("Write failed")
        store.request = request
        return store

    def records(self):
        return [{"summary_id": f"row-{i}", "report_date": DAY, "area": "KV1", "total_orders": 1} for i in range(2)]

    def test_delivery_writes_dashboard_table_then_scoped_cleanup(self):
        store = self.make_store()
        result = store.commit_snapshot("delivery", DAY, self.records())
        self.assertEqual([call[0] for call in store.calls], ["POST", "POST", "DELETE"])
        self.assertTrue(all(call[1] == "delivery_order" for call in store.calls))
        self.assertEqual(store.calls[-1][2]["report_date"], "eq." + DAY)
        self.assertEqual(result, {"upserted": 2, "stale_removed": 1})

    def test_failed_batch_never_deletes_old_rows(self):
        store = self.make_store(fail=True)
        with self.assertRaises(RuntimeError):
            store.commit_snapshot("pickup", DAY, self.records())
        self.assertNotIn("DELETE", [call[0] for call in store.calls])

    def test_empty_snapshot_never_writes_or_deletes(self):
        store = self.make_store()
        with self.assertRaises(ValueError):
            store.commit_snapshot("pickup", DAY, [])
        self.assertEqual(store.calls, [])

    def test_outside_scope_snapshot_fails_before_any_request(self):
        store = self.make_store()
        rows = self.records()
        rows[0]["area"] = "KV6"
        with self.assertRaises(ValueError):
            store.commit_snapshot("pickup", DAY, rows)
        self.assertEqual(store.calls, [])


class FetchTests(unittest.TestCase):
    def test_single_second_cap_fails_before_requesting_thousands_of_pages(self):
        client = SpxClient.__new__(SpxClient)
        client.settings = settings(Path("."))
        client.post = lambda payload: {"total": 10000, "list": [{"shipment_id": "A"}], "count": 200}
        with self.assertRaisesRegex(RuntimeError, "10.000"):
            client.fetch_window("pickup", 100, 100)

    def test_adaptive_windows_cover_every_second_without_overlap(self):
        windows = split_window(100, 3699, 9100)
        seconds = [second for left, right in windows for second in range(left, right + 1)]
        self.assertEqual(seconds, list(range(100, 3700)))

    def test_only_volume_fields_are_kept(self):
        self.assertEqual(volume_fields({"shipment_id": "A", "zone_id": "a", "receiver_name": "private", "phone": "private"}),
                         {"shipment_id": "A", "zone_id": "a"})

    def test_unstable_sort_is_avoided_by_splitting_into_complete_pages(self):
        client = SpxClient.__new__(SpxClient)
        client.settings = settings(Path("."))
        calls = []

        def post(payload):
            calls.append(payload)
            span = payload["create_time"]
            ids = ["A", "B", "C"] if span == "0,3" else ["A"] if span == "0,1" else ["B", "C"]
            return {"total": len(ids), "count": 2, "list": [{"shipment_id": sid} for sid in ids[:2]]}
        client.post = post
        rows = client.fetch_window("pickup", 0, 3)
        self.assertEqual({row["shipment_id"] for row in rows}, {"A", "B", "C"})
        self.assertTrue(all(payload["page_no"] == 1 for payload in calls))

    def test_missing_page_is_error_not_a_valid_partial_report(self):
        client = SpxClient.__new__(SpxClient)
        client.settings = settings(Path("."))
        client.post = lambda payload: {"total": 3, "count": 2, "list": [{"shipment_id": "A"}, {"shipment_id": "B"}]} if payload["page_no"] == 1 else {"total": 3, "list": []}
        with self.assertRaisesRegex(RuntimeError, "phân trang"):
            client.fetch_window("pickup", 0, 100)

    def test_both_payloads_keep_launcher2_time_fields(self):
        client = SpxClient.__new__(SpxClient)
        client.settings = settings(Path("."))
        self.assertIn("create_time", client.payload("pickup", 1, 2))
        self.assertIn("pickup_station_ids", client.payload("pickup", 1, 2))
        self.assertIn("current_station_received_time", client.payload("delivery", 1, 2))
        self.assertIn("current_station_ids", client.payload("delivery", 1, 2))

    def test_auth_failure_is_not_retried_or_treated_as_zero(self):
        client = SpxClient.__new__(SpxClient)
        client.cookies = {"fms_user_skey": "test"}
        with patch("requests.Session") as session:
            request = session.return_value.post
            request.return_value.status_code = 401
            with self.assertRaises(AuthenticationError):
                client.post({})
            self.assertEqual(request.call_count, 1)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        catalog = patch("sdd_data.runner.load_seller_wards", return_value={})
        catalog.start()
        self.addCleanup(catalog.stop)
        routes = patch("sdd_data.runner.enrich_pickup_routes", return_value={})
        routes.start()
        self.addCleanup(routes.stop)

    def test_pickup_keeps_unknown_orders_and_delivery_still_skips_them(self):
        for job, zone_field, time_field in [("pickup", "pickup_zone_id", "create_time"),
                                             ("delivery", "zone_id", "current_station_received_time")]:
            with self.subTest(job=job), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                orders = [{"shipment_id": str(i), zone_field: zone, time_field: stamp()}
                          for i, zone in enumerate(["route-a", "missing", ""])]
                with patch("sdd_data.runner.load_coverage", return_value=MAPPING), \
                     patch("sdd_data.runner.SpxClient") as client, \
                     patch("sdd_data.runner.WebsiteStore") as store:
                    client.return_value.fetch.return_value = orders
                    store.return_value.commit_snapshot.return_value = {"upserted": 1, "stale_removed": 0}
                    result = run(settings(root), job, DAY, root=root)[0]
                    rows = store.return_value.commit_snapshot.call_args.args[2]
                    self.assertEqual(sum(row["total_orders"] for row in rows), 3 if job == "pickup" else 1)
                    if job == "pickup":
                        retry = json.loads(Path(result["unresolved_file"]).read_text(encoding="utf-8"))
                        self.assertEqual(retry["count"], 2)
                        self.assertEqual({order["shipment_id"] for order in retry["orders"]}, {"1", "2"})
                        self.assertEqual(set(Path(result["unresolved_ids_file"]).read_text(encoding="utf-8").splitlines()), {"1", "2"})
                        self.assertEqual(sum(row["total_orders"] for row in rows if row["area"] == "UNRESOLVED"), 2)
                    else:
                        self.assertEqual(result["skipped_unmapped"], 2)
                    blank = "seller_address_id:(trống)" if job == "pickup" else "(trống)"
                    self.assertEqual(result["stats"]["unmapped_zones"], {"missing": 1, blank: 1})
                    self.assertEqual(result["status"], "synced")
                    report = json.loads(next((root / "data").glob(f"{job}-{DAY}-*.json")).read_text(encoding="utf-8"))
                    if job == "pickup":
                        self.assertEqual(report["stats"]["unresolved"], 2)
                    else:
                        self.assertEqual(report["skipped_unmapped"], 2)

    def test_strict_mapping_and_invalid_times_still_block_writes(self):
        for zone, timestamp, options in [("missing", stamp(), {"allow_unmapped": False}),
                                          ("route-a", 0, {})]:
            with self.subTest(zone=zone, timestamp=timestamp), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with patch("sdd_data.runner.load_coverage", return_value=MAPPING), \
                     patch("sdd_data.runner.SpxClient") as client, \
                     patch("sdd_data.runner.WebsiteStore") as store:
                    client.return_value.fetch.return_value = [
                        {"shipment_id": "valid", "zone_id": "route-a", "current_station_received_time": stamp()},
                        {"shipment_id": "bad", "zone_id": zone, "current_station_received_time": timestamp}]
                    with self.assertRaisesRegex(RuntimeError, "không hợp lệ/chưa có mapping"):
                        run(settings(root), "delivery", DAY, root=root, **options)
                    store.assert_not_called()

    def test_demo_fixture_can_never_be_uploaded(self):
        with self.assertRaisesRegex(ValueError, "dry-run"):
            run(settings(Path(".")), "both", DAY, orders_json=Path("fake.json"))

    def test_offline_dry_run_never_constructs_a_database_client(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mapping = root / "map.csv"
            mapping.write_text("Zone ID,Zone,Phường mới\nroute-a,KV1,Phường A\n", encoding="utf-8")
            orders = root / "orders.json"
            orders.write_text(json.dumps({"pickup": [{"shipment_id": "demo", "pickup_zone_id": "route-a", "create_time": stamp()}], "delivery": []}))
            with patch("sdd_data.runner.WebsiteStore", side_effect=AssertionError("Database must not be accessed")):
                result = run(settings(root), "both", DAY, dry_run=True, mapping_csv=mapping, orders_json=orders, root=root)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["stats"]["included"], 1)

    def test_cookie_validation_does_not_print_secret_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.json"
            path.write_text(json.dumps([{"name": "fms_user_skey", "value": "must-not-be-logged", "expiry": time.time() - 5}]))
            with self.assertRaises(AuthenticationError) as raised:
                read_cookies(path)
            self.assertNotIn("must-not-be-logged", str(raised.exception))

    def test_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.lock"
            with self.assertRaises(ValueError):
                with RunLock(path):
                    raise ValueError("intentional")
            with RunLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
