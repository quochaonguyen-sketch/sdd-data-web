from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdd_data.config import Settings
from sdd_data.pickup_routes import AuthenticationError, PickupRouteClient, enrich_pickup_routes, group_routes, pickup_point
from sdd_data.transform import TZ, summarize, confirmed_pickup_location


class PickupRouteTests(unittest.TestCase):
    def test_bulk_skips_future_hours_and_accepts_zero_total_without_list(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client.settings = type('Config', (), {'station_id':'4232', 'workers':4})()
        now = datetime(2026,10,25,10,30,tzinfo=TZ)
        with patch('sdd_data.pickup_routes.datetime', wraps=datetime) as clock, patch.object(client, 'get', return_value={'total':0}) as get:
            clock.now.return_value = now
            self.assertEqual(client.bulk_points('2026-10-25', {'A'}), {})
            self.assertEqual(get.call_count, 11)
            self.assertTrue(all(int(call.args[1]['ctime'].split(',')[1]) < now.timestamp() for call in get.call_args_list))

    def test_bulk_incomplete_index_keeps_good_matches_for_detail_fallback(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client.settings = type('Config', (), {'station_id':'4232', 'workers':4})()
        def read(path, params):
            left = int(params['ctime'].split(',')[0])
            if datetime.fromtimestamp(left,TZ).hour == 20:
                return {'total':1}  # Index failure must not discard authoritative tracking rows.
            return {'total':1, 'list':[{'pickup_order_id':'A','pickup_point_id':'point','pickup_station_id':'4232'}]}
        with patch.object(client, 'get', side_effect=read):
            self.assertEqual(client.bulk_points('2026-10-23', {'A','B'}), {'A':'point'})
        with patch.object(client, 'get', side_effect=AuthenticationError('expired')):
            with self.assertRaises(AuthenticationError):
                client.bulk_points('2026-10-23', {'A'})

    def test_user_confirmed_prefixes_and_ward_use_former_districts(self):
        mapping = {
            "one": {"area":"KV3", "district":"Quận Bình Tân", "old_ward":"Phường Bình Hưng Hòa A", "ward":"Phường Bình Hưng Hòa"},
            "two": {"area":"KV3", "district":"Quận Tân Phú", "old_ward":"Phường Hòa Thạnh", "ward":"Phường Tân Phú"},
        }
        for route in ("BT_BHHA1_SDD", "BT_BHHA1_SDD-1"):
            self.assertEqual(confirmed_pickup_location(route, "Chưa rõ phường/xã", mapping),
                {"area":"KV3", "district":"Quận Bình Tân", "ward":"Phường Bình Hưng Hòa"})
        self.assertEqual(confirmed_pickup_location("TP_Hoa Thanh02_SDD", "", mapping),
            {"area":"KV3", "district":"Quận Tân Phú", "ward":"Phường Tân Phú"})
        self.assertEqual(confirmed_pickup_location("", "Phường Bình Hưng Hòa", mapping)["district"], "Quận Bình Tân")
        self.assertEqual(confirmed_pickup_location("BTH_P1301_SDD", "", mapping)["district"], "Quận Bình Thạnh")
        self.assertEqual(confirmed_pickup_location("BT_Unmapped01_SDD", "", mapping)["ward"], "Chưa rõ phường/xã")

    def test_numbered_districts_ward_numbers_and_catalog_abbreviations(self):
        mapping = {
            "a": {"area":"KV2", "district":"Quận Bình Thạnh", "old_ward":"Phường 13", "ward":"Phường Bình Lợi Trung"},
            "b": {"area":"KV1", "district":"Quận 6", "old_ward":"Phường 3", "ward":"Phường Bình Tây"},
            "c": {"area":"KV5", "district":"Quận 12", "old_ward":"Phường Đông Hưng Thuận", "ward":"Phường Đông Hưng Thuận"},
            "d": {"area":"KV5", "district":"Quận Gò Vấp", "old_ward":"Phường 1", "ward":"Phường Hạnh Thông"},
        }
        for route, area, district, ward in [
            ("BTH_P1301_SDD", "KV2", "Quận Bình Thạnh", "Phường Bình Lợi Trung"),
            ("Q6_P03_1_SDD", "KV1", "Quận 6", "Phường Bình Tây"),
            ("Q12_PDHT01_SDD", "KV5", "Quận 12", "Phường Đông Hưng Thuận"),
            ("GV_P01_SDD", "KV5", "Quận Gò Vấp", "Phường Hạnh Thông"),
        ]:
            self.assertEqual(confirmed_pickup_location(route, "", mapping), {"area":area,"district":district,"ward":ward})
        for number in range(1,13):
            self.assertEqual(confirmed_pickup_location(f"Q{number}_Unknown_SDD", "", mapping)["district"], f"Quận {number}")
        self.assertIsNone(confirmed_pickup_location("Q13_P01_SDD", "", mapping))

    def test_bulk_splits_time_before_the_spx_ten_thousand_record_limit(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client.settings = type("Config", (), {"station_id": "4232", "workers": 2})()
        calls = []
        def read(path, params):
            left, right = map(int, params["ctime"].split(","))
            calls.append(params)
            return {"total": 10000 if right-left > 1800 else 0, "count":200,"list":[]}
        with patch.object(client, "get", side_effect=read):
            self.assertEqual(client.bulk_points("2026-10-23", {"A"}), {})
        self.assertEqual(len(calls), 72)  # 24 hours, each split into two halves
        self.assertTrue(all(call["pageno"] == 1 for call in calls))

    def test_bulk_index_requires_exact_shipment_and_station_and_rejects_conflicting_points(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client.settings = type("Config", (), {"station_id": "4232", "workers": 2})()
        pages = {
            1: [{"pickup_order_id": "A", "pickup_point_id": "one", "pickup_station_id": 4232},
                {"pickup_order_id": "B", "pickup_point_id": "wrong-station", "pickup_station_id": 999}],
            2: [{"pickup_order_id": "A", "pickup_point_id": "two", "pickup_station_id": 4232},
                {"pickup_order_id": "C", "pickup_point_id": "three", "pickup_station_id": 4232}],
            3: [{"pickup_order_id": "EXTRA", "pickup_point_id": "extra", "pickup_station_id": 4232},
                {"pickup_order_id": "B", "pickup_point_id": "", "pickup_station_id": 4232}],
        }
        with patch.object(client, "get", side_effect=lambda path, args: {"total": 6, "count": 2, "list": pages[args["pageno"]]}):
            self.assertEqual(client.bulk_points("2026-10-23", {"A", "B", "C"}), {"C": "three"})

    def test_bulk_misses_use_details_instead_of_dropping_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(root, root, root/'cookie', root/'sa', 'https://example.supabase.co', 'test')
            orders = [{"shipment_id": str(i)} for i in range(201)]
            with patch("sdd_data.pickup_routes.PickupRouteClient") as cls:
                client = cls.return_value
                client.routes.return_value = ({"point": "route"}, 0)
                client.bulk_points.return_value = {str(i): "point" for i in range(200)}
                client.get.return_value = {"shipment_id": "200", "pickup_info_list": [{"pickup_point_id": "point"}]}
                stats = enrich_pickup_routes(settings, orders, "2026-10-23", root)
                self.assertEqual(stats["pickup_route_found"], 201)
                self.assertEqual(client.get.call_count, 1)

    def test_auth_failure_stops_without_treating_it_as_an_empty_point(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        with patch.object(client, "session") as session:
            session.return_value.get.return_value.status_code = 403
            with self.assertRaises(AuthenticationError):
                client.get("order/detail/pickup_info", {})
            self.assertEqual(session.return_value.get.call_count, 1)

    def test_latest_event_and_ambiguous_points(self):
        self.assertEqual(pickup_point({"pickup_info_list": []}), "")
        self.assertEqual(pickup_point({"pickup_info_list": [
            {"pickup_point_id": "old", "assigned_time": 100},
            {"pickup_point_id": "new", "pickedup_time": 200}]}), "new")
        self.assertEqual(pickup_point({"pickup_info_list": [
            {"pickup_point_id": "a", "assigned_time": 100},
            {"pickup_point_id": "b", "assigned_time": 100}]}), "")
        with self.assertRaises(RuntimeError):
            pickup_point({})

    def test_placeholder_groups_and_conflicts_are_not_routes(self):
        routes, conflicts = group_routes([
            {"group_name": "all", "pickup_point_id_list": "1,2,3"},
            {"group_name": "no group", "pickup_point_id_list": "1,4"},
            {"group_name": "PICK_A", "pickup_point_id_list": "1,2"},
            {"group_name": "PICK_B", "pickup_point_id_list": ["2", "3"]}])
        self.assertEqual(routes, {"1": "PICK_A", "3": "PICK_B"})
        self.assertEqual(conflicts, 1)

    def test_routes_paginate_and_reject_incomplete_snapshot(self):
        client = PickupRouteClient.__new__(PickupRouteClient)
        client.settings = type("Config", (), {"station_id": "4232"})()
        with patch.object(client, "get", side_effect=[
            {"total": 201, "list": [{"id": 0, "group_name": "all"}, {"id": 1, "group_name": "A", "pickup_point_id_list": "1"}]},
            {"total": 201, "list": [{"id": 0, "group_name": "all"}, {"id": 2, "group_name": "B", "pickup_point_id_list": "2"}]}]):
            self.assertEqual(client.routes()[0], {"1": "A", "2": "B"})
        with patch.object(client, "get", side_effect=[
            {"total": 201, "list": [{"id": 1}]}, {"total": 201, "list": []}]):
            with self.assertRaises(RuntimeError):
                client.routes()

    def test_positive_cache_reuses_points_but_refreshes_route_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(root, root, root/'cookie', root/'sa', 'https://example.supabase.co', 'test')
            orders = [{"shipment_id": "A"}, {"shipment_id": "B"}]
            with patch("sdd_data.pickup_routes.PickupRouteClient") as cls:
                client = cls.return_value
                client.routes.return_value = ({"point": "route-1"}, 0)
                client.get.side_effect = lambda path, params: {"shipment_id": params["shipment_id"],
                    "pickup_info_list": [{"pickup_point_id": "point"}] if params["shipment_id"] == "A" else []}
                stats = enrich_pickup_routes(settings, orders, "2026-10-23", root)
                self.assertEqual(stats["pickup_route_found"], 1)
                self.assertEqual(orders[0]["pickup_route_name"], "route-1")
                client.get.reset_mock()
                client.routes.return_value = ({"point": "route-2"}, 0)
                stats = enrich_pickup_routes(settings, orders, "2026-10-23", root)
                self.assertEqual(orders[0]["pickup_route_name"], "route-2")
                self.assertEqual(client.get.call_count, 1)  # retry only the unresolved B
                self.assertEqual(stats["pickup_point_cached"], 1)

    def test_mismatched_shipment_response_blocks_enrichment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(root, root, root/'cookie', root/'sa', 'https://example.supabase.co', 'test')
            with patch("sdd_data.pickup_routes.PickupRouteClient") as cls:
                cls.return_value.routes.return_value = ({"point": "route"}, 0)
                cls.return_value.get.return_value = {"shipment_id": "OTHER", "pickup_info_list": [{"pickup_point_id": "point"}]}
                with self.assertRaisesRegex(RuntimeError, "vận đơn khác"):
                    enrich_pickup_routes(settings, [{"shipment_id": "A"}], "2026-10-23", root)

    def test_pickup_route_overrides_destination_and_preserves_address_fallback(self):
        stamp = int(datetime(2026,10,23,7,tzinfo=TZ).timestamp())
        mapping = {"pickup-a": {"area": "KV1", "district": "Quận A", "ward": "Phường A"}}
        rows, _ = summarize("pickup", [{"shipment_id": "A", "create_time": stamp,
            "pickup_route_name": "pickup-a", "zone_id": "buyer-route"}], mapping, "2026-10-23", ("KV1",))
        self.assertEqual(rows[0]["ma_tuyen"], "pickup-a")
        rows, _ = summarize("pickup", [{"shipment_id": "B", "create_time": stamp,
            "pickup_route_name": "PICKUP_B", "lowest_seller_address_id": 123}], mapping,
            "2026-10-23", ("KV1",), seller_wards={"123": "Phường A"})
        self.assertEqual(rows[0]["ma_tuyen"], "PICKUP_B")
        self.assertEqual(rows[0]["area"], "KV1")

    def test_two_routes_in_one_ward_keep_separate_counts(self):
        stamp = int(datetime(2026,10,23,7,tzinfo=TZ).timestamp())
        mapping = {key: {"area": "KV1", "district": "Quận A", "ward": "Phường A"} for key in ("a", "b")}
        rows, _ = summarize("pickup", [{"shipment_id": str(i), "create_time": stamp, "pickup_route_name": route}
            for i, route in enumerate(["a", "a", "b"])], mapping, "2026-10-23", ("KV1",))
        self.assertEqual({r["ma_tuyen"]: r["total_orders"] for r in rows}, {"a":2,"b":1})


if __name__ == "__main__":
    unittest.main()
