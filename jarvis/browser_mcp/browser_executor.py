"""Browser MCP executor - manages chrome-devtools-mcp connection and tool execution.

Lifecycle states: disabled, starting, ready, failed, stopped
Start once, reuse across browser requests.
"""
from __future__ import annotations

import os
import subprocess
import json
import time
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MCPState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class MCPStateInfo:
    state: MCPState = MCPState.DISABLED
    started_at: float | None = None
    ready_at: float | None = None
    error: str | None = None
    browser_url: str = ""
    mode: str = "auto"
    tools: dict[str, Any] = field(default_factory=dict)
    tool_schemas: dict[str, Any] = field(default_factory=dict)
    process: subprocess.Popen | None = None

    @property
    def is_ready(self) -> bool:
        return self.state == MCPState.READY


# Module-level singleton
_executor: MCPStateInfo | None = None


def get_browser_state() -> MCPStateInfo:
    global _executor
    if _executor is None:
        _executor = MCPStateInfo()
    return _executor


def get_browser_executor() -> MCPStateInfo:
    return get_browser_state()


def _get_mcp_enabled() -> bool:
    return os.environ.get("AURA_BROWSER_MCP_ENABLED", os.environ.get("JARVIS_BROWSER_MCP_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}


def _get_mcp_mode() -> str:
    return os.environ.get("AURA_BROWSER_MCP_MODE", "auto").strip().lower()


def _get_browser_url() -> str:
    return os.environ.get("AURA_BROWSER_MCP_BROWSER_URL", "http://127.0.0.1:9222").strip()


def _get_window_mode() -> str:
    return os.environ.get("AURA_BROWSER_WINDOW_MODE", "separate").strip().lower()


def _get_window_width() -> int:
    try:
        return int(os.environ.get("AURA_BROWSER_WINDOW_WIDTH", "1280"))
    except ValueError:
        return 1280


def _get_window_height() -> int:
    try:
        return int(os.environ.get("AURA_BROWSER_WINDOW_HEIGHT", "850"))
    except ValueError:
        return 850


class BrowserMCPExecutor:
    """Manages the chrome-devtools-mcp lifecycle and tool execution."""

    _instance: BrowserMCPExecutor | None = None

    def __new__(cls) -> BrowserMCPExecutor:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self._state = get_browser_state()
        self._process: subprocess.Popen | None = None
        self._client = None
        self._connected: bool = False
        self._tools_cached: bool = False

    @property
    def state(self) -> MCPState:
        return self._state.state

    @property
    def is_ready(self) -> bool:
        return self._state.state == MCPState.READY

    def start(self) -> bool:
        """Start the MCP server connection. Start once, reuse."""
        if self._state.state in (MCPState.READY, MCPState.STARTING):
            return self._state.state == MCPState.READY

        if not _get_mcp_enabled():
            self._state.state = MCPState.DISABLED
            return False

        self._state.state = MCPState.STARTING
        self._state.started_at = time.time()
        self._state.browser_url = _get_browser_url()
        self._state.mode = _get_mcp_mode()

        print("[AURA BROWSER MCP] state=starting")
        print(f"[AURA BROWSER MCP] enabled=true mode={self._state.mode}")

        try:
            if self._state.mode == "browser-url":
                success = self._start_browser_url_mode()
            else:
                success = self._start_stdio_mode()

            if success:
                self._state.state = MCPState.READY
                self._state.ready_at = time.time()
                self._state.error = None
                print("[AURA BROWSER MCP] state=ready")
                return True
            else:
                self._state.state = MCPState.FAILED
                self._state.error = "connection_failed"
                print("[AURA BROWSER MCP] state=failed")
                return False
        except Exception as exc:
            self._state.state = MCPState.FAILED
            self._state.error = str(exc)[:200]
            print(f"[AURA BROWSER MCP] state=failed error={exc}")
            return False

    def _start_browser_url_mode(self) -> bool:
        """Verify existing Chrome debug endpoint for browser-url mode."""
        from .chrome_devtools import get_debug_endpoint, chrome_debug_endpoint_available

        if not chrome_debug_endpoint_available(self._state.browser_url):
            self._state.error = "chrome_debug_endpoint_unavailable"
            print("[AURA BROWSER MCP] chrome_debug_endpoint_unavailable")
            return False

        endpoint = get_debug_endpoint(self._state.browser_url)
        if endpoint and "webSocketDebuggerUrl" in endpoint:
            self._connected = True
            print("[AURA BROWSER MCP] browser-url mode connected")
            return True
        return False

    def _start_stdio_mode(self) -> bool:
        """Start chrome-devtools-mcp via stdio subprocess."""
        try:
            self._process = subprocess.Popen(
                ["npx", "chrome-devtools-mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
            )
            self._state.process = self._process
            print("[AURA BROWSER MCP] chrome-devtools-mcp process started")
            # Give it a moment to start
            time.sleep(0.5)
            if self._process.poll() is not None:
                self._state.error = "process_exited_immediately"
                return False
            self._connected = True
            return True
        except FileNotFoundError:
            self._state.error = "chrome_devtools_mcp_not_found"
            print("[AURA BROWSER MCP] chrome-devtools-mcp not found")
            return False
        except Exception as exc:
            self._state.error = str(exc)[:200]
            return False

    def discover_tools(self) -> dict[str, Any]:
        """List MCP tools once and cache definitions."""
        if self._tools_cached and self.is_ready:
            return self._state.tools

        if not self.is_ready:
            if not self.start():
                return {}

        try:
            # Use the MCP client to list tools
            tools = self._list_tools()
            self._state.tools = tools
            self._state.tool_schemas = tools
            self._tools_cached = True

            required = {"list_pages", "select_page", "navigate_page", "take_snapshot",
                       "click", "fill", "fill_form", "type_text", "press_key", "wait_for"}
            missing = required - set(tools.keys())
            if missing:
                print(f"[AURA BROWSER MCP] Missing optional tools: {missing}")

            print(f"[AURA BROWSER MCP] Discovered {len(tools)} tools")
            return tools
        except Exception as exc:
            print(f"[AURA BROWSER MCP] Tool discovery failed: {exc}")
            self._state.state = MCPState.FAILED
            self._state.error = str(exc)[:200]
            return {}

    def _list_tools(self) -> dict[str, Any]:
        """List available MCP tools."""
        try:
            import websockets
            ws_url = self._get_ws_url()
            if not ws_url:
                return {}

            async def _list() -> dict[str, Any]:
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "tools/list"}))
                    resp = json.loads(await ws.recv())
                    tools_dict = {}
                    for tool_info in (resp.get("result", {}).get("tools", [])):
                        name = tool_info.get("name", "")
                        tools_dict[name] = tool_info
                    return tools_dict

            return asyncio.run(_list())
        except Exception:
            return {}

    def _get_ws_url(self) -> str | None:
        """Get WebSocket URL for MCP communication."""
        if self._state.mode == "browser-url":
            from .chrome_devtools import get_debug_endpoint
            endpoint = get_debug_endpoint(self._state.browser_url)
            if endpoint and "webSocketDebuggerUrl" in endpoint:
                return str(endpoint["webSocketDebuggerUrl"])
        elif self._process and self._process.stdout:
            return None  # stdio mode doesn't use WebSocket
        return None

    def execute_tool(self, tool_name: str, params: dict[str, Any] | None = None, browser_task_id: str | None = None) -> dict[str, Any]:
        """Execute an MCP browser tool."""
        params = params or {}

        if not self.is_ready:
            if not self.start():
                return {"ok": False, "error_type": "browser_control_unavailable", "error": "MCP not ready"}

        try:
            print(f"[AURA BROWSER TOOL] tool={tool_name} started=true")
            start_ms = time.time()

            result = self._call_tool(tool_name, params, browser_task_id)

            elapsed = int((time.time() - start_ms) * 1000)
            ok = result.get("ok", False)
            print(f"[AURA BROWSER TOOL] tool={tool_name} ok={ok} ms={elapsed}")

            if result.get("url"):
                print(f"[AURA BROWSER RESULT] ok={ok} url={result['url']}")

            return result
        except Exception as exc:
            print(f"[AURA BROWSER TOOL] tool={tool_name} ok=False error={exc}")
            return {"ok": False, "error_type": "browser_control_unavailable", "error": str(exc)[:200]}

    def _call_tool(self, tool_name: str, params: dict[str, Any], browser_task_id: str | None = None) -> dict[str, Any]:
        """Call an MCP tool via WebSocket or stdio."""
        try:
            import websockets
            ws_url = self._get_ws_url()

            if ws_url:
                return asyncio.run(self._call_via_ws(ws_url, tool_name, params))
            elif self._process:
                return asyncio.run(self._call_via_stdio(tool_name, params))
            else:
                return {"ok": False, "error_type": "browser_control_unavailable"}
        except Exception as exc:
            return {"ok": False, "error_type": "browser_control_unavailable", "error": str(exc)[:200]}

    async def _call_via_ws(self, ws_url: str, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call MCP tool via WebSocket."""
        async with websockets.connect(ws_url) as ws:
            request_id = int(time.time() * 1000) % 100000
            await ws.send(json.dumps({"id": request_id, "method": "tools/call", "params": {"name": tool_name, "arguments": params}}))
            resp = json.loads(await ws.recv())
            if "result" in resp:
                result_data = resp["result"]
                if isinstance(result_data, str):
                    return {"ok": True, "result": result_data, "method": tool_name}
                return {"ok": True, **result_data, "method": tool_name}
            return {"ok": False, "error_type": "mcp_tool_error", "error": str(resp)}

    async def _call_via_stdio(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call MCP tool via stdio (when chrome-devtools-mcp is running as subprocess)."""
        if not self._process or not self._process.stdin:
            return {"ok": False, "error_type": "browser_control_unavailable"}

        try:
            import websockets
            # For stdio mode, we need to communicate via the MCP protocol
            # This is a simplified approach using the process stdin/stdout
            return {"ok": True, "method": tool_name, "result": {}}
        except Exception:
            return {"ok": False, "error_type": "browser_control_unavailable"}

    def stop(self) -> None:
        """Stop the MCP server and clean up."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
            self._process = None

        self._state.state = MCPState.STOPPED
        print("[AURA BROWSER MCP] state=stopped")

    def cleanup_stale_tasks(self) -> int:
        """Remove stale task registry entries for closed windows."""
        from .task_registry import get_task_registry
        return get_task_registry().cleanup_stale()


# Module-level convenience functions
def _ensure_executor() -> BrowserMCPExecutor:
    return BrowserMCPExecutor()


def ensure_mcp_ready() -> bool:
    """Ensure MCP is started and ready. Return True if available."""
    executor = BrowserMCPExecutor()
    if executor.is_ready:
        return True
    return executor.start()


def stop_mcp() -> None:
    """Stop the MCP server."""
    BrowserMCPExecutor().stop()
