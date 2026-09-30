"""Safe local capability registry for A.U.R.A.

This module reports what the local runtime can actually do. It never returns
secrets and never treats LLM/TTS keys as account integrations.
"""
from __future__ import annotations

import os
from typing import Any

from .tools.tool_router import status as _tool_status


def _has_any_env(*names: str) -> bool:
    return any(bool(os.environ.get(name, "").strip()) for name in names)


def get_capability_state() -> dict[str, Any]:
    email_configured = _has_any_env(
        "AURA_EMAIL_PROVIDER",
        "AURA_SMTP_HOST",
        "SMTP_HOST",
        "GMAIL_CLIENT_ID",
        "GMAIL_REFRESH_TOKEN",
    )
    cloud_configured = _has_any_env(
        "AURA_CLOUD_STORAGE_PROVIDER",
        "GOOGLE_DRIVE_CLIENT_ID",
        "GOOGLE_DRIVE_REFRESH_TOKEN",
        "DROPBOX_ACCESS_TOKEN",
    )
    calendar_configured = _has_any_env(
        "AURA_CALENDAR_PROVIDER",
        "GOOGLE_CALENDAR_CLIENT_ID",
        "GOOGLE_CALENDAR_REFRESH_TOKEN",
    )
    tool_state = _tool_status()
    return {
        "ok": True,
        "email": {
            "configured": email_configured,
            "can_draft_in_account": email_configured,
            "can_send": False,
            "provider": os.environ.get("AURA_EMAIL_PROVIDER", "").strip() or None if email_configured else None,
        },
        "cloud_storage": {
            "configured": cloud_configured,
            "can_read": cloud_configured,
            "can_write": cloud_configured,
        },
        "calendar": {
            "configured": calendar_configured,
            "can_create_events": calendar_configured,
        },
        "desktop_runtime": {
            "camera_control": True,
            "mic_control": True,
            "tracking_control": True,
            "orb_ui_control": True,
        },
        "vision": {
            "camera": True,
            "person_tracking": True,
            "owner_identity": True,
            "hand_detection": False,
        },
        "brain": {
            "openrouter_configured": _has_any_env("OPENROUTER_API_KEY", "AURA_OPENROUTER_API_KEY"),
            "groq_configured": _has_any_env("GROQ_API_KEY", "AURA_GROQ_API_KEY"),
        },
        "tts": {
            "elevenlabs_configured": bool(os.environ.get("ELEVENLABS_API_KEY", "").strip() or os.environ.get("AURA_ELEVENLABS_API_KEY", "").strip()),
            "fish_audio_configured": _has_any_env("FISH_AUDIO_API_KEY", "AURA_FISH_AUDIO_API_KEY") and _has_any_env("FISH_AUDIO_REFERENCE_ID", "AURA_FISH_AUDIO_REFERENCE_ID"),
        },
        "tools": tool_state.get("tools", {}) if tool_state.get("ok") else {},
    }
