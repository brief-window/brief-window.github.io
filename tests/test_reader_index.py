import base64
import json
import subprocess
import unittest
from unittest.mock import patch

import reader_index


STATUS = {
    "ok": True, "generated_at": "2026-10-21T16:03:17+00:00",
    "cutoff": "2026-10-21T13:03:17+00:00", "lookback_hours": 3,
    "categories": ["玩卡", "旅行", "理财"], "total_posts": 378,
    "total_topics": 109, "pages_fetched": 20, "error": None,
}
DIGEST = "a" * 64


def index(status=None):
    return reader_index.make_index(status or STATUS, "brief-window/brief-window.github.io", 100, 200, DIGEST, "b" * 40)


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.content = None
        self.put_error = None
        self.bad_readback = False
        self.artifacts = [
            {"id": 900, "name": "brief-window-feed", "created_at": "2026-10-21T15:54:00Z"},
            {"id": 200, "name": "brief-window-feed", "created_at": "2026-10-21T16:04:00Z"},
            {"id": 100, "name": "brief-window-feed", "created_at": "2026-10-21T16:14:00Z"},
            {"id": 50, "name": "github-pages", "created_at": "2026-10-21T15:54:00Z"},
        ]

    def api(self, method, path, body=None, missing_ok=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/actions/artifacts/200"):
            return {**self.artifacts[1], "workflow_run": {"id": 100}, "digest": "sha256:" + DIGEST, "expired": False}
        if method == "GET" and "/contents/" in path:
            if self.content is None:
                return None
            data = {} if self.bad_readback else self.content
            return {"sha": "current-file-sha", "content": base64.b64encode(json.dumps(data).encode()).decode()}
        if method == "PUT":
            if self.put_error:
                error, self.put_error = self.put_error, None
                raise error
            self.content = json.loads(base64.b64decode(body["content"]))
            return {"commit": {"sha": "new-commit-sha"}}
        if method == "GET" and "/actions/artifacts?" in path:
            return {"artifacts": self.artifacts}
        if method == "DELETE":
            return None
        raise AssertionError((method, path))


def response(code, data=None, headers=""):
    return subprocess.CompletedProcess([], 0 if code < 400 else 1,
        f"HTTP/2.0 {code} Status\n{headers}\n" + (json.dumps(data) if data is not None else ""), "")


class ReaderIndexTests(unittest.TestCase):
    def test_index_contains_metadata_without_bodies_or_credentials(self):
        status = {**STATUS, "posts": [{"text": "SECRET BODY"}], "token": "SECRET TOKEN"}
        result = index(status)
        encoded = json.dumps(result)
        self.assertNotIn("SECRET", encoded)
        self.assertEqual(result["artifact_id"], 200)
        self.assertEqual(result["total_posts"], 378)
        self.assertEqual(result["artifact_sha256"], DIGEST)

    def test_collection_failure_is_published_as_empty_error_metadata(self):
        result = index({**STATUS, "ok": False, "total_posts": 0, "total_topics": 0, "error": "HTTP 503"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 503")
        with self.assertRaises(ValueError):
            index({**STATUS, "ok": False, "error": "Partial data"})

    def test_invalid_scope_window_and_counts_are_rejected(self):
        for change in ({"cutoff": "2026-10-21T12:03:17+00:00"}, {"total_posts": -1},
                       {"categories": ["生活"]}, {"generated_at": "2026-10-21T16:03:17"},
                       {"ok": True, "error": "failure"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                index({**STATUS, **change})

    def test_invalid_artifact_identifier_and_digest_are_rejected(self):
        for artifact_id, digest in (("200; echo unsafe", DIGEST), (200, "not-a-digest")):
            with self.assertRaises(ValueError):
                reader_index.make_index(STATUS, "brief-window/brief-window.github.io", 100, artifact_id, digest, "b" * 40)

    def test_publish_readback_precedes_cleanup_and_ids_need_not_increase(self):
        api = FakeGitHub()
        reader_index.publish(index(), api)
        self.assertEqual([p.rsplit("/", 1)[-1] for m, p, _ in api.calls if m == "DELETE"], ["900"])
        methods = [method for method, _, _ in api.calls]
        self.assertLess(methods.index("PUT"), methods.index("DELETE"))
        put_position = methods.index("PUT")
        self.assertIn("/contents/latest.json?ref=main", api.calls[put_position + 1][1])

    def test_failed_publication_keeps_all_existing_artifacts(self):
        api = FakeGitHub()
        api.put_error = reader_index.GitHubError(403)
        with self.assertRaises(reader_index.GitHubError):
            reader_index.publish(index(), api)
        self.assertFalse(any(method == "DELETE" for method, _, _ in api.calls))

    @patch("reader_index.time.sleep")
    def test_failed_readback_retries_and_keeps_all_existing_artifacts(self, sleep):
        api = FakeGitHub()
        api.bad_readback = True
        with self.assertRaises(RuntimeError):
            reader_index.publish(index(), api)
        self.assertFalse(any(method == "DELETE" for method, _, _ in api.calls))
        self.assertEqual(sleep.call_count, 4)

    @patch("reader_index.time.sleep")
    def test_temporary_stale_readback_recovers_before_cleanup(self, sleep):
        api = FakeGitHub()
        original = api.api
        stale = [True, True, False]

        def request(method, path, body=None, missing_ok=False):
            if method == "GET" and "/contents/" in path and api.content is not None:
                api.bad_readback = stale.pop(0) if stale else False
            return original(method, path, body, missing_ok)

        api.api = request
        reader_index.publish(index(), api)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(any(method == "DELETE" for method, _, _ in api.calls))

    @patch("reader_index.time.sleep")
    def test_conflict_refreshes_pointer_and_retries_publication(self, sleep):
        api = FakeGitHub()
        api.content = {"previous": "index"}
        api.put_error = reader_index.GitHubError(409)
        reader_index.publish(index(), api)
        self.assertEqual(sum(m == "PUT" for m, _, _ in api.calls), 2)
        self.assertEqual(api.content["artifact_id"], 200)

    def test_artifact_digest_mismatch_cannot_replace_pointer(self):
        api = FakeGitHub()
        changed = index()
        changed["artifact_sha256"] = "c" * 64
        with self.assertRaises(ValueError):
            reader_index.publish(changed, api)
        self.assertFalse(any(m in ("PUT", "DELETE") for m, _, _ in api.calls))

    @patch("reader_index.time.sleep")
    @patch("reader_index.subprocess.run")
    def test_network_and_server_failures_have_five_attempts(self, run, sleep):
        run.side_effect = [subprocess.TimeoutExpired("gh", 45), response(503), response(502), response(429), response(200, {"ok": True})]
        self.assertEqual(reader_index.GitHub().api("GET", "test"), {"ok": True})
        self.assertEqual(run.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])

    @patch("reader_index.time.sleep")
    @patch("reader_index.subprocess.run", return_value=response(503))
    def test_exhausted_retries_stop_this_run_without_changing_schedule(self, run, sleep):
        with self.assertRaises(reader_index.GitHubError):
            reader_index.GitHub().api("GET", "test")
        self.assertEqual(run.call_count, 5)

    @patch("reader_index.time.sleep")
    @patch("reader_index.subprocess.run", return_value=response(403))
    def test_permission_error_is_not_misreported_as_transient(self, run, sleep):
        with self.assertRaises(reader_index.GitHubError):
            reader_index.GitHub().api("GET", "test")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
