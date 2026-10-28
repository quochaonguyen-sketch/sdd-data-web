import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sdd_data.source_control import Control


class ControlTests(unittest.TestCase):
    def test_large_pickup_uses_compact_utf8_and_http_budget_above_database(self):
        settings = SimpleNamespace(supabase_url='https://example.invalid', supabase_key='test')
        with patch('sdd_data.source_control.requests.Session') as cls:
            session = cls.return_value
            session.post.return_value = Mock(ok=True, content=b'1', json=Mock(return_value=1))
            control = Control(settings)
            payload = {'p_rows': [{'ward': 'Phường Tân Định', 'status': 'Assigned'}]}
            self.assertEqual(control.rpc('publish_central_pickup', **payload), 1)
            kwargs = session.post.call_args.kwargs
            self.assertEqual(kwargs['timeout'], 150)
            self.assertEqual(json.loads(kwargs['data']), payload)
            self.assertNotIn(b'\\u', kwargs['data'])
            self.assertLess(len(kwargs['data']), len(json.dumps(payload).encode()))
            control.rpc('pulse_data_source_worker', p_worker='test')
            self.assertEqual(session.post.call_args.kwargs['timeout'], 20)
            with self.assertRaises(ValueError):
                control.rpc('publish_central_pickup', p_at=float('nan'))
            control.close()

    def test_rpc_allowlist_and_safe_remote_errors_are_preserved(self):
        settings = SimpleNamespace(supabase_url='https://example.invalid', supabase_key='test')
        with patch('sdd_data.source_control.requests.Session') as cls:
            control = Control(settings)
            with self.assertRaises(ValueError):
                control.rpc('arbitrary_function')
            cls.return_value.post.assert_not_called()
            cls.return_value.post.return_value = Mock(ok=False, status_code=500,
                json=Mock(return_value={'code': '57014', 'message': 'private remote details'}))
            with self.assertRaisesRegex(RuntimeError, 'HTTP 500, mã 57014') as error:
                control.rpc('publish_central_pickup')
            self.assertNotIn('private', str(error.exception))
