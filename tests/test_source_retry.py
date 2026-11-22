import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from sdd_data.clients import AuthenticationError
from sdd_data.retry import ControlError, ROLLED_BACK_EXIT, retry_reason, wait_before_retry
from sdd_data.source_worker import Child, acknowledge


class SourceRetryTests(unittest.TestCase):
    def test_wrapped_timeout_can_retry_but_auth_failure_stops(self):
        try:
            try:
                raise requests.ReadTimeout()
            except requests.ReadTimeout:
                raise RuntimeError('Safe source error') from None
        except RuntimeError as wrapped:
            self.assertEqual(retry_reason(wrapped), 'timeout')
        self.assertIsNone(retry_reason(AuthenticationError('Sign in again')))
        self.assertEqual(retry_reason(RuntimeError('SPX trả HTTP 429')), 'upstream')

    def test_only_confirmed_rollback_can_repeat_a_publication(self):
        self.assertIsNone(retry_reason(requests.ReadTimeout(), publishing=True))
        self.assertIsNone(retry_reason(ControlError('publish_overnight_hold', 500, '57014'), publishing=True))
        self.assertEqual(retry_reason(ControlError('publish_central_pickup', 500, '57014'), publishing=True),
                         'rolled_back')

    def test_honors_retry_after_and_releases_response_before_wait(self):
        response = Mock(headers={'Retry-After': '45'})
        with patch('sdd_data.retry.time.sleep') as sleep:
            wait_before_retry(0, response)
        response.close.assert_called_once()
        sleep.assert_called_once_with(45)

    def test_parent_acknowledgement_preserves_attempt_and_rollback(self):
        control = Mock()
        child = Child(Mock(), Mock(), Path('unused'), 0.0, attempt=3)
        acknowledge(control, 'job', 'worker', child, ROLLED_BACK_EXIT)
        control.rpc.assert_called_once_with('complete_data_source_attempt', p_job='job', p_worker='worker',
                                            p_attempt=3, p_outcome='RETRY', p_reason='rolled_back')
