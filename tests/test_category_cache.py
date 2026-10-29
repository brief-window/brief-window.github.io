import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import collector

NOW = datetime(2026, 10, 29, 19, tzinfo=timezone.utc)
DATA = {"categories": [
    {"id": 12, "name": "玩卡", "description": "DO NOT CACHE"},
    {"id": 15, "name": "旅行"}, {"id": 9, "name": "理财"},
    {"id": 13, "name": "信用卡", "parent_category_id": 12},
], "posts": ["DO NOT CACHE"], "users": ["DO NOT CACHE"]}


class CategoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.json"
        self.client = Mock()
        self.client.get_json.return_value = copy.deepcopy(DATA)

    def test_success_caches_only_classification_and_skips_next_site_request(self):
        expected = collector.category_metadata(self.client, self.path, NOW)
        self.assertNotIn("DO NOT CACHE", self.path.read_text())
        self.client.get_json.reset_mock()
        self.client.get_json.side_effect = AssertionError("unnecessary request")
        self.assertEqual(collector.category_metadata(self.client, self.path, NOW + timedelta(hours=23)), expected)
        self.client.get_json.assert_not_called()

    def test_cache_expires_after_24_hours(self):
        collector.category_metadata(self.client, self.path, NOW)
        self.client.get_json.reset_mock()
        collector.category_metadata(self.client, self.path, NOW + timedelta(hours=24))
        self.client.get_json.assert_called_once_with("/site.json")

    def test_expired_cache_does_not_hide_403_or_get_overwritten(self):
        collector.category_metadata(self.client, self.path, NOW)
        original = self.path.read_bytes()
        self.client.get_json.side_effect = collector.CollectionError("HTTP 403")
        with self.assertRaises(collector.CollectionError):
            collector.category_metadata(self.client, self.path, NOW + timedelta(hours=25))
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_future_wrong_source_and_wrong_categories_refetch(self):
        collector.category_metadata(self.client, self.path, NOW)
        valid = json.loads(self.path.read_text())
        cases = ["not json", json.dumps({**valid, "fetched_at": 5}),
                 json.dumps({**valid, "fetched_at": (NOW + timedelta(hours=1)).isoformat()}),
                 json.dumps({**valid, "source": "https://example.org"}),
                 json.dumps({**valid, "data": []}),
                 json.dumps({**valid, "data": {"categories": []}})]
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content)
                self.client.get_json.reset_mock()
                collector.category_metadata(self.client, self.path, NOW)
                self.client.get_json.assert_called_once_with("/site.json")

    def test_invalid_live_metadata_is_never_cached(self):
        self.client.get_json.return_value = {"categories": []}
        with self.assertRaises(collector.CollectionError):
            collector.category_metadata(self.client, self.path, NOW)
        self.assertFalse(self.path.exists())
