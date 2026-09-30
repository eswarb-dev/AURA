"""Live news tool with API-key gated providers."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
import urllib.parse
import urllib.error
import urllib.request
from typing import Any

from .tool_types import tool_response
from .news_quality import SOURCES, filter_headlines


def _fetch_candidates(provider, query, timeout, *, region='India', topic='general', fallback=False):
    if provider == 'tavily':
        payload = {'api_key': os.getenv('TAVILY_API_KEY') or '', 'query': query,
                   'search_depth': 'basic', 'max_results': 20, 'topic': 'news',
                   'time_range': 'day', 'include_domains': list(SOURCES)}
        request = urllib.request.Request('https://api.tavily.com/search', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        field = 'results'
    else:
        if not fallback and region.lower() == 'india':
            endpoint = 'top-headlines'
            params = {'country': 'in', 'pageSize': 10}
            if topic in {'business', 'technology', 'sports', 'entertainment', 'science', 'health'}:
                params['category'] = topic
        else:
            endpoint = 'everything'
            params = {'q': 'India OR Delhi OR Modi OR "Supreme Court" OR Parliament OR economy' if region.lower() == 'india' and topic == 'general' else query,
                      'language': 'en', 'sortBy': 'publishedAt', 'pageSize': 10,
                      'from': (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),
                      'domains': ','.join(SOURCES)}
        request = urllib.request.Request('https://newsapi.org/v2/' + endpoint + '?' + urllib.parse.urlencode(params), headers={'X-Api-Key': os.getenv('NEWSAPI_KEY') or ''})
        field = 'articles'
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8', errors='replace'))
    values = data.get(field, [])
    return values if isinstance(values, list) else []


def provider_status() -> dict[str, Any]:
    mode = str(os.getenv("AURA_NEWS_PROVIDER") or "tavily").strip().lower()
    if mode not in {"tavily", "newsapi", "auto"}:
        mode = "tavily"
    tavily_configured = bool(os.getenv("TAVILY_API_KEY"))
    newsapi_configured = bool(os.getenv("NEWSAPI_KEY"))
    if mode == "tavily":
        active_provider = "tavily" if tavily_configured else "none"
    elif mode == "newsapi":
        active_provider = "newsapi" if newsapi_configured else "none"
    else:
        active_provider = "tavily" if tavily_configured else ("newsapi" if newsapi_configured else "none")
    try:
        timeout_seconds = min(4.0, max(0.25, float(os.getenv("AURA_NEWS_TIMEOUT_SECONDS", "4"))))
    except (TypeError, ValueError):
        timeout_seconds = 4.0
    return {
        "enabled": True,
        "provider_mode": mode,
        "tavily_configured": tavily_configured,
        "newsapi_configured": newsapi_configured,
        "active_provider": active_provider,
        "configured": tavily_configured or newsapi_configured,
        "timeout_seconds": timeout_seconds,
    }


def _status_text(status: dict[str, Any]) -> str:
    if status.get("configured"):
        return "Yes, Sir. I have a deterministic News tool route. Tavily is the preferred provider when configured, and NewsAPI can be used as a fallback. Current provider status is available in diagnostics."
    return "News is available as a tool, Sir, but Tavily/NewsAPI keys are not configured yet."


def _select_provider(status: dict[str, Any]) -> str | None:
    active = str(status.get("active_provider") or "none")
    return None if active == "none" else active


def _safe_headlines(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    try:
        max_headlines = min(5, max(1, int(os.getenv("AURA_NEWS_MAX_HEADLINES", "5"))))
    except (TypeError, ValueError):
        max_headlines = 5
    for item in items[:max_headlines]:
        out.append({
            "title": str(item.get("title") or "")[:180],
            "source": str(item.get("source") or item.get("source_name") or "")[:80],
            "time": str(item.get("published_date") or item.get("publishedAt") or "")[:80],
            "url": str(item.get("url") or "")[:220],
        })
    return [x for x in out if x["title"]]


def _news_debug(status: dict[str, Any], *, provider: str, query: str, topic: str | None, region: str | None = None, result_count: int = 0, failure_class: str | None = None, elapsed_ms: int = 0) -> dict[str, Any]:
    return {
        "intent_detected": True,
        "provider_mode": status.get("provider_mode"),
        "active_provider": provider,
        "source": provider if provider not in {"", "none"} else "tavily_or_newsapi",
        "tavily_configured": bool(status.get("tavily_configured")),
        "newsapi_configured": bool(status.get("newsapi_configured")),
        "timeout_seconds": status.get("timeout_seconds", 4.0),
        "query": " ".join(str(query or "").split())[:120],
        "topic": topic or "general",
        "region": region or "India",
        "result_count": result_count,
        "failure_class": failure_class,
        "elapsed_ms": elapsed_ms,
    }


def _safe_error_message(exc: BaseException) -> str:
    return type(exc).__name__


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    status = provider_status()
    if action == "status":
        return tool_response(ok=True, tool="news", action=action, executed=False, result={"provider_status": status}, response_text=_status_text(status), metadata={"news": _news_debug(status, provider=str(status.get("active_provider") or "none"), query="", topic=None)})
    provider = _select_provider(status)
    tavily_key = os.getenv("TAVILY_API_KEY") or ""
    newsapi_key = os.getenv("NEWSAPI_KEY") or ""
    normalized_query = str(params.get("normalized_query") or "").strip()
    requested_topic = str(params.get("topic") or "general").strip() or "general"
    requested_region = str(params.get("region") or "India").strip() or "India"
    used_followup_context = bool(params.get("used_followup_context"))
    if not provider:
        debug = _news_debug(status, provider="none", query=normalized_query or "general news today", topic=requested_topic, region=requested_region, failure_class="news_provider_config_missing")
        debug["used_followup_context"] = used_followup_context
        return tool_response(ok=False, tool="news", action=action, response_text="I could not fetch live news right now, Sir, but the news tool route is active.", error_type="news_provider_config_missing", metadata={"news": debug})
    topic = str(params.get("query") or requested_topic or "").strip()
    if not topic:
        topic = {
            "topic": str(params.get("topic") or "current news").strip() or "current news",
            "headlines": "top current headlines",
            "top_headlines": "top current headlines",
            "ai_news": "latest AI news",
            "tech_news": "latest technology news",
            "india_news": "India top news",
        }.get(action, "top current headlines")
    started = time.perf_counter()
    timeout_seconds = float(status.get("timeout_seconds") or 4.0)
    try:
        query = (f"{requested_region} top news headlines today national politics economy Reuters Indian Express Times of India"
                 if requested_topic == "general" else f"{requested_region} latest {requested_topic} news today")
        candidates = _fetch_candidates(provider, query, timeout_seconds, region=requested_region, topic=requested_topic)
        headlines, quality = filter_headlines(candidates)
        if len(headlines) < 5:
            fallback_query = f"{requested_region} breaking news today latest national headlines" if requested_topic == "general" else f"{requested_region} breaking {requested_topic} news today"
            candidates += _fetch_candidates(provider, fallback_query, timeout_seconds, region=requested_region, topic=requested_topic, fallback=True)
            headlines, quality = filter_headlines(candidates)
        topic = query
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if len(headlines) < 5:
            debug = _news_debug(status, provider=provider, query=normalized_query or topic, topic=requested_topic, region=requested_region, failure_class="news_no_results", elapsed_ms=elapsed_ms)
            debug["used_followup_context"] = used_followup_context
            debug.update(quality)
            return tool_response(ok=False, tool="news", action=action, response_text=f"I could not find enough reliable {requested_region} headlines right now, Sir.", error_type="news_no_results", metadata={"news": debug})
        text = "Here are today's top headlines, Sir: " + " ".join(f"{index}. {headline['title']}" for index, headline in enumerate(headlines[:5], 1))
        debug = _news_debug(status, provider=provider, query=normalized_query or topic, topic=requested_topic, region=requested_region, result_count=len(headlines), elapsed_ms=elapsed_ms)
        debug.update(quality)
        print('[AURA NEWS] source_quality=mainstream', flush=True)
        debug["used_followup_context"] = used_followup_context
        return tool_response(ok=True, tool="news", action=action, executed=True, result={"headlines": headlines, "topic": requested_topic, "region": requested_region, "query": topic, "provider": provider}, response_text=text, metadata={"news": debug})
    except TimeoutError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        debug = _news_debug(status, provider=provider, query=normalized_query or topic, topic=requested_topic, region=requested_region, failure_class="news_provider_timeout", elapsed_ms=elapsed_ms)
        debug["used_followup_context"] = used_followup_context
        return tool_response(ok=False, tool="news", action=action, response_text="I could not fetch live news right now, Sir, but the news tool route is active.", error_type="news_provider_timeout", error_message=_safe_error_message(exc), metadata={"news": debug})
    except urllib.error.URLError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        is_timeout = isinstance(getattr(exc, "reason", None), TimeoutError) or "timed out" in str(exc.reason).lower()
        err = "news_provider_timeout" if is_timeout else "news_provider_error"
        debug = _news_debug(status, provider=provider, query=normalized_query or topic, topic=requested_topic, region=requested_region, failure_class=err, elapsed_ms=elapsed_ms)
        debug["used_followup_context"] = used_followup_context
        return tool_response(ok=False, tool="news", action=action, response_text="I could not fetch live news right now, Sir, but the news tool route is active.", error_type=err, error_message=_safe_error_message(exc), metadata={"news": debug})
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        debug = _news_debug(status, provider=provider, query=normalized_query or topic, topic=requested_topic, region=requested_region, failure_class="news_provider_error", elapsed_ms=elapsed_ms)
        debug["used_followup_context"] = used_followup_context
        return tool_response(ok=False, tool="news", action=action, response_text="I could not fetch live news right now, Sir, but the news tool route is active.", error_type="news_provider_error", error_message=_safe_error_message(exc), metadata={"news": debug})
