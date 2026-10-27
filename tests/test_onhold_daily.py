import unittest
import tempfile
from pathlib import Path
from datetime import datetime, date
from types import SimpleNamespace
from unittest.mock import patch, Mock
from sdd_data.onhold_daily import aggregate, OnholdClient, station_matches, prepare_onhold_observations
from sdd_data.transform import TZ

DAY = date(2026, 10, 25)
def epoch(hour, day=25):
    return int(datetime(2026, 10, day, hour, tzinfo=TZ).timestamp())
def order(sid='A', **extra):
    return {'shipment_id': sid, 'order_status': 5, 'delivering_time': epoch(8), 'on_hold_time': epoch(10),
            'current_station_id': 4232, 'zone_id': 'Z6.OLD', 'on_hold_reason': 169, **extra}

class OHTests(unittest.TestCase):
    def test_central_worker_captures_only_in_cutoff_window(self):
        from sdd_data.source_job import execute
        today=datetime.now(TZ).date()
        for hour,minute,due in [(22,59,False),(23,0,True),(23,15,False)]:
            boundary=Mock(publishing=False,job='job',worker='worker')
            boundary.begin.side_effect=lambda: setattr(boundary,'publishing',True)
            boundary.control.rpc.return_value=True
            stats={'input':0,'excluded':0,'scan_started_at':'stamp'}
            with tempfile.TemporaryDirectory() as folder,patch('sdd_data.source_job.datetime') as clock,patch('sdd_data.onhold_daily.prepare_onhold_observations',return_value=([],stats)):
                clock.now.return_value=datetime.combine(today,datetime.min.time(),TZ).replace(hour=hour,minute=minute)
                execute(Mock(), 'onhold_daily',today.isoformat(),boundary,root=Path(folder))
            calls=[c for c in boundary.control.rpc.call_args_list if c.args[0]=='publish_overnight_hold']
            self.assertEqual(len(calls),int(due))

    def test_overnight_scan_includes_delivering_without_prior_onhold(self):
        client=object.__new__(OnholdClient);client.settings=SimpleNamespace(workers=2)
        payloads=[]
        def post(payload):
            payloads.append(payload)
            return {'total':0,'list':[]}
        client.post=post
        client.overnight=True
        client.fetch(DAY)
        self.assertTrue(payloads)
        self.assertTrue(all('on_hold_times' not in p for p in payloads))
        payloads.clear();client.overnight=False;client.fetch(DAY)
        self.assertTrue(all(p['on_hold_times']=='1,10' for p in payloads))

    def test_overnight_requires_today_delivering_and_current_hub_rider(self):
        today=datetime.now(TZ).date()
        stamp=int(datetime.combine(today,datetime.min.time(),TZ).timestamp())+3600
        valid=order('NIGHT',delivering_time=stamp,driver_id=123,order_status=1)
        rows=[valid,{**valid,'shipment_id':'OLD','delivering_time':stamp-86400},
              {**valid,'shipment_id':'NO-RIDER','driver_id':0},
              {**valid,'shipment_id':'OTHER-HUB','current_station_id':1}]
        with patch('sdd_data.onhold_daily.load_coverage',return_value={}),patch('sdd_data.onhold_daily.OnholdClient') as client:
            client.return_value.fetch.return_value=rows
            events,_=prepare_onhold_observations(SimpleNamespace(station_id='4232'),today,overnight=True)
        self.assertEqual([e['order_code'] for e in events],['NIGHT'])
        self.assertEqual(datetime.fromisoformat(events[0]['delivering_at']).date(),today)

    def test_current_area_mapping_dedup_and_pivot_privacy(self):
        mapping={'z6.old':{'district':'Quận 12','old_ward':'Phường Thạnh Lộc','area':'KV5'}}
        pivot,stats=aggregate([order(),order(),order('B',order_status=1)],mapping,DAY,'4232','Home')
        self.assertEqual(sum(c['orders'] for c in pivot),2)
        self.assertEqual({c['area'] for c in pivot},{'Khu vực 5'})
        self.assertEqual(set(pivot[0]),{'area','reason','hour','status','orders'})
        self.assertNotIn('shipment_id',str(pivot))
        self.assertEqual(stats['included'],2)

    def test_scope_status_date_station_and_unknown_are_explicit(self):
        rows=[order('A'),order('B',order_status=2),order('C',delivering_time=epoch(8,24)),
              order('D',current_station_id=1),order('E',on_hold_time=epoch(10,24))]
        pivot,stats=aggregate(rows,{},DAY,4232,'Home')
        self.assertEqual(stats['included'],2)
        self.assertEqual(stats['excluded'],3)
        self.assertEqual(stats['unmapped'],2)
        self.assertEqual({c['hour'] for c in pivot},{10,-1})

    def test_legacy_cross_station_rule_compares_report_day(self):
        row=order(current_station_id=1,current_station_received_time=epoch(11))
        self.assertTrue(station_matches(row,4232,'Home',DAY))
        self.assertFalse(station_matches(row,4232,'Home',date(2026,10,24)))

    def test_fetch_rejects_duplicates_and_incomplete_pages(self):
        client=object.__new__(OnholdClient);client.settings=SimpleNamespace(workers=2)
        client.post=lambda payload:{'total':2,'list':[order(),order()]}
        with self.assertRaisesRegex(RuntimeError,'pivot'):
            client.fetch(DAY)

    def test_empty_source_is_complete(self):
        client=object.__new__(OnholdClient);client.settings=SimpleNamespace(workers=2)
        client.post=lambda payload:{'total':0,'list':[]}
        self.assertEqual(client.fetch(DAY),[])

    def test_recursive_time_buckets_are_disjoint_and_complete(self):
        client=object.__new__(OnholdClient);client.settings=SimpleNamespace(workers=2)
        source=[order('A',delivering_time=epoch(8)+1),order('B',delivering_time=epoch(9)+1),order('C',delivering_time=epoch(10)+1)]
        def post(payload):
            left,right=map(int,payload['delivering_time'].split(','))
            found=[r for r in source if left<=r['delivering_time']<=right]
            self.assertEqual(payload['page_no'],1)
            return {'total':len(found),'list':found[:1]}
        client.post=post
        self.assertEqual({r['shipment_id'] for r in client.fetch(DAY)},{'A','B','C'})

    def test_arbitrary_reason_strings_never_persist(self):
        pivot,_=aggregate([order(on_hold_reason='a private note with order ID')],{},DAY,4232,'Home')
        self.assertEqual(pivot[0]['reason'],'unknown')

    def test_publisher_payload_has_stable_day_digest_dimensions_and_english_reason(self):
        today=datetime.now(TZ).date()
        stamp=int(datetime.combine(today,datetime.min.time(),TZ).timestamp())+3600
        source=order('PRIVATE-ORDER-ABC',delivering_time=stamp,on_hold_time=stamp,driver_id=123,driver_name='Test Rider')
        settings=SimpleNamespace(station_id='4232')
        with patch('sdd_data.onhold_daily.load_coverage',return_value={}),patch('sdd_data.onhold_daily.OnholdClient') as client:
            client.return_value.fetch.return_value=[source]
            events,stats=prepare_onhold_observations(settings,today)
            again,_=prepare_onhold_observations(settings,today)
        self.assertEqual(events[0]['order_key'],again[0]['order_key'])
        self.assertEqual(len(events[0]['order_key']),64)
        self.assertEqual(events[0]['order_code'],'PRIVATE-ORDER-ABC')
        self.assertNotIn('shipment_id',str(events))
        self.assertEqual(events[0]['reason_label'],'Recipient unavailable for parcel')
        self.assertEqual(events[0]['rider_code'],'123')
        self.assertEqual(stats['input'],1)

if __name__=='__main__':unittest.main()
