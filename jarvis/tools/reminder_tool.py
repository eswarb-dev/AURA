"""Local JSON reminder storage."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .tool_types import tool_response

ROOT = Path(__file__).resolve().parents[2]
REMINDERS = ROOT / "diagnostics" / "tools" / "reminders.json"


def _load() -> list[dict[str, Any]]:
    if not REMINDERS.exists():
        return []
    try:
        data = json.loads(REMINDERS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict[str, Any]]) -> None:
    REMINDERS.parent.mkdir(parents=True, exist_ok=True)
    REMINDERS.write_text(json.dumps(items, indent=2), encoding="utf-8")


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    if action == "list_reminders":
        items = _load()
        return tool_response(ok=True, tool="reminder", action=action, executed=True, result={"reminders": items[:10]}, response_text=f"You have {len(items)} local reminders, Sir.")
    if action == "delete_reminder":
        return tool_response(ok=False, tool="reminder", action=action, risk_level="confirm_required", response_text="That action requires confirmation, Sir.", error_type="confirmation_required")
    if action == "create_reminder":
        text = str(params.get("text") or "Reminder").strip()[:180]
        raw = str(params.get("raw_text") or "").lower()
        if "every " in raw or "daily" in raw or "weekly" in raw:
            return tool_response(ok=False, tool="reminder", action=action, response_text="I can save one-time local reminders now, Sir. Recurring reminders can be added later.", error_type="recurrence_unsupported")
        due = datetime.now(timezone.utc) + timedelta(minutes=10)
        match = re.search(r"in\s+(\d+)\s+(minute|minutes|hour|hours)", raw)
        if match:
            amount = int(match.group(1))
            due = datetime.now(timezone.utc) + (timedelta(hours=amount) if "hour" in match.group(2) else timedelta(minutes=amount))
        items = _load()
        item = {"id": str(uuid.uuid4()), "text": text, "due_at": due.isoformat(), "created_at": datetime.now(timezone.utc).isoformat()}
        items.append(item)
        _save(items)
        return tool_response(ok=True, tool="reminder", action=action, executed=True, result={"reminder": item}, response_text="Reminder saved, Sir.")
    return tool_response(ok=False, tool="reminder", action=action, response_text="That reminder action is not available yet, Sir.", error_type="unknown_action")
