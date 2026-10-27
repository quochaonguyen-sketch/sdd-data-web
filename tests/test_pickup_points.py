import unittest
from unittest.mock import Mock, patch
from sdd_data.pickup_points import daily_counts, PointsClient, publish_daily, fetch_shop_orders

class PickupPointsTests(unittest.TestCase):
    def test_counts_by_point_not_shop_name_and_preserves_unmapped_count(self):
        rows,missing=daily_counts([{'shipment_id':'1','pickup_point_id':'A'},
          {'shipment_id':'2','pickup_point_id':'A'},{'shipment_id':'3','pickup_point_id':'B'},{'shipment_id':'4'}])
        self.assertEqual(rows,[{'pickup_point_id':'A','total_orders':2},{'pickup_point_id':'B','total_orders':1}])
        self.assertEqual(missing,1)

    def test_duplicate_orders_fail_before_publication(self):
        with self.assertRaises(ValueError): daily_counts([{'shipment_id':'1'},{'shipment_id':'1'}])

    def test_refuses_cross_hub_details(self):
        client=PointsClient.__new__(PointsClient);client.settings=Mock(station_id='4232')
        client.get=Mock(return_value={'extra':{'pickup_point_id':'A','station_id':1}})
        with self.assertRaises(RuntimeError):client.point('A')
        self.assertEqual(client.get.call_args.args[0],'pickup_point/detail')

    def test_fetches_catalog_without_area_or_driver_sheet(self):
        c=PointsClient.__new__(PointsClient);c.settings=Mock(station_id='4232')
        c.get=Mock(side_effect=[{'total':1,'list':[{'id':1,'station_id':4232,'group_name':'KV5_ROUTE','pickup_point_id_list':'A,B'}]},
          {'total':1,'list':[{'pickup_point_id':'C'}]}])
        rows=c.catalog()
        self.assertEqual(set(rows),{'A','B','C'})
        self.assertEqual(rows['A'],{'KV5_ROUTE'})

    def test_confirmed_orders_without_pup_are_not_unknown_shop_volume(self):
        with patch('sdd_data.pickup_points.PickupRouteClient') as client, patch('sdd_data.pickup_points.Control') as control:
            client.return_value.get.return_value={'shipment_id':'2','pickup_info_list':[]}
            publish_daily(Mock(station_id='4232'),'2026-10-26',[{'shipment_id':'1','pickup_point_id':'A'},{'shipment_id':'2'}])
            payload=control.return_value.rpc.call_args.kwargs
            self.assertEqual(payload['p_input'],1)
            self.assertEqual(payload['p_unmapped'],0)
            client.return_value.close.assert_called_once()

    def test_unavailable_mapping_remains_unknown_instead_of_zero(self):
        with patch('sdd_data.pickup_points.PickupRouteClient') as client, patch('sdd_data.pickup_points.Control') as control:
            client.return_value.get.side_effect=RuntimeError('unavailable')
            publish_daily(Mock(station_id='4232'),'2026-10-26',[{'shipment_id':'1'}])
            self.assertEqual(control.return_value.rpc.call_args.kwargs['p_unmapped'],1)

    def test_stop_file_prevents_any_spx_read(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            stop=Path(folder)/'stop';stop.touch()
            client=PointsClient.__new__(PointsClient);client.stop_file=stop
            with self.assertRaises(InterruptedError):client.get('pickup_point/detail',{})

    def test_history_reads_every_hour_and_rejects_wrong_dates(self):
        from datetime import datetime
        from sdd_data.transform import TZ
        with patch('sdd_data.clients.SpxClient') as cls:
            client=cls.return_value
            client.payload.side_effect=lambda kind,left,right:{'left':left}
            client.post.side_effect=lambda payload:{'total':1,'list':[{'shipment_id':str(payload['left']),'create_time':payload['left']}]}
            self.assertEqual(len(fetch_shop_orders(Mock(workers=2),'2026-10-26')),24)
            client.close.assert_called_once()
            wrong=int(datetime(2026,10,25,tzinfo=TZ).timestamp())
            client.post.side_effect=lambda payload:{'total':1,'list':[{'shipment_id':str(payload['left']),'create_time':wrong}]}
            with self.assertRaisesRegex(RuntimeError,'sai ngày'):fetch_shop_orders(Mock(workers=2),'2026-10-26')
