from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdd_data.clients import load_coverage
from sdd_data.config import Settings

VALUES = [['Zone ID', 'Zone', 'Phường mới', 'Phường cũ', 'Quận & phường cũ'],
          ['route-a', 'KV1', 'Phường A mới', 'Phường A', 'Quận 10 - Phường A']]


class CoverageCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'sa').write_text('{}')
        self.settings = Settings(self.root, self.root, self.root / 'cookie', self.root / 'sa',
                                 'https://example.supabase.co', 'test-only')
        self.cache = self.root / 'coverage-sa-4232.json'
        credentials = patch('google.oauth2.service_account.Credentials.from_service_account_file')
        session = patch('google.auth.transport.requests.AuthorizedSession')
        credentials.start()
        self.addCleanup(credentials.stop)
        self.session = session.start().return_value.__enter__.return_value
        self.addCleanup(session.stop)
        self.session.get.return_value = Mock(status_code=200, json=Mock(return_value={'values': VALUES}))

    def load(self, settings=None):
        return load_coverage(settings or self.settings, cache_dir=self.root)

    def test_warm_cache_reuses_validated_coverage_without_google(self):
        first = self.load()
        self.assertEqual(self.load(), first)
        self.session.get.assert_called_once()
        self.assertNotIn('test-only', self.cache.read_text())

    def test_expired_cache_and_changed_sheet_are_refreshed(self):
        self.load()
        cached = json.loads(self.cache.read_text())
        cached['saved_at'] -= 3601
        self.cache.write_text(json.dumps(cached))
        self.load()
        self.load(replace(self.settings, sheet_name='Other Coverage'))
        self.assertEqual(self.session.get.call_count, 3)

    def test_corrupt_cache_is_rebuilt_and_expired_cache_is_not_used_on_error(self):
        self.cache.write_text('{broken')
        self.load()
        cached = json.loads(self.cache.read_text())
        cached['saved_at'] -= 3601
        self.cache.write_text(json.dumps(cached))
        self.session.get.return_value.status_code = 403
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            self.load()

    def test_alias_changes_are_applied_even_with_warm_cache(self):
        alias = self.root / 'alias.csv'
        settings = replace(self.settings, coverage_alias_file=alias)
        self.load(settings)
        alias.write_text('Zone ID,Zone\nnew-route,KV2\n', encoding='utf-8')
        self.assertIn('new-route', self.load(settings))
        self.session.get.assert_called_once()

    def test_explicit_csv_bypasses_google_and_cache(self):
        csv = self.root / 'coverage.csv'
        csv.write_text('Zone ID,Zone\ncsv-route,KV3\n', encoding='utf-8')
        self.assertIn('csv-route', load_coverage(self.settings, csv, cache_dir=self.root))
        self.session.get.assert_not_called()
        self.assertFalse(self.cache.exists())
