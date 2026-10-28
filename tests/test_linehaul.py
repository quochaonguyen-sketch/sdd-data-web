from datetime import datetime, time
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sdd_data.linehaul import build_trip, packs, gps_point, LinehaulClient, prepare_linehaul, count, dashboard_packs, build_dashboard_trip
from sdd_data.transform import TZ

DAY=datetime.now(TZ).date()
MID=int(datetime.combine(DAY,time.min,TZ).timestamp())

def raw(**extra):
    return {'id':1,'trip_number':'LT-TEST','trip_date':MID,'driver':99,'driver_name':'Test Driver',
            'vehicle_number':'TEST-PLATE','vehicle_type_name':'VAN','pack_type':'10Bao 90x70,237NA',
            'order_loaded_quantity':807,'to_loaded_quantity':247,'current_sequence_number':1,'display_station_sequence':2,
            'trip_station':[{'station':4289,'sequence_number':1,'station_name':'Origin','atd':MID+100},
                            {'station':4232,'sequence_number':2,'station_name':'Hub','sta':MID+3600,'eta':MID+3600}],**extra}

def dashboard(**extra):
    return {'trip_number':'LTTEST001','station_id':4232,'arrived_station_id':4232,'driver_id':99,'driver_name':'Test driver',
            'trip_sta':MID+3600,'trip_ata':MID+4000,'trip_atd':MID+300,'trip_eta':MID+3900,'trip_status':3,
            'departed_station_name':'Origin','arrived_station_name':'Hub','vehicle_group_type':'VAN','vehicle_number':'TEST',
            'trip_order_qty_td':234,'trip_to_qty_td':35,'to_pack_type_name_list':'Bao 90x70 10,Other 25',**extra}


class LinehaulTests(unittest.TestCase):
    def test_today_assigned_driver_and_hub_are_required(self):
        for extra in [{'driver_name':''},{'driver':0}]:
            self.assertIsNone(build_trip(raw(**extra),'4232',DAY))
        self.assertIsNone(build_trip(raw(),'other',DAY))
        row=build_trip(raw(),'4232',DAY)
        self.assertEqual((row['orders'],row['bags'],row['loose']),(807,10,237))
        self.assertEqual((row['direction'],row['phase']),('inbound','en_route'))
        self.assertIsNotNone(build_trip(raw(trip_date=MID-86400),'4232',DAY))
        outside=raw();outside['trip_station'][1]['sta']=MID-1
        self.assertIsNone(build_trip(outside,'4232',DAY))

    def test_pack_types_and_unknown_loads_do_not_become_zero(self):
        self.assertEqual(packs('10Bao 90x70,237NA'),(10,237,''))
        self.assertEqual(packs('14 Bao 90x70, 2NA'),(14,2,''))
        self.assertEqual(packs(''),(None,None,''));self.assertEqual(packs('new format'),(None,None,'new format'))
        self.assertIsNone(count(-1));self.assertEqual(count(0),0)
        with self.assertRaises(ValueError):count(-2)

    def test_latest_valid_gps_fix_is_selected_and_hub_is_not_a_vehicle_fix(self):
        now=int(datetime.now(TZ).timestamp())
        result=gps_point({'location_list':[{'lat':10,'lng':106,'upload_time':now-60},{'lat':11,'lng':107,'upload_time':now-120}]})
        self.assertEqual((result['lat'],result['lng']),(10,106))
        self.assertIsNone(gps_point({'trip_station':[{'lat':10,'lng':106}]})['lat'])
        self.assertIsNone(gps_point({'location_list':[{'lat':999,'lng':106,'upload_time':now}]})['lat'])

    def test_count_100_and_vietnam_today_window_with_complete_pagination(self):
        client=LinehaulClient.__new__(LinehaulClient)
        rows=[{'id':i} for i in range(1,158)]
        def get(path,params):
            self.assertEqual(params['count'],100)
            self.assertEqual(params['sta'],f'{MID},{MID+86399}')
            return {'total':157,'list':rows[(params['pageno']-1)*100:params['pageno']*100]}
        client.get=Mock(side_effect=get)
        self.assertEqual(len(client.trips(DAY)),157);self.assertEqual(client.get.call_count,2)

    def test_dashboard_uses_post_and_validates_full_list(self):
        client=LinehaulClient.__new__(LinehaulClient);client.pace=Mock()
        session=Mock();client.session=Mock(return_value=session)
        session.post.return_value=Mock(status_code=200,json=Mock(return_value={'retcode':0,'data':{'list':[dashboard()]}}))
        self.assertEqual(len(client.dashboard_trips()),1)
        session.get.assert_not_called()
        self.assertEqual(session.post.call_args.args[0],'https://spx.shopee.vn/mgmt/api/pc/dashboard/lm/linehaul_trips')
        session.post.return_value=Mock(status_code=200,json=Mock(return_value={'retcode':0,'data':{'list':[dashboard(),dashboard()]}}))
        with self.assertRaisesRegex(RuntimeError,'trùng'):client.dashboard_trips()
        session.post.return_value=Mock(status_code=200,json=Mock(return_value={'retcode':0,'data':{}}))
        with self.assertRaisesRegex(RuntimeError,'thiếu'):client.dashboard_trips()

    def test_dashboard_load_fields_and_arrival_are_not_unload_or_hub_departure(self):
        trip=build_dashboard_trip(dashboard(),'4232',DAY)
        self.assertEqual((trip['orders'],trip['bags'],trip['loose'],trip['load_total']),(234,10,25,35))
        self.assertEqual(trip['source'],'dashboard');self.assertEqual(trip['phase'],'at_hub')
        self.assertIsNone(trip['hub_unloaded_at']);self.assertIsNone(trip['hub_atd']);self.assertIsNone(trip['unloaded_units'])
        self.assertEqual(trip['origin_atd'],datetime.fromtimestamp(MID+300,TZ).isoformat())
        for extra in [{'driver_name':''},{'driver_id':0},{'station_id':1},{'arrived_station_id':1},
                      {'trip_ata':MID-1},{'trip_sta':MID-1},{'trip_ata':0},{'trip_status':2}]:
            self.assertIsNone(build_dashboard_trip(dashboard(**extra),'4232',DAY))

    def test_dashboard_pack_type_is_quantity_last(self):
        self.assertEqual(dashboard_packs('Bao 90x70 10,Other 25'),(10,25,''))
        self.assertEqual(dashboard_packs('Other 50'),(0,50,''))
        self.assertEqual(dashboard_packs(''),(None,None,''))
        self.assertEqual(dashboard_packs('new format'),(None,None,'new format'))

    def test_dashboard_wins_duplicate_lt_without_gps_fetch(self):
        settings=SimpleNamespace(station_id='4232');live=raw(trip_number='LTTEST001')
        with tempfile.TemporaryDirectory() as folder,patch('sdd_data.linehaul.LinehaulClient') as cls:
            client=cls.return_value;client.trips.return_value=[live];client.dashboard_trips.return_value=[dashboard()]
            rows,_=prepare_linehaul(settings,Path(folder))
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['trip_id'],'1')
            self.assertEqual((rows[0]['orders'],rows[0]['bags'],rows[0]['loose']),(234,10,25))
            self.assertIsNone(rows[0]['gps_at']);client.get.assert_not_called()

    def test_dashboard_order_total_keeps_actual_hub_unloading_evidence(self):
        settings=SimpleNamespace(station_id='4232');live=raw(trip_number='LTTEST001',order_loaded_quantity=-1,to_loaded_quantity=-1)
        live['trip_station'][1].update(ata=MID+2000,expect_unload_quantity=12,unload_quantity=12,unloaded_time=MID+2500)
        with tempfile.TemporaryDirectory() as folder,patch('sdd_data.linehaul.LinehaulClient') as cls:
            client=cls.return_value;client.trips.return_value=[live];client.dashboard_trips.return_value=[dashboard(trip_order_qty_td=71,trip_to_qty_td=12)]
            rows,_=prepare_linehaul(settings,Path(folder))
            self.assertEqual(len(rows),1)
            self.assertEqual((rows[0]['orders'],rows[0]['load_total'],rows[0]['expected_unload'],rows[0]['unloaded_units']),(71,12,12,12))
            self.assertIsNotNone(rows[0]['hub_unloaded_at']);self.assertEqual(rows[0]['phase'],'unloaded')
            client.get.assert_not_called()

    def test_duplicate_or_missing_pages_never_become_a_complete_snapshot(self):
        client=LinehaulClient.__new__(LinehaulClient)
        client.get=Mock(return_value={'total':2,'list':[{'id':1},{'id':1}]})
        with self.assertRaisesRegex(RuntimeError,'phân trang'):client.trips(DAY)

    def test_business_rate_limit_is_retried_but_auth_error_is_not(self):
        client=LinehaulClient.__new__(LinehaulClient);client.pace=Mock()
        session=Mock();client.session=Mock(return_value=session)
        session.get.side_effect=[Mock(status_code=200,json=Mock(return_value={'retcode':130008001})),
                                 Mock(status_code=200,json=Mock(return_value={'retcode':0,'data':{'ok':True}}))]
        self.assertEqual(client.get('location',{'trip_id':1}),{'ok':True});self.assertEqual(session.get.call_count,2)
        session.get.reset_mock(side_effect=True);session.get.return_value=Mock(status_code=403)
        with self.assertRaisesRegex(RuntimeError,'403'):client.get('location',{'trip_id':1})
        self.assertEqual(session.get.call_count,1)

    def test_vehicle_cache_is_reused_but_never_carried_to_another_vehicle(self):
        settings=SimpleNamespace(station_id='4232')
        with tempfile.TemporaryDirectory() as folder,patch('sdd_data.linehaul.LinehaulClient') as cls:
            client=cls.return_value;client.dashboard_trips.return_value=[];client.trips.return_value=[raw(),raw(id=2,driver_name='')]
            client.get.return_value={'location_list':[{'lat':10,'lng':106,'upload_time':int(datetime.now(TZ).timestamp())}],'trip_station':[]}
            result,_=prepare_linehaul(settings,Path(folder));self.assertEqual(len(result),1)
            client.get.reset_mock();prepare_linehaul(settings,Path(folder));client.get.assert_not_called()
            client.trips.return_value=[raw(vehicle_number='DIFFERENT')];client.get.side_effect=RuntimeError('unavailable')
            result,_=prepare_linehaul(settings,Path(folder));self.assertIsNone(result[0]['lat']);self.assertTrue(result[0]['location_error'])

    def test_publish_boundary_precedes_database_write(self):
        from sdd_data.source_job import execute
        boundary=Mock(publishing=False,job='job',worker='worker');events=[]
        def begin():events.append('boundary');boundary.publishing=True
        boundary.begin.side_effect=begin;boundary.control.rpc.side_effect=lambda *a,**k:events.append('write')
        with patch('sdd_data.linehaul.prepare_linehaul',return_value=([],datetime.now(TZ))):
            execute(SimpleNamespace(station_id='4232'),'linehaul',DAY.isoformat(),boundary)
        self.assertEqual(events,['boundary','write'])

    def test_unloading_phase_on_unseal_or_partial_unload(self):
        r_unseal = raw()
        r_unseal['trip_station'][1].update(ata=MID+1000, unseal_time=MID+1200)
        trip = build_trip(r_unseal, '4232', DAY)
        self.assertEqual(trip['phase'], 'unloading')
        self.assertIsNotNone(trip['hub_unseal_at'])

        r_unload = raw()
        r_unload['trip_station'][1].update(ata=MID+1000, unload_quantity=5, expect_unload_quantity=10)
        trip2 = build_trip(r_unload, '4232', DAY)
        self.assertEqual(trip2['phase'], 'unloading')

        r_status60 = raw()
        r_status60['trip_station'][1].update(ata=MID+1000, trip_station_status=60)
        trip3 = build_trip(r_status60, '4232', DAY)
        self.assertEqual(trip3['phase'], 'unloading')

        r_arrived = raw()
        r_arrived['trip_station'][1].update(ata=MID+1000)
        trip4 = build_trip(r_arrived, '4232', DAY)
        self.assertEqual(trip4['phase'], 'at_hub')

    def test_arrived_location_verification(self):
        from sdd_data.linehaul import check_arrived_location
        hub_lat, hub_lng = 10.819469, 106.672067
        ata = MID + 2000
        near_points = {'location_list': [{'lat': 10.819, 'lng': 106.675, 'upload_time': ata - 60}]}
        res_near = check_arrived_location(near_points, ata, hub_lat, hub_lng)
        self.assertTrue(res_near['arrived_at_hub'])
        self.assertLess(res_near['arrived_distance_km'], 1.0)

        far_points = {'location_list': [{'lat': 10.95, 'lng': 106.67, 'upload_time': ata - 30}]}
        res_far = check_arrived_location(far_points, ata, hub_lat, hub_lng)
        self.assertFalse(res_far['arrived_at_hub'])
        self.assertGreater(res_far['arrived_distance_km'], 10.0)

    def test_dashboard_without_active_becomes_unloaded(self):
        settings = SimpleNamespace(station_id='4232')
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.linehaul.LinehaulClient') as cls:
            client = cls.return_value; client.trips.return_value = []
            client.dashboard_trips.return_value = [dashboard(trip_number='LTGONE01')]
            rows, _ = prepare_linehaul(settings, Path(folder))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['phase'], 'unloaded')
            self.assertEqual(rows[0]['hub_unloaded_at'], rows[0]['hub_ata'])
