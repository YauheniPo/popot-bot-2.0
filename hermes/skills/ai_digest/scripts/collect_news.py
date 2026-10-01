#!/usr/bin/env python3
"""Collect bounded, source-backed news data for an unattended Hermes cron run."""

from __future__ import annotations

import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
from html import unescape
from html.parser import HTMLParser
import http.client
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
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
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
    body = re.search(r"<body\b[^>]*>(.*?)</body>", html, re.S | re.I)
    if body:
        return plain(body.group(1)), False
    return plain(html), False


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("non-HTTP or missing-host URL")
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query)
                       if not key.lower().startswith("utm_") and key.lower() not in {"ref", "fbclid"}])
    netloc = parsed.hostname.lower() + ((":" + str(parsed.port)) if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/") or "/", query, ""))


def _public_url(url: str, *, allow_loopback: bool = False) -> None:
    _public_addresses(url, allow_loopback=allow_loopback)


def _public_addresses(url: str, *, allow_loopback: bool = False) -> list[tuple]:  # noqa: S3776
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("unsafe source URL")
    host = parsed.hostname.lower()
    if allow_loopback and host == "127.0.0.1":
        try:
            addresses = socket.getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError("local search host could not be resolved") from exc
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_loopback for row in addresses):
            raise ValueError("local search host did not resolve only to loopback")
        return addresses
    if host in {"localhost", "localhost.localdomain", "127.0.0.1"} or host.endswith((".local", ".internal")):
        raise ValueError("private source URL")
    try:
        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ValueError("source host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("source host resolves to a private address")
    return addresses


def _connect_resolved(addresses: list[tuple], timeout: float) -> socket.socket:
    last_error = None
    for family, socktype, protocol, _, sockaddr in addresses:
        connection = socket.socket(family, socktype, protocol)
        try:
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            connection.close()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("no resolved source addresses")


def _pinned_connection_class(connection_type, url: str, *, allow_loopback: bool = False):
    addresses = _public_addresses(url, allow_loopback=allow_loopback)

    class PinnedConnection(connection_type):
        def connect(self):
            connection = _connect_resolved(addresses, self.timeout)
            if issubclass(connection_type, http.client.HTTPSConnection):
                try:
                    self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                except BaseException:
                    connection.close()
                    raise
            else:
                self.sock = connection

    return PinnedConnection


class _SafeHTTPHandler(HTTPHandler):
    def __init__(self, *, allow_loopback: bool = False):
        super().__init__()
        self.allow_loopback = allow_loopback

    def http_open(self, req):
        connection_type = _pinned_connection_class(
            http.client.HTTPConnection, req.full_url, allow_loopback=self.allow_loopback,
        )
        return self.do_open(connection_type, req)


class _SafeHTTPSHandler(HTTPSHandler):
    def __init__(self, *, allow_loopback: bool = False):
        super().__init__()
        self.allow_loopback = allow_loopback

    def https_open(self, req):
        connection_type = _pinned_connection_class(
            http.client.HTTPSConnection, req.full_url, allow_loopback=self.allow_loopback,
        )
        return self.do_open(connection_type, req, context=self._context)


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def http_fetch(url: str, max_bytes: int, timeout: int = 12, *, allow_loopback: bool = False) -> bytes:
    _public_url(url, allow_loopback=allow_loopback)
    opener = build_opener(ProxyHandler({}), _SafeHTTPHandler(allow_loopback=allow_loopback),
                          _SafeHTTPSHandler(allow_loopback=allow_loopback), _SafeRedirect())
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
    body = _element_text(node.find(ATOM + "content")) or _element_text(node.find(ATOM + "summary"))
    published = node.findtext(ATOM + "published") or node.findtext(ATOM + "updated")
    return (_element_text(node.find(ATOM + "title")), link, published, body,
            node.find(ATOM + "content") is not None)


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    if len(element):
        return " ".join(element.itertext())
    return element.text or ""


def _rss_fields(node) -> tuple[str, str, str | None, str, bool]:
    body = node.findtext(CONTENT + "encoded") or node.findtext("description") or ""
    published = node.findtext("pubDate") or node.findtext("date")
    return (node.findtext("title") or "", node.findtext("link") or "", published, body,
            node.find(CONTENT + "encoded") is not None)


def _json(fetch, url: str, max_bytes: int):
    return json.loads(fetch(url, max_bytes))


def _trending_repository_path(article: str) -> str | None:
    match = re.search(r'<h2\b[^>]*>\s*<a\b[^>]*\bhref="(/[^"/]+/[^"/]+)"',
                      article, re.S | re.I)
    return match.group(1) if match else None


def _trending_language(article: str) -> str | None:
    match = re.search(
        r'<span\b[^>]*\bitemprop="programmingLanguage"[^>]*>(.*?)</span\s*>',
        article, re.S | re.I,
    )
    return plain(match.group(1)) if match else None


def _trending_star_count(article: str) -> int | None:
    stars_link = re.search(
        r'<a\b[^>]*\bhref="/[^"/]+/[^"/]+/stargazers(?:\?[^"]*)?"[^>]*>(.*?)</a\s*>',
        article, re.S | re.I,
    )
    if not stars_link:
        return None  # pragma: no cover
    count = re.search(r"\b[\d,]+\b", plain(stars_link.group(1)))
    return int(count.group().replace(",", "")) if count else None


def _trending_stars_today(article: str) -> int | None:
    for span in re.finditer(r'<span\b[^>]*>(.*?)</span\s*>', article, re.S | re.I):
        match = re.fullmatch(r"\s*([\d,]+)\s+stars?\s+today\s*",
                             plain(span.group(1)), re.I)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _trending_card(article: str) -> dict | None:
    path = _trending_repository_path(article)
    if not path:
        return None
    description = re.search(r"<p\b[^>]*>(.*?)</p\s*>", article, re.S | re.I)
    return {
        "path": path,
        "description": plain(description.group(1)) if description else "",
        "language": _trending_language(article),
        "stars": _trending_star_count(article),
        "stars_today": _trending_stars_today(article),
    }


def _trending_articles(html: str) -> list[dict]:
    """Extract repository details from GitHub Trending cards."""
    articles = re.findall(r"(<article\b[^>]*>)(.*?)</article\s*>", html, re.S | re.I)
    result = []
    for opening_tag, article in articles:
        class_attr = re.search(r'\bclass="([^"]*)"', opening_tag, re.I)
        if not class_attr or "Box-row" not in class_attr.group(1).split():
            continue
        card = _trending_card(article)
        if card:
            result.append(card)
    return result


def _markdown_heading(line: str) -> str | None:
    indentation = len(line) - len(line.lstrip(" "))
    if indentation > 3:
        return None  # pragma: no cover
    content = line[indentation:]
    level = len(content) - len(content.lstrip("#"))
    if not 1 <= level <= 4 or len(content) == level or not content[level].isspace():
        return None
    title = content[level:].strip()
    while title.endswith("#"):
        title = title[:-1].rstrip()  # pragma: no cover
    return plain(title) or None


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
            item["listed_at"] = listed.isoformat().replace(UTC_SUFFIX, "Z") if listed else None
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


def _try_reddit_fallback(source: dict, source_id: str, subreddit: str, exc: BaseException,
                         now: datetime, fetch, limit: int, max_bytes: int,
                         max_chars: int, window: int, issues: list[dict]) -> list[dict] | None:
    """Attempt search fallback for a subreddit; returns items or None if fallback fails."""
    fallback = source.get("search_fallback")
    query_template = fallback.get("query") if isinstance(fallback, dict) else None
    if not isinstance(query_template, str) or "{subreddit}" not in query_template:
        issues.append({"id": source_id, "kind": "degraded",
                       "reason": f"r/{subreddit}: {str(exc)[:120]}"})
        return None
    fallback_source = {"query": query_template.replace("{subreddit}", subreddit)}
    try:
        fallback_items = _collect_searxng(
            fallback_source, source_id, now, fetch, limit, max_bytes, max_chars, window, issues,
        )
    except (OSError, ValueError) as fallback_error:  # noqa: S5713
        issues.append({"id": source_id, "kind": "degraded",
                       "reason": (f"r/{subreddit}: direct API failed ({str(exc)[:60]}); "
                                  f"search fallback failed ({str(fallback_error)[:60]})")})
        return None
    if not fallback_items:
        issues.append({"id": source_id, "kind": "degraded",
                       "reason": f"r/{subreddit}: direct API failed; search fallback returned no dated results"})
        return None
    for item in fallback_items:
        item["evidence_kind"] = "reddit_search_snippet"
    issues.append({"id": source_id, "kind": "degraded",
                   "reason": (f"r/{subreddit}: direct API failed ({str(exc)[:80]}); "
                              "used search fallback")})
    return fallback_items


def _collect_reddit(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:  # noqa: S3776
    items = []
    for subreddit in source.get("subreddits", []):
        url = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
        try:
            listing = _json(fetch, url, max_bytes)
        except (OSError, ValueError) as exc:  # noqa: S5713
            fallback_items = _try_reddit_fallback(
                source, source_id, subreddit, exc,
                now, fetch, limit, max_bytes, max_chars, window, issues,
            )
            if fallback_items:
                items.extend(fallback_items)
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
                 issues: list[dict]) -> dict | None:  # noqa: S3776
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


def _github_trending_item(repo: dict, source_id: str, now: datetime, window: int,
                          max_chars: int) -> dict | None:
    evidence = [repo["description"]] if repo["description"] else []
    evidence.extend(f"{label}: {repo[key]:,}" for key, label in (
        ("stars", "GitHub stars"), ("stars_today", "GitHub stars today")
    ) if repo[key] is not None)
    if repo["language"]:
        evidence.append("Language: " + repo["language"])
    item = _item(repo["path"].strip("/").replace("/", " / "),
                 "https://github.com" + repo["path"], now,
                 ". ".join(evidence), source_id, now, window, max_chars,
                 score=repo["stars_today"] or 0)
    if item:
        item["published_at"] = None
        item["observed_at"] = now.isoformat().replace(UTC_SUFFIX, "Z")
        item["time_basis"] = "trending_observation"
        item["programming_language"] = repo["language"]
        item["stars"] = repo["stars"]
        item["stars_today"] = repo["stars_today"]
        item["full_text_available"] = False
    return item


def _collect_github_notable_fallback(source: dict, source_id: str, now: datetime,
                                     fetch, limit: int, max_bytes: int,
                                     max_chars: int, window: int,
                                     issues: list[dict]) -> list[dict]:
    fallback = source["fallback"]
    query = (f'{fallback.get("query", "topic:ai")} '
             f'pushed:>={now.date() - timedelta(hours=window)} '
             f'stars:>={fallback.get("min_stars", 500)}')
    url = GITHUB_API_URL + "/search/repositories?" + urlencode(
        {"q": query, "sort": "stars", "per_page": limit}
    )
    items = []
    for repo in _json(fetch, url, max_bytes).get("items", [])[:limit]:
        pushed_at = _date(repo.get("pushed_at"))
        description = repo.get("description", "")
        if pushed_at:
            description += (". " if description else "") + "Last pushed: " + pushed_at.isoformat().replace(UTC_SUFFIX, "Z")
        item = _item(repo.get("full_name", ""), repo.get("html_url", ""),
                     pushed_at, description, source_id, now, window, max_chars,
                     score=int(repo.get("stargazers_count", 0)))
        if not item:
            continue  # pragma: no cover
        item["published_at"] = None
        item["observed_at"] = now.isoformat().replace(UTC_SUFFIX, "Z")
        item["last_pushed_at"] = pushed_at.isoformat().replace(UTC_SUFFIX, "Z") if pushed_at else None
        item["time_basis"] = "repository_update"
        item["evidence_kind"] = "repository_metadata"
        item["full_text_available"] = False
        items.append(item)
    issues.append({"id": source_id, "kind": "degraded",
                   "reason": "GitHub Trending unavailable; used search fallback"})
    return items


def _collect_github_trending(source: dict, source_id: str, now: datetime, fetch,
                             limit: int, max_bytes: int, max_chars: int, window: int,
                             issues: list[dict]) -> list[dict]:
    query = {"since": source.get("since", "daily")}
    if source.get("topic"):
        query["topic"] = source["topic"]
    url = "https://github.com/trending?" + urlencode(query)
    html = fetch(url, max_bytes).decode("utf-8", "replace")
    items = [item for item in (
        _github_trending_item(repo, source_id, now, window, max_chars)
        for repo in _trending_articles(html)[:limit]
    ) if item]
    fallback = source.get("fallback", {})
    if items or fallback.get("type") != "github_notable":
        return items
    return _collect_github_notable_fallback(source, source_id, now, fetch, limit,
                                            max_bytes, max_chars, window, issues)


def _curated_projects(markdown: str) -> dict[str, dict]:  # noqa: S3776  # noqa: S3776
    """Parse categorized GitHub project links from a curated-list README."""
    projects = {}
    category = "Projects"
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    for line in markdown.splitlines():
        heading = _markdown_heading(line)
        if heading:
            category = heading
            continue
        for match in link_pattern.finditer(line):
            title = plain(match.group(1))
            url = match.group(2).rstrip(".,;:")
            parsed = urlsplit(url)
            path = [part for part in parsed.path.split("/") if part]
            if (parsed.hostname != "github.com" or len(path) != 2
                    or path[0].lower() in {"topics", "collections", "orgs", "sponsors"}):
                continue
            try:
                project_url = canonical_url("https://github.com/" + "/".join(path))
            except ValueError:  # pragma: no cover
                continue  # pragma: no cover
            cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
            target_url = url
            cleaned = link_pattern.sub(
                lambda link, target=target_url: "" if link.group(2) == target else link.group(1),
                cleaned,
            )
            cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned)
            description = plain(re.sub(r"[`*_~|]", " ", cleaned))
            description = re.sub(r"\s+", " ", description).strip(" -:")
            if not title or title.lower() in {"readme", "source", "here"}:
                continue  # pragma: no cover
            projects[project_url] = {
                "title": title[:300], "url": project_url,
                "category": category[:160], "description": description[:1200],
            }
    return projects


def _curated_developments(markdown: str) -> dict[str, dict]:
    """Parse dated source-backed rows under a Recent Developments section."""
    updates = {}
    in_section = False
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    for line in markdown.splitlines():
        heading = _markdown_heading(line)
        if heading:
            in_section = "recent developments" in heading.lower()
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        event_date = cells[0]
        if _date(event_date) is None:
            continue  # pragma: no cover
        summary = cells[1]
        source_match = link_pattern.search(cells[2])
        if not summary or not source_match:
            continue  # pragma: no cover
        source_url = canonical_url(source_match.group(2).rstrip(".,;:"))
        clean_summary = plain(link_pattern.sub(lambda match: match.group(1), summary))
        clean_summary = re.sub(r"[`*_~]", "", clean_summary).strip()
        title = re.split(r"(?<=[.!?])\s+|:\s+", clean_summary, maxsplit=1)[0].strip(" .:")
        identity = source_url
        updates[identity] = {
            "title": title[:300] or clean_summary[:300], "url": source_url,
            "event_date": event_date, "summary": clean_summary[:1200],
        }
    return updates


def _curated_commit_pair(commits: object) -> tuple[str, str] | None:
    if not isinstance(commits, list):
        raise ValueError("invalid curated-list commit response")  # pragma: no cover
    if not commits:
        return None
    latest = commits[0].get("sha") if isinstance(commits[0], dict) else None
    oldest = commits[-1] if isinstance(commits[-1], dict) else {}
    parents = oldest.get("parents") or []
    previous = parents[0].get("sha") if parents and isinstance(parents[0], dict) else None
    valid_shas = all(isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                     for sha in (latest, previous))
    if not valid_shas:
        raise ValueError("curated-list commit history has no comparable parent")  # pragma: no cover
    return latest, previous


def _curated_readme_versions(repository: str, shas: tuple[str, str], fetch,
                             max_bytes: int) -> tuple[str, str, str]:
    latest, previous = shas
    raw_base = f"https://raw.githubusercontent.com/{repository}/"
    current = fetch(raw_base + latest + "/README.md", max_bytes).decode("utf-8", "replace")
    baseline = fetch(raw_base + previous + "/README.md", max_bytes).decode("utf-8", "replace")
    source_url = f"https://github.com/{repository}/blob/{latest}/README.md"
    return current, baseline, source_url


def _mark_curated_item(item: dict, now: datetime, readme_url: str, *,
                       category: str, subcategory: str, evidence_kind: str,
                       change_type: str, event_date: str | None = None) -> dict:
    item["published_at"] = None
    item["observed_at"] = now.isoformat().replace(UTC_SUFFIX, "Z")
    item["time_basis"] = "curation_window"
    item["evidence_kind"] = evidence_kind
    item["change_type"] = change_type
    item["category"] = category
    item["subcategory"] = subcategory
    item["urls"] = [item["url"], canonical_url(readme_url)]
    item["full_text_available"] = False
    if event_date:
        item["event_date"] = event_date
    return item


def _curated_update_items(current: dict, baseline: dict, source_id: str,
                          now: datetime, readme_url: str, max_chars: int,
                          window: int) -> list[dict]:
    items = []
    for identity, update in current.items():
        previous = baseline.get(identity)
        if previous == update:
            continue  # pragma: no cover
        evidence = f"Recent Developments entry dated {update['event_date']}: {update['summary']}"
        item = _item(update["title"], update["url"], now, evidence,
                     source_id, now, window, max_chars)
        if item:
            change = "added" if previous is None else "updated"
            items.append(_mark_curated_item(
                item, now, readme_url, category="release",
                subcategory="Recent Developments", evidence_kind="curated_update",
                change_type=change, event_date=update["event_date"],
            ))
    return items


def _curated_project_items(current: dict, baseline: dict, source: dict,
                           source_id: str, now: datetime, readme_url: str,
                           max_chars: int, window: int) -> list[dict]:
    items = []
    for url, project in current.items():
        previous = baseline.get(url)
        if previous == project:
            continue  # pragma: no cover
        change_type = "added" if previous is None else "updated"
        evidence = f"{change_type.title()} in curated list under {project['category']}."
        if project["description"]:
            evidence += " " + project["description"]
        item = _item(project["title"], url, now, evidence,
                     source_id, now, window, max_chars)
        if item:
            items.append(_mark_curated_item(
                item, now, readme_url, category=source.get("category", "project"),
                subcategory=project["category"], evidence_kind="curated_project",
                change_type=change_type,
            ))
    return items


def _collect_curated_readme(source: dict, source_id: str, now: datetime, fetch,
                            limit: int, max_bytes: int, max_chars: int, window: int,
                            issues: list[dict]) -> list[dict]:
    repository = source.get("repository", "")
    repository_parts = repository.split("/") if isinstance(repository, str) else []
    valid_repository = (
        len(repository_parts) == 2
        and all(part not in {".", ".."}
                and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                for part in repository_parts)
    )
    if not valid_repository:
        raise ValueError("invalid curated-list repository")
    cutoff = (now - timedelta(hours=window)).isoformat().replace(UTC_SUFFIX, "Z")
    commits_url = f"{GITHUB_API_URL}/repos/{repository}/commits?" + urlencode(
        {"path": "README.md", "since": cutoff, "per_page": 100}
    )
    commits = _json(fetch, commits_url, max_bytes)
    shas = _curated_commit_pair(commits)
    if shas is None:
        return []
    current_markdown, baseline_markdown, readme_url = _curated_readme_versions(
        repository, shas, fetch, max_bytes
    )
    current_projects = _curated_projects(current_markdown)
    baseline_projects = _curated_projects(baseline_markdown)
    current_updates = _curated_developments(current_markdown)
    baseline_updates = _curated_developments(baseline_markdown)
    items = _curated_update_items(current_updates, baseline_updates, source_id,
                                  now, readme_url, max_chars, window)
    items.extend(_curated_project_items(current_projects, baseline_projects, source,
                                        source_id, now, readme_url, max_chars, window))
    if len(commits) >= 100:
        issues.append({"id": source_id, "kind": "degraded",  # pragma: no cover
                       "reason": "GitHub commit history reached the response cap; changes may be incomplete"})
    if len(items) > limit:
        issues.append({"id": source_id, "kind": "degraded",  # pragma: no cover
                       "reason": f"curated-list changes truncated to source max_items ({limit})"})
    return items[:limit]


def _collect_searxng(source: dict, source_id: str, now: datetime, fetch, limit: int, max_bytes: int, max_chars: int, window: int, issues: list[dict]) -> list[dict]:
    items = []
    endpoint = os.environ.get("AI_DIGEST_SEARCH_URL") or os.environ.get("SEARXNG_URL", "")
    if not endpoint:
        issues.append({"id": source_id, "kind": "unavailable", "reason": "search endpoint is not configured"})
        return items
    # Only an explicit loopback SearXNG endpoint is allowed to reach the private
    # local service. Search-result URLs and redirects still require public IPs.
    allow_loopback = urlsplit(endpoint).hostname == "127.0.0.1"
    url = endpoint.rstrip("/") + "/search?" + urlencode({"q": source["query"], "format": "json"})
    _public_url(url, allow_loopback=allow_loopback)
    if allow_loopback and getattr(fetch, "supports_loopback_search", False):
        results = json.loads(fetch(url, max_bytes, allow_loopback=True)).get("results", [])
    else:
        results = _json(fetch, url, max_bytes).get("results", [])
    for result in results[:limit]:
        item = _item(result.get("title", ""), result.get("url", ""),
                     _date(result.get("publishedDate") or result.get("published_at")),
                     result.get("content", ""), source_id, now, window, max_chars)
        if item:
            item["full_text_available"] = False
            items.append(item)
    return items


SOURCE_COLLECTORS = {
    "rss": _collect_rss,
    "arxiv": _collect_arxiv,
    "hf_papers": _collect_hf_papers,
    "hf_trending": _collect_hf_trending,
    "swebench": _collect_swebench,
    "hackernews": _collect_hackernews,
    "reddit": _collect_reddit,
    "github_trending": _collect_github_trending,
    "curated_readme": _collect_curated_readme,
    "searxng": _collect_searxng,
}


def _source(source: dict, defaults: dict, now: datetime, fetch) -> tuple[list[dict], list[dict]]:
    source_id = source["id"]
    kind = source["type"]
    limit = int(source.get("max_items", defaults.get("max_items", 25)))
    max_bytes = int(defaults.get("max_response_bytes", 5_242_880))
    max_chars = int(defaults.get("max_summary_chars", 1200))
    window = int(defaults.get("window_hours", 24))
    issues = []
    collector = SOURCE_COLLECTORS.get(kind)
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
        _merge_duplicate_metadata(match, item)
    for key in ("comment_ids", "reddit_post_id", "max_comments", "discussion_url"):
        if key in item and key not in match:
            match[key] = item[key]


def _merge_duplicate_metadata(match: dict, item: dict) -> None:
    for key in ("published_at", "listed_at", "observed_at", "last_pushed_at"):
        if item.get(key) is not None:
            match[key] = item[key]
    if item.get("published_at") or item.get("listed_at") or not (
            match.get("published_at") or match.get("listed_at")):
        if "time_basis" in item:
            match["time_basis"] = item["time_basis"]
        else:
            match.pop("time_basis", None)
    for key in ("evidence_kind", "category"):
        if key in item:
            match[key] = item[key]


def _importance(item: dict, now: datetime, window_hours: int) -> float:
    curated_at = item.get("observed_at") if item.get("time_basis") == "curation_window" else None
    published = _date(item.get("listed_at") or item.get("published_at") or curated_at)
    age_hours = max(0, (now - published).total_seconds() / 3600) if published else window_hours
    freshness = 10 * max(0, 1 - age_hours / window_hours)
    popularity = 2 * min(6, math.log1p(max(0, item["score"])))
    discussion = 2 * min(6, math.log1p(max(0, item["discussion_count"])))
    curated_update = 5 if item.get("evidence_kind") == "curated_update" else 0
    return freshness + popularity + discussion + curated_update + 10 * (len(item["source_ids"]) - 1)


def _select_items(items: list[dict], limit: int, mode: str, now: datetime,  # noqa: S3776
                  window_hours: int, *, history: list[dict] | None = None) -> list[dict]:
    from feedback import preference_bonus

    bonuses = {id(item): preference_bonus(item, history or []) for item in items}
    ranked = sorted(items, key=lambda item: (
        -(_importance(item, now, window_hours) + bonuses[id(item)]),
        item["title"]))
    selected, counts = [], {}
    max_per_source = max(1, math.ceil(limit / 3))
    reserve_exploration = bool(history) and limit > 1
    main_limit = limit - int(reserve_exploration)
    if mode == "weekly":
        _select_weekly_categories(ranked, selected, counts, main_limit, max_per_source)
    for item in ranked:
        if len(selected) >= main_limit:
            break
        if item in selected:
            continue
        primary = item["source_ids"][0]
        if not _source_capacity_available(item, ranked, selected, counts, max_per_source):
            continue
        selected.append(item)
        counts[primary] = counts.get(primary, 0) + 1
    if reserve_exploration:
        by_base = sorted(items, key=lambda item: (-_importance(item, now, window_hours),
                                                  item["title"]))
        for candidates in ([item for item in by_base if abs(bonuses[id(item)]) < 0.5], by_base):
            candidate = next((item for item in candidates if item not in selected
                              and _source_capacity_available(item, ranked, selected, counts,
                                                             max_per_source)), None)
            if candidate is not None:
                selected.append(candidate)
                primary = candidate["source_ids"][0]
                counts[primary] = counts.get(primary, 0) + 1
                break
    for item in ranked:
        if len(selected) >= limit:
            break
        if item not in selected and _source_capacity_available(
                item, ranked, selected, counts, max_per_source):
            selected.append(item)
            primary = item["source_ids"][0]
            counts[primary] = counts.get(primary, 0) + 1
    return selected


def _select_weekly_categories(ranked: list[dict], selected: list[dict],
                              counts: dict[str, int], limit: int,
                              max_per_source: int) -> None:
    for category in ("research", "podcast", "benchmark"):
        candidate = next((item for item in ranked
                          if item.get("category") == category and item not in selected
                          and _source_capacity_available(item, ranked, selected, counts,
                                                          max_per_source)), None)
        if candidate is not None and candidate not in selected and len(selected) < limit:
            selected.append(candidate)
            primary = candidate["source_ids"][0]
            counts[primary] = counts.get(primary, 0) + 1


def _source_capacity_available(item: dict, ranked: list[dict], selected: list[dict],
                               counts: dict[str, int], max_per_source: int) -> bool:
    primary = item["source_ids"][0]
    return counts.get(primary, 0) < max_per_source or not any(
        counts.get(other["source_ids"][0], 0) < max_per_source
        for other in ranked if other not in selected)


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
            except (ValueError, KeyError, TypeError, AttributeError, IndexError,
                    OverflowError, ET.ParseError, OSError) as exc:  # noqa: S5713
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
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:  # noqa: S5713
        item["discussion_issue"] = f"Reddit comments unavailable: {str(exc)[:120]}"


def _read_article(item: dict, defaults: dict, fetch) -> None:  # noqa: S3776
    parsed = urlsplit(item["url"])
    if parsed.hostname == "github.com":
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
            try:
                url = f"{GITHUB_API_URL}/repos/{parts[0]}/{parts[1]}/readme"
                response = _json(fetch, url, min(int(defaults.get("max_response_bytes", 5_242_880)), 2_000_000))
                if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
                    raise ValueError("repository README has no readable content")
                encoded = re.sub(r"\s+", "", response["content"])
                readme = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
                readme = re.sub(r"```.*?```", " ", readme, flags=re.S)
                readme = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", readme)
                readme = plain(readme)
                if len(readme) < 40:
                    raise ValueError("repository README is too short")
                item["evidence"] = (item.get("evidence", "") + "\nRepository README excerpt: " + readme)[
                    :int(defaults.get("max_article_chars", 4500))]
                item["evidence_kind"] = "repository_readme"
            except (OSError, ValueError, TypeError, AttributeError, binascii.Error) as exc:  # noqa: S5713
                item["read_issue"] = f"repository README unavailable: {str(exc)[:100]}"
        return
    if (item["full_text_available"] or item.get("evidence_kind") in
            {"abstract", "benchmark_result", "model_metadata", "show_notes", "reddit_search_snippet"}):
        return
    try:
        body = fetch(item["url"], min(int(defaults.get("max_response_bytes", 5_242_880)), 2_000_000))
        if body.startswith(b"%PDF"):
            raise ValueError("PDF article is not supported")
        html = body.decode("utf-8", "replace")
        article, region_found = article_plain(html)
        article = article[:int(defaults.get("max_article_chars", 4500))]
        if len(article) >= 400 and region_found:
            item["evidence"] = article
            item["full_text_available"] = True
        elif len(article) >= 400 and re.search(r"<body\b", html, re.I):
            item["evidence"] = article
            item["evidence_kind"] = "page_text"
        else:
            item["read_issue"] = "article body unavailable or too short"
    except (OSError, ValueError) as exc:  # noqa: S5713
        item["read_issue"] = str(exc)[:120]


def collect(config: dict, *, now: datetime | None = None, fetch=None,  # noqa: S3776
            window_hours: int | None = None, limit: int | None = None, topic: str = "",
            mode: str = "daily", history: list[dict] | None = None,
            seen_items: list[dict] | None = None) -> dict:  # noqa: S3776
    if fetch is None:
        fetch = http_fetch
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
        def fetch(url: str, max_bytes: int, *, allow_loopback: bool = False) -> bytes:
            return http_fetch(url, max_bytes, timeout=timeout, allow_loopback=allow_loopback)
        fetch.supports_loopback_search = True
    sources = [source for source in config["sources"] if mode in source.get("modes", ["daily"])]
    raw, issues = _collect_sources(sources, defaults, now, fetch)
    in_window = len(raw)
    if topic:
        words = _topic_words(topic)
        raw = [item for item in raw if words & _topic_words(item["title"] + " " + item["evidence"])]
    merged = _deduplicate(raw)
    previously_sent = seen_items or []
    sent_urls = {item["url"] for item in previously_sent}
    fresh = [item for item in merged
             if not sent_urls.intersection(item.get("urls", [item["url"]]))
             and _duplicate_match(item, previously_sent) is None]
    selected = _select_items(fresh, limit, mode, now, defaults["window_hours"], history=history)
    _hydrate_items(selected, defaults, fetch)
    return {"schema_version": 1, "mode": mode, "generated_at": now.isoformat().replace(UTC_SUFFIX, "Z"),
            "window_hours": defaults["window_hours"], "limit": limit, "topic": topic or None,
            "items": selected, "source_issues": sorted(issues, key=lambda issue: issue["id"]),
            "stats": {"fetched": in_window, "after_dedup": len(merged),
                      "repeated_excluded": len(merged) - len(fresh), "returned": len(selected)}}


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
        from feedback import FeedbackStore, owner_for_digest_jobs
        import sqlite3

        hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        owner = owner_for_digest_jobs(hermes_home / "cron" / "jobs.json")
        feedback_path = args.state_dir / "feedback.db"
        run_now = datetime.now(timezone.utc)
        cooldown_hours = int(config.get("defaults", {}).get("repeat_cooldown_hours", 168))
        if not 1 <= cooldown_hours <= 720:
            raise ValueError("repeat cooldown outside supported range")
        history, seen_items = [], []
        feedback_issue = None
        if owner and feedback_path.is_file():
            try:
                store = FeedbackStore(feedback_path)
                history = store.history(owner)
                seen_items = store.recently_sent_items(
                    owner, (run_now - timedelta(hours=cooldown_hours)).isoformat())
            except (sqlite3.DatabaseError, OSError, ValueError):
                feedback_issue = {"id": "feedback", "kind": "degraded",
                                  "reason": "stored ratings or delivery history unavailable; used base ranking"}
        result = collect(config, now=run_now, window_hours=args.window_hours, limit=args.limit,
                         topic=args.topic, mode=args.mode, history=history,
                         seen_items=seen_items)
        if feedback_issue:
            result["source_issues"].append(feedback_issue)
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
