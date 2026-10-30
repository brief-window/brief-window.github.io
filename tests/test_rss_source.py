import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import collector
import rss_source as rss

NOW = datetime(2026, 10, 30, 16, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(hours=3)
NAMES = {12: "玩卡", 15: "旅行", 9: "理财", 5: "信用卡", 99: "生活"}
ROOTS = {12: 12, 15: 15, 9: 9, 5: 12}


def item(pid, tid=123, when=NOW - timedelta(minutes=10), number=7221, body="<p>完整正文</p>"):
    return (f'<item><title>Topic {tid}</title><dc:creator>@reader Display Name</dc:creator>'
            f'<description><![CDATA[{body}]]></description>'
            f'<link>https://www.uscardforum.com/t/topic/{tid}?page=352#post_{number}</link>'
            f'<pubDate>{format_datetime(when)}</pubDate>'
            f'<guid>www.uscardforum.com-post-{pid}</guid></item>')


def feed(items):
    return rss.parse_xml(('<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<channel><title>Posts</title>' + ''.join(items) + '</channel></rss>').encode())


def topic(tid=123, category="信用卡"):
    return rss.parse_xml((f'<rss><channel><link>https://www.uscardforum.com/t/topic/{tid}</link>'
                          f'<category>{category}</category></channel></rss>').encode())


class FakeClient:
    def __init__(self, pages, topics=None):
        self.pages = iter(pages)
        self.topics = topics or {123: topic()}
        self.calls = []

    def get_xml(self, path, params=None):
        self.calls.append((path, params))
        return next(self.pages) if path == "/posts.rss" else self.topics[int(path.rsplit('/', 1)[-1][:-4])]


class RSSTests(unittest.TestCase):
    def collect(self, client, cache=None, max_pages=200):
        return rss.fetch_recent_posts_rss(client, CUTOFF, NOW, ROOTS, NAMES,
                                          collector.TARGET_CATEGORIES, collector.full_text,
                                          max_pages, cache)

    def test_pagination_full_body_large_floor_and_exact_category(self):
        body = '<p>完整内容 ' + 'long text ' * 3000 + '</p><a href="/x">link</a><img src="/a.png">'
        client = FakeClient([feed([item(100, body=body)]), feed([item(99, when=CUTOFF - timedelta(seconds=1))])])
        records, pages = self.collect(client)
        self.assertEqual(pages, 2)
        self.assertEqual(records[0]['id'], 100)
        self.assertEqual(records[0]['topic_id'], 123)
        self.assertEqual(records[0]['post_number'], 7221)
        self.assertEqual(records[0]['username'], 'reader')
        self.assertEqual(records[0]['category'], '玩卡')
        self.assertEqual(records[0]['subcategory'], '信用卡')
        self.assertEqual(records[0]['text'].count('long text'), 3000)
        self.assertIn('https://www.uscardforum.com/x', records[0]['text'])
        self.assertIn('https://www.uscardforum.com/a.png', records[0]['text'])
        self.assertIn(('/posts.rss', {'before': 100}), client.calls)

    def test_mixed_old_boundary_future_and_non_target_posts(self):
        client = FakeClient([
            feed([item(100, when=CUTOFF - timedelta(seconds=1)), item(99, when=CUTOFF),
                  item(98, when=NOW + timedelta(seconds=1)), item(97, tid=999)]),
            feed([item(96)]), feed([]),
        ], {123: topic(), 999: topic(999, '生活')})
        records, pages = self.collect(client)
        self.assertEqual([post['id'] for post in records], [99, 96])
        self.assertEqual(pages, 3)

    def test_ignored_cursor_duplicates_and_page_limit_fail(self):
        clients = [FakeClient([feed([item(100)]), feed([item(100)])]),
                   FakeClient([feed([item(100), item(100)])])]
        for client in clients:
            with self.assertRaises(rss.RSSCollectionError):
                self.collect(client)
        with self.assertRaises(rss.RSSCollectionError):
            self.collect(FakeClient([feed([item(100)])]), max_pages=1)

    def test_metadata_cache_reuse_contains_no_body(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'topics.json'
            self.collect(FakeClient([feed([item(100, body='<p>SECRET BODY</p>')]), feed([])]), cache)
            self.assertNotIn('SECRET BODY', cache.read_text())
            client = FakeClient([feed([item(101)]), feed([])], {})
            self.collect(client, cache)
            self.assertFalse(any(path.startswith('/t/') for path, _ in client.calls))

    def test_expired_or_unknown_cached_category_is_revalidated(self):
        for category, age in [('信用卡', 25), ('Unknown', 1)]:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / 'topics.json'
                cache.write_text(json.dumps({'schema_version': 1, 'source': rss.BASE_URL,
                    'topics': {'123': {'category_name': category, 'checked_at': (NOW-timedelta(hours=age)).isoformat()}}}))
                client = FakeClient([feed([item(100)]), feed([])])
                self.collect(client, cache)
                self.assertIn(('/t/topic/123.rss', None), client.calls)

    def test_wrong_topic_unknown_category_or_duplicate_category_names_fail_closed(self):
        for xml in [topic(999), topic(123, 'Unknown')]:
            with self.assertRaises(rss.RSSCollectionError):
                self.collect(FakeClient([feed([item(100)])], {123: xml}))
        with self.assertRaises(rss.RSSCollectionError):
            rss.TopicCategories(None, NOW, {1: 'Same', 2: 'Same'})

    def test_external_ambiguous_and_missing_floor_links_are_rejected(self):
        for url in ['https://evil.example/t/topic/123/1', 'http://www.uscardforum.com/t/topic/123/1',
                    'https://www.uscardforum.com/t/123/456',
                    'https://www.uscardforum.com/t/topic/123?page=2',
                    'https://www.uscardforum.com/t/topic/123/2#post_3']:
            with self.subTest(url=url), self.assertRaises(rss.RSSCollectionError):
                rss.topic_link(url)
        self.assertEqual(rss.topic_link('https://www.uscardforum.com/t/topic/123'), (123, 1))

    def test_active_xml_declarations_rejected_but_literal_examples_preserved(self):
        with self.assertRaises(rss.RSSCollectionError):
            rss.parse_xml(b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel>&x;</channel></rss>')
        parsed = rss.parse_posts(feed([item(100, body='<pre>&lt;!DOCTYPE html&gt;</pre>')]))
        self.assertIn('&lt;!DOCTYPE html&gt;', parsed[0]['cooked'])
        with self.assertRaises(rss.RSSCollectionError):
            rss.parse_xml(b'<html>challenge</html>')

    def test_missing_body_and_naive_dates_are_rejected(self):
        with self.assertRaises(rss.RSSCollectionError):
            rss.parse_posts(feed([item(100, body='')]))
        bad = item(100).replace(format_datetime(NOW-timedelta(minutes=10)), 'Fri, 30 Oct 2026 15:50:00')
        with self.assertRaises(rss.RSSCollectionError):
            rss.parse_posts(feed([bad]))

    @patch('rss_source.time.sleep')
    def test_403_does_not_retry_or_switch_endpoints(self, sleep):
        client = rss.RssForumClient()
        client.session.get = Mock(return_value=Mock(status_code=403, headers={}))
        with self.assertRaisesRegex(rss.RSSCollectionError, 'HTTP 403'):
            client.get_xml('/posts.rss')
        self.assertEqual(client.session.get.call_count, 1)
        self.assertNotIn('Mozilla', client.session.headers['User-Agent'])
        self.assertFalse(client.session.trust_env)

    @patch('rss_source.time.sleep')
    def test_transient_failure_is_bounded_and_long_retry_after_is_respected(self, sleep):
        for code, headers, expected in [(503, {}, 3), (429, {'Retry-After': '300'}, 1)]:
            with self.subTest(code=code):
                client = rss.RssForumClient()
                client.session.get = Mock(return_value=Mock(status_code=code, headers=headers))
                with self.assertRaises(rss.RSSCollectionError):
                    client.get_xml('/posts.rss')
                self.assertEqual(client.session.get.call_count, expected)

    def test_main_rss_failure_records_the_real_error_and_preserves_empty_error_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(rss.RssForumClient, 'get_json', side_effect=rss.RSSCollectionError('HTTP 403 for /site.json')):
                with patch('sys.argv', ['collector.py', '--source', 'rss', '--output', directory]):
                    self.assertEqual(collector.main(), 1)
            output = json.loads((Path(directory)/'status.json').read_text())
            self.assertFalse(output['ok'])
            self.assertEqual(output['total_posts'], 0)
            self.assertIn('HTTP 403', output['error'])
