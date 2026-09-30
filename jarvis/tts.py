"""Text-to-speech.

Engine priority:
  1. edge-tts  — Microsoft neural TTS (internet required; needs mpv or ffplay)
  2. piper     — Offline neural TTS fallback

Public API:
  speak(text, lang)    — strip, truncate, synthesise, play; blocks until done
  cancel_speaking()    — kill current utterance and discard any queued ones
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import shutil
from typing import Any

IS_WINDOWS = platform.system() == "Windows"


# ── Text cleaning ─────────────────────────────────────────────────────────

_JARVIS_BLOCK_RE   = re.compile(r"<jarvis:([a-zA-Z_][\w-]*)\b[^>]*>.*?</jarvis:\1>", re.DOTALL)
_JARVIS_SELF_RE    = re.compile(r"</?jarvis:[^>]*/?>")
# Partial tags that leaked through the stream parser, e.g. "arvis:step n="7".../>"
_PARTIAL_TAG_RE    = re.compile(r"\b\w+:(?:step|task_plan|task_complete|think)\b[^>\n]*/?>")
_ANY_XML_TAG_RE    = re.compile(r"<[^>]+>")
_MD_FENCE_RE       = re.compile(r"```[\s\S]*?```")
_MD_TABLE_RE       = re.compile(r"^\|.*\|.*$", re.MULTILINE)
_MD_RULE_RE        = re.compile(r"^(?:[-*_]){3,}\s*$", re.MULTILINE)
_MD_HEADER_RE      = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD_RE        = re.compile(r"\*{2,3}([^*\n]+)\*{2,3}")
_MD_ITALIC_RE      = re.compile(r"\*([^*\n]+)\*")
_MD_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE        = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_EMOJI_RE          = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF\U00002702-\U000027B0]+"
)
_TOOL_ARTIFACT_RE  = re.compile(r"\[tool_(?:call|result):[^\]]*\]")
_EXTRA_PUNCT_RE    = re.compile(r"[*_`#>|\\]+")
_WHITESPACE_RE     = re.compile(r"\s+")
_AURA_DOTTED_RE    = re.compile(r"\bA\s*\.\s*U\s*\.\s*R\s*\.\s*A\b", re.IGNORECASE)
_AURA_SPACED_RE    = re.compile(r"\bA\s+U\s+R\s+A\b", re.IGNORECASE)
_AURA_WORD_RE      = re.compile(r"\bAURA\b", re.IGNORECASE)
TTS_PRONUNCIATION_ALIAS_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "trustee_name",
        (
            ("Shri. Sundar Ramakrishnan", "Shree Sundher Ramakrishnan"),
            ("Sri. Sundar Ramakrishnan", "Shree Sundher Ramakrishnan"),
            ("Sundar Ramakrishnan", "Sundher Ramakrishnan"),
            ("Shri.", "Shree"),
            ("Sri.", "Shree"),
        ),
    ),
    (
        "principal_name",
        (
            ("Dr. A. Soundarrajan", "Doctor Ay Soundarrajan"),
            ("A. Soundarrajan", "Ay Soundarrajan"),
            ("Dr.", "Doctor"),
        ),
    ),
)

MAX_TTS_CHARS = 550


def general_talk_chunks(text: str) -> list[str]:
    """Select complete sentences for speech without changing the displayed answer."""
    def limit(name: str, default: int, minimum: int) -> int:
        try:
            return max(minimum, min(default, int(os.environ.get(name, default))))
        except ValueError:
            return default

    sentence_limit = limit('AURA_GENERAL_TALK_MAX_SENTENCES', 4, 1)
    char_limit = limit('AURA_GENERAL_TALK_MAX_CHARS', 650, 100)
    chunk_limit = limit('AURA_CARTESIA_CHUNK_MAX_CHARS', 220, 40)
    clean = sanitize_text_for_tts(strip_for_tts(text)).translate(str.maketrans({
        '\u2014': ', ', '\u2013': ', ', '\u2011': '-', '\u00a0': ' ',
        '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    }))
    clean = re.sub(r'\s+', ' ', clean).strip()
    sentences = [part for part in re.split(r'(?<=[.!?])\s+', clean) if re.search(r'[.!?]["\']?$', part)]
    selected: list[str] = []
    for sentence in sentences[:sentence_limit]:
        sentence = sentence.strip()
        if sum(map(len, selected)) + len(selected) + len(sentence) > char_limit:
            break
        selected.append(sentence)
    chunks: list[str] = []
    for sentence in selected:
        current = ''
        for word in sentence.split():
            if len(word) > chunk_limit:
                return chunks  # Never split a token into an unfinished spoken word.
            if current and len(current) + len(word) + 1 > chunk_limit:
                chunks.append(current)
                current = ''
            current = f'{current} {word}'.strip()
        if current:
            chunks.append(current)
    return chunks


def strip_for_tts(text: str) -> str:
    """Remove all markup, leaving only speakable prose."""
    if not text:
        return ""
    t = _JARVIS_BLOCK_RE.sub(" ", text)
    t = _JARVIS_SELF_RE.sub(" ", t)
    t = _PARTIAL_TAG_RE.sub(" ", t)
    t = _MD_FENCE_RE.sub(" ", t)
    t = _MD_TABLE_RE.sub(" ", t)
    t = _MD_RULE_RE.sub(" ", t)
    t = _MD_HEADER_RE.sub("", t)
    t = _MD_BOLD_RE.sub(r"\1", t)
    t = _MD_ITALIC_RE.sub(r"\1", t)
    t = _MD_INLINE_CODE_RE.sub("", t)
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _EMOJI_RE.sub(" ", t)
    t = _TOOL_ARTIFACT_RE.sub(" ", t)
    t = _ANY_XML_TAG_RE.sub(" ", t)
    t = _EXTRA_PUNCT_RE.sub("", t)
    return _WHITESPACE_RE.sub(" ", t).strip()


def truncate_for_tts(text: str, limit: int = MAX_TTS_CHARS) -> str:
    """Cut at the nearest sentence boundary within *limit* chars."""
    if len(text) <= limit:
        return text
    chunk = text[:limit]
    for sep in (". ", "! ", "? "):
        idx = chunk.rfind(sep)
        if idx > limit // 2:
            return chunk[:idx + 1] + " Full details are on screen."
    return chunk.rstrip() + ". Full details are on screen."


def sanitize_text_for_tts(text: str) -> str:
    """Keep visual A.U.R.A branding out of spoken text."""
    if not text:
        return ""
    t = _AURA_DOTTED_RE.sub("Aura", text)
    t = _AURA_SPACED_RE.sub("Aura", t)
    t = _AURA_WORD_RE.sub("Aura", t)
    return t


def apply_tts_pronunciation_aliases(text: str) -> tuple[str, int]:
    """Return a speak-only version of text with known name pronunciations."""
    if not text:
        return "", 0
    spoken = text
    applied_groups: set[str] = set()
    for group_name, aliases in TTS_PRONUNCIATION_ALIAS_GROUPS:
        for display, speak in aliases:
            if display in spoken:
                spoken = spoken.replace(display, speak)
                applied_groups.add(group_name)
    return spoken, len(applied_groups)


def build_tts_speak_text(display_text: str) -> str:
    """Build the text that may be sent to a TTS provider."""
    return apply_tts_pronunciation_aliases(display_text)[0]


# ── Cancellation machinery ────────────────────────────────────────────────
# Each speak() captures _generation at call time.
# cancel_speaking() increments _generation and kills the running proc.
# Any speak() waiting for the lock (or mid-stream) will see the mismatch
# and abort without playing, so no stale utterances ever queue up.

_tts_lock = asyncio.Lock()
_current_proc: asyncio.subprocess.Process | None = None
_generation: int = 0


async def cancel_speaking() -> None:
    """Interrupt the current TTS utterance; discard any queued speaks."""
    global _generation, _current_proc
    _generation += 1
    p = _current_proc
    if p is not None:
        try:
            p.terminate()
        except Exception:
            pass


# ── Configuration ─────────────────────────────────────────────────────────

def _detect_piper_bin() -> str:
    """Find the piper binary. Arch packages it as 'piper-tts', others as 'piper'."""
    explicit = os.environ.get("JARVIS_PIPER_BIN")
    if explicit:
        return explicit
    for candidate in ("piper-tts", "piper"):
        if shutil.which(candidate):
            return candidate
    return "piper"  # fall back to the conventional name


def _piper_data_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "piper")
    return os.path.expanduser("~/.local/share/piper")


PIPER_BIN      = _detect_piper_bin()
_PIPER_DIR     = _piper_data_dir()
# en_GB-alan-medium is a male British voice — correct for JARVIS.
# en_US-lessac-high is female and must NOT be used as the default.
PIPER_MODEL_EN = os.environ.get(
    "JARVIS_PIPER_MODEL_EN",
    os.path.join(_PIPER_DIR, "en_GB-alan-medium.onnx"),
)
PIPER_MODEL_IT = os.environ.get(
    "JARVIS_PIPER_MODEL_IT",
    os.path.join(_PIPER_DIR, "it_IT-riccardo-x_low.onnx"),
)
PIPER_MODEL_RU = os.environ.get(
    "JARVIS_PIPER_MODEL_RU",
    os.path.join(_PIPER_DIR, "ru_RU-ruslan-medium.onnx"),
)
PIPER_SPEED     = float(os.environ.get("JARVIS_PIPER_SPEED", "0.85"))
EDGE_VOICE      = os.environ.get("JARVIS_EDGE_VOICE",    "en-GB-RyanNeural")
EDGE_VOICE_IT   = os.environ.get("JARVIS_EDGE_VOICE_IT", "it-IT-DiegoNeural")
EDGE_VOICE_RU   = os.environ.get("JARVIS_EDGE_VOICE_RU", "ru-RU-DmitryNeural")


def required_tts_provider() -> str:
    return os.environ.get("AURA_TTS_REQUIRED_PROVIDER", "").strip().lower().replace("-", "_")


def strict_primary_tts_enabled() -> bool:
    return os.environ.get("AURA_TTS_STRICT_PRIMARY", "false").strip().lower() in {"1", "true", "yes", "on"}


def cartesia_only_required() -> bool:
    return required_tts_provider() == "cartesia"


def browser_tts_fallback_allowed() -> bool:
    return os.environ.get("AURA_TTS_ALLOW_BROWSER_FALLBACK", "true").strip().lower() not in {"0", "false", "no", "off"}


def cartesia_tts_timeout_seconds() -> float:
    raw = os.environ.get("AURA_CARTESIA_TTS_TIMEOUT_SECONDS", "5").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 5.0
    return max(1.0, min(value, 30.0))


def cartesia_tts_chunk_timeout_seconds() -> float:
    raw = os.environ.get("AURA_CARTESIA_CHUNK_TIMEOUT_SECONDS", os.environ.get("AURA_CARTESIA_TTS_CHUNK_TIMEOUT_SECONDS", "8")).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 8.0
    return max(1.0, min(value, 30.0))


def _pick_edge_voice(lang: str) -> str:
    """Return the edge-tts voice for the given language code."""
    l = lang.lower()
    if l.startswith("it"):
        return EDGE_VOICE_IT
    if l.startswith("ru"):
        return EDGE_VOICE_RU
    return EDGE_VOICE


def _pick_pcm_player() -> str | None:
    if IS_WINDOWS:
        if shutil.which("ffplay"):
            return "ffplay -nodisp -autoexit -loglevel quiet -f s16le -ar 22050 -ac 1 -"
        if shutil.which("mpv"):
            return "mpv --no-video --really-quiet --demuxer=rawaudio --demuxer-rawaudio-format=s16le --demuxer-rawaudio-rate=22050 --demuxer-rawaudio-channels=1 -"
        return None
    if shutil.which("paplay"):
        return "paplay --raw --rate=22050 --channels=1 --format=s16le"
    if shutil.which("aplay"):
        return "aplay -q -r 22050 -c 1 -f S16_LE -t raw -"
    return None


def _pick_mp3_player() -> tuple[str, list[str]] | None:
    for binary, args in [
        ("mpv",    ["--no-video", "--really-quiet"]),
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet", "-f", "mp3"]),
    ]:
        if shutil.which(binary):
            return binary, args
    return None


def _piper_model(lang: str) -> str:
    l = lang.lower()
    if l.startswith("it"):
        return PIPER_MODEL_IT
    if l.startswith("ru"):
        return PIPER_MODEL_RU
    return PIPER_MODEL_EN


# ── edge-tts engine ───────────────────────────────────────────────────────

async def _speak_edge(text: str, gen: int, lang: str = "en") -> dict[str, Any]:
    try:
        import edge_tts  # type: ignore
    except ImportError:
        return {"ok": False, "error": "edge-tts not installed; pip install edge-tts"}

    player = _pick_mp3_player()
    if player is None:
        return {"ok": False, "error": "no MP3 player found (install mpv or ffplay)"}
    player_bin, player_args = player

    voice = _pick_edge_voice(lang)
    global _current_proc
    try:
        communicate = edge_tts.Communicate(text, voice)

        # Launch player, stream MP3 chunks into its stdin as they arrive
        # so audio starts playing with minimal latency.
        proc = await asyncio.create_subprocess_exec(
            player_bin, *player_args, "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _current_proc = proc
        assert proc.stdin is not None

        async for chunk in communicate.stream():
            if _generation != gen:
                try:
                    proc.terminate()
                except Exception:
                    pass
                await proc.wait()
                _current_proc = None
                return {"ok": True, "skipped": True, "reason": "superseded"}
            if chunk["type"] == "audio":
                try:
                    proc.stdin.write(chunk["data"])
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break

        try:
            proc.stdin.close()
        except Exception:
            pass
        await proc.wait()
        _current_proc = None

    except Exception as exc:
        _current_proc = None
        return {"ok": False, "error": f"edge-tts failed: {exc}"}

    return {"ok": True, "engine": "edge-tts", "voice": voice}


# ── piper engine ──────────────────────────────────────────────────────────

async def _speak_piper(text: str, lang: str, gen: int) -> dict[str, Any]:
    if not shutil.which(PIPER_BIN):
        return {"ok": False, "error": f"{PIPER_BIN!r} not in PATH"}

    model = _piper_model(lang)
    if not os.path.exists(model):
        return {"ok": False, "error": f"piper model not found: {model}"}

    player_cmd = _pick_pcm_player()
    if player_cmd is None:
        if IS_WINDOWS:
            return {"ok": False, "error": "no PCM player found (need ffplay or mpv)"}
        return {"ok": False, "error": "no PCM player found (need paplay or aplay)"}

    piper_args = [PIPER_BIN, "--model", model, "--length-scale", str(PIPER_SPEED), "--output-raw"]
    player_args = shlex.split(player_cmd, posix=not IS_WINDOWS)

    global _current_proc
    try:
        piper_proc = await asyncio.create_subprocess_exec(
            *piper_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        player_proc = await asyncio.create_subprocess_exec(
            *player_args,
            stdin=piper_proc.stdout,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:
        return {"ok": False, "error": f"spawn failed: {exc}"}

    _current_proc = piper_proc
    assert piper_proc.stdin is not None
    try:
        piper_proc.stdin.write(text.encode("utf-8"))
        await piper_proc.stdin.drain()
        piper_proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    _, err = await piper_proc.communicate()
    await player_proc.wait()
    _current_proc = None

    rc = piper_proc.returncode
    if rc not in (0, -15, None):  # -15 = SIGTERM (cancelled gracefully)
        return {"ok": False, "error": f"piper exited {rc}",
                "stderr": err.decode(errors="replace")}
    return {"ok": True, "engine": "piper", "model": model}


# ── Piper pre-warming ────────────────────────────────────────────────────────

_piper_warmed = False
_piper_lock = asyncio.Lock()

async def ensure_piper_loaded() -> None:
    """Pre-warm piper by checking if all models exist.

    This avoids cold-start latency on the first speak() call.
    """
    global _piper_warmed
    if _piper_warmed:
        return
    async with _piper_lock:
        if _piper_warmed:
            return
        # Just verify models exist; actual loading happens on first speak
        for lang, model_path in [
            ("en", PIPER_MODEL_EN),
            ("it", PIPER_MODEL_IT),
            ("ru", PIPER_MODEL_RU),
        ]:
            if not os.path.exists(model_path):
                import logging
                log = logging.getLogger("jarvis.tts")
                log.warning(f"Piper model not found: {model_path}")
        _piper_warmed = True


async def synthesize_fish_audio(
    text: str,
    include_base64: bool = True,
    purpose: str = "voice_turn",
) -> dict[str, Any]:
    """Return Fish Audio browser-playable audio through the diagnostics provider path."""
    from . import diagnostics

    return await diagnostics.synthesize_fish_audio(
        text,
        include_base64=include_base64,
        purpose=purpose,
    )


async def synthesize_cartesia_audio(
    text: str,
    include_base64: bool = True,
    purpose: str = "voice_turn",
    mode: str = "voice_turn",
    force_live: bool = False,
) -> dict[str, Any]:
    """Return Cartesia Sonic browser-playable audio through the shared provider path."""
    from . import diagnostics

    return await diagnostics.synthesize_cartesia_audio(
        text,
        include_base64=include_base64,
        purpose=purpose,
        mode=mode,
        force_live=force_live,
    )


# ── Public API ────────────────────────────────────────────────────────────

async def speak(text: str, lang: str = "en") -> dict[str, Any]:
    """Clean, truncate, synthesise and play *text*.

    Returns immediately (skipped) if cancel_speaking() was called since
    this speak() was queued — generation mismatch detected before and
    during playback.
    """
    gen = _generation
    clean = sanitize_text_for_tts(strip_for_tts(text))
    if not clean:
        return {"ok": True, "skipped": True, "reason": "empty after strip"}
    clean = truncate_for_tts(clean)

    async with _tts_lock:
        if _generation != gen:
            return {"ok": True, "skipped": True, "reason": "superseded"}

        result = await _speak_edge(clean, gen, lang=lang)
        if not result.get("ok") and not result.get("skipped"):
            result = await _speak_piper(clean, lang, gen)
        return result
