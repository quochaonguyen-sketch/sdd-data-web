import unittest
from datetime import datetime

from sdd_data.clients import volume_fields
from sdd_data.delivery_inventory import build_inventory
from sdd_data.transform import TZ, coverage_map, match_zone, summarize


HEADERS = ["Khu vực", "Phường mới", "Quận cũ", "Phường cũ", "Zone ID",
           "Zone ID+Hub_Code_NDD", "Khu vực v2", "Zone ID v2"]


class CoverageAliasesTests(unittest.TestCase):
    def setUp(self):
        self.mapping = coverage_map([HEADERS,
            ["Khu vực 5", "Phường Nhiêu Lộc", "Quận 3", "Phường 9", "Z5.01.NL.01",
             "Z5.01.NL.01-A51-158", "Khu vực 4", "Z4.01.NL.01"],
            ["Khu vực 6", "Phường Tân Thới Hiệp", "Quận 12", "Phường Hiệp Thành", "Z6.01.TTH.01",
             "Z6.02.TTH.01-B51-73", "Khu vực 5", "Z5.01.TTHIEP.01"],
            ["Khu vực 6", "Phường Thông Tây Hội", "Quận Gò Vấp", "Phường 8", "Z6.01.TTH.01",
             "Z6.01.TTH.01-B51-93", "Khu vực 5", "Z5.01.TTHOI.01"],
            ["Khu vực 4", "Phường Sài Gòn", "Quận 1", "Phường Bến Nghé", "Z4.01.SG.01.#",
             "Z4.01.SG.01.#-A50-01", "Khu vực 4", "Z4.01.SG.01.#"],
        ])

    def test_old_new_and_hub_suffixes_share_current_geography(self):
        for code in ("Z5.01.NL.01", "Z4.01.NL.01", "Z5.01.NL.01-A51-158", "Z4.01.NL.01-A51-158"):
            info = match_zone(code, self.mapping)[1]
            self.assertEqual((info["area"], info["district"], info["old_ward"]), ("KV4", "Quận 3", "Phường 9"))

    def test_hub_base_corrects_catalog_zone_typo_without_prefix_guessing(self):
        self.assertEqual(match_zone("Z6.02.TTH.01", self.mapping)[1]["district"], "Quận 12")
        self.assertEqual(match_zone("Z6.02.TTH.01-B51-73", self.mapping)[1]["area"], "KV5")
        self.assertEqual(match_zone("Z6.02.TTH.99", self.mapping), ("", {}))
        self.assertEqual(match_zone("Z4.01.SG.01", self.mapping)[1]["old_ward"], "Phường Bến Nghé")

    def test_reused_code_requires_receiver_locality(self):
        self.assertEqual(match_zone("Z6.01.TTH.01", self.mapping), ("", {}))
        self.assertEqual(match_zone("Z6.01.TTH.01", self.mapping, ward_hint="Phường Hiệp Thành")[1]["district"], "Quận 12")
        self.assertEqual(match_zone("Z6.01.TTH.01", self.mapping, district_hint="Quận Gò Vấp")[1]["old_ward"], "Phường 8")
        self.assertEqual(match_zone("Z6.01.TTH.01", self.mapping, ward_hint="Phường khác"), ("", {}))

    def test_delivery_uses_buyer_ward_id_for_reused_code_and_keeps_cot(self):
        timestamp = int(datetime(2026, 10, 22, 7, tzinfo=TZ).timestamp())
        rows, stats = summarize("delivery", [{"shipment_id": "A", "zone_id": "Z6.01.TTH.01",
            "lowest_buyer_address_id": 100, "current_station_received_time": timestamp}],
            self.mapping, "2026-10-22", ("KV5",), seller_wards={"100": "Phường Tân Thới Hiệp"})
        self.assertEqual(stats["included"], 1)
        self.assertEqual(rows[0]["old_ward"], "Phường Hiệp Thành")
        self.assertEqual(rows[0]["cot"], "2026-10-22 COT 1.1")

    def test_buyer_district_retained_without_private_address(self):
        fields = volume_fields({"buyer_address": "123 đường riêng, Phường 8, Quận Gò Vấp, Hồ Chí Minh"})
        self.assertEqual(fields["buyer_district_name"], "Quận Gò Vấp")
        self.assertNotIn("buyer_address", fields)

    def test_inventory_uses_same_old_new_aliases_and_buyer_hints(self):
        timestamp = int(datetime(2026, 10, 22, 7, tzinfo=TZ).timestamp())
        rows, stats = build_inventory([{"shipment_id": "A", "zone_id": "Z6.01.TTH.01",
            "order_status": 1, "create_time": timestamp - 3600,
            "current_station_received_time": timestamp, "lowest_buyer_address_id": 100}],
            self.mapping, ("KV5",), {"100": "Phường Tân Thới Hiệp"})
        self.assertEqual(stats["included"], 1)
        self.assertEqual((rows[0]["area"], rows[0]["ward"]), ("KV5", "Phường Hiệp Thành"))

    def test_strict_mode_rejects_conflicting_aliases(self):
        with self.assertRaises(ValueError):
            coverage_map([HEADERS,
                ["6", "A", "Quận 12", "A", "same", "", "5", "new-a"],
                ["6", "B", "Quận Gò Vấp", "B", "same", "", "5", "new-b"],
            ], duplicate_policy="error")


if __name__ == "__main__":
    unittest.main()
