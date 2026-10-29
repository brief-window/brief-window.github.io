"""Publish a metadata-only pointer before removing superseded reader artifacts."""

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ARTIFACT_NAME = "brief-window-feed"
INDEX_PATH = "latest.json"
STATUS_KEYS = (
    "ok", "generated_at", "cutoff", "lookback_hours", "categories",
    "total_posts", "total_topics", "pages_fetched", "error",
)


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp must include its timezone")
    return result


def status_metadata(status):
    # An allowlist prevents post bodies or unrelated fields entering Git history.
    result = {key: status[key] for key in STATUS_KEYS}
    if type(result["ok"]) is not bool:
        raise ValueError("Invalid collection status")
    if result["lookback_hours"] != 3 or set(result["categories"]) != {"玩卡", "旅行", "理财"}:
        raise ValueError("Unexpected collection scope")
    if (stamp(result["generated_at"]) - stamp(result["cutoff"])).total_seconds() != 10800:
        raise ValueError("Unexpected collection window")
    for key in ("total_posts", "total_topics", "pages_fetched"):
        if type(result[key]) is not int or result[key] < 0:
            raise ValueError("Invalid collection count")
    if result["total_topics"] > result["total_posts"]:
        raise ValueError("Inconsistent collection counts")
    if result["ok"]:
        if result["error"] is not None:
            raise ValueError("Successful collection cannot have an error")
    elif not isinstance(result["error"], str) or not result["error"]:
        raise ValueError("Failed collection must report its error")
    elif result["total_posts"] or result["total_topics"]:
        raise ValueError("Failed collection cannot publish partial posts")
    return result


def make_index(status, repository, run_id, artifact_id, digest, source_sha):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    for value in (run_id, artifact_id):
        if not re.fullmatch(r"[1-9][0-9]*", str(value)):
            raise ValueError("Invalid GitHub identifier")
    digest = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("Invalid artifact or source digest")
    return {
        "schema_version": 1,
        "repository": repository,
        "workflow": "Refresh Brief Window",
        "run_id": int(run_id),
        "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "artifact_id": int(artifact_id),
        "artifact_name": ARTIFACT_NAME,
        "artifact_sha256": digest,
        "source_sha": source_sha,
        "published_at": datetime.now(timezone.utc).isoformat(),
        **status_metadata(status),
    }


class GitHubError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub request failed (HTTP {status or 'network error'})")


class GitHub:
    def api(self, method, path, body=None, missing_ok=False, attempts=5):
        for attempt in range(attempts):
            command = ["gh", "api", "--include", "--method", method, path]
            if body is not None:
                command += ["--input", "-"]
            try:
                result = subprocess.run(
                    command, input=json.dumps(body) if body is not None else None,
                    capture_output=True, text=True, timeout=45,
                )
                headers, separator, payload = result.stdout.partition("\n\n")
                match = re.search(r"^HTTP/\S+ (\d+)", headers)
                code = int(match.group(1)) if match else None
                if result.returncode == 0 and code and 200 <= code < 300:
                    return json.loads(payload) if separator and payload.strip() else None
                if missing_ok and code == 404:
                    return None
                transient = code is None or code in (408, 425, 429, 500, 502, 503, 504)
                transient |= code == 403 and bool(re.search(
                    r"(?im)^retry-after:|^x-ratelimit-remaining:\s*0\s*$", headers,
                ))
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                code, transient = None, True
            if not transient or attempt == attempts - 1:
                raise GitHubError(code)
            print(f"GitHub HTTP {code or 'timeout'}; retry {attempt + 2}/{attempts}", flush=True)
            time.sleep(2 ** (attempt + 1))


def publish(index, api):
    repository = index["repository"]
    root = f"repos/{repository}"
    current = api.api("GET", f"{root}/actions/artifacts/{index['artifact_id']}")
    if (current["name"] != ARTIFACT_NAME or current.get("expired")
            or current["workflow_run"]["id"] != index["run_id"]
            or current.get("digest") != "sha256:" + index["artifact_sha256"]):
        raise ValueError("Uploaded artifact does not match the reader index")
    current_created = stamp(current["created_at"])
    contents_path = f"{root}/contents/{INDEX_PATH}"
    encoded = base64.b64encode((json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode()).decode()
    for attempt in range(5):
        previous = api.api("GET", contents_path + "?ref=main", missing_ok=True)
        if previous and json.loads(base64.b64decode(previous["content"])) == index:
            break
        body = {"message": "Update latest reader index [skip ci]", "content": encoded, "branch": "main"}
        if previous:
            body["sha"] = previous["sha"]
        try:
            api.api("PUT", contents_path, body)
            break
        except GitHubError as error:
            if error.status not in (409, 422) or attempt == 4:
                raise
            time.sleep(2 ** (attempt + 1))
    print(f"Published {INDEX_PATH}: run {index['run_id']}, artifact {index['artifact_id']}", flush=True)

    # Publish and read back the pointer before deleting anything it used to name.
    for attempt in range(5):
        verified = api.api("GET", contents_path + "?ref=main", missing_ok=True)
        if verified and json.loads(base64.b64decode(verified["content"])) == index:
            break
        if attempt < 4:
            time.sleep(2 ** (attempt + 1))
    else:
        raise RuntimeError("Reader index read-back failed; keeping older artifacts")
    artifacts = []
    for page in range(1, 101):
        batch = api.api("GET", f"{root}/actions/artifacts?per_page=100&page={page}")["artifacts"]
        artifacts.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError("Artifact pagination limit reached; keeping older artifacts")
    for artifact in artifacts:
        if (artifact["name"] == ARTIFACT_NAME and artifact["id"] != index["artifact_id"]
                and stamp(artifact["created_at"]) < current_created):
            api.api("DELETE", f"{root}/actions/artifacts/{artifact['id']}", missing_ok=True)
            print(f"Removed superseded reader artifact {artifact['id']}", flush=True)


def main():
    if sys.argv[1:] == ["status"]:
        status = status_metadata(json.loads(Path("public/status.json").read_text(encoding="utf-8")))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("status_json=" + json.dumps(status, ensure_ascii=False) + "\n")
    elif sys.argv[1:] == ["publish"]:
        index = make_index(
            json.loads(os.environ["FEED_STATUS_JSON"]), os.environ["GH_REPO"],
            os.environ["RUN_ID"], os.environ["READER_ARTIFACT_ID"],
            os.environ["READER_ARTIFACT_DIGEST"], os.environ["SOURCE_SHA"],
        )
        publish(index, GitHub())
    else:
        raise SystemExit("Usage: python reader_index.py status|publish")


if __name__ == "__main__":
    main()
