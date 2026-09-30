from __future__ import annotations

import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JARVIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = JARVIS_DIR.parent

IDENTITY_STORAGE_DIR = Path(os.getenv("AURA_IDENTITY_STORAGE_DIR", "diagnostics/identity"))
if not IDENTITY_STORAGE_DIR.is_absolute():
    IDENTITY_STORAGE_DIR = PROJECT_ROOT / IDENTITY_STORAGE_DIR
PROFILES_PATH = IDENTITY_STORAGE_DIR / "profiles.json"
EVENTS_PATH = IDENTITY_STORAGE_DIR / "identity_events.jsonl"
_LAST_LOAD_WARNING: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _storage_dir() -> Path:
    return IDENTITY_STORAGE_DIR


def _profiles_path() -> Path:
    return PROFILES_PATH


def _events_path() -> Path:
    return EVENTS_PATH


def _threshold(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _threshold_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _default_profiles() -> dict[str, Any]:
    now = _now_iso()
    return {
        "version": 1,
        "profiles": [
            {
                "id": "owner",
                "display_name": "Owner",
                "role": "owner",
                "address": "Sir",
                "preferred_address": "Sir",
                "address_source": "explicit_profile",
                "face_embedding": None,
                "voice_embedding": None,
                "face_adapter": None,
                "voice_adapter": None,
                "face_enrolled_at": None,
                "voice_enrolled_at": None,
                "created_at": now,
                "updated_at": now,
            }
        ],
    }


def ensure_identity_storage() -> None:
    IDENTITY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if not PROFILES_PATH.exists():
        save_profiles_atomic(_default_profiles())


def _normalize_profiles_data(data: Any) -> dict[str, Any]:
    default = _default_profiles()
    if not isinstance(data, dict):
        return default
    if isinstance(data.get("profiles"), list):
        profiles = [p for p in data.get("profiles", []) if isinstance(p, dict)]
    elif data.get("id") or data.get("display_name") or data.get("face_embedding") is not None:
        profiles = [data]
    else:
        profiles = [v for v in data.values() if isinstance(v, dict) and v.get("id")]
    by_id = {str(profile.get("id") or "").strip(): profile for profile in profiles}
    if "owner" not in by_id:
        by_id["owner"] = default["profiles"][0]
    normalized = {"version": int(data.get("version") or 1), "profiles": []}
    for profile_id, profile in by_id.items():
        if not profile_id:
            continue
        base = dict(default["profiles"][0]) if profile_id == "owner" else {}
        base.update(profile)
        base["id"] = profile_id
        base.setdefault("display_name", "Owner" if profile_id == "owner" else profile_id)
        base.setdefault("role", "owner" if profile_id == "owner" else "known_guest")
        preferred = base.get("preferred_address") or base.get("address") or base.get("honorific") or ("Sir" if profile_id == "owner" else "Guest")
        if preferred == "Mam":
            preferred = "Ma'am"
        base["preferred_address"] = preferred
        base["address"] = preferred
        base["address_source"] = base.get("address_source") or "explicit_profile"
        base.setdefault("face_embedding", None)
        base.setdefault("voice_embedding", None)
        base.setdefault("face_adapter", None)
        base.setdefault("voice_adapter", None)
        base.setdefault("face_enrolled_at", None)
        base.setdefault("voice_enrolled_at", None)
        base.setdefault("created_at", _now_iso())
        base.setdefault("updated_at", base.get("created_at"))
        normalized["profiles"].append(base)
    return normalized


def load_profiles() -> dict[str, Any]:
    global _LAST_LOAD_WARNING
    _LAST_LOAD_WARNING = None
    ensure_identity_storage()
    path = _profiles_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        normalized = _normalize_profiles_data(raw)
        if raw != normalized:
            save_profiles_atomic(normalized)
        return normalized
    except Exception:
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = path.with_name(f"{path.name}.broken.{stamp}")
            try:
                shutil.copy2(path, backup)
                _LAST_LOAD_WARNING = f"invalid_profiles_json_backed_up_to_{backup.name}"
            except Exception:
                _LAST_LOAD_WARNING = "invalid_profiles_json_backup_failed"
            save_profiles_atomic(_default_profiles())
        return _default_profiles()


def _profiles_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(profile.get("id")): profile for profile in data.get("profiles", []) if isinstance(profile, dict) and profile.get("id")}


def _default_owner_profile() -> dict[str, Any]:
    return dict(_default_profiles()["profiles"][0])


def save_profiles_atomic(data: dict[str, Any]) -> None:
    IDENTITY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_profiles_data(data)
    tmp = PROFILES_PATH.with_suffix(PROFILES_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(normalized, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(PROFILES_PATH)


def _load_profiles() -> dict[str, dict[str, Any]]:
    return _profiles_by_id(load_profiles())


def _save_profiles(profiles: dict[str, Any]) -> None:
    if "profiles" in profiles:
        save_profiles_atomic(profiles)
    else:
        save_profiles_atomic({"version": 1, "profiles": list(profiles.values())})


def _valid_embedding(value: Any, allow_empty: bool = False) -> list[float]:
    if not isinstance(value, list):
        raise ValueError("embedding_must_be_array")
    if not value and allow_empty:
        return []
    if not value or len(value) > 2048:
        raise ValueError("invalid_embedding_length")
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError("embedding_must_be_numeric")
        result.append(float(item))
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    left = math.sqrt(sum(x * x for x in a))
    right = math.sqrt(sum(y * y for y in b))
    if left <= 0 or right <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (left * right)))


def _public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": profile.get("id"),
        "display_name": profile.get("display_name"),
        "role": profile.get("role"),
        "address": profile.get("address"),
        "preferred_address": profile.get("preferred_address") or profile.get("address"),
        "address_source": profile.get("address_source") or "explicit_profile",
        "face_enrolled": bool(profile.get("face_embedding")),
        "voice_enrolled": bool(profile.get("voice_embedding")),
        "face_adapter": profile.get("face_adapter"),
        "voice_adapter": profile.get("voice_adapter"),
        "updated_at": profile.get("updated_at"),
        "face_enrolled_at": profile.get("face_enrolled_at"),
        "voice_enrolled_at": profile.get("voice_enrolled_at"),
    }


def resolve_effective_address(identity_summary: dict[str, Any] | None, default_address: str = "Sir") -> dict[str, Any]:
    summary = identity_summary if isinstance(identity_summary, dict) else {}
    threshold = _threshold("AURA_FACE_MATCH_THRESHOLD", 0.75)
    ttl_ms = _threshold_int("AURA_IDENTITY_LIVE_MATCH_TTL_MS", 5000)
    people_tracking_state = str(
        summary.get("people_tracking_state")
        or summary.get("people_tracking_status")
        or summary.get("tracking_state")
        or summary.get("tracking_status")
        or ""
    ).strip().lower()
    gesture_tracking_state = str(summary.get("gesture_tracking_state") or "off").strip().lower()
    identity_state = str(summary.get("identity_state") or summary.get("decision") or summary.get("current_identity") or "").strip().lower()
    try:
        confidence = float(summary.get("identity_confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    last_match_at = summary.get("last_live_match_at") or summary.get("last_owner_verified_at")
    live_identity_fresh = bool(summary.get("live_identity_fresh", True))
    if last_match_at:
        try:
            stamp = str(last_match_at).replace("Z", "+00:00")
            age_ms = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() * 1000
            live_identity_fresh = live_identity_fresh and age_ms <= ttl_ms
        except Exception:
            live_identity_fresh = False
    active_profile_is_unambiguous = bool(summary.get("active_profile_is_unambiguous", True))
    live_identity_address_allowed = (
        people_tracking_state == "active"
        and identity_state in {"matched", "owner", "owner_verified_face_only", "owner_verified_high", "owner_verified_voice_only"}
        and confidence >= threshold
        and active_profile_is_unambiguous
        and live_identity_fresh
    )
    matched = summary.get("matched_profile") if isinstance(summary.get("matched_profile"), dict) else None
    if live_identity_address_allowed and matched and matched.get("preferred_address"):
        return {
            "effective_address": str(matched.get("preferred_address")),
            "address_source": "explicit_profile_live_identity",
            "matched_profile_name": matched.get("display_name"),
            "matched_profile_id": matched.get("id"),
            "used_visual_gender_inference": False,
            "live_identity_address_allowed": True,
            "people_tracking_state": people_tracking_state,
            "gesture_tracking_state": gesture_tracking_state,
        }
    matched_profile_id = str(summary.get("matched_profile_id") or "").strip()
    if live_identity_address_allowed and matched_profile_id:
        profile = _load_profiles().get(matched_profile_id, {})
        if profile.get("preferred_address"):
            return {
                "effective_address": str(profile.get("preferred_address")),
                "address_source": "explicit_profile_live_identity",
                "matched_profile_name": profile.get("display_name"),
                "matched_profile_id": profile.get("id") or matched_profile_id,
                "used_visual_gender_inference": False,
                "live_identity_address_allowed": True,
                "people_tracking_state": people_tracking_state,
                "gesture_tracking_state": gesture_tracking_state,
            }
    if live_identity_address_allowed and (summary.get("owner_verified") or summary.get("current_identity") == "owner" or summary.get("role") == "owner"):
        owner = _load_profiles().get("owner", _default_owner_profile())
        return {
            "effective_address": str(owner.get("preferred_address") or owner.get("address") or default_address),
            "address_source": "explicit_profile_live_identity",
            "matched_profile_name": owner.get("display_name"),
            "matched_profile_id": owner.get("id") or "owner",
            "used_visual_gender_inference": False,
            "live_identity_address_allowed": True,
            "people_tracking_state": people_tracking_state,
            "gesture_tracking_state": gesture_tracking_state,
        }
    return {
        "effective_address": default_address,
        "address_source": "default",
        "matched_profile_name": None,
        "matched_profile_id": None,
        "used_visual_gender_inference": False,
        "live_identity_address_allowed": False,
        "people_tracking_state": people_tracking_state or "off",
        "gesture_tracking_state": gesture_tracking_state or "off",
    }


def test_effective_address(body: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "matched_profile": body.get("matched_profile") if isinstance(body.get("matched_profile"), dict) else None,
        "people_count": body.get("people_count"),
        "owner_verified": bool(body.get("owner_verified")),
    }
    resolved = resolve_effective_address(summary, default_address="Sir")
    append_identity_event({
        "type": "effective_address_test",
        "effective_address": resolved["effective_address"],
        "address_source": resolved["address_source"],
        "matched_profile": resolved.get("matched_profile_name"),
        "visual_gender_inference": False,
    })
    return {"ok": True, **resolved}


def profiles() -> dict[str, Any]:
    try:
        data = load_profiles()
        safe_profiles = [_public_profile(profile) for profile in data.get("profiles", []) if isinstance(profile, dict)]
    except Exception:
        return safe_identity_error("identity_profile_load_failed")
    return {
        "ok": True,
        "profiles": safe_profiles,
        "profiles_by_id": {str(profile.get("id")): profile for profile in safe_profiles if profile.get("id")},
        "storage": {
            "profiles_path": _safe_relative(PROFILES_PATH),
            "exists": PROFILES_PATH.exists(),
        },
        "warning": _LAST_LOAD_WARNING,
    }


def enroll_face(body: dict[str, Any]) -> dict[str, Any]:
    try:
        embedding = _valid_embedding(body.get("embedding"), allow_empty=True)
    except ValueError:
        return {"ok": False, "error": "invalid_face_embedding"}
    profile_id = str(body.get("profile_id") or body.get("id") or "owner").strip() or "owner"
    adapter = str(body.get("adapter") or "face_api_descriptor_v1").strip()
    if adapter != "face_api_descriptor_v1":
        return {"ok": False, "error": "invalid_adapter"}
    if not embedding:
        return {"ok": False, "error": "empty_face_embedding"}
    if len(embedding) != 128:
        return {"ok": False, "error": "invalid_face_embedding", "expected_length": 128, "actual_length": len(embedding)}
    profiles_data = _load_profiles()
    owner = profiles_data.setdefault(profile_id, _default_owner_profile())
    owner["id"] = profile_id
    owner["face_embedding"] = embedding
    owner["face_adapter"] = adapter
    now = _now_iso()
    owner["face_enrolled_at"] = now
    owner["updated_at"] = now
    _save_profiles(profiles_data)
    append_identity_event({"type": "owner_face_enrolled", "profile_id": profile_id, "source": body.get("source"), "adapter": adapter})
    return {
        "ok": True,
        "profile": _public_profile(owner),
        "storage": {"profiles_path": _safe_relative(PROFILES_PATH), "persisted": True},
    }


def verify_face(body: dict[str, Any]) -> dict[str, Any]:
    try:
        embedding = _valid_embedding(body.get("embedding"))
    except ValueError:
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "invalid_face_embedding"}
    adapter = str(body.get("adapter") or "face_api_descriptor_v1").strip()
    if adapter != "face_api_descriptor_v1":
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "invalid_adapter"}
    if len(embedding) != 128:
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "invalid_face_embedding", "expected_length": 128, "actual_length": len(embedding)}
    owner = _load_profiles().get("owner", {})
    if not owner.get("face_embedding"):
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "owner_face_not_enrolled"}
    stored_adapter = owner.get("face_adapter") or "face_api_descriptor_v1"
    if adapter != stored_adapter:
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "face_adapter_mismatch", "adapter": adapter, "stored_adapter": stored_adapter}
    score = _cosine(embedding, [float(x) for x in owner.get("face_embedding", [])])
    threshold = _threshold("AURA_FACE_MATCH_THRESHOLD", 0.75)
    matched = score >= threshold
    return {"ok": True, "matched": matched, "confidence": round(score, 4), "threshold": threshold, "adapter": adapter, "decision": "owner_face_verified" if matched else "owner_face_not_verified"}


def clear_face(body: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_id = str((body or {}).get("profile_id") or (body or {}).get("id") or "owner").strip() or "owner"
    profiles_data = _load_profiles()
    owner = profiles_data.setdefault(profile_id, _default_owner_profile())
    owner["face_embedding"] = None
    owner["face_adapter"] = None
    owner["face_enrolled_at"] = None
    owner["updated_at"] = _now_iso()
    _save_profiles(profiles_data)
    append_identity_event({"type": "owner_face_cleared", "profile_id": profile_id})
    return {"ok": True, "profile": _public_profile(owner)}


def enroll_voice(body: dict[str, Any]) -> dict[str, Any]:
    embedding = _valid_embedding(body.get("embedding"), allow_empty=True)
    profiles_data = _load_profiles()
    owner = profiles_data.setdefault("owner", _default_owner_profile())
    owner["voice_embedding"] = embedding or None
    owner["voice_adapter"] = str(body.get("adapter") or "voice_features_v1").strip() if embedding else None
    owner["voice_enrolled_at"] = _now_iso() if embedding else None
    owner["updated_at"] = _now_iso()
    _save_profiles(profiles_data)
    append_identity_event({"type": "owner_voice_enrolled" if embedding else "owner_voice_cleared", "profile_id": "owner"})
    return {"ok": True, "profile": _public_profile(owner)}


def verify_voice(body: dict[str, Any]) -> dict[str, Any]:
    embedding = _valid_embedding(body.get("embedding"))
    owner = _load_profiles().get("owner", {})
    if not owner.get("voice_embedding"):
        return {"ok": False, "matched": False, "confidence": 0.0, "error": "owner_voice_not_enrolled"}
    score = _cosine(embedding, [float(x) for x in owner.get("voice_embedding", [])])
    threshold = _threshold("AURA_VOICE_MATCH_THRESHOLD", 0.70)
    matched = score >= threshold
    return {"ok": True, "matched": matched, "confidence": round(score, 4), "threshold": threshold, "decision": "owner_voice_verified" if matched else "owner_voice_not_verified"}


def fusion_check(body: dict[str, Any]) -> dict[str, Any]:
    face_owner = bool(body.get("face_owner"))
    voice_owner = bool(body.get("voice_owner"))
    voice_status = str(body.get("voice_status") or "").strip().lower()
    voice_checked = "voice_owner" in body and voice_status not in {"", "not_enrolled", "not_tested", "missing"}
    if face_owner and voice_owner:
        decision, confidence = "owner_verified_high", 0.91
    elif face_owner and voice_checked:
        decision, confidence = "identity_mismatch", 0.35
    elif face_owner:
        decision, confidence = "owner_verified_face_only", 0.8
    elif voice_owner:
        decision, confidence = "owner_verified_voice_only", 0.74
    else:
        decision, confidence = "guest_or_unknown", 0.0
    owner = _load_profiles().get("owner", _default_owner_profile())
    identity = "owner" if decision.startswith("owner_verified") else "unknown"
    result = {
        "ok": True,
        "decision": decision,
        "identity": identity,
        "display_name": owner.get("display_name", "Owner") if identity == "owner" else "Guest",
        "address": owner.get("preferred_address") or owner.get("address", "Sir") if identity == "owner" else "Guest",
        "preferred_address": owner.get("preferred_address") if identity == "owner" else None,
        "address_source": owner.get("address_source", "explicit_profile") if identity == "owner" else "default",
        "confidence": confidence,
    }
    event({"type": "fusion_check", "decision": decision, "identity": identity})
    return result


def append_identity_event(body: dict[str, Any]) -> dict[str, Any]:
    ensure_identity_storage()
    safe = {k: v for k, v in body.items() if k not in {"embedding", "audio", "image", "frame", "raw_audio"}}
    safe["timestamp"] = _now_iso()
    with _events_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(safe) + "\n")
    return {"ok": True}


def event(body: dict[str, Any]) -> dict[str, Any]:
    return append_identity_event(body)


def storage_status() -> dict[str, Any]:
    return debug_identity_storage_status()


def debug_identity_storage_status() -> dict[str, Any]:
    try:
        data = load_profiles()
        owner = _profiles_by_id(data).get("owner", {})
        modified = datetime.fromtimestamp(PROFILES_PATH.stat().st_mtime, timezone.utc).isoformat() if PROFILES_PATH.exists() else None
        return {
            "ok": True,
            "storage_dir": _safe_relative(IDENTITY_STORAGE_DIR),
            "profiles_path": _safe_relative(PROFILES_PATH),
            "profiles_exists": PROFILES_PATH.exists(),
            "events_path": _safe_relative(EVENTS_PATH),
            "events_exists": EVENTS_PATH.exists(),
            "profiles_count": len(data.get("profiles", [])),
            "owner_face_enrolled": bool(owner.get("face_embedding")),
            "owner_voice_enrolled": bool(owner.get("voice_embedding")),
            "face_adapter": owner.get("face_adapter"),
            "voice_adapter": owner.get("voice_adapter"),
            "profiles_modified_at": modified,
            "warning": _LAST_LOAD_WARNING,
        }
    except Exception:
        return safe_identity_error("identity_storage_status_failed")


def safe_identity_error(error_type: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": error_type,
        "message": "Identity profile could not be loaded. See server log.",
        "storage": {
            "profiles_path": _safe_relative(PROFILES_PATH),
            "exists": PROFILES_PATH.exists(),
        },
    }


def status() -> dict[str, Any]:
    return {
        "ok": True,
        "enabled": os.getenv("AURA_ENABLE_EXPO_VISION_IDENTITY", "true").lower() == "true",
        "storage_dir": str(_storage_dir()),
        "thresholds": {
            "face": _threshold("AURA_FACE_MATCH_THRESHOLD", 0.75),
            "voice": _threshold("AURA_VOICE_MATCH_THRESHOLD", 0.70),
            "gender": _threshold("AURA_GENDER_CONFIDENCE_THRESHOLD", 0.75),
        },
        "profiles": profiles()["profiles"],
    }
