import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sdd_data.overnight_status import current_status, lookup_statuses, sync_once


def order(code='A'):
    return {'order_code': code, 'created_at': '2026-10-27T16:04:00+00:00'}


class OvernightStatusTests(unittest.TestCase):
    def test_spx_status_codes_and_return_receipt_evidence(self):
        for raw, expected in [(2, 'Delivering'), (4, 'Delivered'), (5, 'Onhold'), (10, 'Returned'),
                              (67, 'Returned'), (72, 'Returned'), (999, 'SPX (999)')]:
            self.assertEqual(current_status({'order_status': raw}, order(), '4232')['current_status'], expected)
        from datetime import datetime
        cutoff = int(datetime.fromisoformat(order()['created_at']).timestamp())
        for station, received, expected in [(4232, cutoff-1, 'LMHub_Received'), (4232, cutoff+1, 'Returned'),
                                             (9999, cutoff+1, 'LMHub_Received'), (4232, 0, 'LMHub_Received')]:
            row = {'order_status': 1, 'current_station_id': station, 'current_station_received_time': received}
            self.assertEqual(current_status(row, order(), '4232')['current_status'], expected)

    def test_exact_batches_use_search_id_list_and_match_out_of_order(self):
        client = Mock(settings=SimpleNamespace(station_id='4232'))
        client.post.side_effect = lambda p: {'total': len(p['search_id_list']),
            'list': [{'shipment_id': c, 'order_status': 4} for c in reversed(p['search_id_list'])]}
        orders = [order(str(i)) for i in range(51)]
        self.assertEqual([r['order_code'] for r in lookup_statuses(client, orders)], [o['order_code'] for o in orders])
        self.assertEqual([len(c.args[0]['search_id_list']) for c in client.post.call_args_list], [24, 24, 3])
        self.assertTrue(all(c.args[0]['count'] == 24 and c.args[0]['page_no'] == 1 for c in client.post.call_args_list))

    def test_missing_duplicate_wrong_ids_and_missing_status_fail_closed(self):
        client = Mock(settings=SimpleNamespace(station_id='4232'))
        cases = [[], [{'shipment_id': 'WRONG', 'order_status': 4}], [{'shipment_id': 'A'}]]
        for rows in cases:
            client.post.return_value = {'total': len(rows), 'list': rows}
            with self.assertRaises(RuntimeError):
                lookup_statuses(client, [order()])
        client.post.return_value = {'total': 2, 'list': [{'shipment_id': 'A', 'order_status': 4}]*2}
        with self.assertRaises(RuntimeError):
            lookup_statuses(client, [order('A'), order('B')])

    def test_failure_records_error_without_publishing_partial_statuses(self):
        control = Mock()
        control.rpc.side_effect = [{'day': '2026-10-27', 'token': 'lease', 'orders': [order()]}, {'success': False}]
        client = Mock()
        client.post.side_effect = RuntimeError('private remote failure')
        with patch('sdd_data.overnight_status.Control', return_value=control), patch('sdd_data.overnight_status.SpxClient', return_value=client):
            with self.assertRaises(RuntimeError):
                sync_once(Mock())
        finish = control.rpc.call_args
        self.assertEqual(finish.kwargs['p_rows'], [])
        self.assertNotIn('private', finish.kwargs['p_error'])
        client.close.assert_called_once()
        control.close.assert_called_once()
