"""Small extraction checks embedded into the pinned upstream by managed patches."""

import re
from datetime import datetime


_EXTRACT_DATE_FIELDS = (
    'datePublished', 'dateModified', 'publishedTime', 'modifiedTime',
    'publishedDate', 'modifiedDate', 'published_date', 'modified_date',
    'article:published_time', 'article:modified_time', 'og:updated_time',
)


def _safe_extract_metadata(metadata):
    """Keep only bounded ISO publication/modification dates, never headers or vendor payloads."""
    if not isinstance(metadata, dict):
        return {}
    dates = {}
    for key in _EXTRACT_DATE_FIELDS:
        value = metadata.get(key)
        if not isinstance(value, str) or len(value) > 64:
            continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[Tt ][\d:.+Zz-]+)?', value):
            continue
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            continue
        dates[key] = value
    return dates


def _consent_dominated_extract(result):
    """Recognise the observed CookieYes shell with at most a short landing-page teaser.

    Require both consent controls and the closing vendor attribution; text before/after
    the complete dialog protects ordinary articles that happen to include a banner.
    """
    text = result.get('raw_content') or result.get('content') or ''
    lower = text.lower()
    if len(text) < 1000 or not all(marker in lower for marker in (
            'we value your privacy', 'consent preferences', 'accept all', 'reject all')):
        return False
    endings = list(re.finditer(
        r'Powered by\s*\[Visit CookieYes website\]\(https?://(?:www\.)?cookieyes\.com/[^\s)]*\)',
        text, re.IGNORECASE))
    if not endings:
        return False
    before = text[:lower.index('we value your privacy')]
    outside = before + '\n' + text[endings[-1].end():]
    outside = re.sub(r'!\[[^\]]*\]\([^\n)]*\)', '', outside)
    outside = re.sub(r'\[([^\]]*)\]\([^\n)]*\)', r'\1', outside)
    outside = re.sub(r'\s+', ' ', outside).strip()
    return len(outside) < 160


def _mark_incomplete_extractions(results):
    for result in results:
        if not result.get('error') and _consent_dominated_extract(result):
            result.update(content='', raw_content='', error=(
                'Extraction incomplete: consent UI dominates the page; use browser_navigate '
                'or another public source before treating coverage as complete.'))
    return results


def _requested_extract_url(result, requested):
    meta = result.get('metadata')
    source = meta.get('sourceURL') if isinstance(meta, dict) else None
    return next((url for url in (result.get('url'), source) if url in requested), None)


def _rate_limited_extract_indices(results, requested):
    """Rescue only unambiguous requested URLs; website-policy refusals stay untouched."""
    from plugins.web.keyless_mcp import _is_rate_limitish
    reported = [_requested_extract_url(result, requested) for result in results]
    return [i for i, (result, url) in enumerate(zip(results, reported))
            if url and requested.count(url) == reported.count(url) == 1
            and not result.get('blocked_by_policy')
            and 'blocked by website policy' not in str(result.get('error') or '').lower()
            and result.get('error') and _is_rate_limitish(str(result['error']))]
