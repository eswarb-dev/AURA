"""Voice command intent classification.

Lightweight, deterministic — no LLM involved. Intercepts a transcribed wake-word
utterance *before* it reaches the normal chat turn, so simple commands like
"create a new project called finance" can be answered immediately by JARVIS
without burning a full LLM round-trip.

Add new commands by extending ``INTENT_PATTERNS`` and providing a handler in
``handle_voice_intent``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.voice_intents")

SendFn = Callable[[dict[str, Any]], Awaitable[None]]

AURA_WAKE_ALIASES = [
    "wakeup", "wake up", "wake-up", "wake up aura", "wake up a u r a",
    "aura", "a u r a", "a.u.r.a", "hey aura", "hey a u r a",
    "hello aura", "ok aura", "okay aura", "ora", "hey ora",
    "aurora", "hey aurora", "aira", "hey aira", "ara", "hey ara",
    "oura", "hey oura", "cosmo", "hey cosmo", "abba", "hey abba",
    "eva", "hey eva", "ada", "hey ada",
]
_KNOWN_MISTAKES = {"makeup", "wake", "wake-up", "ora", "aurora", "aira", "ara", "oura", "cosmo", "abba", "eva", "ada"}
_NOISE = {"uh", "um", "hmm", "..."}


def _clean_for_wake(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", re.sub(r"[.,!?;:-]+", " ", text.strip().lower())).strip()
    replacements = (
        (r"\bcrhome\b", "chrome"),
        (r"\bcrome\b", "chrome"),
        (r"\bchrom\b", "chrome"),
        (r"\bchrome\s+tap\b", "chrome tab"),
        (r"\bsystme\b", "system"),
    )
    for pattern, value in replacements:
        cleaned = re.sub(pattern, value, cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    left = _clean_for_wake(a)
    right = _clean_for_wake(b)
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return 1.0 - (_levenshtein(left, right) / max(len(left), len(right)))


def _find_phrase(words: list[str], phrase: list[str]) -> int:
    if not phrase:
        return -1
    for i in range(0, len(words) - len(phrase) + 1):
        if words[i:i + len(phrase)] == phrase:
            return i
    return -1


def _replace_alias(text: str, alias: str) -> str:
    words = text.split()
    alias_words = alias.split()
    idx = _find_phrase(words, alias_words)
    if idx < 0:
        return text
    before = [w for w in words[:idx] if w not in {"hey", "hello", "ok", "okay"}]
    after = words[idx + len(alias_words):]
    return " ".join([*before, "hey", "aura", *after]).strip()


def normalize_aura_transcript(text: str) -> dict[str, Any]:
    """Normalize common STT wake-name mistakes while preserving raw text."""
    raw = (text or "").strip()
    cleaned = _clean_for_wake(raw)
    if not cleaned or cleaned in _NOISE:
        return {
            "raw": raw,
            "normalized": cleaned,
            "wake_detected": False,
            "alias_matched": None,
            "confidence_hint": "low",
        }

    for alias in sorted(AURA_WAKE_ALIASES, key=len, reverse=True):
        alias_clean = _clean_for_wake(alias)
        if _find_phrase(cleaned.split(), alias_clean.split()) >= 0:
            matched = re.sub(r"^(hey|hello|ok|okay)\s+", "", alias_clean)
            return {
                "raw": raw,
                "normalized": _replace_alias(cleaned, alias_clean),
                "wake_detected": True,
                "alias_matched": matched,
                "confidence_hint": "medium" if matched in _KNOWN_MISTAKES else "high",
            }

    words = cleaned.split()
    for word in words:
        if similarity(word, "aura") >= 0.65:
            return {
                "raw": raw,
                "normalized": _replace_alias(cleaned, word),
                "wake_detected": True,
                "alias_matched": word,
                "confidence_hint": "low",
            }

    if similarity(" ".join(words[:2]), "hey aura") >= 0.65:
        return {
            "raw": raw,
            "normalized": " ".join(["hey", "aura", *words[2:]]).strip(),
            "wake_detected": True,
            "alias_matched": " ".join(words[:2]),
            "confidence_hint": "low",
        }

    return {
        "raw": raw,
        "normalized": cleaned,
        "wake_detected": False,
        "alias_matched": None,
        "confidence_hint": "low",
    }


# Each pattern is (regex, intent_name). First match wins. Patterns are
# intentionally specific so we never false-positive on normal chat.
INTENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(
        r"\b(create|add|new|register)\b.{0,12}\b(project|profile)\b"
        r"(?:\s+(?:called|named|for))?\s+(?P<name>[a-z0-9][a-z0-9_-]{0,31})\b",
        re.IGNORECASE),
     "create_project"),
    (re.compile(
        r"\b(list|show)\b(?:\s+(?:me|all))?\s+(?:all\s+)?\b(projects|profiles|modes)\b",
        re.IGNORECASE),
     "list_projects"),
    (re.compile(
        r"\bswitch\s+to\s+(?:project\s+|profile\s+|mode\s+)?(?P<name>[a-z0-9][a-z0-9_-]{0,31})\b",
        re.IGNORECASE),
     "switch_mode"),
]


def classify(text: str) -> tuple[str, dict[str, str]] | None:
    """Return (intent_name, groups) if the utterance matches a voice command, else None."""
    if not text:
        return None
    for pattern, intent in INTENT_PATTERNS:
        m = pattern.search(text)
        if m:
            return intent, m.groupdict()
    return None


async def handle_voice_intent(
    intent: str,
    groups: dict[str, str],
    *,
    send: SendFn,
    broadcast: SendFn | None = None,
) -> bool:
    """Dispatch a voice-intent. Returns True if it handled the utterance
    (so the caller should NOT pass it to the normal chat loop)."""

    if intent == "list_projects":
        from .admin.add_project import list_projects
        projects = list_projects()
        names = [p["name"] for p in projects]
        if send:
            text = "Registered projects: " + ", ".join(names)
            await send({"type": "toast", "kind": "info", "message": text})
        log.info("voice-intent list_projects: %s", names)
        return True

    if intent == "switch_mode":
        from .admin.add_project import list_projects
        name = (groups.get("name") or "").lower()
        valid = {p["name"] for p in list_projects()}
        if name not in valid:
            if send:
                await send({"type": "toast", "kind": "err",
                            "message": f"Unknown mode: {name}"})
            return True
        # Switch via HTTP-style call: we can't easily reach the FastAPI handler
        # from inside this module without circular deps, so we just call the
        # underlying state-mutator exposed in main.py.
        from . import main as _main
        _main._current_mode = name
        _main._reset_conversation()
        await _main.hub.broadcast({"type": "mode_changed", "mode": name})
        if send:
            await send({"type": "toast", "kind": "ok", "message": f"Mode: {name}"})
        log.info("voice-intent switch_mode: %s", name)
        return True

    if intent == "create_project":
        name = (groups.get("name") or "").lower()
        # We need a cwd. The user didn't supply one via voice, so we prompt
        # the orb UI to open the wizard with this name pre-filled. Falls back
        # to a toast telling them to finish via the UI.
        from . import main as _main
        await _main.hub.broadcast({
            "type": "wizard_request",
            "intent": "create_project",
            "name": name,
        })
        if send:
            await send({"type": "toast", "kind": "info",
                        "message": (f"Opening project wizard for '{name}'. "
                                    "Pick the working directory and continue.")})
        log.info("voice-intent create_project: name=%s", name)
        return True

    return False
