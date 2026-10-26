import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from sdd_data.return_inventory import ReturnClient, build_returns, resolve_seller, return_points, UNKNOWN_AREA

MAPPING = {'z3': {'area':'KV3','source_area':'KV4','district':'Quận Bình Tân','old_ward':'Phường Bình Hưng Hòa A','ward':'Phường Bình Hưng Hòa'},
           'z5': {'area':'KV5','source_area':'KV6','district':'Quận 12','old_ward':'Phường Tân Thới Hiệp','ward':'Phường Tân Thới Hiệp'}}


def row(sid='TEST', status=67, **extra):
    return {'shipment_id':sid,'order_status':status,'current_station_id':4232,'create_time':1792800000, **extra}


class ReturnTests(unittest.TestCase):
    def test_five_area_mapping_and_cot_rider_candidates(self):
        rows, stats = build_returns([row(lowest_seller_address_id='1')], MAPPING, {'1':'Phường Bình Hưng Hòa'},
            {'TEST':'P1'}, {'P1':'BT_BHHA1_SDD'}, {},
            [{'rider_code':'1','full_name':'A','status':'active','point_name':'BT_BHHA1_SDD','cot':'COT 1'},
             {'rider_code':'2','full_name':'B','status':'active','point_name':'BT_BHHA1_SDD','cot':'COT 2'},
             {'rider_code':'3','full_name':'C','status':'inactive','point_name':'BT_BHHA1_SDD','cot':'COT 1'}], '4232')
        self.assertEqual(rows[0]['seller_area'], 'Khu vực 3')
        self.assertEqual(rows[0]['seller_district'], 'Quận Bình Tân')
        self.assertIn('1 · A', rows[0]['return_riders_cot1'])
        self.assertIn('2 · B', rows[0]['return_riders_cot2'])
        self.assertNotIn('3', rows[0]['return_rider_codes'])
        self.assertTrue(stats['source_complete'])

    def test_no_buyer_zone_fallback_and_no_unknown_orders_dropped(self):
        rows, stats = build_returns([row(zone_id='z3')], MAPPING, {}, {}, {}, {}, [], '4232')
        self.assertEqual(rows[0]['seller_area'], UNKNOWN_AREA)
        self.assertEqual(rows[0]['seller_zone_id'], '')
        self.assertEqual(stats['included'], 1)

    def test_current_returning_driver_is_distinct_from_candidate_riders(self):
        rows, _ = build_returns([row(status=72, driver_id=99, driver_name='Current')], {}, {}, {}, {}, {}, [], '4232')
        self.assertEqual(rows[0]['return_driver_id'], '99')
        rows, _ = build_returns([row(status=10, driver_id=99)], {}, {}, {}, {}, {}, [], '4232')
        self.assertEqual(rows[0]['return_driver_id'], '')

    def test_invalid_status_station_and_duplicate_rejected(self):
        for records in ([row(status=4)], [row(current_station_id=1)], [row(),row()]):
            with self.assertRaises(RuntimeError):
                build_returns(records, {}, {}, {}, {}, {}, [], '4232')

    def test_complete_empty_and_incomplete_source(self):
        client = ReturnClient.__new__(ReturnClient)
        client.settings = SimpleNamespace(station_id='4232', workers=2)
        client.post = Mock(return_value={'total':0,'list':[]})
        self.assertEqual(client.fetch(), [])
        client.post.return_value = {'total':1,'list':[row(status=4)]}
        with self.assertRaises(RuntimeError):
            client.fetch()

    def test_parallel_pages_checked_against_total_and_unique_ids(self):
        client = ReturnClient.__new__(ReturnClient)
        client.settings = SimpleNamespace(station_id='4232', workers=2)
        client.post = lambda payload: {'total':3, 'list':[row('A'),row('B')] if payload['page_no'] == 1 else [row('C')]}
        self.assertEqual([r['shipment_id'] for r in client.fetch()], ['A','B','C'])
        client.post = lambda payload: {'total':3, 'list':[row('A'),row('B')] if payload['page_no'] == 1 else [row('B')]}
        with self.assertRaises(RuntimeError):
            client.fetch()

    def test_pickup_info_identity_verified_and_positive_cache_reused(self):
        settings = SimpleNamespace(station_id='4232', workers=2)
        client = Mock()
        client.get.return_value = {'shipment_id':'TEST','pickup_info_list':[{'pickup_point_id':'P1'}]}
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(return_points(settings, [row()], client, Path(folder)), {'TEST':'P1'})
            client.get.reset_mock()
            self.assertEqual(return_points(settings, [row()], client, Path(folder)), {'TEST':'P1'})
            client.get.assert_not_called()
        client.get.return_value = {'shipment_id':'WRONG','pickup_info_list':[]}
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(RuntimeError):
            return_points(settings, [row()], client, Path(folder))


if __name__ == '__main__':
    unittest.main()
