"""Token-protected mobile text queue for the local A.U.R.A orb.

The phone only enqueues text. The laptop orb polls this queue and executes the
turn through the normal frontend runtime so audio stays on the laptop.
"""
from __future__ import annotations

import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


QUEUE_MAX = 20
COMMAND_TTL_SECONDS = 60.0
LAPTOP_CLIENT_TTL_SECONDS = 5.0


class RemoteControlError(Exception):
    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message

    def as_detail(self) -> dict[str, Any]:
        return {"ok": False, "error_type": self.error_type, "message": self.message}


@dataclass
class RemoteCommand:
    command_id: str
    text: str
    source: str
    label: str | None
    shortcut: str | None
    frontend_kind: str
    created_at: float
    expires_at: float
    status: str = "queued"
    safe_message: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "text": self.text,
            "source": self.source,
            "label": self.label,
            "shortcut": self.shortcut,
            "frontend_kind": self.frontend_kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


_queue: list[RemoteCommand] = []
_known_commands: dict[str, RemoteCommand] = {}
_last_command_status = "none"
_last_laptop_poll_at = 0.0
_test_enabled: bool | None = None
_test_token: str | None = None


def _repo_env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def _read_repo_env_value(name: str) -> str | None:
    try:
        lines = _repo_env_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return None


def _env_value(name: str) -> str:
    if name == "AURA_REMOTE_CONTROL_TOKEN" and _test_token is not None:
        return _test_token
    value = _read_repo_env_value(name)
    if value is not None:
        return value
    return os.environ.get(name, "")


def is_enabled() -> bool:
    if _test_enabled is not None:
        return _test_enabled
    value = _env_value("AURA_REMOTE_CONTROL_ENABLED").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def _configured_token() -> str:
    return _env_value("AURA_REMOTE_CONTROL_TOKEN").strip()


def _frontend_kind_for(text: str, requested: str | None = None, shortcut: str | None = None) -> str:
    if requested:
        return str(requested).strip()[:64]
    norm = " ".join(text.lower().split())
    if shortcut == "I" or norm == "start expo intro":
        return "expo_intro"
    if norm in {"wakeup", "wake up", "hey aura", "hello aura", "hello a.u.r.a", "aura"}:
        return "wake"
    if norm in {"stop speaking", "mute mic", "unmute mic"}:
        return "runtime_control"
    if "memory" in norm:
        return "memory"
    if "weather" in norm or "news" in norm or "headlines" in norm:
        return "tool_command"
    expo_demo_phrases = (
        "introduce yourself", "tell me about yourself", "who are you", "what can you do",
        "explain your features", "tell me about this project", "what is this project",
        "what makes aura different", "different from chatgpt", "how does aura help students",
        "what is engineers week", "what is this expo about", "explain engineers week expo", "what projects are shown here",
        "department projects", "tell me about aura", "explain aura", "edge storm",
        "aura project", "game centre", "tech hub", "safe tools", "tool safety", "can we talk",
        "can we chat", "general talk", "summarize this demo", "conclude the demo",
    )
    if any(phrase in norm for phrase in expo_demo_phrases):
        return "expo_demo"
    return "normal_chat"


def _require_token(token: str | None, *, allow_loopback_laptop: bool = False) -> None:
    if not is_enabled():
        raise RemoteControlError(403, "remote_control_disabled", "Remote control disabled.")
    expected = _configured_token()
    if not expected:
        raise RemoteControlError(403, "remote_control_token_missing", "Remote control token is not configured.")
    if allow_loopback_laptop and not token:
        return
    supplied = (token or "").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise RemoteControlError(401, "invalid_remote_control_token", "Invalid remote control token.")


def _cleanup(now: float | None = None) -> None:
    global _last_command_status
    current = time.time() if now is None else now
    expired_ids = [cmd.command_id for cmd in _queue if cmd.expires_at <= current]
    if expired_ids:
        for command_id in expired_ids:
            cmd = _known_commands.get(command_id)
            if cmd:
                cmd.status = "failed"
                cmd.safe_message = "expired"
        _last_command_status = "failed: expired"
    _queue[:] = [cmd for cmd in _queue if cmd.expires_at > current]


def status(token: str | None = None) -> dict[str, Any]:
    _cleanup()
    enabled = is_enabled()
    token_valid = False
    if enabled and token:
        expected = _configured_token()
        token_valid = bool(expected and secrets.compare_digest(token.strip(), expected))
    return {
        "ok": True,
        "enabled": enabled,
        "laptop_client_connected": enabled and (time.time() - _last_laptop_poll_at) <= LAPTOP_CLIENT_TTL_SECONDS,
        "queue_size": len(_queue) if token_valid else 0,
        "last_command_status": _last_command_status if token_valid else "none",
    }


def send(body: dict[str, Any] | None) -> dict[str, Any]:
    global _last_command_status
    payload = body or {}
    _require_token(str(payload.get("token") or ""))
    _cleanup()
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RemoteControlError(400, "empty_remote_command", "Remote command text is required.")
    if len(text) > 1000:
        raise RemoteControlError(400, "remote_command_too_long", "Remote command text is too long.")
    if len(_queue) >= QUEUE_MAX:
        raise RemoteControlError(429, "remote_queue_full", "Remote command queue is full.")
    source = str(payload.get("source") or "mobile").strip()[:64] or "mobile"
    shortcut_raw = payload.get("shortcut")
    shortcut = str(shortcut_raw).strip()[:16] if shortcut_raw else None
    label_raw = payload.get("label")
    label = str(label_raw).strip()[:120] if label_raw else None
    frontend_kind = _frontend_kind_for(text, str(payload.get("frontend_kind") or ""), shortcut)
    now = time.time()
    command = RemoteCommand(
        command_id=f"remote_{uuid.uuid4().hex[:12]}",
        text=text,
        source=source,
        label=label,
        shortcut=shortcut,
        frontend_kind=frontend_kind,
        created_at=now,
        expires_at=now + COMMAND_TTL_SECONDS,
    )
    _queue.append(command)
    _known_commands[command.command_id] = command
    _last_command_status = "queued"
    return {"ok": True, "command_id": command.command_id, "queue_size": len(_queue), "status": "queued"}


def poll(client: str | None, token: str | None = None, *, loopback_request: bool = False) -> dict[str, Any]:
    global _last_command_status, _last_laptop_poll_at
    client_name = str(client or "").strip()
    allow_loopback_laptop = loopback_request and client_name == "main_orb"
    _require_token(token, allow_loopback_laptop=allow_loopback_laptop)
    if client_name == "main_orb":
        _last_laptop_poll_at = time.time()
    _cleanup()
    command = _queue.pop(0) if _queue else None
    if command is None:
        return {"ok": True, "command": None, "queue_size": 0}
    command.status = "picked_up"
    _last_command_status = "picked_up"
    return {"ok": True, "command": command.to_public_dict(), "queue_size": len(_queue)}


def ack(body: dict[str, Any] | None, *, loopback_request: bool = False) -> dict[str, Any]:
    global _last_command_status
    payload = body or {}
    client_name = str(payload.get("client") or "").strip()
    _require_token(
        str(payload.get("token") or ""),
        allow_loopback_laptop=loopback_request and client_name == "main_orb",
    )
    command_id = str(payload.get("command_id") or "").strip()
    status_value = str(payload.get("status") or "").strip()
    if status_value not in {"picked_up", "route_completed", "speech_played", "route_completed_speech_failed", "executed", "failed"}:
        raise RemoteControlError(400, "invalid_remote_ack_status", "Remote acknowledgement status is invalid.")
    if not command_id:
        raise RemoteControlError(400, "missing_remote_command_id", "Remote command id is required.")
    command = _known_commands.get(command_id)
    safe_message = str(payload.get("safe_message") or "").strip()[:240] or None
    if command:
        command.status = status_value
        command.safe_message = safe_message
    _last_command_status = status_value if not safe_message else f"{status_value}: {safe_message}"
    return {"ok": True, "command_id": command_id, "status": status_value}


def reset_for_tests(enabled: bool | None = None, token: str | None = None) -> None:
    global _test_enabled, _test_token, _last_command_status, _last_laptop_poll_at
    _queue.clear()
    _known_commands.clear()
    _test_enabled = enabled
    _test_token = token
    _last_command_status = "none"
    _last_laptop_poll_at = 0.0


def expire_queued_for_tests() -> None:
    for command in _queue:
        command.expires_at = time.time() - 1.0
