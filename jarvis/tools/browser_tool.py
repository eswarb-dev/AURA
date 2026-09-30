"""Safe browser opening/search action generator with MCP backend.

When AURA_BROWSER_MCP_ENABLED is set, uses chrome-devtools-mcp
for deterministic browser execution instead of frontend window.open.
Top-level browser tasks open in separate Chrome windows.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import urllib.parse

from .tool_types import tool_response

SITES = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "chatgpt": "https://chatgpt.com",
    "cartesia": "https://cartesia.ai",
    "openrouter": "https://openrouter.ai",
    "groq": "https://console.groq.com",
    "canva": "https://www.canva.com",
}

CHROME_SAFE_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

def _get_mcp_enabled() -> bool:
    return os.environ.get("AURA_BROWSER_MCP_ENABLED", os.environ.get("JARVIS_BROWSER_MCP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}

def _get_window_mode() -> str:
    return os.environ.get("AURA_BROWSER_WINDOW_MODE", os.environ.get("JARVIS_BROWSER_WINDOW_MODE", "separate")).strip().lower()

def _is_mcp_mode() -> bool:
    if not _get_mcp_enabled():
        return False
    if _get_window_mode() != "separate":
        return False
    return True


def _safe_chrome_executable() -> str | None:
    configured = os.getenv("AURA_CHROME_PATH") or ""
    candidates = [configured, *CHROME_SAFE_PATHS]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.name.lower() == "chrome.exe" and path.exists():
            return str(path)
    return None


def _launch_chrome(action: str, target_window: str = "unspecified") -> dict:
    chrome = _safe_chrome_executable()
    target = "chrome://newtab/" if action == "new_tab" else None
    debug = {"intent": action, "target_window": target_window if action == "new_tab" else "unspecified", "executor_used": "windows_process", "chrome_path_found": bool(chrome), "process_started": False, "pid_present": False, "used_new_window_flag": action == "new_window", "target_url_or_mode": target or "new_window", "verification_level": "failed", "failure_class": None}
    if not chrome:
        debug["failure_class"] = "browser_chrome_not_found"
        return {"ok": False, "debug": debug, "response": "I could not find Chrome in the safe launcher list, Sir."}
    args = [chrome, "--new-tab", target] if action == "new_tab" else [chrome, "--new-window"]
    try:
        process = subprocess.Popen(args, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        debug["failure_class"] = "browser_process_start_failed"
        return {"ok": False, "debug": debug, "response": "Chrome could not accept the local command, Sir."}
    debug["process_started"] = True
    debug["pid_present"] = bool(process.pid)
    debug["verification_level"] = "process_started"
    if action == "new_tab" and target_window in {"current", "existing"}:
        text = "Chrome new tab command was sent to the existing Chrome session, Sir, but I cannot verify the exact target window."
    else:
        text = "Chrome new tab command was sent locally, Sir." if action == "new_tab" else "Chrome new window command was sent locally, Sir."
    return {"ok": True, "debug": debug, "response": text, "pid": process.pid}


def _build_google_url(query: str) -> str:
    """Build a properly encoded Google search URL."""
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


def _extract_search_query(text: str) -> str | None:
    """Extract search query from natural language browser commands."""
    import re
    text = text.strip().lower()
    for prefix in ["search google for ", "search web for ", "search google ", "search web ", "google for ", "google ", "look up ", "search for "]:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    if text.startswith("open google and search "):
        return text[len("open google and search "):].strip()
    if text.startswith("open chrome and search "):
        return text[len("open chrome and search "):].strip()
    if text.startswith("open google "):
        return text[len("open google "):].strip()
    if text.startswith("open chrome "):
        return text[len("open chrome "):].strip()
    return None


def _execute_mcp_browser_open(url: str, browser_task_id: str | None = None) -> dict:
    """Execute browser open via MCP backend in a separate window."""
    try:
        from ..browser_mcp import get_browser_executor, create_task
        executor = get_browser_executor()
        if not executor.start():
            return {"ok": False, "error_type": "browser_control_unavailable", "action": "browser_open", "response": "Browser control is unavailable. Chrome debug endpoint is not reachable."}

        executor.discover_tools()

        task = create_task(url=url)
        result = executor.execute_tool("navigate_page", {"url": url}, browser_task_id=task.browser_task_id)

        if result.get("ok"):
            task.current_url = url
            return {
                "ok": True,
                "action": "browser_open",
                "browser_task_id": task.browser_task_id,
                "target_id": task.target_id,
                "page_id": task.page_id,
                "window_id": task.window_id,
                "url": url,
                "method": "navigate_page",
                "error_type": None,
                "response_text": f"Opened {url} in a separate Chrome window.",
            }
        return {
            "ok": False,
            "action": "browser_open",
            "browser_task_id": task.browser_task_id,
            "error_type": result.get("error_type", "mcp_execution_failed"),
            "response_text": result.get("error", "Browser execution failed."),
        }
    except Exception as exc:
        return {"ok": False, "action": "browser_open", "error_type": "browser_control_unavailable", "response_text": f"Browser control unavailable: {exc}"}


def _execute_mcp_browser_search(query: str, browser_task_id: str | None = None) -> dict:
    """Execute browser search via MCP backend in a separate window."""
    url = _build_google_url(query)
    return _execute_mcp_browser_open(url, browser_task_id)


def _execute_mcp_browser_new_window(url: str = "https://www.google.com/", browser_task_id: str | None = None) -> dict:
    """Create a new separate Chrome window via CDP."""
    try:
        from ..browser_mcp import get_browser_executor, create_task
        from ..browser_mcp.chrome_devtools import create_browser_task_window
        executor = get_browser_executor()
        if not executor.start():
            return {"ok": False, "error_type": "browser_control_unavailable", "action": "browser_new_window", "response": "Browser control is unavailable."}

        window_result = create_browser_task_window(url=url)
        if window_result and "targetId" in window_result:
            task = create_task(target_id=window_result["targetId"], url=url, window_id=window_result.get("windowId", ""))
            return {
                "ok": True,
                "action": "browser_new_window",
                "browser_task_id": task.browser_task_id,
                "target_id": task.target_id,
                "page_id": task.page_id,
                "window_id": task.window_id,
                "url": url,
                "method": "Target.createTarget",
                "error_type": None,
                "response_text": f"Opened a separate Chrome window: {url}",
            }
        return {"ok": False, "action": "browser_new_window", "error_type": "window_creation_failed", "response_text": "Could not create a separate Chrome window."}
    except Exception as exc:
        return {"ok": False, "action": "browser_new_window", "error_type": "browser_control_unavailable", "response_text": f"Browser control unavailable: {exc}"}


def execute(action: str, params: dict | None = None, context: dict | None = None) -> dict:
    params = params or {}

    if _is_mcp_mode():
        return _execute_mcp_mode(action, params, context)

    return _execute_legacy_mode(action, params, context)


def _execute_mcp_mode(action: str, params: dict, context: dict | None) -> dict:
    """Handle browser actions via MCP backend."""
    print(f"[AURA BROWSER MCP] action={action}")
    from ..browser_mcp import get_browser_executor, create_task, get_task

    executor = get_browser_executor()

    if action == "browser_open":
        url = str(params.get("url") or "").strip()
        if not url:
            return tool_response(ok=False, tool="browser", action=action, response_text="No URL provided.", error_type="missing_url")
        result = _execute_mcp_browser_open(url)
        return _format_mcp_result(result, tool="browser", action=action)

    if action == "browser_search":
        query = str(params.get("query") or str(params.get("search_query") or "")).strip()
        if not query:
            return tool_response(ok=False, tool="browser", action=action, response_text="No search query provided.", error_type="missing_query")
        result = _execute_mcp_browser_search(query)
        return _format_mcp_result(result, tool="browser", action=action)

    if action == "browser_new_window":
        url = str(params.get("url") or "https://www.google.com/").strip()
        result = _execute_mcp_browser_new_window(url)
        return _format_mcp_result(result, tool="browser", action=action)

    if action in {"browser_click", "browser_type", "browser_back", "browser_forward", "browser_reload", "browser_read", "browser_scroll", "browser_followup"}:
        browser_task_id = str(params.get("browser_task_id") or "").strip()
        task = get_task(browser_task_id) if browser_task_id else None
        if not task:
            return tool_response(ok=False, tool="browser", action=action, response_text="No browser task found. Start a new browser task first.", error_type="no_active_task")

        tool_map = {
            "browser_click": "click",
            "browser_type": "type_text",
            "browser_back": "navigate_page",
            "browser_forward": "navigate_page",
            "browser_reload": "reload_page",
            "browser_read": "take_snapshot",
            "browser_scroll": "scroll",
            "browser_followup": "navigate_page",
        }
        mcp_tool = tool_map.get(action)
        if not mcp_tool:
            return tool_response(ok=False, tool="browser", action=action, response_text="Unsupported follow-up action.", error_type="unknown_action")

        result = executor.execute_tool(mcp_tool, params, browser_task_id=browser_task_id)
        return _format_mcp_result(result, tool="browser", action=action, browser_task_id=browser_task_id)

    return tool_response(ok=False, tool="browser", action=action, response_text="That browser action is not available yet, Sir.", error_type="unknown_action")


def _format_mcp_result(result: dict, tool: str, action: str, browser_task_id: str | None = None) -> dict:
    """Format MCP execution result into tool_response format."""
    if result.get("ok"):
        return tool_response(
            ok=True,
            tool=tool,
            action=action,
            executed=True,
            result={
                "browser_task_id": result.get("browser_task_id", ""),
                "target_id": result.get("target_id", ""),
                "page_id": result.get("page_id", ""),
                "window_id": result.get("window_id", ""),
                "url": result.get("url", ""),
                "method": result.get("method", action),
            },
            response_text=result.get("response_text", "Browser action completed."),
            metadata={"browser": {"browser_task_id": result.get("browser_task_id", ""), "url": result.get("url", "")}},
        )
    return tool_response(
        ok=False,
        tool=tool,
        action=action,
        executed=False,
        result={},
        response_text=result.get("response_text", "Browser control unavailable."),
        error_type=result.get("error_type", "browser_control_unavailable"),
        metadata={"browser": {"browser_task_id": browser_task_id or "", "error": result.get("error_type", "unknown")}},
    )


def _execute_legacy_mode(action: str, params: dict, context: dict | None) -> dict:
    """Handle browser actions using the legacy frontend-action approach."""
    if action in {"browser_open", "browser_search", "browser_new_window", "browser_click", "browser_type", "browser_back", "browser_forward", "browser_reload", "browser_read", "browser_scroll", "browser_followup"}:
        return tool_response(ok=False, tool="browser", action=action, executed=False, response_text="Browser control is unavailable. Chrome debug endpoint is not reachable.", error_type="browser_control_unavailable")
    if action == "open_site":
        site = str(params.get("site") or "").lower().strip()
        url = SITES.get(site)
        if not url:
            return tool_response(ok=False, tool="browser", action=action, response_text="That site is not in my safe browser list yet, Sir.", error_type="site_not_allowlisted")
        return tool_response(ok=True, tool="browser", action=action, executed=False, result={"frontend_action": {"type": "browser.open_url", "label": site, "value": url}}, response_text=f"Opening {site}, Sir.")
    if action == "web_search":
        query = str(params.get("query") or "").strip()
        url = _build_google_url(query)
        return tool_response(ok=True, tool="browser", action=action, executed=False, result={"frontend_action": {"type": "browser.search", "label": "Search web", "value": url}}, response_text="Opening web search, Sir.")
    if action == "open_url":
        url = str(params.get("url") or "").strip()
        if not (url.startswith("https://") or url.startswith("http://")):
            return tool_response(ok=False, tool="browser", action=action, response_text="I can only open normal web URLs safely, Sir.", error_type="url_not_allowed")
        return tool_response(ok=True, tool="browser", action=action, executed=False, result={"frontend_action": {"type": "browser.open_url", "label": "Open URL", "value": url}}, response_text="Opening the URL, Sir.")
    if action == "new_tab":
        target_window = str(params.get("target_window") or "unspecified").lower()
        if target_window not in {"current", "existing", "unspecified"}:
            target_window = "unspecified"
        launched = _launch_chrome(action, target_window)
        return tool_response(ok=launched["ok"], tool="browser", action=action, executed=launched["ok"], result={"pid": launched.get("pid")} if launched.get("pid") else {}, response_text=launched["response"], error_type=launched["debug"].get("failure_class"), metadata={"browser": launched["debug"]})
    if action == "new_window":
        launched = _launch_chrome(action, "unspecified")
        return tool_response(ok=launched["ok"], tool="browser", action=action, executed=launched["ok"], result={"pid": launched.get("pid")} if launched.get("pid") else {}, response_text=launched["response"], error_type=launched["debug"].get("failure_class"), metadata={"browser": launched["debug"]})
    return tool_response(ok=False, tool="browser", action=action, response_text="That browser action is not available yet, Sir.", error_type="unknown_action")
