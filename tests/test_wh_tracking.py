import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from sdd_data.transform import TZ
from sdd_data.wh_tracking import WhClient, build_rows, pending_receive_timestamp, prepare_wh
from sdd_data.source_job import execute


def order(sid='TEST001', status=1, **kwargs):
    return {'shipment_id': sid, 'order_status': status, 'create_time': 1793930400,
            'buyer_addr_district': 'Quận 1', 'lowest_buyer_address_name': 'Phường Bến Nghé',
            'on_hold_times': 0, **kwargs}


class WhTests(unittest.TestCase):
    def client(self, pages):
        client = object.__new__(WhClient)
        client.settings = Mock(station_id='4232')
        client.post = Mock(side_effect=pages)
        client.fetch_window = Mock(return_value=[])
        return client

    def test_exact_dual_payload_and_no_volume_account_filter(self):
        client = self.client([])
        self.assertEqual(client.payload(2), {'count': 24, 'high_value': 1, 'page_no': 1,
            'pickup_station_ids': '5521', 'station_ids': '4232', 'order_status': '0,7'})
        self.assertEqual(client.payload(1)['current_station_ids'], '4232')
        self.assertNotIn('station_ids', client.payload(1))
        self.assertNotIn('order_account', client.payload(1))

    def test_pages_can_be_short_but_must_match_total_and_ids(self):
        pages = [{'list': [order()], 'total': 2}, {'list': [order('TEST002')], 'total': 2}]
        self.assertEqual(len(self.client(pages).fetch_api(1)), 2)
        broken = [{'list': [order()], 'total': 2}, {'list': [order()], 'total': 2}]
        with self.assertRaisesRegex(RuntimeError, 'không xác minh'):
            self.client(broken * 2).fetch_api(1)
        self.assertEqual(self.client([{'list': [], 'total': 0}]).fetch_api(2), [])

    def test_changed_total_retries_whole_snapshot(self):
        client = self.client([{'list': [order()], 'total': 2}, {'list': [], 'total': 1},
                              {'list': [order()], 'total': 1}])
        self.assertEqual(len(client.fetch_api(1)), 1)
        self.assertEqual([c.args[0]['page_no'] for c in client.post.call_args_list], [1, 2, 1])

    def test_tracking_uses_latest_nested_status_even_with_empty_message(self):
        ts = 1793930400
        self.assertEqual(pending_receive_timestamp([{'status': 7, 'timestamp': ts,
            'children': [{'status': '7', 'timestamp': ts + 60, 'message': ''}]}]),
            datetime.fromtimestamp(ts + 60, TZ).isoformat())
        self.assertIsNone(pending_receive_timestamp([{'status': 7, 'timestamp': 'bad'}]))

    def test_adaptive_windows_avoid_offset_pagination(self):
        a, b = order(create_time=1), order('TEST002', create_time=2)
        client = self.client([{'total': 2, 'list': [a]}, {'total': 1, 'list': [a]}, {'total': 1, 'list': [b]}])
        del client.fetch_window
        self.assertEqual(client.fetch_window(1, 1, 2), [a, b])
        client = self.client([{'total': 1, 'list': [order(create_time=3)]}])
        with self.assertRaisesRegex(RuntimeError, 'không tuân thủ'):
            WhClient.fetch_window(client, 1, 1, 2)

    def test_current_day_filter_and_current_web_geography(self):
        day = date(2026, 11, 6)
        today = int(datetime(2026, 11, 6, tzinfo=TZ).timestamp())
        rows = build_rows([order(), order('DONE', 4, delivered_time=today),
                           order('OLD', 4, delivered_time=today-1),
                           order('UNKNOWN', 7, buyer_addr_district='', lowest_buyer_address_name='')], {}, day)
        self.assertEqual([r['shipment_id'] for r in rows], ['TEST001', 'DONE', 'UNKNOWN'])
        self.assertEqual(rows[0]['area'], 'KV4')
        self.assertEqual(rows[0]['attempt'], 0)
        self.assertEqual(rows[2]['area'], 'UNRESOLVED')
        for bad in ([order(), order()], [order(status=4)], [order(on_hold_times='bad')]):
            with self.assertRaises(ValueError):
                build_rows(bad, {}, day)

    def test_dual_merge_and_failed_enrichment_never_publishes(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            client.fetch_api.side_effect = [[order()], [order(status=7)]]
            client.tracking.return_value = None
            settings = Mock(workers=4)
            with patch('sdd_data.wh_tracking.load_coverage', return_value={}), patch('sdd_data.wh_tracking.WhClient', return_value=client):
                rows, _ = prepare_wh(settings, Path(folder))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['raw_status'], 'LMhub_received')
            client.tracking.assert_called_once_with('TEST001')
            boundary = Mock(publishing=False, job='job', worker='worker')
            with patch('sdd_data.wh_tracking.prepare_wh', side_effect=RuntimeError('incomplete')):
                with self.assertRaises(RuntimeError):
                    execute(settings, 'wh_tracking', '2026-11-06', boundary, root=Path(folder))
            boundary.begin.assert_not_called()
            boundary.control.rpc.assert_not_called()
            boundary.begin.side_effect = lambda: setattr(boundary, 'publishing', True)
            with patch('sdd_data.wh_tracking.prepare_wh', return_value=([], datetime.now(TZ))):
                execute(settings, 'wh_tracking', '2026-11-06', boundary, root=Path(folder))
            self.assertEqual(boundary.control.rpc.call_args.args[0], 'publish_wh_tracking')


if __name__ == '__main__':
    unittest.main()
