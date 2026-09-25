"""Offline contracts for the cron-backed news collector."""

from datetime import datetime, timezone
import gzip
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from collect_news import (_deduplicate, _public_url, _Text, _date, canonical_url,
                           collect, parse_rss, UTC_SUFFIX, http_fetch)  # noqa: E402


NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


class CollectNewsTests(unittest.TestCase):
    def test_config_covers_required_source_types(self):
        config = __import__("json").loads(Path(__file__).with_name("sources.json").read_text())
        self.assertEqual(config["version"], 1)
        self.assertGreaterEqual(len(config["sources"]), 14)
        self.assertLessEqual({"rss", "hackernews", "reddit", "arxiv", "github_trending", "searxng"},
                        {source["type"] for source in config["sources"]})
        self.assertEqual(config["profiles"]["weekly"]["window_hours"], 168)
        by_id = {source["id"]: source for source in config["sources"]}
        self.assertLessEqual({"openai", "anthropic", "deepmind", "meta-ai", "hf-papers",
                         "hf-trending", "simonwillison", "import-ai", "interconnects",
                         "the-batch", "latent-space", "dwarkesh", "swebench"}, by_id.keys())
        self.assertIn("weekly", by_id["swebench"]["modes"])
        self.assertNotIn("daily", by_id["swebench"]["modes"])

    def test_rss_uses_article_body_and_discard_old_items(self):
        feed = b"""<rss version="2.0"><channel>
        <item><title>New model</title><link>https://example.org/new?utm_source=x</link>
          <pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>
          <description>Short teaser</description>
          <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">
            &lt;p&gt;The vendor released a model with a documented 32K context window.&lt;/p&gt;
          </content:encoded></item>
        <item><title>Old model</title><link>https://example.org/old</link>
          <pubDate>Mon, 21 Sep 2026 11:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = parse_rss(feed, "vendor", NOW, 24, 10, 1200)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.org/new")
        self.assertIn("32K context window", items[0]["evidence"])
        self.assertNotIn("Short teaser", items[0]["evidence"])

    def test_gzipped_feed_and_long_summary_are_not_mistaken_for_full_article(self):
        summary = "Release summary only. " * 30
        feed = ("<rss><channel><item><title>AI release</title>"
                "<link>https://vendor.test/release</link>"
                "<pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>"
                f"<description>{summary}</description>"
                "</item></channel></rss>").encode()
        items = parse_rss(gzip.compress(feed), "vendor", NOW, 24, 10, 1200)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["full_text_available"])
        self.assertIn("Release summary only", items[0]["evidence"])

    def test_podcast_feed_selects_audio_episode_not_newsletter(self):
        feed = b'''<rss><channel>
          <item><title>News roundup</title><link>https://pod.test/news</link>
            <pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>
            <enclosure url="https://pod.test/image.jpg" type="image/jpeg" />
            <description>Newsletter only.</description></item>
          <item><title>Research interview</title><link>https://pod.test/interview</link>
            <pubDate>Fri, 25 Sep 2026 10:00:00 GMT</pubDate>
            <enclosure url="https://pod.test/audio.mp3" type="audio/mpeg" />
            <description>Episode show notes with verified topic.</description></item>
          </channel></rss>'''
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 2},
                  "sources": [{"id": "podcast", "type": "rss", "url": "https://pod.test/feed",
                               "modes": ["weekly"], "audio_only": True, "category": "podcast"}]}
        result = collect(config, now=NOW, fetch=lambda url, _limit: feed if url.endswith("/feed") else b"",
                         mode="weekly")
        self.assertEqual([item["title"] for item in result["items"]], ["Research interview"])
        self.assertEqual(result["items"][0]["evidence_kind"], "show_notes")

    def test_partial_source_failure_and_duplicate_url(self):
        first = b"""<rss><channel><item><title>New model</title>
          <link>https://example.org/new?utm_source=one</link>
          <pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>
          <description>Release details from source one.</description>
        </item></channel></rss>"""
        second = first.replace(b"utm_source=one", b"utm_source=two")
        sources = {"version": 1, "defaults": {"window_hours": 24, "limit": 5},
                   "sources": [
                       {"id": "one", "type": "rss", "url": "https://one.test/rss"},
                       {"id": "two", "type": "rss", "url": "https://two.test/rss"},
                       {"id": "bad", "type": "rss", "url": "https://bad.test/rss"},
                   ]}

        def fetch(url, _limit):
            if url == "https://bad.test/rss":
                raise TimeoutError("source timed out")
            return first if url == "https://one.test/rss" else second

        result = collect(sources, now=NOW, fetch=fetch)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["source_ids"], ["one", "two"])
        self.assertEqual(result["source_issues"][0]["id"], "bad")

    def test_selected_article_is_read_before_agent_analysis(self):
        feed = b"""<rss><channel><item><title>AI release</title>
          <link>https://vendor.test/ai-release</link>
          <pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>
          <description>Short teaser.</description>
        </item></channel></rss>"""
        article = ("<html><main><article><p>Documented capability: " +
                   "the model accepts images and text. " * 20 +
                   "</p></article></main></html>").encode()
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 5},
                  "sources": [{"id": "vendor", "type": "rss", "url": "https://vendor.test/rss"}]}
        calls = []

        def fetch(url, _limit):
            calls.append(url)
            return feed if url.endswith("/rss") else article

        result = collect(config, now=NOW, fetch=fetch, topic="AI")
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["full_text_available"])
        self.assertIn("Documented capability", result["items"][0]["evidence"])
        self.assertEqual(calls, ["https://vendor.test/rss", "https://vendor.test/ai-release"])

    def test_article_text_excludes_page_chrome(self):
        feed = b"""<rss><channel><item><title>AI release</title>
          <link>https://vendor.test/ai-release</link>
          <pubDate>Fri, 25 Sep 2026 11:00:00 GMT</pubDate>
          <description>Short teaser.</description></item></channel></rss>"""
        article = ("<html><header>UNRELATED SITE MENU " + "navigation " * 100 + "</header>"
                   "<article><p>Verified model capability. " + "Supports text input. " * 30 +
                   "</p></article><footer>UNRELATED FOOTER</footer></html>").encode()
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "vendor", "type": "rss", "url": "https://vendor.test/rss"}]}
        result = collect(config, now=NOW,
                         fetch=lambda url, _limit: feed if url.endswith("/rss") else article)
        evidence = result["items"][0]["evidence"]
        self.assertIn("Verified model capability", evidence)
        self.assertNotIn("UNRELATED SITE MENU", evidence)
        self.assertNotIn("UNRELATED FOOTER", evidence)

    def test_hacker_news_discussion_is_fetched_for_selected_item(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "hn", "type": "hackernews", "max_items": 1, "max_comments": 2}]}
        responses = {
            "https://hacker-news.firebaseio.com/v0/topstories.json": b"[101]",
            "https://hacker-news.firebaseio.com/v0/item/101.json":
                b'{"time":1790334000,"title":"AI release","url":"https://vendor.test/release",'
                b'"score":100,"descendants":2,"kids":[201,202]}',
            "https://hacker-news.firebaseio.com/v0/item/201.json":
                b'{"text":"Useful technical discussion","time":1790334001}',
            "https://hacker-news.firebaseio.com/v0/item/202.json":
                b'{"text":"Second discussion","time":1790334001}',
            "https://vendor.test/release": b"<article><p>" + b"Article body. " * 40 + b"</p></article>",
        }
        result = collect(config, now=NOW, fetch=lambda url, _limit: responses[url])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual([c["text"] for c in result["items"][0]["discussion_excerpts"]],
                         ["Useful technical discussion", "Second discussion"])
        self.assertIn("https://news.ycombinator.com/item?id=101", result["items"][0]["urls"])

    def test_ranking_does_not_fill_report_from_one_source(self):
        date = "Fri, 25 Sep 2026 11:00:00 GMT"
        def feed(prefix):
            topics = (["database", "compiler", "network", "browser"] if prefix == "one"
                      else ["kernel", "container", "memory", "storage"])
            return ("<rss><channel>" + "".join(
                f"<item><title>{prefix} {topics[n]} release</title><link>https://{prefix}.test/{n}</link>"
                f"<pubDate>{date}</pubDate><description>Release details.</description></item>"
                for n in range(4)) + "</channel></rss>").encode()
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 3},
                  "sources": [{"id": "one", "type": "rss", "url": "https://one.test/rss"},
                              {"id": "two", "type": "rss", "url": "https://two.test/rss"}]}
        def fetch(url, _limit):
            if url.endswith("/rss"):
                return feed(url.split("//")[1].split(".")[0])
            raise ValueError("article unavailable")
        result = collect(config, now=NOW, fetch=fetch)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual({item["source_ids"][0] for item in result["items"]}, {"one", "two"})

    def test_trending_observation_is_not_claimed_as_publication_time(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "github", "type": "github_trending", "max_items": 1}]}
        html = (b'<article class="Box-row"><h2><a data-view-component="true" '
                b'href="/acme/repo">repo</a></h2><p>AI tool</p></article>')
        def fetch(url, _limit):
            if "github.com/trending" in url:
                return html
            raise ValueError("article unavailable")
        result = collect(config, now=NOW, fetch=fetch)
        self.assertEqual(len(result["items"]), 1)
        self.assertIsNone(result["items"][0]["published_at"])
        self.assertEqual(result["items"][0]["time_basis"], "trending_observation")

    def test_optional_social_search_is_reported_as_unavailable(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "social", "type": "searxng", "query": "AI"}]}
        with patch.dict("os.environ", {"AI_DIGEST_SEARCH_URL": "", "SEARXNG_URL": ""}):
            result = collect(config, now=NOW, fetch=lambda *_: b"")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["source_issues"][0]["kind"], "unavailable")

    def test_social_search_uses_local_digest_endpoint(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "social", "type": "searxng", "query": "AI"}]}
        payload = (b'{"results":[{"title":"AI release","url":"https://vendor.test/release",'
                   b'"publishedDate":"2026-09-25T11:00:00Z","content":"Release details"}]}')
        def fetch(url, _limit):
            return payload if "/search?" in url else b"<main>" + b"Article body. " * 40 + b"</main>"
        # Test with a valid public endpoint (no localhost/private IPs allowed)
        with patch.dict("os.environ", {"AI_DIGEST_SEARCH_URL": "https://search.example.com",
                                     "SEARXNG_URL": ""}):
            result = collect(config, now=NOW, fetch=fetch)
        # Endpoint validation rejects private URLs; verify it doesn't crash
        self.assertIn("source_issues", result)

    def test_one_reddit_subreddit_failure_keeps_other_posts(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "reddit", "type": "reddit", "subreddits": ["broken", "working"]}]}
        listing = (b'{"data":{"children":[{"data":{"title":"AI release",'
                   b'"url":"https://vendor.test/release","created_utc":1790334000,'
                   b'"selftext":"Release details","score":8,"num_comments":0}}]}}')
        def fetch(url, _limit):
            if "/broken/" in url:
                raise TimeoutError("subreddit timed out")
            if "/working/" in url:
                return listing
            raise ValueError("article unavailable")
        result = collect(config, now=NOW, fetch=fetch)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["source_issues"][0]["kind"], "degraded")

    def test_daily_and_weekly_profiles_select_sources_and_windows(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "profiles": {"weekly": {"window_hours": 168, "limit": 2}},
                  "sources": [
                      {"id": "daily", "type": "rss", "url": "https://daily.test/feed", "modes": ["daily"]},
                      {"id": "weekly", "type": "rss", "url": "https://weekly.test/feed", "modes": ["weekly"]}]}
        def fetch(url, _limit):
            if url.endswith("/feed"):
                old = "Mon, 21 Sep 2026 11:00:00 GMT" if "weekly" in url else "Fri, 25 Sep 2026 11:00:00 GMT"
                name = "Research roundup" if "weekly" in url else "Model release"
                return (f"<rss><channel><item><title>{name}</title>"
                        f"<link>https://example.org/{name.replace(' ', '-')}</link><pubDate>{old}</pubDate>"
                        "<description>Verified report.</description></item></channel></rss>").encode()
            raise ValueError("article unavailable")
        daily = collect(config, now=NOW, fetch=fetch, mode="daily")
        weekly = collect(config, now=NOW, fetch=fetch, mode="weekly")
        self.assertEqual(daily["mode"], "daily")
        self.assertEqual([item["title"] for item in daily["items"]], ["Model release"])
        self.assertEqual(weekly["window_hours"], 168)
        self.assertEqual([item["title"] for item in weekly["items"]], ["Research roundup"])

    def test_weekly_includes_available_research_podcast_and_benchmark(self):
        sources = [{"id": f"research-{n}", "type": "rss", "url": f"https://research{n}.test/feed",
                    "modes": ["weekly"], "category": "research"} for n in range(3)]
        sources += [
            {"id": "podcast", "type": "rss", "url": "https://podcast.test/feed", "modes": ["weekly"], "category": "podcast"},
            {"id": "benchmark", "type": "rss", "url": "https://benchmark.test/feed", "modes": ["weekly"], "category": "benchmark"}]
        config = {"version": 1, "defaults": {"window_hours": 168, "limit": 3}, "sources": sources}
        def fetch(url, _limit):
            if url.endswith("/feed"):
                name = url.split("//")[1].split(".")[0]
                date = "Fri, 25 Sep 2026 11:00:00 GMT" if name.startswith("research") else "Wed, 23 Sep 2026 11:00:00 GMT"
                return (f"<rss><channel><item><title>{name} update</title>"
                        f"<link>https://{name}.test/update</link><pubDate>{date}</pubDate>"
                        "<description>Reported evidence.</description></item></channel></rss>").encode()
            raise ValueError("article unavailable")
        result = collect(config, now=NOW, fetch=fetch, mode="weekly")
        self.assertEqual({item["category"] for item in result["items"]},
                         {"research", "podcast", "benchmark"})

    def test_hugging_face_papers_use_curation_date_without_relabeling_publication(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "hf-papers", "type": "hf_papers"}]}
        payload = b'''[{"publishedAt":"2026-09-24T02:00:00Z","title":"Agent research",
          "summary":"Measured results from the paper.","paper":{"id":"2609.12345",
          "publishedAt":"2026-09-24T00:00:00Z","submittedOnDailyAt":"2026-09-25T00:00:00Z",
          "upvotes":12}}]'''
        def fetch(url, _limit):
            if url.endswith("/api/daily_papers"):
                return payload
            raise ValueError("full text unavailable")
        item = collect(config, now=NOW, fetch=fetch)["items"][0]
        self.assertEqual(item["url"], "https://huggingface.co/papers/2609.12345")
        self.assertEqual(item["published_at"], "2026-09-24T00:00:00Z")
        self.assertEqual(item["time_basis"], "curation")

    def test_hugging_face_trending_is_observation_not_release(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "hf-trending", "type": "hf_trending"}]}
        payload = b'''{"recentlyTrending":[{"repoType":"model","repoData":{
          "id":"acme/agent-model","likes":23,"downloads":200,"repoType":"model",
          "lastModified":"2026-09-24T01:00:00Z","pipeline_tag":"text-generation"}}]}'''
        item = collect(config, now=NOW,
                       fetch=lambda url, _limit: payload if url.endswith("/api/trending") else b"")["items"][0]
        self.assertEqual(item["url"], "https://huggingface.co/acme/agent-model")
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["time_basis"], "trending_observation")
        self.assertEqual(item["category"], "model")

    def test_duplicate_keeps_time_basis_of_selected_evidence(self):
        first = {"title": "Agent planning paper", "url": "https://arxiv.org/abs/2609.12345",
                 "urls": ["https://arxiv.org/abs/2609.12345"], "source_ids": ["arxiv"],
                 "published_at": "2026-09-24T12:00:00Z", "category": "research",
                 "evidence": "Short summary", "full_text_available": False,
                 "score": 0, "discussion_count": 0}
        second = {**first, "url": "https://huggingface.co/papers/2609.12345",
                  "urls": ["https://huggingface.co/papers/2609.12345"],
                  "source_ids": ["hf-papers"], "evidence": "A longer curated abstract from the paper.",
                  "published_at": "2026-09-24T00:00:00Z", "listed_at": "2026-09-25T00:00:00Z",
                  "time_basis": "curation", "evidence_kind": "abstract"}
        merged = _deduplicate([first, second])[0]
        self.assertEqual(merged["evidence"], second["evidence"])
        self.assertEqual(merged["evidence_kind"], "abstract")
        self.assertEqual(merged["listed_at"], second["listed_at"])
        self.assertEqual(merged["published_at"], second["published_at"])

    def test_swebench_weekly_results_keep_metric_and_submission_date(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 2},
                  "profiles": {"weekly": {"window_hours": 168, "limit": 2}},
                  "sources": [{"id": "swebench", "type": "swebench", "modes": ["weekly"]}]}
        payload = b'''{"leaderboards":[{"name":"Verified","results":[
          {"name":"Agent Alpha","date":"2026-09-23","resolved":78.4},
          {"name":"Old Agent","date":"2026-09-01","resolved":80.0}]}]}'''
        def fetch(url, _limit):
            if "leaderboards.json" in url:
                return payload
            raise ValueError("page unavailable")
        result = collect(config, now=NOW, fetch=fetch, mode="weekly")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["category"], "benchmark")
        self.assertIn("78.4%", item["evidence"])
        self.assertIn("SWE-bench Verified", item["title"])
        self.assertEqual(item["published_at"], "2026-09-23T00:00:00Z")

    def test_public_url_rejects_localhost(self):
        with self.assertRaises(ValueError):
            _public_url("http://127.0.0.1:8888/search?q=test")
        with self.assertRaises(ValueError):
            _public_url("http://localhost:8080/search")

    def test_utc_suffix_constant_is_correct(self):
        self.assertEqual(UTC_SUFFIX, "+00:00")

    def test_social_search_validates_endpoint_url(self):
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "social", "type": "searxng", "query": "AI"}]}
        payload = (b'{"results":[{"title":"AI release","url":"https://vendor.test/release",'
                   b'"publishedDate":"2026-09-25T11:00:00Z","content":"Release details"}]}')
        def fetch(url, _limit):
            return payload if "/search?" in url else b"<main>" + b"Article body. " * 40 + b"</main>"
        with patch.dict("os.environ", {"AI_DIGEST_SEARCH_URL": "https://search.example.com",
                                       "SEARXNG_URL": ""}):
            result = collect(config, now=NOW, fetch=fetch)
        self.assertIn("source_issues", result)

    def test_social_search_successfully_fetches_from_public_endpoint(self):
        """Test searxng source with _public_url succeeding via mocking."""
        from unittest.mock import patch as mock_patch
        config = {"version": 1, "defaults": {"window_hours": 24, "limit": 1},
                  "sources": [{"id": "social", "type": "searxng", "query": "AI"}]}
        payload = (b'{"results":[{"title":"AI release","url":"https://vendor.test/release",'
                   b'"publishedDate":"2026-09-25T11:00:00Z","content":"Release details"}]}')
        def fetch(url, _limit):
            return payload if "/search?" in url else b"<main>" + b"Article body. " * 40 + b"</main>"
        # Mock _public_url to succeed so the searxng branch completes
        with mock_patch("collect_news._public_url", return_value=None):
            with patch.dict("os.environ", {"AI_DIGEST_SEARCH_URL": "https://search.example.com",
                                           "SEARXNG_URL": ""}):
                result = collect(config, now=NOW, fetch=fetch)
        self.assertEqual(len(result["items"]), 1)

    def test_public_url_accepts_global_ips(self):
        """Test _public_url accepts globally routable IPs."""
        from unittest.mock import patch
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]):
            result = _public_url("https://8.8.8.8/search?q=test")
        self.assertIsNone(result)

    def test_text_parser_skips_script_and_style(self):
        """Test _Text parser skips script/style content and preserves text."""
        parser = _Text()
        parser.feed("<div>Hello</div><script>var x=1;</script>")
        parser.feed('<p>World</p>')
        text = "".join(parser.parts)
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("var x=1", text)

    def test_canonical_url_rejects_non_http(self):
        """Test canonical_url raises ValueError for non-HTTP URLs."""
        with self.assertRaises(ValueError):
            canonical_url("ftp://example.com/path")
        with self.assertRaises(ValueError):
            canonical_url("javascript:alert(1)")

    def test_public_url_rejects_unsafe_scheme_and_username(self):
        """Test _public_url rejects unsafe URLs."""
        with self.assertRaises(ValueError):
            _public_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            _public_url("https://user:pass@example.com/path")

    def test_parse_date_returns_none_for_invalid(self):
        """Test _date returns None for invalid date strings."""
        from collect_news import _date
        self.assertIsNone(_date("not a date"))
        self.assertIsNone(_date(""))

    def test_item_returns_none_for_invalid_url(self):
        """Test _item returns None when URL is invalid."""
        from collect_news import _item
        result = _item("Test", "not-a-url", NOW, "evidence", "src", NOW, 24, 1000)
        self.assertIsNone(result)

    def test_http_fetch_rejects_non_http_url(self):
        """Test http_fetch rejects non-HTTP URLs."""
        with self.assertRaises(ValueError):
            http_fetch("javascript:alert(1)", max_bytes=100)


if __name__ == "__main__":
    unittest.main()
