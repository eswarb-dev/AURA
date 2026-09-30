"""Secret-safe redaction before persistent memory writes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

SECRET_PATTERNS = [
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|gsk|pk|rk|or|elev|fish|cartesia|sb)[-_A-Za-z0-9]{12,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?im)^\s*[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|SERVICE_ROLE)[A-Z0-9_]*\s*=\s*.+$"),
    re.compile(r"(?i)\b(password|passwd|pwd|api[_ -]?key|token|secret|service[_ -]?role)\s*[:=]\s*[^\s,;]{4,}"),
    re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b"),
    re.compile(r"(?i)\b(cookie|sessionid|csrf|xsrf|refresh_token|access_token)\s*[:=]\s*[^\s,;]{8,}"),
]

PRIVATE_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+\\[^\n\r\t]*")
SENSITIVE_METADATA_KEYS = {
    "audio_base64",
    "base64",
    "clipboard",
    "clipboard_text",
    "screenshot",
    "screenshot_path",
    "frame",
    "camera_frame",
    "face_descriptor",
    "embedding",
    "cookies",
    "password",
    "token",
    "api_key",
    "authorization",
    "service_role",
}


def redact_for_memory(text: str) -> str:
    value = str(text or "")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    value = PRIVATE_PATH_RE.sub("[REDACTED_PATH]", value)
    return value[:12000]


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[REDACTED_NESTED]"
    if isinstance(value, str):
        return redact_for_memory(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return redact_metadata_for_memory(value, depth + 1)
    return redact_for_memory(str(value))


def redact_metadata_for_memory(metadata: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    if not isinstance(metadata, dict):
        return safe
    for key, value in metadata.items():
        key_text = str(key)[:80]
        lowered = key_text.lower()
        if any(marker in lowered for marker in SENSITIVE_METADATA_KEYS):
            safe[key_text] = REDACTED
            continue
        if isinstance(value, (str, Path)) and ("base64" in lowered or len(str(value)) > 4000):
            safe[key_text] = redact_for_memory(str(value)[:400])
            continue
        safe[key_text] = _safe_value(value, depth + 1)
    return safe
