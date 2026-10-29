import base64
import copy
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import collection_health as health
from reader_index import GitHub, GitHubError

REPO = "brief-window/brief-window.github.io"
NOW = datetime(2026, 10, 29, 19, tzinfo=timezone.utc)


def status(minute, ok=False, **extra):
    when = NOW + timedelta(minutes=minute)
    return {
        "ok": ok, "generated_at": when.isoformat(),
        "cutoff": (when - timedelta(hours=3)).isoformat(), "lookback_hours": 3,
        "categories": ["玩卡", "旅行", "理财"], "total_posts": 0,
        "total_topics": 0, "pages_fetched": 0,
        "error": None if ok else "HTTP 403 for /site.json; untrusted response must not be sent", **extra,
    }


class FakeGitHub:
    def __init__(self):
        self.state = None
        self.issues = []
        self.comments = []
        self.calls = []
        self.fail_put = False
        self.ambiguous_post = False
        self.reject_post = False

    def api(self, method, path, body=None, missing_ok=False, attempts=5):
        self.calls.append((method, path, body, attempts))
        url = urlsplit(path)
        if "/contents/" in url.path:
            if method == "GET":
                return None if self.state is None else {
                    "sha": "file-sha", "content": base64.b64encode(json.dumps(self.state).encode()).decode()}
            if method == "PUT":
                if self.fail_put:
                    self.fail_put = False
                    raise GitHubError(409)
                self.state = json.loads(base64.b64decode(body["content"]))
                return {}
        if method == "GET" and url.path.endswith("/issues"):
            self.assert_query(url.query, state="all", per_page="100")
            return copy.deepcopy(self.issues)
        if method == "GET" and url.path.endswith("/comments"):
            return copy.deepcopy(self.comments)
        if method == "GET":
            return copy.deepcopy(next(i for i in self.issues if str(i["number"]) == url.path.rsplit("/", 1)[-1]))
        if method == "POST":
            if self.reject_post:
                raise GitHubError(403)
            result = {**body, "user": {"login": "github-actions[bot]"}}
            if url.path.endswith("/comments"):
                result["id"] = len(self.comments) + 1
                self.comments.append(result)
            else:
                result.update(number=len(self.issues) + 1, state="open")
                self.issues.append(result)
            if self.ambiguous_post:
                self.ambiguous_post = False
                raise GitHubError(502)
            return result
        if method == "PATCH":
            self.issues[int(url.path.rsplit("/", 1)[-1]) - 1].update(body)
            return {}
        raise AssertionError((method, path))

    @staticmethod
    def assert_query(query, **expected):
        values = parse_qs(query)
        for key, value in expected.items():
            if values.get(key) != [value]:
                raise AssertionError((query, key, value))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def update(self, minute, ok=False, run_id=None, **extra):
        return health.update_health(self.api, REPO, status(minute, ok, **extra), run_id or minute + 100)

    def sustained_outage(self):
        self.update(0)
        self.update(30)
        return self.update(60)

    def test_alert_at_60_minutes_once_then_one_recovery(self):
        self.update(0)
        self.update(30)
        self.assertEqual(self.api.issues, [])
        current, event = self.update(60)
        self.assertEqual(event, "outage")
        self.assertEqual(current["failure_count"], 3)
        self.assertEqual(len(self.api.issues), 1)
        self.update(90)
        self.assertEqual(len(self.api.issues), 1)
        current, event = self.update(120, True)
        self.assertEqual(event, "recovery")
        self.assertEqual(len(self.api.comments), 1)
        self.assertEqual(self.api.issues[0]["state"], "closed")
        self.assertEqual(current["failure_count"], 0)
        self.update(150, True)
        self.assertEqual(len(self.api.comments), 1)

    def test_short_outage_is_silent_and_success_resets_timer(self):
        self.update(0)
        self.update(30, True)
        self.update(60)
        self.update(90)
        self.assertEqual(self.api.issues, [])
        self.update(120)
        self.assertEqual(len(self.api.issues), 1)

    def test_new_outage_after_recovery_gets_its_own_thread(self):
        self.sustained_outage()
        self.update(90, True)
        self.update(120)
        self.update(150)
        self.update(180)
        self.assertEqual(len(self.api.issues), 2)

    def test_replay_and_old_run_cannot_duplicate_or_rollback(self):
        self.sustained_outage()
        previous = copy.deepcopy(self.api.state)
        self.update(60)
        self.update(30, True)
        self.assertEqual(self.api.state, previous)
        self.assertEqual(len(self.api.issues), 1)

    def test_lost_state_write_after_issue_creation_is_reconciled(self):
        self.update(0)
        self.update(30)
        self.api.fail_put = True
        with self.assertRaises(GitHubError):
            self.update(60)
        self.update(90)
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.state["alert_issue_number"], 1)

    def test_recovery_reconciles_issue_created_before_failed_state_write(self):
        self.update(0)
        self.update(30)
        self.api.fail_put = True
        with self.assertRaises(GitHubError):
            self.update(60)
        self.update(90, True)
        self.assertEqual(len(self.api.comments), 1)
        self.assertEqual(self.api.issues[0]["state"], "closed")

    def test_ambiguous_issue_and_comment_posts_are_not_retried_blindly(self):
        self.update(0)
        self.update(30)
        self.api.ambiguous_post = True
        self.update(60)
        self.api.ambiguous_post = True
        self.update(90, True)
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(call[3] == 1 for call in posts))
        self.assertEqual(len(self.api.comments), 1)

    def test_lost_state_write_after_recovery_does_not_repeat_email(self):
        self.sustained_outage()
        self.api.fail_put = True
        with self.assertRaises(GitHubError):
            self.update(90, True)
        self.update(120, True)
        self.assertEqual(len(self.api.comments), 1)
        self.assertTrue(self.api.state["last_ok"])

    def test_notification_denied_does_not_mark_it_sent(self):
        self.update(0)
        self.update(30)
        self.api.reject_post = True
        with self.assertRaises(GitHubError):
            self.update(60)
        self.assertIsNone(self.api.state["alert_issue_number"])

    def test_closed_outage_issue_is_not_recreated(self):
        self.sustained_outage()
        self.api.issues[0]["state"] = "closed"
        self.update(90)
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.comments, [])

    def test_error_bodies_posts_and_credentials_do_not_enter_notifications_or_state(self):
        for minute in (0, 30, 60):
            self.update(minute, error="SECRET BODY @someone `command`", posts=["SECRET POST"], token="SECRET TOKEN")
        self.assertNotIn("SECRET", json.dumps([self.api.state, self.api.issues, self.api.comments]))

    def test_invalid_or_out_of_order_status_does_not_write_state(self):
        self.update(30)
        original = copy.deepcopy(self.api.state)
        self.update(0, run_id=999)
        with self.assertRaises(ValueError):
            self.update(60, lookback_hours=4)
        self.assertEqual(self.api.state, original)

    def test_new_collection_does_not_require_increasing_github_ids(self):
        self.update(0, run_id=999)
        self.update(30, True, run_id=2)
        self.assertTrue(self.api.state["last_ok"])

    @patch("reader_index.time.sleep")
    @patch("reader_index.subprocess.run")
    def test_non_idempotent_post_can_disable_automatic_retry(self, run, sleep):
        run.return_value = subprocess.CompletedProcess([], 1, "HTTP/2.0 503 unavailable\n\n{}", "")
        with self.assertRaises(GitHubError):
            GitHub().api("POST", "repos/x/y/issues", {"title": "test"}, attempts=1)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
