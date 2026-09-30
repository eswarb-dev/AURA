"""Safe provider and voice-loop diagnostics for A.U.R.A."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
import asyncio
import base64
import shutil
import subprocess
import tempfile
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from . import capabilities, hermes_client, identity, tts
from .expo_content import EXPO_GUEST_GREETING_TEXT, EXPO_INTRO_TEXT
from .general_reasoning import GENERAL_PROVIDER_TIMEOUT_SECONDS, build_general_helpful_fallback, is_general_question
from .memory import get_memory_manager
from .tools import connectors, tool_router

ROOT = Path(__file__).resolve().parents[1]
DIAG_DIR = ROOT / "diagnostics"
DIAG_LOG = DIAG_DIR / "diagnostics.log"
LATEST_PATH = DIAG_DIR / "latency_latest.json"
VOICE_BENCH_MEMORY = DIAG_DIR / "voice_bench_memory.jsonl"
PENDING_TOOL_CONTEXTS: dict[str, dict[str, Any]] = {}
PENDING_TOOL_TTL_MS = 30_000

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_ROOT_URL = "https://api.elevenlabs.io"
FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"
FISH_AUDIO_ROOT_URL = "https://api.fish.audio"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_ROOT_URL = "https://api.cartesia.ai"

_last_successful_tts_client: dict[str, Any] = {}
_tts_audio_cache: dict[str, dict[str, Any]] = {}
_cartesia_tts_health: dict[str, Any] = {"last_error_type": None, "consecutive_failures": 0, "first_failure_at": 0.0, "circuit_until": 0.0}
_background_memory_tasks: set[asyncio.Task] = set()
_DETERMINISTIC_FAST_PATH_ROUTES = {
    "deterministic_wake",
    "deterministic_runtime_control",
    "deterministic_capability",
    "deterministic_smalltalk",
    "deterministic_expo_demo",
    "deterministic_expo_department_projects",
    "deterministic_cpr_awareness",
    "deterministic_expo_intro",
    "deterministic_expo_guest_greeting",
    "deterministic_memory",
    "deterministic_memory_recall",
    "deterministic_tool_command",
}
_TTS_CACHE_PHRASES = {
    "awake, sir.",
    "microphone muted, sir.",
    "microphone active, sir.",
    "standby, sir.",
    "camera is off, sir.",
    "camera is active, sir.",
    "tracking is active, sir.",
    "running smoothly, sir.",
    "i'm aura, sir.",
    "i’m aura, sir.",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expo_browser_fallback_allowed() -> bool:
    return os.environ.get("AURA_EXPO_ALLOW_BROWSER_TTS_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}


def _deterministic_tts_deferred() -> bool:
    return os.environ.get("AURA_DETERMINISTIC_TTS_DEFERRED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _deterministic_fast_path(provider_route: str, brain: dict[str, Any]) -> bool:
    if provider_route not in _DETERMINISTIC_FAST_PATH_ROUTES:
        return False
    if provider_route == "deterministic_tool_command":
        tool_result = brain.get("tool_result") or {}
        tool_name = str(tool_result.get("tool") or "").lower()
        return tool_name not in {"weather", "news", "browser"}
    return True


def _track_background_memory_task(task: asyncio.Task) -> None:
    _background_memory_tasks.add(task)
    task.add_done_callback(_background_memory_tasks.discard)


def _schedule_voice_turn_memory(
    *,
    memory_manager: Any,
    conversation_id: str,
    raw: str,
    normalized: str,
    input_source: str,
    frontend_kind: str,
    response_text: str,
    provider_route: str,
    intent_detected: str,
    failure_class: str | None,
    tool_name: str,
    tool_status: str | None,
    request_id: Any,
    tool_action: Any,
    fallback_reason: Any,
) -> None:
    async def _persist() -> None:
        try:
            user_write = await asyncio.to_thread(
                memory_manager.record_user_turn,
                conversation_id=conversation_id,
                content=raw,
                normalized_content=normalized,
                input_source=input_source,
                frontend_kind=frontend_kind,
                metadata={"request_id": request_id, "conversation_mode": "voice_turn"},
            )
            assistant_write = await asyncio.to_thread(
                memory_manager.record_assistant_turn,
                conversation_id=conversation_id,
                content=response_text,
                provider_route=provider_route,
                frontend_kind=frontend_kind,
                intent_detected=intent_detected,
                tool_name=tool_name,
                failure_class=failure_class,
                actions_returned=0,
                actions_executed=[],
                metadata={"request_id": request_id, "fallback_reason": fallback_reason, "tool_action": tool_action},
            )
            tool_write = None
            if tool_name and tool_status:
                tool_write = await asyncio.to_thread(
                    memory_manager.record_tool_event,
                    conversation_id=conversation_id,
                    event_status=tool_status,
                    tool_name=tool_name,
                    intent_detected=intent_detected,
                    failure_class=failure_class,
                    metadata={"request_id": request_id, "actions_returned": 0, "tool_action": tool_action},
                )
            writes = [user_write, assistant_write] + ([tool_write] if tool_write else [])
            successful = all(bool(item.get("primary_ok", item.get("ok"))) for item in writes)
            if successful:
                _log_safe("persistent_write_success", {
                    "backend": memory_manager.memory_status().get("backend"),
                    "messages": 2,
                    "facts": int(user_write.get("facts_saved") or 0),
                })
            else:
                failed = next((item for item in writes if not bool(item.get("primary_ok", item.get("ok")))), {})
                _log_safe("persistent_write_failed", {
                    "backend": memory_manager.memory_status().get("backend"),
                    "safe_reason": str(failed.get("error") or "memory_write_failed")[:120],
                })
        except Exception as exc:
            _log_safe("persistent_write_failed", {
                "backend": memory_manager.memory_status().get("backend"),
                "safe_reason": type(exc).__name__,
            })

    task = asyncio.create_task(_persist())
    _track_background_memory_task(task)
    _log_safe("persistent_write_scheduled", {"backend": memory_manager.memory_status().get("backend")})


def _safe_recent_turns(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    turns: list[dict[str, str]] = []
    for item in value[-8:]:
        if not isinstance(item, dict):
            continue
        turns.append({
            "user": str(item.get("normalized_text") or item.get("user_text") or "")[:500],
            "assistant": str(item.get("aura_response") or item.get("spoken_text") or "")[:700],
            "provider": str(item.get("provider") or "")[:80],
            "identity_summary": str(item.get("identity_summary") or "")[:240],
            "vision_summary": str(item.get("vision_summary") or "")[:240],
        })
    return [turn for turn in turns if turn["user"] or turn["assistant"]]


def _compact_identity_summary(context: Any) -> str:
    if not isinstance(context, dict):
        return "identity unavailable"
    return (
        f"decision={context.get('decision', 'unknown')}; "
        f"role={context.get('role', 'guest')}; "
        f"address={context.get('address', 'Guest')}; "
        f"preferred_address={context.get('preferred_address', 'none')}; "
        f"matched_profile={context.get('matched_profile_name') or context.get('matched_profile_id') or 'none'}; "
        f"people_count={context.get('people_count', 'unknown')}; "
        f"tracking_mode={context.get('tracking_mode', 'unknown')}"
    )


def _compact_vision_summary(context: Any) -> str:
    if not isinstance(context, dict):
        return "vision unavailable"
    raw_tracks = context.get("tracks")
    tracks = raw_tracks if isinstance(raw_tracks, list) else []
    safe_tracks = [
        {
            "track_id": str(track.get("track_id", ""))[:40],
            "status": str(track.get("status", ""))[:40],
            "identity": str(track.get("identity", "guest"))[:40],
            "address": str(track.get("address", "Guest"))[:40],
            "visual_estimate": str(track.get("visual_estimate", "unknown"))[:40],
            "gender_confidence": track.get("gender_confidence", 0),
        }
        for track in tracks
        if isinstance(track, dict)
    ]
    return json.dumps({
        "camera_status": context.get("camera_status", "unknown"),
        "tracking_status": context.get("tracking_status", "unknown"),
        "people_count": context.get("people_count", 0),
        "current_identity": context.get("current_identity", "guest"),
        "address_selected": context.get("address_selected", "Guest"),
        "fusion_decision": context.get("fusion_decision", "unknown"),
        "tracking_mode": context.get("tracking_mode", "unknown"),
        "tracks": safe_tracks[:6],
    }, ensure_ascii=True)


def _safe_vision_context(context: Any) -> dict[str, Any]:
    if isinstance(context, dict):
        people_tracking_state = context.get("people_tracking_state") or context.get("tracking_state") or context.get("tracking_status") or "off"
        gesture_tracking_state = context.get("gesture_tracking_state") or "off"
        identity_state = context.get("identity_state") or context.get("current_identity") or "guest"
        return {
            "camera_state": context.get("camera_state") or context.get("camera_status") or "off",
            "camera_status": context.get("camera_status") or "off",
            "stream_ready": bool(context.get("stream_ready", False)),
            "video_ready": bool(context.get("video_ready", False)),
            "video_width": int(context.get("video_width") or 0),
            "video_height": int(context.get("video_height") or 0),
            "gesture_tracking_state": gesture_tracking_state,
            "gesture_tracking_error": context.get("gesture_tracking_error") if isinstance(context.get("gesture_tracking_error"), dict) else None,
            "people_tracking_state": people_tracking_state,
            "people_tracking_error": context.get("people_tracking_error") if isinstance(context.get("people_tracking_error"), dict) else (context.get("tracking_error") if isinstance(context.get("tracking_error"), dict) else None),
            "tracking_state": people_tracking_state,
            "tracking_status": context.get("tracking_status") or "off",
            "tracking_started_at": context.get("tracking_started_at"),
            "tracking_error": context.get("tracking_error") if isinstance(context.get("tracking_error"), dict) else None,
            "tracking_last_error_code": context.get("tracking_last_error_code"),
            "tracking_last_error_message": context.get("tracking_last_error_message"),
            "tracking_backend": context.get("tracking_backend"),
            "model_loaded": bool(context.get("model_loaded", False)),
            "loop_running": bool(context.get("loop_running", False)),
            "people_count": int(context.get("people_count") or 0),
            "last_owner_verified_at": context.get("last_owner_verified_at"),
            "last_person_seen_at": context.get("last_person_seen_at"),
            "last_known_people_count": int(context.get("last_known_people_count") or 0),
            "owner_verified": bool(context.get("owner_verified")),
            "matched_profile_id": context.get("matched_profile_id"),
            "matched_profile_name": context.get("matched_profile_name"),
            "matched_profile": context.get("matched_profile") if isinstance(context.get("matched_profile"), dict) else None,
            "preferred_address": context.get("preferred_address"),
            "identity_confidence": context.get("identity_confidence"),
            "identity_state": identity_state,
            "current_identity": context.get("current_identity") or "guest",
            "address_selected": context.get("address_selected") or "Guest",
            "address_source": context.get("address_source") or "default",
            "effective_address": context.get("effective_address") or context.get("address_selected") or "Sir",
            "live_identity_address_allowed": bool(context.get("live_identity_address_allowed", False)),
            "live_identity_fresh": bool(context.get("live_identity_fresh", False)),
            "active_profile_is_unambiguous": bool(context.get("active_profile_is_unambiguous", False)),
            "owner_profile_enrolled": bool(context.get("owner_profile_enrolled", context.get("owner_face_enrolled", False))),
            "visual_gender_inference": False,
            "descriptors_exposed": False,
            "raw_frames_uploaded": False,
            "fusion_decision": context.get("fusion_decision"),
            "tracking_mode": context.get("tracking_mode") or "all",
            "tracks": context.get("tracks") if isinstance(context.get("tracks"), list) else [],
        }
    return {
        "camera_state": "off",
        "camera_status": "off",
        "stream_ready": False,
        "video_ready": False,
        "video_width": 0,
        "video_height": 0,
        "gesture_tracking_state": "off",
        "gesture_tracking_error": None,
        "people_tracking_state": "off",
        "people_tracking_error": None,
        "tracking_state": "off",
        "tracking_status": "off",
        "tracking_started_at": None,
        "tracking_error": None,
        "tracking_last_error_code": None,
        "tracking_last_error_message": None,
        "tracking_backend": "none",
        "model_loaded": False,
        "loop_running": False,
        "people_count": 0,
        "last_owner_verified_at": None,
        "last_person_seen_at": None,
        "last_known_people_count": 0,
        "owner_verified": False,
        "matched_profile_id": None,
        "matched_profile_name": None,
        "matched_profile": None,
        "preferred_address": None,
        "identity_confidence": None,
        "identity_state": "guest",
        "current_identity": "guest",
        "address_selected": "Guest",
        "address_source": "default",
        "effective_address": "Sir",
        "live_identity_address_allowed": False,
        "live_identity_fresh": False,
        "active_profile_is_unambiguous": False,
        "owner_profile_enrolled": False,
        "visual_gender_inference": False,
        "descriptors_exposed": False,
        "raw_frames_uploaded": False,
        "fusion_decision": None,
        "tracking_mode": "all",
        "tracks": [],
    }


def _vision_question_response(text: str, vision_context: Any, address_override: str | None = None) -> str | None:
    from .vision_commands import detect_live_camera_address_intent, detect_sensitive_vision_attribute, is_vision_command, needs_vision_clarification, normalize_vision_stt
    normalized = normalize_vision_stt(text)
    vision_context = _safe_vision_context(vision_context)
    address = str(address_override or vision_context.get("address_selected") or "Sir")
    suffix = _address_suffix(address)
    sensitive_attribute = detect_sensitive_vision_attribute(normalized)
    if sensitive_attribute == "gender_inference":
        return (
            f"No, {address}. I can detect people count and recognize enrolled profiles locally, "
            "but I do not infer gender from the live camera feed. Addressing such as Sir or Ma'am "
            "comes only from explicit saved profile preferences."
        )
    address_intent = detect_live_camera_address_intent(normalized)
    if address_intent == "address_visible_person":
        return _address_visible_person_response(vision_context, address)
    if needs_vision_clarification(normalized):
        return f"I may have misheard that, {address}. Did you mean, can I see you through the camera?"
    if not is_vision_command(text):
        return None
    camera = str(vision_context.get("camera_status") or "off")
    tracking = str(vision_context.get("tracking_status") or "off")
    people = int(vision_context.get("people_count") or 0)
    owner_verified = bool(vision_context.get("owner_verified") or str(vision_context.get("current_identity")).lower() == "owner")
    tracks = vision_context.get("tracks") if isinstance(vision_context.get("tracks"), list) else []

    camera_active = camera in ("online", "active", "on")
    tracking_active = tracking in ("on", "active")
    if not camera_active:
        return f"Camera is off{suffix}."
    if not tracking_active:
        return f"Camera is online, but people tracking is not active{suffix}."

    if any(term in normalized for term in ("spectacles", "glasses")):
        if people > 0 or owner_verified:
            return f"I can see you{suffix}, but reliable glasses detection is not enabled yet."
        return f"I do not see anyone clearly in the camera feed{suffix}."

    if any(term in normalized for term in ("hand", "palm", "finger")):
        if people > 0 or owner_verified:
            return f"I can see you{suffix}, but I do not have reliable hand detection enabled."
        return f"I do not see anyone clearly in the camera feed{suffix}, and reliable hand detection is not enabled."

    if "who am i" in normalized:
        if str(vision_context.get("current_identity")) == "owner":
            return f"You are the owner, {address}."
        return "I can see you, but I cannot verify you as the owner yet."

    if "who is here" in normalized or "what do you see" in normalized:
        owner = any(isinstance(track, dict) and track.get("identity") == "owner" for track in tracks)
        guests = max(0, people - (1 if owner else 0))
        if people == 0:
            return f"I do not see anyone clearly in the camera feed, {address}."
        if owner:
            return f"I can see {people} {'person' if people == 1 else 'people'}. Owner verified, {address}. {guests} guests detected."
        return f"I can see {people} {'person' if people == 1 else 'people'} in the camera feed, {address}."

    if _is_people_count_intent(normalized):
        if people == 1:
            return f"I can see 1 person{suffix}."
        if people > 1:
            return f"I can see {people} people{suffix}."
        return f"I do not see anyone clearly in the camera feed{suffix}."

    # Visibility / border / frame / position questions
    is_border_or_frame_question = any(term in normalized for term in ("border", "frame", "inside", "outside", "check the person", "visible or not", "person visible", "am i visible", "face visible"))
    if is_border_or_frame_question:
        if people > 0:
            has_bbox = any(isinstance(t, dict) and ("bbox" in t or "box" in t or "bounds" in t) for t in tracks)
            if has_bbox:
                return f"Yes, {address}. The person is inside the camera frame."
            return f"Yes, {address}. I can see a person, but I cannot reliably judge the frame border from this context."
        return f"I do not see anyone clearly in the camera feed, {address}."

    if people > 0 or owner_verified:
        visible_people = max(people, 1 if owner_verified else 0)
        if owner_verified and "see me" in normalized:
            return f"Yes{suffix}. I can see you in the camera feed."
        return f"Yes, {address}. I can see {visible_people} {'person' if visible_people == 1 else 'people'} in the camera feed."
    return f"I do not see anyone clearly in the camera feed, {address}."


def _address_visible_person_response(vision_context: Any, address: str) -> str:
    from . import identity

    context = _safe_vision_context(vision_context)
    camera = str(context.get("camera_status") or context.get("camera_state") or "off").lower()
    people_tracking_state = str(context.get("people_tracking_state") or context.get("tracking_state") or context.get("tracking_status") or "off").lower()
    identity_state = str(context.get("identity_state") or context.get("current_identity") or "guest").lower()
    people = int(context.get("people_count") or context.get("last_known_people_count") or 0)
    matched_profile = context.get("matched_profile") if isinstance(context.get("matched_profile"), dict) else None
    if not matched_profile and (context.get("matched_profile_id") or context.get("preferred_address")):
        matched_profile = {
            "id": context.get("matched_profile_id"),
            "display_name": context.get("matched_profile_name"),
            "preferred_address": context.get("preferred_address"),
            "address_source": context.get("address_source") or "explicit_profile",
        }
    identity_summary = {
        **context,
        "identity_state": identity_state,
        "people_tracking_state": people_tracking_state,
        "matched_profile": matched_profile,
    }
    resolved = identity.resolve_effective_address(identity_summary, default_address="Sir")
    print("[AURA IDENTITY] address_visible_person requested")
    print(f"[AURA IDENTITY] people_tracking_state={people_tracking_state} identity_state={identity_state} people={people}")
    if bool(resolved.get("live_identity_address_allowed")) and resolved.get("address_source") == "explicit_profile_live_identity":
        print(
            f"[AURA IDENTITY] matched_profile={resolved.get('matched_profile_name') or 'matched'} "
            f"preferred_address={resolved.get('effective_address')} source=explicit_profile_live_identity"
        )
        print("[AURA IDENTITY] visual_gender_inference=false")
        return f"Hello, {resolved.get('effective_address') or address}."

    reason = "people_tracking_not_active"
    if camera not in {"online", "active", "on"}:
        reason = "camera_not_active"
        print(f"[AURA IDENTITY] live_identity_address_allowed=false reason={reason}")
        print("[AURA IDENTITY] visual_gender_inference=false")
        return f"Camera is not active, {address}. I need the local camera feed and an enrolled profile to address the visible person."
    if people_tracking_state != "active":
        print(f"[AURA IDENTITY] live_identity_address_allowed=false reason={reason}")
        print("[AURA IDENTITY] visual_gender_inference=false")
        return f"Camera is available, {address}, but people tracking is not active, so I cannot verify a matched profile to address."
    if people > 1 and not bool(context.get("active_profile_is_unambiguous")):
        reason = "multiple_people_no_unambiguous_match"
        print(f"[AURA IDENTITY] live_identity_address_allowed=false reason={reason}")
        print("[AURA IDENTITY] visual_gender_inference=false")
        return f"I can see multiple people, {address}, but I cannot choose an address without a single matched enrolled profile."
    if not matched_profile:
        reason = "no_matched_profile"
    elif not matched_profile.get("preferred_address"):
        reason = "matched_profile_missing_preferred_address"
    elif not bool(context.get("live_identity_fresh")):
        reason = "stale_identity_match"
    elif identity_state not in {"matched", "owner", "owner_verified_face_only", "owner_verified_high", "owner_verified_voice_only"}:
        reason = "identity_not_matched"
    print(f"[AURA IDENTITY] live_identity_address_allowed=false reason={reason}")
    print("[AURA IDENTITY] visual_gender_inference=false")
    person_word = "person" if people == 1 else "people"
    visible = f"I can see {people} {person_word} in the camera feed" if people > 0 else "I do not see anyone clearly in the camera feed"
    return (
        f"{visible}, {address}, but I cannot choose Sir or Ma'am from appearance. "
        "I can address them only if they are enrolled with an explicit preferred address."
    )


def _address_visible_person_resolution_reason(command_text: str, vision_context: Any, address_reason: str) -> str | None:
    from .vision_commands import vision_match_reason

    context = _safe_vision_context(vision_context)
    if address_reason == "explicit_profile_live_identity":
        return "explicit_profile_live_identity"
    camera = str(context.get("camera_status") or context.get("camera_state") or "off").lower()
    people_tracking_state = str(context.get("people_tracking_state") or context.get("tracking_state") or context.get("tracking_status") or "off").lower()
    identity_state = str(context.get("identity_state") or context.get("current_identity") or "guest").lower()
    people = int(context.get("people_count") or context.get("last_known_people_count") or 0)
    matched_profile = context.get("matched_profile") if isinstance(context.get("matched_profile"), dict) else None
    if vision_match_reason(command_text) != "vision_identity.address_visible_person":
        return None
    if camera not in {"online", "active", "on"}:
        return "camera_not_active"
    if people_tracking_state != "active":
        return "people_tracking_not_active"
    if people > 1 and not bool(context.get("active_profile_is_unambiguous")):
        return "multiple_people_no_unambiguous_match"
    if not matched_profile and not (context.get("matched_profile_id") or context.get("preferred_address")):
        return "no_matched_profile"
    if not (matched_profile or {}).get("preferred_address") and not context.get("preferred_address"):
        return "matched_profile_missing_preferred_address"
    if not bool(context.get("live_identity_fresh")):
        return "stale_identity_match"
    if identity_state not in {"matched", "owner", "owner_verified_face_only", "owner_verified_high", "owner_verified_voice_only"}:
        return "identity_not_matched"
    return "address_gate_not_allowed"


def _vision_followup_response(text: str, vision_context: Any, address_override: str | None = None) -> str | None:
    from .vision_commands import normalize_vision_stt
    normalized = normalize_vision_stt(text)
    vision_context = _safe_vision_context(vision_context)
    address = str(address_override or vision_context.get("address_selected") or "Sir")

    current_people = int(vision_context.get("people_count") or 0)
    last_known = int(vision_context.get("last_known_people_count") or 0)
    people_eval = max(current_people, last_known)

    if "missed one" in normalized or "missed 1" in normalized:
        return f"Understood, {address}. The tracker may have missed one person. I will re-check the camera feed."

    if any(p in normalized for p in ("2 people", "2 persons", "two people", "two persons", "count is 2", "people count is 2", "where 2 peoples", "were 2 people")):
        if people_eval == 2:
            return f"You're right, {address}. I now have 2 people in the camera feed."
        return f"Understood, {address}. The visual tracker currently reports {people_eval} {'person' if people_eval == 1 else 'people'}, but I will treat your correction as 2 people for this moment."

    if "3 people" in normalized or "three people" in normalized or "count is 3":
        if people_eval == 3:
            return f"You're right, {address}. I now have 3 people in the camera feed."
        return f"Understood, {address}. The visual tracker currently reports {people_eval} {'person' if people_eval == 1 else 'people'}, but I will treat your correction as 3 people for this moment."

    return f"Understood, {address}. I have updated the vision tracking context based on your correction."




def _is_people_count_intent(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    patterns = (
        "how many person",
        "how many persons",
        "how many people",
        "how many users",
        "how many faces",
        "how many are there",
        "how many do you see",
        "how many on your camera",
        "how many persons on your camera",
        "how many persons on your camera phone",
        "count people",
        "count persons",
        "people count",
        "persons count",
    )
    return any(pattern in normalized for pattern in patterns)


def _parse_context_timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp() * 1000
    except Exception:
        return None


def _apply_recent_owner_context(identity_context: Any, vision_context: dict[str, Any]) -> tuple[Any, dict[str, Any], bool, int | None]:
    id_ctx = dict(identity_context) if isinstance(identity_context, dict) else identity_context
    vis_ctx = dict(vision_context)
    stamp = None
    if isinstance(id_ctx, dict):
        stamp = id_ctx.get("last_owner_verified_at")
    stamp = stamp or vis_ctx.get("last_owner_verified_at")
    parsed = _parse_context_timestamp(stamp)
    age_ms = round(time.time() * 1000 - parsed) if parsed is not None else None
    people_tracking_state = str(vis_ctx.get("people_tracking_state") or vis_ctx.get("tracking_state") or vis_ctx.get("tracking_status") or "off").lower()
    used = bool(people_tracking_state in {"active", "on"} and age_ms is not None and 0 <= age_ms < 8000)
    last_known = max(int(vis_ctx.get("last_known_people_count") or 0), int(vis_ctx.get("people_count") or 0))
    if used:
        vis_ctx["people_count"] = max(1, int(vis_ctx.get("people_count") or 0), last_known)
        vis_ctx["owner_verified"] = True
        vis_ctx["current_identity"] = "owner"
        if isinstance(id_ctx, dict):
            id_ctx["people_count"] = max(1, int(id_ctx.get("people_count") or 0), last_known)
            id_ctx["owner_verified"] = True
            id_ctx["owner_live_verified"] = True
            id_ctx["current_identity"] = "owner"
            id_ctx["role"] = "owner"
    return id_ctx, vis_ctx, used, age_ms


def _is_vision_intent(text: str) -> bool:
    from .vision_commands import is_vision_command
    return is_vision_command(text)


def _normalize_runtime_control_text(text: str) -> str:
    normalized = " ".join(str(text or "").lower().strip().split())
    normalized = re.sub(r"[.,!?;:-]+", " ", normalized)
    normalized = " ".join(normalized.split())
    # Grammar normalization: "an general" → "a general"
    normalized = re.sub(r"\ban\b\s+(general|giant|unique|university|European|one)", r"a \1", normalized)
    replacements = (
        ("meet the mic", "mute the mic"),
        ("meet mic", "mute mic"),
        ("meet the microphone", "mute the microphone"),
        ("meet microphone", "mute microphone"),
        ("meet my mic", "mute mic"),
        ("meet my microphone", "mute microphone"),
        ("mute the mike", "mute the mic"),
        ("mute mike", "mute mic"),
        ("mute my mic", "mute mic"),
        ("mute my microphone", "mute microphone"),
        ("crhome", "chrome"),
        ("crome", "chrome"),
        ("chrom", "chrome"),
        ("chrome tap", "chrome tab"),
    )
    for old, new in replacements:
        normalized = re.sub(rf"\b{re.escape(old)}\b", new, normalized)
    normalized = " ".join(normalized.split())
    if re.fullmatch(r"(?:can you |could you |please |can you perhaps )?(?:mute|turn off|stop|disable)\s+(?:the |my )?(?:mic|microphone)(?:\s+(?:for a second|for one second|for a moment|now))?", normalized):
        return "mute mic"
    return normalized


FAILURE_CLASSES = {
    "local_command_unrecognized",
    "local_tool_unsupported",
    "local_tool_failed",
    "local_action_dispatched_unverified",
    "deterministic_followup_failure",
    "cloud_reasoning_failed",
    "unclear_input",
    "weather_location_not_found",
    "weather_location_too_broad",
    "weather_provider_config_missing",
    "weather_provider_timeout",
    "weather_provider_error",
    "news_provider_config_missing",
    "news_provider_timeout",
    "news_provider_error",
    "news_no_results",
}

COMMAND_LIKE_RE = re.compile(
    r"\b(open|close|launch|start|stop|mute|unmute|search|find|show|check|read|copy|paste|screenshot|remind|schedule|lock|volume|brightness|battery|cpu|ram|disk|network|git)\b",
    re.IGNORECASE,
)
UNCLEAR_RE = re.compile(r"\b(uhh?|umm?|maybe|that one|there)\b", re.IGNORECASE)
FOLLOWUP_FAILURE_RE = re.compile(
    r"\b(no new tab opened|it did not open|nothing opened|nothing happened|not opened yet|failed|didn'?t work|not working)\b",
    re.IGNORECASE,
)
CITY_FOLLOWUP_RE = re.compile(r"^[a-zA-Z][a-zA-Z .'-]{1,40}$")
KNOWN_CITY_FOLLOWUPS = {
    "mumbai", "paris", "chennai", "london", "coimbatore", "bangalore", "bengaluru",
    "delhi", "new delhi", "hyderabad", "pune", "kolkata",
}


def _pending_key(conversation_id: str) -> str:
    return str(conversation_id or "default")


def _prune_pending_tool_contexts(now_ms: int | None = None) -> None:
    now = now_ms if now_ms is not None else round(time.time() * 1000)
    expired = [
        key for key, value in PENDING_TOOL_CONTEXTS.items()
        if now - int(value.get("created_at") or 0) > int(value.get("ttl_ms") or PENDING_TOOL_TTL_MS)
    ]
    for key in expired:
        PENDING_TOOL_CONTEXTS.pop(key, None)


def _city_like_short_phrase(text: str) -> str | None:
    norm = _normalize_runtime_control_text(text).strip()
    if not norm or len(norm.split()) > 3 or not CITY_FOLLOWUP_RE.fullmatch(norm):
        return None
    if norm in KNOWN_CITY_FOLLOWUPS:
        return " ".join(part.capitalize() for part in norm.split())
    if len(norm.split()) == 1 and 3 <= len(norm) <= 24 and norm not in {"hello", "thanks", "news", "weather", "aura"}:
        return norm.capitalize()
    return None


def _consume_pending_tool_context(conversation_id: str, text: str) -> tuple[str, dict[str, Any] | None]:
    _prune_pending_tool_contexts()
    city = _city_like_short_phrase(text)
    if not city:
        return text, None
    pending = PENDING_TOOL_CONTEXTS.pop(_pending_key(conversation_id), None)
    if not pending or pending.get("reason") != "location_clarification":
        return text, None
    tool = str(pending.get("tool") or "")
    if tool == "weather":
        return f"weather in {city}", pending
    if tool == "news":
        return f"news in {city}", pending
    return text, None


def _store_pending_tool_context(conversation_id: str, *, tool: str, reason: str, original_query: str) -> dict[str, Any]:
    context = {
        "tool": tool,
        "reason": reason,
        "created_at": round(time.time() * 1000),
        "ttl_ms": PENDING_TOOL_TTL_MS,
        "original_query": str(original_query or "")[:160],
    }
    PENDING_TOOL_CONTEXTS[_pending_key(conversation_id)] = context
    return context


def _is_command_like_local_phrase(text: str) -> bool:
    norm = _normalize_runtime_control_text(text)
    if not norm:
        return False
    explicit_phrases = (
        "open new tab", "open chrome", "new chrome window", "check battery",
        "check cpu", "check ram", "system status", "mute mic", "take screenshot",
        "search file", "open app", "remind me", "git status",
        "open calculator", "open notepad", "open powershell", "open file explorer",
        "find file", "find report", "find pdf", "find image", "find photo",
        "recent files", "show recent", "clipboard", "copy text", "read clipboard",
        "create reminder", "add reminder", "list reminders", "my reminders",
        "what is my cpu", "cpu usage", "how much ram", "memory info",
        "disk info", "storage info", "network status", "running apps",
        "volume up", "volume down", "volume mute", "brightness",
        "lock laptop", "shutdown", "restart", "clean temp",
        "open terminal", "open cmd", "open explorer", "open vscode",
        "open vs code", "open chrome and search", "search google",
        "search web", "search wikipedia", "search youtube",
        "take snapshot", "screenshot now", "capture screen",
        "close app", "close notepad", "kill process",
        "npm build", "python compile", "which branch", "open this project",
        "copy this text", "what is on clipboard",
    )
    return any(phrase in norm for phrase in explicit_phrases) or bool(COMMAND_LIKE_RE.search(norm))


def _is_unclear_input(text: str) -> bool:
    norm = _normalize_runtime_control_text(text)
    if not norm:
        return True
    words = norm.split()
    return len(words) <= 6 and bool(UNCLEAR_RE.search(norm))


def _is_failed_local_action_followup(text: str) -> bool:
    return bool(FOLLOWUP_FAILURE_RE.search(_normalize_runtime_control_text(text)))


def _memory_command_intent(text: str) -> str | None:
    normalized = _normalize_runtime_control_text(text)
    if not normalized:
        return None
    if (
        "what did i tell you about tomorrow" in normalized
        or "what did i say about tomorrow" in normalized
        or "recall my last memory" in normalized
        or normalized == "what do you remember"
        or normalized == "what did i tell you"
    ):
        return "memory.recall_query"
    if "show memory facts" in normalized or normalized == "memory facts":
        return "memory.fact_list"
    if "memory context" in normalized or "context preview" in normalized:
        return "memory.context_preview"
    if "are you storing this conversation" in normalized or "did you save this conversation" in normalized:
        return "memory.write_status"
    if normalized in {"check your persistent memory", "check memory", "check supabase memory", "memory status", "persistent memory status"}:
        return "memory.status_check"
    return None


def _memory_command_response(intent: str, manager: Any, context: Any, address: str) -> str:
    status = manager.memory_status()
    backend = str(status.get("backend") or "disabled")
    configured = bool(status.get("supabase_configured"))
    primary_read_ok = bool(manager.last_read_primary_ok)
    suffix = _address_suffix(address)
    if intent == "memory.fact_list":
        facts = context.facts
        if not facts:
            return f"I checked persistent memory{suffix}, but I could not find any saved facts."
        return f"I found {len(facts)} saved memory fact{'s' if len(facts) != 1 else ''}{suffix}."
    if intent == "memory.context_preview":
        if context.context_text:
            return f"Persistent memory context is available{suffix}, with {len(context.facts)} saved fact{'s' if len(context.facts) != 1 else ''}."
        return f"Persistent memory context is empty{suffix}."
    if backend == "disabled":
        return f"Persistent memory is disabled{suffix}."
    if backend == "supabase" and configured and not primary_read_ok:
        return f"Persistent memory is configured{suffix}, but Supabase readback is currently failing. Local fallback status is separate."
    write_status = str(status.get("last_write_status") or "not_attempted")
    write_phrase = "last write succeeded" if write_status == "success" else ("last write failed" if write_status == "failed" else "no write has been attempted yet")
    if intent == "memory.write_status":
        return f"Conversation storage is enabled{suffix}. Backend is {backend}, Supabase configured is {str(configured).lower()}, and {write_phrase}."
    return f"Persistent memory is enabled{suffix}. Backend is {backend}, Supabase is configured, and {write_phrase}. I can also run a readback check."


def _expo_intro_intent(text: str) -> bool:
    normalized = _normalize_runtime_control_text(text)
    return any(phrase in normalized for phrase in (
        "start expo intro",
        "begin expo intro",
        "engineers week intro",
        "project expo intro",
        "welcome staff",
        "expo welcome",
    ))


def _expo_guest_greeting_intent(text: str) -> bool:
    normalized = _normalize_runtime_control_text(text)
    return any(phrase in normalized for phrase in (
        "guest greeting",
        "trustee greeting",
        "principal greeting",
        "welcome trustee",
        "welcome principal",
        "welcome trustee and principal",
        "start guest greeting",
        "start principal greeting",
    ))


GENERAL_CONVERSATION_PHRASES = [
    "can we have a general talk",
    "can we have an general talk",
    "general talk",
    "let's have a talk",
    "lets have a talk",
    "can we talk",
    "can we chat",
    "let's chat",
    "talk with me",
    "speak with me",
    "start general conversation",
]


def _general_conversation_intent(text: str) -> bool:
    normalized = _normalize_runtime_control_text(text)
    return any(phrase in normalized for phrase in GENERAL_CONVERSATION_PHRASES)


def _tool_failure_class(tool_result: dict[str, Any] | None, tool_detection: dict[str, Any] | None) -> str | None:
    if not tool_result:
        return None
    if tool_detection and tool_detection.get("failure_class") in FAILURE_CLASSES:
        return str(tool_detection.get("failure_class"))
    if tool_result.get("ok") is not False:
        return None
    risk = str(tool_result.get("risk_level") or "")
    error_type = str(tool_result.get("error_type") or "")
    if tool_result.get("tool") == "weather" and error_type in FAILURE_CLASSES:
        return error_type
    if tool_result.get("tool") == "news" and error_type in FAILURE_CLASSES:
        return error_type
    if risk == "blocked":
        return "local_tool_unsupported"
    if error_type == "confirmation_required":
        return "local_tool_unsupported"
    return "local_tool_failed"


def _failure_response_text(failure_class: str | None, *, address: str = "Sir", tool_result: dict[str, Any] | None = None, tool_name: str | None = None) -> str | None:
    if failure_class == "local_command_unrecognized":
        return "I understood this as a local command, Sir, but I do not have a safe tool route for it yet."
    if failure_class == "local_tool_unsupported":
        if tool_result and str(tool_result.get("risk_level") or "") == "blocked":
            return "I cannot perform that action, Sir."
        return "I understood the command, Sir, but that local action is not supported safely yet."
    if failure_class == "local_tool_failed":
        if tool_name == "system_monitor":
            return "I could not read system usage right now, Sir."
        if tool_result and tool_result.get("response_text"):
            return str(tool_result.get("response_text"))
        return "I could not complete that local tool action, Sir."
    if failure_class and failure_class.startswith("weather_"):
        if tool_result and tool_result.get("response_text"):
            return str(tool_result.get("response_text"))
        return "I could not complete that weather request, Sir."
    if failure_class and failure_class.startswith("news_"):
        if tool_result and tool_result.get("response_text"):
            return str(tool_result.get("response_text"))
        return "I could not complete that news request, Sir."
    if failure_class == "local_action_dispatched_unverified":
        return "I sent the command, Sir, but I could not verify that it completed."
    if failure_class == "deterministic_followup_failure":
        if tool_name == "browser":
            return "The previous browser action did not appear to complete, Sir. I can retry through the safe local tool route."
        return "The previous local action did not appear to complete, Sir. I can retry through the safe local tool route."
    if failure_class == "cloud_reasoning_failed":
        return "Cloud reasoning is unavailable right now, Sir."
    if failure_class == "unclear_input":
        return "I could not understand that clearly, Sir. Please repeat the command."
    return None


def _apply_resolved_address_text(text: str, address: str) -> str:
    clean = str(address or "").strip()
    if not clean or clean == "Sir":
        return text
    result = str(text or "")
    result = result.replace(", Sir.", f", {clean}.")
    result = result.replace(", Sir,", f", {clean},")
    result = result.replace(" Sir.", f" {clean}.")
    return result


def _runtime_control_response(text: str, address: str) -> dict[str, Any] | None:
    normalized = _normalize_runtime_control_text(text)
    suffix = _address_suffix(address)

    # CAMERA OFF
    if any(p in normalized for p in (
        "turn off the camera", "switch off the camera", "stop camera", "close camera",
        "disable camera", "camera off", "turn the camera off", "turn off camera",
        "switch off camera", "shut off camera", "shut off the camera",
        "turn off the camera now", "turn off camera now", "turn off the cam",
        "turn off cam", "stop the cam", "stop the camera"
    )):
        return {
            "response_text": f"Camera is off{suffix}.",
            "action_type": "camera.stop",
            "action_label": "Turn off camera",
            "intent": "runtime_control.camera.stop",
        }

    # CAMERA ON
    if any(p in normalized for p in (
        "turn on the camera", "switch on the camera", "start camera", "enable camera",
        "camera on", "open camera", "turn the camera on", "turn on camera", "switch on camera"
    )):
        return {
            "response_text": f"Turning on the camera{suffix}.",
            "action_type": "camera.start",
            "action_label": "Turn on camera",
            "intent": "runtime_control.camera.start",
        }

    # GESTURE TRACKING OFF
    if any(p in normalized for p in (
        "stop gesture control", "disable gesture control", "gesture control off",
        "stop gesture tracking", "turn off gesture tracking", "stop orb gesture tracking"
    )):
        return {
            "response_text": f"Stopping gesture control{suffix}.",
            "action_type": "gesture_tracking.stop",
            "action_label": "Turn off gesture control",
            "intent": "runtime_control.gesture_tracking.stop",
            "actions": [{
                "type": "gesture_tracking.stop",
                "target": "orb_gesture_tracking",
                "reason": "voice_command",
            }],
        }

    # GESTURE TRACKING ON
    if any(p in normalized for p in (
        "enable gesture control", "start gesture control", "gesture control on",
        "start gesture tracking", "enable gesture tracking", "start orb gesture tracking",
        "control orb with hand", "control the orb with hand", "control orb with gestures"
    )):
        return {
            "response_text": f"Starting gesture control{suffix}.",
            "action_type": "gesture_tracking.start",
            "action_label": "Turn on gesture control",
            "intent": "runtime_control.gesture_tracking.start",
            "actions": [{
                "type": "gesture_tracking.start",
                "target": "orb_gesture_tracking",
                "reason": "voice_command",
            }],
        }

    # PEOPLE TRACKING OFF
    if any(p in normalized for p in (
        "turn off live tracking", "stop live tracking", "disable live tracking", "live tracking off",
        "turn off tracking", "stop tracking", "disable tracking", "tracking off",
        "pause tracking", "turn tracking off", "stop following me",
        "stop people tracking", "turn off people tracking", "disable people tracking",
        "stop camera tracking", "stop identity tracking"
    )):
        return {
            "response_text": f"Stopping people tracking{suffix}.",
            "action_type": "people_tracking.stop",
            "action_label": "Turn off people tracking",
            "intent": "runtime_control.people_tracking.stop",
            "actions": [{
                "type": "people_tracking.stop",
                "target": "camera_people_identity_tracking",
                "reason": "voice_command",
            }],
        }

    # PEOPLE TRACKING ON
    if any(p in normalized for p in (
        "turn on live tracking", "start live tracking", "enable live tracking", "live tracking on",
        "turn on tracking", "start tracking", "enable tracking", "tracking on",
        "resume tracking", "activate tracking", "turn tracking on", "track me", "follow me",
        "turn on person tracking", "activate camera tracking", "activate live tracking",
        "enable people tracking", "start people tracking", "track people",
        "start camera tracking", "start identity tracking", "detect people",
        "turn on people tracking", "enable camera tracking", "enable identity tracking"
    )):
        return {
            "response_text": f"Starting people tracking{suffix}.",
            "action_type": "people_tracking.start",
            "action_label": "Turn on people tracking",
            "intent": "runtime_control.people_tracking.start",
            "requires_camera": True,
            "actions": [{
                "type": "people_tracking.start",
                "target": "camera_people_identity_tracking",
                "reason": "voice_command",
            }],
        }

    # MIC / LISTENING OFF
    if normalized == "mute mic" or any(p in normalized for p in (
        "mute mic", "mute microphone", "mic mute", "microphone mute", "turn off mic",
        "turn off microphone", "stop mic", "stop microphone", "disable mic", "disable microphone",
        "stop listening", "pause listening", "stop hearing", "mic off", "turn off the mic",
        "turn off the microphone", "stop the microphone", "turn the microphone off", "turn mic off",
        "mute the mic", "mute my mic", "mute the microphone", "mute my microphone",
        "can you mute the mic", "can you meet the mic", "meet the mic", "meet mic",
        "meet microphone", "meet the microphone"
    )):
        return {
            "response_text": f"Microphone muted{suffix}.",
            "action_type": "mic.stop",
            "action_label": "Turn off microphone",
            "intent": "runtime_control.mic.stop",
            "actions": [{
                "type": "mic.stop",
                "target": "microphone",
                "reason": "voice_command",
            }],
        }

    # MIC / LISTENING ON
    if any(p in normalized for p in (
        "unmute mic", "unmute microphone", "mic on", "microphone on", "turn on mic",
        "turn on microphone", "start mic", "start microphone", "enable mic", "enable microphone",
        "start listening", "listen again", "resume listening", "turn on the mic",
        "turn on the microphone", "listen continuously", "mic active"
    )):
        return {
            "response_text": f"Microphone active{suffix}.",
            "action_type": "mic.start",
            "action_label": "Turn on microphone",
            "intent": "runtime_control.mic.start",
            "actions": [{
                "type": "mic.start",
                "target": "microphone",
                "reason": "voice_command",
            }],
        }

    # STANDBY
    if any(p in normalized for p in (
        "go standby", "go to sleep", "sleep mode", "enter standby"
    )) or normalized == "standby":
        return {
            "response_text": f"Standing by{suffix}.",
            "action_type": "standby.enter",
            "action_label": "Enter standby",
            "intent": "runtime_control.standby.enter",
        }

    # WAKE
    if any(p in normalized for p in (
        "wake up", "hey aura", "aura wake up"
    )):
        return {
            "response_text": f"Awake{suffix}.",
            "action_type": "wake.enter",
            "action_label": "Wake up",
            "intent": "runtime_control.wake.enter",
        }

    # PANEL COMMANDS
    if any(p in normalized for p in ("show projects", "open projects")):
        return {
            "response_text": f"Opening projects panel{suffix}.",
            "action_type": "panel.projects.open",
            "action_label": "Open projects panel",
            "intent": "runtime_control.panel.projects.open",
        }
    if any(p in normalized for p in ("close projects", "hide projects")):
        return {
            "response_text": f"Closing projects panel{suffix}.",
            "action_type": "panel.projects.close",
            "action_label": "Close projects panel",
            "intent": "runtime_control.panel.projects.close",
        }
    if any(p in normalized for p in ("show tasks", "open tasks")):
        return {
            "response_text": f"Opening tasks panel{suffix}.",
            "action_type": "panel.tasks.open",
            "action_label": "Open tasks panel",
            "intent": "runtime_control.panel.tasks.open",
        }
    if any(p in normalized for p in ("close tasks", "hide tasks")):
        return {
            "response_text": f"Closing tasks panel{suffix}.",
            "action_type": "panel.tasks.close",
            "action_label": "Close tasks panel",
            "intent": "runtime_control.panel.tasks.close",
        }
    if any(p in normalized for p in ("open diagnostics", "show diagnostics")):
        return {
            "response_text": f"Opening diagnostics{suffix}.",
            "action_type": "diagnostics.open",
            "action_label": "Open diagnostics",
            "intent": "runtime_control.diagnostics.open",
        }

    # ORB BRIGHTNESS
    if "full brightness" in normalized or "brightness to 100" in normalized or "set brightness to 100" in normalized:
        return {
            "response_text": f"Setting orb brightness to 100 percent{suffix}.",
            "action_type": "orb.brightness.set",
            "action_label": "Set brightness to 100",
            "value": 100,
            "intent": "runtime_control.orb.brightness.set",
        }
    if "dim the orb" in normalized or "dim orb" in normalized or "decrease orb brightness" in normalized:
        return {
            "response_text": f"Dimming the orb{suffix}.",
            "action_type": "orb.brightness.decrease",
            "action_label": "Dim orb",
            "intent": "runtime_control.orb.brightness.decrease",
        }
    if "increase orb brightness" in normalized or "brighten orb" in normalized:
        return {
            "response_text": f"Increasing orb brightness{suffix}.",
            "action_type": "orb.brightness.increase",
            "action_label": "Increase orb brightness",
            "intent": "runtime_control.orb.brightness.increase",
        }

    return None


def _capability_response(text: str, address: str) -> str | None:
    normalized = " ".join(text.lower().strip().split())
    if _is_tamil_language_capability(normalized):
        return f"Tamil konjam puriyum{_address_suffix(address)}. I can understand simple Tamil phrases, but full Tamil voice and TTS mode still needs pronunciation validation."
    capability_state = capabilities.get_capability_state()
    email_connected = bool(capability_state.get("email", {}).get("configured"))
    cloud_connected = bool(capability_state.get("cloud_storage", {}).get("configured"))
    email_patterns = (
        "do you have any permission to draft an email",
        "permission to draft an email",
        "can you draft an email",
        "can you send email",
        "can you send an email",
        "can you access my email",
        "do you have gmail",
        "do you have email access",
        "create an email draft",
        "draft an email",
        "write an email",
        "send an email",
        "draught an email",
    )
    if any(pattern in normalized for pattern in email_patterns):
        if email_connected:
            return f"Email is connected{_address_suffix(address)}. I can create a draft, but I will not send it without your approval."
        return f"I do not have an email account connected{_address_suffix(address)}. I can write the email text here, but I cannot create or send a real email draft yet."
    cloud_patterns = (
        "do you have cloud account",
        "do you have cloud access",
        "can you access drive",
        "can you access google drive",
        "can you upload to cloud",
        "can you save to cloud",
        "cloud storage",
        "google drive",
    )
    if any(pattern in normalized for pattern in cloud_patterns):
        if cloud_connected:
            return f"Cloud storage is connected{_address_suffix(address)}."
        return f"I do not have cloud storage connected{_address_suffix(address)}. I can help prepare files or instructions here, but I cannot read, upload, or save to a cloud account yet."
    patterns = (
        "can you hear me",
        "can you hear",
        "are you hearing me",
        "are you listening",
        "are you listening me",
        "are you able to listen me",
        "are able to listen me",
        "are you able to listen to me",
        "can you listen me",
        "can you listen to me",
        "can you listen",
        "are you able to hear me",
        "do you hear me",
        "listen me",
        "to listen me",
        "mic working",
        "microphone working",
    )
    if any(pattern in normalized for pattern in patterns):
        return f"Yes{_address_suffix(address)}. I can hear you."
    return None


def _is_tamil_language_capability(text: str) -> bool:
    normalized = _normalize_tamil_request_text(text)
    patterns = (
        "tamil puriyuma",
        "tamil theriyuma",
        "tamil pesuviya",
        "tamil pesuva",
        "unaku tamil theriyuma",
        "unakku tamil theriyuma",
        "tamil understand",
        "do you understand tamil",
        "do you know tamil",
        "can you speak tamil",
        "can you understand tamil",
    )
    return any(pattern in normalized for pattern in patterns)


def _normalize_tamil_request_text(text: str) -> str:
    normalized = " ".join(str(text or "").lower().strip().split())
    replacements = (
        ("tamilla", "tamil la"),
        ("tamil ah", "tamil la"),
        ("tanglish la", "tamil la"),
        ("puriuma", "puriyuma"),
        ("theriuma", "theriyuma"),
        ("prema", "puriyuma"),
        ("eluththukal", "eluthukkal"),
        ("eluthukal", "eluthukkal"),
        ("ezhuthukkal", "eluthukkal"),
        ("ipo", "ippo"),
    )
    for old, new in replacements:
        normalized = normalized.replace(old, new)
    return " ".join(normalized.split())


def _tamil_language_response(text: str, address: str) -> dict[str, Any] | None:
    normalized = _normalize_tamil_request_text(text)
    if _is_tamil_language_capability(normalized):
        return None
    tamil_markers = (
        "tamil la", "tanglish", "uyir eluthukkal", "mei eluthukkal",
        "tamil letters", "tamil vowels", "tamil consonants",
    )
    if not any(marker in normalized for marker in tamil_markers):
        return None

    local_answer = ""
    intent = "language_response.tamil_unknown"
    if any(pattern in normalized for pattern in (
        "uyir eluthukkal ethanai", "uyir eluthukkal evlo", "tamil vowels how many",
    )):
        local_answer = "தமிழில் உயிரெழுத்துக்கள் 12, Sir."
        intent = "language_response.tamil_basic_knowledge"
    elif any(pattern in normalized for pattern in (
        "mei eluthukkal ethanai", "tamil consonants how many",
    )):
        local_answer = "தமிழில் மெய்யெழுத்துக்கள் 18, Sir."
        intent = "language_response.tamil_basic_knowledge"
    elif any(pattern in normalized for pattern in (
        "motham eluthukkal ethanai", "tamil letters ethanai", "tamil basic letters how many",
    )):
        local_answer = "தமிழில் அடிப்படை எழுத்துக்கள் 247 என்று பொதுவாகக் கணக்கிடப்படுகின்றன, Sir."
        intent = "language_response.tamil_basic_knowledge"
    elif any(pattern in normalized for pattern in (
        "tamil la unna intro kudu", "tamil la un intro sollu", "tamil la introduce yourself",
        "tamil la introduce pannu",
    )):
        local_answer = "வணக்கம், Sir. நான் A.U.R.A, உங்கள் உள்ளூர் டெஸ்க்டாப் உதவியாளர். குரல் கட்டளைகள், கருவிகள், காலநிலை, செய்திகள், கணினி நிலை, மற்றும் பாதுகாப்பான உள்ளூர் செயல்பாடுகளில் உதவுவேன்."
        intent = "language_response.tamil_intro"

    if local_answer:
        return {
            "response_text": local_answer[:300],
            "intent": intent,
            "requested_language": "tamil",
            "language_input_mode": "tanglish",
            "local_answer_used": True,
            "cloud_route_used": False,
            "tamil_tts_limited": len(local_answer) <= 300,
        }
    return {
        "response_text": f"I understood this as a Tamil request{_address_suffix(address)}, but cloud reasoning is unavailable and I do not have a local answer for it yet.",
        "intent": intent,
        "requested_language": "tamil",
        "language_input_mode": "tanglish",
        "local_answer_used": False,
        "cloud_route_used": False,
        "tamil_tts_limited": True,
    }


def _self_capability_response(
    text: str,
    address: str,
    identity_context: Any,
    vision_context: Any,
) -> str | None:
    normalized = " ".join(str(text or "").lower().strip().split())
    name_only_phrases = (
        "tell me your name",
        "first your name",
        "your name only",
        "name nothing else",
        "just your name",
    )
    if any(phrase in normalized for phrase in name_only_phrases):
        return "Aura."

    patterns = (
        "tell me about yourself",
        "about yourself",
        "introduce yourself",
        "who are you",
        "what are you",
        "what can you do",
        "mention what you can do",
        "explain your capabilities",
        "what are your abilities",
        "what can aura do",
        "what can a.u.r.a do",
        "your features",
        "your functions",
        "your purpose",
        "who made you",
    )
    if not any(pattern in normalized for pattern in patterns):
        return None

    suffix = _address_suffix(address)
    response = (
        f"I'm Aura, your local desktop companion{suffix}. "
        "I can listen, answer, control the runtime, manage camera and tracking, "
        "and use the live camera feed for owner awareness."
    )
    safe_vision = _safe_vision_context(vision_context)
    if int(safe_vision.get("people_count") or 0) > 0 or safe_vision.get("camera_status") in ("active", "online", "on"):
        response += " I can also see the active camera feed and track visible people."
    if isinstance(identity_context, dict) and (identity_context.get("owner_verified") or identity_context.get("current_identity") == "owner"):
        response += f" I recognize you as my enrolled owner{suffix}."
    return response


def _smalltalk_response(text: str, address: str) -> str | None:
    normalized = " ".join(str(text or "").lower().strip().split())
    if not normalized:
        return None
    if _is_date_question(normalized):
        return f"Today is {datetime.now().strftime('%d %B %Y')}, {address or 'Sir'}."

    expo_greeting = (
        "Hello, Sir. I am A.U.R.A, the Artificial Unified Response Assistant, "
        "a local desktop AI agent built for this project expo."
    )
    if normalized in {"hi", "hello", "hey", "what about you"}:
        return expo_greeting.replace("Sir", address or "Sir")
    if re.fullmatch(r"(?:hi|hello|hey)[, ]+i am [a-z .'-]+ from [a-z0-9 .&'-]+ what about you[?.!]?", normalized):
        return expo_greeting.replace("Sir", address or "Sir")

    smalltalk_map = {
        "how was your day": f"Running smoothly, {_address_suffix(address).replace(', ', '') if _address_suffix(address) else 'Sir'}.",
        "how are you": "I'm online and ready, Sir.",
        "are you okay": "I'm okay, Sir.",
        "are you working": "I am working, Sir.",
        "are you online": "I am online and ready, Sir.",
        "nice": "Glad to hear that, Sir.",
        "good": "Glad to hear that, Sir.",
        "thank you": "You're welcome, Sir.",
        "thanks": "You're welcome, Sir.",
        "okay good": "Glad to hear that, Sir.",
        "good to know": "Glad to hear that, Sir.",
    }

    for phrase, response in smalltalk_map.items():
        if phrase in normalized:
            return response.replace("Sir", address or "Sir")

    if _general_conversation_intent(normalized):
        return f"Of course, {address or 'Sir'}. We can have a general conversation. What would you like to talk about?"

    return None


def _junk_stt_response(text: str) -> str | None:
    cleaned = " ".join(str(text or "").lower().strip().split())
    if not cleaned:
        return "I didn’t catch that clearly, Sir."

    junk_phrases = {
        "voice command voice input",
        "voice input",
        "command voice command",
        "microphone microphone",
        "listening listening",
        "test test test",
        "voice command",
        "microphone",
        "listening",
    }
    filler_words = {"um", "uh", "hmm", "ah"}
    if cleaned in junk_phrases or cleaned in filler_words:
        return "I didn’t catch that clearly, Sir."
    if any(p in cleaned for p in ("voice command", "voice input", "microphone microphone", "listening listening")):
        return "I didn’t catch that clearly, Sir."
    if re.fullmatch(r"(?:\w+\s+){2,}\w+", cleaned) and len(re.findall(r"\b(\w+)\b", cleaned)) >= 3:
        repeated_tokens = re.findall(r"\b(\w+)\b", cleaned)
        if len(repeated_tokens) >= 3 and repeated_tokens.count(repeated_tokens[0]) >= 2:
            return "I didn’t catch that clearly, Sir."
    return None


def _identity_profile_response(text: str, address: str) -> str | None:
    normalized = " ".join(text.lower().strip().split())
    from .vision_commands import is_identity_profile_question

    if not is_identity_profile_question(normalized):
        return None
    try:
        owner = identity.profiles().get("profiles_by_id", {}).get("owner", {})
    except Exception:
        owner = {}
    face_enrolled = bool(owner.get("face_enrolled"))
    voice_enrolled = bool(owner.get("voice_enrolled"))

    if "voice" in normalized:
        if voice_enrolled:
            return f"Yes, {address}. Owner voice profile is enrolled."
        return f"Owner voice profile is not enrolled yet, {address}."
    if "profile ready" in normalized or "am i enrolled" in normalized:
        if face_enrolled and voice_enrolled:
            return f"Owner profile is ready, {address}. Face and voice are enrolled."
        missing = []
        if not face_enrolled:
            missing.append("face")
        if not voice_enrolled:
            missing.append("voice")
        return f"Owner profile is not fully ready, {address}. Missing {' and '.join(missing)} enrollment."
    if face_enrolled:
        return f"Yes, {address}. Owner face profile is enrolled."
    return f"No, {address}. Owner face profile is not enrolled yet."


def _identity_owner_question_response(
    text: str,
    identity_context: Any,
    vision_context: Any,
    address_override: str | None = None,
) -> str | None:
    from .vision_commands import is_owner_identity_question
    if not is_owner_identity_question(text):
        return None

    id_ctx = identity_context if isinstance(identity_context, dict) else {}
    vis_ctx = _safe_vision_context(vision_context)

    address = str(address_override or id_ctx.get("address_used") or vis_ctx.get("address_selected") or "Sir")
    suffix = _address_suffix(address)

    camera_status = str(vis_ctx.get("camera_status") or "off").lower()
    tracking_status = str(vis_ctx.get("tracking_status") or "off").lower()
    people_count = int(vis_ctx.get("people_count") or 0)

    owner_verified = bool(
        id_ctx.get("owner_verified")
        or id_ctx.get("owner_live_verified")
        or vis_ctx.get("owner_verified")
        or (str(id_ctx.get("current_identity") or vis_ctx.get("current_identity")).lower() == "owner")
    )

    owner_face_enrolled = bool(
        id_ctx.get("owner_face_enrolled", True)
    )

    if camera_status not in ("active", "online", "on"):
        return f"Camera is off{suffix}."

    if tracking_status not in ("active", "on"):
        return f"Camera is online, but people tracking is not active{suffix}."

    if people_count == 0:
        return f"I do not see anyone clearly in the camera feed{suffix}."

    if owner_verified:
        return f"Yes{suffix}. I can see my enrolled owner."

    if owner_face_enrolled:
        return f"I can see a person{suffix}, but I cannot verify them as the owner."

    return f"I can see a person{suffix}, but no owner face is enrolled yet."


def _address_suffix(address: str) -> str:
    clean = (address or "").strip()
    return f", {clean}" if clean in {"Sir", "Mam", "Ma'am", "Mx"} else ""


def _owner_profile_enrolled() -> bool:
    try:
        owner = identity.profiles().get("profiles_by_id", {}).get("owner", {})
    except Exception:
        owner = {}
    return bool(owner.get("face_enrolled") or owner.get("voice_enrolled"))


def _effective_voice_address(
    *,
    identity_context: dict[str, Any] | None,
    vision_context: dict[str, Any] | None,
    effective_address: str,
    session_address_override: str,
    address_reason: str,
    conversation_mode: str,
) -> tuple[str, str]:
    identity_summary = dict(identity_context or {})
    if isinstance(vision_context, dict):
        for key in (
            "matched_profile", "matched_profile_id", "matched_profile_name",
            "preferred_address", "identity_confidence", "people_tracking_state",
            "people_tracking_status", "gesture_tracking_state", "tracking_state",
            "tracking_status", "identity_state", "live_identity_fresh",
            "active_profile_is_unambiguous", "last_live_match_at",
            "last_owner_verified_at", "live_identity_address_allowed",
        ):
            if key not in identity_summary and key in vision_context:
                identity_summary[key] = vision_context.get(key)
    matched_profile = identity_summary.get("matched_profile") if isinstance(identity_summary.get("matched_profile"), dict) else None
    if not matched_profile and identity_summary.get("preferred_address"):
        matched_profile = {
            "id": identity_summary.get("matched_profile_id"),
            "display_name": identity_summary.get("matched_profile_name"),
            "preferred_address": identity_summary.get("preferred_address"),
            "address_source": identity_summary.get("address_source") or "explicit_profile",
        }
        identity_summary["matched_profile"] = matched_profile
    resolved = identity.resolve_effective_address(identity_summary, default_address="Sir")
    print(
        "[AURA IDENTITY] "
        f"live_identity_address_allowed={str(bool(resolved.get('live_identity_address_allowed'))).lower()} "
        f"reason={resolved.get('address_source') or 'default'}"
    )
    if resolved.get("address_source") == "explicit_profile_live_identity":
        print(f"[AURA IDENTITY] effective_address={resolved.get('effective_address')} source=explicit_profile_live_identity matched_profile={resolved.get('matched_profile_name') or 'none'} visual_gender_inference=false")
        return str(resolved.get("effective_address") or "Sir"), "explicit_profile_live_identity"
    identity_address = str((identity_context or {}).get("preferred_address") or (identity_context or {}).get("address") or "").strip()
    if identity_address == "Mam":
        identity_address = "Ma'am"
    vision_address = str((vision_context or {}).get("preferred_address") or (vision_context or {}).get("address_selected") or "").strip()
    if vision_address == "Mam":
        vision_address = "Ma'am"
    current_identity = str((identity_context or {}).get("role") or "").strip().lower()
    if current_identity == "owner":
        if bool(resolved.get("live_identity_address_allowed")):
            return identity_address if identity_address in {"Sir", "Mam", "Ma'am", "Mx"} else "Sir", address_reason or "owner_verified"
    if session_address_override in {"Sir", "Mam", "Ma'am", "Mx", ""} and session_address_override:
        return session_address_override, "session_override"
    if effective_address == "Mam":
        effective_address = "Ma'am"
    if effective_address in {"Sir", "Mam", "Ma'am", "Mx"}:
        return effective_address, address_reason or "effective_address"
    if identity_address in {"Sir", "Mam", "Ma'am", "Mx"}:
        if bool(resolved.get("live_identity_address_allowed")):
            return identity_address, address_reason or "identity"
    if conversation_mode in {"main_orb", "expo_vision_identity", "awake_latched"} and _owner_profile_enrolled():
        return "Sir", "owner_profile_enrolled_local"
    if effective_address == "Guest":
        return "Guest", address_reason or "neutral"
    return "Sir", address_reason or "local_default"


def _wake_command_tail(normalized: str) -> str:
    text = " ".join(normalized.lower().strip().split())
    prefixes = ("hey aura ara", "hey aura", "wake up", "wakeup", "hello aura", "aura")
    for prefix in prefixes:
        if text == prefix:
            return ""
        if text.startswith(prefix + " "):
            return text[len(prefix):].strip()
    return text


def _strip_wake_terms(text: str) -> str:
    normalized = " ".join(text.lower().strip().split())
    if not normalized:
        return ""
    replacements = (
        "hey aura ara",
        "hey aura",
        "hello aura",
        "ok aura",
        "okay aura",
        "wake up",
        "wakeup",
        "a u r a",
        "aura",
        "aurora",
        "ara",
        "ora",
    )
    words = normalized
    for term in replacements:
        words = f" {words} ".replace(f" {term} ", " ")
        words = " ".join(words.split())
    return words


def _is_wake_only(normalized: str, wake_detected: bool) -> bool:
    text = " ".join(normalized.lower().strip().split())
    if not text:
        return False
    if wake_detected and not _strip_wake_terms(text):
        return True
    return text in {"wake up", "wakeup", "hey aura", "hey aura ara", "aura", "hello aura"}


def _local_stub_response(current_text: str, recent_turns: list[dict[str, str]], vision_context: Any, address: str, fallback_reason: str | None = None) -> str:
    lowered = current_text.lower()
    suffix = _address_suffix(address)
    profile_answer = _identity_profile_response(current_text, address)
    if profile_answer:
        return profile_answer
    vision_answer = _vision_question_response(current_text, vision_context, address)
    if vision_answer:
        return vision_answer
    unclear = " ".join(lowered.strip().split())
    if unclear in {"can you tell me is very nice", "can tell me is very nice"}:
        return f"I heard you{suffix}, but that sentence was unclear. Please repeat it once."

    if any(q in lowered for q in ("why the response engine is not currently available", "why is the response engine not available", "why response engine unavailable", "response engine status")):
        if fallback_reason:
            return "Cloud reasoning is unavailable right now, Sir."
        return f"The response engine is available now, {address}. Earlier failures were caused by provider request errors, not maintenance."

    if any(token in lowered for token in ("can you", "are you able", "do you listen", "are you listening", "can you hear")):
        if fallback_reason:
            return "Cloud reasoning is unavailable right now, Sir."
        return f"I heard you{suffix}, but cloud reasoning fell back to local mode."

    if any(token in lowered for token in ("why", "that", "it", "explain", "repeat", "what about")):
        last = next((turn for turn in reversed(recent_turns) if turn.get("user") or turn.get("assistant")), None)
        if not last:
            return f"I need the previous context to answer that{suffix}."
        last_user = last.get("user", "")
        last_assistant = last.get("assistant", "")
        if "see" in last_user or "camera" in last_assistant.lower() or "vision" in last_assistant.lower():
            return f"That refers to the camera and vision context from the previous turn{suffix}. I can only answer from the browser context sent to me."
        return f"You were referring to the previous response{suffix}. {last_assistant[:160] or 'I can clarify it.'}"

    if fallback_reason:
        return "Cloud reasoning is unavailable right now, Sir."
    return f"I heard you{suffix}, but cloud reasoning fell back to local mode."


def _perf_ms() -> float:
    return time.perf_counter() * 1000


def _mask_secret(value: str, *, prefix: int = 0, suffix: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return value[:prefix] + ("*" * 8) + value[-suffix:]


def _mask_openrouter(value: str) -> str:
    if not value:
        return ""
    return _mask_secret(value, prefix=min(12, max(0, len(value) - 4)), suffix=4)


def _mask_elevenlabs(value: str) -> str:
    if not value:
        return ""
    prefix = 4 if value.lower().startswith("elev") else 0
    return _mask_secret(value, prefix=prefix, suffix=4)


def _mask_fish_audio(value: str) -> str:
    if not value:
        return ""
    prefix = 5 if value.lower().startswith("fish_") else 0
    return _mask_secret(value, prefix=prefix, suffix=4)


def _read_connector_secret(connector_id: str, field_name: str) -> str:
    data = connectors._load_config()
    server = data.get("mcpServers", {}).get(connector_id)
    if not isinstance(server, dict):
        return ""
    return connectors._extract_value(server, field_name)


def _openrouter_key() -> str:
    return (
        os.environ.get("OPENROUTER_API_KEY", "").strip()
        or os.environ.get("AURA_OPENROUTER_API_KEY", "").strip()
    )


def _groq_key() -> str:
    return (
        os.environ.get("GROQ_API_KEY", "").strip()
        or os.environ.get("AURA_GROQ_API_KEY", "").strip()
    )


def _fish_audio_key() -> str:
    return (
        os.environ.get("FISH_AUDIO_API_KEY", "").strip()
        or os.environ.get("AURA_FISH_AUDIO_API_KEY", "").strip()
    )


def _fish_audio_reference_id() -> str:
    return (
        os.environ.get("FISH_AUDIO_REFERENCE_ID", "").strip()
        or os.environ.get("AURA_FISH_AUDIO_REFERENCE_ID", "").strip()
    )


def _fish_audio_model() -> str:
    return os.environ.get("FISH_AUDIO_MODEL", "s2-pro").strip() or "s2-pro"


def _cartesia_key() -> str:
    return os.environ.get("CARTESIA_API_KEY", "").strip() or os.environ.get("AURA_CARTESIA_API_KEY", "").strip()


def _cartesia_voice_id() -> str:
    return os.environ.get("CARTESIA_VOICE_ID", "").strip() or os.environ.get("AURA_CARTESIA_VOICE_ID", "").strip()


def _cartesia_model() -> str:
    return os.environ.get("CARTESIA_MODEL_ID", "sonic-3.5").strip() or "sonic-3.5"


def _cartesia_version() -> str:
    return os.environ.get("CARTESIA_VERSION", "2024-11-13").strip() or "2024-11-13"


def _tts_cache_key(provider: str, text: str) -> str:
    voice_hash = hashlib.sha256(_cartesia_voice_id().encode("utf-8")).hexdigest()[:16]
    output = _cartesia_output_settings()
    normalized = " ".join(text.lower().strip().split())
    return "|".join((provider, _cartesia_model(), voice_hash, normalized, json.dumps(output, sort_keys=True)))


def _cached_tts_result(text: str) -> dict[str, Any] | None:
    cached = _tts_audio_cache.get(_tts_cache_key("cartesia", text))
    if not cached:
        return None
    result = dict(cached)
    result["tts_cache_hit"] = True
    result["latency_ms"] = 0
    result["attempts"] = [{
        "provider": "cartesia",
        "attempted": True,
        "success": True,
        "ok": True,
        "error_type": None,
        "message": None,
        "http_status": 200,
        "selected_client": cached.get("selected_client"),
        "audio_bytes": int(cached.get("audio_bytes") or 0),
        "audio_base64_present": bool(cached.get("audio_base64")),
        "latency_ms": 0,
    }]
    return result


def _cartesia_output_settings() -> dict[str, Any]:
    container = os.environ.get("CARTESIA_OUTPUT_CONTAINER", "wav").strip().lower() or "wav"
    if container not in {"wav", "mp3"}:
        container = "wav"
    encoding = os.environ.get("CARTESIA_ENCODING", "pcm_s16le").strip() or "pcm_s16le"
    raw_sample_rate = os.environ.get("CARTESIA_SAMPLE_RATE", "44100").strip()
    try:
        sample_rate = int(raw_sample_rate)
    except (TypeError, ValueError):
        sample_rate = 44100
    if sample_rate <= 0:
        sample_rate = 44100
    if not raw_sample_rate or raw_sample_rate != str(sample_rate):
        _log_safe("cartesia_sample_rate_defaulted_44100", {"provider": "cartesia", "sample_rate": sample_rate})
    return {"container": container, "encoding": encoding, "sample_rate": sample_rate}


def _tts_provider_priority() -> list[str]:
    if tts.cartesia_only_required():
        return ["cartesia"]
    raw = os.environ.get("AURA_TTS_PROVIDER_PRIORITY", "cartesia,fish_audio,elevenlabs,browser_speech")
    aliases = {
        "cartesia": "cartesia",
        "cartesia_sonic": "cartesia",
        "sonic": "cartesia",
        "fish": "fish_audio",
        "fishaudio": "fish_audio",
        "fish_audio": "fish_audio",
        "eleven": "elevenlabs",
        "elevenlabs": "elevenlabs",
        "browser": "browser_speech",
        "browser_speech": "browser_speech",
        "speechsynthesis": "browser_speech",
    }
    priority: list[str] = []
    for item in raw.split(","):
        provider = aliases.get(item.strip().lower().replace("-", "_"))
        if provider and provider not in priority:
            priority.append(provider)
    for fallback in ("cartesia", "fish_audio", "elevenlabs", "browser_speech"):
        if fallback not in priority:
            priority.append(fallback)
    return priority


def _tts_strict_primary() -> bool:
    return tts.strict_primary_tts_enabled()


def _tts_required_provider() -> str:
    return tts.required_tts_provider()


def _tts_browser_fallback_allowed() -> bool:
    return tts.browser_tts_fallback_allowed()


def _cartesia_strict_mode() -> bool:
    return _tts_strict_primary() and _tts_required_provider() == "cartesia"


def _cartesia_tts_timeout_seconds() -> float:
    return tts.cartesia_tts_timeout_seconds()


def _cartesia_tts_chunk_timeout_seconds() -> float:
    return tts.cartesia_tts_chunk_timeout_seconds()


def _openrouter_model() -> str:
    return (
        os.environ.get("AURA_OPENROUTER_MODEL", "").strip()
        or os.environ.get("OPENROUTER_MODEL", "").strip()
        or os.environ.get("JARVIS_MODEL", "").strip()
        or hermes_client.get_active_model()
        or "openai/gpt-oss-120b:free"
    )


def _groq_model() -> str:
    return (
        os.environ.get("AURA_GROQ_MODEL", "").strip()
        or os.environ.get("GROQ_MODEL", "").strip()
        or "openai/gpt-oss-20b"
    )


def _cloud_fast_mode_enabled() -> bool:
    return os.environ.get("AURA_CLOUD_FAST_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _cloud_int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 60) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _cloud_float_env(name: str, default: float, *, minimum: float = 0.0, maximum: float = 2.0) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _is_date_question(text: str) -> bool:
    normalized = re.sub(r"[^a-z ]", "", str(text).lower())
    return " ".join(normalized.split()) in {"what is todays date", "what is today date", "what is the date today"}


def _cloud_timeout_seconds() -> int:
    return _cloud_int_env("AURA_CLOUD_TIMEOUT_SECONDS", 6 if _cloud_fast_mode_enabled() else 12, minimum=1, maximum=18)


def _cloud_max_tokens() -> int:
    return _cloud_int_env("AURA_CLOUD_MAX_TOKENS", 160 if _cloud_fast_mode_enabled() else 220, minimum=16, maximum=160 if _cloud_fast_mode_enabled() else 800)


def _cloud_temperature() -> float:
    return _cloud_float_env("AURA_CLOUD_TEMPERATURE", 0.4, minimum=0.0, maximum=1.5)


def _cloud_max_recent_turns() -> int:
    return _cloud_int_env("AURA_CLOUD_MAX_RECENT_TURNS", 4 if _cloud_fast_mode_enabled() else 8, minimum=0, maximum=4 if _cloud_fast_mode_enabled() else 12)


def _cloud_max_context_chars() -> int:
    return _cloud_int_env("AURA_CLOUD_MAX_CONTEXT_CHARS", 2200 if _cloud_fast_mode_enabled() else 6000, minimum=500, maximum=2200 if _cloud_fast_mode_enabled() else 20000)


def _cloud_timeout_fallback() -> str:
    return "I could not reach cloud reasoning quickly enough, Sir. Please try again."


def _cloud_short_answer_mode(text: str) -> bool:
    normalized = _normalize_runtime_control_text(text)
    compact = re.sub(r"[^a-z0-9 ]+", "", normalized).strip()
    if compact in {"hi", "hello", "hey", "how are you", "do you know tamil", "what is todays date", "what is today date"}:
        return True
    return len(compact.split()) <= 5 and compact.startswith(("hi", "hello", "hey"))


def _bounded_cloud_recent_turns(recent_turns: list[dict[str, str]]) -> list[dict[str, str]]:
    return list(recent_turns[-_cloud_max_recent_turns():]) if _cloud_max_recent_turns() else []


def _clamp_cloud_text(text: str, limit: int) -> str:
    clean = str(text or "")
    if len(clean) <= limit:
        return clean
    return clean[-limit:]


def _log_cloud_latency(
    *,
    provider: str,
    prompt_chars: int,
    recent_turns: int,
    timeout_seconds: int,
    response_ms: int | None = None,
    fallback_used: bool | None = None,
    reason: str | None = None,
) -> None:
    parts = [
        f"provider={provider}",
        f"prompt_chars={prompt_chars}",
        f"recent_turns={recent_turns}",
        f"timeout_seconds={timeout_seconds}",
    ]
    if response_ms is not None:
        parts.append(f"response_ms={response_ms}")
    if fallback_used is not None:
        parts.append(f"fallback_used={str(bool(fallback_used)).lower()}")
    if reason:
        parts.append(f"reason={reason}")
    print("[AURA CLOUD LATENCY] " + " ".join(parts), flush=True)


def _openrouter_fallback_models() -> list[str]:
    configured = (
        os.environ.get("OPENROUTER_FALLBACK_MODELS", "").strip()
        or os.environ.get("AURA_OPENROUTER_FALLBACK_MODELS", "").strip()
        or "openai/gpt-oss-120b,openrouter/free"
    )
    return [model.strip() for model in configured.split(",") if model.strip()]


def _safe_preview(text: str, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[:limit]


def _elevenlabs_key() -> str:
    return (
        os.environ.get("ELEVENLABS_API_KEY", "").strip()
        or _read_connector_secret("elevenlabs", "elevenlabs_key").strip()
    )


def _elevenlabs_voice_id() -> str:
    return (
        os.environ.get("AURA_ELEVENLABS_VOICE_ID", "").strip()
        or os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
        or os.environ.get("JARVIS_ELEVENLABS_VOICE_ID", "").strip()
    )


def _elevenlabs_voice_id_source() -> str | None:
    for key in ("AURA_ELEVENLABS_VOICE_ID", "ELEVENLABS_VOICE_ID", "JARVIS_ELEVENLABS_VOICE_ID"):
        if os.environ.get(key, "").strip():
            return key
    return None


def _mask_voice_id(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * max(4, len(value) - 7)}{value[-4:]}"


def _log_safe(event: str, data: dict[str, Any]) -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {
        k: v for k, v in data.items()
        if "key" not in k.lower() and "token" not in k.lower() and "authorization" not in k.lower()
    }
    line = json.dumps({"ts": _now_iso(), "event": event, **safe}, ensure_ascii=False)
    with DIAG_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _error_class(status: int | None, text: str, *, provider: str) -> str:
    if provider == "cartesia":
        return _cartesia_error_class(status, text)
    if provider == "fish_audio":
        return _fish_audio_error_class(status, text)
    lowered = text.lower()
    if provider == "elevenlabs" and status == 400:
        if "voice_id" in lowered or "voice not found" in lowered or "invalid voice" in lowered:
            return "invalid_voice_id"
        if "model_id" in lowered or "model not found" in lowered or "invalid model" in lowered:
            return "invalid_model"
        if "quota" in lowered or "credit" in lowered or "character limit" in lowered or "tier" in lowered or "subscription" in lowered:
            return "quota_or_plan_restriction"
        if "body" in lowered or "payload" in lowered or "json" in lowered or "text" in lowered:
            return "invalid_payload"
        return "elevenlabs_bad_request"
    if status in (401, 403) or "invalid api key" in lowered or "unauthorized" in lowered:
        return "invalid_key"
    if status == 404:
        return "voice_not_found_or_bad_endpoint" if provider == "elevenlabs" else "model_not_found"
    if status == 402:
        return "quota_or_plan_required"
    if status == 429 or "rate limit" in lowered or "quota" in lowered or "credits" in lowered:
        return "quota_or_rate_limit"
    if status and 500 <= status <= 599:
        return "provider_5xx"
    if "model" in lowered and "not found" in lowered:
        return "model_not_found"
    return "unknown"


def _cartesia_error_class(status: int | None, text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("winerror 10013", "access is denied", "forbidden by its access permissions")):
        return "cartesia_client_network_blocked"
    if "timed out" in lowered or "timeout" in lowered:
        return "cartesia_timeout"
    if any(marker in lowered for marker in ("name or service not known", "getaddrinfo failed", "nodename nor servname", "could not resolve host")):
        return "cartesia_dns_error"
    if any(marker in lowered for marker in ("ssl", "certificate", "tls")):
        return "cartesia_ssl_error"
    if status == 401 or "invalid api key" in lowered or "unauthorized" in lowered:
        return "cartesia_invalid_key"
    if status == 402:
        return "cartesia_payment_required_or_quota"
    if status == 403:
        return "cartesia_forbidden"
    if status == 404:
        return "cartesia_endpoint_or_voice_not_found"
    if status == 422:
        return "cartesia_invalid_payload_or_voice_id"
    if status == 429 or "rate limit" in lowered:
        return "cartesia_rate_limited"
    if status == 400:
        return "cartesia_bad_request"
    if status and 500 <= status <= 599:
        return "cartesia_provider_5xx"
    if any(marker in lowered for marker in ("connection reset", "connection refused", "could not connect", "failed to connect", "all connection attempts failed", "network")):
        return "cartesia_network_error"
    return "cartesia_network_error" if not status else "cartesia_unknown"


def _fish_audio_error_class(status: int | None, text: str) -> str:
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if any(marker in lowered for marker in ("winerror 10013", "access is denied", "forbidden by its access permissions")):
        return "client_network_blocked"
    if any(marker in lowered for marker in ("name or service not known", "getaddrinfo failed", "nodename nor servname", "could not resolve host")):
        return "dns_error"
    if any(marker in lowered for marker in ("ssl", "certificate", "tls")):
        return "ssl_error"
    if any(marker in lowered for marker in ("connection reset", "connection refused", "could not connect", "failed to connect")):
        return "network_error"
    if any(marker in lowered for marker in ("proxy", "407")):
        return "proxy_error"
    if status == 401 or "invalid api key" in lowered or "unauthorized" in lowered:
        return "invalid_key"
    if status == 402:
        return "payment_required_or_quota"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "endpoint_or_reference_not_found"
    if status == 422:
        return "invalid_payload_or_reference_id"
    if status == 429 or "rate limit" in lowered or "quota" in lowered or "credits" in lowered:
        return "rate_limited"
    if status and 500 <= status <= 599:
        return "provider_5xx"
    return "unknown"


def _elevenlabs_model_id() -> str:
    return os.environ.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")


def _client_error_type(exc: BaseException) -> str:
    reason = getattr(exc, "reason", "")
    msg = f"{exc} {reason}"
    lowered = msg.lower()
    blocked_markers = ("access is denied", "winerror 10013", "forbidden by its access permissions")
    if any(marker in lowered for marker in blocked_markers):
        return "client_network_blocked"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if any(marker in lowered for marker in ("getaddrinfo failed", "could not resolve host", "name or service not known")):
        return "dns_error"
    if any(marker in lowered for marker in ("ssl", "certificate", "tls")):
        return "ssl_error"
    if "proxy" in lowered:
        return "proxy_error"
    return "network_error"


def _attempt_from_result(result: dict[str, Any]) -> dict[str, Any]:
    attempt = {
        "provider": result.get("provider"),
        "attempted": True,
        "client": result.get("client"),
        "ok": bool(result.get("ok")),
        "success": bool(result.get("ok")),
        "error_type": result.get("error_type") or result.get("error"),
        "message": result.get("message"),
        "http_status": result.get("http_status"),
        "selected_client": result.get("selected_client") or result.get("client"),
        "audio_bytes": int(result.get("audio_bytes") or 0),
        "audio_base64_present": bool(result.get("audio_base64")),
    }
    for key in ("latency_ms", "model", "version", "raw_error_preview", "request_url_used", "audio_base64_encode_ms", "payload_summary"):
        if result.get(key) is not None:
            attempt[key] = result.get(key)
    return attempt


def _chat_content_from_data(data: Any) -> tuple[str, dict[str, Any]]:
    raw_shape: dict[str, Any] = {
        "top_level_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        "choices_length": 0,
        "message_keys": [],
        "finish_reason": None,
    }
    if not isinstance(data, dict):
        return "", raw_shape
    choices = data.get("choices")
    if isinstance(choices, list):
        raw_shape["choices_length"] = len(choices)
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    raw_shape["finish_reason"] = first.get("finish_reason")
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    raw_shape["message_keys"] = sorted(message.keys()) if isinstance(message, dict) else []
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip(), raw_shape
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text") or part.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts).strip(), raw_shape
    text = first.get("text")
    if isinstance(text, str):
        return text.strip(), raw_shape
    return "", raw_shape


def _sanitize_provider_output(text: str) -> str:
    clean = str(text or "")
    clean = re.sub(r"<\|(?:start|end|assistant|user|system|message)\|>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<(?:think|aura:think|jarvis:think|cosmo:think)>.*?</(?:think|aura:think|jarvis:think|cosmo:think)>", "", clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"</?(?:think|aura:think|jarvis:think|cosmo:think)>", "", clean, flags=re.IGNORECASE)
    return " ".join(clean.split()).strip()


def _provider_http_error_type(provider: str, status: int, text: str) -> str:
    lowered = text.lower()
    if provider == "openrouter" and status == 404 and "model" not in lowered:
        return "openrouter_http_404"
    return _error_class(status, text, provider=provider)


def _classification_result(
    *,
    client: str,
    ok: bool,
    classification: str,
    message: str,
    latency_ms: int,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "client": client,
        "classification": classification,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "error_type": None if ok else classification,
        "message": message[:300],
    }


def _elevenlabs_payload(text: str) -> dict[str, Any]:
    from . import tts

    return {
        "text": tts.sanitize_text_for_tts(text),
        "model_id": _elevenlabs_model_id(),
        "voice_settings": {
            "stability": 0.65,
            "similarity_boost": 0.75,
        },
    }


def _fish_audio_payload(text: str) -> dict[str, Any]:
    from . import tts

    return {
        "text": tts.sanitize_text_for_tts(text),
        "reference_id": _fish_audio_reference_id(),
        "format": "mp3",
    }


def _elevenlabs_root_check_urllib() -> dict[str, Any]:
    started = _perf_ms()
    req = urllib.request.Request(ELEVENLABS_ROOT_URL, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = int(getattr(resp, "status", 200))
            return _classification_result(client="urllib", ok=True, classification="reachable", http_status=status, message="ElevenLabs host is reachable.", latency_ms=round(_perf_ms() - started))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _classification_result(client="urllib", ok=True, classification="reachable_root_404_expected", http_status=exc.code, message="ElevenLabs host is reachable. Root 404 is expected because this is not a TTS endpoint.", latency_ms=round(_perf_ms() - started))
        cls = _error_class(exc.code, "", provider="elevenlabs")
        return _classification_result(client="urllib", ok=False, classification=cls, http_status=exc.code, message=f"ElevenLabs root returned HTTP {exc.code}", latency_ms=round(_perf_ms() - started))
    except urllib.error.URLError as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc.reason), latency_ms=round(_perf_ms() - started))
    except OSError as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))


def elevenlabs_root_check_curl() -> dict[str, Any]:
    started = _perf_ms()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return _classification_result(client="curl_exe", ok=False, classification="curl_missing", message="curl.exe is not available.", latency_ms=0)
    try:
        proc = subprocess.run(
            [curl, "-I", "--max-time", "10", ELEVENLABS_ROOT_URL],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="curl_exe", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))
    output = f"{proc.stdout}\n{proc.stderr}"
    status = _parse_curl_http_status(output)
    if status == 404:
        return _classification_result(client="curl_exe", ok=True, classification="reachable_root_404_expected", http_status=status, message="ElevenLabs host is reachable. Root 404 is expected because this is not a TTS endpoint.", latency_ms=round(_perf_ms() - started))
    if status and 200 <= status < 500:
        return _classification_result(client="curl_exe", ok=True, classification="reachable", http_status=status, message="ElevenLabs host is reachable.", latency_ms=round(_perf_ms() - started))
    cls = _client_error_type(Exception(output)) if output.strip() else "network_error"
    return _classification_result(client="curl_exe", ok=False, classification=cls, http_status=status, message=(output.strip() or f"curl.exe exited {proc.returncode}"), latency_ms=round(_perf_ms() - started))


def _fish_audio_root_check_urllib() -> dict[str, Any]:
    started = _perf_ms()
    req = urllib.request.Request(FISH_AUDIO_ROOT_URL, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = int(getattr(resp, "status", 200))
            return _classification_result(client="urllib", ok=True, classification="reachable", http_status=status, message="Fish Audio host is reachable.", latency_ms=round(_perf_ms() - started))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 405}:
            return _classification_result(client="urllib", ok=True, classification="reachable_root_http_expected", http_status=exc.code, message="Fish Audio host is reachable. Root HTTP response is expected because this is not a TTS endpoint.", latency_ms=round(_perf_ms() - started))
        cls = _fish_audio_error_class(exc.code, "")
        return _classification_result(client="urllib", ok=False, classification=cls, http_status=exc.code, message=f"Fish Audio root returned HTTP {exc.code}", latency_ms=round(_perf_ms() - started))
    except urllib.error.URLError as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc.reason), latency_ms=round(_perf_ms() - started))
    except OSError as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))


def fish_audio_root_check_curl() -> dict[str, Any]:
    started = _perf_ms()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return _classification_result(client="curl_exe", ok=False, classification="curl_missing", message="curl.exe is not available.", latency_ms=0)
    try:
        proc = subprocess.run(
            [curl, "-I", "--max-time", "10", FISH_AUDIO_ROOT_URL],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        cls = _client_error_type(exc)
        return _classification_result(client="curl_exe", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))
    output = f"{proc.stdout}\n{proc.stderr}"
    status = _parse_curl_http_status(output)
    if status and 200 <= status < 500:
        return _classification_result(client="curl_exe", ok=True, classification="reachable", http_status=status, message="Fish Audio host is reachable.", latency_ms=round(_perf_ms() - started))
    cls = _client_error_type(Exception(output)) if output.strip() else "network_error"
    return _classification_result(client="curl_exe", ok=False, classification=cls, http_status=status, message=(output.strip() or f"curl.exe exited {proc.returncode}"), latency_ms=round(_perf_ms() - started))


def _cartesia_root_check_urllib() -> dict[str, Any]:
    started = _perf_ms()
    req = urllib.request.Request(CARTESIA_ROOT_URL, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = int(getattr(resp, "status", 200))
            return _classification_result(client="urllib", ok=True, classification="reachable", http_status=status, message="Cartesia host is reachable.", latency_ms=round(_perf_ms() - started))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 405}:
            return _classification_result(client="urllib", ok=True, classification="reachable_root_http_expected", http_status=exc.code, message="Cartesia host is reachable. Root HTTP response is expected because this is not a TTS endpoint.", latency_ms=round(_perf_ms() - started))
        cls = _cartesia_error_class(exc.code, "")
        return _classification_result(client="urllib", ok=False, classification=cls, http_status=exc.code, message=f"Cartesia root returned HTTP {exc.code}", latency_ms=round(_perf_ms() - started))
    except urllib.error.URLError as exc:
        cls = _cartesia_error_class(None, str(exc.reason))
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc.reason), latency_ms=round(_perf_ms() - started))
    except OSError as exc:
        cls = _cartesia_error_class(None, str(exc))
        return _classification_result(client="urllib", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))


def cartesia_root_check_curl() -> dict[str, Any]:
    started = _perf_ms()
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return _classification_result(client="curl_exe", ok=False, classification="curl_missing", message="curl.exe is not available.", latency_ms=0)
    try:
        proc = subprocess.run(
            [curl, "-I", "--max-time", "10", CARTESIA_ROOT_URL],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        cls = _cartesia_error_class(None, str(exc))
        return _classification_result(client="curl_exe", ok=False, classification=cls, message=str(exc), latency_ms=round(_perf_ms() - started))
    output = f"{proc.stdout}\n{proc.stderr}"
    status = _parse_curl_http_status(output)
    if status and 200 <= status < 500:
        return _classification_result(client="curl_exe", ok=True, classification="reachable", http_status=status, message="Cartesia host is reachable.", latency_ms=round(_perf_ms() - started))
    cls = _cartesia_error_class(None, output) if output.strip() else "cartesia_network_error"
    return _classification_result(client="curl_exe", ok=False, classification=cls, http_status=status, message=(output.strip() or f"curl.exe exited {proc.returncode}"), latency_ms=round(_perf_ms() - started))


def _parse_curl_http_status(output: str) -> int | None:
    matches = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                matches.append(int(parts[1]))
        elif line.isdigit() and len(line) == 3:
            matches.append(int(line))
    return matches[-1] if matches else None


def test_elevenlabs_network() -> dict[str, Any]:
    urllib_root = _elevenlabs_root_check_urllib()
    curl_root = elevenlabs_root_check_curl()
    root = urllib_root if urllib_root.get("ok") else curl_root
    ok = bool(root.get("ok"))
    result = {
        "ok": ok,
        "provider": "elevenlabs",
        "root": root,
        "urllib_root": urllib_root,
        "curl_root": curl_root,
        "client": root.get("client"),
        "latency_ms": int(urllib_root.get("latency_ms") or 0) + int(curl_root.get("latency_ms") or 0),
        "error": None if ok else root.get("classification"),
    }
    _log_safe("elevenlabs_network", result)
    return result


def _attempts_have_error(attempts: list[dict[str, Any]], error_type: str) -> bool:
    return any((attempt.get("error_type") or attempt.get("error")) == error_type for attempt in attempts)


def _tts_firewall_guidance() -> dict[str, Any]:
    python_paths = []
    for candidate in (sys.executable, shutil.which("python.exe") or shutil.which("python")):
        if candidate and candidate not in python_paths:
            python_paths.append(candidate)
    return {
        "summary": "Backend outbound HTTPS appears blocked for one or more local clients. Allow Python, curl.exe, and Node through firewall/security software, then rerun the network diagnostic.",
        "paths": {
            "venv_or_active_python": sys.executable,
            "python_candidates": python_paths,
            "curl": shutil.which("curl.exe") or shutil.which("curl"),
            "node": shutil.which("node.exe") or shutil.which("node"),
        },
        "note": "A.U.R.A does not change firewall rules automatically.",
    }


async def test_fish_audio_network() -> dict[str, Any]:
    urllib_root = _fish_audio_root_check_urllib()
    curl_root = fish_audio_root_check_curl()
    root = urllib_root if urllib_root.get("ok") else curl_root
    tts = await synthesize_fish_audio("A.U.R.A Fish Audio network diagnostic.", include_base64=False, purpose="network_diagnostic")
    attempts = tts.get("attempts", [])
    configured = bool(_fish_audio_key() and _fish_audio_reference_id())
    root_blocked = root.get("classification") == "client_network_blocked"
    tts_blocked = _attempts_have_error(attempts, "client_network_blocked")
    result = {
        "ok": bool(root.get("ok") and tts.get("ok")),
        "provider": "fish_audio",
        "configured": configured,
        "key_present": bool(_fish_audio_key()),
        "reference_id_present": bool(_fish_audio_reference_id()),
        "model": _fish_audio_model(),
        "root_connectivity": root,
        "urllib_root": urllib_root,
        "curl_root": curl_root,
        "tts_probe": {
            "ok": bool(tts.get("ok")),
            "selected_client": tts.get("selected_client"),
            "audio_bytes": tts.get("audio_bytes"),
            "mime_type": tts.get("mime_type"),
            "http_status": tts.get("http_status"),
            "latency_ms": tts.get("latency_ms"),
            "error_type": tts.get("error_type") or tts.get("error"),
            "message": tts.get("message"),
        },
        "attempts": attempts,
        "latency_ms": int(urllib_root.get("latency_ms") or 0) + int(curl_root.get("latency_ms") or 0) + int(tts.get("latency_ms") or 0),
        "error": None if tts.get("ok") else (tts.get("error_type") or tts.get("error") or root.get("classification")),
        "warning": None if tts.get("ok") else "Fish Audio is configured, but backend network access failed. The TTS chain will continue to ElevenLabs and browser speech fallback.",
        "firewall_guidance": _tts_firewall_guidance() if root_blocked or tts_blocked else None,
    }
    _log_safe("fish_audio_network", result)
    return result


def _safe_tts_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for attempt in attempts:
        safe.append({
            "provider": attempt.get("provider"),
            "client": attempt.get("client") or attempt.get("selected_client"),
            "ok": bool(attempt.get("ok") or attempt.get("success")),
            "http_status": attempt.get("http_status"),
            "error_type": attempt.get("error_type") or attempt.get("error"),
            "latency_ms": attempt.get("latency_ms"),
            "audio_bytes": int(attempt.get("audio_bytes") or 0),
            "audio_base64_present": bool(attempt.get("audio_base64_present") or attempt.get("audio_base64")),
            "model": attempt.get("model"),
            "version": attempt.get("version"),
            "raw_error_preview": _safe_preview(str(attempt.get("raw_error_preview") or attempt.get("message") or ""), 500),
        })
    return safe


async def test_cartesia_network() -> dict[str, Any]:
    urllib_root = _cartesia_root_check_urllib()
    curl_root = cartesia_root_check_curl()
    root = urllib_root if urllib_root.get("ok") else curl_root
    payload, payload_summary = _cartesia_payload_validation("A.U.R.A Cartesia network diagnostic.")
    configured = bool(_cartesia_key() and _cartesia_voice_id())
    if payload is None:
        tts = _cartesia_invalid_payload_failure("local_config", "A.U.R.A Cartesia network diagnostic.") or {}
    else:
        tts = await synthesize_cartesia_audio(
            "A.U.R.A Cartesia network diagnostic.",
            include_base64=False,
            purpose="network_diagnostic",
            mode="diagnostic",
        )
    attempts = _safe_tts_attempts(tts.get("attempts", [_attempt_from_result(tts)] if tts else []))
    tts_error = tts.get("error_type") or tts.get("error")
    blocked = root.get("classification") == "cartesia_client_network_blocked" or _attempts_have_error(attempts, "cartesia_client_network_blocked")
    result = {
        "ok": bool(root.get("ok") and tts.get("ok")),
        "provider": "cartesia",
        "configured": configured,
        "key_present": bool(_cartesia_key()),
        "voice_id_present": bool(_cartesia_voice_id()),
        "model": _cartesia_model(),
        "version": _cartesia_version(),
        "root_connectivity": root,
        "urllib_root": urllib_root,
        "curl_root": curl_root,
        "payload_summary": payload_summary,
        "circuit": _cartesia_circuit_state(),
        "tts_probe": {
            "ok": bool(tts.get("ok")),
            "selected_client": tts.get("selected_client"),
            "http_status": tts.get("http_status"),
            "audio_bytes": int(tts.get("audio_bytes") or 0),
            "audio_base64_present": bool(tts.get("audio_base64")),
            "mime_type": tts.get("mime_type"),
            "latency_ms": tts.get("latency_ms"),
            "error_type": tts_error,
            "message": tts.get("message"),
        },
        "attempts": attempts,
        "latency_ms": int(urllib_root.get("latency_ms") or 0) + int(curl_root.get("latency_ms") or 0) + int(tts.get("latency_ms") or 0),
        "error": None if tts.get("ok") else (tts_error or root.get("classification")),
        "warning": None if tts.get("ok") else "Cartesia is configured, but backend did not receive playable audio. Browser speech fallback remains active.",
        "firewall_guidance": {
            **_tts_firewall_guidance(),
            "summary": "Backend Python/curl outbound access may be blocked. Allow outbound access for .venv Python and curl.exe.",
        } if blocked else None,
    }
    _log_safe("cartesia_network", result)
    return result


_cached_elevenlabs_ready: bool = False
_cached_selected_client: str | None = None


def provider_status() -> dict[str, Any]:
    cartesia_key = _cartesia_key()
    cartesia_voice_id = _cartesia_voice_id()
    openrouter_key = _openrouter_key()
    groq_key = _groq_key()
    eleven_key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    fish_key = _fish_audio_key()
    fish_reference_id = _fish_audio_reference_id()
    openrouter_model = _openrouter_model()
    groq_model = _groq_model()
    tts_priority = _tts_provider_priority()

    return {
        "cartesia": {
            "configured": bool(cartesia_key and cartesia_voice_id),
            "key_present": bool(cartesia_key),
            "key_masked": _mask_secret(cartesia_key, prefix=0, suffix=4),
            "voice_id_present": bool(cartesia_voice_id),
            "model": _cartesia_model(),
            "version": _cartesia_version(),
            "status": "configured" if cartesia_key and cartesia_voice_id else "not_configured",
        },
        "openrouter": {
            "configured": bool(openrouter_key),
            "key_present": bool(openrouter_key),
            "key_masked": _mask_openrouter(openrouter_key),
            "model": openrouter_model,
            "status": "not_tested",
            "source": "server_env",
        },
        "groq": {
            "configured": bool(groq_key),
            "key_present": bool(groq_key),
            "key_masked": _mask_secret(groq_key, prefix=0, suffix=4),
            "model": groq_model,
            "status": "not_tested",
            "source": "server_env",
        },
        "elevenlabs": {
            "configured": bool(eleven_key),
            "key_present": bool(eleven_key),
            "key_masked": _mask_elevenlabs(eleven_key),
            "voice_id_present": bool(voice_id),
            "selected_client": _cached_selected_client,
            "status": "ready" if _cached_elevenlabs_ready else "not_tested",
            "source": "server_env_or_connector_config",
        },
        "fish_audio": {
            "configured": bool(fish_key and fish_reference_id),
            "key_present": bool(fish_key),
            "key_masked": _mask_fish_audio(fish_key),
            "reference_id_present": bool(fish_reference_id),
            "model": _fish_audio_model(),
            "status": "configured" if fish_key and fish_reference_id else "not_configured",
            "source": "server_env",
        },
        "brain": {
            "primary_provider": "groq",
            "primary_model": openrouter_model,
            "fallback_provider": "openrouter",
            "fallback_model": groq_model,
            "local_stub_available": True,
            "health": brain_provider_health(),
        },
        "stt": {
            "provider": "browser_or_faster_whisper",
            "configured": True,
        },
        "tts": {
            "provider_priority": tts_priority,
            "configured": bool((fish_key and fish_reference_id) or (eleven_key and voice_id)),
            "primary_provider": next((p for p in tts_priority if p == "browser_speech" or (p == "cartesia" and cartesia_key and cartesia_voice_id) or (p == "fish_audio" and fish_key and fish_reference_id) or (p == "elevenlabs" and eleven_key and voice_id)), "browser_speech"),
            "provider": next((p for p in tts_priority if p == "browser_speech" or (p == "cartesia" and cartesia_key and cartesia_voice_id) or (p == "fish_audio" and fish_key and fish_reference_id) or (p == "elevenlabs" and eleven_key and voice_id)), "browser_speech"),
        },
        "server": {
            "bind": "127.0.0.1",
            "port": 8765,
            "safe_localhost_only": True,
        },
    }


_brain_health: dict[str, dict[str, Any]] = {
    "openrouter": {"status": "unknown", "last_success_at": None, "last_error_type": None, "consecutive_failures": 0, "circuit_until": 0.0},
    "groq": {"status": "unknown", "last_success_at": None, "last_error_type": None, "consecutive_failures": 0, "circuit_until": 0.0},
}


def brain_provider_health() -> dict[str, Any]:
    now = time.time()
    safe: dict[str, Any] = {}
    for provider, state in _brain_health.items():
        circuit_until = float(state.get("circuit_until") or 0)
        status = "unhealthy" if circuit_until > now else state.get("status", "unknown")
        safe[provider] = {
            "status": status,
            "last_success_at": state.get("last_success_at"),
            "last_error_type": state.get("last_error_type"),
            "consecutive_failures": int(state.get("consecutive_failures") or 0),
            "circuit_until": int(circuit_until) if circuit_until > now else None,
        }
    return safe


def _provider_circuit_open(provider: str) -> bool:
    return float(_brain_health.get(provider, {}).get("circuit_until") or 0) > time.time()


def _record_provider_result(provider: str, result: dict[str, Any]) -> None:
    state = _brain_health[provider]
    if result.get("ok"):
        state.update({"status": "healthy", "last_success_at": _now_iso(), "last_error_type": None, "consecutive_failures": 0, "circuit_until": 0.0})
        return
    error_type = result.get("error_type") or result.get("error") or "failed"
    state["status"] = "unhealthy"
    state["last_error_type"] = error_type
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    if state["consecutive_failures"] >= 2 or error_type in {"timeout", "network_error", "client_network_blocked", "unexpected_response", "response_parse_failed"}:
        state["circuit_until"] = time.time() + 60
        _log_safe("brain_provider_circuit_open", {"provider": provider, "error_type": error_type, "circuit_seconds": 60})


async def test_openrouter() -> dict[str, Any]:
    result = await _openrouter_completion("Reply with exactly: AURA_OPENROUTER_OK", max_tokens=16, exact="AURA_OPENROUTER_OK")
    _log_safe("openrouter", result)
    return result


async def test_groq() -> dict[str, Any]:
    result = await _groq_completion("Reply with exactly: AURA_GROQ_OK", max_tokens=16, exact="AURA_GROQ_OK")
    _log_safe("groq", result)
    return result


async def test_provider_chain() -> dict[str, Any]:
    started = _perf_ms()
    primary = await _openrouter_completion("Reply with exactly: AURA_PROVIDER_CHAIN_OK", max_tokens=16, exact="AURA_PROVIDER_CHAIN_OK")
    if primary.get("ok"):
        return {
            "ok": True,
            "status": "primary_ok",
            "provider": primary.get("provider"),
            "model": primary.get("model"),
            "http_status": primary.get("http_status"),
            "response_text": primary.get("response_text"),
            "latency_ms": round(_perf_ms() - started),
            "fallback_used": False,
            "attempts": [_attempt_from_result(primary)],
            "model_attempts": primary.get("model_attempts", []),
            "request_url_used": primary.get("request_url_used"),
            "raw_error_preview": primary.get("raw_error_preview", ""),
            "error_type": None,
        }
    fallback = await _groq_completion("Reply with exactly: AURA_PROVIDER_CHAIN_OK", max_tokens=16, exact="AURA_PROVIDER_CHAIN_OK")
    attempts = [_attempt_from_result(primary), _attempt_from_result(fallback)]
    if fallback.get("ok"):
        return {
            "ok": True,
            "status": "fallback_used",
            "provider": fallback.get("provider"),
            "model": fallback.get("model"),
            "http_status": fallback.get("http_status"),
            "response_text": fallback.get("response_text"),
            "latency_ms": round(_perf_ms() - started),
            "fallback_used": True,
            "attempts": attempts,
            "model_attempts": (primary.get("model_attempts", []) or []) + (fallback.get("model_attempts", []) or []),
            "request_url_used": fallback.get("request_url_used"),
            "raw_error_preview": fallback.get("raw_error_preview", ""),
            "error_type": None,
        }
    return {
        "ok": True,
        "status": "local_stub",
        "provider": "local_stub",
        "model": "diagnostic_stub",
        "response_text": "Aura provider chain local stub online.",
        "latency_ms": round(_perf_ms() - started),
        "fallback_used": True,
        "attempts": attempts,
        "model_attempts": (primary.get("model_attempts", []) or []) + (fallback.get("model_attempts", []) or []),
        "request_url_used": fallback.get("request_url_used") or primary.get("request_url_used"),
        "raw_error_preview": fallback.get("raw_error_preview") or primary.get("raw_error_preview", ""),
        "error_type": "cloud_providers_unavailable",
    }


def tts_provider_status() -> dict[str, Any]:
    cartesia_key = _cartesia_key()
    cartesia_voice_id = _cartesia_voice_id()
    fish_key = _fish_audio_key()
    reference_id = _fish_audio_reference_id()
    eleven_key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    return {
        "cartesia": {
            "configured": bool(cartesia_key and cartesia_voice_id),
            "key_present": bool(cartesia_key),
            "key_masked": _mask_secret(cartesia_key, prefix=0, suffix=4),
            "voice_id_present": bool(cartesia_voice_id),
            "model": _cartesia_model(),
            "version": _cartesia_version(),
            "status": "configured" if cartesia_key and cartesia_voice_id else "not_configured",
        },
        "fish_audio": {
            "configured": bool(fish_key and reference_id),
            "key_present": bool(fish_key),
            "key_masked": _mask_fish_audio(fish_key),
            "reference_id_present": bool(reference_id),
            "model": _fish_audio_model(),
        },
        "elevenlabs": {
            "configured": bool(eleven_key and voice_id),
            "key_present": bool(eleven_key),
            "key_masked": _mask_elevenlabs(eleven_key),
            "voice_id_present": bool(voice_id),
            "voice_id_masked": _mask_voice_id(voice_id),
            "voice_id_source": _elevenlabs_voice_id_source(),
            "selected_client": _cached_selected_client,
        },
        "priority": _tts_provider_priority(),
    }


def tts_strict_status() -> dict[str, Any]:
    state = _cartesia_circuit_state()
    return {
        "strict_primary": _tts_strict_primary(),
        "required_provider": _tts_required_provider() or None,
        "browser_fallback_allowed": _tts_browser_fallback_allowed(),
        "provider_priority": _tts_provider_priority(),
        "cartesia_configured": bool(_cartesia_key() and _cartesia_voice_id()),
        "cartesia_circuit_open": bool(state.get("cartesia_tts_circuit_open")),
        "cartesia_circuit_reason": state.get("cartesia_circuit_reason") if state.get("cartesia_tts_circuit_open") else None,
        "cartesia_circuit_until": state.get("cartesia_circuit_until"),
        "cartesia_failure_count": int(_cartesia_tts_health.get("consecutive_failures") or 0),
        "last_cartesia_error_type": _cartesia_tts_health.get("last_error_type"),
        "last_successful_tts_client": _last_successful_tts_client.get("client") if _last_successful_tts_client.get("provider") == "cartesia" else None,
        "cartesia_cache_entries": sum(1 for key in _tts_audio_cache if str(key).startswith("cartesia|")),
        "known_cached_phrases": sorted(_TTS_CACHE_PHRASES),
    }


def reset_cartesia_state(*, clear_cache: bool = False) -> dict[str, Any]:
    _cartesia_tts_health.update({"last_error_type": None, "consecutive_failures": 0, "first_failure_at": 0.0, "circuit_until": 0.0})
    if _last_successful_tts_client.get("provider") == "cartesia":
        _last_successful_tts_client.clear()
    if clear_cache:
        for key in list(_tts_audio_cache):
            if str(key).startswith("cartesia|"):
                _tts_audio_cache.pop(key, None)
    return {
        "ok": True,
        "cartesia_circuit_open": False,
        "failure_count": 0,
        "last_error_type": None,
        "cache_entries": sum(1 for key in _tts_audio_cache if str(key).startswith("cartesia|")),
    }


def _cartesia_payload(text: str) -> dict[str, Any]:
    from . import tts

    output = _cartesia_output_settings()
    return {
        "model_id": _cartesia_model(),
        "transcript": tts.sanitize_text_for_tts(text),
        "voice": {"mode": "id", "id": _cartesia_voice_id()},
        "output_format": output,
        "language": "en",
        "generation_config": {"volume": 1, "speed": 1},
        "speed": "normal",
    }


def _cartesia_payload_validation(text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    payload = _cartesia_payload(text)
    raw_output = payload.get("output_format")
    output = raw_output if isinstance(raw_output, dict) else {}
    raw_voice = payload.get("voice")
    voice = raw_voice if isinstance(raw_voice, dict) else {}
    problems: list[str] = []
    if not payload.get("model_id"):
        problems.append("model_id_blank")
    if not voice.get("id"):
        problems.append("voice_id_blank")
    if not _cartesia_version():
        problems.append("cartesia_version_blank")
    if not isinstance(output.get("sample_rate"), int) or int(output.get("sample_rate") or 0) <= 0:
        problems.append("sample_rate_invalid")
    if any(value is None for value in output.values()):
        problems.append("output_format_contains_null")
    summary = {
        "model_id": payload.get("model_id"),
        "transcript_preview": _safe_preview(str(payload.get("transcript") or ""), 120),
        "transcript_length": len(str(payload.get("transcript") or "")),
        "voice": {"mode": voice.get("mode"), "id_present": bool(voice.get("id"))},
        "output_format": output,
        "language": payload.get("language"),
        "generation_config": payload.get("generation_config"),
        "version": _cartesia_version(),
        "valid": not problems,
        "problems": problems,
    }
    return (None if problems else payload), summary


def _cartesia_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_cartesia_key()}",
        "Cartesia-Version": _cartesia_version(),
        "Content-Type": "application/json",
    }


def _cartesia_success(client: str, audio: bytes, *, include_base64: bool, latency_ms: int, http_status: int, mime_type: str | None = None) -> dict[str, Any]:
    if not audio:
        return _cartesia_failure(client, "cartesia_audio_missing", "Cartesia returned empty audio.", http_status=http_status, latency_ms=latency_ms)
    encode_started = _perf_ms()
    audio_base64 = base64.b64encode(audio).decode("ascii") if include_base64 else None
    encode_ms = round(_perf_ms() - encode_started)
    return {
        "ok": True,
        "provider": "cartesia",
        "client": client,
        "selected_client": client,
        "model": _cartesia_model(),
        "version": _cartesia_version(),
        "voice_id_present": bool(_cartesia_voice_id()),
        "mime_type": (mime_type or ("audio/wav" if _cartesia_output_settings()["container"] == "wav" else "audio/mpeg")).split(";")[0] or "audio/wav",
        "audio_bytes": len(audio),
        "audio_base64": audio_base64,
        "audio_base64_present": bool(audio_base64),
        "audio_base64_encode_ms": encode_ms,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "error": None,
        "error_type": None,
    }


def _cartesia_failure(client: str, error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return _failure(
        "cartesia",
        error_type,
        message,
        client=client,
        model=_cartesia_model(),
        version=_cartesia_version(),
        voice_id_present=bool(_cartesia_voice_id()),
        raw_error_preview=_safe_preview(extra.pop("raw_error_preview", message)),
        **extra,
    )


def _cartesia_invalid_payload_failure(client: str, text: str) -> dict[str, Any] | None:
    payload, summary = _cartesia_payload_validation(text)
    if payload is not None:
        return None
    return _cartesia_failure(
        client,
        "cartesia_invalid_local_config",
        "Cartesia local request configuration is invalid.",
        http_status=None,
        latency_ms=0,
        payload_summary=summary,
        raw_error_preview="Cartesia local request configuration is invalid.",
    )


def _cartesia_circuit_open() -> bool:
    return float(_cartesia_tts_health.get("circuit_until") or 0) > time.time()


def _cartesia_circuit_state() -> dict[str, Any]:
    until = float(_cartesia_tts_health.get("circuit_until") or 0)
    return {
        "cartesia_tts_circuit_open": until > time.time(),
        "cartesia_circuit_reason": _cartesia_tts_health.get("last_error_type"),
        "cartesia_circuit_until": int(until) if until > time.time() else None,
        "cartesia_consecutive_failures": int(_cartesia_tts_health.get("consecutive_failures") or 0),
    }


def _record_cartesia_tts_result(result: dict[str, Any], *, mode: str) -> None:
    if result.get("ok"):
        _cartesia_tts_health.update({"last_error_type": None, "consecutive_failures": 0, "first_failure_at": 0.0, "circuit_until": 0.0})
        if result.get("selected_client"):
            _last_successful_tts_client.update({"provider": "cartesia", "client": result.get("selected_client"), "timestamp": _now_iso()})
        _log_safe("cartesia_circuit_reset", {"success": True, "source": f"{mode}_cartesia", "selected_client": result.get("selected_client")})
        return
    if mode != "voice_turn":
        return
    error_type = str(result.get("error_type") or result.get("error") or "cartesia_network_error")
    now = time.time()
    first_failure_at = float(_cartesia_tts_health.get("first_failure_at") or 0)
    if error_type != _cartesia_tts_health.get("last_error_type") or now - first_failure_at > 60:
        _cartesia_tts_health.update({"last_error_type": error_type, "consecutive_failures": 1, "first_failure_at": now, "circuit_until": 0.0})
        return
    failures = int(_cartesia_tts_health.get("consecutive_failures") or 0) + 1
    circuit_until = now + 60 if failures >= 3 and error_type in {"cartesia_network_error", "cartesia_client_network_blocked", "cartesia_dns_error", "cartesia_ssl_error", "cartesia_timeout"} else 0.0
    _cartesia_tts_health.update({"last_error_type": error_type, "consecutive_failures": failures, "circuit_until": circuit_until})
    if circuit_until:
        _log_safe("cartesia_tts_circuit_open", {"provider": "cartesia", "error_type": error_type, "circuit_seconds": 60})


async def _cartesia_tts_aiohttp(text: str, *, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    started = _perf_ms()
    payload, payload_summary = _cartesia_payload_validation(text)
    if payload is None:
        return _cartesia_invalid_payload_failure("aiohttp", text) or {}
    try:
        timeout = aiohttp.ClientTimeout(total=10 if mode == "voice_turn" else 30, connect=3 if mode == "voice_turn" else 10, sock_read=8 if mode == "voice_turn" else 30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(CARTESIA_TTS_URL, headers=_cartesia_headers(), json=payload) as resp:
                body = await resp.read()
                latency = round(_perf_ms() - started)
                if resp.status >= 400:
                    preview = _safe_preview(body.decode("utf-8", errors="replace"))
                    error_type = _cartesia_error_class(resp.status, preview)
                    return _cartesia_failure("aiohttp", error_type, f"Cartesia returned HTTP {resp.status}", http_status=resp.status, latency_ms=latency, raw_error_preview=preview, payload_summary=payload_summary)
                return _cartesia_success("aiohttp", body, include_base64=include_base64, latency_ms=latency, http_status=resp.status, mime_type=resp.headers.get("Content-Type"))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        return _cartesia_failure("aiohttp", "cartesia_timeout", str(exc), latency_ms=round(_perf_ms() - started))
    except (aiohttp.ClientError, OSError) as exc:
        return _cartesia_failure("aiohttp", _cartesia_error_class(None, str(exc)), str(exc), latency_ms=round(_perf_ms() - started))


def cartesia_tts_urllib(text: str, include_base64: bool = True, mode: str = "diagnostic") -> dict[str, Any]:
    started = _perf_ms()
    payload, payload_summary = _cartesia_payload_validation(text)
    if payload is None:
        return _cartesia_invalid_payload_failure("urllib", text) or {}
    request = urllib.request.Request(CARTESIA_TTS_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers=_cartesia_headers())
    try:
        with urllib.request.urlopen(request, timeout=10 if mode == "voice_turn" else 30) as response:
            audio = response.read()
            return _cartesia_success("urllib", audio, include_base64=include_base64, latency_ms=round(_perf_ms() - started), http_status=int(getattr(response, "status", 200)), mime_type=response.headers.get("Content-Type"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        error_type = _cartesia_error_class(exc.code, body)
        return _cartesia_failure("urllib", error_type, f"Cartesia returned HTTP {exc.code}", http_status=exc.code, latency_ms=round(_perf_ms() - started), raw_error_preview=body, payload_summary=payload_summary)
    except (urllib.error.URLError, OSError) as exc:
        return _cartesia_failure("urllib", _cartesia_error_class(None, str(exc)), str(exc), latency_ms=round(_perf_ms() - started))


async def _cartesia_tts_urllib(text: str, *, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    return await asyncio.to_thread(cartesia_tts_urllib, text, include_base64, mode)


async def _cartesia_tts_requests(text: str, *, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    try:
        import requests  # type: ignore
    except ImportError:
        return {"ok": False, "provider": "cartesia", "client": "requests", "error": "client_not_installed", "error_type": "client_not_installed", "model": _cartesia_model(), "voice_id_present": bool(_cartesia_voice_id())}

    def run() -> dict[str, Any]:
        started = _perf_ms()
        payload, payload_summary = _cartesia_payload_validation(text)
        if payload is None:
            return _cartesia_invalid_payload_failure("requests", text) or {}
        try:
            response = requests.post(CARTESIA_TTS_URL, headers=_cartesia_headers(), json=payload, timeout=10 if mode == "voice_turn" else 30)
            latency = round(_perf_ms() - started)
            if response.status_code >= 400:
                error_type = _cartesia_error_class(response.status_code, response.text)
                return _cartesia_failure("requests", error_type, f"Cartesia returned HTTP {response.status_code}", http_status=response.status_code, latency_ms=latency, raw_error_preview=response.text, payload_summary=payload_summary)
            return _cartesia_success("requests", response.content, include_base64=include_base64, latency_ms=latency, http_status=response.status_code, mime_type=response.headers.get("Content-Type"))
        except Exception as exc:
            return _cartesia_failure("requests", _cartesia_error_class(None, str(exc)), str(exc), latency_ms=round(_perf_ms() - started))

    return await asyncio.to_thread(run)


async def _cartesia_tts_httpx(text: str, *, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except ImportError:
        return {"ok": False, "provider": "cartesia", "client": "httpx", "error": "client_not_installed", "error_type": "client_not_installed", "model": _cartesia_model(), "voice_id_present": bool(_cartesia_voice_id())}
    started = _perf_ms()
    payload, payload_summary = _cartesia_payload_validation(text)
    if payload is None:
        return _cartesia_invalid_payload_failure("httpx", text) or {}
    try:
        async with httpx.AsyncClient(timeout=10 if mode == "voice_turn" else 30) as client:
            response = await client.post(CARTESIA_TTS_URL, headers=_cartesia_headers(), json=payload)
        latency = round(_perf_ms() - started)
        if response.status_code >= 400:
            error_type = _cartesia_error_class(response.status_code, response.text)
            return _cartesia_failure("httpx", error_type, f"Cartesia returned HTTP {response.status_code}", http_status=response.status_code, latency_ms=latency, raw_error_preview=response.text, payload_summary=payload_summary)
        return _cartesia_success("httpx", response.content, include_base64=include_base64, latency_ms=latency, http_status=response.status_code, mime_type=response.headers.get("Content-Type"))
    except Exception as exc:
        return _cartesia_failure("httpx", _cartesia_error_class(None, str(exc)), str(exc), latency_ms=round(_perf_ms() - started))


async def _cartesia_tts_curl(text: str, *, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    return await asyncio.to_thread(_cartesia_tts_curl_sync, text, include_base64, mode)


def _cartesia_tts_curl_sync(text: str, include_base64: bool = False, mode: str = "diagnostic") -> dict[str, Any]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return {"ok": False, "provider": "cartesia", "client": "curl_exe", "error": "curl_missing", "error_type": "curl_missing", "model": _cartesia_model(), "voice_id_present": bool(_cartesia_voice_id())}
    started = _perf_ms()
    payload, payload_summary = _cartesia_payload_validation(text)
    if payload is None:
        return _cartesia_invalid_payload_failure("curl_exe", text) or {}
    try:
        with tempfile.TemporaryDirectory(prefix="aura_cartesia_") as tmp:
            tmpdir = Path(tmp)
            request_path = tmpdir / "request.json"
            response_path = tmpdir / "response.audio"
            config_path = tmpdir / "curl.cfg"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            config_lines = [
                f'url = "{_curl_cfg_quote(CARTESIA_TTS_URL)}"',
                'request = "POST"',
                f'header = "Authorization: Bearer {_curl_cfg_quote(_cartesia_key())}"',
                f'header = "Cartesia-Version: {_curl_cfg_quote(_cartesia_version())}"',
                'header = "Content-Type: application/json"',
                f'data = "@{_curl_cfg_quote(str(request_path))}"',
                f'output = "{_curl_cfg_quote(str(response_path))}"',
                "max-time = 30", "silent", "show-error", 'write-out = "\\n%{http_code}"',
            ]
            config_path.write_text("\n".join(config_lines), encoding="utf-8")
            process = subprocess.run([curl, "--config", str(config_path)], capture_output=True, text=True, timeout=12 if mode == "voice_turn" else 35, check=False)
            latency = round(_perf_ms() - started)
            status = _parse_curl_http_status(process.stdout)
            audio = response_path.read_bytes() if response_path.exists() else b""
            if status and 200 <= status < 300:
                return _cartesia_success("curl_exe", audio, include_base64=include_base64, latency_ms=latency, http_status=status, mime_type="audio/wav")
            preview = (audio[:1000].decode("utf-8", errors="replace") if audio else process.stderr.strip()) or f"curl.exe exited {process.returncode}"
            error_type = _cartesia_error_class(status, preview) if status else _cartesia_error_class(None, preview)
            return _cartesia_failure("curl_exe", error_type, f"curl.exe Cartesia request failed{f' with HTTP {status}' if status else ''}", http_status=status, latency_ms=latency, raw_error_preview=preview, payload_summary=payload_summary)
    except (subprocess.SubprocessError, OSError) as exc:
        return _cartesia_failure("curl_exe", _cartesia_error_class(None, str(exc)), str(exc), latency_ms=round(_perf_ms() - started))


async def synthesize_cartesia_audio(text: str, include_base64: bool = True, purpose: str = "voice_turn", mode: str = "voice_turn", force_live: bool = False) -> dict[str, Any]:
    if mode == "voice_turn":
        cached = _cached_tts_result(text)
        if cached:
            return cached
        if _cartesia_circuit_open() and not force_live:
            state = _cartesia_circuit_state()
            return _failure(
                "cartesia",
                str(state.get("cartesia_circuit_reason") or "cartesia_circuit_open"),
                "Cartesia TTS circuit is temporarily open.",
                model=_cartesia_model(),
                version=_cartesia_version(),
                voice_id_present=bool(_cartesia_voice_id()),
                attempts=[],
                **state,
            )
    if not _cartesia_key():
        return _failure("cartesia", "cartesia_invalid_key", "Cartesia API key missing", model=_cartesia_model(), voice_id_present=bool(_cartesia_voice_id()), attempts=[])
    if not _cartesia_voice_id():
        return _failure("cartesia", "cartesia_invalid_payload_or_voice_id", "Cartesia voice ID missing", model=_cartesia_model(), voice_id_present=False, attempts=[])
    attempts: list[dict[str, Any]] = []
    clients = {
        "aiohttp": _cartesia_tts_aiohttp,
        "urllib": _cartesia_tts_urllib,
        "requests": _cartesia_tts_requests,
        "httpx": _cartesia_tts_httpx,
        "curl_exe": _cartesia_tts_curl,
    }
    preferred = _last_successful_tts_client.get("client") if _last_successful_tts_client.get("provider") == "cartesia" else None
    client_names = list(clients)
    if mode == "voice_turn":
        fast_order = []
        if preferred in clients:
            fast_order.append(str(preferred))
        fast_order.extend(["aiohttp", "curl_exe"])
        client_names = []
        for name in fast_order:
            if name in clients and name not in client_names:
                client_names.append(name)
        client_names = client_names[:2]
    for client_name in client_names:
        result = await clients[client_name](text, include_base64=include_base64, mode=mode)
        attempts.append(_attempt_from_result(result))
        if result.get("ok") and int(result.get("audio_bytes") or 0) > 0:
            result["selected_client"] = client_name
            result["attempts"] = attempts
            result["purpose"] = purpose
            if result.get("audio_base64") and len(text) <= 650:
                if len(_tts_audio_cache) >= 128:
                    _tts_audio_cache.pop(next(iter(_tts_audio_cache)))
                _tts_audio_cache[_tts_cache_key("cartesia", text)] = dict(result)
            _log_safe("cartesia", {key: value for key, value in result.items() if key != "audio_base64"})
            _record_cartesia_tts_result(result, mode=mode)
            return result
    last = attempts[-1] if attempts else {}
    error_type = next((str(item.get("error_type")) for item in reversed(attempts) if item.get("error_type")), "cartesia_network_error")
    result = {
        "ok": False, "provider": "cartesia", "model": _cartesia_model(), "voice_id_present": True,
        "selected_client": last.get("selected_client"), "http_status": last.get("http_status"),
        "audio_bytes": 0, "audio_base64": None, "audio_base64_present": False,
        "error_type": error_type, "error": error_type, "message": last.get("message") or f"All Cartesia TTS clients failed: {error_type}.",
        "raw_error_preview": next((item.get("raw_error_preview") for item in reversed(attempts) if item.get("raw_error_preview")), None),
        "latency_ms": sum(int(item.get("latency_ms") or 0) for item in attempts) or None, "attempts": attempts,
    }
    _log_safe("cartesia", result)
    _record_cartesia_tts_result(result, mode=mode)
    return result


async def synthesize_fish_audio(
    text: str,
    include_base64: bool = True,
    purpose: str = "voice_turn",
) -> dict[str, Any]:
    key = _fish_audio_key()
    reference_id = _fish_audio_reference_id()
    model = _fish_audio_model()
    if not key:
        result = _failure("fish_audio", "missing_key", "Fish Audio key missing", model=model, reference_id_present=bool(reference_id), attempts=[])
        _log_safe("fish_audio", result)
        return result
    if not reference_id:
        result = _failure("fish_audio", "missing_reference_id", "Fish Audio reference ID missing", model=model, reference_id_present=False, attempts=[])
        _log_safe("fish_audio", result)
        return result

    attempts: list[dict[str, Any]] = []
    for client_name, client in (
        ("aiohttp", _fish_audio_tts_aiohttp),
        ("urllib", _fish_audio_tts_urllib),
        ("requests", _fish_audio_tts_requests),
        ("httpx", _fish_audio_tts_httpx),
        ("curl_exe", _fish_audio_tts_curl),
    ):
        result = await client(text, include_base64=include_base64)
        attempts.append(_attempt_from_result(result))
        if result.get("ok"):
            result["selected_client"] = client_name
            result["attempts"] = attempts
            result["purpose"] = purpose
            _log_safe("fish_audio", {k: v for k, v in result.items() if k != "audio_base64"})
            return result

    last = attempts[-1] if attempts else {"error_type": "unknown"}
    error_type = _best_fish_audio_error(attempts, str(last.get("error_type") or "unknown"))
    selected_attempt = next((attempt for attempt in reversed(attempts) if attempt.get("error_type") == error_type), last)
    result = {
        "ok": False,
        "provider": "fish_audio",
        "selected_client": selected_attempt.get("selected_client"),
        "attempts": attempts,
        "model": model,
        "reference_id_present": True,
        "latency_ms": sum(int(a.get("latency_ms") or 0) for a in attempts) or None,
        "error": error_type,
        "error_type": error_type,
        "message": selected_attempt.get("message") or f"Fish Audio TTS failed: {error_type}.",
        "http_status": selected_attempt.get("http_status"),
        "audio_bytes": int(selected_attempt.get("audio_bytes") or 0),
        "audio_base64_present": bool(selected_attempt.get("audio_base64_present")),
        "raw_error_preview": next((a.get("raw_error_preview") for a in reversed(attempts) if a.get("raw_error_preview")), None),
    }
    _log_safe("fish_audio", result)
    return result


def _fish_audio_success(client: str, audio: bytes, *, include_base64: bool, latency_ms: int, http_status: int, mime_type: str | None = None) -> dict[str, Any]:
    result = {
        "ok": True,
        "provider": "fish_audio",
        "client": client,
        "selected_client": client,
        "model": _fish_audio_model(),
        "reference_id_present": True,
        "mime_type": (mime_type or "audio/mpeg").split(";")[0] or "audio/mpeg",
        "audio_bytes": len(audio),
        "audio_base64": base64.b64encode(audio).decode("ascii") if include_base64 else None,
        "latency_ms": latency_ms,
        "http_status": http_status,
        "error": None,
        "error_type": None,
    }
    if not audio:
        return _failure("fish_audio", "audio_missing", "Fish Audio returned empty audio.", client=client, model=_fish_audio_model(), reference_id_present=True, http_status=http_status, latency_ms=latency_ms, audio_bytes=0, mime_type=result["mime_type"])
    return result


def _fish_audio_failure(client: str, error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return _failure("fish_audio", error_type, message, client=client, model=_fish_audio_model(), reference_id_present=bool(_fish_audio_reference_id()), error_type=error_type, raw_error_preview=_safe_preview(extra.pop("raw_error_preview", message)), **extra)


def _best_fish_audio_error(attempts: list[dict[str, Any]], fallback: str) -> str:
    errors = [str(a.get("error_type") or "") for a in attempts if a.get("error_type")]
    for preferred in ("invalid_key", "invalid_payload_or_reference_id", "endpoint_or_reference_not_found", "payment_required_or_quota", "rate_limited", "forbidden", "provider_5xx"):
        if preferred in errors:
            return preferred
    if errors and all(e in {"client_network_blocked", "curl_missing", "client_not_installed"} for e in errors):
        return "client_network_blocked"
    if "client_network_blocked" in errors and fallback in {"network_error", "timeout", "dns_error", "ssl_error", "proxy_error"}:
        return "client_network_blocked"
    return fallback or "unknown"


async def _fish_audio_tts_aiohttp(text: str, *, include_base64: bool = False) -> dict[str, Any]:
    started = _perf_ms()
    try:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        headers = {"Authorization": f"Bearer {_fish_audio_key()}", "Content-Type": "application/json", "model": _fish_audio_model()}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(FISH_AUDIO_TTS_URL, headers=headers, json=_fish_audio_payload(text)) as resp:
                body = await resp.read()
                latency = round(_perf_ms() - started)
                if resp.status >= 400:
                    preview = _safe_preview(body.decode("utf-8", errors="replace"))
                    cls = _fish_audio_error_class(resp.status, preview)
                    return _fish_audio_failure("aiohttp", cls, f"Fish Audio returned HTTP {resp.status}", http_status=resp.status, latency_ms=latency, raw_error_preview=preview)
                return _fish_audio_success("aiohttp", body, include_base64=include_base64, latency_ms=latency, http_status=resp.status, mime_type=resp.headers.get("Content-Type"))
    except (TimeoutError, asyncio.TimeoutError) as exc:
        return _fish_audio_failure("aiohttp", "timeout", str(exc), latency_ms=round(_perf_ms() - started))
    except (aiohttp.ClientError, OSError) as exc:
        return _fish_audio_failure("aiohttp", _client_error_type(exc), str(exc), latency_ms=round(_perf_ms() - started))


def fish_audio_tts_urllib(text: str, include_base64: bool = True) -> dict[str, Any]:
    started = _perf_ms()
    body = json.dumps(_fish_audio_payload(text)).encode("utf-8")
    req = urllib.request.Request(
        FISH_AUDIO_TTS_URL,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {_fish_audio_key()}", "Content-Type": "application/json", "model": _fish_audio_model()},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio = resp.read()
            status = int(getattr(resp, "status", 200))
            return _fish_audio_success("urllib", audio, include_base64=include_base64, latency_ms=round(_perf_ms() - started), http_status=status, mime_type=resp.headers.get("Content-Type"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        cls = _fish_audio_error_class(exc.code, body_text)
        return _fish_audio_failure("urllib", cls, f"Fish Audio returned HTTP {exc.code}", http_status=exc.code, latency_ms=round(_perf_ms() - started), raw_error_preview=body_text)
    except urllib.error.URLError as exc:
        cls = _client_error_type(exc)
        return _fish_audio_failure("urllib", cls, str(exc.reason), latency_ms=round(_perf_ms() - started))
    except OSError as exc:
        cls = _client_error_type(exc)
        return _fish_audio_failure("urllib", cls, str(exc), latency_ms=round(_perf_ms() - started))


async def _fish_audio_tts_urllib(text: str, *, include_base64: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(fish_audio_tts_urllib, text, include_base64)


async def _fish_audio_tts_requests(text: str, *, include_base64: bool = False) -> dict[str, Any]:
    try:
        import requests  # type: ignore
    except ImportError:
        return {"ok": False, "client": "requests", "provider": "fish_audio", "error": "client_not_installed", "error_type": "client_not_installed"}

    def _run() -> dict[str, Any]:
        started = _perf_ms()
        try:
            resp = requests.post(
                FISH_AUDIO_TTS_URL,
                headers={"Authorization": f"Bearer {_fish_audio_key()}", "Content-Type": "application/json", "model": _fish_audio_model()},
                json=_fish_audio_payload(text),
                timeout=30,
            )
            latency = round(_perf_ms() - started)
            if resp.status_code >= 400:
                cls = _fish_audio_error_class(resp.status_code, resp.text)
                return _fish_audio_failure("requests", cls, f"Fish Audio returned HTTP {resp.status_code}", http_status=resp.status_code, latency_ms=latency, raw_error_preview=resp.text)
            return _fish_audio_success("requests", resp.content, include_base64=include_base64, latency_ms=latency, http_status=resp.status_code, mime_type=resp.headers.get("Content-Type"))
        except Exception as exc:
            return _fish_audio_failure("requests", _client_error_type(exc), str(exc), latency_ms=round(_perf_ms() - started))

    return await asyncio.to_thread(_run)


async def _fish_audio_tts_httpx(text: str, *, include_base64: bool = False) -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except ImportError:
        return {"ok": False, "client": "httpx", "provider": "fish_audio", "error": "client_not_installed", "error_type": "client_not_installed"}
    started = _perf_ms()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(FISH_AUDIO_TTS_URL, headers={"Authorization": f"Bearer {_fish_audio_key()}", "Content-Type": "application/json", "model": _fish_audio_model()}, json=_fish_audio_payload(text))
        latency = round(_perf_ms() - started)
        if resp.status_code >= 400:
            cls = _fish_audio_error_class(resp.status_code, resp.text)
            return _fish_audio_failure("httpx", cls, f"Fish Audio returned HTTP {resp.status_code}", http_status=resp.status_code, latency_ms=latency, raw_error_preview=resp.text)
        return _fish_audio_success("httpx", resp.content, include_base64=include_base64, latency_ms=latency, http_status=resp.status_code, mime_type=resp.headers.get("Content-Type"))
    except Exception as exc:
        return _fish_audio_failure("httpx", _client_error_type(exc), str(exc), latency_ms=round(_perf_ms() - started))


async def _fish_audio_tts_curl(text: str, *, include_base64: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(_fish_audio_tts_curl_sync, text, include_base64)


def _fish_audio_tts_curl_sync(text: str, include_base64: bool = False) -> dict[str, Any]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return {"ok": False, "client": "curl_exe", "provider": "fish_audio", "error": "curl_missing", "error_type": "curl_missing"}
    started = _perf_ms()
    try:
        with tempfile.TemporaryDirectory(prefix="aura_fish_audio_") as tmp:
            tmpdir = Path(tmp)
            request_path = tmpdir / "request.json"
            response_path = tmpdir / "response.audio"
            config_path = tmpdir / "curl.cfg"
            request_path.write_text(json.dumps(_fish_audio_payload(text)), encoding="utf-8")
            config_lines = [
                f'url = "{_curl_cfg_quote(FISH_AUDIO_TTS_URL)}"',
                'request = "POST"',
                f'header = "Authorization: Bearer {_curl_cfg_quote(_fish_audio_key())}"',
                'header = "Content-Type: application/json"',
                f'header = "model: {_curl_cfg_quote(_fish_audio_model())}"',
                f'data = "@{_curl_cfg_quote(str(request_path))}"',
                f'output = "{_curl_cfg_quote(str(response_path))}"',
                "max-time = 30",
                "silent",
                "show-error",
                'write-out = "\\n%{http_code}"',
            ]
            config_path.write_text("\n".join(config_lines), encoding="utf-8")
            proc = subprocess.run([curl, "--config", str(config_path)], capture_output=True, text=True, timeout=35, check=False)
            latency = round(_perf_ms() - started)
            status = _parse_curl_http_status(proc.stdout)
            audio = response_path.read_bytes() if response_path.exists() else b""
            if status and 200 <= status < 300:
                return _fish_audio_success("curl_exe", audio, include_base64=include_base64, latency_ms=latency, http_status=status, mime_type="audio/mpeg")
            body_text = audio[:1000].decode("utf-8", errors="replace") if audio else ""
            stderr = proc.stderr.strip()
            preview = body_text or stderr or f"curl.exe exited {proc.returncode}"
            cls = _fish_audio_error_class(status, preview) if status else _client_error_type(Exception(preview))
            return _fish_audio_failure("curl_exe", cls, f"curl.exe Fish Audio request failed{f' with HTTP {status}' if status else ''}", http_status=status, latency_ms=latency, raw_error_preview=preview)
    except (subprocess.SubprocessError, OSError) as exc:
        return _fish_audio_failure("curl_exe", _client_error_type(exc), str(exc), latency_ms=round(_perf_ms() - started))


async def synthesize_tts_chain(text: str, *, include_audio: bool = True, purpose: str = "voice_turn", mode: str = "voice_turn") -> dict[str, Any]:
    chain_started = _perf_ms()
    attempts: list[dict[str, Any]] = []
    cartesia_attempted = False
    fish_attempted = False
    cartesia_error_type = None
    eleven_attempted = False
    fish_error_type = None
    eleven_error_type = None
    strict_cartesia = _cartesia_strict_mode()
    browser_fallback_allowed = _tts_browser_fallback_allowed()
    provider_priority = _tts_provider_priority()
    chain_priority = provider_priority
    for provider in chain_priority:
        if provider == "cartesia":
            if not (_cartesia_key() and _cartesia_voice_id()):
                cartesia_error_type = "not_configured"
                attempts.append({"provider": "cartesia", "attempted": False, "success": False, "error_type": cartesia_error_type, "message": "Cartesia key or voice ID missing.", "http_status": None, "selected_client": None, "audio_bytes": 0, "audio_base64_present": False})
                continue
            cartesia_attempted = True
            try:
                result = await asyncio.wait_for(
                    synthesize_cartesia_audio(
                        text,
                        include_base64=include_audio,
                        purpose=purpose,
                        mode=mode,
                        force_live=(_cartesia_strict_mode() and mode == "voice_turn"),
                    ),
                    timeout=_cartesia_tts_timeout_seconds() if mode == "voice_turn" else None,
                )
            except asyncio.TimeoutError:
                result = {
                    "ok": False,
                    "provider": "cartesia",
                    "error": "cartesia_unavailable",
                    "error_type": "cartesia_unavailable",
                    "selected_client": "timeout_guard",
                    "audio_bytes": 0,
                    "audio_base64": None,
                    "audio_base64_present": False,
                    "latency_ms": round(_cartesia_tts_timeout_seconds() * 1000),
                    "attempts": [{
                        "provider": "cartesia",
                        "attempted": True,
                        "success": False,
                        "ok": False,
                        "status": "failed",
                        "error_type": "cartesia_unavailable",
                        "reason": "cartesia_unavailable",
                        "message": "Cartesia TTS timed out.",
                        "selected_client": "timeout_guard",
                        "audio_bytes": 0,
                        "audio_base64_present": False,
                    }],
                }
            attempts.extend(result.get("attempts", [_attempt_from_result(result)]))
            cartesia_error_type = result.get("error_type") or result.get("error")
            if result.get("ok") and int(result.get("audio_bytes") or 0) > 0:
                result["tts_provider_attempts"] = attempts
                result["tts_debug"] = _tts_debug_from_chain(attempts, cartesia_attempted, True, cartesia_error_type, fish_attempted, False, fish_error_type, eleven_attempted, False, eleven_error_type, False, audio_base64_present=bool(result.get("audio_base64")), tts_total_ms=round(_perf_ms() - chain_started), cache_hit=bool(result.get("tts_cache_hit")))
                result["tts_debug"].update({"strict_primary": strict_cartesia, "required_provider": _tts_required_provider() or None, "browser_fallback_allowed": browser_fallback_allowed, "fallback_attempted": False})
                return result
            if strict_cartesia:
                return _strict_cartesia_unavailable_result(
                    attempts,
                    cartesia_attempted,
                    cartesia_error_type,
                    fish_attempted,
                    fish_error_type,
                    eleven_attempted,
                    eleven_error_type,
                    "cartesia_unavailable",
                    chain_started,
                    result.get("selected_client"),
                )
        elif provider == "fish_audio":
            if not (_fish_audio_key() and _fish_audio_reference_id()):
                fish_error_type = "not_configured"
                attempts.append({
                    "provider": "fish_audio",
                    "attempted": False,
                    "success": False,
                    "error_type": fish_error_type,
                    "message": "Fish Audio key or reference ID missing.",
                    "http_status": None,
                    "selected_client": None,
                    "audio_bytes": 0,
                    "audio_base64_present": False,
                })
                continue
            fish_attempted = True
            result = await synthesize_fish_audio(text, include_base64=include_audio, purpose=purpose)
            attempts.extend(result.get("attempts", [_attempt_from_result(result)]))
            fish_error_type = result.get("error_type") or result.get("error")
            if result.get("ok") and int(result.get("audio_bytes") or 0) > 0:
                result["tts_provider_attempts"] = attempts
                result["tts_debug"] = _tts_debug_from_chain(attempts, cartesia_attempted, False, cartesia_error_type, fish_attempted, True, fish_error_type, eleven_attempted, False, eleven_error_type, False, audio_base64_present=bool(result.get("audio_base64")), tts_total_ms=round(_perf_ms() - chain_started), cache_hit=False)
                result["tts_debug"].update({"strict_primary": strict_cartesia, "required_provider": _tts_required_provider() or None, "browser_fallback_allowed": browser_fallback_allowed, "fallback_attempted": bool(cartesia_attempted and not result["tts_debug"].get("cartesia_success"))})
                return result
        elif provider == "elevenlabs":
            if not (_elevenlabs_key() and _elevenlabs_voice_id()):
                eleven_error_type = "not_configured"
                attempts.append({
                    "provider": "elevenlabs",
                    "attempted": False,
                    "success": False,
                    "error_type": eleven_error_type,
                    "message": "ElevenLabs key or voice ID missing.",
                    "http_status": None,
                    "selected_client": None,
                    "audio_bytes": 0,
                    "audio_base64_present": False,
                })
                continue
            eleven_attempted = True
            result = await synthesize_elevenlabs(text, include_audio=include_audio)
            attempts.extend(result.get("attempts", [_attempt_from_result(result)]))
            eleven_error_type = result.get("error_type") or result.get("error")
            if result.get("ok") and int(result.get("audio_bytes") or 0) > 0:
                result["tts_provider_attempts"] = attempts
                result["tts_debug"] = _tts_debug_from_chain(attempts, cartesia_attempted, False, cartesia_error_type, fish_attempted, False, fish_error_type, eleven_attempted, True, eleven_error_type, False, audio_base64_present=bool(result.get("audio_base64")), tts_total_ms=round(_perf_ms() - chain_started), cache_hit=False)
                result["tts_debug"].update({"strict_primary": strict_cartesia, "required_provider": _tts_required_provider() or None, "browser_fallback_allowed": browser_fallback_allowed, "fallback_attempted": bool(cartesia_attempted and not result["tts_debug"].get("cartesia_success"))})
                return result
        elif provider == "browser_speech":
            break

    # Determine strongest fallback error
    fallback_reason = _determine_fallback_reason(cartesia_error_type, fish_error_type, eleven_error_type, attempts)
    if strict_cartesia:
        return _strict_cartesia_unavailable_result(
            attempts,
            cartesia_attempted,
            cartesia_error_type,
            fish_attempted,
            fish_error_type,
            eleven_attempted,
            eleven_error_type,
            "cartesia_unavailable",
            chain_started,
            None,
        )

    return {
        "ok": False,
        "provider": "browser_speech",
        "selected_client": None,
        "mime_type": None,
        "audio_bytes": 0,
        "audio_base64": None,
        "audio_base64_present": False,
        "latency_ms": sum(int(a.get("latency_ms") or 0) for a in attempts) or 0,
        "error": fallback_reason,
        "error_type": fallback_reason,
        "fallback_reason": fallback_reason,
        "tts_provider_attempts": attempts,
        "attempts": attempts,
        "tts_debug": _tts_debug_from_chain(attempts, cartesia_attempted, False, cartesia_error_type, fish_attempted, False, fish_error_type, eleven_attempted, False, eleven_error_type, True, fallback_reason_override=fallback_reason, audio_base64_present=False, tts_total_ms=round(_perf_ms() - chain_started), cache_hit=False),
    }


def _determine_fallback_reason(cartesia_err: str | None, fish_err: str | None, eleven_err: str | None, attempts: list[dict[str, Any]]) -> str:
    if cartesia_err and cartesia_err not in ("not_configured", "unknown", "cloud_tts_unavailable"):
        return f"cartesia_{cartesia_err}" if not cartesia_err.startswith("cartesia_") else cartesia_err
    if fish_err and fish_err not in ("not_configured", "unknown", "cloud_tts_unavailable"):
        return f"fish_audio_{fish_err}" if not fish_err.startswith("fish_audio_") else fish_err
    if eleven_err and eleven_err not in ("not_configured", "unknown", "cloud_tts_unavailable"):
        return f"elevenlabs_{eleven_err}" if not eleven_err.startswith("elevenlabs_") else eleven_err
    if cartesia_err == "not_configured" and fish_err == "not_configured" and eleven_err == "not_configured":
        return "provider_not_configured"
    if fish_err == "not_configured":
        return "fish_audio_not_configured"
    if eleven_err == "not_configured":
        return "elevenlabs_not_configured"
    if attempts:
        for a in reversed(attempts):
            err = a.get("error_type") or a.get("error")
            prov = a.get("provider") or "tts"
            if err and err != "unknown":
                return f"{prov}_{err}" if not err.startswith(f"{prov}_") else err
    return "tts_chain_audio_missing"


def _strict_cartesia_unavailable_result(
    attempts: list[dict[str, Any]],
    cartesia_attempted: bool,
    cartesia_error_type: Any,
    fish_attempted: bool,
    fish_error_type: Any,
    eleven_attempted: bool,
    eleven_error_type: Any,
    fallback_reason: str,
    chain_started: float,
    selected_client: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": "cartesia",
        "audio_provider": "cartesia",
        "selected_client": selected_client,
        "mime_type": None,
        "audio_bytes": 0,
        "audio_base64": None,
        "audio_base64_present": False,
        "latency_ms": sum(int(a.get("latency_ms") or 0) for a in attempts) or 0,
        "error": fallback_reason,
        "error_type": fallback_reason,
        "fallback_reason": fallback_reason,
        "strict_mode_blocked_browser_fallback": True,
        "fallback_attempted": False,
        "tts_provider_attempts": attempts,
        "attempts": attempts,
        "tts_debug": {
            **_tts_debug_from_chain(
                attempts,
                cartesia_attempted,
                False,
                cartesia_error_type,
                fish_attempted,
                False,
                fish_error_type,
                eleven_attempted,
                False,
                eleven_error_type,
                False,
                fallback_reason_override=fallback_reason,
                audio_base64_present=False,
                tts_total_ms=round(_perf_ms() - chain_started),
                cache_hit=False,
            ),
            "strict_primary": True,
            "required_provider": "cartesia",
            "browser_fallback_allowed": False,
            "browser_speech_fallback_used": False,
            "strict_mode_blocked_browser_fallback": True,
            "final_provider": "cartesia",
            "fallback_reason": fallback_reason,
            "fallback_attempted": False,
        },
    }


def _tts_debug_from_chain(
    attempts: list[dict[str, Any]],
    cartesia_attempted: bool,
    cartesia_success: bool,
    cartesia_error_type: Any,
    fish_attempted: bool,
    fish_success: bool,
    fish_error_type: Any,
    eleven_attempted: bool,
    eleven_success: bool,
    eleven_error_type: Any,
    browser_fallback: bool,
    fallback_reason_override: str | None = None,
    audio_base64_present: bool = False,
    tts_total_ms: int = 0,
    cache_hit: bool = False,
) -> dict[str, Any]:
    final_provider = "browser_speech"
    audio_bytes = 0
    for attempt in reversed(attempts):
        if attempt.get("ok") or attempt.get("success"):
            final_provider = str(attempt.get("provider") or attempt.get("client") or final_provider)
            audio_bytes = int(attempt.get("audio_bytes") or 0)
            break

    fallback_reason = None
    if browser_fallback:
        fallback_reason = fallback_reason_override or _determine_fallback_reason(cartesia_error_type, fish_error_type, eleven_error_type, attempts)

    return {
        "requested_auto_tts": True,
        "provider_priority": _tts_provider_priority(),
        "tts_provider_attempts": attempts,
        "cartesia_attempted": cartesia_attempted,
        "cartesia_success": cartesia_success,
        "cartesia_error_type": None if cartesia_success else cartesia_error_type,
        "fish_audio_attempted": fish_attempted,
        "fish_audio_success": fish_success,
        "fish_audio_error_type": None if fish_success else fish_error_type,
        "elevenlabs_attempted": eleven_attempted,
        "elevenlabs_success": eleven_success,
        "elevenlabs_error_type": None if eleven_success else eleven_error_type,
        "browser_speech_fallback_used": browser_fallback,
        "fallback_reason": fallback_reason,
        "final_provider": final_provider,
        "audio_base64_present": audio_base64_present,
        "audio_bytes": audio_bytes,
        "tts_total_ms": tts_total_ms,
        "cartesia_ms": sum(int(a.get("latency_ms") or 0) for a in attempts if a.get("provider") == "cartesia"),
        "fish_audio_ms": sum(int(a.get("latency_ms") or 0) for a in attempts if a.get("provider") == "fish_audio"),
        "elevenlabs_ms": sum(int(a.get("latency_ms") or 0) for a in attempts if a.get("provider") == "elevenlabs"),
        "selected_client": next((a.get("selected_client") for a in reversed(attempts) if a.get("success") or a.get("ok")), None),
        "audio_download_ms": sum(int(a.get("latency_ms") or 0) for a in attempts),
        "audio_base64_encode_ms": sum(int(a.get("audio_base64_encode_ms") or 0) for a in attempts),
        "provider_attempt_count": len(attempts),
        "fallback_attempt_count": sum(1 for a in attempts if not (a.get("success") or a.get("ok")) and a.get("attempted", True)),
        "cache_hit": cache_hit,
    }


async def test_fish_audio(*, include_audio: bool = False) -> dict[str, Any]:
    result = await synthesize_fish_audio("A.U.R.A voice test.", include_base64=include_audio, purpose="diagnostic")
    urllib_root = _fish_audio_root_check_urllib()
    curl_root = fish_audio_root_check_curl()
    root = urllib_root if urllib_root.get("ok") else curl_root
    key = _fish_audio_key()
    reference_id = _fish_audio_reference_id()
    return {
        "ok": bool(result.get("ok")),
        "provider": "fish_audio",
        "configured": bool(key and reference_id),
        "key_present": bool(key),
        "reference_id_present": bool(reference_id),
        "model": result.get("model") or _fish_audio_model(),
        "selected_client": result.get("selected_client"),
        "http_status": result.get("http_status"),
        "audio_bytes": result.get("audio_bytes", 0),
        "mime_type": result.get("mime_type"),
        "latency_ms": result.get("latency_ms"),
        "error_type": result.get("error_type"),
        "message": result.get("message"),
        "raw_error_preview": result.get("raw_error_preview"),
        "attempts": result.get("attempts", []),
        "root_connectivity": root,
        **({"audio_base64": result.get("audio_base64")} if include_audio and result.get("audio_base64") else {}),
    }


async def test_cartesia(*, include_audio: bool = False, text: str = "Awake, Sir.") -> dict[str, Any]:
    result = await synthesize_cartesia_audio(text, include_base64=include_audio, purpose="diagnostic", mode="diagnostic", force_live=True)
    return {
        "ok": bool(result.get("ok")),
        "provider": "cartesia",
        "configured": bool(_cartesia_key() and _cartesia_voice_id()),
        "model": result.get("model") or _cartesia_model(),
        "version": _cartesia_version(),
        "voice_id_present": bool(_cartesia_voice_id()),
        "selected_client": result.get("selected_client"),
        "http_status": result.get("http_status"),
        "audio_bytes": result.get("audio_bytes", 0),
        "mime_type": result.get("mime_type"),
        "latency_ms": result.get("latency_ms"),
        "error_type": result.get("error_type"),
        "raw_error_preview": result.get("raw_error_preview"),
        "message": result.get("message"),
        "attempts": result.get("attempts", []),
        **({"audio_base64": result.get("audio_base64")} if include_audio and result.get("audio_base64") else {}),
    }


async def test_tts_chain(*, include_audio: bool = False) -> dict[str, Any]:
    result = await synthesize_tts_chain("A.U.R.A voice test.", include_audio=include_audio, purpose="diagnostic_chain", mode="diagnostic")
    has_b64 = bool(result.get("audio_base64"))
    tts_debug = dict(result.get("tts_debug", {}))
    if include_audio:
        tts_debug["audio_base64_present"] = has_b64
    return {
        "ok": bool(result.get("ok")),
        "provider": result.get("provider"),
        "selected_client": result.get("selected_client"),
        "model": result.get("model"),
        "audio_bytes": result.get("audio_bytes", 0),
        "mime_type": result.get("mime_type"),
        "audio_base64_present": has_b64,
        "latency_ms": result.get("latency_ms"),
        "error_type": result.get("error_type"),
        "attempts": result.get("tts_provider_attempts", result.get("attempts", [])),
        "tts_debug": tts_debug,
        **({"audio_base64": result.get("audio_base64")} if include_audio and has_b64 else {}),
    }


async def synthesize_elevenlabs(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    if not key:
        result = _failure("elevenlabs", "missing_key", "ElevenLabs key missing", voice_id_present=bool(voice_id))
        _log_safe("elevenlabs", result)
        return result
    if not voice_id:
        result = _failure("elevenlabs", "elevenlabs_voice_id_missing", "ElevenLabs voice id missing", voice_id_present=False)
        _log_safe("elevenlabs", result)
        return result

    attempts: list[dict[str, Any]] = []
    for client_name, client in (
        ("aiohttp", _elevenlabs_tts_aiohttp),
        ("urllib", _elevenlabs_tts_urllib),
        ("requests", _elevenlabs_tts_requests),
        ("httpx", _elevenlabs_tts_httpx),
        ("curl_exe", _elevenlabs_tts_curl),
    ):
        result = await client(text, include_audio=include_audio)
        attempts.append(_attempt_from_result(result))
        if result.get("ok"):
            result["selected_client"] = client_name
            result["attempts"] = attempts
            _log_safe("elevenlabs", {k: v for k, v in result.items() if k != "audio_base64"})
            return result
    last = attempts[-1] if attempts else {"error_type": "unknown"}
    error_type = _best_elevenlabs_error(attempts, last.get("error_type") or "unknown")
    result = {
        "ok": False,
        "provider": "elevenlabs",
        "selected_client": None,
        "attempts": attempts,
        "voice_id_present": True,
        "latency_ms": sum(int(a.get("latency_ms") or 0) for a in attempts) or None,
        "error": error_type,
        "error_type": error_type,
        "message": "All ElevenLabs TTS clients failed.",
    }
    _log_safe("elevenlabs", result)
    return result


def _best_elevenlabs_error(attempts: list[dict[str, Any]], fallback: str) -> str:
    errors = [str(a.get("error_type") or "") for a in attempts if a.get("error_type")]
    for preferred in (
        "invalid_key",
        "voice_not_found_or_bad_endpoint",
        "quota_or_plan_required",
        "quota_or_rate_limit",
        "provider_5xx",
    ):
        if preferred in errors:
            return preferred
    if errors and all(e in {"client_network_blocked", "curl_missing", "client_not_installed"} for e in errors):
        return "client_network_blocked"
    if "client_network_blocked" in errors and fallback == "network_error":
        return "client_network_blocked"
    return fallback or "unknown"


async def _elevenlabs_tts_aiohttp(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    started = _perf_ms()
    try:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        headers = {
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = _elevenlabs_payload(text)
        url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                audio = await resp.read()
                latency = round(_perf_ms() - started)
                if resp.status >= 400:
                    error_text = audio.decode("utf-8", errors="replace")
                    cls = _error_class(resp.status, error_text, provider="elevenlabs")
                    return _failure("elevenlabs", cls, f"ElevenLabs returned HTTP {resp.status}", client="aiohttp", voice_id_present=True, http_status=resp.status, latency_ms=latency, error_type=cls)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        cls = _client_error_type(exc)
        return _failure("elevenlabs", cls, str(exc), client="aiohttp", voice_id_present=True, latency_ms=round(_perf_ms() - started), error_type=cls)

    result = {
        "ok": True,
        "client": "aiohttp",
        "provider": "elevenlabs",
        "voice_id_present": True,
        "latency_ms": latency,
        "audio_bytes": len(audio),
        "audio_base64": None,
        "mime_type": "audio/mpeg",
        "error": None,
        "error_type": None,
        "http_status": 200,
    }
    if include_audio:
        import base64
        result["audio_base64"] = base64.b64encode(audio).decode("ascii")
    return result


def elevenlabs_tts_urllib(text: str, include_audio: bool = True) -> dict[str, Any]:
    key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    started = _perf_ms()
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    body = json.dumps(_elevenlabs_payload(text)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio = resp.read()
            status = int(getattr(resp, "status", 200))
            result = {
                "ok": True,
                "client": "urllib",
                "provider": "elevenlabs",
                "mime_type": resp.headers.get("Content-Type", "audio/mpeg").split(";")[0] or "audio/mpeg",
                "audio_base64": None,
                "audio_bytes": len(audio),
                "latency_ms": round(_perf_ms() - started),
                "error": None,
                "error_type": None,
                "http_status": status,
                "voice_id_present": True,
            }
            if include_audio:
                import base64
                result["audio_base64"] = base64.b64encode(audio).decode("ascii")
            return result
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        cls = _error_class(exc.code, body_text, provider="elevenlabs")
        return _failure("elevenlabs", cls, f"ElevenLabs returned HTTP {exc.code}", client="urllib", error_type=cls, http_status=exc.code, voice_id_present=True, latency_ms=round(_perf_ms() - started))
    except urllib.error.URLError as exc:
        cls = _client_error_type(exc)
        return _failure("elevenlabs", cls, str(exc.reason), client="urllib", error_type=cls, voice_id_present=True, latency_ms=round(_perf_ms() - started))
    except OSError as exc:
        cls = _client_error_type(exc)
        return _failure("elevenlabs", cls, str(exc), client="urllib", error_type=cls, voice_id_present=True, latency_ms=round(_perf_ms() - started))


async def _elevenlabs_tts_urllib(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(elevenlabs_tts_urllib, text, include_audio)


async def _elevenlabs_tts_requests(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    try:
        import requests  # type: ignore
    except ImportError:
        return {"ok": False, "client": "requests", "provider": "elevenlabs", "error": "client_not_installed", "error_type": "client_not_installed"}

    def _run() -> dict[str, Any]:
        started = _perf_ms()
        try:
            resp = requests.post(
                ELEVENLABS_TTS_URL.format(voice_id=_elevenlabs_voice_id()),
                headers={"xi-api-key": _elevenlabs_key(), "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json=_elevenlabs_payload(text),
                timeout=30,
            )
            latency = round(_perf_ms() - started)
            if resp.status_code >= 400:
                cls = _error_class(resp.status_code, resp.text, provider="elevenlabs")
                return _failure("elevenlabs", cls, f"ElevenLabs returned HTTP {resp.status_code}", client="requests", error_type=cls, http_status=resp.status_code, voice_id_present=True, latency_ms=latency)
            result = {"ok": True, "client": "requests", "provider": "elevenlabs", "voice_id_present": True, "latency_ms": latency, "audio_bytes": len(resp.content), "audio_base64": None, "mime_type": resp.headers.get("Content-Type", "audio/mpeg").split(";")[0] or "audio/mpeg", "error": None, "error_type": None, "http_status": resp.status_code}
            if include_audio:
                import base64
                result["audio_base64"] = base64.b64encode(resp.content).decode("ascii")
            return result
        except Exception as exc:
            cls = _client_error_type(exc)
            return _failure("elevenlabs", cls, str(exc), client="requests", error_type=cls, voice_id_present=True, latency_ms=round(_perf_ms() - started))

    return await asyncio.to_thread(_run)


async def _elevenlabs_tts_httpx(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    try:
        import httpx  # type: ignore
    except ImportError:
        return {"ok": False, "client": "httpx", "provider": "elevenlabs", "error": "client_not_installed", "error_type": "client_not_installed"}

    started = _perf_ms()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                ELEVENLABS_TTS_URL.format(voice_id=_elevenlabs_voice_id()),
                headers={"xi-api-key": _elevenlabs_key(), "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json=_elevenlabs_payload(text),
            )
        latency = round(_perf_ms() - started)
        if resp.status_code >= 400:
            cls = _error_class(resp.status_code, resp.text, provider="elevenlabs")
            return _failure("elevenlabs", cls, f"ElevenLabs returned HTTP {resp.status_code}", client="httpx", error_type=cls, http_status=resp.status_code, voice_id_present=True, latency_ms=latency)
        result = {"ok": True, "client": "httpx", "provider": "elevenlabs", "voice_id_present": True, "latency_ms": latency, "audio_bytes": len(resp.content), "audio_base64": None, "mime_type": resp.headers.get("Content-Type", "audio/mpeg").split(";")[0] or "audio/mpeg", "error": None, "error_type": None, "http_status": resp.status_code}
        if include_audio:
            import base64
            result["audio_base64"] = base64.b64encode(resp.content).decode("ascii")
        return result
    except Exception as exc:
        cls = _client_error_type(exc)
        return _failure("elevenlabs", cls, str(exc), client="httpx", error_type=cls, voice_id_present=True, latency_ms=round(_perf_ms() - started))


async def _elevenlabs_tts_curl(text: str, *, include_audio: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(_elevenlabs_tts_curl_sync, text, include_audio)


def _curl_cfg_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _elevenlabs_tts_curl_sync(text: str, include_audio: bool = False) -> dict[str, Any]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return {"ok": False, "client": "curl_exe", "provider": "elevenlabs", "error": "curl_missing", "error_type": "curl_missing"}

    started = _perf_ms()
    try:
        with tempfile.TemporaryDirectory(prefix="aura_elevenlabs_") as tmp:
            tmpdir = Path(tmp)
            request_path = tmpdir / "request.json"
            response_path = tmpdir / "response.audio"
            config_path = tmpdir / "curl.cfg"
            request_path.write_text(json.dumps(_elevenlabs_payload(text)), encoding="utf-8")
            url = ELEVENLABS_TTS_URL.format(voice_id=_elevenlabs_voice_id())
            config_lines = [
                f'url = "{_curl_cfg_quote(url)}"',
                'request = "POST"',
                f'header = "xi-api-key: {_curl_cfg_quote(_elevenlabs_key())}"',
                'header = "Content-Type: application/json"',
                'header = "Accept: audio/mpeg"',
                f'data = "@{_curl_cfg_quote(str(request_path))}"',
                f'output = "{_curl_cfg_quote(str(response_path))}"',
                "max-time = 30",
                "silent",
                "show-error",
                'write-out = "\\n%{http_code}"',
            ]
            config_path.write_text("\n".join(config_lines), encoding="utf-8")
            proc = subprocess.run(
                [curl, "--config", str(config_path)],
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            latency = round(_perf_ms() - started)
            status = _parse_curl_http_status(proc.stdout)
            audio = response_path.read_bytes() if response_path.exists() else b""
            if status and 200 <= status < 300 and audio:
                result = {
                    "ok": True,
                    "client": "curl_exe",
                    "provider": "elevenlabs",
                    "voice_id_present": True,
                    "latency_ms": latency,
                    "audio_bytes": len(audio),
                    "audio_base64": None,
                    "mime_type": "audio/mpeg",
                    "error": None,
                    "error_type": None,
                    "http_status": status,
                }
                if include_audio:
                    import base64
                    result["audio_base64"] = base64.b64encode(audio).decode("ascii")
                return result
            stderr = proc.stderr.strip()
            body_text = ""
            if audio:
                body_text = audio[:1000].decode("utf-8", errors="replace")
            cls = _error_class(status, body_text or stderr, provider="elevenlabs") if status else _client_error_type(Exception(stderr)) if stderr else "curl_failed"
            return _failure("elevenlabs", cls, f"curl.exe ElevenLabs request failed{f' with HTTP {status}' if status else ''}", client="curl_exe", error_type=cls, http_status=status, voice_id_present=True, latency_ms=latency)
    except (subprocess.SubprocessError, OSError) as exc:
        cls = _client_error_type(exc)
        return _failure("elevenlabs", cls, str(exc), client="curl_exe", error_type=cls, voice_id_present=True, latency_ms=round(_perf_ms() - started))


async def test_elevenlabs(*, include_audio: bool = False) -> dict[str, Any]:
    root = test_elevenlabs_network()
    result = await synthesize_elevenlabs("Aura voice diagnostic online.", include_audio=include_audio)
    result["root_connectivity"] = root
    if root.get("root", {}).get("classification") == "reachable_root_404_expected":
        result["root_connectivity_note"] = root.get("root", {}).get("message")
    return result


async def tts_preflight() -> dict[str, Any]:
    global _cached_elevenlabs_ready, _cached_selected_client
    key = _elevenlabs_key()
    voice_id = _elevenlabs_voice_id()
    if not key or not voice_id:
        _cached_elevenlabs_ready = False
        _cached_selected_client = None
        return {
            "ok": False,
            "elevenlabs_ready": False,
            "selected_client": None,
            "error_type": "elevenlabs_not_configured",
            "message": "ElevenLabs key or Voice ID missing.",
        }
    result = await synthesize_elevenlabs("Aura preflight.", include_audio=True)
    if result.get("ok"):
        _cached_elevenlabs_ready = True
        _cached_selected_client = result.get("selected_client")
        return {
            "ok": True,
            "elevenlabs_ready": True,
            "selected_client": result.get("selected_client"),
            "audio_bytes": result.get("audio_bytes", 0),
            "mime_type": result.get("mime_type", "audio/mpeg"),
            "latency_ms": result.get("latency_ms"),
            "attempts": result.get("attempts", []),
        }
    _cached_elevenlabs_ready = False
    _cached_selected_client = None
    return {
        "ok": False,
        "elevenlabs_ready": False,
        "selected_client": None,
        "error_type": result.get("error_type") or result.get("error") or "preflight_failed",
        "message": result.get("message") or "ElevenLabs preflight synthesis failed.",
        "attempts": result.get("attempts", []),
    }


async def voice_loop(transcript: str, source: str, started_at: str | None) -> dict[str, Any]:
    from .voice_intents import normalize_aura_transcript

    normalization = normalize_aura_transcript(transcript)
    normalized_transcript = str(normalization.get("normalized") or transcript)
    backend_received = _now_iso()
    overall_start = _perf_ms()
    llm_start = _now_iso()
    llm_start_ms = _perf_ms()
    response = ""
    brain_provider = "local_stub"

    if _openrouter_key():
        brain_provider = "openrouter"
        llm_result = await _openrouter_completion("Reply with one short diagnostic sentence confirming receipt of: " + normalized_transcript)
        response = llm_result.get("response_text") or ""
        llm_error = llm_result.get("error")
    else:
        response = "A.U.R.A diagnostic received the transcript."
        llm_error = None
    llm_done = _now_iso()
    llm_done_ms = _perf_ms()

    tts_start = _now_iso()
    tts_start_ms = _perf_ms()
    tts_result = await synthesize_tts_chain(response, include_audio=False, purpose="voice_loop")
    tts_provider = tts_result.get("provider") or "browser_speech"
    tts_done = _now_iso()
    tts_done_ms = _perf_ms()

    result = {
        "ok": not llm_error and bool(response),
        "transcript": normalized_transcript,
        "raw_transcript": transcript,
        "normalization": normalization,
        "source": source,
        "response": response,
        "timings": {
            "browser_started_at": started_at,
            "backend_received_at": backend_received,
            "llm_start_at": llm_start,
            "llm_done_at": llm_done,
            "tts_start_at": tts_start,
            "tts_done_at": tts_done,
            "total_backend_ms": round(_perf_ms() - overall_start),
            "llm_ms": round(llm_done_ms - llm_start_ms),
            "tts_ms": round(tts_done_ms - tts_start_ms),
        },
        "providers": {
            "brain": brain_provider,
            "tts": tts_provider,
        },
        "tts": {
            "ok": bool(tts_result.get("ok")),
            "audio_bytes": tts_result.get("audio_bytes", 0),
            "latency_ms": tts_result.get("latency_ms", 0),
            "error": tts_result.get("error"),
            "error_type": tts_result.get("error_type"),
            "attempts": tts_result.get("tts_provider_attempts", tts_result.get("attempts", [])),
        },
        "error": llm_error,
    }
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_safe("voice_loop", {
        "ok": result["ok"],
        "brain": brain_provider,
        "tts": tts_provider,
        "total_backend_ms": result["timings"]["total_backend_ms"],
    })
    return result


async def voice_bench_loop(body: dict[str, Any]) -> dict[str, Any]:
    from . import tts
    from .voice_intents import normalize_aura_transcript
    from .expo_demo_responses import cloud_failure_fallback, expo_demo_enabled, match_expo_demo_response, match_expo_department_projects
    from .cpr_awareness_responses import cpr_awareness_enabled, match_cpr_awareness_response
    from .tools import tool_router

    overall_start = _perf_ms()
    raw = str(body.get("raw_transcript") or body.get("transcript") or "").strip()
    normalized = str(body.get("normalized_transcript") or "").strip()
    if not normalized:
        normalized = str(normalize_aura_transcript(raw).get("normalized") or raw).strip()
    wake_detected = bool(body.get("wake_detected"))
    command_text = str(body.get("command_text") or "").strip()
    if not command_text and body.get("command_after_wake"):
        command_text = _strip_wake_terms(normalized)
    if not command_text and wake_detected:
        command_text = _strip_wake_terms(normalized)
    if not command_text:
        command_text = normalized
    normalized = _normalize_runtime_control_text(normalized)
    command_text = _normalize_runtime_control_text(command_text)
    normalized = tool_router.normalize_tool_command_text(normalized)
    command_text = tool_router.normalize_tool_command_text(command_text)
    alias = str(body.get("alias_matched") or "").strip()
    aura_awake = bool(body.get("aura_awake"))
    conversation_mode = str(body.get("conversation_mode") or "").strip()
    wake_required = bool(body.get("wake_required"))
    identity_context = body.get("identity_context") if isinstance(body.get("identity_context"), dict) else None
    vision_context = _safe_vision_context(body.get("vision_context"))
    identity_context, vision_context, used_recent_owner_context, last_owner_verified_age_ms = _apply_recent_owner_context(identity_context, vision_context)
    recent_turns = _safe_recent_turns(body.get("recent_turns"))
    conversation_id = str(body.get("conversation_id") or "").strip()
    if not conversation_id:
        conversation_id = str(uuid.uuid4())
    command_text, consumed_pending_tool_context = _consume_pending_tool_context(conversation_id, command_text)
    normalized = command_text if consumed_pending_tool_context else normalized
    session_address_override = str(body.get("session_address_override") or "").strip()
    effective_address = str(body.get("effective_address") or "").strip()
    address_reason = str(body.get("address_reason") or "").strip()
    auto_tts = bool(body.get("auto_tts", True))
    backend_receive_ms = 0
    failure_class: str | None = None
    failure_origin: str | None = None
    pending_tool_context: dict[str, Any] | None = None
    memory_manager = get_memory_manager()
    memory_command_intent = _memory_command_intent(command_text or normalized)
    memory_read_required = memory_command_intent not in {"memory.status_check", "memory.write_status"}
    expo_guest_greeting_requested = _expo_guest_greeting_intent(command_text or normalized)
    expo_intro_requested = (not expo_guest_greeting_requested) and _expo_intro_intent(command_text or normalized)
    expo_department_projects_answer = match_expo_department_projects(command_text or normalized)
    cpr_awareness_answer = None if expo_department_projects_answer else match_cpr_awareness_response(command_text or normalized)
    frontend_kind = str(body.get("frontend_kind") or "").strip()
    local_route_candidate = bool(
        _is_date_question(command_text)
        or _is_tamil_language_capability(_normalize_tamil_request_text(command_text))
        or _is_wake_only(normalized, wake_detected)
        or expo_guest_greeting_requested
        or expo_intro_requested
        or expo_department_projects_answer
        or cpr_awareness_answer
        or frontend_kind in {"wake", "runtime_control", "capability", "smalltalk", "tool_command", "expo_intro", "expo_guest_greeting"}
        or re.search(r"\b(hi|hello|hey|what about you|what can you do|who are you|are you online|how are you|can we talk|let's chat|lets chat|camera|microphone|tracking|start expo demo|tell me about aura|tell about aura|explain edge storm|tell about edge storm|explain game centre|tell about game centre|explain tech hub|tell about tech hub|what projects are shown|department projects|dept projects|engineers week|cpr|aed|cardiac arrest|chest compression)\b", command_text, re.IGNORECASE)
    )
    if memory_read_required and (memory_command_intent or not local_route_candidate):
        persistent_memory_context = memory_manager.load_context_for_turn(conversation_id, command_text or normalized)
        _log_safe("memory_read", {
            "backend": persistent_memory_context.backend,
            "read_ok": persistent_memory_context.read_ok,
            "facts": len(persistent_memory_context.facts),
        })
    else:
        from .memory.memory_types import MemoryContext

        persistent_memory_context = MemoryContext(enabled=False, backend="deferred", read_ok=True)

        async def _defer_memory_read() -> None:
            try:
                context = await asyncio.to_thread(memory_manager.load_context_for_turn, conversation_id, command_text or normalized)
                _log_safe("memory_read_background_complete", {
                    "backend": context.backend,
                    "read_ok": context.read_ok,
                    "facts": len(context.facts),
                })
            except Exception as exc:
                _log_safe("memory_read_failed", {"backend": "background", "safe_reason": type(exc).__name__})

        _track_background_memory_task(asyncio.create_task(_defer_memory_read()))
        _log_safe("memory_read_scheduled", {"backend": memory_manager.memory_status().get("backend")})
    memory_recall_answer = None if local_route_candidate or not memory_read_required else memory_manager.recall_from_memory(command_text or normalized, address=effective_address or "Sir")
    expo_intro_answer = EXPO_GUEST_GREETING_TEXT if expo_guest_greeting_requested else (EXPO_INTRO_TEXT if expo_intro_requested else None)
    expo_demo_answer: dict[str, Any] | None = None
    memory_command_answer = None
    if memory_command_intent == "memory.recall_query" and not memory_recall_answer:
        memory_command_answer = f"I checked persistent memory{_address_suffix(effective_address or 'Sir')}, but I could not find a saved fact about tomorrow."
    elif memory_command_intent and memory_command_intent != "memory.recall_query":
        memory_command_answer = _memory_command_response(memory_command_intent, memory_manager, persistent_memory_context, effective_address or "Sir")
    if memory_command_intent:
        memory_status = memory_manager.memory_status()
        _log_safe("memory_status_check", {
            "backend": memory_status.get("backend"),
            "supabase_configured": bool(memory_status.get("supabase_configured")),
            "read_enabled": bool(memory_status.get("read_enabled")),
            "write_enabled": bool(memory_status.get("write_enabled")),
        })
        if memory_manager.last_read_primary_ok:
            _log_safe("persistent_read_success", {
                "backend": memory_status.get("backend"),
                "facts": len(persistent_memory_context.facts),
            })
        else:
            _log_safe("persistent_read_failed", {
                "backend": memory_status.get("backend"),
                "safe_reason": memory_status.get("last_error_safe") or persistent_memory_context.last_error or "memory_read_failed",
            })
    memory_user_write: dict[str, Any] = {"ok": False, "primary_ok": False, "facts_saved": 0, "facts": []}

    llm_started = _perf_ms()
    address, address_reason = _effective_voice_address(
        identity_context=identity_context,
        vision_context=vision_context,
        effective_address=effective_address,
        session_address_override=session_address_override,
        address_reason=address_reason,
        conversation_mode=conversation_mode,
    )

    provider_chain_attempts: list[dict[str, Any]] = []
    wake_only = _is_wake_only(normalized, wake_detected)
    if wake_detected and command_text and not wake_only:
        wake_only = False
    provider_chain_attempts.append({"provider": "deterministic_wake", "used": wake_only})

    from .vision_commands import vision_match_reason
    openrouter_key = _openrouter_key()
    groq_key = _groq_key()
    openrouter_last_status = "configured" if openrouter_key else "missing_key"
    groq_last_status = "configured" if groq_key else "missing_key"
    provider_available_now = bool(openrouter_key or groq_key)

    if wake_only:
        brain = {
            "ok": True,
            "provider": "deterministic_wake",
            "model": "wake_handler",
            "response_text": f"Awake{_address_suffix(address)}.",
            "latency_ms": 0,
            "fallback_used": False,
            "attempts": [],
            "error_type": None,
            "fallback_reason": None,
        }
        intent_detected = "wake"
        vision_reason = "none"
        provider_route = "deterministic_wake"
    else:
        runtime_action = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer) else _runtime_control_response(command_text, address)
        capability_answer = None if runtime_action else _capability_response(command_text, address)
        capability_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer) else capability_answer
        language_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer) else _tamil_language_response(command_text, address)
        identity_owner_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer) else _identity_owner_question_response(command_text, identity_context, vision_context, address)
        profile_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_owner_answer) else _identity_profile_response(command_text, address)
        identity_answer = identity_owner_answer or profile_answer
        vision_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_answer) else _vision_question_response(command_text, vision_context, address)

        from .vision_commands import is_vision_followup_command
        is_followup = is_vision_followup_command(command_text, recent_turns) or str(body.get("frontend_kind") or "") == "vision_followup"
        vision_followup_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_answer or vision_answer) and not is_followup else (_vision_followup_response(command_text, vision_context, address) if is_followup else None)
        self_capability_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_answer or vision_answer or vision_followup_answer) else _self_capability_response(command_text, address, identity_context, vision_context)
        smalltalk_answer = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_answer or vision_answer or vision_followup_answer or self_capability_answer) else _smalltalk_response(command_text, address)
        tool_detection = None if (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or capability_answer or language_answer or identity_answer or vision_answer or vision_followup_answer or self_capability_answer or smalltalk_answer) else tool_router.detect_tool_command(command_text, {
            "request_id": body.get("request_id") or conversation_id,
            "root": str(ROOT),
            "identity_context": identity_context,
            "vision_context": vision_context,
        })
        if str(body.get("frontend_kind") or "") == "tool_command" and not tool_detection:
            tool_detection = tool_router.detect_tool_command(command_text or normalized, {
                "request_id": body.get("request_id") or conversation_id,
                "root": str(ROOT),
                "identity_context": identity_context,
                "vision_context": vision_context,
            })
        if not tool_detection and _is_failed_local_action_followup(command_text):
            tool_detection = tool_router.detect_tool_command(command_text or normalized, {
                "request_id": body.get("request_id") or conversation_id,
                "root": str(ROOT),
                "identity_context": identity_context,
                "vision_context": vision_context,
            })
        if not (memory_recall_answer or memory_command_answer or expo_intro_answer or expo_department_projects_answer or cpr_awareness_answer or runtime_action or language_answer or identity_answer or vision_answer or vision_followup_answer or tool_detection):
            expo_demo_answer = match_expo_demo_response(command_text or normalized, effective_address=address)
            if expo_demo_answer:
                capability_answer = None
                self_capability_answer = None
                smalltalk_answer = None
        city_followup = _city_like_short_phrase(command_text)
        general_question = (is_general_question(command_text)
                            and not _is_command_like_local_phrase(command_text)
                            and _normalize_runtime_control_text(command_text) not in KNOWN_CITY_FOLLOWUPS)
        city_clarification_answer = None if (general_question or tool_detection or smalltalk_answer or capability_answer or language_answer) else (
            f"What would you like to know about {city_followup}, {address}?" if city_followup else None
        )
        junk_response = _junk_stt_response(command_text or raw)

        if junk_response or str(body.get("frontend_kind") or "") == "junk_stt":
            brain = {
                "ok": True,
                "provider": "deterministic_junk_stt",
                "model": "junk_stt_handler",
                "response_text": junk_response or "I didn’t catch that clearly, Sir.",
                "latency_ms": 0,
                "fallback_used": False,
                "attempts": [],
                "error_type": None,
                "fallback_reason": None,
            }
            intent_detected = "deterministic_junk_stt"
            vision_reason = "none"
            provider_route = "deterministic_junk_stt"
            provider_chain_attempts.append({"provider": "deterministic_junk_stt", "used": True})
        else:
            provider_chain_attempts.append({"provider": "deterministic_runtime_control", "used": bool(runtime_action)})
            provider_chain_attempts.append({"provider": "deterministic_memory_recall", "used": bool(memory_recall_answer)})
            provider_chain_attempts.append({"provider": "deterministic_capability", "used": bool(capability_answer)})
            provider_chain_attempts.append({"provider": "deterministic_language_response", "used": bool(language_answer)})
            provider_chain_attempts.append({"provider": "deterministic_identity", "used": bool(identity_answer)})
            provider_chain_attempts.append({"provider": "deterministic_vision", "used": bool(vision_answer)})
            provider_chain_attempts.append({"provider": "deterministic_vision_followup", "used": bool(vision_followup_answer)})
            provider_chain_attempts.append({"provider": "deterministic_self_capability", "used": bool(self_capability_answer)})
            provider_chain_attempts.append({"provider": "deterministic_smalltalk", "used": bool(smalltalk_answer)})
            provider_chain_attempts.append({"provider": "deterministic_tool_command", "used": bool(tool_detection)})
            provider_chain_attempts.append({"provider": "deterministic_expo_department_projects", "used": bool(expo_department_projects_answer)})
            provider_chain_attempts.append({"provider": "deterministic_cpr_awareness", "used": bool(cpr_awareness_answer)})
            provider_chain_attempts.append({"provider": "deterministic_expo_demo", "used": bool(expo_demo_answer)})
            provider_chain_attempts.append({"provider": "deterministic_city_clarification", "used": bool(city_clarification_answer)})

            tool_result = None
            if tool_detection:
                tool_result = tool_router.execute_aura_tool(
                    str(tool_detection.get("tool_name") or ""),
                    str(tool_detection.get("action") or ""),
                    tool_detection.get("params") if isinstance(tool_detection.get("params"), dict) else {},
                    {
                        "request_id": body.get("request_id") or conversation_id,
                        "root": str(ROOT),
                        "identity_context": identity_context,
                        "vision_context": vision_context,
                    },
                )
                tool_failure_class = _tool_failure_class(tool_result, tool_detection)
                if tool_failure_class:
                    failure_class = tool_failure_class
                    failure_origin = "tool_executor" if tool_failure_class == "local_tool_failed" else "tool_router"
                if tool_result.get("tool") == "weather" and tool_result.get("error_type") in {"weather_location_too_broad", "weather_location_not_found"}:
                    pending_tool_context = _store_pending_tool_context(conversation_id, tool="weather", reason="location_clarification", original_query=command_text)

            deterministic = ((memory_recall_answer or {}).get("response_text") if memory_recall_answer else (memory_command_answer or expo_intro_answer or ((expo_department_projects_answer or {}).get("response_text") if expo_department_projects_answer else None) or ((cpr_awareness_answer or {}).get("response_text") if cpr_awareness_answer else None) or (runtime_action.get("response_text") if runtime_action else (capability_answer or ((language_answer or {}).get("response_text") if language_answer else None) or identity_answer or vision_answer or vision_followup_answer or ((expo_demo_answer or {}).get("response_text") if expo_demo_answer else None) or self_capability_answer or smalltalk_answer or city_clarification_answer or ((tool_result or {}).get("response_text") if tool_result else None)))))
            failure_text = _failure_response_text(failure_class, address=address, tool_result=tool_result, tool_name=str((tool_detection or {}).get("tool_name") or ""))
            if failure_text:
                deterministic = failure_text
            if deterministic:
                provider_name = (
                    (memory_recall_answer.get("provider") if memory_recall_answer else ("deterministic_memory" if memory_command_answer else (("deterministic_expo_guest_greeting" if expo_guest_greeting_requested else "deterministic_expo_intro") if expo_intro_answer else (expo_department_projects_answer.get("provider_route") if expo_department_projects_answer else (cpr_awareness_answer.get("provider_route") if cpr_awareness_answer else None))))) or ("deterministic_runtime_control" if runtime_action
                    else ("deterministic_capability" if capability_answer
                    else ("deterministic_language_response" if language_answer
                    else ("deterministic_identity" if identity_answer
                    else ("deterministic_vision" if vision_answer
                    else ("deterministic_vision_followup" if vision_followup_answer
                    else ("deterministic_expo_demo" if expo_demo_answer
                    else ("deterministic_self_capability" if self_capability_answer
                    else ("deterministic_city_clarification" if city_clarification_answer
                    else ("deterministic_tool_command" if tool_result else "deterministic_smalltalk")))))))))))
                if _is_date_question(command_text) and smalltalk_answer:
                    provider_name = "deterministic_datetime"
                elif capability_answer and _is_tamil_language_capability(_normalize_tamil_request_text(command_text)):
                    provider_name = "deterministic_language_capability"
                model_name = (
                    "memory_recall_handler" if memory_recall_answer
                    else ("runtime_control_handler" if runtime_action
                    else ("capability_handler" if capability_answer
                    else ("language_response_handler" if language_answer
                    else ("identity_owner_handler" if identity_owner_answer
                    else ("identity_profile_handler" if profile_answer
                    else ("vision_context_handler" if vision_answer
                    else ("vision_followup_handler" if vision_followup_answer
                    else ("expo_department_projects_handler" if expo_department_projects_answer
                    else ("cpr_awareness_handler" if cpr_awareness_answer
                    else ("expo_demo_handler" if expo_demo_answer
                    else ("self_capability_handler" if self_capability_answer
                    else ("city_clarification_handler" if city_clarification_answer
                    else ("tool_router" if tool_result else "smalltalk_handler"))))))))))))))
                if memory_command_answer:
                    model_name = "memory_command_handler"
                elif expo_intro_answer:
                    model_name = "expo_guest_greeting_handler" if expo_guest_greeting_requested else "expo_intro_handler"
                action_payload = []
                if runtime_action:
                    action_payload = runtime_action.get("actions") or [{
                        "type": runtime_action.get("action_type"),
                        "label": runtime_action.get("action_label"),
                        **({"value": runtime_action["value"]} if "value" in runtime_action else {}),
                        **({"requires_camera": runtime_action["requires_camera"]} if "requires_camera" in runtime_action else {}),
                    }]
                    request_id = str(body.get("request_id") or conversation_id or "current")
                    for action in action_payload:
                        action_type = str(action.get("type") or runtime_action.get("action_type") or "unknown")
                        action_target = str(action.get("target") or action.get("label") or "default")
                        action["request_id"] = request_id
                        action["action_execution_id"] = f"{request_id}:{action_type}:{action_target}"
                if tool_result:
                    frontend_action = (tool_result.get("result") or {}).get("frontend_action")
                    if isinstance(frontend_action, dict) and frontend_action.get("type"):
                        request_id = str(body.get("request_id") or conversation_id or "current")
                        action_type = str(frontend_action.get("type"))
                        action_payload = [{
                            "type": action_type,
                            "label": str(frontend_action.get("label") or action_type),
                            **({"value": frontend_action.get("value")} if "value" in frontend_action else {}),
                            **({"url": frontend_action.get("url")} if "url" in frontend_action else {}),
                            **({"mode": frontend_action.get("mode")} if "mode" in frontend_action else {}),
                            "request_id": request_id,
                            "action_execution_id": f"{request_id}:{action_type}:tool",
                        }]
                brain = {
                    "ok": True,
                    "provider": provider_name,
                    "model": model_name,
                    "response_text": deterministic,
                    "actions": action_payload,
                    "latency_ms": 0,
                    "fallback_used": False,
                    "attempts": [],
                    "error_type": None,
                    "fallback_reason": None,
                    "tool_result": tool_result,
                    "tool_detection": tool_detection,
                    "language_response": language_answer,
                    "memory_recall": memory_recall_answer,
                    "expo_department_projects": expo_department_projects_answer,
                    "cpr_awareness": cpr_awareness_answer,
                    "expo_demo": expo_demo_answer,
                }
                intent_detected = (
                    memory_recall_answer.get("intent") if memory_recall_answer
                    else (runtime_action.get("intent") if runtime_action
                    else ("capability.language.tamil" if capability_answer and _is_tamil_language_capability(command_text)
                    else ("capability_check" if capability_answer
                    else (str(language_answer.get("intent") or "language_response.tamil") if language_answer
                    else ("identity_owner_question" if identity_owner_answer
                    else ("identity_profile_question" if profile_answer
                    else (vision_match_reason(command_text) if vision_answer
                    else (f"tool_command.{tool_result.get('tool')}.{tool_result.get('action')}" if tool_result else (expo_department_projects_answer.get("intent") if expo_department_projects_answer else (cpr_awareness_answer.get("intent") if cpr_awareness_answer else (expo_demo_answer.get("intent") if expo_demo_answer else ("city_clarification" if city_clarification_answer else ("smalltalk" if smalltalk_answer else provider_name)))))))))))))
                )
                if memory_command_answer:
                    intent_detected = memory_command_intent
                elif expo_intro_answer:
                    intent_detected = "expo_guest_greeting.start" if expo_guest_greeting_requested else "expo_intro.start"
                elif provider_name == "deterministic_smalltalk" and _general_conversation_intent(command_text):
                    intent_detected = "smalltalk.general_conversation_start"
                vision_reason = vision_match_reason(command_text) if (vision_answer or identity_owner_answer) else "none"
                provider_route = provider_name
                if provider_name == "deterministic_self_capability":
                    _log_safe("self_capability_fast_path", {
                        "intent": "self_capability",
                        "provider_route": provider_name,
                        "backend_ms": round(_perf_ms() - overall_start),
                    })
                if provider_name == "deterministic_expo_department_projects":
                    _log_safe("expo_department_projects_intent", {
                        "department_projects_intent_detected": True,
                        "provider_route": provider_name,
                        "backend_intent_detected": "expo_department_projects.explain",
                        "canonical_content_used": True,
                        "cloud_bypassed": True,
                        "reason": "expo_demo_canonical_content",
                    })
            else:
                if _is_unclear_input(command_text):
                    failure_class = "unclear_input"
                    failure_origin = "backend_router"
                    brain = {
                        "ok": True,
                        "provider": "deterministic_unclear_input",
                        "model": "failure_classifier",
                        "response_text": _failure_response_text(failure_class, address=address),
                        "latency_ms": 0,
                        "fallback_used": False,
                        "attempts": [],
                        "error_type": None,
                        "fallback_reason": None,
                    }
                    intent_detected = "unclear_input"
                    vision_reason = "none"
                    provider_route = "deterministic_unclear_input"
                    provider_chain_attempts.append({"provider": "deterministic_unclear_input", "used": True})
                elif _is_failed_local_action_followup(command_text):
                    failure_class = "deterministic_followup_failure"
                    failure_origin = "backend_router"
                    brain = {
                        "ok": True,
                        "provider": "deterministic_tool_followup",
                        "model": "failure_classifier",
                        "response_text": _failure_response_text(failure_class, address=address),
                        "latency_ms": 0,
                        "fallback_used": False,
                        "attempts": [],
                        "error_type": None,
                        "fallback_reason": None,
                    }
                    intent_detected = "deterministic_followup_failure"
                    vision_reason = "none"
                    provider_route = "deterministic_tool_followup"
                    provider_chain_attempts.append({"provider": "deterministic_tool_followup", "used": True})
                elif not general_question and (_is_command_like_local_phrase(command_text) or str(body.get("frontend_kind") or "") == "tool_command"):
                    failure_class = "local_command_unrecognized"
                    failure_origin = "backend_router"
                    brain = {
                        "ok": True,
                        "provider": "deterministic_tool_command",
                        "model": "failure_classifier",
                        "response_text": _failure_response_text(failure_class, address=address),
                        "latency_ms": 0,
                        "fallback_used": False,
                        "attempts": [],
                        "error_type": None,
                        "fallback_reason": None,
                    }
                    intent_detected = "local_command_unrecognized"
                    vision_reason = "none"
                    provider_route = "deterministic_tool_command"
                    provider_chain_attempts.append({"provider": "deterministic_tool_command", "used": False, "failure_class": failure_class})
                else:
                    caps = capabilities.get_capability_state()
                    cloud_recent_turns = _bounded_cloud_recent_turns(recent_turns)
                    history_text = "\n".join(
                        f"User: {turn['user']}\nAssistant: {turn['assistant']}"
                        for turn in cloud_recent_turns
                    )
                    cloud_context_limit = _cloud_max_context_chars()
                    cloud_history_text = _clamp_cloud_text(history_text or "None", cloud_context_limit)
                    cloud_memory_context = (persistent_memory_context.context_text or "None")[:max(0, cloud_context_limit - len(cloud_history_text))]
                    short_answer_mode = _cloud_short_answer_mode(command_text)
                    cloud_timeout_seconds = min(_cloud_timeout_seconds(), GENERAL_PROVIDER_TIMEOUT_SECONDS)
                    print("[AURA REASONING ROUTER] query_type=general_question primary=groq fallback=openrouter", flush=True)
                    cloud_max_tokens = min(_cloud_max_tokens(), 90) if short_answer_mode else _cloud_max_tokens()
                    cloud_temperature = _cloud_temperature()
                    prompt = (
                        "You are Aura, an interactive live AI assistant. "
                        "Use recent conversation history to resolve pronouns like that, it, he, she, they, why, and what about it. "
                        "Use vision_context when answering camera, seeing, identity, or tracking questions. "
                        "Be concise and suitable for voice. "
                        f"{'Answer in one or two short direct sentences. ' if short_answer_mode else ''}"
                        "Use effective_address exactly when provided. If effective_address is Sir, address the user as Sir. "
                        "If effective_address is Mam, address the user as Mam. "
                        "If effective_address is Guest or empty, use neutral wording unless talking about a guest. "
                        "Do not call the owner Guest when session_address_override or owner verification says Sir. "
                        "Do not claim access to email, cloud storage, calendar, files, apps, or external accounts unless the capability registry below says that integration is configured. "
                        "If email/cloud/calendar are not configured, say you can compose text here or guide the user manually, but cannot create/send/upload/read inside those accounts. "
                        "Do not mention internal memory, recent turns, provider chain, or provider names unless asked.\n"
                        f"effective_address={address}; session_address_override={session_address_override or 'none'}; address_reason={address_reason or 'unknown'}\n"
                        "Capabilities: "
                        f"email_configured={bool(caps.get('email', {}).get('configured'))}; "
                        f"cloud_storage_configured={bool(caps.get('cloud_storage', {}).get('configured'))}; "
                        f"calendar_configured={bool(caps.get('calendar', {}).get('configured'))}; "
                        f"desktop_runtime_control={bool(caps.get('desktop_runtime', {}).get('orb_ui_control'))}; "
                        f"camera_control={bool(caps.get('desktop_runtime', {}).get('camera_control'))}; "
                        f"mic_control={bool(caps.get('desktop_runtime', {}).get('mic_control'))}; "
                        f"tracking_control={bool(caps.get('desktop_runtime', {}).get('tracking_control'))}\n"
                        f"Current identity context: {_compact_identity_summary(identity_context)}\n"
                        f"Current vision context: {_compact_vision_summary(vision_context)}\n"
                        f"Persistent memory context:\n{cloud_memory_context}\n"
                        f"Recent conversation turns:\n{cloud_history_text}\n"
                        f"Current user: {command_text}"
                    )
                    prompt = _clamp_cloud_text(prompt, cloud_context_limit + 1800)
                    backend_cloud_timeout_ms = cloud_timeout_seconds * 2000
                    try:
                        brain = await asyncio.wait_for(
                            _provider_chain_completion(
                                prompt,
                                command_text,
                                cloud_recent_turns,
                                vision_context,
                                address,
                                timeout_seconds=cloud_timeout_seconds,
                                max_tokens=cloud_max_tokens,
                                temperature=cloud_temperature,
                            ),
                            timeout=cloud_timeout_seconds * 2 + 0.5,
                        )
                    except asyncio.TimeoutError:
                        failure_class = "cloud_reasoning_timeout"
                        failure_origin = "cloud_provider"
                        timeout_provider = "groq" if groq_key else ("openrouter" if openrouter_key else "cloud_chain")
                        _log_cloud_latency(
                            provider=timeout_provider,
                            prompt_chars=len(prompt),
                            recent_turns=len(cloud_recent_turns),
                            timeout_seconds=cloud_timeout_seconds,
                            response_ms=backend_cloud_timeout_ms,
                            fallback_used=True,
                            reason="cloud_timeout",
                        )
                        print("[AURA CLOUD LATENCY] fallback_used=true reason=cloud_timeout", flush=True)
                        brain = {
                            "ok": True,
                            "provider": "local_stub",
                            "model": "cloud_timeout_guard",
                            "response_text": build_general_helpful_fallback(command_text),
                            "latency_ms": backend_cloud_timeout_ms,
                            "fallback_used": True,
                            "attempts": [],
                            "fallback_reason": "cloud_reasoning_timeout",
                            "error_type": "cloud_reasoning_timeout",
                            "error": "cloud_reasoning_timeout",
                            "timeout_guard_used": True,
                            "backend_timeout_ms": backend_cloud_timeout_ms,
                        }
                    brain["cloud_fast_mode"] = _cloud_fast_mode_enabled()
                    brain["cloud_timeout_seconds"] = cloud_timeout_seconds
                    brain["cloud_recent_turns"] = len(cloud_recent_turns)
                    brain["cloud_max_tokens"] = cloud_max_tokens
                    brain["cloud_temperature"] = cloud_temperature
                    brain["cloud_prompt_chars"] = len(prompt)
                    brain["cloud_short_answer_mode"] = short_answer_mode
                    provider_chain_attempts.extend(brain.get("attempts", []))
                    provider_chain_attempts.append({"provider": "local_stub", "used": brain.get("provider") == "local_stub"})
                    if brain.get("provider") == "local_stub" and brain.get("error_type") == "cloud_providers_unavailable":
                        failure_class = "cloud_reasoning_failed"
                        failure_origin = "cloud_provider"
                        brain["response_text"] = build_general_helpful_fallback(command_text)
                    if brain.get("error_type") == "cloud_reasoning_timeout":
                        failure_class = "cloud_reasoning_timeout"
                        failure_origin = "cloud_provider"
                    intent_detected = brain.get("provider")
                    vision_reason = "none"
                    provider_route = brain.get("provider")

    llm_ms = round(_perf_ms() - llm_started)
    backend_ms = llm_ms
    actions = brain.get("actions", [])
    response_text = str(brain.get("response_text") or "Aura diagnostic received the transcript.").strip()
    response_text = _apply_resolved_address_text(response_text, address)
    brain["response_text"] = response_text
    action_claim_words = ("opening", "open ", "launching", "launch ", "running", "run ", "starting", "start ")
    if provider_route in {"openrouter", "groq", "local_stub"} and not actions and any(word in response_text.lower() for word in action_claim_words):
        response_text = "I understood the command, Sir, but I did not execute it because it was not routed through the local tool layer."
        brain["response_text"] = response_text
        brain["fallback_reason"] = brain.get("fallback_reason") or "action_claim_blocked_no_local_tool"
        failure_class = failure_class or "local_command_unrecognized"
        failure_origin = failure_origin or "backend_router"
    spoken_text = tts.sanitize_text_for_tts(response_text).strip() or response_text

    fast_path = _deterministic_fast_path(str(provider_route or ""), brain)
    tool_result_for_tts = brain.get("tool_result") or {}
    news_tts_segmented = (
        str(provider_route or "") == "deterministic_tool_command"
        and str(tool_result_for_tts.get("tool") or "").lower() == "news"
        and bool(tool_result_for_tts.get("ok"))
    )
    expo_browser_fallback_allowed = str(provider_route or "") in {"deterministic_expo_intro", "deterministic_expo_guest_greeting", "deterministic_expo_demo"} and _expo_browser_fallback_allowed()
    general_tts_segmented = str(provider_route or '') in {'groq', 'openrouter', 'local_stub'}
    general_tts_chunks = tts.general_talk_chunks(response_text) if general_tts_segmented else []
    deterministic_tts_deferred = general_tts_segmented or news_tts_segmented or (fast_path and (expo_intro_requested or expo_guest_greeting_requested or _deterministic_tts_deferred()))
    route_ms = round(_perf_ms() - llm_started)
    tts_started = _perf_ms()
    tts_result: dict[str, Any] = {"ok": False, "provider": "browser_speech", "audio_bytes": 0, "mime_type": None, "latency_ms": 0, "error_type": None, "tts_debug": {}}
    tts_attempts_debug: list[dict[str, Any]] = []
    if deterministic_tts_deferred and auto_tts:
        if str(provider_route or "") in {"deterministic_expo_intro", "deterministic_expo_guest_greeting", "deterministic_expo_demo"} and _cartesia_strict_mode() and _expo_browser_fallback_allowed():
            _log_safe("expo_browser_fallback_allowed_by_env", {"enabled": True})
        tts_result = {
            "ok": False,
            "provider": None,
            "audio_bytes": 0,
            "mime_type": None,
            "latency_ms": 0,
            "error_type": "tts_deferred",
            "strict_mode_blocked_browser_fallback": False,
            "tts_debug": {
                "fallback_reason": None,
                "strict_primary": _cartesia_strict_mode(),
                "required_provider": _tts_required_provider() or None,
                "browser_fallback_allowed": _expo_browser_fallback_allowed() if str(provider_route or "") in {"deterministic_expo_intro", "deterministic_expo_guest_greeting", "deterministic_expo_demo"} else (_tts_browser_fallback_allowed() and not _cartesia_strict_mode()),
                "strict_mode_blocked_browser_fallback": False,
                "post_response": True,
            },
        }
    elif auto_tts:
        tts_result = await synthesize_tts_chain(spoken_text, include_audio=True, purpose="voice_turn", mode="voice_turn")
        tts_attempts_debug = tts_result.get("tts_provider_attempts", tts_result.get("attempts", []))
    else:
        tts_result["error_type"] = "auto_tts_disabled"
    tts_ms = round(_perf_ms() - tts_started)

    tts_provider = str(tts_result.get("provider") or "")
    audio_provider = str(tts_result.get("audio_provider") or tts_provider)
    tts_error_type = None if deterministic_tts_deferred else (None if tts_result.get("ok") else (tts_result.get("error_type") or tts_result.get("error")))
    strict_tts_blocked = auto_tts and audio_provider == "cartesia" and bool(tts_result.get("strict_mode_blocked_browser_fallback")) and not expo_browser_fallback_allowed
    tts_block_reason = tts_result.get("tts_debug", {}).get("fallback_reason") or tts_error_type
    result = {
        "ok": True,
        "response_text": response_text,
        "spoken_text": spoken_text,
        "actions": actions,
        "providers": {
            "brain": brain.get("provider"),
            "brain_model": brain.get("model"),
            "fallback_used": bool(brain.get("fallback_used")),
            "fallback_reason": brain.get("fallback_reason"),
            "tts": tts_provider,
        },
        "conversation": {
            "aura_awake": aura_awake,
            "conversation_mode": conversation_mode or "awake_latched",
            "wake_required": wake_required,
            "command_text": command_text,
            "identity_context": identity_context,
            "vision_context": vision_context,
            "effective_address": address,
            "session_address_override": session_address_override,
            "address_reason": address_reason,
        },
        "address": {
            "address_used": address,
            "address_reason": address_reason,
            "effective_address": address,
            "session_address_override": session_address_override,
        },
        "memory": {
            "conversation_id": conversation_id,
            "memory_used": bool(recent_turns),
            "history_turns": len(recent_turns),
        },
        "timings": {
            "backend_receive_ms": backend_receive_ms,
            "backend_ms": backend_ms,
            "llm_ms": llm_ms,
            "tts_ms": tts_ms,
            "total_backend_ms": round(_perf_ms() - overall_start),
        },
        "audio": {
            "provider": audio_provider,
            "mime_type": tts_result.get("mime_type"),
            "audio_bytes": tts_result.get("audio_bytes", 0),
            "audio_base64": tts_result.get("audio_base64"),
            "fallback_reason": tts_result.get("tts_debug", {}).get("fallback_reason") or tts_error_type,
            "strict_mode_blocked_browser_fallback": bool(tts_result.get("strict_mode_blocked_browser_fallback")),
        },
        "audio_status": {
            "mode": "deferred" if deterministic_tts_deferred else "inline",
            "playback": "deferred" if deterministic_tts_deferred else ("blocked" if strict_tts_blocked else ("available" if tts_result.get("ok") else "failed")),
            "deferred": deterministic_tts_deferred,
            "blocked": bool(strict_tts_blocked),
            "provider": None if deterministic_tts_deferred else (audio_provider or None),
            "fallback_reason": None if deterministic_tts_deferred else (tts_result.get("tts_debug", {}).get("fallback_reason") or tts_error_type),
            "fallback_attempted": bool((tts_result.get("tts_debug") or {}).get("fallback_attempted", False)),
        },
        "audioBase64": tts_result.get("audio_base64"),
        "tts": {
            "provider": tts_provider,
            "mime_type": tts_result.get("mime_type"),
            "audio_bytes": tts_result.get("audio_bytes", 0),
            "audio_base64": tts_result.get("audio_base64"),
            "fallback_reason": tts_result.get("tts_debug", {}).get("fallback_reason") or tts_error_type,
            "strict_mode_blocked_browser_fallback": bool(tts_result.get("strict_mode_blocked_browser_fallback")),
        },
        "result": {
            "audio": {
                "provider": audio_provider,
                "mime_type": tts_result.get("mime_type"),
                "audio_bytes": tts_result.get("audio_bytes", 0),
                "audio_base64": tts_result.get("audio_base64"),
                "fallback_reason": tts_result.get("tts_debug", {}).get("fallback_reason") or tts_error_type,
                "strict_mode_blocked_browser_fallback": bool(tts_result.get("strict_mode_blocked_browser_fallback")),
            }
        },
        "brain_attempts": brain.get("attempts", []),
        "provider_chain_attempts": provider_chain_attempts,
        "debug": {
            "request_id": body.get("request_id"),
            "abort_reason": body.get("abort_reason"),
            "is_duplicate_transcript": bool(body.get("is_duplicate_transcript")),
            "raw_transcript": raw,
            "normalized_for_routing": normalized,
            "frontend_kind": body.get("frontend_kind"),
            "command_text": command_text,
            "intent_detected": intent_detected,
            "intent": intent_detected,
            "backend_intent_detected": intent_detected,
            "failure_class": failure_class,
            "failure_origin": failure_origin,
            "fast_path": fast_path,
            "fast_path_reason": "deterministic_route" if fast_path else None,
            "route_ms": route_ms,
            "memory_write_mode": "background",
            "tts_mode": "post_response_or_async" if deterministic_tts_deferred else "inline",
            "fast_text_response": bool(fast_path),
            "deterministic_tts_deferred": deterministic_tts_deferred,
            "general_tts": {"segmented": general_tts_segmented, "chunks": general_tts_chunks},
            "tts_pipeline": {"prefetch": _cloud_int_env("AURA_CARTESIA_PREFETCH_CHUNKS", 2, minimum=1, maximum=2), "timeout_seconds": tts.cartesia_tts_chunk_timeout_seconds()},
            "expo_browser_fallback_allowed": bool(expo_browser_fallback_allowed),
            "memory_write_scheduled": True,
            "latency": {
                "route_ms": route_ms,
                "memory_schedule_ms": 0,
                "memory_write_ms_if_awaited": None,
                "tts_prepare_ms": 0 if fast_path else tts_ms,
                "tts_provider_ms": 0 if fast_path else tts_ms,
                "response_build_ms": 0,
                "total_backend_request_ms": round(_perf_ms() - overall_start),
            },
            "requires_frontend_execution": bool(actions),
            "tool_detected": bool((brain.get("tool_result") or {}).get("tool") or (brain.get("tool_detection") or {}).get("tool_name")),
            "tool_name": (brain.get("tool_result") or {}).get("tool") or (brain.get("tool_detection") or {}).get("tool_name"),
            "tool_action": (brain.get("tool_result") or {}).get("action"),
            "tool_risk_level": (brain.get("tool_result") or {}).get("risk_level"),
            "tool_requires_confirmation": bool((brain.get("tool_result") or {}).get("requires_confirmation")),
            "tool_executed": bool((brain.get("tool_result") or {}).get("executed")),
            "tool_error_type": (brain.get("tool_result") or {}).get("error_type"),
            "tool_error_message": (brain.get("tool_result") or {}).get("error_message"),
            "weather": ((brain.get("tool_result") or {}).get("metadata") or {}).get("weather"),
            "news": ((brain.get("tool_result") or {}).get("metadata") or {}).get("news"),
            "browser": ((brain.get("tool_result") or {}).get("metadata") or {}).get("browser"),
            "pending_tool_context": pending_tool_context,
            "consumed_pending_tool_context": consumed_pending_tool_context,
            "vision_match_reason": vision_reason,
            "provider_route": provider_route,
            "timeout_guard_used": bool(brain.get("timeout_guard_used")),
            "backend_timeout_ms": brain.get("backend_timeout_ms") if brain.get("timeout_guard_used") else None,
            "frontend_timeout_ms": 18000 if brain.get("timeout_guard_used") else None,
            "expo": {
                "shortcut": "P" if expo_guest_greeting_requested else ("I" if expo_intro_requested else None),
                "intent": "expo_guest_greeting.start" if expo_guest_greeting_requested else ("expo_intro.start" if expo_intro_requested else None),
                "deterministic": bool((expo_intro_requested and provider_route == "deterministic_expo_intro") or (expo_guest_greeting_requested and provider_route == "deterministic_expo_guest_greeting")),
                "tts_segmented": False,
                "timeout_ms": 60000 if (expo_intro_requested or expo_guest_greeting_requested) else None,
                "started": bool((expo_intro_requested or expo_guest_greeting_requested) and failure_class is None and provider_route in {"deterministic_expo_intro", "deterministic_expo_guest_greeting"} and not strict_tts_blocked),
                "speech_played": bool((expo_intro_requested or expo_guest_greeting_requested) and tts_result.get("ok") and not strict_tts_blocked),
                "tts_blocked": bool((expo_intro_requested or expo_guest_greeting_requested) and strict_tts_blocked),
                "browser_fallback_allowed": bool(expo_browser_fallback_allowed),
                "block_reason": tts_block_reason if (expo_intro_requested or expo_guest_greeting_requested) and strict_tts_blocked else None,
                "failure_class": failure_class if (expo_intro_requested or expo_guest_greeting_requested) else None,
            },
            "expo_demo": {
                "enabled": expo_demo_enabled(),
                "matched": bool(expo_demo_answer),
                "intent": (expo_demo_answer or {}).get("intent"),
                "provider_route": "deterministic_expo_demo" if expo_demo_answer else None,
                "cloud_route_used": bool(provider_route in {"openrouter", "groq"}),
                "browser_fallback_allowed": bool(expo_browser_fallback_allowed),
            },
            "expo_department_projects": {
                "matched": bool(expo_department_projects_answer),
                "intent": (expo_department_projects_answer or {}).get("intent"),
                "provider_route": (expo_department_projects_answer or {}).get("provider_route"),
                "canonical_content_used": bool((expo_department_projects_answer or {}).get("debug", {}).get("canonical_content_used")),
                "cloud_bypassed": bool((expo_department_projects_answer or {}).get("debug", {}).get("cloud_bypassed")),
                "cloud_bypass_reason": (expo_department_projects_answer or {}).get("debug", {}).get("cloud_bypass_reason"),
            },
            "cpr_awareness": {
                "enabled": cpr_awareness_enabled(),
                "matched": bool(cpr_awareness_answer),
                "intent": (cpr_awareness_answer or {}).get("intent"),
                "provider_route": (cpr_awareness_answer or {}).get("provider_route"),
                "question_id": ((cpr_awareness_answer or {}).get("debug") or {}).get("question_id"),
                "mode": ((cpr_awareness_answer or {}).get("debug") or {}).get("mode"),
                "canonical_content_used": bool(((cpr_awareness_answer or {}).get("debug") or {}).get("canonical_content_used")),
                "cloud_bypassed": bool(((cpr_awareness_answer or {}).get("debug") or {}).get("cloud_bypassed")),
                "cloud_bypass_reason": ((cpr_awareness_answer or {}).get("debug") or {}).get("cloud_bypass_reason"),
            },
            "requested_language": ((brain.get("language_response") or {}).get("requested_language") or ("tamil" if intent_detected == "capability.language.tamil" else None)),
            "language_input_mode": ((brain.get("language_response") or {}).get("language_input_mode") or ("tanglish" if intent_detected == "capability.language.tamil" else None)),
            "local_answer_used": bool((brain.get("language_response") or {}).get("local_answer_used") or intent_detected == "capability.language.tamil"),
            "cloud_route_used": bool(provider_route in {"openrouter", "groq"}),
            "cloud": {
                "fast_mode": bool(brain.get("cloud_fast_mode", _cloud_fast_mode_enabled())),
                "timeout_seconds": int(brain.get("cloud_timeout_seconds") or _cloud_timeout_seconds()),
                "recent_turns": int(brain.get("cloud_recent_turns") or 0),
                "max_recent_turns": _cloud_max_recent_turns(),
                "max_tokens": int(brain.get("cloud_max_tokens") or _cloud_max_tokens()),
                "temperature": float(brain.get("cloud_temperature") or _cloud_temperature()),
                "prompt_chars": int(brain.get("cloud_prompt_chars") or 0),
                "max_context_chars": _cloud_max_context_chars(),
                "short_answer_mode": bool(brain.get("cloud_short_answer_mode")),
                "fallback_used": bool(brain.get("fallback_used")) if provider_route in {"openrouter", "groq", "local_stub"} else False,
                "fallback_reason": brain.get("fallback_reason") if provider_route in {"openrouter", "groq", "local_stub"} else None,
            },
            "tamil_tts_limited": bool((brain.get("language_response") or {}).get("tamil_tts_limited") or intent_detected == "capability.language.tamil"),
            "sensitive_vision_attribute": "gender" if intent_detected == "vision_sensitive_attribute.gender_inference" else None,
            "owner_verified": bool((identity_context or {}).get("owner_verified") or (vision_context or {}).get("owner_verified")),
            "matched_profile_id": (identity_context or {}).get("matched_profile_id") or (vision_context or {}).get("matched_profile_id"),
            "matched_profile_name": (identity_context or {}).get("matched_profile_name") or (vision_context or {}).get("matched_profile_name"),
            "preferred_address": (identity_context or {}).get("preferred_address") or (vision_context or {}).get("preferred_address"),
            "identity_confidence": (identity_context or {}).get("identity_confidence") or (vision_context or {}).get("identity_confidence"),
            "camera_state": (vision_context or {}).get("camera_state") or (vision_context or {}).get("camera_status"),
            "gesture_tracking_state": (vision_context or {}).get("gesture_tracking_state") or "off",
            "gesture_tracking_error": (vision_context or {}).get("gesture_tracking_error"),
            "people_tracking_state": (vision_context or {}).get("people_tracking_state") or (vision_context or {}).get("tracking_state") or (vision_context or {}).get("tracking_status"),
            "people_tracking_error": (vision_context or {}).get("people_tracking_error") or (vision_context or {}).get("tracking_error"),
            "tracking_state": (vision_context or {}).get("people_tracking_state") or (vision_context or {}).get("tracking_state") or (vision_context or {}).get("tracking_status"),
            "identity_state": (identity_context or {}).get("identity_state") or (vision_context or {}).get("identity_state"),
            "tracking_error": (vision_context or {}).get("people_tracking_error") or (vision_context or {}).get("tracking_error"),
            "live_identity_fresh": bool((identity_context or {}).get("live_identity_fresh") or (vision_context or {}).get("live_identity_fresh")),
            "live_identity_address_allowed": address_reason == "explicit_profile_live_identity",
            "active_profile_is_unambiguous": bool((identity_context or {}).get("active_profile_is_unambiguous") or (vision_context or {}).get("active_profile_is_unambiguous")),
            "visual_gender_inference": False,
            "descriptors_exposed": False,
            "raw_frames_uploaded": False,
            "effective_address": address,
            "address_source": address_reason,
            "address_resolution_reason": _address_visible_person_resolution_reason(command_text, vision_context, address_reason) if intent_detected == "vision_identity.address_visible_person" else None,
            "used_visual_gender_inference": False,
            "owner_face_enrolled": bool((identity_context or {}).get("owner_face_enrolled", True)),
            "used_cached_context": str(provider_route or "").startswith("deterministic_"),
            "used_recent_owner_context": used_recent_owner_context,
            "lastOwnerVerifiedAgeMs": last_owner_verified_age_ms,
            "lastKnownPeopleCount": vision_context.get("last_known_people_count", 0),
            "actions_returned": len(actions),
            "fallback_reason": brain.get("fallback_reason"),
            "openrouter_last_status": openrouter_last_status,
            "groq_last_status": groq_last_status,
            "provider_available_now": provider_available_now,
            "vision_context_present": isinstance(vision_context, dict),
            "identity_context_present": isinstance(identity_context, dict),
            "vision_people_count": (vision_context or {}).get("people_count") if isinstance(vision_context, dict) else 0,
            "provider_chain_attempts": provider_chain_attempts,
            "tts_attempts": tts_attempts_debug,
            "tts_fallback_reason": tts_error_type,
            "backend_ms": backend_ms,
        },
        "fallback_reason": brain.get("fallback_reason"),
        "tts_attempts": tts_attempts_debug,
        "tts_debug": {
            **(tts_result.get("tts_debug") or {}),
            "requested_auto_tts": auto_tts,
            "provider_priority": _tts_provider_priority(),
            "strict_primary": _cartesia_strict_mode(),
            "required_provider": _tts_required_provider() or None,
            "browser_fallback_allowed": expo_browser_fallback_allowed if fast_path else (_tts_browser_fallback_allowed() and not _cartesia_strict_mode()),
            "tts_provider_attempts": tts_attempts_debug,
            "fish_audio_attempted": bool((tts_result.get("tts_debug") or {}).get("fish_audio_attempted")),
            "fish_audio_success": bool((tts_result.get("tts_debug") or {}).get("fish_audio_success")),
            "fish_audio_error_type": (tts_result.get("tts_debug") or {}).get("fish_audio_error_type"),
            "elevenlabs_attempted": bool((tts_result.get("tts_debug") or {}).get("elevenlabs_attempted")),
            "elevenlabs_success": bool((tts_result.get("tts_debug") or {}).get("elevenlabs_success")),
            "elevenlabs_error_type": (tts_result.get("tts_debug") or {}).get("elevenlabs_error_type"),
            "browser_speech_fallback_used": tts_provider == "browser_speech",
            "final_provider": tts_provider,
            "audio_base64_present": bool(tts_result.get("audio_base64")),
            "audio_bytes": int(tts_result.get("audio_bytes") or 0),
        },
        "tts_error_type": tts_error_type,
        "error": brain.get("error_type") if brain.get("provider") == "local_stub" else tts_error_type,
    }
    memory_assistant_write: dict[str, Any] = {"ok": False, "primary_ok": False}
    memory_tool_event: dict[str, Any] | None = None
    if result["debug"].get("tool_detected"):
        status = "success"
        if result["debug"].get("failure_class"):
            status = "blocked" if result["debug"].get("tool_risk_level") == "blocked" else "failed"
        elif actions:
            status = "dispatched"
        memory_tool_event = memory_manager.record_tool_event(
            conversation_id=conversation_id,
            event_status=status,
            tool_name=str(result["debug"].get("tool_name") or ""),
            intent_detected=str(intent_detected or ""),
            failure_class=failure_class,
            metadata={
                "request_id": body.get("request_id"),
                "actions_returned": len(actions),
                "tool_action": result["debug"].get("tool_action"),
            },
        )
    memory_status = memory_manager.memory_status()
    user_fact_write_success = not memory_user_write.get("facts") or int(memory_user_write.get("facts_saved") or 0) > 0
    memory_write_success = bool(memory_user_write.get("primary_ok", memory_user_write.get("ok")) and user_fact_write_success and memory_assistant_write.get("primary_ok", memory_assistant_write.get("ok")) and (memory_tool_event is None or memory_tool_event.get("primary_ok", memory_tool_event.get("ok"))))
    memory_facts_saved = int(memory_user_write.get("facts_saved") or 0)
    memory_local_fallback_write_success = bool(memory_user_write.get("fallback_ok") or memory_user_write.get("facts_fallback_saved") or memory_assistant_write.get("fallback_ok") or (memory_tool_event or {}).get("fallback_ok"))
    memory_read_success = bool(memory_manager.last_read_primary_ok)
    memory_failure_class = "memory_read_failed" if memory_command_intent and not memory_read_success else None
    memory_error_safe = memory_status.get("last_error_safe") or memory_status.get("last_error") or persistent_memory_context.last_error
    result["debug"]["memory"] = {
        "backend": memory_status.get("backend"),
        "status_checked": bool(memory_command_intent),
        "read_attempted": True,
        "read_success": memory_read_success,
        "write_attempted": not bool(memory_command_intent),
        "write_success": None if memory_command_intent else memory_write_success,
        "messages_saved": (1 if memory_user_write.get("ok") else 0) + (1 if memory_assistant_write.get("ok") else 0),
        "facts_saved": memory_facts_saved,
        "local_fallback_write_success": memory_local_fallback_write_success,
        "facts_loaded": len(persistent_memory_context.facts),
        "facts_returned": len(persistent_memory_context.facts) if memory_command_intent else 0,
        "context_loaded": bool(persistent_memory_context.context_text),
        "last_write_status": memory_status.get("last_write_status") or "not_attempted",
        "last_read_status": ("success" if memory_read_success else "failed") if memory_command_intent else (memory_status.get("last_read_status") or "not_attempted"),
        "supabase_configured": bool(memory_status.get("supabase_configured")),
        "local_fallback_enabled": bool(memory_status.get("local_fallback_enabled")),
        "fallback_read_success": bool(memory_command_intent and not memory_read_success and persistent_memory_context.backend == "local_fallback" and persistent_memory_context.read_ok),
        "failure_class": memory_failure_class,
        "last_error_safe": memory_error_safe,
    }
    result["debug"]["memory_backend"] = memory_status.get("backend")
    result["debug"]["memory_context_loaded"] = bool(persistent_memory_context.context_text)
    result["debug"]["memory_read_ok"] = memory_read_success
    result["debug"]["memory_write_ok"] = memory_write_success
    result["debug"]["memory_facts_saved"] = memory_facts_saved
    result["debug"]["memory_last_error"] = memory_status.get("last_error_safe") or memory_status.get("last_error")
    if memory_manager.last_read_primary_ok:
        _log_safe("persistent_read_success", {
            "backend": memory_status.get("backend"),
            "facts": len(persistent_memory_context.facts),
        })
    _schedule_voice_turn_memory(
        memory_manager=memory_manager,
        conversation_id=conversation_id,
        raw=raw,
        normalized=command_text or normalized,
        input_source="voice" if body.get("wake_detected") else "text",
        frontend_kind=str(body.get("frontend_kind") or ""),
        response_text=response_text,
        provider_route=str(provider_route or ""),
        intent_detected=str(intent_detected or ""),
        failure_class=failure_class,
        tool_name=str(result["debug"].get("tool_name") or ""),
        tool_status=("blocked" if failure_class and result["debug"].get("tool_risk_level") == "blocked" else "failed" if failure_class else "dispatched" if actions else "success") if result["debug"].get("tool_detected") else None,
        request_id=body.get("request_id"),
        tool_action=result["debug"].get("tool_action"),
        fallback_reason=brain.get("fallback_reason"),
    )
    result["debug"]["memory"]["write_attempted"] = True
    result["debug"]["memory"]["write_success"] = None
    result["debug"]["memory"]["write_mode"] = "background"
    result["debug"]["memory_write_mode"] = "background"
    _log_safe("voice_turn_backend_debug", {
        "backend_intent_detected": result["debug"]["intent_detected"],
        "provider_route": result["debug"]["provider_route"],
        "failure_class": result["debug"]["failure_class"],
        "failure_origin": result["debug"]["failure_origin"],
        "tool_name": result["debug"]["tool_name"],
        "actions_returned": result["debug"]["actions_returned"],
        "memory_backend": result["debug"]["memory_backend"],
        "memory_write_ok": result["debug"]["memory_write_ok"],
    })
    append_voice_bench_memory({
        "timestamp": _now_iso(),
        "raw_transcript": raw,
        "normalized_transcript": normalized,
        "command_text": command_text,
        "wake_detected": wake_detected,
        "alias_matched": alias,
        "aura_awake": aura_awake,
        "conversation_mode": conversation_mode or "awake_latched",
        "wake_required": wake_required,
        "conversation_id": conversation_id,
        "turn_index": len(recent_turns) + 1,
        "memory_used": bool(recent_turns),
        "history_turns": len(recent_turns),
        "vision_context_summary": _compact_vision_summary(vision_context),
        "identity_context_summary": _compact_identity_summary(identity_context),
        "address_used": address,
        "address_reason": address_reason,
        "brain_provider": result["providers"]["brain"],
        "intent_detected": result["debug"]["intent_detected"],
        "vision_match_reason": vision_reason,
        "openrouter_last_status": openrouter_last_status,
        "groq_last_status": groq_last_status,
        "provider_available_now": provider_available_now,
        "vision_context_present": result["debug"]["vision_context_present"],
        "identity_context_present": result["debug"]["identity_context_present"],
        "vision_people_count": result["debug"]["vision_people_count"],
        "brain_model": result["providers"]["brain_model"],
        "fallback_reason": result["fallback_reason"],
        "llm_ms": llm_ms,
        "tts_provider": tts_provider,
        "tts_error_type": tts_error_type,
        "tts_ms": tts_ms,
        "total_ms": result["timings"]["total_backend_ms"],
        "error_type": result["error"],
    })
    return result


async def _provider_chain_completion(
    prompt: str,
    current_text: str | None = None,
    recent_turns: list[dict[str, str]] | None = None,
    vision_context: Any = None,
    address: str = "Sir",
    *,
    timeout_seconds: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    providers = ["groq", "openrouter"]
    print("[AURA CLOUD ROUTER] primary=groq fallback=openrouter", flush=True)
    provider_timeout = min(int(timeout_seconds or GENERAL_PROVIDER_TIMEOUT_SECONDS), GENERAL_PROVIDER_TIMEOUT_SECONDS)
    provider_max_tokens = int(max_tokens or _cloud_max_tokens())
    provider_temperature = _cloud_temperature() if temperature is None else float(temperature)
    prompt_chars = len(prompt or "")
    recent_count = len(recent_turns or [])

    results: dict[str, dict[str, Any]] = {}
    for provider in providers:
        if provider == "openrouter":
            print("[AURA CLOUD ROUTER] fallback_provider=openrouter", flush=True)
        if _provider_circuit_open(provider):
            print(f"[AURA CLOUD ATTEMPT] provider={provider} status=failed reason=circuit_open", flush=True)
            result = _failure(provider, "circuit_open", f"{provider} circuit is temporarily open.", model=_openrouter_model() if provider == "openrouter" else _groq_model())
            attempts.append(_attempt_from_result(result))
            results[provider] = result
            continue
        started = _perf_ms()
        print(f"[AURA CLOUD ATTEMPT] provider={provider} status=started timeout_seconds={provider_timeout}", flush=True)
        try:
            result = await asyncio.wait_for(
                _openrouter_completion(prompt, timeout_seconds=provider_timeout, max_tokens=provider_max_tokens, temperature=provider_temperature)
                if provider == "openrouter"
                else _groq_completion(prompt, timeout_seconds=provider_timeout, max_tokens=provider_max_tokens, temperature=provider_temperature),
                timeout=provider_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            result = _failure(provider, "timeout", "Cloud request timed out.")
        except Exception:
            result = _failure(provider, "network_error", "Cloud request failed.")
        if result.get("ok") and not str(result.get("response_text") or "").strip():
            result = _failure(provider, "empty_response", "Cloud returned no answer.")
        print(f"[AURA CLOUD LATENCY] provider={provider} timeout_seconds={provider_timeout} max_tokens={provider_max_tokens} response_ms={round(_perf_ms() - started)}", flush=True)
        if not result.get("ok"):
            safe_reason = "timeout" if result.get("error_type") == "timeout" else "provider_unavailable"
            print(f"[AURA CLOUD LATENCY] provider={provider} failed=true reason={safe_reason}", flush=True)
            print(f"[AURA CLOUD ATTEMPT] provider={provider} status=failed reason={safe_reason}", flush=True)
        _log_cloud_latency(
            provider=provider,
            prompt_chars=prompt_chars,
            recent_turns=recent_count,
            timeout_seconds=provider_timeout,
            response_ms=round(_perf_ms() - started),
            fallback_used=not bool(result.get("ok")),
            reason=str(result.get("error_type") or result.get("error") or "") or None,
        )
        _record_provider_result(provider, result)
        attempts.append(_attempt_from_result(result))
        results[provider] = result
        if result.get("ok"):
            print(f"[AURA CLOUD ATTEMPT] provider={provider} status=success response_ms={round(_perf_ms() - started)}", flush=True)
            result["fallback_used"] = provider != providers[0]
            result["attempts"] = attempts
            result["fallback_reason"] = None if provider == providers[0] else f"{providers[0]}_{results[providers[0]].get('error_type') or results[providers[0]].get('error') or 'failed'}"
            return result

    print("[AURA CLOUD LATENCY] fallback_used=true reason=all_cloud_providers_failed", flush=True)
    print("[AURA REASONING FALLBACK] local_helpful_answer_used=true reason=all_cloud_failed", flush=True)
    if all((results.get(provider) or {}).get("error_type") == "circuit_open" for provider in providers):
        return {
            "ok": True,
            "provider": "local_stub",
            "model": "diagnostic_stub",
            "response_text": build_general_helpful_fallback(current_text or prompt),
            "latency_ms": 0,
            "fallback_used": True,
            "attempts": attempts,
            "fallback_reason": "brain_provider_circuit_open",
            "error_type": "cloud_reasoning_timeout",
            "error": "cloud_reasoning_timeout",
        }
    primary = results.get("openrouter") or _failure("openrouter", "skipped", "OpenRouter skipped")
    fallback = results.get("groq") or _failure("groq", "skipped", "Groq skipped")
    fallback_reason = f"openrouter_{primary.get('error_type') or primary.get('error') or 'failed'}_groq_{fallback.get('error_type') or fallback.get('error') or 'failed'}"
    return {
        "ok": True,
        "provider": "local_stub",
        "model": "diagnostic_stub",
        "response_text": build_general_helpful_fallback(current_text or prompt),
        "latency_ms": 0,
        "fallback_used": True,
        "attempts": attempts,
        "fallback_reason": fallback_reason,
        "error_type": "cloud_reasoning_timeout",
        "error": "cloud_reasoning_timeout",
    }


def _system_prompt_for_llm() -> str:
    openrouter_key = _openrouter_key()
    groq_key = _groq_key()
    openrouter_status = "configured" if openrouter_key else "missing_key"
    groq_status = "configured" if groq_key else "missing_key"
    caps = capabilities.get_capability_state()

    return (
        "You are A.U.R.A, a direct, precise, and highly capable desktop partner.\n"
        "PROVIDER DIAGNOSTICS & SYSTEM STATUS:\n"
        f"- OpenRouter API: {openrouter_status} (Model: {_openrouter_model()})\n"
        f"- Groq API: {groq_status} (Model: {_groq_model()})\n"
        "- Response Engine Current State: ONLINE and WORKING (you are actively generating this response via cloud LLM).\n\n"
        "STRICT HONESTNESS & STATUS RULES:\n"
        "1. You MUST NEVER claim the response engine, server, or processing services are under 'routine maintenance' or managed by a 'maintenance team'.\n"
        "2. If asked why the response engine is or was unavailable, state honestly that the response engine is online and operational right now. Earlier failures were caused by provider request/network errors, not system maintenance.\n"
        "3. Do not invent fake explanations or apologies. State actual facts concisely.\n"
        "4. Keep spoken responses short, natural, and suitable for voice synthesis.\n"
        "5. You MUST NOT claim access to email, cloud storage, calendar, files, apps, or external accounts unless the capability registry says that integration is configured. If not configured, say you can only compose text or guide the user manually.\n\n"
        "CAPABILITY REGISTRY:\n"
        f"- email_configured: {bool(caps.get('email', {}).get('configured'))}\n"
        f"- cloud_storage_configured: {bool(caps.get('cloud_storage', {}).get('configured'))}\n"
        f"- calendar_configured: {bool(caps.get('calendar', {}).get('configured'))}\n"
        f"- desktop_runtime_control: {bool(caps.get('desktop_runtime', {}).get('orb_ui_control'))}\n"
        f"- camera_control: {bool(caps.get('desktop_runtime', {}).get('camera_control'))}\n"
        f"- mic_control: {bool(caps.get('desktop_runtime', {}).get('mic_control'))}\n"
        f"- tracking_control: {bool(caps.get('desktop_runtime', {}).get('tracking_control'))}"
    )


async def _openrouter_completion(prompt: str, *, max_tokens: int = 80, exact: str | None = None, timeout_seconds: int = 8, temperature: float | None = None) -> dict[str, Any]:
    key = _openrouter_key()
    primary_model = _openrouter_model()
    models = [primary_model]
    for model in _openrouter_fallback_models():
        if model not in models:
            models.append(model)
    if not key:
        return _failure("openrouter", "missing_key", "OpenRouter key missing", model=primary_model, request_url_used=OPENROUTER_URL, model_attempts=[])

    model_attempts: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None
    system_content = _system_prompt_for_llm()
    for model in models:
        started = _perf_ms()
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(5, timeout_seconds))
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://127.0.0.1:8765",
                "X-Title": "A.U.R.A Local Agent",
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0 if exact else (_cloud_temperature() if temperature is None else temperature),
                "max_tokens": max_tokens,
            }
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(OPENROUTER_URL, headers=headers, json=payload) as resp:
                    text = await resp.text()
                    latency = round(_perf_ms() - started)
                    if resp.status >= 400:
                        err = _provider_http_error_type("openrouter", resp.status, text)
                        attempt = {"model": model, "ok": False, "http_status": resp.status, "error_type": err, "latency_ms": latency, "raw_error_preview": _safe_preview(text), "request_url_used": OPENROUTER_URL}
                        model_attempts.append(attempt)
                        last_result = _failure("openrouter", err, f"OpenRouter returned HTTP {resp.status}", model=model, latency_ms=latency, http_status=resp.status, raw_error_preview=attempt["raw_error_preview"], request_url_used=OPENROUTER_URL, model_attempts=model_attempts)
                        if resp.status == 404:
                            continue
                        return last_result
                    data = json.loads(text)
                    if isinstance(data, dict) and isinstance(data.get("error"), dict):
                        error_preview = _safe_preview(json.dumps(data.get("error"), ensure_ascii=False))
                        last_result = _failure("openrouter", "provider_error", "OpenRouter returned an error object.", model=model, latency_ms=latency, http_status=resp.status, raw_error_preview=error_preview, request_url_used=OPENROUTER_URL, model_attempts=model_attempts)
                        model_attempts.append({"model": model, "ok": False, "http_status": resp.status, "error_type": "provider_error", "latency_ms": latency, "raw_error_preview": error_preview, "request_url_used": OPENROUTER_URL})
                        continue
                    content, raw_shape = _chat_content_from_data(data)
                    content = _sanitize_provider_output(content)
                    ok = content == exact if exact else bool(content)
                    if not ok:
                        _log_safe("brain_provider_output_malformed_retry", {"provider": "openrouter", "model": model, "raw_shape": raw_shape})
                    attempt = {"model": model, "ok": ok, "http_status": resp.status, "error_type": None if ok else "unexpected_response", "latency_ms": latency, "request_url_used": OPENROUTER_URL}
                    model_attempts.append(attempt)
                    return {"ok": ok, "provider": "openrouter", "model": model, "http_status": resp.status, "response_text": content, "latency_ms": latency, "error": None if ok else "unexpected_response", "error_type": None if ok else "unexpected_response", "raw_shape": raw_shape if not ok else None, "raw_error_preview": "" if ok else _safe_preview(text), "request_url_used": OPENROUTER_URL, "model_attempts": model_attempts}
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
            latency = round(_perf_ms() - started)
            err_type = "timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "network_error"
            attempt = {"model": model, "ok": False, "error_type": err_type, "latency_ms": latency, "raw_error_preview": _safe_preview(str(exc)), "request_url_used": OPENROUTER_URL}
            model_attempts.append(attempt)
            last_result = _failure("openrouter", err_type, str(exc), model=model, latency_ms=latency, raw_error_preview=attempt["raw_error_preview"], request_url_used=OPENROUTER_URL, model_attempts=model_attempts)
            return last_result
        except Exception as exc:
            latency = round(_perf_ms() - started)
            attempt = {"model": model, "ok": False, "error_type": "response_parse_failed", "latency_ms": latency, "raw_error_preview": _safe_preview(str(exc)), "request_url_used": OPENROUTER_URL}
            model_attempts.append(attempt)
            last_result = _failure("openrouter", "response_parse_failed", "OpenRouter response parse failed", model=model, latency_ms=latency, raw_error_preview=attempt["raw_error_preview"], request_url_used=OPENROUTER_URL, model_attempts=model_attempts)
            continue
    if last_result:
        last_result["model_attempts"] = model_attempts
        return last_result
    return _failure("openrouter", "unknown", "OpenRouter did not produce a result.", model=primary_model, request_url_used=OPENROUTER_URL, model_attempts=model_attempts)


async def _groq_completion(prompt: str, *, max_tokens: int = 160, exact: str | None = None, timeout_seconds: int = 8, temperature: float | None = None) -> dict[str, Any]:
    key = _groq_key()
    model = _groq_model()
    if not key:
        return _failure("groq", "missing_key", "Groq key missing", model=model)
    started = _perf_ms()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(5, timeout_seconds))
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt_for_llm()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0 if exact else (_cloud_temperature() if temperature is None else temperature),
            "max_completion_tokens": max_tokens,
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(GROQ_URL, headers=headers, json=payload) as resp:
                text = await resp.text()
                latency = round(_perf_ms() - started)
                if resp.status >= 400:
                    return _failure("groq", _error_class(resp.status, text, provider="groq"), f"Groq returned HTTP {resp.status}", model=model, latency_ms=latency, http_status=resp.status, raw_error_preview=_safe_preview(text), request_url_used=GROQ_URL, model_attempts=[{"model": model, "ok": False, "http_status": resp.status, "error_type": _error_class(resp.status, text, provider="groq"), "latency_ms": latency, "raw_error_preview": _safe_preview(text), "request_url_used": GROQ_URL}])
                data = json.loads(text)
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    error_preview = _safe_preview(json.dumps(data.get("error"), ensure_ascii=False))
                    return _failure("groq", "provider_error", "Groq returned an error object.", model=model, latency_ms=latency, http_status=resp.status, raw_error_preview=error_preview, request_url_used=GROQ_URL, model_attempts=[{"model": model, "ok": False, "http_status": resp.status, "error_type": "provider_error", "latency_ms": latency, "raw_error_preview": error_preview, "request_url_used": GROQ_URL}])
                content, raw_shape = _chat_content_from_data(data)
                content = _sanitize_provider_output(content)
                ok = content == exact if exact else bool(content)
                if not ok:
                    _log_safe("brain_provider_output_malformed_retry", {"provider": "groq", "model": model, "raw_shape": raw_shape})
                return {"ok": ok, "provider": "groq", "model": model, "http_status": resp.status, "response_text": content, "latency_ms": latency, "error": None if ok else "unexpected_response", "error_type": None if ok else "unexpected_response", "raw_shape": raw_shape if not ok else None, "raw_error_preview": "" if ok else _safe_preview(text), "request_url_used": GROQ_URL, "model_attempts": [{"model": model, "ok": ok, "http_status": resp.status, "error_type": None if ok else "unexpected_response", "latency_ms": latency, "request_url_used": GROQ_URL}]}
    except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
        err = "timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else _client_error_type(exc)
        return _failure("groq", err, str(exc), model=model, latency_ms=round(_perf_ms() - started), raw_error_preview=_safe_preview(str(exc)), request_url_used=GROQ_URL, model_attempts=[{"model": model, "ok": False, "error_type": err, "latency_ms": round(_perf_ms() - started), "raw_error_preview": _safe_preview(str(exc)), "request_url_used": GROQ_URL}])
    except Exception as exc:
        return _failure("groq", "response_parse_failed", "Groq response parse failed", model=model, latency_ms=round(_perf_ms() - started), raw_error_preview=_safe_preview(str(exc)), request_url_used=GROQ_URL, model_attempts=[{"model": model, "ok": False, "error_type": "response_parse_failed", "latency_ms": round(_perf_ms() - started), "raw_error_preview": _safe_preview(str(exc)), "request_url_used": GROQ_URL}])


def append_voice_bench_memory(entry: dict[str, Any]) -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in entry.items() if "key" not in k.lower() and "token" not in k.lower() and "authorization" not in k.lower()}
    with VOICE_BENCH_MEMORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


def export_voice_bench_memory() -> dict[str, Any]:
    if not VOICE_BENCH_MEMORY.exists():
        return {"ok": True, "path": str(VOICE_BENCH_MEMORY.relative_to(ROOT)), "entries": 0, "items": []}
    items = []
    with VOICE_BENCH_MEMORY.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"ok": True, "path": str(VOICE_BENCH_MEMORY.relative_to(ROOT)), "entries": len(items), "items": items[-100:]}


def clear_voice_bench_memory() -> dict[str, Any]:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    VOICE_BENCH_MEMORY.write_text("", encoding="utf-8")
    return {"ok": True, "path": str(VOICE_BENCH_MEMORY.relative_to(ROOT)), "entries": 0}


def latest_latency() -> dict[str, Any]:
    if not LATEST_PATH.exists():
        return {"ok": False, "error": "no_latency_result"}
    try:
        return {"ok": True, "latest": json.loads(LATEST_PATH.read_text(encoding="utf-8"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def export_report(payload: dict[str, Any]) -> dict[str, Any]:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DIAG_DIR / f"aura_voice_diagnostic_report_{stamp}.md"
    lines = [
        "# A.U.R.A Voice Diagnostic Report",
        "",
        f"Generated: {_now_iso()}",
        "",
        "## Provider Status",
        "```json",
        json.dumps(payload.get("providers", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## API Tests",
        "```json",
        json.dumps(payload.get("api_tests", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Voice Test",
        "```json",
        json.dumps(payload.get("voice_test", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Audio Output",
        "```json",
        json.dumps(payload.get("audio_output", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Notes",
        "- Secrets are masked or omitted.",
        "- No raw microphone audio is stored.",
        "- Keep diagnostics on localhost only.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    _log_safe("export_report", {"path": str(path.relative_to(ROOT))})
    return {"ok": True, "path": str(path.relative_to(ROOT))}


def _failure(provider: str, error: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "provider": provider,
        **extra,
        "latency_ms": extra.get("latency_ms"),
        "error": error,
        "error_type": extra.get("error_type") or error,
        "message": message[:300],
    }
