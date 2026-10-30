"""Read public Discourse post RSS and validated topic/category metadata."""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

BASE_URL = "https://www.uscardforum.com"
USER_AGENT = "BriefWindowReader/1.0 (+https://github.com/brief-window/brief-window.github.io)"
MAX_XML_BYTES = 4_000_000
CATEGORY_TTL_SECONDS = 86400


class RSSCollectionError(RuntimeError):
    pass


class RssForumClient:
    """Ordinary HTTPS only; no browser impersonation or challenge solving."""

    def __init__(self, delay=3.0):
        self.session = requests.Session()
        self.session.trust_env = False  # Do not read netrc credentials or proxy settings.
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = max(3.0, delay)
        self.last_request = 0.0
        self.request_count = 0

    def response(self, path, params=None, accept="application/rss+xml, application/xml;q=0.9"):
        if not re.fullmatch(r"/(?:posts\.rss|site\.json|t/topic/[1-9][0-9]*\.rss)", path):
            raise RSSCollectionError("Unsupported RSS source path")
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.request_count += 1
            try:
                response = self.session.get(BASE_URL + path, params=params,
                                            headers={"Accept": accept}, timeout=(10, 35))
            except requests.RequestException as error:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RSSCollectionError(f"Network request failed for {path}.") from error
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    retry = response.headers.get("Retry-After", "")
                    wait = 2 ** (attempt + 1)
                    if retry:
                        try:
                            wait = max(0, float(retry))
                        except ValueError:
                            try:
                                wait = max(0, (parsedate_to_datetime(retry)
                                               - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError, OverflowError):
                                pass
                    if wait > 60:
                        raise RSSCollectionError(f"HTTP {response.status_code} for {path}; server requested a longer cooldown.")
                    time.sleep(wait)
                    continue
            if response.status_code != 200:
                # 401/403 stop this run immediately; never try another identity.
                raise RSSCollectionError(f"HTTP {response.status_code} for {path}; public feed access was refused.")
            parsed = urlsplit(response.url)
            if parsed.scheme != "https" or parsed.netloc != urlsplit(BASE_URL).netloc:
                raise RSSCollectionError("Unexpected feed redirect")
            return response
        raise RSSCollectionError("RSS retries exhausted")

    def get_json(self, path, params=None):
        response = self.response(path, params, "application/json")
        try:
            data = response.json()
        except ValueError as error:
            raise RSSCollectionError("Invalid category metadata JSON") from error
        if not isinstance(data, dict):
            raise RSSCollectionError("Invalid category metadata shape")
        return data

    def get_xml(self, path, params=None):
        response = self.response(path, params)
        if "xml" not in response.headers.get("Content-Type", "").lower():
            raise RSSCollectionError("RSS request returned a non-XML response")
        return parse_xml(response.content)


def parse_xml(data):
    if len(data) > MAX_XML_BYTES:
        raise RSSCollectionError("Unsafe or oversized RSS response")
    class NoDTD(ET.TreeBuilder):
        def doctype(self, name, public_id, system_id):
            raise RSSCollectionError("DTD declarations are not allowed in RSS")
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=NoDTD()))
    except ET.ParseError as error:
        raise RSSCollectionError("Malformed RSS XML") from error
    if root.tag != "rss" or root.find("channel") is None:
        raise RSSCollectionError("Missing RSS channel")
    return root


def topic_link(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != urlsplit(BASE_URL).netloc:
        raise RSSCollectionError("Unexpected RSS post host")
    match = re.fullmatch(r"/t/([^/]+)/([1-9][0-9]*)(?:/([1-9][0-9]*))?/?", parsed.path)
    if not match or match[1].isdigit():
        raise RSSCollectionError("Ambiguous RSS post link")
    number = int(match[3]) if match[3] else None
    if parsed.fragment:
        fragment = re.fullmatch(r"post_([1-9][0-9]*)", parsed.fragment)
        if not fragment or number and number != int(fragment[1]):
            raise RSSCollectionError("Invalid RSS post position")
        number = int(fragment[1])
    if number is None:
        page = parse_qs(parsed.query).get("page", ["1"])
        if page != ["1"]:
            raise RSSCollectionError("RSS post position is missing")
        number = 1
    return int(match[2]), number


def parse_posts(root):
    records = []
    for item in root.findall("./channel/item"):
        identifier = item.findtext("guid") or ""
        match = re.fullmatch(r"www\.uscardforum\.com-post-([1-9][0-9]*)", identifier)
        creator = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
        author = re.fullmatch(r"@([^\s<>]+)(?:\s+.*)?", creator.strip())
        description = item.find("description")
        body = item.findtext("description")
        title = item.findtext("title")
        if (not match or not author or not isinstance(body, str) or not body.strip() or not title
                or description is None or len(description)):
            raise RSSCollectionError("Incomplete RSS post metadata or body")
        try:
            created_at = parsedate_to_datetime(item.findtext("pubDate"))
        except (ValueError, TypeError, OverflowError) as error:
            raise RSSCollectionError("Invalid RSS timestamp") from error
        if created_at.tzinfo is None:
            raise RSSCollectionError("RSS timestamp has no timezone")
        topic_id, post_number = topic_link(item.findtext("link") or "")
        records.append({"id": int(match[1]), "topic_id": topic_id,
                        "topic_title": title, "post_number": post_number,
                        "username": author[1], "created_at": created_at.astimezone(timezone.utc),
                        "cooked": body})
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise RSSCollectionError("Duplicate stable IDs in RSS page")
    return records


class TopicCategories:
    def __init__(self, path, now, names):
        if len(names) != len(set(names.values())):
            raise RSSCollectionError("Category names are ambiguous; cannot classify RSS safely")
        self.path = path
        self.now = now
        self.ids = {name: identifier for identifier, name in names.items()}
        self.entries = {}
        self.lookups = 0
        if path is not None:
            try:
                cached = json.loads(path.read_text())
                if cached["schema_version"] == 1 and cached["source"] == BASE_URL:
                    for key, entry in cached["topics"].items():
                        stamp = datetime.fromisoformat(entry["checked_at"])
                        age = (now - stamp).total_seconds()
                        if (re.fullmatch(r"[1-9][0-9]*", key) and 0 <= age < CATEGORY_TTL_SECONDS
                                and entry["category_name"] in self.ids):
                            self.entries[key] = {"category_name": entry["category_name"],
                                                 "checked_at": stamp.isoformat()}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.entries = {}

    def category_id(self, client, tid):
        key = str(tid)
        if key not in self.entries:
            channel = client.get_xml(f"/t/topic/{tid}.rss").find("channel")
            linked_tid, _ = topic_link(channel.findtext("link") or "")
            values = channel.findall("category")
            if linked_tid != tid or len(values) != 1 or values[0].text not in self.ids:
                raise RSSCollectionError("Topic RSS category could not be validated")
            self.entries[key] = {"category_name": values[0].text,
                                 "checked_at": self.now.isoformat()}
            self.lookups += 1
        return self.ids[self.entries[key]["category_name"]]

    def save(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"schema_version": 1, "source": BASE_URL,
                                            "topics": self.entries}, ensure_ascii=False) + "\n")
            temporary.replace(self.path)


def fetch_recent_posts_rss(client, cutoff, now, roots, names, targets,
                           text_converter, max_pages=200, cache_path=None):
    categories = TopicCategories(cache_path, now, names)
    before = None
    seen = set()
    result = []
    try:
        for page in range(1, max_pages + 1):
            records = parse_posts(client.get_xml("/posts.rss", {"before": before} if before else None))
            print(f"RSS page {page}: {len(records)} posts; topic lookups so far: {categories.lookups}", flush=True)
            if not records:
                return result, page
            ids = [post["id"] for post in records]
            if before is not None and any(identifier >= before for identifier in ids):
                raise RSSCollectionError("RSS ignored its before cursor; refusing a partial feed")
            for post in records:
                if post["id"] in seen:
                    continue
                seen.add(post["id"])
                if not cutoff <= post["created_at"] <= now:
                    continue
                category_id = categories.category_id(client, post["topic_id"])
                if category_id not in roots:
                    continue
                result.append({key: post[key] for key in (
                    "id", "topic_id", "topic_title", "post_number", "username", "created_at")})
                result[-1].update(category=targets[roots[category_id]], subcategory=names[category_id],
                                  text=text_converter(post))
            if all(post["created_at"] < cutoff for post in records):
                return result, page
            before = min(ids)
        raise RSSCollectionError(f"Reached {max_pages} RSS pages before covering the three-hour window")
    finally:
        # Retain validated classification only, even if a later body page failed.
        # No post bodies or credentials ever enter this cache.
        categories.save()
