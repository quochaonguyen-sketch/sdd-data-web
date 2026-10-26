import unittest
from unittest.mock import Mock

from sdd_data.source_control import Control


class ControlTests(unittest.TestCase):
    def test_successful_void_rpcs_accept_empty_200_and_204_responses(self):
        for status, content in [(200, b''), (204, b''), (200, b'  \r\n')]:
            with self.subTest(status=status, content=content):
                control = Control(Mock(supabase_url='https://example.supabase.co', supabase_key='test'))
                response = Mock(ok=True, status_code=status, content=content)
                control.session.post = Mock(return_value=response)
                try:
                    self.assertIsNone(control.rpc('finish_spx_session_refresh', p_worker='test', p_request='test', p_success=True))
                    response.json.assert_not_called()
                finally:
                    control.close()

    def test_json_rpc_results_are_preserved(self):
        control = Control(Mock(supabase_url='https://example.supabase.co', supabase_key='test'))
        response = Mock(ok=True, status_code=200, content=b'"request-id"')
        response.json.return_value = 'request-id'
        control.session.post = Mock(return_value=response)
        try:
            self.assertEqual(control.rpc('pulse_spx_session'), 'request-id')
        finally:
            control.close()
