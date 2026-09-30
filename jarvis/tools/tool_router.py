"""Deterministic tool detection, routing, and safe audit logging."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import app_launcher_tool, browser_tool, clipboard_tool, coding_tool, file_search_tool, local_command_tool, news_tool, reminder_tool, system_monitor_tool, weather_tool
from .tool_types import blocked, tool_response

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "diagnostics" / "tools" / "tool_audit_log.jsonl"
TOOL_MODULES = {
    "weather": weather_tool,
    "news": news_tool,
    "local_command": local_command_tool,
    "system_monitor": system_monitor_tool,
    "app_launcher": app_launcher_tool,
    "reminder": reminder_tool,
    "file_search": file_search_tool,
    "clipboard": clipboard_tool,
    "browser": browser_tool,
    "coding": coding_tool,
}
TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "weather": {"module": weather_tool, "display_name": "Weather", "actions": ["forecast_today", "forecast_tomorrow", "rain_check", "temperature_now", "status"], "risk_level": "safe", "description": "Provides weather forecasts and current conditions"},
    "news": {"module": news_tool, "display_name": "News", "actions": ["headlines", "topic", "status", "provider_status"], "risk_level": "safe", "description": "Provides news headlines and topic search"},
    "local_command": {"module": local_command_tool, "display_name": "Local Commands", "actions": ["volume_up", "volume_down", "volume_mute", "brightness_info", "lock_laptop", "shutdown_laptop", "restart_laptop", "clean_temp_files", "close_app", "system_status", "open_app", "screenshot", "read_clipboard", "list_reminders", "create_reminder", "battery_status", "cpu_ram_status", "disk_status", "network_status", "process_summary"], "risk_level": "safe", "description": "Local system control commands"},
    "system_monitor": {"module": system_monitor_tool, "display_name": "System Monitor", "actions": ["battery_status", "cpu_ram_status", "disk_status", "network_status", "process_summary"], "risk_level": "safe", "description": "Monitors CPU, RAM, disk, network, and battery status"},
    "app_launcher": {"module": app_launcher_tool, "display_name": "App Launcher", "actions": ["open_app", "open_folder", "open_workspace_preset"], "risk_level": "safe", "description": "Launches allowlisted applications and workspaces"},
    "reminder": {"module": reminder_tool, "display_name": "Reminder", "actions": ["create_reminder", "list_reminders", "delete_reminder"], "risk_level": "safe", "description": "Creates and manages local JSON reminders"},
    "file_search": {"module": file_search_tool, "display_name": "File Search", "actions": ["find_file", "recent_files", "summarize_text_file", "open_file_location"], "risk_level": "safe", "description": "Searches allowlisted local file roots"},
    "clipboard": {"module": clipboard_tool, "display_name": "Clipboard", "actions": ["read_clipboard", "copy_text", "take_screenshot", "screenshot_status"], "risk_level": "safe", "description": "Manages clipboard content and screenshots"},
    "browser": {"module": browser_tool, "display_name": "Browser", "actions": ["browser_open", "browser_search", "browser_new_window", "browser_click", "browser_type", "browser_back", "browser_forward", "browser_reload", "browser_read", "browser_scroll", "browser_followup", "new_tab", "new_window", "open_site", "web_search"], "risk_level": "safe", "description": "Controls the Chrome browser via MCP or legacy mode"},
    "coding": {"module": coding_tool, "display_name": "Coding", "actions": ["git_status", "git_branch", "list_project_files", "open_project_in_vscode", "npm_build_status_command_preview", "python_compile_command_preview"], "risk_level": "safe", "description": "Coding helpers for git, project files, and build commands"},
}


def get_tool_registry() -> dict[str, dict[str, Any]]:
    return TOOL_REGISTRY


def print_tool_registry() -> str:
    lines = ["[AURA TOOL REGISTRY]", "=" * 40]
    for name, meta in TOOL_REGISTRY.items():
        actions = ", ".join(meta["actions"])
        lines.append(f"  {name} ({meta['display_name']}): risk={meta['risk_level']}, actions=[{actions}]")
    lines.append("=" * 40)
    return "\n".join(lines)


_LAST_LOCAL_ACTION: dict[str, Any] | None = None


def _clean(text: str) -> str:
    cleaned = (text or "").lower().replace("?", " ").replace(".", " ").replace("'", "")
    cleaned = cleaned.replace("systme", "system")
    cleaned = cleaned.replace("what's", "whats")
    return " ".join(cleaned.split())


def normalize_tool_command_text(text: str) -> str:
    """Normalize safe, deterministic tool-command STT mistakes."""
    norm = _clean(text)
    norm = norm.replace("crhome", "chrome").replace("crome", "chrome")
    norm = norm.replace("newsai", "newsapi").replace("newz", "news")
    norm = norm.replace("intresting", "interesting")
    norm = norm.replace("hedlines", "headlines").replace("head lines", "headlines")
    norm = norm.replace("tamilnadu", "tamil nadu")
    norm = norm.replace("todays", "today")
    words = ["chrome" if word == "chrom" else word for word in norm.split()]
    norm = " ".join(words)
    browser_context = any(word in norm.split() for word in ("chrome", "browser")) or "new tab" in norm or "new window" in norm
    if browser_context:
        words = ["tab" if word in {"tba", "tap", "tb"} else word for word in norm.split()]
        norm = " ".join(words)
    return " ".join(norm.split())


def _weather_location_from_text(norm: str) -> str | None:
    return weather_tool.extract_weather_location(norm)


def _news_region_from_text(norm: str) -> str:
    region_map = {
        "Coimbatore, Tamil Nadu, India": ("coimbatore",),
        "Tamil Nadu, India": ("tamil nadu",),
        "India": ("india",),
        "Chennai, Tamil Nadu, India": ("chennai",),
    }
    for region, phrases in region_map.items():
        if any(phrase in norm for phrase in phrases):
            return region
    return ""


def _news_topic_from_text(norm: str) -> str:
    topic_map = {
        "technology": ("technology", "tech", "ai", "artificial intelligence"),
        "politics": ("political", "politics", "election"),
        "sports": ("sports", "cricket", "football"),
        "entertainment": ("cinema", "movie", "movies", "entertainment"),
        "business": ("business", "market", "markets", "finance"),
        "education": ("education", "exam", "exams"),
        "international": ("international news", "world news", "global news"),
        "india": ("india", "indian"),
    }
    for topic, phrases in topic_map.items():
        if any(phrase in norm for phrase in phrases):
            return topic
    if "news about " in norm:
        return norm.split("news about ", 1)[1].strip()
    return "general"


def _news_provider_query(topic: str, region: str, norm: str) -> str:
    topic_label = {
        "politics": "political",
        "technology": "technology",
        "entertainment": "entertainment",
        "international": "international",
        "sports": "sports",
        "business": "business",
        "education": "education",
    }.get(topic, topic or "general")
    region_label = {
        "Tamil Nadu, India": "Tamil Nadu",
        "India": "India",
        "Chennai, Tamil Nadu, India": "Chennai Tamil Nadu",
        "Coimbatore, Tamil Nadu, India": "Coimbatore Tamil Nadu",
    }.get(region, region or "")
    if region_label and topic_label and topic_label != "general":
        return f"{region_label} {topic_label} news today"
    if region_label:
        return f"{region_label} news headlines today"
    if topic_label and topic_label != "general":
        return f"{topic_label} news today"
    return "top current headlines"


def _is_news_status_query(norm: str) -> bool:
    provider_terms = ("tavily", "newsapi", "news api", "newsai")
    status_terms = ("access", "configured", "configuration", "do you have", "is ", "check", "use ")
    return any(term in norm for term in provider_terms) and any(term in norm for term in status_terms)


def _is_news_query(norm: str) -> bool:
    if _is_news_status_query(norm):
        return True
    news_phrases = (
        "news", "headline", "headlines", "interesting news", "latest news", "current news", "today news", "news headlines",
        "give me headlines", "tell me headlines", "top headlines", "top stories", "hot news", "breaking news",
        "latest updates", "current updates", "interesting updates", "trending", "updates", "international news",
        "spicy international news", "technology news", "tech news", "ai news",
        "business news", "sports news", "india news", "news about", "happening today",
        "political news", "politics news", "election news", "current news in",
    )
    return any(phrase in norm for phrase in news_phrases) or (("news" in norm or "headlines" in norm) and bool(_news_region_from_text(norm)))


def _is_news_followup(norm: str) -> bool:
    if not _LAST_LOCAL_ACTION or _LAST_LOCAL_ACTION.get("tool") != "news" or not _LAST_LOCAL_ACTION.get("ok"):
        return False
    if _is_news_query(norm):
        return True
    if norm in {"technology", "tech", "ai", "politics", "political", "sports", "cinema", "movies", "business", "market", "education", "exams"}:
        return True
    return any(phrase in norm for phrase in ("what about tamil nadu", "tamil nadu", "more like this", "more news", "more updates"))


def _news_detection_from_text(norm: str, *, followup: bool = False) -> dict[str, Any]:
    previous_params = _LAST_LOCAL_ACTION.get("params", {}) if (_LAST_LOCAL_ACTION and isinstance(_LAST_LOCAL_ACTION.get("params"), dict)) else {}
    topic = _news_topic_from_text(norm)
    region = _news_region_from_text(norm)
    if followup:
        if "more like this" in norm and previous_params.get("topic"):
            topic = str(previous_params.get("topic") or "general")
        elif topic == "general" and previous_params.get("topic"):
            topic = str(previous_params.get("topic") or "general")
        if not region and previous_params.get("region"):
            region = str(previous_params.get("region") or "")
    if not region:
        region = "India"
    query = _news_provider_query(topic, region, norm)
    action = "topic" if topic and topic != "general" else "headlines"
    normalized_query = norm.replace("today s", "today").replace("today's", "today")
    params = {
        "topic": topic or "general",
        "region": region,
        "query": query,
        "normalized_query": normalized_query,
        "used_followup_context": bool(followup),
    }
    print(f'[AURA NEWS ROUTER] news_intent_detected query="{normalized_query}" topic="{params["topic"]}" region="{region}"')
    return {"tool_name": "news", "action": action, "params": params, "risk_level": "safe", "confidence": 0.94 if followup else 0.92}


def _is_failed_action_followup(norm: str) -> bool:
    return any(phrase in norm for phrase in (
        "no new tab opened", "it did not open", "nothing opened",
        "not opened yet", "nothing happened", "didnt work",
        "didn't work", "not working", "failed",
    ))


def _audit(request_id: str, result: dict[str, Any]) -> None:
    if str(os.getenv("AURA_TOOL_AUDIT_LOG", "true")).lower() in {"0", "false", "no"}:
        return
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "tool": result.get("tool"),
        "action": result.get("action"),
        "risk_level": result.get("risk_level"),
        "executed": bool(result.get("executed")),
        "ok": bool(result.get("ok")),
        "error_type": result.get("error_type"),
        "safe_summary": str(result.get("response_text") or "")[:220],
    }
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def status() -> dict[str, Any]:
    mcp_enabled = os.environ.get("AURA_BROWSER_MCP_ENABLED", os.environ.get("JARVIS_BROWSER_MCP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
    window_mode = os.environ.get("AURA_BROWSER_WINDOW_MODE", os.environ.get("JARVIS_BROWSER_WINDOW_MODE", "separate")).strip().lower()
    browser_caps = ["new_tab", "new_window", "open_allowlisted_url", "safe_search"]
    if mcp_enabled and window_mode == "separate":
        browser_caps.extend(["browser_open", "browser_search", "browser_new_window", "browser_click", "browser_type", "browser_back", "browser_forward", "browser_reload", "browser_read", "browser_scroll", "browser_followup"])
    tools_info = {}
    for name, meta in TOOL_REGISTRY.items():
        tools_info[name] = {
            "enabled": True,
            "display_name": meta["display_name"],
            "risk_level": meta["risk_level"],
            "description": meta["description"],
            "capabilities": meta["actions"],
        }
    tools_info["browser"]["capabilities"] = browser_caps
    return {"ok": True, "tools": tools_info, "registry": {name: {"actions": meta["actions"], "risk_level": meta["risk_level"]} for name, meta in TOOL_REGISTRY.items()}}


def _extract_google_query(text: str) -> str | None:
    """Extract search query from natural language browser commands deterministically."""
    norm = text.strip().lower()
    patterns = [
        r"^open google chrome and search (.+)$", r"^open google and search (.+)$", r"^open chrome and search (.+)$",
        r"^search (.+) in google chrome$", r"^search (.+) in chrome$", r"^search (.+) in browser$",
        r"^search google for (.+)$", r"^search web for (.+)$",
        r"^search google (.+)$", r"^search web (.+)$",
        r"^google for (.+)$", r"^google (.+)$", r"^look up (.+)$",
        r"^search for (.+)$", r"^search (.+)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, norm)
        if m:
            return m.group(1).strip()
    return None


def _browser_alias_target(norm: str) -> str | None:
    browser_aliases = {
        "chrome": "chrome",
        "google chrome": "chrome",
        "browser": "chrome",
        "web browser": "chrome",
        "google": "google",
    }
    for verb in ("open", "launch", "start"):
        if norm.startswith(f"{verb} "):
            target = norm.replace(f"{verb} ", "", 1).strip()
            return browser_aliases.get(target)
    return None


def _is_browser_open_command(norm: str) -> bool:
    if _extract_google_query(norm):
        return True
    if _browser_alias_target(norm):
        return True
    open_patterns = [
        "open google", "open chrome", "open github", "open youtube",
        "search wikipedia", "search google", "search web", "search youtube",
        "open chrome and search", "open google and search", "open google chrome and search",
        "search for ", "search google for", "search web for",
    ]
    return any(norm.startswith(p) for p in open_patterns)


def _is_separate_window_mode() -> bool:
    return os.environ.get("AURA_BROWSER_WINDOW_MODE", os.environ.get("JARVIS_BROWSER_WINDOW_MODE", "separate")).strip().lower() == "separate"


def _is_mcp_browser_enabled() -> bool:
    return os.environ.get("AURA_BROWSER_MCP_ENABLED", os.environ.get("JARVIS_BROWSER_MCP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _is_browser_followup_command(norm: str) -> bool:
    followup_patterns = [
        "open the first result", "click that", "click the first",
        "go back", "go forward", "go to the first",
        "scroll down", "scroll up", "search for instead", "search for ",
        "open the first", "click first", "take snapshot", "take a snapshot",
        "reload", "refresh page",
    ]
    return any(norm.startswith(p) or norm == p for p in followup_patterns)


def _detect_browser_followup(norm: str) -> dict[str, Any]:
    if norm.startswith("open the first") or norm.startswith("open the first result") or norm.startswith("click the first") or norm.startswith("click that") or norm.startswith("click first"):
        return {"tool_name": "browser", "action": "browser_click", "params": {"target": "first_result"}, "risk_level": "safe", "confidence": 0.9}
    if norm == "go back":
        return {"tool_name": "browser", "action": "browser_back", "params": {}, "risk_level": "safe", "confidence": 0.9}
    if norm == "go forward":
        return {"tool_name": "browser", "action": "browser_forward", "params": {}, "risk_level": "safe", "confidence": 0.9}
    if norm.startswith("scroll down"):
        return {"tool_name": "browser", "action": "browser_scroll", "params": {"direction": "down"}, "risk_level": "safe", "confidence": 0.9}
    if norm.startswith("scroll up"):
        return {"tool_name": "browser", "action": "browser_scroll", "params": {"direction": "up"}, "risk_level": "safe", "confidence": 0.9}
    if norm.startswith("search for instead") or norm.startswith("search for "):
        query = norm.replace("search for instead ", "").replace("search for ", "").strip()
        if query:
            return {"tool_name": "browser", "action": "browser_search", "params": {"query": query, "search_query": query}, "risk_level": "safe", "confidence": 0.9}
    if norm in {"take_snapshot", "take a snapshot"}:
        return {"tool_name": "browser", "action": "browser_read", "params": {"action": "snapshot"}, "risk_level": "safe", "confidence": 0.9}
    if norm in {"reload", "refresh page"}:
        return {"tool_name": "browser", "action": "browser_reload", "params": {}, "risk_level": "safe", "confidence": 0.9}
    return {"tool_name": "browser", "action": "browser_followup", "params": {"query": norm}, "risk_level": "safe", "confidence": 0.85}


def _log_route_detection(text: str, norm: str, detection: dict[str, Any], context: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    tool = str(detection.get("tool_name") or "")
    action = str(detection.get("action") or "")
    params = detection.get("params") if isinstance(detection.get("params"), dict) else {}
    input_kind = str((context or {}).get("input_kind") or (context or {}).get("frontend_kind") or "text")
    if tool == "browser" and action == "browser_search":
        print(f"[AURA ROUTE] input_kind={input_kind} intent=browser_search query={params.get('query') or params.get('search_query') or ''} selected_tool=browser_tool reason={reason}")
    elif tool == "browser" and action == "browser_open":
        print(f"[AURA ROUTE] input_kind={input_kind} intent=browser_open target={params.get('target') or _browser_alias_target(norm) or params.get('url') or ''} selected_tool=browser_tool reason={reason}")
    return detection


def detect_tool_command(text: str, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    norm = normalize_tool_command_text(text)
    if not norm:
        return None
    if _is_failed_action_followup(norm) and _LAST_LOCAL_ACTION and _LAST_LOCAL_ACTION.get("tool") == "browser":
        action = str(_LAST_LOCAL_ACTION.get("action") or "")
        if action in {"new_tab", "new_window"}:
            return {"tool_name": "browser", "action": action, "params": {"retry": True, "followup": True, "retry_allowed": not _LAST_LOCAL_ACTION.get("retry_used")}, "risk_level": "safe", "failure_class": "deterministic_followup_failure", "confidence": 0.96}
    if any(x in norm for x in ["format drive", "format my drive", "format c drive", "delete personal files", "disable antivirus", "read saved passwords", "read browser passwords"]):
        return {"tool_name": "local_command", "action": "blocked_unsafe_action", "params": {}, "risk_level": "blocked", "confidence": 0.98}
    if any(x in norm for x in ["weather", "forecast", "rain", "temperature"]):
        action = "forecast_tomorrow" if "tomorrow" in norm else ("rain_check" if "rain" in norm else ("temperature_now" if "temperature" in norm else "forecast_today"))
        city = _weather_location_from_text(norm)
        params = {"query_text": norm}
        if city: params["city"] = city
        return {"tool_name": "weather", "action": action, "params": params, "risk_level": "safe", "confidence": 0.95}
    if _is_news_query(norm) or _is_news_followup(norm):
        if _is_news_status_query(norm):
            return {"tool_name": "news", "action": "status", "params": {}, "risk_level": "safe", "confidence": 0.94}
        return _news_detection_from_text(norm, followup=_is_news_followup(norm) and not _is_news_query(norm))
    if any(x in norm for x in ["battery", "cpu", "ram", "memory usage", "disk", "storage", "network status", "running apps", "what is my cpu", "cpu usage", "how much ram", "memory info", "disk info", "storage info", "network info"]):
        action = "battery_status" if "battery" in norm else ("cpu_ram_status" if any(x in norm for x in ["cpu", "ram", "memory", "what is my cpu", "cpu usage"]) else ("disk_status" if any(x in norm for x in ["disk", "storage"]) else ("network_status" if "network" in norm else "process_summary")))
        return {"tool_name": "system_monitor", "action": action, "params": {}, "risk_level": "safe", "confidence": 0.95}
    if any(x in norm for x in ["increase volume", "decrease volume", "mute volume", "brightness", "lock laptop", "shutdown", "restart", "clean temp", "close app", "close notepad", "volume up", "volume down", "mute", "brightness info", "brightness level", "is my laptop locked", "lock my laptop"]):
        action = "volume_up" if "increase volume" in norm or "volume up" in norm else ("volume_down" if "decrease volume" in norm or "volume down" in norm else ("volume_mute" if "mute volume" in norm or "mute" in norm else ("brightness_info" if "brightness" in norm else ("lock_laptop" if "lock laptop" in norm or "is my laptop locked" in norm or "lock my laptop" in norm else ("shutdown_laptop" if "shutdown" in norm else ("restart_laptop" if "restart" in norm else ("clean_temp_files" if "clean temp" in norm else "close_app")))))))
        risk = "confirm_required" if action in local_command_tool.CONFIRM_ACTIONS else "safe"
        return {"tool_name": "local_command", "action": action, "params": {}, "risk_level": risk, "confidence": 0.95}
    browser_words = set(norm.split())
    browser_context = bool({"chrome", "browser"} & browser_words)
    current_window_phrases = ("current window", "existing window", "same window", "active window")
    current_window_qualifiers = {"current", "existing", "same", "active"}
    target_window = "current" if (any(phrase in norm for phrase in current_window_phrases) or ("window" in browser_words and bool(current_window_qualifiers & browser_words))) else "unspecified"
    if browser_context and "tab" in browser_words:
        return {"tool_name": "browser", "action": "new_tab", "params": {"target_window": target_window}, "risk_level": "safe", "confidence": 0.97}
    explicit_new_window = ("new window" in norm or "new chrome window" in norm or "new browser window" in norm or "separate window" in norm or "launch chrome in new window" in norm or "open browser window" in norm or "open chrome window" in norm)
    if browser_context and "window" in browser_words and ("open" in browser_words or "new" in browser_words or "launch" in browser_words or "separate" in browser_words) and explicit_new_window:
        return {"tool_name": "browser", "action": "new_window", "params": {"target_window": "unspecified"}, "risk_level": "safe", "confidence": 0.96}
    if _is_browser_open_command(norm):
        query = _extract_google_query(norm)
        if query:
            return _log_route_detection(text, norm, {"tool_name": "browser", "action": "browser_search", "params": {"query": query, "search_query": query}, "risk_level": "safe", "confidence": 0.95}, context, "browser_search_phrase")
        browser_alias = _browser_alias_target(norm)
        if browser_alias:
            url = "https://www.google.com/"
            return _log_route_detection(text, norm, {"tool_name": "browser", "action": "browser_open", "params": {"url": url, "target": browser_alias}, "risk_level": "safe", "confidence": 0.96}, context, "browser_alias")
        if norm.startswith("open ") and any(x in norm for x in ["github", "youtube"]):
            site = norm.replace("open ", "", 1).strip()
            if _is_separate_window_mode() and _is_mcp_browser_enabled():
                url_map = {"google": "https://www.google.com/", "youtube": "https://www.youtube.com/", "github": "https://github.com/"}
                return {"tool_name": "browser", "action": "browser_open", "params": {"url": url_map.get(site, f"https://{site}.com/")}, "risk_level": "safe", "confidence": 0.95}
            # When MCP is not enabled, let it fall through to app_launcher below
            if not _is_mcp_browser_enabled():
                pass  # fall through to app_launcher
            else:
                return {"tool_name": "browser", "action": "open_site", "params": {"site": site}, "risk_level": "safe", "confidence": 0.9}
    if norm.startswith("open ") and any(x in norm for x in ["calculator", "vscode", "vs code", "powershell", "notepad", "file explorer", "file_explorer", "explorer", "terminal", "cmd"]):
        app = norm.replace("open ", "", 1).strip()
        return {"tool_name": "app_launcher", "action": "open_app", "params": {"app": app}, "risk_level": "safe", "confidence": 0.9}
    if re.match(r"^open [a-z0-9_.-]+$", norm):
        app = norm.replace("open ", "", 1).strip()
        return {"tool_name": "app_launcher", "action": "open_app", "params": {"app": app}, "risk_level": "safe", "confidence": 0.72}
    if any(x in norm for x in ["open cartesia", "open openrouter", "open groq", "open canva"]):
        if norm.startswith("search "):
            query = norm.replace("search google for", "").replace("search web for", "").replace("search google", "").replace("search web", "").strip()
            return {"tool_name": "browser", "action": "web_search", "params": {"query": query}, "risk_level": "safe", "confidence": 0.9}
        site = norm.replace("open ", "", 1).strip()
        return {"tool_name": "browser", "action": "open_site", "params": {"site": site}, "risk_level": "safe", "confidence": 0.9}
    if _is_browser_followup_command(norm):
        return _detect_browser_followup(norm)
    if any(x in norm for x in ["coding setup", "aura project", "project mode"]):
        return {"tool_name": "app_launcher", "action": "open_workspace_preset", "params": {"preset": "aura_project" if "aura" in norm else "coding"}, "risk_level": "safe", "confidence": 0.88}
    if any(x in norm for x in ["remind me", "list reminders", "delete reminder", "create reminder", "create a reminder", "add reminder", "new reminder", "my reminders", "remind me to"]):
        action = "list_reminders" if "list" in norm else ("delete_reminder" if "delete" in norm else "create_reminder")
        return {"tool_name": "reminder", "action": action, "params": {"raw_text": norm, "text": norm.replace("remind me", "").replace("create reminder", "").replace("add reminder", "").replace("new reminder", "").strip() or "Reminder"}, "risk_level": "safe" if action != "delete_reminder" else "confirm_required", "confidence": 0.9}
    if any(x in norm for x in ["find file", "search downloads", "latest ppt", "project report", "show recent files", "open file location", "find ", "search file", "search document", "find document", "find report", "find pdf", "find image", "find photo", "find video", "find audio", "find music", "find movie", "find word", "find excel", "find powerpoint", "find ppt", "find zip", "find archive", "where is file", "where is the file", "recent files", "show recent", "recent downloads", "recent documents", "recent photos", "recent images"]):
        action = "recent_files" if any(x in norm for x in ["recent", "show recent", "recent downloads", "recent documents", "recent photos", "recent images"]) else ("open_file_location" if "open file location" in norm or "where is the file" in norm or "where is file" in norm else "find_file")
        query = norm.replace("find ", "").replace("search ", "").replace("file search", "").replace("search file", "").strip()
        return {"tool_name": "file_search", "action": action, "params": {"query": query}, "risk_level": "safe", "confidence": 0.88}
    if any(x in norm for x in ["clipboard", "screenshot", "capture screen", "copy this text", "copy text", "read clipboard", "what is on clipboard", "clipboard content", "screenshot now", "take a screenshot"]):
        action = "take_screenshot" if "screenshot" in norm or "capture screen" in norm or "screenshot now" in norm or "take a screenshot" in norm else "read_clipboard"
        return {"tool_name": "clipboard", "action": action, "params": {}, "risk_level": "safe", "confidence": 0.9}
    if any(x in norm for x in ["git status", "which branch", "npm build", "python compile", "list project files", "open this project in vs code"]):
        action = "git_status" if "git status" in norm else ("git_branch" if "branch" in norm else ("npm_build_status_command_preview" if "npm build" in norm else ("python_compile_command_preview" if "python compile" in norm else ("open_project_in_vscode" if "vs code" in norm else "list_project_files"))))
        return {"tool_name": "coding", "action": action, "params": {}, "risk_level": "safe", "confidence": 0.9}
    return None


def execute_aura_tool(tool_name: str, action: str, params: dict[str, Any] | None, context: dict[str, Any] | None) -> dict[str, Any]:
    global _LAST_LOCAL_ACTION
    params = params or {}
    if str(os.getenv("AURA_ENABLE_LOCAL_TOOLS", "true")).lower() in {"0", "false", "no"}:
        result = tool_response(ok=False, tool=tool_name, action=action, response_text="Local tools are disabled, Sir.", error_type="local_tools_disabled")
    elif action == "blocked_unsafe_action":
        result = blocked(tool_name, action, "I cannot perform that action, Sir.")
    else:
        module = TOOL_MODULES.get(tool_name)
        try:
            if tool_name == "browser":
                operation = "open_window" if action in {"browser_open", "browser_search", "browser_new_window", "new_window"} else action
                print(f"[AURA TOOL EXEC] tool=browser_tool operation={operation}")
            result = blocked(tool_name, action, "That tool is not available yet, Sir.") if not module else module.execute(action, params, context or {})
        except Exception as exc:
            result = tool_response(ok=False, tool=tool_name, action=action, executed=False, response_text="I could not read system usage right now, Sir." if tool_name == "system_monitor" else "I could not execute that local tool safely, Sir.", error_type=f"{tool_name}_tool_exception", error_message=str(exc)[:160])
    if tool_name == "browser" and action in {"new_tab", "new_window", "browser_open", "browser_search", "browser_new_window"}:
        if params.get("followup"):
            _LAST_LOCAL_ACTION = {**(_LAST_LOCAL_ACTION or {}), "retry_used": True}
            result["response_text"] = "The previous browser action was dispatched, Sir, but I could not verify the Chrome tab. I will retry using the fallback Chrome launch path."
            result.setdefault("metadata", {})["followup_retry"] = True
        else:
            _LAST_LOCAL_ACTION = {"tool": tool_name, "action": action, "ok": result.get("ok"), "executed": result.get("executed"), "retry_used": False}
    elif tool_name == "browser" and action in {"browser_click", "browser_type", "browser_back", "browser_forward", "browser_reload", "browser_read", "browser_scroll", "browser_followup"}:
        _LAST_LOCAL_ACTION = {"tool": tool_name, "action": action, "ok": result.get("ok"), "executed": result.get("executed"), "browser_task_id": params.get("browser_task_id", "")}
    elif tool_name == "news":
        _LAST_LOCAL_ACTION = {"tool": tool_name, "action": action, "ok": result.get("ok"), "executed": result.get("executed"), "params": dict(params or {})}
    request_id = str((context or {}).get("request_id") or "current")
    window_mode = os.environ.get("AURA_BROWSER_WINDOW_MODE", os.environ.get("JARVIS_BROWSER_WINDOW_MODE", "separate")).strip().lower()
    mcp_enabled = os.environ.get("AURA_BROWSER_MCP_ENABLED", os.environ.get("JARVIS_BROWSER_MCP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
    if tool_name == "browser":
        intent_name = f"deterministic_browser_search" if action == "browser_search" else f"tool_command.browser.{action}"
        print(f"[AURA BROWSER ROUTE] intent={intent_name} method=deterministic window_mode={window_mode}")
    print(f"[AURA TOOL] detected tool={tool_name} action={action} risk={result.get('risk_level')}")
    print(f"[AURA TOOL] executed tool={tool_name} action={action} ok={bool(result.get('ok'))}")
    _audit(request_id, result)
    return result


def read_audit_log(limit: int = 50) -> dict[str, Any]:
    if not AUDIT_PATH.exists():
        return {"ok": True, "entries": []}
    lines = AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return {"ok": True, "entries": entries}
