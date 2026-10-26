from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdd_data.auth import CookieManager, SpxSession, auth_failure, _crypt
from sdd_data.config import Settings


def config(root):
    return Settings(root, root/'legacy', root/'config/cookies/spx.json', root/'sa.json',
        'https://example.supabase.co', 'test')


class AuthTests(unittest.TestCase):
    def test_cookie_default_is_owned_by_this_project(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root=Path(temp)
            self.assertEqual(Settings.load(root).cookie_file, (root/'config/cookies/spx.json').resolve())

    def test_auth_errors_include_html_but_not_transient_failures(self):
        for status in (401,403,302):
            self.assertTrue(auth_failure(Mock(status_code=status)))
        response=Mock(status_code=200, headers={'Content-Type':'text/html'})
        response.json.side_effect=ValueError()
        self.assertTrue(auth_failure(response))
        self.assertFalse(auth_failure(Mock(status_code=500)))
        self.assertTrue(auth_failure(Mock(status_code=200,json=lambda:{'retcode':90309999})))

    def test_api_refreshes_once_and_reloads_csrf(self):
        manager=Mock()
        manager.cookies.return_value={'fms_user_skey':'old-test','csrftoken':'old-csrf'}
        manager.refresh.return_value={'fms_user_skey':'new-test','csrftoken':'new-csrf'}
        manager.fingerprint.return_value='old-generation'
        expired=Mock(status_code=401)
        success=Mock(status_code=200,json=lambda:{'retcode':0,'data':{}})
        with SpxSession(manager) as session, patch('requests.Session.request',side_effect=[expired,success]) as request:
            self.assertIs(session.get('https://spx.shopee.vn/api/test'), success)
            self.assertEqual(request.call_count,2)
            manager.refresh.assert_called_once_with(rejected='old-generation')
            self.assertEqual(session.headers['x-csrftoken'],'new-csrf')

    def test_repeated_rejection_does_not_loop(self):
        manager=Mock()
        manager.cookies.return_value=manager.refresh.return_value={'fms_user_skey':'test'}
        with SpxSession(manager) as session, patch('requests.Session.request',return_value=Mock(status_code=403)) as request:
            self.assertEqual(session.get('https://spx.shopee.vn/api/test').status_code,403)
            self.assertEqual(request.call_count,2)
            self.assertEqual(manager.refresh.call_count,1)

    def test_does_not_send_spx_session_to_another_host(self):
        with SpxSession(Mock()) as session, patch('requests.Session.request') as request:
            with self.assertRaises(ValueError):session.get('https://example.com')
            request.assert_not_called()

    def test_concurrent_expired_requests_only_refresh_one_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=CookieManager(config(Path(temp)))
            manager.path.parent.mkdir(parents=True)
            manager.path.write_text('[]')
            rejected=manager.fingerprint()
            def login(**kwargs):
                return [{'name':'fms_user_skey','value':'test-only','domain':'spx.shopee.vn','expiry':9999999999}]
            with patch.object(manager,'browser_login',side_effect=login) as browser, patch.object(manager,'verify'):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    result=list(pool.map(lambda _:manager.refresh(rejected=rejected),range(4)))
            self.assertEqual(browser.call_count,1)
            self.assertTrue(all(x['fms_user_skey']=='test-only' for x in result))

    def test_failed_validation_keeps_old_cookie_and_hides_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=CookieManager(config(Path(temp)))
            manager.path.parent.mkdir(parents=True)
            manager.path.write_text('[]')
            with patch.object(manager,'browser_login',return_value=[]), patch.object(manager,'verify',side_effect=ValueError('SECRET')):
                with self.assertRaises(RuntimeError) as error:manager.refresh()
                self.assertNotIn('SECRET',str(error.exception))
            self.assertEqual(manager.path.read_text(),'[]')

    def test_migration_never_overwrites_local_cookie(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);manager=CookieManager(config(root))
            source=manager.settings.launcher_dir/'config/cookies/cookies_assign_pick.json'
            source.parent.mkdir(parents=True);source.write_text('[]')
            manager.migrate_once()
            manager.path.write_text('["local"]')
            manager.migrate_once()
            self.assertEqual(manager.path.read_text(),'["local"]')

    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_windows_credential_roundtrip_is_encrypted(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=CookieManager(config(Path(temp)))
            manager.save_credentials('test-user','dummy-test-password')
            self.assertNotIn(b'dummy-test-password',manager.credentials_path.read_bytes())
            self.assertEqual(manager.credentials(),('test-user','dummy-test-password'))
