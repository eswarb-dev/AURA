"""Deterministic source, freshness, and relevance checks for news results."""
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, unquote

SOURCES = {
    'reuters.com': 'Reuters', 'ptinews.com': 'PTI', 'thehindu.com': 'The Hindu',
    'indianexpress.com': 'Indian Express', 'timesofindia.indiatimes.com': 'Times of India',
    'hindustantimes.com': 'Hindustan Times', 'ndtv.com': 'NDTV',
    'business-standard.com': 'Business Standard', 'economictimes.indiatimes.com': 'Economic Times',
    'financialexpress.com': 'Financial Express', 'indiatoday.in': 'India Today',
    'livemint.com': 'LiveMint', 'newsonair.gov.in': 'NewsOnAir', 'newsonair.com': 'NewsOnAir',
    'pib.gov.in': 'PIB',
}
IST = timezone(timedelta(hours=5, minutes=30))


def parse_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def has_old_title_date(text, now):
    today = now.astimezone(IST).date()
    normalized = re.sub(r'[-_/]+', ' ', unquote(text).lower())
    for match in re.finditer(r'\b(20\d{2})\s+(\d{1,2})\s+(\d{1,2})\b', normalized):
        try:
            if datetime(*map(int, match.groups())).date() < today:
                return True
        except ValueError:
            pass
    months = 'january february march april may june july august september october november december'.split()
    for month_num, month in enumerate(months, 1):
        pattern = rf'\b{month}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b'
        for match in re.finditer(pattern, normalized):
            try:
                if datetime(int(match[2] or today.year), month_num, int(match[1])).date() < today:
                    return True
            except ValueError:
                pass
    return False


def filter_headlines(items, now=None):
    now = now or datetime.now(timezone.utc)
    stats = dict(time_window='today', results_before_filter=len(items), results_after_filter=0,
                 excluded_school_assembly=0, excluded_old_date=0, excluded_unreliable=0,
                 excluded_missing_date=0, sources_used=[])
    ranked, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = ' '.join(str(item.get('title') or '').split())
        url = str(item.get('url') or '')
        text = re.sub(r'[-_]+', ' ', unquote(title + ' ' + url).lower())
        if re.search(r'school assembly|assembly news|thought of the day|\bquiz\b|student news headlines|education assembly|horoscope', text):
            stats['excluded_school_assembly'] += 1
            continue
        try:
            host = (urlsplit(url).hostname or '').lower()
        except ValueError:
            host = ''
        source = next((name for domain, name in SOURCES.items() if host == domain or host.endswith('.' + domain)), None)
        if not title or not source:
            stats['excluded_unreliable'] += 1
            continue
        published = next((stamp for key in ('publishedAt', 'published_date', 'published_at', 'crawled_at', 'crawled_date') if (stamp := parse_date(item.get(key)))), None)
        updated = next((stamp for key in ('updatedAt', 'updated_at', 'updated_date', 'dateModified') if (stamp := parse_date(item.get(key)))), None)
        updated_today = updated is not None and updated <= now and updated.astimezone(IST).date() == now.astimezone(IST).date()
        if not updated_today and (has_old_title_date(title + ' ' + url, now) or (published is not None and not timedelta(0) <= now - published <= timedelta(hours=24))):
            stats['excluded_old_date'] += 1
            continue
        if not published and not updated_today:
            stats['excluded_missing_date'] += 1
            continue
        identity = re.sub(r'\W+', '', title.lower())
        if identity in seen:
            continue
        seen.add(identity)
        score = 0
        for pattern, weight in [(r'\b(parliament|government|supreme court|cabinet|election|minister)\b', 5),
                                (r'\b(economy|rbi|inflation|trade|budget|business)\b', 4),
                                (r'\b(flood|cyclone|disaster|earthquake|alert)\b', 5),
                                (r'\b(technology|semiconductor|ai)\b', 2),
                                (r'\b(world cup|olympic|final|championship)\b', 1)]:
            if re.search(pattern, title, re.I):
                score += weight
        stamp = updated if updated_today else published
        ranked.append((score, stamp.timestamp(), dict(title=title[:240], source=source, time=stamp.isoformat(), url=url[:500])))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    stats['results_after_filter'] = len(ranked)
    selected = [row[2] for row in ranked[:5]]
    stats['sources_used'] = list(dict.fromkeys(item['source'] for item in selected))
    print(f"[AURA NEWS FILTER] before={len(items)} after={len(ranked)} excluded_school_assembly={stats['excluded_school_assembly']} excluded_old_date={stats['excluded_old_date']}", flush=True)
    return selected, stats
