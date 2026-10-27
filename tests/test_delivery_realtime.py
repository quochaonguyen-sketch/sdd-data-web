import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from sdd_data.delivery_realtime import build_rows, DeliveryRealtimeClient, prepare_delivery_realtime
from sdd_data.transform import TZ


def rider(**extra):
    return {'driver_id': '123', 'driver_name': 'Rider',
            'lm_hub_fleet_assigned_order_qty_td': 10,
            'lm_hub_fleet_delivered_order_qty_eod': 4,
            'lm_hub_fleet_delivering_in_system_order_qty_eod': 5,
            'lm_hub_fleet_failed_delivery_order_qty_eod': 1, **extra}


class DeliveryRealtimeTests(unittest.TestCase):
    def test_maps_real_spx_counters_without_raw_payload(self):
        row, = build_rows([rider(lm_hub_fleet_first_delivering_time_eod=1793062800,
                                lm_hub_fleet_idle_delivery_second=1800)])
        self.assertEqual([row[k] for k in ('total_assigned','delivered','delivering','failed')], [10,4,5,1])
        self.assertEqual(row['first_delivery_at'], '08:00:00')
        self.assertEqual(row['idle_delivery_seconds'], 1800)
        self.assertNotIn('raw_data', row)

    def test_rejects_missing_negative_duplicate_and_empty_data(self):
        for raw in ([], [{}], [rider(),rider()], [rider(lm_hub_fleet_assigned_order_qty_td=-1)],
                    [rider(lm_hub_fleet_delivered_order_qty_eod=None)],
                    [rider(lm_hub_fleet_assigned_order_qty_td=0)]):
            with self.subTest(raw=raw), self.assertRaises((ValueError, KeyError, TypeError)):
                build_rows(raw)

    def test_zero_assigned_riders_are_excluded(self):
        self.assertEqual(len(build_rows([rider(),rider(driver_id='other',lm_hub_fleet_assigned_order_qty_td=0)])),1)

    def test_rejects_partial_response_and_auth_error(self):
        client = DeliveryRealtimeClient.__new__(DeliveryRealtimeClient)
        session = Mock()
        client.session = Mock(return_value=session)
        for body in ({'data':{'list':[rider()], 'total':2}}, {'retcode':401}, {'data':{}}, {'is_login':False}):
            session.post.return_value = Mock(status_code=200, json=Mock(return_value=body))
            with self.assertRaises(RuntimeError): client.fetch_progress()
        session.post.return_value = Mock(status_code=200, json=Mock(return_value={'retcode':0,'data':{'list':[rider()]}}))
        self.assertEqual(len(client.fetch_progress()),1)

    def test_fetch_failure_always_closes_sessions(self):
        with patch('sdd_data.delivery_realtime.DeliveryRealtimeClient') as cls:
            cls.return_value.fetch_progress.side_effect = RuntimeError('unavailable')
            with self.assertRaises(RuntimeError): prepare_delivery_realtime(Mock())
            cls.return_value.close.assert_called_once()

    def test_source_timestamp_is_used_and_stale_source_is_rejected(self):
        now = int(datetime.now(TZ).timestamp())
        with patch('sdd_data.delivery_realtime.DeliveryRealtimeClient') as cls:
            cls.return_value.fetch_progress.return_value = [rider(update_at_timestamp=now)]
            rows, stamp = prepare_delivery_realtime(Mock())
            self.assertEqual(int(stamp.timestamp()), now)
            cls.return_value.fetch_progress.return_value = [rider(update_at_timestamp=now-3600)]
            with self.assertRaisesRegex(RuntimeError, 'quá cũ'): prepare_delivery_realtime(Mock())

    def test_central_adapter_publishes_only_after_boundary(self):
        from sdd_data.source_job import execute
        boundary = Mock(publishing=False, job='job', worker='worker')
        events = []
        def begin():
            events.append('boundary')
            boundary.publishing = True
        boundary.begin.side_effect = begin
        boundary.control.rpc.side_effect = lambda *args, **kwargs: events.append('write')
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.delivery_realtime.prepare_delivery_realtime', return_value=([rider()],datetime.now(TZ))):
            execute(Mock(), 'delivery_realtime', datetime.now(TZ).date().isoformat(), boundary, root=Path(folder))
        self.assertEqual(events, ['boundary','write'])
