import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import collector


NOW = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(hours=3)
CATEGORIES = {"categories": [
    {"id": 12, "name": "玩卡"}, {"id": 15, "name": "旅行"},
    {"id": 9, "name": "理财"}, {"id": 13, "name": "信用卡", "parent_category_id": 12},
    {"id": 99, "name": "生活"},
]}


def post(post_id, stamp=NOW - timedelta(minutes=10), **changes):
    value = {"id": post_id, "created_at": stamp.isoformat(), "category_id": 13,
             "topic_id": 1, "topic_title": "A < B", "post_number": 3203,
             "username": "reader", "raw": "完整正文 <script>alert(1)</script>",
             "excerpt": "截断", "post_type": 1}
    value.update(changes)
    return value


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, params))
        return {"latest_posts": next(self.pages)}


class CollectorTests(unittest.TestCase):
    def collect(self, client, max_pages=200):
        roots, names = collector.category_roots(CATEGORIES)
        return collector.fetch_recent_posts(client, CUTOFF, NOW, roots, names, max_pages)

    def test_cursor_large_floor_mixed_dates_and_category_filter(self):
        client = FakeClient([
            [post(110), post(109, CUTOFF - timedelta(minutes=1)), post(108, category_id=99)],
            [post(107, post_number=3204), post(106, hidden=True), post(105, deleted_at="deleted")],
            [post(104, CUTOFF - timedelta(minutes=2))],
        ])
        posts, pages = self.collect(client)
        self.assertEqual([p["id"] for p in posts], [110, 107])
        self.assertEqual(pages, 3)
        self.assertEqual(client.calls, [("/posts.json", None), ("/posts.json", {"before": 108}),
                                        ("/posts.json", {"before": 105})])
        self.assertEqual(posts[0]["post_number"], 3203)
        self.assertEqual(posts[0]["category"], "玩卡")

    def test_boundary_future_and_duplicate_posts(self):
        client = FakeClient([[post(10, CUTOFF), post(10, CUTOFF), post(9, NOW + timedelta(seconds=1))], []])
        posts, _ = self.collect(client)
        self.assertEqual([p["id"] for p in posts], [10])

    def test_cursor_ignored_is_failure(self):
        with self.assertRaises(collector.CollectionError):
            self.collect(FakeClient([[post(10)], [post(10)]]))

    def test_page_limit_is_failure_instead_of_partial_success(self):
        with self.assertRaises(collector.CollectionError):
            self.collect(FakeClient([[post(10)]]), max_pages=1)

    def test_unknown_category_is_failure(self):
        with self.assertRaises(collector.CollectionError):
            self.collect(FakeClient([[post(10, category_id=1000)]]))

    def test_category_id_drift_is_failure(self):
        with self.assertRaises(collector.CollectionError):
            collector.category_roots({"categories": [{"id": 12, "name": "Changed"}]})

    def test_full_raw_body_is_used_and_html_is_escaped(self):
        posts, _ = self.collect(FakeClient([[post(10)], []]))
        output = collector.render(posts, NOW, CUTOFF)
        self.assertIn("完整正文", output)
        self.assertNotIn("截断", output)
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn("noindex, nofollow, noarchive", output)
        self.assertIn("/t/topic/1/3203", output)
        self.assertIn("Total posts: 1", output)

    def test_cooked_fallback_keeps_link_and_image_urls(self):
        text = collector.full_text({"id": 1, "cooked": '<p>hi <a href="/t/topic/1">link</a><img src="/x.png" alt="picture"></p>'})
        self.assertIn("https://www.uscardforum.com/t/topic/1", text)
        self.assertIn("https://www.uscardforum.com/x.png", text)

    def test_failure_replaces_existing_body_and_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "index.html").write_text("STALE POST", encoding="utf-8")
            with patch.object(collector.ForumClient, "get_json", side_effect=collector.CollectionError("HTTP 403")), \
                 patch("sys.argv", ["collector.py", "--output", str(output)]):
                self.assertEqual(collector.main(), 1)
            text = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("STALE POST", text)
            self.assertIn("HTTP 403", text)
            self.assertIn("Total posts: 0", text)
            self.assertEqual((output / "robots.txt").read_text(), collector.ROBOTS)


if __name__ == "__main__":
    unittest.main()
