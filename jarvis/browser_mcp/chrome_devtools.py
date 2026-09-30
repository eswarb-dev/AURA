"""Chrome DevTools Protocol helpers for AURA browser window management.

Provides CDP-level operations for creating separate Chrome windows
and verifying the debug endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any


def get_debug_endpoint(browser_url: str | None = None) -> dict[str, Any] | None:
    """Verify Chrome debug endpoint by fetching /json/version.

    Returns parsed JSON with Browser, Protocol-Version, webSocketDebuggerUrl
    fields, or None if unavailable.
    """
    url = browser_url or os.environ.get("AURA_BROWSER_MCP_BROWSER_URL", "http://127.0.0.1:9222")
    endpoint = f"{url}/json/version"
    try:
        req = urllib.request.Request(endpoint)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "Browser" in data and "webSocketDebuggerUrl" in data:
            return data
    except Exception:
        return None
    return None


def chrome_debug_endpoint_available(browser_url: str | None = None) -> bool:
    """Check if Chrome debug endpoint is available."""
    return get_debug_endpoint(browser_url) is not None


def create_browser_task_window(
    url: str = "about:blank",
    width: int | None = None,
    height: int | None = None,
    left: int | None = None,
    top: int | None = None,
    browser_url: str | None = None,
) -> dict[str, Any] | None:
    """Create a separate Chrome window using Chrome DevTools Protocol.

    Uses Target.createTarget with newWindow=true to create a real Chrome window,
    not a new tab in an existing window.

    Returns dict with targetId, windowId, url on success, None on failure.
    """
    ws_url = _get_websocket_url(browser_url)
    if not ws_url:
        return None

    try:
        import websockets
        import asyncio

        return asyncio.run(_create_target_via_ws(ws_url, url, width, height, left, top))
    except Exception:
        return None


async def _create_target_via_ws(
    ws_url: str,
    url: str,
    width: int | None,
    height: int | None,
    left: int | None,
    top: int | None,
) -> dict[str, Any] | None:
    """Create a CDP target via WebSocket."""
    try:
        async with websockets.connect(ws_url) as ws:
            # Target.createTarget with newWindow=true
            params = {"url": url, "newWindow": True, "focus": True}
            if width:
                params["width"] = width
            if height:
                params["height"] = height
            if left is not None:
                params["left"] = left
            if top is not None:
                params["top"] = top

            request_id = 1
            await ws.send(json.dumps({"id": request_id, "method": "Target.createTarget", "params": params}))

            response = json.loads(await ws.recv())
            if "result" in response and "targetId" in response["result"]:
                target_id = response["result"]["targetId"]
                return {
                    "targetId": target_id,
                    "url": url,
                    "method": "Target.createTarget",
                }
            return None
    except Exception:
        return None


def _get_websocket_url(browser_url: str | None = None) -> str | None:
    """Get the WebSocket debugger URL from Chrome's debug endpoint."""
    data = get_debug_endpoint(browser_url)
    if data and "webSocketDebuggerUrl" in data:
        return str(data["webSocketDebuggerUrl"])
    return None


class ChromeDevToolsProtocol:
    """Minimal CDP connection for browser-level operations.

    Used only for creating separate windows via Target.createTarget.
    All other browser operations go through the MCP layer.
    """

    def __init__(self, browser_url: str | None = None) -> None:
        self._browser_url = browser_url or os.environ.get("AURA_BROWSER_MCP_BROWSER_URL", "http://127.0.0.1:9222")
        self._ws_url: str | None = None
        self._connected: bool = False

    @property
    def available(self) -> bool:
        """Check if CDP endpoint is available."""
        return chrome_debug_endpoint_available(self._browser_url)

    @property
    def websocket_url(self) -> str | None:
        if not self._ws_url:
            self._ws_url = _get_websocket_url(self._browser_url)
        return self._ws_url

    async def connect(self) -> bool:
        """Connect to Chrome debug endpoint."""
        self._ws_url = self.websocket_url
        if not self._ws_url:
            return False
        self._connected = True
        return True

    async def create_target(self, url: str = "about:blank", **kwargs: Any) -> dict[str, Any] | None:
        """Create a separate browser target/window."""
        if not self._connected and not await self.connect():
            return None
        return await _create_target_via_ws(
            self._ws_url or "",
            url,
            kwargs.get("width"),
            kwargs.get("height"),
            kwargs.get("left"),
            kwargs.get("top"),
        )

    async def get_window_for_target(self, target_id: str) -> dict[str, Any] | None:
        """Get window info for a target."""
        try:
            import websockets
            import asyncio
            async with websockets.connect(self._ws_url or "") as ws:
                await ws.send(json.dumps({"id": 2, "method": "Browser.getWindowForTarget", "params": {"targetId": target_id}}))
                response = json.loads(await ws.recv())
                return response.get("result")
        except Exception:
            return None

    async def set_window_bounds(self, window_id: str, bounds: dict[str, int]) -> bool:
        """Set window bounds."""
        try:
            import websockets
            import asyncio
            async with websockets.connect(self._ws_url or "") as ws:
                await ws.send(json.dumps({"id": 3, "method": "Browser.setWindowBounds", "params": {"windowId": window_id, "bounds": bounds}}))
                response = json.loads(await ws.recv())
                return "error" not in response
        except Exception:
            return False
