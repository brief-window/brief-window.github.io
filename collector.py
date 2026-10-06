"""Publish an anonymous, rolling three-hour Discourse post snapshot."""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import cloudscraper
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.uscardforum.com"
LOOKBACK_HOURS = 3
TARGET_CATEGORIES = {12: "玩卡", 15: "旅行", 9: "理财"}
ROBOTS = "User-agent: *\nDisallow: /\n"


class CollectionError(RuntimeError):
    pass


def parse_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise CollectionError("The forum returned a timestamp without a timezone.")
    return stamp.astimezone(timezone.utc)


class ForumClient:
    """No forum account, API key, saved cookie, proxy, or CAPTCHA service."""

    def __init__(self, delay: float = 1.5):
        self.session = cloudscraper.create_scraper()
        self.session.headers.update({"Accept": "application/json"})
        self.delay = delay
        self.last_request = 0.0

    def get_json(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(
                    BASE_URL + path, params=params, timeout=(10, 35)
                )
            except requests.RequestException as exc:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise CollectionError(f"Network request failed for {path}.") from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    retry_after = response.headers.get("Retry-After", "")
                    wait = min(float(retry_after), 60) if retry_after.isdigit() else 2 ** (attempt + 1)
                    time.sleep(wait)
                    continue
            if response.status_code != 200:
                raise CollectionError(
                    f"HTTP {response.status_code} for {path}; anonymous access may be blocked."
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise CollectionError(f"Non-JSON response for {path}; a challenge page may be present.") from exc
            if not isinstance(data, dict):
                raise CollectionError(f"Unexpected JSON shape for {path}.")
            return data
        raise CollectionError(f"Retries exhausted for {path}.")


def category_roots(data: dict) -> tuple[dict[int, int], dict[int, str]]:
    categories = data.get("categories")
    if not isinstance(categories, list):
        raise CollectionError("Missing category metadata in /site.json.")
    by_id = {int(cat["id"]): cat for cat in categories}
    for root, name in TARGET_CATEGORIES.items():
        if root not in by_id or by_id[root]["name"] != name:
            raise CollectionError(f"The configured category {root} ({name}) no longer matches the forum.")
    roots = {}
    names = {}
    for category_id, category in by_id.items():
        names[category_id] = category["name"]
        current = category_id
        visited = set()
        while current in by_id and current not in visited:
            visited.add(current)
            if current in TARGET_CATEGORIES:
                roots[category_id] = current
                break
            parent = by_id[current].get("parent_category_id")
            if parent is None:
                break
            current = int(parent)
    return roots, names


def full_text(post: dict) -> str:
    # /posts.json includes raw Markdown; do not use its truncated excerpt.
    if isinstance(post.get("raw"), str):
        return post["raw"]
    if not isinstance(post.get("cooked"), str):
        raise CollectionError(f"Post {post['id']} is missing its full body.")
    soup = BeautifulSoup(post["cooked"], "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for image in soup.find_all("img"):
        image.replace_with(f"![{image.get('alt', 'image')}]({urljoin(BASE_URL, image.get('src', ''))})")
    for link in soup.find_all("a", href=True):
        link.replace_with(f"{link.get_text()} ({urljoin(BASE_URL, link['href'])})")
    return soup.get_text("\n", strip=True)


def fetch_recent_posts(
    client: ForumClient,
    cutoff: datetime,
    now: datetime,
    roots: dict[int, int],
    names: dict[int, str],
    max_pages: int = 200,
) -> tuple[list[dict], int]:
    before = None
    seen = set()
    posts = []
    for page_number in range(1, max_pages + 1):
        params = {"before": before} if before is not None else None
        data = client.get_json("/posts.json", params)
        batch = data.get("latest_posts")
        if not isinstance(batch, list):
            raise CollectionError("Missing latest_posts in /posts.json.")
        if not batch:
            return posts, page_number
        ids = [int(post["id"]) for post in batch]
        if before is not None and any(post_id >= before for post_id in ids):
            raise CollectionError("The forum ignored the before cursor; refusing a partial feed.")
        dates = [parse_time(post["created_at"]) for post in batch]
        for post, created_at in zip(batch, dates):
            post_id = int(post["id"])
            if post_id in seen:
                continue
            seen.add(post_id)
            if not cutoff <= created_at <= now:
                continue
            if post.get("hidden") or post.get("deleted_at") or post.get("user_deleted"):
                continue
            if post.get("post_type", 1) != 1:
                continue
            category_id = int(post["category_id"])
            if category_id not in names:
                raise CollectionError(f"Post {post_id} has an unknown category {category_id}.")
            if category_id not in roots:
                continue
            posts.append({
                "id": post_id,
                "topic_id": int(post["topic_id"]),
                "topic_title": post["topic_title"],
                "post_number": int(post["post_number"]),
                "username": post["username"],
                "created_at": created_at,
                "category": TARGET_CATEGORIES[roots[category_id]],
                "subcategory": names[category_id],
                "text": full_text(post),
            })
        # Do not stop at one backdated/imported post in a mixed-time page.
        if all(stamp < cutoff for stamp in dates):
            return posts, page_number
        before = min(ids)
    raise CollectionError(f"Reached {max_pages} pages before covering the three-hour window.")


def render(posts: list[dict], now: datetime, cutoff: datetime, error: str | None = None) -> str:
    esc = html.escape
    out = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="robots" content="noindex, nofollow, noarchive">',
        '<meta name="referrer" content="no-referrer">',
        '<title>Brief Window · 最近 3 小时</title>',
        '<style>body{max-width:1000px;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui,sans-serif;color:#202124}h1{font-size:1.8rem}section{border-top:2px solid #ddd;margin-top:2rem}article{border-top:1px solid #eee;padding:1rem 0}.meta{color:#555;font-size:.9rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.notice{background:#fff6dc;padding:1rem}a{color:#1558b0}</style>',
        '</head><body><h1>Brief Window · 最近 3 小时</h1>',
        '<p>数据来源：美卡论坛公开帖子。分类：玩卡、旅行、理财（含公开子分类）。以下为原始帖子正文，不是摘要。</p>',
        f'<p>Generated UTC: <time>{esc(now.isoformat())}</time><br>',
        f'Window UTC: {esc(cutoff.isoformat())} — {esc(now.isoformat())}<br>',
        f'Total posts: {len(posts)}</p>',
        '<p>每约 10 分钟更新。时间窗口以 Generated UTC 为准；若时间戳过旧，请勿将本页当作当前数据。</p>',
    ]
    if error:
        out.append(f'<p class="notice">抓取失败；本次不提供帖子。{esc(error)}</p>')
    elif not posts:
        out.append('<p>本次时间窗口内没有可见的目标分类帖子。</p>')
    grouped = defaultdict(list)
    for post in posts:
        grouped[post["topic_id"]].append(post)
    for topic_id, topic_posts in sorted(
        grouped.items(), key=lambda item: max(p["created_at"] for p in item[1]), reverse=True
    ):
        first = topic_posts[0]
        topic_url = f"{BASE_URL}/t/topic/{topic_id}"
        out.extend([
            f'<section><h2>[{esc(first["category"])}] <a href="{topic_url}">{esc(first["topic_title"])}</a></h2>',
            f'<p class="meta">Topic ID: {topic_id} | New posts: {len(topic_posts)}</p>',
        ])
        for post in sorted(topic_posts, key=lambda item: (item["created_at"], item["id"])):
            post_url = f'{topic_url}/{post["post_number"]}'
            out.extend([
                f'<article id="post-{post["id"]}"><div class="meta">',
                f'Post #{post["post_number"]} | Post ID {post["id"]} | ',
                f'{esc(post["username"])} | {esc(post["subcategory"])} | ',
                f'{esc(post["created_at"].isoformat())} | <a href="{post_url}">原帖</a></div>',
                f'<pre>{esc(post["text"])}</pre></article>',
            ])
        out.append('</section>')
    out.append('</body></html>')
    return "\n".join(out)


def write_output(output: Path, posts: list[dict], now: datetime, cutoff: datetime, pages: int = 0, error: str | None = None):
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(render(posts, now, cutoff, error), encoding="utf-8")
    (output / "robots.txt").write_text(ROBOTS, encoding="utf-8")
    (output / ".nojekyll").touch()
    # Operational metadata only: no duplicate or archived post bodies.
    (output / "status.json").write_text(json.dumps({
        "ok": error is None,
        "generated_at": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "categories": list(TARGET_CATEGORIES.values()),
        "total_posts": len(posts),
        "total_topics": len({post["topic_id"] for post in posts}),
        "pages_fetched": pages,
        "error": error,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("public"))
    parser.add_argument("--request-delay", type=float, default=1.5)
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()
    if args.request_delay < 1 or args.max_pages < 1:
        parser.error("request-delay must be at least 1 second and max-pages must be positive")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    write_output(args.output, [], now, cutoff, error="Collection has not completed.")
    try:
        client = ForumClient(args.request_delay)
        roots, names = category_roots(client.get_json("/site.json"))
        posts, pages = fetch_recent_posts(client, cutoff, now, roots, names, args.max_pages)
        write_output(args.output, posts, now, cutoff, pages)
        print(f"Recent target posts: {len(posts)}; topics: {len({p['topic_id'] for p in posts})}; pages: {pages}")
        return 0
    except Exception as exc:
        error = str(exc) if isinstance(exc, CollectionError) else f"Unexpected collector error ({type(exc).__name__})."
        write_output(args.output, [], now, cutoff, error=error)
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
