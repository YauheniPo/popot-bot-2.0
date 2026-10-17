"""Behavior checks against the pinned extraction pipeline, without network calls."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock


RUNTIME = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('quality_patches', RUNTIME / 'apply-hermes-patches.py')
PATCHES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHES)
UPSTREAM = os.environ.get('HERMES_UPSTREAM_DIR')
A, B, C = ('https://example.com/' + name for name in ('article', 'limited', 'missing'))
CONSENT = (
    '![Revisit consent button](https://cdn-cookieyes.com/revisit.svg)\n\n'
    'We value your privacy\n\nWe use cookies to enhance your browsing experience.\n\n'
    'CustomiseReject AllAccept All\n\nCustomise Consent Preferences\n\n'
    + ('Necessary cookies support site functions and consent preferences.\n\n' * 20)
    + 'Reject AllSave My PreferencesAccept All\n\n'
    'Powered by [Visit CookieYes website](https://www.cookieyes.com/product/cookie-consent/)\n\n'
)
LANDING = '# HEALTHY LIFESTYLE FOR EVERYONE\n\nGet in shape. Stay healthy.\n\n[Get Started](https://example.com/join)'
ARTICLE = '# A new training programme\n\n' + (
    'The company launched an eight week strength course for its members on October 17. '
    'The course includes guided sessions and an updated training plan.\n\n'
) * 4


@unittest.skipUnless(UPSTREAM, 'HERMES_UPSTREAM_DIR is not configured')
class WebExtractQualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache_root = Path(self.directory.name)
        self.rescue = mock.Mock()
        self.eligible = mock.Mock(return_value=True)
        modules = {
            'tools': ModuleType('tools'),
            'tools.tool_backend_helpers': SimpleNamespace(selection_error=mock.Mock(), selection_exists=mock.Mock()),
            'tools.url_safety': SimpleNamespace(normalize_url_for_request=lambda url: url),
            'tools.web_tools_rescue': SimpleNamespace(_rescue_eligible=self.eligible, _rescue_extract=self.rescue),
            'tools.web_tools': SimpleNamespace(_load_web_config=lambda: {}),
            'tools.website_policy': SimpleNamespace(check_website_access=lambda url: None),
            'tools.spill_safety': SimpleNamespace(write_text_exclusive=lambda path, text, **kw: path.write_text(text)),
            'utils': SimpleNamespace(atomic_json_write=lambda path, data, **kw: path.write_text(json.dumps(data))),
            'plugins.web.keyless_mcp': SimpleNamespace(
                _is_rate_limitish=lambda message: any(word in message.lower() for word in ('rate limit', '429'))),
        }
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.extract = self.load_module('tools.web_tools_extract', 'tools/web_tools_extract.py')
        self.trim = self.load_module('tools.web_tools_truncate', 'tools/web_tools_truncate.py')
        self.cache = self.load_module('tools.web_result_cache', 'tools/web_result_cache.py')
        self.cache._cache_dir = lambda: self.cache_root

    def load_module(self, name, path):
        source = (Path(UPSTREAM) / path).read_text()
        for target, marker, old, new in PATCHES._PATCHES:
            if target == path:
                self.assertIn(old, source, marker)
                source = source.replace(old, new, 1)
        module = ModuleType(name)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, path, 'exec'), module.__dict__)
        return module

    def provider(self, results):
        return SimpleNamespace(name='firecrawl', extract=mock.AsyncMock(return_value=results))

    def run_extract(self, provider, urls):
        return asyncio.run(self.extract._extract_safe_urls(provider, urls, 'markdown'))

    def test_mixed_batch_rescues_only_rate_limited_url_and_does_not_cache_rescue(self):
        good = {'url': A, 'content': ARTICLE, 'error': None}
        limited = {'url': B, 'content': '', 'error': 'Rate Limit Exceeded: retry after 15s'}
        missing = {'url': C, 'content': '', 'error': 'HTTP 404'}
        rescued = {'url': B, 'content': 'Recovered article', 'error': None,
                   'metadata': {'rescued_from': 'firecrawl'}}
        provider = self.provider([good, limited, missing])
        self.rescue.return_value = [rescued]
        with mock.patch.object(self.cache, 'extract_cache_put') as put:
            result = self.run_extract(provider, [A, B, C])
        self.assertEqual(result, [good, rescued, missing])
        self.rescue.assert_called_once_with('firecrawl', [B], [limited])
        provider.extract.assert_awaited_once_with([A, B, C], format='markdown')
        self.assertEqual([call.args[0] for call in put.call_args_list], [A])

    def test_rescue_disabled_or_already_keyless_does_not_switch_backend(self):
        self.eligible.return_value = False
        results = [{'url': A, 'content': ARTICLE}, {'url': B, 'error': 'HTTP 429'}]
        self.assertEqual(self.run_extract(self.provider(results), [A, B]), results)
        self.rescue.assert_not_called()

    def test_rescue_never_fetches_policy_blocks_or_unknown_or_ambiguous_urls(self):
        results = [
            {'url': A, 'content': ARTICLE},
            {'url': B, 'error': 'Blocked by website policy: 429.example', 'blocked_by_policy': True},
            {'url': 'https://unknown.example/429', 'error': 'HTTP 429'},
        ]
        self.run_extract(self.provider(results), [A, B, C])
        self.rescue.assert_not_called()
        ambiguous = [{'url': A, 'content': ARTICLE}, {'url': B, 'error': 'HTTP 429'}]
        self.run_extract(self.provider(ambiguous), [A, B, B])
        self.rescue.assert_not_called()

    def test_rescue_must_match_url_before_replacing_failure(self):
        results = [{'url': A, 'content': ARTICLE}, {'url': B, 'error': 'HTTP 429'}]
        self.rescue.return_value = [{'url': C, 'content': 'Wrong article', 'error': None}]
        self.assertEqual(self.run_extract(self.provider(results), [A, B]), results)
        self.rescue.assert_called_once()

    def test_rate_limit_rescue_uses_requested_source_url_for_redirect(self):
        limited = {'url': 'https://redirect.example/', 'metadata': {'sourceURL': B}, 'error': 'HTTP 429'}
        rescued = {'url': B, 'content': ARTICLE, 'error': None, 'metadata': {'rescued_from': 'firecrawl'}}
        self.rescue.return_value = [rescued]
        result = self.run_extract(self.provider([{'url': A, 'content': ARTICLE}, limited]), [A, B])
        self.assertEqual(result[1], rescued)
        self.rescue.assert_called_once_with('firecrawl', [B], [limited])

    def test_multiple_rescues_match_urls_and_keep_missing_failures_in_place(self):
        limited_b = {'url': B, 'error': 'HTTP 429'}
        limited_c = {'url': C, 'error': 'HTTP 429'}
        good = {'url': A, 'content': ARTICLE}
        recovered_b = {'url': B, 'content': 'Article B'}
        recovered_c = {'url': C, 'content': 'Article C'}
        for rescued, expected in (([recovered_c, recovered_b], [good, recovered_b, recovered_c]),
                                  ([recovered_c], [good, limited_b, recovered_c]),
                                  ([limited_b, limited_c], [good, limited_b, limited_c])):
            self.rescue.reset_mock()
            self.rescue.return_value = rescued
            with self.subTest(rescued=rescued), mock.patch.object(self.cache, 'extract_cache_get', return_value=None):
                self.assertEqual(self.run_extract(self.provider([good, limited_b, limited_c]), [A, B, C]), expected)
                self.rescue.assert_called_once_with('firecrawl', [B, C], [limited_b, limited_c])

    def test_native_rescue_annotations_and_consent_checks_reach_the_final_response(self):
        rescue_module = self.load_module('tools.web_tools_rescue', 'tools/web_tools_rescue.py')
        ring = sys.modules['plugins.web.keyless_mcp']
        ring.extract_with_failover = mock.Mock(return_value=[{'url': B, 'content': CONSENT + LANDING}])
        provider = self.provider([{'url': A, 'content': ARTICLE}, {'url': B, 'error': 'HTTP 429'}])
        with mock.patch.object(self.extract, '_rescue_extract', rescue_module._rescue_extract), \
                mock.patch.object(self.cache, 'extract_cache_put') as put:
            results = self.run_extract(provider, [A, B])
        ring.extract_with_failover.assert_called_once_with('firecrawl', [B])
        self.assertIn('consent', results[1]['error'])
        self.assertEqual(results[1]['content'], '')
        self.assertEqual([call.args[0] for call in put.call_args_list], [A])

    def test_whole_batch_rescue_still_works_without_caching_and_checks_page_quality(self):
        self.rescue.return_value = [{'url': A, 'content': CONSENT}]
        with mock.patch.object(self.cache, 'extract_cache_put') as put:
            result = self.run_extract(self.provider([{'url': A, 'error': 'HTTP 503'}]), [A])[0]
        self.assertIn('consent', result['error'])
        self.rescue.assert_called_once()
        put.assert_not_called()

    def test_consent_dominated_page_is_an_explicit_error_and_is_not_cached(self):
        for content in (CONSENT, CONSENT + LANDING):
            with self.subTest(content=content[-100:]), mock.patch.object(self.cache, 'extract_cache_put') as put:
                result = self.run_extract(self.provider([{'url': A, 'content': content, 'raw_content': content}]), [A])[0]
                self.assertIn('consent', result.get('error') or '')
                self.assertIn('browser', result['error'])
                self.assertEqual(result['content'], '')
                self.assertEqual(result['raw_content'], '')
                self.assertNotIn('blocked_by_policy', result)
                put.assert_not_called()

    def test_real_article_with_cookie_banner_or_cookie_subject_is_kept(self):
        for content in (CONSENT + ARTICLE, ARTICLE + CONSENT,
                        '# Cookie consent guide\n\n' + ARTICLE,
                        CONSENT.rstrip() + ' ' + ARTICLE.replace('\n', ' ') + '[Details](https://example.com/news)',
                        'We value your privacy\nAccept All\n' + ARTICLE):
            with self.subTest(content=content[:60]), mock.patch.object(self.cache, 'extract_cache_get', return_value=None):
                result = self.run_extract(self.provider([{'url': A, 'content': content}]), [A])[0]
                self.assertFalse(result.get('error'))
                self.assertEqual(result['content'], content)

    def test_old_cached_consent_stub_is_refetched(self):
        self.cache.extract_cache_put(A, CONSENT + LANDING, provider='firecrawl', format='markdown')
        provider = self.provider([{'url': A, 'content': ARTICLE}])
        result = self.run_extract(provider, [A])[0]
        self.assertEqual(result['content'], ARTICLE)
        provider.extract.assert_awaited_once()

    def test_dates_survive_fresh_and_cached_results_without_arbitrary_metadata(self):
        safe = {'article:published_time': '2026-10-17T05:30:00+00:00', 'dateModified': '2026-10-17'}
        metadata = {**safe, 'headers': {'authorization': 'sensitive-provider-value'}, 'apiKey': 'sensitive-provider-value',
                    'sourceURL': A, 'publishedTime': 'not a date', 'datePublished': ['2026-10-17']}
        provider = self.provider([{'url': A, 'content': ARTICLE, 'metadata': metadata}])
        for call in range(2):
            result = self.trim._trim_results(self.run_extract(provider, [A]))[0]
            self.assertEqual(result.get('metadata'), safe, call)
            self.assertNotIn('sensitive-provider-value', json.dumps(result))
        self.assertNotIn('sensitive-provider-value', json.dumps(self.cache._load_index()))
        provider.extract.assert_awaited_once()

    def test_bad_or_absent_dates_are_omitted_and_cache_namespace_is_preserved(self):
        for metadata in (None, [], {'datePublished': '2026-02-30'}, {'datePublished': '2026-10-17\nprivate'}):
            result = self.trim._trim_results([{'url': A, 'content': ARTICLE, 'metadata': metadata}])[0]
            self.assertNotIn('metadata', result)
        metadata = {'datePublished': '2026-10-17'}
        provider = self.provider([{'url': A, 'content': ARTICLE, 'metadata': metadata}])
        self.run_extract(provider, [A])
        self.assertIsNone(self.cache.extract_cache_get(A, provider='exa', format='markdown'))
        self.assertIsNone(self.cache.extract_cache_get(A, provider='firecrawl', format='html'))

    def test_cancellation_is_propagated_without_rescue(self):
        provider = self.provider([])
        provider.extract.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.run_extract(provider, [A])
        self.rescue.assert_not_called()


QUALITY_SPEC = importlib.util.spec_from_file_location(
    'web_extract_quality', RUNTIME / 'web_extract_quality.py')
QUALITY = importlib.util.module_from_spec(QUALITY_SPEC)
QUALITY_SPEC.loader.exec_module(QUALITY)


class SafeExtractMetadataTests(unittest.TestCase):
    """Only bounded, real ISO dates may survive the metadata scrub."""

    def test_non_dict_metadata_yields_nothing(self):
        for value in (None, [], 'datePublished=2026-10-17', 20261017):
            with self.subTest(value=value):
                self.assertEqual(QUALITY._safe_extract_metadata(value), {})

    def test_keeps_valid_dates_and_drops_everything_else(self):
        metadata = {
            'datePublished': '2026-10-17',                 # date only -> kept
            'dateModified': '2026-10-17T05:30:00Z',        # trailing Z -> kept
            'publishedTime': '2026-10-17T05:30:00+00:00',  # explicit offset -> kept
            'article:published_time': 'not-a-date',        # regex mismatch -> dropped
            'modifiedTime': '2026-02-30',                  # calendar-impossible -> dropped
            'publishedDate': 20261017,                     # not a string -> dropped
            'modifiedDate': 'x' * 65,                      # longer than 64 chars -> dropped
            'published_date': '2026-10-17\nprivate',       # trailing payload -> dropped
            'unknownField': '2026-10-17',                  # not a tracked field -> ignored
        }
        self.assertEqual(QUALITY._safe_extract_metadata(metadata), {
            'datePublished': '2026-10-17',
            'dateModified': '2026-10-17T05:30:00Z',
            'publishedTime': '2026-10-17T05:30:00+00:00',
        })

    def test_value_longer_than_the_limit_is_dropped(self):
        metadata = {'datePublished': '2026-10-17T05:30:00' + '0' * 60}
        self.assertEqual(QUALITY._safe_extract_metadata(metadata), {})


class ConsentDominanceTests(unittest.TestCase):
    """Consent detection needs page size, every marker and the vendor attribution."""

    def test_short_markerless_or_unattributed_pages_are_not_consent_shells(self):
        for result in ({'raw_content': 'We value your privacy'},
                       {'raw_content': 'We value your privacy\n' + 'x' * 1100},
                       {'raw_content': 'We value your privacy consent preferences accept all reject all\n' + 'x' * 1100},
                       {}):
            with self.subTest(result=str(result)[:40]):
                self.assertFalse(QUALITY._consent_dominated_extract(result))

    def test_consent_shell_is_detected_from_raw_or_trimmed_content(self):
        self.assertTrue(QUALITY._consent_dominated_extract({'raw_content': CONSENT}))
        self.assertTrue(QUALITY._consent_dominated_extract({'content': CONSENT}))
        self.assertTrue(QUALITY._consent_dominated_extract({'raw_content': CONSENT + LANDING}))
        # Trailing markdown links and images are stripped before residue is measured.
        self.assertTrue(QUALITY._consent_dominated_extract(
            {'raw_content': CONSENT + '\n![icon](https://example.com/icon.svg)[Read more](https://example.com/more)\n'}))

    def test_real_article_beside_a_consent_banner_is_not_consent_dominated(self):
        for text in (CONSENT + ARTICLE, ARTICLE + CONSENT):
            with self.subTest(text=text[:40]):
                self.assertFalse(QUALITY._consent_dominated_extract({'raw_content': text}))


class MarkIncompleteExtractionsTests(unittest.TestCase):
    def test_failed_results_are_left_alone_and_consent_shells_are_flagged(self):
        failed = {'url': 'https://example.com/failed', 'content': CONSENT,
                  'raw_content': CONSENT, 'error': 'HTTP 500'}
        article = {'url': 'https://example.com/article', 'content': 'Ordinary coverage.'}
        consent = {'url': 'https://example.com/consent', 'content': CONSENT, 'raw_content': CONSENT}
        results = QUALITY._mark_incomplete_extractions([failed, article, consent])
        self.assertIs(results[0], failed)
        self.assertEqual((failed['content'], failed['raw_content'], failed['error']),
                         (CONSENT, CONSENT, 'HTTP 500'))
        self.assertEqual((article['content'], article.get('error')), ('Ordinary coverage.', None))
        self.assertEqual((consent['content'], consent['raw_content']), ('', ''))
        self.assertIn('consent', consent['error'])
        self.assertIn('browser_navigate', consent['error'])


class RequestedExtractUrlTests(unittest.TestCase):
    def setUp(self):
        self.requested = ['https://example.com/article', 'https://example.com/limited']

    def test_result_url_that_was_requested_is_returned(self):
        self.assertEqual(
            QUALITY._requested_extract_url({'url': self.requested[0]}, self.requested), self.requested[0])

    def test_source_url_metadata_is_accepted(self):
        result = {'url': 'https://redirect.example/', 'metadata': {'sourceURL': self.requested[1]}}
        self.assertEqual(QUALITY._requested_extract_url(result, self.requested), self.requested[1])

    def test_unknown_urls_and_non_dict_metadata_yield_none(self):
        self.assertIsNone(QUALITY._requested_extract_url(
            {'url': 'https://unknown.example/', 'metadata': {'sourceURL': 'https://also.example/'}}, self.requested))
        self.assertIsNone(QUALITY._requested_extract_url(
            {'url': 'https://unknown.example/', 'metadata': 'not-a-dict'}, self.requested))
        self.assertEqual(QUALITY._requested_extract_url(
            {'url': 'https://unknown.example/', 'metadata': 'not-a-dict', 'nested': None}, self.requested), None)


class RateLimitedExtractIndicesTests(unittest.TestCase):
    ARTICLE_URL = 'https://example.com/article'
    LIMITED_URL = 'https://example.com/limited'

    def rate_limited_indices(self, results, requested, is_rate_limitish=lambda message: '429' in message):
        fake = ModuleType('plugins.web.keyless_mcp')
        fake._is_rate_limitish = mock.Mock(side_effect=is_rate_limitish)
        with mock.patch.dict(sys.modules, {'plugins.web.keyless_mcp': fake}):
            indices = QUALITY._rate_limited_extract_indices(results, requested)
        return indices, fake._is_rate_limitish

    def test_unique_requested_rate_limit_failure_is_rescued(self):
        results = [{'url': self.ARTICLE_URL, 'content': 'ok'},
                   {'url': self.LIMITED_URL, 'error': 'HTTP 429: rate limit'}]
        indices, called = self.rate_limited_indices(results, [self.ARTICLE_URL, self.LIMITED_URL])
        self.assertEqual(indices, [1])
        called.assert_called_once_with('HTTP 429: rate limit')

    def test_duplicate_requested_urls_are_ambiguous_and_left_alone(self):
        results = [{'url': self.LIMITED_URL, 'error': 'HTTP 429'},
                   {'url': self.LIMITED_URL, 'error': 'HTTP 429'}]
        indices, _ = self.rate_limited_indices(results, [self.LIMITED_URL, self.LIMITED_URL])
        self.assertEqual(indices, [])

    def test_policy_blocks_and_unknown_urls_are_never_rescued(self):
        results = [{'url': self.LIMITED_URL, 'error': 'HTTP 429', 'blocked_by_policy': True},
                   {'url': self.ARTICLE_URL, 'error': 'Blocked by website policy: HTTP 429'},
                   {'url': 'https://unknown.example/', 'error': 'HTTP 429'}]
        indices, _ = self.rate_limited_indices(results, [self.ARTICLE_URL, self.LIMITED_URL])
        self.assertEqual(indices, [])

    def test_non_rate_limit_failures_are_left_alone(self):
        results = [{'url': self.LIMITED_URL, 'error': 'HTTP 404'}]
        indices, called = self.rate_limited_indices(results, [self.LIMITED_URL])
        self.assertEqual(indices, [])
        called.assert_called_once_with('HTTP 404')


if __name__ == '__main__':
    unittest.main()
