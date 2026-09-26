#!/usr/bin/env python3
"""Collect bounded, source-backed news data for an unattended Hermes cron run."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
from html import unescape
from html.parser import HTMLParser
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import xml.etree.ElementTree as ET


USER_AGENT = "HermesNewsDigest/1.0 (+news aggregation; no scraping credentials)"
GITHUB_API_URL = "https://api.github.com"
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
STOP_WORDS = {"about", "after", "from", "into", "over", "that", "this", "with", "your"}
UTC_SUFFIX = "+00:00"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style", "nav", "footer"}:
            self.skip += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain(value: str) -> str:
    parser = _Text()
    parser.feed(value or "")
    return re.sub(r"[ \t]+", " ", unescape("".join(parser.parts))).strip()


def article_plain(html: str) -> tuple[str, bool]:
    for tag in ("article", "main"):
        match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.S | re.I)
        if match:
            return plain(match.group(1)), True
    return plain(html), False


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("non-HTTP or missing-host URL")
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query)
                       if not key.lower().startswith("utm_") and key.lower() not in {"ref", "fbclid"}])
    netloc = parsed.hostname.lower() + ((":" + str(parsed.port)) if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/") or "/", query, ""))


def _public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("unsafe source URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain", "127.0.0.1"} or host.endswith((".local", ".internal")):
        raise ValueError("private source URL")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise ValueError("source host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("source host resolves to a private address")


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def http_fetch(url: str, max_bytes: int, timeout: int = 12) -> bytes:
    _public_url(url)
    opener = build_opener(_SafeRedirect)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml, text/html, */*"})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response too large")
    return data


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value) if "," in value else datetime.fromisoformat(value.replace("Z", UTC_SUFFIX))
        return result.astimezone(timezone.utc) if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _item(title: str, url: str, published: datetime | None, evidence: str,
          source_id: str, now: datetime, window_hours: int,
          max_summary_chars: int, *, score: int = 0, discussion_count: int = 0) -> dict | None:
    if not published or published < now - timedelta(hours=window_hours) or published > now:
        return None
    try:
        url = canonical_url(url)
    except ValueError:
        return None
    evidence = plain(evidence)[:max_summary_chars]
    return {"title": plain(title)[:300], "url": url, "urls": [url], "source_ids": [source_id],
            "published_at": published.isoformat().replace(UTC_SUFFIX, "Z"), "evidence": evidence,
            "full_text_available": False, "score": score,
            "discussion_count": discussion_count, "discussion_excerpts": []}


def parse_rss(data: bytes, source_id: str, now: datetime, window_hours: int,
              max_items: int, max_summary_chars: int, *, audio_only: bool = False) -> list[dict]:
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(5_242_881)
        if len(data) > 5_242_880:
            raise ValueError("decompressed feed too large")
    root = ET.fromstring(data)
    nodes = root.findall("./channel/item") if root.tag != ATOM + "feed" else root.findall(ATOM + "entry")
    result = []
    for node in nodes[:max_items]:
        if audio_only and not _has_audio_enclosure(node):
            continue
        item = _parse_rss_node(node, root.tag == ATOM + "feed", source_id, now,
                               window_hours, max_summary_chars, audio_only)
        if item:
            result.append(item)
    return result


def _has_audio_enclosure(node) -> bool:
    return any((enclosure.get("type") or "").startswith("audio/")
               for enclosure in node.findall("enclosure"))


def _parse_rss_node(node, is_atom: bool, source_id: str, now: datetime,
                    window_hours: int, max_summary_chars: int, audio_only: bool) -> dict | None:
    fields = _atom_fields(node) if is_atom else _rss_fields(node)
    title, link, published, body, has_full_content = fields
    item = _item(title, link, _date(published), body, source_id, now, window_hours, max_summary_chars)
    if not item or not item["title"]:
        return None
    item["full_text_available"] = has_full_content and len(item["evidence"]) >= 400
    if audio_only:
        item["evidence_kind"] = "show_notes"
        item["full_text_available"] = False
    return item


def _atom_fields(node) -> tuple[str, str, str | None, str, bool]:
    link = next((entry.get("href") for entry in node.findall(ATOM + "link")
                 if entry.get("rel", "alternate") == "alternate"), "")
    body = node.findtext(ATOM + "content") or node.findtext(ATOM + "summary") or ""
    published = node.findtext(ATOM + "published") or node.findtext(ATOM + "updated")
    return (node.findtext(ATOM + "title") or "", link, published, body,
            node.find(ATOM + "content") is not None)


def _rss_fields(node) -> tuple[str, str, str | None, str, bool]:
    body = node.findtext(CONTENT + "encoded") or node.findtext("description") or ""
    published = node.findtext("pubDate") or node.findtext("date")
    return (node.findtext("title") or "", node.findtext("link") or "", published, body,
            node.find(CONTENT + "encoded") is not None)


def _json(fetch, url: str, max_bytes: int):
    return json.loads(fetch(url, max_bytes))


def _trending_articles(html: str) -> list[tuple[str, str]]:
    """Extract repository paths and descriptions from GitHub Trending cards."""
    articles = re.findall(r"(<article\b[^>]*>)(.*?)</article\s*>", html, re.S | re.I)
    result = []
    for opening_tag, article in articles:
        class_attr = re.search(r'\bclass="([^"]*)"', opening_tag, re.I)
        if not class_attr or "Box-row" not in class_attr.group(1).split():
            continue
        match = re.search(r'<h2\b[^>]*>\s*<a\b[^>]*\bhref="(/[^"/]+/[^"/]+)"',
                          article, re.S | re.I)
        if not match:
            continue
        description = re.search(r"<p\b[^>]*>(.*?)</p\s*>", article, re.S | re.I)
        result.append((match.group(1), plain(description.group(1)) if description else ""))
    return result


def _collect_rss(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    return parse_rss(fetch(source["url"], max_bytes), source_id, now, window, limit, max_chars,
                     audio_only=bool(source.get("audio_only", False)))


def _collect_arxiv(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    categories = source.get("categories", [])
    if not categories:
        raise ValueError("no categories configured")
    query = " OR ".join("cat:" + str(category) for category in categories)
    url = ("https://export.arxiv.org/api/query?" + urlencode({"search_query": query,
           "sortBy": "submittedDate", "sortOrder": "descending", "max_results": limit}))
    return parse_rss(fetch(url, max_bytes), source_id, now, window, limit, max_chars)


def _collect_hf_papers(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    for row in _json(fetch, "https://huggingface.co/api/daily_papers", max_bytes)[:limit]:
        paper = row.get("paper", {})
        paper_id = paper.get("id", "")
        listed = _date(paper.get("submittedOnDailyAt") or row.get("publishedAt"))
        if not paper_id:
            continue
        item = _item(row.get("title") or paper.get("title", ""),
                     "https://huggingface.co/papers/" + paper_id, listed,
                     row.get("summary") or paper.get("summary", ""),
                     source_id, now, window, max_chars,
                     score=int(paper.get("upvotes", 0)),
                     discussion_count=int(row.get("numComments", 0)))
        if item:
            published = _date(paper.get("publishedAt"))
            item["published_at"] = published.isoformat().replace(UTC_SUFFIX, "Z") if published else None
            item["listed_at"] = listed.isoformat().replace(UTC_SUFFIX, "Z")
            item["time_basis"] = "curation"
            item["evidence_kind"] = "abstract"
            items.append(item)
    return items


def _collect_hf_trending(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    rows = _json(fetch, "https://huggingface.co/api/trending", max_bytes).get("recentlyTrending", [])
    for row in rows[:limit]:
        repo = row.get("repoData") or {}
        if (row.get("repoType") or repo.get("repoType")) != "model" or not repo.get("id"):
            continue
        repo_id = repo["id"]
        evidence = (f"Observed in Hugging Face recently trending models. "
                    f"Repository: {repo_id}; pipeline: {repo.get('pipeline_tag') or 'unspecified'}; "
                    f"likes: {repo.get('likes', 0)}; downloads: {repo.get('downloads', 0)}. "
                    "Trending status is not a release date or quality measurement.")
        item = _item(repo_id, "https://huggingface.co/" + repo_id, now,
                     evidence, source_id, now, window, max_chars,
                     score=int(repo.get("likes") or 0))
        if item:
            item["published_at"] = None
            item["observed_at"] = now.isoformat().replace(UTC_SUFFIX, "Z")
            item["time_basis"] = "trending_observation"
            item["evidence_kind"] = "model_metadata"
            item["category"] = "model"
            items.append(item)
    return items


def _collect_swebench(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    data_url = "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json"
    leaderboards = _json(fetch, data_url, max_bytes).get("leaderboards", [])
    verified = next((board for board in leaderboards if board.get("name") == "Verified"), None)
    if verified is None:
        raise ValueError("SWE-bench Verified leaderboard is missing")
    recent = sorted(verified.get("results", []), key=lambda row: row.get("date", ""), reverse=True)
    for row in recent[:max(limit * 3, limit)]:
        name = row.get("name", "")
        score = row.get("resolved")
        published = _date(row.get("date"))
        if not name or not isinstance(score, (int, float)):
            continue
        evidence = (f"SWE-bench Verified submission dated {row['date']}: "
                    f"{name} resolved {score:g}% of tasks. "
                    "This is a submitted agent result, not an isolated model score.")
        item = _item("SWE-bench Verified: " + name,
                     "https://www.swebench.com/", published, evidence,
                     source_id, now, window, max_chars, score=int(score))
        if item:
            item["urls"].append(data_url)
            item["category"] = "benchmark"
            item["time_basis"] = "submission"
            item["evidence_kind"] = "benchmark_result"
            items.append(item)
    return items


def _collect_hackernews(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    ids = _json(fetch, "https://hacker-news.firebaseio.com/v0/topstories.json", max_bytes)[:limit]
    for story_id in ids:
        story = _json(fetch, f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", max_bytes)
        if not isinstance(story, dict) or story.get("dead") or story.get("deleted"):
            continue
        url = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        item = _item(story.get("title", ""), url, datetime.fromtimestamp(story["time"], timezone.utc),
                     story.get("text", ""), source_id, now, window, max_chars,
                     score=int(story.get("score", 0)), discussion_count=int(story.get("descendants", 0)))
        if item:
            item["discussion_url"] = f"https://news.ycombinator.com/item?id={story_id}"
            item["urls"].append(item["discussion_url"])
            item["comment_ids"] = story.get("kids", [])[:int(source.get("max_comments", 5))]
            items.append(item)
    return items


def _collect_reddit(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    for subreddit in source.get("subreddits", []):
        url = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
        try:
            listing = _json(fetch, url, max_bytes)
        except (OSError, ValueError) as exc:  # noqa: S5713
            issues.append({"id": source_id, "kind": "degraded",
                           "reason": f"r/{subreddit}: {str(exc)[:120]}"})
            continue
        items.extend(_reddit_items(listing, source, source_id, subreddit, now, limit,
                                   max_chars, window, issues))
    return items


def _reddit_items(listing: dict, source: dict, source_id: str, subreddit: str,
                  now: datetime, limit: int, max_chars: int, window: int,
                  issues: list[dict]) -> list[dict]:
    items = []
    rows = listing.get("data", {}).get("children", [])[:limit]
    for row in rows:
        item = _reddit_item(row.get("data", {}), source, source_id, subreddit,
                            now, max_chars, window, issues)
        if item:
            items.append(item)
    return items


def _reddit_item(post: dict, source: dict, source_id: str, subreddit: str,
                 now: datetime, max_chars: int, window: int,
                 issues: list[dict]) -> dict | None:
    created_utc = post.get("created_utc")
    if not isinstance(created_utc, (int, float)) or created_utc <= 0:
        issues.append({"id": source_id, "kind": "degraded",
                       "reason": f"r/{subreddit}: item missing or invalid created_utc"})
        return None
    item = _item(post.get("title", ""), post.get("url", ""),
                 datetime.fromtimestamp(created_utc, timezone.utc),
                 post.get("selftext", ""), source_id, now, window, max_chars,
                 score=int(post.get("score", 0)), discussion_count=int(post.get("num_comments", 0)))
    if not item:
        return None
    item["discussion_url"] = "https://www.reddit.com" + post.get("permalink", "")
    if post.get("permalink"):
        item["urls"].append(item["discussion_url"])
    item["reddit_post_id"] = post.get("id", "")
    item["max_comments"] = int(source.get("max_comments", 5))
    return item


def _collect_github_trending(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    html = fetch("https://github.com/trending?since=" + source.get("since", "daily"), max_bytes).decode("utf-8", "replace")
    for repo, description in _trending_articles(html)[:limit]:
        item = _item(repo.strip("/").replace("/", " / "), "https://github.com" + repo, now,
                     description, source_id, now, window, max_chars)
        if item:
            item["published_at"] = None
            item["observed_at"] = now.isoformat().replace(UTC_SUFFIX, "Z")
            item["time_basis"] = "trending_observation"
            item["full_text_available"] = False
            items.append(item)
    if not items and source.get("fallback", {}).get("type") == "github_notable":
        fallback = source["fallback"]
        query = f'{fallback.get("query", "topic:ai")} pushed:>={now.date() - timedelta(hours=window)} stars:>={fallback.get("min_stars", 500)}'
        url = GITHUB_API_URL + "/search/repositories?" + urlencode({"q": query, "sort": "stars", "per_page": limit})
        for repo in _json(fetch, url, max_bytes).get("items", [])[:limit]:
            item = _item(repo.get("full_name", ""), repo.get("html_url", ""),
                         _date(repo.get("pushed_at")),
                         repo.get("description", ""), source_id, now, window, max_chars,
                         score=int(repo.get("stargazers_count", 0)))
            if item:
                item["full_text_available"] = False
                items.append(item)
        issues.append({"id": source_id, "kind": "degraded", "reason": "GitHub Trending unavailable; used search fallback"})
    return items


def _collect_searxng(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    endpoint = os.environ.get("AI_DIGEST_SEARCH_URL") or os.environ.get("SEARXNG_URL", "")
    if not endpoint:
        issues.append({"id": source_id, "kind": "unavailable", "reason": "search endpoint is not configured"})
        return items
    # Validate the configured endpoint once (it's deployment-controlled, not user-supplied)
    _public_url(endpoint.rstrip("/") + "/search?q=test&format=json")
    url = endpoint.rstrip("/") + "/search?" + urlencode({"q": source["query"], "format": "json"})
    for result in _json(fetch, url, max_bytes).get("results", [])[:limit]:
        item = _item(result.get("title", ""), result.get("url", ""),
                     _date(result.get("publishedDate") or result.get("published_at")),
                     result.get("content", ""), source_id, now, window, max_chars)
        if item:
            item["full_text_available"] = False
            items.append(item)
    return items


def _source(source: dict, defaults: dict, now: datetime, fetch) -> tuple[list[dict], list[dict]]:
    source_id = source["id"]
    kind = source["type"]
    limit = int(source.get("max_items", defaults.get("max_items", 25)))
    max_bytes = int(defaults.get("max_response_bytes", 5_242_880))
    max_chars = int(defaults.get("max_summary_chars", 1200))
    window = int(defaults.get("window_hours", 24))
    issues = []
    collectors = {
        "rss": _collect_rss,
        "arxiv": _collect_arxiv,
        "hf_papers": _collect_hf_papers,
        "hf_trending": _collect_hf_trending,
        "swebench": _collect_swebench,
        "hackernews": _collect_hackernews,
        "reddit": _collect_reddit,
        "github_trending": _collect_github_trending,
        "searxng": _collect_searxng,
    }
    collector = collectors.get(kind)
    if collector is None:
        raise ValueError("unknown source type: " + str(kind))
    items = collector(source, source_id, now, fetch, limit, max_bytes, max_chars, window, issues)
    if not items and not (kind == "searxng" and issues and issues[-1]["kind"] == "unavailable"):
        issues.append({"id": source_id, "kind": "empty", "reason": "no items in window"})
    for item in items:
        item.setdefault("category", source.get("category", "news"))
    return items, issues


def _title_words(title: str) -> set[str]:
    return {word for word in re.findall(r"[\w-]{4,}", title.lower()) if word not in STOP_WORDS}


def _topic_words(topic: str) -> set[str]:
    return set(re.findall(r"[\w-]{2,}", topic.lower()))


def _deduplicate(items: list[dict]) -> list[dict]:
    merged = []
    for item in items:
        match = _duplicate_match(item, merged)
        if match is None:
            merged.append(item)
            continue
        _merge_duplicate(match, item)
    return merged


def _duplicate_match(item: dict, merged: list[dict]) -> dict | None:
    words = _title_words(item["title"])
    for existing in merged:
        existing_words = _title_words(existing["title"])
        union = words | existing_words
        if existing["url"] == item["url"] or (union and len(words & existing_words) / len(union) >= 0.6):
            return existing
    return None


def _merge_duplicate(match: dict, item: dict) -> None:
    match["source_ids"] = sorted(set(match["source_ids"] + item["source_ids"]))
    match["urls"] = sorted(set(match["urls"] + item["urls"]))
    match["score"] += item["score"]
    match["discussion_count"] += item["discussion_count"]
    if len(item["evidence"]) > len(match["evidence"]):
        match["evidence"] = item["evidence"]
        match["full_text_available"] = item["full_text_available"]
        for key in ("published_at", "listed_at", "observed_at", "time_basis", "evidence_kind", "category"):
            if key in item:
                match[key] = item[key]
            else:
                match.pop(key, None)
    for key in ("comment_ids", "reddit_post_id", "max_comments", "discussion_url"):
        if key in item and key not in match:
            match[key] = item[key]


def _importance(item: dict, now: datetime, window_hours: int) -> float:
    published = _date(item.get("listed_at") or item["published_at"])
    age_hours = max(0, (now - published).total_seconds() / 3600) if published else window_hours
    freshness = 10 * max(0, 1 - age_hours / window_hours)
    popularity = 2 * min(6, math.log1p(max(0, item["score"])))
    discussion = 2 * min(6, math.log1p(max(0, item["discussion_count"])))
    return freshness + popularity + discussion + 10 * (len(item["source_ids"]) - 1)


def _select_items(items: list[dict], limit: int, mode: str, now: datetime,
                  window_hours: int) -> list[dict]:
    ranked = sorted(items, key=lambda item: (-_importance(item, now, window_hours), item["title"]))
    selected, counts = [], {}
    max_per_source = max(1, math.ceil(limit / 3))
    if mode == "weekly":
        _select_weekly_categories(ranked, selected, counts, limit)
    for item in ranked:
        if len(selected) >= limit:
            break
        if item in selected:
            continue
        primary = item["source_ids"][0]
        if counts.get(primary, 0) >= max_per_source and any(
                counts.get(other["source_ids"][0], 0) < max_per_source
                for other in ranked if other not in selected):
            continue
        selected.append(item)
        counts[primary] = counts.get(primary, 0) + 1
    return selected


def _select_weekly_categories(ranked: list[dict], selected: list[dict],
                              counts: dict[str, int], limit: int) -> None:
    for category in ("research", "podcast", "benchmark"):
        candidate = next((item for item in ranked if item.get("category") == category), None)
        if candidate is not None and candidate not in selected and len(selected) < limit:
            selected.append(candidate)
            primary = candidate["source_ids"][0]
            counts[primary] = counts.get(primary, 0) + 1


def _collect_sources(sources: list[dict], defaults: dict, now: datetime, fetch) -> tuple[list[dict], list[dict]]:
    raw, issues = [], []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(sources)))) as pool:
        futures = {pool.submit(_source, source, defaults, now, fetch): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                items, source_issues = future.result()
                raw.extend(items)
                issues.extend(source_issues)
            except (ValueError, KeyError, TypeError, ET.ParseError, OSError) as exc:  # noqa: S5713
                issues.append({"id": source.get("id", "unknown"), "kind": "failed", "reason": str(exc)[:160]})
    return raw, issues


def _hydrate_items(selected: list[dict], defaults: dict, fetch) -> None:
    for item in selected:
        _fetch_discussions(item, fetch)
        _read_article(item, defaults, fetch)


def _fetch_discussions(item: dict, fetch) -> None:
    for comment_id in item.pop("comment_ids", []):
        try:
            if not isinstance(comment_id, int) or comment_id <= 0:
                raise ValueError("invalid comment_id")
            comment_url = f"https://hacker-news.firebaseio.com/v0/item/{comment_id}.json"
            _public_url(comment_url)
            comment = _json(fetch, comment_url, 100_000)
            body = plain(comment.get("text", ""))[:400]
            if body:
                item["discussion_excerpts"].append({"source_id": "hackernews", "text": body})
        except (OSError, ValueError, TypeError) as exc:  # noqa: S5713
            item["discussion_issue"] = f"Hacker News comment {comment_id} unavailable: {exc}"
    reddit_id = item.pop("reddit_post_id", "")
    max_comments = min(5, item.pop("max_comments", 5))
    if reddit_id:
        _fetch_reddit_comments(item, reddit_id, max_comments, fetch)


def _fetch_reddit_comments(item: dict, reddit_id: str, max_comments: int, fetch) -> None:
    try:
        reddit_url = f"https://www.reddit.com/comments/{reddit_id}.json?limit={max_comments}&sort=top"
        _public_url(reddit_url)
        discussion = _json(fetch, reddit_url, 500_000)
        for row in discussion[1].get("data", {}).get("children", [])[:max_comments]:
            body = plain(row.get("data", {}).get("body", ""))[:400]
            if row.get("kind") == "t1" and body:
                item["discussion_excerpts"].append({"source_id": "reddit", "text": body})
    except (OSError, ValueError, KeyError, IndexError, TypeError):  # noqa: S5713
        item["discussion_issue"] = "Reddit comments unavailable"


def _read_article(item: dict, defaults: dict, fetch) -> None:
    if (item["full_text_available"] or item.get("evidence_kind") in
            {"abstract", "benchmark_result", "model_metadata", "show_notes"}
            or item["url"].startswith("https://github.com/")):
        return
    try:
        body = fetch(item["url"], min(int(defaults.get("max_response_bytes", 5_242_880)), 2_000_000))
        if body.startswith(b"%PDF"):
            raise ValueError("PDF article is not supported")
        article, region_found = article_plain(body.decode("utf-8", "replace"))
        article = article[:int(defaults.get("max_article_chars", 4500))]
        if len(article) >= 400 and region_found:
            item["evidence"] = article
            item["full_text_available"] = True
        else:
            item["read_issue"] = "article body unavailable or too short"
    except (OSError, ValueError) as exc:  # noqa: S5713
        item["read_issue"] = str(exc)[:120]


def collect(config: dict, *, now: datetime | None = None, fetch=http_fetch,
            window_hours: int | None = None, limit: int | None = None, topic: str = "",
            mode: str = "daily") -> dict:
    if config.get("version") != 1 or not isinstance(config.get("sources"), list):
        raise ValueError("invalid sources.json schema")
    if mode not in {"daily", "weekly"}:
        raise ValueError("unknown digest mode")
    now = now or datetime.now(timezone.utc)
    defaults = dict(config.get("defaults", {}))
    profile = config.get("profiles", {}).get(mode, {})
    defaults["window_hours"] = (window_hours if window_hours is not None else
                                 int(profile.get("window_hours", defaults.get("window_hours", 24))))
    limit = limit if limit is not None else int(profile.get("limit", defaults.get("limit", 5)))
    timeout = int(defaults.get("request_timeout_s", 12))
    if not 1 <= defaults["window_hours"] <= 720 or not 1 <= limit <= 20 or not 1 <= timeout <= 30:
        raise ValueError("window or limit outside supported range")
    if fetch is http_fetch:
        def fetch(url: str, max_bytes: int) -> bytes:
            return http_fetch(url, max_bytes, timeout=timeout)
    sources = [source for source in config["sources"] if mode in source.get("modes", ["daily"])]
    raw, issues = _collect_sources(sources, defaults, now, fetch)
    in_window = len(raw)
    if topic:
        words = _topic_words(topic)
        raw = [item for item in raw if words & _topic_words(item["title"] + " " + item["evidence"])]
    merged = _deduplicate(raw)
    selected = _select_items(merged, limit, mode, now, defaults["window_hours"])
    _hydrate_items(selected, defaults, fetch)
    return {"schema_version": 1, "mode": mode, "generated_at": now.isoformat().replace(UTC_SUFFIX, "Z"),
            "window_hours": defaults["window_hours"], "limit": limit, "topic": topic or None,
            "items": selected, "source_issues": sorted(issues, key=lambda issue: issue["id"]),
            "stats": {"fetched": in_window, "after_dedup": len(merged), "returned": len(selected)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("sources.json"))
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("AI_DIGEST_STATE_DIR", "~/.hermes/ops/news")).expanduser())
    parser.add_argument("--window-hours", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--topic", default="")
    parser.add_argument("--mode", choices=("daily", "weekly"), default="daily")
    args = parser.parse_args(argv)
    started = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    try:
        config = json.loads(args.sources.read_text())
        result = collect(config, window_hours=args.window_hours, limit=args.limit,
                         topic=args.topic, mode=args.mode)
        result["run_id"] = run_id
        args.state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        output = args.state_dir / f"raw-{run_id}.json"
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        exit_code = 0 if result["items"] else 3
        with (args.state_dir / "runs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"ts": result["generated_at"], "run_id": run_id, "event": "collect",
                                     "returned": len(result["items"]), "source_issues": result["source_issues"],
                                     "exit_code": exit_code, "duration_s": round(time.monotonic() - started, 2)}) + "\n")
        print(output)
        return exit_code
    except (OSError, ValueError, TypeError) as exc:  # noqa: S5713
        print(f"collection failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
