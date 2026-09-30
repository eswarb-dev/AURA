"""Clipboard and screenshot assistant."""
from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .tool_types import tool_response

ROOT = Path(__file__).resolve().parents[2]
SCREENSHOTS = ROOT / "diagnostics" / "screenshots"
SECRET_RE = re.compile(r"(api[_-]?key|token|password|secret|-----BEGIN|sk-[A-Za-z0-9]|gsk_[A-Za-z0-9])", re.I)


def _sensitive(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if action == "read_clipboard":
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            text = str(root.clipboard_get() or "")
            root.destroy()
            if _sensitive(text):
                return tool_response(ok=False, tool="clipboard", action=action, executed=True, response_text="Clipboard appears to contain sensitive content, Sir. I will not read it aloud.", error_type="clipboard_sensitive")
            preview = text[:300]
            return tool_response(ok=True, tool="clipboard", action=action, executed=True, result={"text_preview": preview, "length": len(text)}, response_text=f"Clipboard has {len(text)} characters, Sir.")
        if action == "copy_text":
            return tool_response(ok=False, tool="clipboard", action=action, response_text="Copying text requires explicit content selection, Sir.", error_type="copy_text_needs_payload")
        if action == "take_screenshot":
            try:
                from PIL import ImageGrab  # type: ignore
                SCREENSHOTS.mkdir(parents=True, exist_ok=True)
                path = SCREENSHOTS / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                ImageGrab.grab().save(path)
                return tool_response(ok=True, tool="clipboard", action=action, executed=True, result={"path": str(path)}, response_text="Screenshot captured, Sir.")
            except Exception:
                return tool_response(ok=True, tool="clipboard", action=action, executed=False, result={"frontend_action": {"type": "screenshot.capture", "label": "Capture screenshot"}}, response_text="Screenshot capture is ready, Sir.")
        if action == "screenshot_status":
            count = len(list(SCREENSHOTS.glob("*.png"))) if SCREENSHOTS.exists() else 0
            return tool_response(ok=True, tool="clipboard", action=action, executed=True, result={"count": count}, response_text=f"There are {count} local screenshots saved, Sir.")
    except Exception as exc:
        return tool_response(ok=False, tool="clipboard", action=action, response_text="Clipboard or screenshot access failed, Sir.", error_type="clipboard_tool_failed", error_message=str(exc)[:160])
    return tool_response(ok=False, tool="clipboard", action=action, response_text="That clipboard action is not available yet, Sir.", error_type="unknown_action")
