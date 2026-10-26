from concurrent.futures import Future
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdd_data.config import Settings
from sdd_data.session_control import SessionControl, cookie_expiry


class SessionControlTests(unittest.TestCase):
    def test_expiry_tracks_auth_cookie_and_earlier_csrf_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'cookie.json'
            self.assertEqual(cookie_expiry(path), (None, 'missing'))
            path.write_text('broken')
            self.assertEqual(cookie_expiry(path), (None, 'unknown'))
            path.write_text(json.dumps([dict(name='fms_user_skey', value='test')]))
            self.assertEqual(cookie_expiry(path), (None, 'session'))
            rows = [dict(name='fms_user_skey', value='test', expiry=2000000000),
                    dict(name='google_auth_redirect', value='test', expiry=1),
                    dict(name='csrftoken', value='test', expires=1900000000),
                    dict(name='fms_user_skey', value='test', domain='evil.example', expiry=1)]
            path.write_text(json.dumps({'cookies':rows}))
            self.assertEqual(cookie_expiry(path), ('2030-03-17T17:46:40+00:00', 'fixed'))
            rows[0]['expiry'] = 1
            path.write_text(json.dumps(rows))
            self.assertEqual(cookie_expiry(path), ('1970-01-01T00:00:01+00:00', 'fixed'))

    def make_control(self, root):
        return SessionControl(Settings(root, root/'legacy', root/'config/cookies/spx.json',
                                       root/'sa.json', 'https://example.supabase.co', 'test'))

    def test_metadata_never_reads_cookie_or_password_values(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            try:
                control.manager.path.parent.mkdir(parents=True)
                control.manager.path.write_text('SECRET-COOKIE')
                control.manager.credentials_path.parent.mkdir(parents=True)
                control.manager.credentials_path.write_text('SECRET-PASSWORD')
                metadata = control.metadata()
                self.assertTrue(metadata['p_cookie'])
                self.assertTrue(metadata['p_account'])
                self.assertNotIn('SECRET', str(metadata))
                self.assertEqual(set(metadata), {'p_cookie', 'p_account', 'p_profile', 'p_updated', 'p_expires', 'p_expiry_kind'})
            finally:
                control.close()

    def test_refresh_uses_existing_lock_and_suppresses_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            try:
                with patch.object(control.manager, 'refresh', side_effect=RuntimeError('SECRET')) as refresh:
                    self.assertFalse(control.refresh())
                    refresh.assert_called_once_with(rejected='missing')
            finally:
                control.close()

    def test_tick_runs_in_background_and_retains_result_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            try:
                rpc = Mock()
                control.advertised = True
                rpc.rpc.return_value = 'test-request'
                future = Future()
                with patch.object(control.pool, 'submit', return_value=future) as submit:
                    control.tick(rpc, 'worker')
                    control.tick(rpc, 'worker')
                    submit.assert_called_once()
                future.set_result(True)
                rpc.rpc.side_effect = RuntimeError('offline')
                with self.assertRaises(RuntimeError):
                    control.tick(rpc, 'worker')
                self.assertIs(control.future, future)
                rpc.rpc.side_effect = [None, None]
                control.tick(rpc, 'worker')
                self.assertIsNone(control.future)
                rpc.rpc.assert_any_call('finish_spx_session_refresh', p_worker='worker',
                                        p_request='test-request', p_success=True)
            finally:
                control.close()

    def test_web_credentials_saved_under_lock_before_forced_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            control = self.make_control(Path(temp))
            control.transport = Mock()
            control.transport.decrypt.return_value = ('test-user', 'dummy-password')
            try:
                with patch.object(control.manager, 'save_credentials') as save, patch.object(control.manager, 'refresh') as refresh:
                    self.assertTrue(control.refresh({'encrypted': True}, 'worker'))
                    save.assert_called_once_with('test-user', 'dummy-password')
                    refresh.assert_called_once_with(rejected=None)
                control.transport.decrypt.side_effect = ValueError('tampered')
                with patch.object(control.manager, 'save_credentials') as save:
                    self.assertFalse(control.refresh({'encrypted': True}, 'worker'))
                    save.assert_not_called()
            finally:
                control.close()
