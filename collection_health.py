"""One GitHub email thread per collection outage lasting at least 60 minutes."""

import base64
import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from reader_index import GitHub, GitHubError, stamp, status_metadata

STATE_PATH = "collection-health.json"
THRESHOLD = timedelta(minutes=60)
RECIPIENT = "ruiningyang1"


def initial_state():
    return {
        "schema_version": 1, "last_run_id": 0, "checked_at": None,
        "last_ok": None, "last_success_at": None, "first_failed_at": None,
        "failure_count": 0, "alert_issue_number": None,
    }


def validate_state(state):
    if set(state) != set(initial_state()) or state["schema_version"] != 1:
        raise ValueError("Unsupported collection health state")
    for key in ("last_run_id", "failure_count"):
        if type(state[key]) is not int or state[key] < 0:
            raise ValueError("Invalid health counter")
    if state["last_ok"] is not None and type(state["last_ok"]) is not bool:
        raise ValueError("Invalid health status")
    if state["last_run_id"] and (state["checked_at"] is None or state["last_ok"] is None):
        raise ValueError("Missing last collection check")
    for key in ("checked_at", "last_success_at", "first_failed_at"):
        if state[key] is not None:
            stamp(state[key])
    if state["first_failed_at"] and state["checked_at"] and stamp(state["first_failed_at"]) > stamp(state["checked_at"]):
        raise ValueError("Outage start is after the last check")
    number = state["alert_issue_number"]
    if number is not None and (type(number) is not int or number <= 0):
        raise ValueError("Invalid alert issue number")
    if state["last_ok"] is False and (not state["first_failed_at"] or not state["failure_count"]):
        raise ValueError("Missing outage start")
    if state["last_ok"] is not False and (state["first_failed_at"] or state["failure_count"] or number):
        raise ValueError("Inconsistent healthy state")
    return state


def transition(previous, status, run_id):
    """Return a new state and optional outage/recovery event, without I/O."""
    validate_state(previous)
    status = status_metadata(status)
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("Invalid run ID")
    if run_id == previous["last_run_id"]:
        return previous.copy(), None
    when = stamp(status["generated_at"])
    if previous["checked_at"] and when <= stamp(previous["checked_at"]):
        return previous.copy(), None
    current = previous.copy()
    current.update(last_run_id=run_id, checked_at=status["generated_at"], last_ok=status["ok"])
    if status["ok"]:
        event = "recovery" if previous["alert_issue_number"] else None
        current.update(last_success_at=status["generated_at"], first_failed_at=None,
                       failure_count=0, alert_issue_number=None)
    else:
        if previous["last_ok"] is not False:
            current["first_failed_at"] = status["generated_at"]
        current["failure_count"] += 1
        event = ("outage" if not previous["alert_issue_number"]
                 and when - stamp(current["first_failed_at"]) >= THRESHOLD else None)
    return current, event


def incident_marker(repository, state):
    identifier = hashlib.sha256((repository + "\n" + state["first_failed_at"]).encode()).hexdigest()[:24]
    return f"<!-- brief-window-collection-outage:{identifier} -->"


def pages(api, path):
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        values = api.api("GET", f"{path}{separator}per_page=100&page={page}")
        if not isinstance(values, list):
            raise ValueError("Invalid GitHub list response")
        yield from values
        if len(values) < 100:
            return
    raise RuntimeError("GitHub pagination limit reached; refusing duplicate notifications")


def find_issue(api, root, marker):
    # Include closed issues so a user closing an alert never causes another alert.
    for issue in pages(api, root + "/issues?state=all&sort=created&direction=desc"):
        if (not issue.get("pull_request") and marker in (issue.get("body") or "")
                and issue.get("user", {}).get("login") == "github-actions[bot]"):
            return issue
    return None


def post_once(api, path, body, lookup):
    """Reconcile ambiguous POST results rather than blindly retrying messages."""
    try:
        return api.api("POST", path, body, attempts=1)
    except GitHubError:
        result = lookup()
        if result is not None:
            return result
        raise


def notify_outage(api, repository, current, status):
    root = f"repos/{repository}"
    marker = incident_marker(repository, current)
    issue = find_issue(api, root, marker)
    if issue is None:
        match = re.match(r"HTTP (\d{3}) for ", status["error"] or "")
        reason = f"HTTP {match[1]}" if match else "collection request failed"
        issue = post_once(api, root + "/issues", {
            "title": "Brief Window：采集连续失败已达60分钟",
            "body": f"{marker}\n@{RECIPIENT}\n\n"
                    f"采集连续失败已达到60分钟，本次故障只提醒一次；恢复后会在本线程提醒。\n\n"
                    f"- 首次失败（UTC）：{current['first_failed_at']}\n"
                    f"- 最近检查（UTC）：{current['checked_at']}\n"
                    f"- 连续失败次数：{current['failure_count']}\n"
                    f"- 错误类别：{reason}\n"
                    f"- [查看运行日志](https://github.com/{repository}/actions/runs/{current['last_run_id']})\n\n"
                    "后台仍会按约30分钟间隔（带随机错峰）继续采集。帖子正文和错误响应正文不会写入这个通知。\n",
        }, lambda: find_issue(api, root, marker))
    return issue["number"]


def notify_recovery(api, repository, previous, current):
    root = f"repos/{repository}"
    path = f"{root}/issues/{previous['alert_issue_number']}"
    issue = api.api("GET", path)
    marker = incident_marker(repository, previous)
    if (marker not in (issue.get("body") or "") or issue.get("pull_request")
            or issue.get("user", {}).get("login") != "github-actions[bot]"):
        raise ValueError("Alert issue does not match this outage")
    recovery_marker = marker.replace("outage:", "recovery:")
    comments_path = path + "/comments"

    def find_comment():
        return next((comment for comment in pages(api, comments_path)
                     if recovery_marker in (comment.get("body") or "")
                     and comment.get("user", {}).get("login") == "github-actions[bot]"), None)

    if find_comment() is None:
        post_once(api, comments_path, {
            "body": f"{recovery_marker}\n@{RECIPIENT} 采集已恢复。\n\n"
                    f"最近成功采集（UTC）：{current['checked_at']}。\n"
                    f"[查看恢复运行](https://github.com/{repository}/actions/runs/{current['last_run_id']})\n",
        }, find_comment)
    if issue["state"] != "closed":
        api.api("PATCH", path, {"state": "closed", "state_reason": "completed"})


def update_health(api, repository, status, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    status = status_metadata(status)
    path = f"repos/{repository}/contents/{STATE_PATH}"
    previous_file = api.api("GET", path + "?ref=main", missing_ok=True)
    previous = (json.loads(base64.b64decode(previous_file["content"]))
                if previous_file else initial_state())
    current, event = transition(previous, status, run_id)
    if current == previous:
        return current, None
    if status["ok"] and previous["last_ok"] is False and not previous["alert_issue_number"]:
        # A notification may have been created just before its state PUT failed.
        # Reconcile it even if the very next collection succeeds.
        issue = find_issue(api, f"repos/{repository}", incident_marker(repository, previous))
        if issue:
            previous = {**previous, "alert_issue_number": issue["number"]}
            event = "recovery"
    if event == "outage":
        current["alert_issue_number"] = notify_outage(api, repository, current, status)
    elif event == "recovery":
        notify_recovery(api, repository, previous, current)
    body = {
        "message": "Update collection health [skip ci]", "branch": "main",
        "content": base64.b64encode((json.dumps(current, indent=2) + "\n").encode()).decode(),
    }
    if previous_file:
        body["sha"] = previous_file["sha"]
    # Workflow concurrency serializes these updates. A conflict is surfaced;
    # the next run reconciles already-created messages before trying again.
    api.api("PUT", path, body)
    return current, event


def main():
    state, event = update_health(
        GitHub(), os.environ["GH_REPO"], json.loads(os.environ["FEED_STATUS_JSON"]),
        int(os.environ["RUN_ID"]),
    )
    summary = (f"## Collection health\n\n"
               f"- Collection succeeded: `{state['last_ok']}`\n"
               f"- Consecutive failures: {state['failure_count']}\n"
               f"- First failed at (UTC): {state['first_failed_at'] or '—'}\n"
               f"- Notification: {event or 'none (deduplicated / below 60 minutes)'}\n")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(summary)
    print(json.dumps({"collection_ok": state["last_ok"], "notification": event,
                      "failure_count": state["failure_count"]}))


if __name__ == "__main__":
    main()
