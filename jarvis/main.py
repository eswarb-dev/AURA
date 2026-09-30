"""JARVIS — FastAPI backend + WebSocket bridge + wake-word loop.

Run:
    uvicorn jarvis.main:app --host 127.0.0.1 --port 8765

Credentials are loaded from ~/.jarvis/.env on startup (CLAUDE_CODE_OAUTH_TOKEN
for Claude Pro, optionally OPENROUTER_API_KEY for fallback).

The wake-word loop is started in the background on app startup if openwakeword
and a microphone are available; on failure it logs and the UI still works for
text input.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


def _load_dotenv() -> None:
    """Load ~/.jarvis/.env into os.environ (without overriding existing vars).

    Implemented inline so we don't require python-dotenv. Supports simple
    KEY=VALUE lines with optional quoting and `#` comments.
    """
    paths = [Path.home() / ".jarvis" / ".env", Path(".env")]
    for path in paths:
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            continue


# Load env files BEFORE importing modules that read env at import time.
_load_dotenv()


from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import hermes_client as llm
from . import tts
from . import context_manager as ctx_mgr
from . import diagnostics
from . import identity
from . import remote_control
from .stt import get_stt
from .task_manager import TaskManager
from .tools import hypr as hypr_tool

logging.basicConfig(
    level=os.environ.get("JARVIS_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jarvis")

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
DIST_DIR = STATIC_DIR / "dist"
MODELS_DIR = DIST_DIR / "models"
if not MODELS_DIR.exists():
    MODELS_DIR = ROOT / "web" / "public" / "models"
# Personal prompt (gitignored) takes priority; fall back to the committed template.
_PERSONAL_PROMPT = ROOT / "personal info cosmo" / "system_prompt.txt"
SYSTEM_PROMPT_PATH = _PERSONAL_PROMPT if _PERSONAL_PROMPT.exists() else ROOT / "system_prompt.txt"
HISTORY_PATH = Path.home() / ".jarvis" / "history.json"
_MAX_HISTORY = 200  # user/assistant messages to persist across reboots

@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """Startup/shutdown lifecycle. Startup logic lives in ``_on_startup``."""
    await _on_startup()
    try:
        yield
    finally:
        # Persist conversation history on any graceful shutdown path
        # (Ctrl+C, systemd/service stop, reboot signal) — not just the
        # explicit /api/shutdown endpoint — so the next launch resumes here.
        _save_history()


app = FastAPI(title="JARVIS", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# New React/Vite frontend build output (jarvis/web -> jarvis/static/dist).
if (DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="dist-assets")
if MODELS_DIR.exists():
    app.mount("/models", StaticFiles(directory=str(MODELS_DIR)), name="models")

# Enable gzip compression for responses (especially WebSocket messages)
app.add_middleware(GZipMiddleware, minimum_size=1024)

_ONLINE_CHECK_RE = re.compile(
    r"are\s+you\s+(?:online|there|alive|running|up|active|ready)"
    r"|you\s+online"
    r"|status\s+check",
    re.IGNORECASE,
)
WEBUI_URL = "http://127.0.0.1:8765"


# ──────────────────────────────────────────────────────────────────────────
# Shared state
# ──────────────────────────────────────────────────────────────────────────


class Hub:
    """Tracks connected WebSocket clients so the wake-word loop can push to them."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)


hub = Hub()
conversation: list[dict[str, Any]] = []
_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()

# ── Mode state ──────────────────────────────────────────────────────────────
# "default" = general JARVIS (Hermes default profile)
# "wwf"     = work mode (Hermes wwf profile, WWF Crotone project)
# Each mode has its own Hermes session + memory. Switching modes resets the
# local conversation so the two contexts never bleed into each other.
_current_mode: str = "default"

# ── Language tracking ──────────────────────────────────────────────────────
# Updated by STT on each voice input; used for TTS language selection.
_current_lang: str = "en"


def _detect_text_language(text: str) -> str:
    """Heuristic language detection from text characters and common words."""
    # Cyrillic → Russian
    if any('Ѐ' <= c <= 'ӿ' for c in text):
        return "ru"
    # Italian diacritics
    if any(c in 'àèéìíîòóùúâêôûãõ' for c in text.lower()):
        return "it"
    # Common Italian words (covers responses without diacritics)
    _IT_WORDS = frozenset({
        "signore", "ciao", "grazie", "prego", "bene", "come", "stai",
        "sono", "hai", "che", "del", "della", "degli", "delle",
        "questo", "questa", "questi", "queste", "anche", "molto",
        "tutto", "tutti", "tutte", "quando", "dove", "ancora", "sempre",
        "subito", "capito", "perfetto", "confermato", "operativo",
        "completato", "avviato", "pronto", "fatto", "disponibile",
        "ricevo", "perfettamente", "attivo", "sistema", "canale",
    })
    words = set(text.lower().split())
    it_hits = len(words & _IT_WORDS)
    if it_hits >= 2 or (it_hits >= 1 and len(words) <= 8):
        return "it"
    return "en"


def _extract_first_sentence(buf: str) -> tuple[str | None, str]:
    """Extract one complete sentence from *buf*.
    Returns (sentence, remainder) or (None, buf) if none found yet."""
    for sep in ('. ', '! ', '? ', '.\n', '!\n', '?\n'):
        i = buf.find(sep)
        if i >= 0:
            return buf[:i + 1].strip(), buf[i + len(sep):]
    return None, buf


def _create_task(coro) -> asyncio.Task:
    """Create a task and keep a strong reference so GC can't cancel it."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


# ── Spoken acknowledgements ────────────────────────────────────────────────
# JARVIS speaks a short ack the instant a turn starts (masking first-token
# latency) and again after each action that changes state, so the operator
# always hears progress.
_START_ACKS = [
    "On it.", "Working on it.", "Yeah, one sec.", "Got it.", "Yep.", "OK.",
    "Yeah, doing that now.", "Hang on.",
]
# Exact tool names that mutate state → a fitting confirmation phrase.
_ACTION_PHRASES = {
    "whatsapp_send": "Message sent.",
    "file_write": "File saved.",
    "file_delete": "Deleted.",
    "schedule_add": "Scheduled.",
    "schedule_cancel": "Schedule cancelled.",
    "memory_save": "Noted.",
}
# Substrings that indicate a state-changing action (covers MCP/Home-Assistant
# tools whose exact names vary, e.g. light.turn_on, switch.toggle).
_ACTION_SUBSTR = ("turn_on", "turn_off", "toggle", "set_state", "send_message",
                  "press", "open_cover", "close_cover", "set_temperature")


def _start_ack_phrase() -> str:
    import random
    return random.choice(_START_ACKS)


def _action_ack_phrase(name: str) -> str | None:
    """Short confirmation for a state-changing tool, or None to stay silent."""
    name = name or ""
    if name in _ACTION_PHRASES:
        return _ACTION_PHRASES[name]
    if any(s in name for s in _ACTION_SUBSTR):
        return "Done."
    return None


def _memory_context_block(limit: int = 8) -> str:
    """Read the most-recently-updated memory entries directly (no tool round trip)."""
    try:
        from .tools import memory
        entries = memory.recent_sync(limit)
    except Exception as exc:
        log.debug("memory context load failed: %s", exc)
        return ""
    if not entries:
        return ""
    lines = []
    for e in entries:
        value = e.get("value")
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"- {e.get('key')}: {value}")
    return "\n\nKNOWN CONTEXT FROM MEMORY (already loaded, do not call memory_recall again for this):\n" + "\n".join(lines)


def _load_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8") + _memory_context_block()
    log.warning("system_prompt.txt not found, using fallback")
    return "You are Cosmo, eli6's direct, truthful assistant." + _memory_context_block()


def _save_history() -> None:
    msgs = [m for m in conversation if m.get("role") in ("user", "assistant")]
    to_save = msgs[-_MAX_HISTORY:]
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(
            json.dumps(to_save, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("history save failed: %s", exc)


def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return [m for m in data if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    except Exception as exc:
        log.warning("history load failed: %s", exc)
        return []


def _reset_conversation() -> None:
    conversation.clear()
    conversation.append({"role": "system", "content": _load_system_prompt()})


_reset_conversation()


# ── Batch history saving (debounced) ───────────────────────────────────────
# Instead of writing history to disk after every turn, batch writes with a 5s delay.
# If another turn starts before the timer fires, cancel and restart the timer.
_history_save_task: asyncio.Task | None = None
_history_save_lock = asyncio.Lock()
_current_turn_task: asyncio.Task | None = None

async def _debounced_save_history() -> None:
    """Save history to disk, but only if no write has happened in 5 seconds."""
    global _history_save_task
    
    async with _history_save_lock:
        # Cancel any pending save
        if _history_save_task is not None:
            _history_save_task.cancel()
            try:
                await _history_save_task
            except asyncio.CancelledError:
                pass
        
        # Schedule a new save 5 seconds from now
        async def _save_later() -> None:
            await asyncio.sleep(5)
            _save_history()
        
        _history_save_task = _create_task(_save_later())


# ──────────────────────────────────────────────────────────────────────────
# Core: handle one user turn
# ──────────────────────────────────────────────────────────────────────────


async def handle_user_turn(text: str, *, voice: bool, send: Any) -> None:
    """Run a full LLM + tool turn, streaming events to `send` (a callable
    awaiting a dict) and broadcasting to all other clients."""
    global _current_lang, _current_turn_task
    # Barge-in: a new request supersedes any still-running turn. Cancel the old
    # one so we don't get two overlapping responses/voices (also fixes the
    # concurrent-turn race where _current_turn_task was silently overwritten).
    _prev_turn = _current_turn_task
    if _prev_turn is not None and not _prev_turn.done() and _prev_turn is not asyncio.current_task():
        _prev_turn.cancel()
    _current_turn_task = asyncio.current_task()

    if not text.strip():
        return
    raw_voice_text = text.strip()
    voice_normalization: dict[str, Any] | None = None
    if voice:
        from .voice_intents import normalize_aura_transcript
        voice_normalization = normalize_aura_transcript(raw_voice_text)
        text = str(voice_normalization.get("normalized") or raw_voice_text).strip()
        if not voice_normalization.get("wake_detected") and text in {"uh", "um", "hmm", "..."}:
            return
        if not text:
            return

    # Stop any ongoing speech from the previous turn immediately
    await tts.cancel_speaking()

    # Determine language — for voice input, _current_lang was already updated by the
    # STT caller; for typed input we do a quick heuristic scan.
    detected_lang = _current_lang if voice else _detect_text_language(text)

    # Fast-path: "are you online" — skip LLM, speak greeting, open WebUI
    if _ONLINE_CHECK_RE.search(text):
        reply = "Online and ready, sir. All systems nominal."
        await _emit({
            "type": "transcript",
            "text": text,
            "raw_text": raw_voice_text,
            "normalized_text": text,
            "normalization": voice_normalization,
            "voice": voice,
        }, send)
        await _emit({"type": "turn_start", "voice": voice}, send)
        await _emit({"type": "turn_end", "final_text": reply, "elapsed": 0.0}, send)
        await _emit({"type": "speaking", "state": "start"}, send)
        await tts.speak(reply, lang=detected_lang)
        await _emit({"type": "speaking", "state": "end"}, send)
        if os.name == "nt":
            os.startfile(WEBUI_URL)  # noqa: S606
        else:
            await hypr_tool.dispatch("exec", f"firefox {WEBUI_URL}")
        return

    # Fast-path: voice intents ("create a new project called X",
    # "switch to project finance", "list projects"). Deterministic, no LLM.
    from .voice_intents import classify, handle_voice_intent
    intent = classify(text)
    if intent is not None:
        intent_name, groups = intent
        handled = await handle_voice_intent(intent_name, groups, send=send, broadcast=hub.broadcast)
        if handled:
            await _emit({
                "type": "transcript",
                "text": text,
                "raw_text": raw_voice_text,
                "normalized_text": text,
                "normalization": voice_normalization,
                "voice": voice,
            }, send)
            await _emit({"type": "turn_start", "voice": voice}, send)
            await _emit({"type": "turn_end", "final_text": "(voice command handled)", "elapsed": 0.0}, send)
            return

    # Fast-path: typed slash commands ("/research foo", "/review bar").
    # The subagent prefix gets prepended to the prompt before going to Hermes.
    from .subagents import slash_to_subagent
    subagent, stripped = slash_to_subagent(text)
    if subagent is not None and stripped:
        text = f"{subagent.system_prefix}\n\n---\n\n{stripped}"
        # fall through to the normal LLM turn with the rewritten text

    user_message = f"[VOICE] {text}" if voice else text
    await _emit({
        "type": "transcript",
        "text": text,
        "raw_text": raw_voice_text if voice else text,
        "normalized_text": text,
        "normalization": voice_normalization,
        "voice": voice,
    }, send)

    async with _lock:
        conversation.append({"role": "user", "content": user_message})
        # Trim in-memory conversation if it's grown too large
        removed = ctx_mgr.trim_conversation_in_place(conversation, max_messages=100)
        if removed:
            log.info("trimmed %d old messages from conversation before LLM call", removed)
        msgs_snapshot = list(conversation)

    task_mgr = TaskManager()

    # ── Sentence-streaming TTS state ──────────────────────────────────────
    # As response_delta events arrive, we buffer text and speak each complete
    # sentence immediately — so speech starts during generation, not after.
    _tts_buf = ""
    _sentence_tasks: list[asyncio.Task] = []
    _speaking_started = False

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal _tts_buf, _speaking_started
        # task_update events (from the Hermes todo tracker) pass through
        # directly — the frontend renders them as the live task tracker.
        if event.get("type") == "task_update":
            await _emit(event, send)
            return
        # Forward raw event to UI
        await _emit({"type": "llm_event", "event": event}, send)

        # Per-action voice confirmation for state-changing tools (sent, saved,
        # deleted, toggled, …). Reads are not announced.
        if event.get("type") == "tool_result":
            res = event.get("result")
            ok = True
            if isinstance(res, dict):
                ok = res.get("ok") is not False and not res.get("error")
            phrase = _action_ack_phrase(event.get("name") or "")
            if phrase and ok:
                if not _speaking_started:
                    _speaking_started = True
                    await _emit({"type": "speaking", "state": "start"}, send)
                _create_task(tts.speak(phrase, lang=detected_lang))

        # Let task manager react
        snap = task_mgr.handle_event(event)
        if snap is not None:
            await _emit({"type": "task_update", **snap}, send)
            if snap.get("kind") == "task_plan":
                goal = snap.get("plan", {}).get("goal", "")
                phrase = f"Understood. Initiating task: {goal}" if goal else "Understood. Task initiated."
                await _emit({"type": "speaking", "state": "start"}, send)
                await tts.speak(phrase, lang=detected_lang)
                await _emit({"type": "speaking", "state": "end"}, send)
            elif snap.get("kind") == "step":
                plan = snap.get("plan", {})
                step = plan.get("changed_step", {})
                status = step.get("status", "")
                total = plan.get("total_steps", 0)
                if status == "running" and total >= 3:
                    n = step.get("n", "?")
                    label = step.get("label", "")
                    phrase = f"Step {n}: {label}." if label else f"Running step {n}."
                    await _emit({"type": "speaking", "state": "start"}, send)
                    await tts.speak(phrase, lang=detected_lang)
                    await _emit({"type": "speaking", "state": "end"}, send)
                elif status == "error":
                    n = step.get("n", "?")
                    reason = step.get("reason", "")
                    phrase = f"Step {n} failed. {reason}" if reason else f"Step {n} failed."
                    await _emit({"type": "speaking", "state": "start"}, send)
                    await tts.speak(phrase, lang=detected_lang)
                    await _emit({"type": "speaking", "state": "end"}, send)

        # Sentence-streaming: queue each complete sentence for TTS as it arrives
        if event.get("type") == "response_delta":
            _tts_buf += event.get("text", "")
            while True:
                sentence, rest = _extract_first_sentence(_tts_buf)
                if sentence is None:
                    break
                _tts_buf = rest
                clean = tts.strip_for_tts(sentence).strip()
                # Only speak if meaningful length (avoids single-char artefacts)
                if clean and len(clean) >= 12:
                    if not _speaking_started:
                        _speaking_started = True
                        await _emit({"type": "speaking", "state": "start"}, send)
                    t = _create_task(tts.speak(clean, lang=detected_lang))
                    _sentence_tasks.append(t)

    started = time.time()
    await _emit({"type": "turn_start", "voice": voice}, send)

    # Brief spoken acknowledgement up front so the operator immediately hears
    # JARVIS engage while the model produces its first tokens.
    _speaking_started = True
    await _emit({"type": "speaking", "state": "start"}, send)
    _sentence_tasks.append(_create_task(tts.speak(_start_ack_phrase(), lang=detected_lang)))

    try:
        result = await llm.stream_chat(msgs_snapshot, on_event, mode=_current_mode)
    except Exception as exc:
        log.exception("LLM stream failed")
        await _emit({"type": "error", "message": f"LLM stream failed: {exc}"}, send)
        return

    async with _lock:
        # Replace conversation with the (longer) one returned from the LLM loop
        if len(result.get("messages", [])) >= len(conversation):
            conversation[:] = result["messages"]
    _create_task(_debounced_save_history())

    final_text = (result.get("final_text") or "").strip()
    elapsed = round(time.time() - started, 2)
    await _emit({
        "type": "turn_end",
        "final_text": final_text,
        "elapsed": elapsed,
    }, send)

    # Speak any remaining buffer fragment not yet queued
    remainder = tts.strip_for_tts(_tts_buf).strip()
    if remainder and len(remainder) >= 4:
        if not _speaking_started:
            _speaking_started = True
            await _emit({"type": "speaking", "state": "start"}, send)
        t = _create_task(tts.speak(remainder, lang=detected_lang))
        _sentence_tasks.append(t)

    if _sentence_tasks:
        # Wait for all sentence TTS tasks to finish, then signal end
        await asyncio.gather(*_sentence_tasks, return_exceptions=True)
        await _emit({"type": "speaking", "state": "end"}, send)
    elif final_text:
        # Fallback: nothing was streamed sentence-by-sentence (very short response)
        await _emit({"type": "speaking", "state": "start"}, send)
        speak_result = await tts.speak(final_text, lang=detected_lang)
        await _emit({"type": "speaking", "state": "end", "result": speak_result}, send)


async def _emit(msg: dict[str, Any], send: Any) -> None:
    """Broadcast a message to all connected clients.

    The originating WebSocket is already in ``hub.clients``, so a single
    broadcast covers it — sending directly via ``send`` would double-deliver.
    The ``send`` parameter is kept for API compatibility with callers (e.g.
    the wake-word loop) that don't have a WS connection of their own.
    """
    await hub.broadcast(msg)


# ──────────────────────────────────────────────────────────────────────────
# HTTP routes
# ──────────────────────────────────────────────────────────────────────────


@app.get("/")
async def index() -> FileResponse:
    dist_index = DIST_DIR / "index.html"
    if dist_index.exists():
        return FileResponse(str(dist_index))
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/diagnostics")
async def diagnostics_index() -> FileResponse:
    return await index()


@app.get("/mobile")
async def mobile_index() -> FileResponse:
    return await index()


@app.get("/mobile-control")
async def mobile_control_index() -> FileResponse:
    return await index()


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    return FileResponse(str(DIST_DIR / "favicon.svg"))


@app.get("/icons.svg")
async def icons() -> FileResponse:
    return FileResponse(str(DIST_DIR / "icons.svg"))


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "backend": llm._pick_backend(),
        "model": llm.get_active_model(),
        "mode": _current_mode,
        "clients": len(hub.clients),
        "conversation_length": len(conversation),
    }


from . import capabilities


@app.get("/api/capabilities")
@app.get("/api/diagnostics/capabilities")
async def api_capabilities() -> dict[str, Any]:
    return capabilities.get_capability_state()


def _remote_http_error(exc: remote_control.RemoteControlError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


@app.get("/api/remote-control/status")
async def api_remote_control_status(token: str | None = None) -> dict[str, Any]:
    return remote_control.status(token)


@app.post("/api/remote-control/send")
async def api_remote_control_send(body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return remote_control.send(body)
    except remote_control.RemoteControlError as exc:
        raise _remote_http_error(exc) from exc


@app.get("/api/remote-control/poll")
async def api_remote_control_poll(request: Request, client: str | None = None, token: str | None = None) -> dict[str, Any]:
    try:
        return remote_control.poll(client, token, loopback_request=_is_loopback_request(request))
    except remote_control.RemoteControlError as exc:
        raise _remote_http_error(exc) from exc


@app.post("/api/remote-control/ack")
async def api_remote_control_ack(request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return remote_control.ack(body, loopback_request=_is_loopback_request(request))
    except remote_control.RemoteControlError as exc:
        raise _remote_http_error(exc) from exc


@app.get("/api/diagnostics/providers")
async def api_diagnostics_providers() -> dict[str, Any]:
    return diagnostics.provider_status()


@app.get("/api/diagnostics/tts-config")
async def api_diagnostics_tts_config() -> dict[str, Any]:
    voice_id = diagnostics._elevenlabs_voice_id()
    tts_status = diagnostics.tts_provider_status()
    return {
        "ok": True,
        "provider": tts_status["priority"][0] if tts_status["priority"] else "browser_speech",
        "preferred_tts_provider": tts_status["priority"][0] if tts_status["priority"] else "browser_speech",
        "fallback_order": tts_status["priority"],
        "fish_audio": tts_status["fish_audio"],
        "elevenlabs": tts_status["elevenlabs"],
        "voice_id_present": bool(voice_id),
        "voice_id_masked": diagnostics._mask_voice_id(voice_id),
        "voice_id_source": diagnostics._elevenlabs_voice_id_source(),
        "same_for_agent_voice_turn": True,
    }


@app.get("/api/diagnostics/tts-providers")
async def api_diagnostics_tts_providers() -> dict[str, Any]:
    return diagnostics.tts_provider_status()


@app.get("/api/diagnostics/tts-strict-status")
async def api_diagnostics_tts_strict_status() -> dict[str, Any]:
    return diagnostics.tts_strict_status()


@app.get("/api/diagnostics/vision-model-assets")
async def api_diagnostics_vision_model_assets() -> dict[str, Any]:
    face_api_dir = MODELS_DIR / "face-api"
    files: list[str] = []
    if face_api_dir.exists():
        files = sorted(
            str(path.relative_to(MODELS_DIR)).replace("\\", "/")
            for path in face_api_dir.rglob("*")
            if path.is_file()
        )
    return {
        "ok": MODELS_DIR.exists(),
        "models_dir": str(MODELS_DIR),
        "exists": MODELS_DIR.exists(),
        "face_api_dir_exists": face_api_dir.exists(),
        "files": files,
        "test_url": "/models/face-api/tiny_face_detector_model-weights_manifest.json",
    }


@app.post("/api/diagnostics/openrouter")
async def api_diagnostics_openrouter() -> dict[str, Any]:
    return await diagnostics.test_openrouter()


@app.post("/api/diagnostics/groq")
async def api_diagnostics_groq() -> dict[str, Any]:
    return await diagnostics.test_groq()


@app.post("/api/diagnostics/provider-chain")
async def api_diagnostics_provider_chain() -> dict[str, Any]:
    return await diagnostics.test_provider_chain()


@app.get("/api/diagnostics/elevenlabs-network")
@app.post("/api/diagnostics/elevenlabs-network")
async def api_diagnostics_elevenlabs_network() -> dict[str, Any]:
    return diagnostics.test_elevenlabs_network()


@app.post("/api/diagnostics/elevenlabs")
async def api_diagnostics_elevenlabs(body: dict[str, Any] | None = None) -> dict[str, Any]:
    include_audio = bool((body or {}).get("include_audio"))
    return await diagnostics.test_elevenlabs(include_audio=include_audio)


@app.post("/api/diagnostics/fish-audio")
async def api_diagnostics_fish_audio(body: dict[str, Any] | None = None) -> dict[str, Any]:
    include_audio = bool((body or {}).get("include_audio") or (body or {}).get("include_base64"))
    return await diagnostics.test_fish_audio(include_audio=include_audio)


@app.post("/api/diagnostics/cartesia")
async def api_diagnostics_cartesia(body: dict[str, Any] | None = None) -> dict[str, Any]:
    include_audio = bool((body or {}).get("include_audio") or (body or {}).get("include_base64"))
    text = str((body or {}).get("text") or "Awake, Sir.")
    return await diagnostics.test_cartesia(include_audio=include_audio, text=text)


@app.post("/api/diagnostics/cartesia-network")
async def api_diagnostics_cartesia_network() -> dict[str, Any]:
    return await diagnostics.test_cartesia_network()


@app.post("/api/diagnostics/cartesia/reset-state")
async def api_diagnostics_cartesia_reset_state(body: dict[str, Any] | None = None) -> dict[str, Any]:
    return diagnostics.reset_cartesia_state(clear_cache=bool((body or {}).get("clear_cache")))


@app.post("/api/diagnostics/fish-audio-network")
async def api_diagnostics_fish_audio_network() -> dict[str, Any]:
    return await diagnostics.test_fish_audio_network()


@app.post("/api/diagnostics/tts-chain")
async def api_diagnostics_tts_chain(body: dict[str, Any] | None = None) -> dict[str, Any]:
    include_audio = bool((body or {}).get("include_audio") or (body or {}).get("include_base64"))
    return await diagnostics.test_tts_chain(include_audio=include_audio)


@app.post("/api/diagnostics/tts-preflight")
async def api_diagnostics_tts_preflight() -> dict[str, Any]:
    return await diagnostics.tts_preflight()


@app.post("/api/diagnostics/voice-loop")
async def api_diagnostics_voice_loop(body: dict[str, Any]) -> dict[str, Any]:
    transcript = str(body.get("transcript", "")).strip()
    if not transcript:
        return {"ok": False, "error": "missing_transcript", "message": "transcript is required"}
    source = str(body.get("source", "browser_stt")).strip() or "browser_stt"
    started_at = body.get("startedAt")
    return await diagnostics.voice_loop(transcript, source, str(started_at) if started_at else None)


@app.post("/api/diagnostics/voice-bench-loop")
async def api_diagnostics_voice_bench_loop(body: dict[str, Any]) -> dict[str, Any]:
    return await diagnostics.voice_bench_loop(body)


@app.get("/api/tools/status")
async def api_tools_status() -> dict[str, Any]:
    from .tools import tool_router
    return tool_router.status()


@app.post("/api/tools/execute")
async def api_tools_execute(body: dict[str, Any]) -> dict[str, Any]:
    from .tools import tool_router
    return tool_router.execute_aura_tool(
        str((body or {}).get("tool_name") or ""),
        str((body or {}).get("action") or ""),
        (body or {}).get("params") if isinstance((body or {}).get("params"), dict) else {},
        (body or {}).get("context") if isinstance((body or {}).get("context"), dict) else {},
    )


@app.get("/api/tools/audit-log")
async def api_tools_audit_log(limit: int = 50) -> dict[str, Any]:
    from .tools import tool_router
    return tool_router.read_audit_log(limit=limit)


@app.post("/api/agent/voice-turn")
async def api_agent_voice_turn(body: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(body or {})
        payload["conversation_mode"] = str(payload.get("conversation_mode") or "main_orb")
        payload["recent_turns"] = payload.get("recent_turns") if isinstance(payload.get("recent_turns"), list) else []
        payload["identity_context"] = payload.get("identity_context") if isinstance(payload.get("identity_context"), dict) else {}
        payload["vision_context"] = payload.get("vision_context") if isinstance(payload.get("vision_context"), dict) else {}
        payload["vision_context"]["tracks"] = payload["vision_context"].get("tracks") if isinstance(payload["vision_context"].get("tracks"), list) else []
        payload["actions"] = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        payload["effective_address"] = str(payload.get("effective_address") or "Sir").strip() or "Sir"
        payload["frontend_kind"] = str(payload.get("frontend_kind") or "normal_chat").strip() or "normal_chat"
        payload["auto_tts"] = bool(payload.get("auto_tts", True))
        payload["aura_awake"] = bool(payload.get("aura_awake", False))
        payload["wake_required"] = bool(payload.get("wake_required", True))
        return await diagnostics.voice_bench_loop(payload)
    except Exception as exc:
        log.exception("[AURA VOICE TURN] recovered from internal error")
        error_preview = f"{type(exc).__name__}: {str(exc)}"[:180]
        return {
            "ok": True,
            "response_text": "I hit a local runtime error, Sir.",
            "spoken_text": "I hit a local runtime error, Sir.",
            "actions": [],
            "providers": {
                "brain": "local_error_recovery",
                "brain_model": "voice_turn_recovery",
                "fallback_used": True,
                "fallback_reason": "voice_turn_internal_error",
                "tts": "browser_speech",
            },
            "audio": {
                "provider": "browser_speech",
                "mime_type": None,
                "audio_base64": None,
                "audio_bytes": 0,
                "fallback_reason": "voice_turn_internal_error",
            },
            "debug": {
                "intent_detected": "voice_turn_internal_error",
                "provider_route": "local_error_recovery",
                "failure_class": "local_tool_failed",
                "failure_origin": "transport",
                "frontend_kind": (body or {}).get("frontend_kind"),
                "tool_detected": False,
                "tool_name": None,
                "actions_returned": 0,
                "error_type": "voice_turn_internal_error",
                "error_preview": error_preview,
            },
            "tts_debug": {
                "requested_auto_tts": bool((body or {}).get("auto_tts", True)),
                "final_provider": "browser_speech",
                "browser_speech_fallback_used": True,
                "fallback_reason": "voice_turn_internal_error",
                "audio_base64_present": False,
                "audio_bytes": 0,
            },
            "fallback_reason": "voice_turn_internal_error",
            "error": "voice_turn_internal_error",
            "tts_error_type": "voice_turn_internal_error",
        }


@app.get("/api/identity/profiles")
async def api_identity_profiles() -> dict[str, Any]:
    try:
        return identity.profiles()
    except Exception:
        log.exception("[AURA IDENTITY] profiles route failed")
        return identity.safe_identity_error("identity_profile_load_failed")


@app.post("/api/identity/enroll-face")
async def api_identity_enroll_face(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.enroll_face(body)
    except Exception:
        log.exception("[AURA IDENTITY] enroll-face route failed")
        return identity.safe_identity_error("identity_face_enroll_failed")


@app.post("/api/identity/verify-face")
async def api_identity_verify_face(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.verify_face(body)
    except Exception:
        log.exception("[AURA IDENTITY] verify-face route failed")
        return identity.safe_identity_error("identity_face_verify_failed")


@app.post("/api/identity/clear-face")
async def api_identity_clear_face(body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return identity.clear_face(body)
    except Exception:
        log.exception("[AURA IDENTITY] clear-face route failed")
        return identity.safe_identity_error("identity_face_clear_failed")


@app.post("/api/identity/clear-profile")
async def api_identity_clear_profile(body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return identity.clear_face(body)
    except Exception:
        log.exception("[AURA IDENTITY] clear-profile route failed")
        return identity.safe_identity_error("identity_profile_clear_failed")


@app.post("/api/identity/enroll-voice")
async def api_identity_enroll_voice(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.enroll_voice(body)
    except Exception:
        log.exception("[AURA IDENTITY] enroll-voice route failed")
        return identity.safe_identity_error("identity_voice_enroll_failed")


@app.post("/api/identity/verify-voice")
async def api_identity_verify_voice(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.verify_voice(body)
    except Exception:
        log.exception("[AURA IDENTITY] verify-voice route failed")
        return identity.safe_identity_error("identity_voice_verify_failed")


@app.post("/api/identity/fusion-check")
async def api_identity_fusion_check(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.fusion_check(body)
    except Exception:
        log.exception("[AURA IDENTITY] fusion-check route failed")
        return identity.safe_identity_error("identity_fusion_check_failed")


@app.post("/api/identity/test-effective-address")
async def api_identity_test_effective_address(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.test_effective_address(body or {})
    except Exception:
        log.exception("[AURA IDENTITY] test-effective-address route failed")
        return identity.safe_identity_error("identity_effective_address_test_failed")


@app.post("/api/identity/event")
async def api_identity_event(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return identity.event(body)
    except Exception:
        log.exception("[AURA IDENTITY] event route failed")
        return identity.safe_identity_error("identity_event_failed")


@app.get("/api/diagnostics/identity-status")
async def api_diagnostics_identity_status() -> dict[str, Any]:
    try:
        return identity.status()
    except Exception:
        log.exception("[AURA IDENTITY] identity-status route failed")
        return identity.safe_identity_error("identity_status_failed")


@app.get("/api/diagnostics/identity-storage")
async def api_diagnostics_identity_storage() -> dict[str, Any]:
    try:
        return identity.storage_status()
    except Exception:
        log.exception("[AURA IDENTITY] identity-storage route failed")
        return identity.safe_identity_error("identity_storage_status_failed")


@app.get("/api/diagnostics/voice-bench-memory")
async def api_diagnostics_voice_bench_memory() -> dict[str, Any]:
    return diagnostics.export_voice_bench_memory()


@app.post("/api/diagnostics/voice-bench-memory/clear")
async def api_diagnostics_voice_bench_memory_clear() -> dict[str, Any]:
    return diagnostics.clear_voice_bench_memory()


@app.get("/api/diagnostics/latency/latest")
async def api_diagnostics_latency_latest() -> dict[str, Any]:
    return diagnostics.latest_latency()


@app.post("/api/diagnostics/export")
async def api_diagnostics_export(body: dict[str, Any]) -> dict[str, Any]:
    return diagnostics.export_report(body)


@app.post("/api/tts/speak")
async def api_tts_speak(body: dict[str, Any]) -> dict[str, Any]:
    text = tts.truncate_for_tts(tts.strip_for_tts(str(body.get("text", ""))).strip())
    tts_text, _pronunciation_alias_count = tts.apply_tts_pronunciation_aliases(text)
    if "Sundar Ramakrishnan" in text and "Sundher Ramakrishnan" in tts_text:
        logging.getLogger("jarvis.tts").info(
            "[AURA TTS PRONUNCIATION] alias_applied target=trustee_sundar_ramakrishnan variant=sundher_no_long_a",
        )
    if "A. Soundarrajan" in text and "Ay Soundarrajan" in tts_text:
        logging.getLogger("jarvis.tts").info(
            "[AURA TTS PRONUNCIATION] alias_applied target=principal_a_soundarrajan variant=smooth_initial_no_pause",
        )
    text = tts_text
    provider = str(body.get("provider", "chain")).strip().lower().replace("-", "_") or "chain"
    include_audio = bool(body.get("include_audio", True))
    timeout_seconds = body.get("timeout_seconds")
    if not text:
        return {
            "ok": False,
            "provider": provider,
            "error_type": "empty_text",
            "message": "No speakable response text was provided.",
        }
    if provider in ("chain", "auto", "default"):
        result = await diagnostics.synthesize_tts_chain(text, include_audio=include_audio, purpose="api_tts_speak")
    elif provider == "cartesia":
        try:
            timeout = float(timeout_seconds) if timeout_seconds is not None else diagnostics._cartesia_tts_chunk_timeout_seconds()
        except (TypeError, ValueError):
            timeout = diagnostics._cartesia_tts_chunk_timeout_seconds()
        try:
            result = await asyncio.wait_for(
                diagnostics.synthesize_cartesia_audio(text, include_base64=include_audio, purpose="api_tts_speak", mode="voice_turn", force_live=True),
                timeout=max(1.0, min(timeout, 30.0)),
            )
        except asyncio.TimeoutError:
            result = {
                "ok": False,
                "provider": "cartesia",
                "error_type": "cartesia_timeout",
                "message": "Cartesia TTS timed out.",
                "audio_bytes": 0,
                "audio_base64": None,
                "attempts": [{"provider": "cartesia", "attempted": True, "success": False, "error_type": "cartesia_timeout", "audio_bytes": 0}],
            }
    elif provider == "fish_audio":
        result = await diagnostics.synthesize_fish_audio(text, include_base64=include_audio, purpose="api_tts_speak")
    elif provider == "elevenlabs":
        result = await diagnostics.synthesize_elevenlabs(text, include_audio=include_audio)
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "provider": provider,
                "error_type": "unsupported_provider",
                "message": "Supported TTS providers are auto, chain, cartesia, fish_audio, and elevenlabs.",
            },
        )
    if not result.get("ok"):
        return {
            "ok": False,
            "provider": result.get("provider") or provider,
            "error_type": result.get("error_type") or result.get("error") or "tts_chain_audio_missing",
            "message": result.get("message") or "Cloud TTS failed.",
            "voice_id_present": bool(result.get("voice_id_present")),
            "reference_id_present": bool(result.get("reference_id_present")),
            "latency_ms": result.get("latency_ms"),
            "selected_client": result.get("selected_client"),
            "attempts": result.get("tts_provider_attempts", result.get("attempts", [])),
            "tts_debug": result.get("tts_debug", {}),
        }
    return {
        "ok": True,
        "provider": result.get("provider") or provider,
        "audio_base64": result.get("audio_base64") if include_audio else None,
        "mime_type": result.get("mime_type") or "audio/mpeg",
        "audio_bytes": result.get("audio_bytes"),
        "latency_ms": result.get("latency_ms"),
        "voice_id_present": bool(result.get("voice_id_present")),
        "reference_id_present": bool(result.get("reference_id_present")),
        "selected_client": result.get("selected_client"),
        "attempts": result.get("tts_provider_attempts", result.get("attempts", [])),
        "tts_debug": result.get("tts_debug", {}),
    }


@app.post("/api/speak")
async def speak_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    """Fire-and-forget TTS endpoint for external scripts (e.g. Rofi launcher)."""
    text = str(body.get("text", "")).strip()
    lang = str(body.get("lang", "en"))
    if not text:
        return {"ok": False, "error": "text is required"}

    async def _run() -> None:
        await hub.broadcast({"type": "speaking", "state": "start"})
        await tts.speak(text, lang=lang)
        await hub.broadcast({"type": "speaking", "state": "end"})

    _create_task(_run())
    return {"ok": True, "text": text}


@app.post("/reset")
async def reset() -> dict[str, Any]:
    async with _lock:
        _reset_conversation()
    llm.reset_session()
    await tts.cancel_speaking()
    try:
        HISTORY_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    await hub.broadcast({"type": "reset"})
    return {"ok": True}


@app.get("/api/models")
async def api_models() -> dict[str, Any]:
    return {
        "ok": True,
        "backend": llm._pick_backend(),
        "active": llm.get_active_model(),
        "models": llm.get_models(),
    }


@app.post("/api/model")
async def api_set_model(body: dict[str, Any]) -> dict[str, Any]:
    model_id = str(body.get("model", "")).strip()
    ok = await llm.set_model(model_id)
    if not ok:
        return {"ok": False, "error": f"unknown model: {model_id}"}
    await hub.broadcast({"type": "model_changed", "model": model_id, "backend": "hermes"})
    return {"ok": True, "model": model_id}


@app.get("/api/mode")
async def api_get_mode() -> dict[str, Any]:
    from . import hermes_client
    modes = ["default"] + sorted(p for p in hermes_client._SESSION_IDS if p not in ("default", "eli6"))
    return {"ok": True, "mode": _current_mode, "modes": modes}


@app.post("/api/mode")
async def api_set_mode(body: dict[str, Any]) -> dict[str, Any]:
    """Switch between normal mode and a project work mode.

    Each mode talks to a different Hermes profile (separate memory, session,
    SOUL.md). The local conversation is cleared on switch so contexts never
    bleed across modes.
    """
    global _current_mode
    from . import hermes_client
    mode = str(body.get("mode", "")).strip()
    if mode not in hermes_client._SESSION_IDS:
        return {"ok": False, "error": f"unknown mode: {mode}"}
    if mode == _current_mode:
        return {"ok": True, "mode": mode}
    _current_mode = mode
    async with _lock:
        _reset_conversation()
    await tts.cancel_speaking()
    await hub.broadcast({"type": "mode_changed", "mode": mode})
    return {"ok": True, "mode": mode}


@app.post("/api/shutdown")
async def api_shutdown() -> dict[str, Any]:
    """Gracefully shut down the entire JARVIS process."""
    global _current_turn_task
    if _current_turn_task and not _current_turn_task.done():
        _current_turn_task.cancel()
    await tts.cancel_speaking()
    _save_history()
    await hub.broadcast({"type": "shutdown"})

    async def _do_shutdown() -> None:
        await asyncio.sleep(0.4)
        if os.name == "nt":
            os._exit(0)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    _create_task(_do_shutdown())
    return {"ok": True}


@app.post("/api/stop")
async def api_stop() -> dict[str, Any]:
    global _current_turn_task
    cancelled = False
    if _current_turn_task and not _current_turn_task.done():
        _current_turn_task.cancel()
        cancelled = True
    await llm.stop_run()
    await tts.cancel_speaking()
    await hub.broadcast({"type": "stopped"})
    return {"ok": True, "cancelled": cancelled}


@app.get("/api/memory")
async def api_memory(limit: int = 200) -> dict[str, Any]:
    """Read-only browse of long-term memory (~/.jarvis/memory.db)."""
    from .tools import memory

    return await memory.list_memories(limit=limit)


@app.post("/api/memory")
async def api_memory_save(body: dict[str, Any]) -> dict[str, Any]:
    """Create or update a long-term memory entry."""
    from .tools import memory

    key = str(body.get("key", "")).strip()
    value = body.get("value")
    tags = body.get("tags") or []
    if not key:
        return {"ok": False, "error": "key is required"}
    await memory.save(key, value, tags)
    return {"ok": True}


@app.delete("/api/memory/{key}")
async def api_memory_delete(key: str) -> dict[str, Any]:
    """Delete a long-term memory entry by key."""
    from .tools import memory

    await memory.delete(key)
    return {"ok": True}


@app.get("/api/memory/status")
async def api_persistent_memory_status() -> dict[str, Any]:
    from .memory import get_memory_manager

    return get_memory_manager().memory_status()


@app.get("/api/memory/context-preview")
async def api_persistent_memory_context_preview(conversation_id: str | None = None, query: str | None = None) -> dict[str, Any]:
    from .memory import get_memory_manager

    return get_memory_manager().context_preview(conversation_id, query)


@app.get("/api/memory/facts")
async def api_persistent_memory_facts(limit: int = 50) -> dict[str, Any]:
    from .memory import get_memory_manager

    return get_memory_manager().list_facts(limit=limit)


@app.post("/api/memory/facts")
async def api_persistent_memory_fact_create(body: dict[str, Any]) -> dict[str, Any]:
    from .memory import get_memory_manager

    fact = str((body or {}).get("fact") or "").strip()
    if not fact:
        return {"ok": False, "error": "fact is required"}
    category = str((body or {}).get("category") or "user_profile").strip() or "user_profile"
    return get_memory_manager().upsert_memory_fact(
        category,
        fact,
        float((body or {}).get("confidence") or 0.8),
        int((body or {}).get("importance") or 3),
    )


@app.patch("/api/memory/facts/{fact_id}")
async def api_persistent_memory_fact_update(fact_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from .memory import get_memory_manager

    return get_memory_manager().update_fact(fact_id, dict(body or {}))


@app.post("/api/memory/test-write")
async def api_persistent_memory_test_write() -> dict[str, Any]:
    from .memory import get_memory_manager

    result = get_memory_manager().upsert_memory_fact("diagnostics", "persistent memory diagnostic write")
    return {"ok": bool(result.get("ok")), "result": result}


@app.post("/api/memory/local-fallback/clear")
async def api_persistent_memory_clear_local_fallback() -> dict[str, Any]:
    from .memory import get_memory_manager

    return get_memory_manager().clear_local_fallback()


# ──────────────────────────────────────────────────────────────────────────
# Admin — privilege introspection
# ──────────────────────────────────────────────────────────────────────────


@app.get("/api/admin/status")
async def api_admin_status() -> dict[str, Any]:
    """Report the current process's effective Windows privileges.

    Cosmo announces this on first boot so the operator knows what he's
    actually allowed to do — install services, kill processes, edit
    system files, etc. — without having to check manually.

    Useful when the user wonders whether they need to run the installer
    script (which elevates the service to LOCAL SYSTEM) to unlock
    system-wide actions.
    """
    import ctypes

    # Open SC manager with CREATE_SERVICE (0x0002) — that's the access
    # mask that actually requires admin. Connecting (0x0001) is granted
    # to non-admin users and would give a false positive.
    try:
        advapi = ctypes.windll.advapi32
        # Default restype for OpenSCManagerW is c_int — make it pointer-sized.
        advapi.OpenSCManagerW.restype = ctypes.c_void_p
        advapi.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        advapi.CloseServiceHandle.restype = ctypes.c_int
        scm = advapi.OpenSCManagerW(None, None, 0x0002)  # CREATE_SERVICE
        can_manage_services = bool(scm)
        if scm:
            advapi.CloseServiceHandle(scm)
    except Exception:
        can_manage_services = False

    # SeShutdownPrivilege / SeDebugPrivilege probe via GetTokenInformation
    # is the canonical check but heavy. The SC manager check is good
    # enough for the operator's "am I admin?" question.
    is_local_system = False
    try:
        import os
        # LOCAL SYSTEM runs as NT AUTHORITY\SYSTEM. Detect via env var.
        is_local_system = os.environ.get("USERNAME", "").upper() == "SYSTEM"
    except Exception:
        pass

    # Process identity + elevation level (TokenElevation)
    identity = {
        "username": "",
        "is_admin": False,
        "is_local_system": is_local_system,
        "can_manage_services": can_manage_services,
    }
    try:
        import os
        identity["username"] = os.environ.get("USERNAME", "") or os.environ.get("USER", "")
    except Exception:
        pass

    # Try the standard "is this process elevated?" check.
    try:
        import ctypes
        token = ctypes.c_void_p()
        token_elevation = ctypes.c_int(0)
        length = ctypes.c_ulong()
        advapi = ctypes.windll.advapi32
        if advapi.OpenProcessToken(ctypes.windll.kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            try:
                ctypes.windll.advapi32.GetTokenInformation(
                    token, 20,  # TokenElevation
                    ctypes.byref(token_elevation), ctypes.sizeof(token_elevation), ctypes.byref(length),
                )
                identity["is_admin"] = bool(token_elevation.value)
            finally:
                ctypes.windll.kernel32.CloseHandle(token)
    except Exception:
        pass

    return {"ok": True, **identity}


# ──────────────────────────────────────────────────────────────────────────
# Admin — project onboarding wizard
# ──────────────────────────────────────────────────────────────────────────


@app.get("/api/admin/projects")
async def api_admin_list_projects() -> dict[str, Any]:
    """List known project profiles (built-in + user-added)."""
    from .admin.add_project import list_projects
    return {"ok": True, "projects": list_projects()}


@app.post("/api/admin/add_project")
async def api_admin_add_project(body: dict[str, Any]) -> dict[str, Any]:
    """Create a new Hermes project profile + register it + restart the gateway.

    Body shape::

        {
          "name": "finance",            # required, kebab-case
          "cwd": "C:/.../finance",      # required, absolute path
          "soul_md": "...",             # required, generated by the UI wizard
          "api_key": "...",             # optional, auto-generated if empty
          "model": "gpt-oss:120b",      # optional
          "provider": "ollama-cloud",   # optional
          "base_url": "https://...",    # optional
          "ollama_api_key": "...",      # optional, inherits from default if empty
          "terminal_backend": "local",  # optional
          "terminal_timeout": 180,      # optional
          "notes": "",                  # optional
          "dry_run": false,             # optional — preview without writing
          "restart": true,              # optional — restart gateway (default true)
        }
    """
    from .admin.add_project import ProjectSpec, add_project

    name = (body.get("name") or "").strip()
    cwd = (body.get("cwd") or "").strip()
    soul_md = body.get("soul_md") or ""
    if not name or not cwd or not soul_md:
        return {"ok": False, "error": "name, cwd, and soul_md are required"}

    spec = ProjectSpec(
        name=name,
        cwd=cwd,
        soul_md=soul_md,
        api_key=body.get("api_key", "") or "",
        model=body.get("model", "gpt-oss:120b"),
        provider=body.get("provider", "ollama-cloud"),
        base_url=body.get("base_url", "https://ollama.com/v1"),
        ollama_api_key=body.get("ollama_api_key", "") or "",
        terminal_backend=body.get("terminal_backend", "local"),
        terminal_timeout=int(body.get("terminal_timeout", 180) or 180),
        notes=body.get("notes", "") or "",
    )
    return add_project(spec,
                       dry_run=bool(body.get("dry_run", False)),
                       restart=bool(body.get("restart", True)))


@app.post("/api/admin/remove_project")
async def api_admin_remove_project(body: dict[str, Any]) -> dict[str, Any]:
    """Delete a user-added project profile. Requires ``confirm=true``."""
    from .admin.add_project import remove_project
    name = (body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    return remove_project(name,
                          confirm=bool(body.get("confirm", False)),
                          restart=bool(body.get("restart", True)))


@app.post("/api/admin/preview_soul")
async def api_admin_preview_soul(body: dict[str, Any]) -> dict[str, Any]:
    """Generate a SOUL.md from wizard answers without writing any files."""
    from .admin.add_project import generate_soul_md
    text = generate_soul_md(
        project_title=body.get("project_title", ""),
        project_description=body.get("project_description", ""),
        cwd=body.get("cwd", ""),
        tech_stack=body.get("tech_stack", ""),
        conventions=body.get("conventions", ""),
    )
    return {"ok": True, "soul_md": text}


@app.get("/api/project/context")
async def api_project_context(cwd: str = "") -> dict[str, Any]:
    """Lightweight project context for the orb dashboard widget.

    Returns git branch + last commit for the cwd (if it's a git repo).
    Designed to never raise — failure to read git just returns nulls.
    """
    import asyncio
    import subprocess

    ctx: dict[str, Any] = {"cwd": cwd, "git_branch": None, "git_last_commit": None}
    if not cwd or not Path(cwd).is_dir():
        return ctx

    async def _git(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return stdout.decode("utf-8", "replace").strip()
        except Exception:
            return ""

    branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        ctx["git_branch"] = branch
    last = await _git("log", "-1", "--pretty=%h %s", "--no-color")
    if last:
        ctx["git_last_commit"] = last
    return ctx


@app.get("/api/logs")
async def api_logs(limit: int = 100, filter: str = "") -> dict[str, Any]:
    """Tail recent JARVIS + Hermes log lines for the orb logs panel.

    Sources: ~/.jarvis/*.log, Hermes gateway.log + agent.log.
    Capped at `limit` entries (newest first).
    """
    import re

    candidates = []
    home = Path.home() / ".jarvis"
    if home.is_dir():
        for p in home.glob("*.log"):
            try: candidates.append(p)
            except OSError: pass
    hermes_logs = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "logs"
    if hermes_logs.is_dir():
        for p in hermes_logs.glob("*.log"):
            try: candidates.append(p)
            except OSError: pass

    entries: list[dict[str, Any]] = []
    rx = re.compile(filter, re.IGNORECASE) if filter else None
    for path in candidates:
        try:
            # Read last ~200 lines of each file (cheap, single pass)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            continue
        for ln in reversed(lines):
            if rx and not rx.search(ln):
                continue
            # Try to parse "[2026-08-17 12:34:56] LEVEL message"
            m = re.match(r"^[\[(]?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[\])]?\s+(\w+)?\s*(.*)$", ln)
            if m:
                ts_str, level, msg = m.groups()
                try:
                    from datetime import datetime
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000
                except ValueError:
                    ts = 0
                entries.append({"time": ts, "level": level or "INFO", "msg": msg, "source": path.name})
            else:
                entries.append({"time": 0, "level": "LOG", "msg": ln, "source": path.name})

    entries.sort(key=lambda e: e["time"], reverse=True)
    return {"ok": True, "entries": entries[:limit]}


# ──────────────────────────────────────────────────────────────────────────
# Subagents (Researcher, Code reviewer, Email assistant, Content writer)
# ──────────────────────────────────────────────────────────────────────────


@app.get("/api/subagents")
async def api_subagents() -> dict[str, Any]:
    """List registered subagents. Used by the orb UI to render quick-launch buttons."""
    from .subagents import list_subagents
    return {"ok": True, "subagents": list_subagents()}


@app.post("/api/subagent/{name}")
async def api_subagent_run(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run a subagent turn synchronously (non-streaming). The result is the
    final assistant text. Use this for short commands from buttons / forms.
    For long outputs, prefer the WS slash-command path (``/research …``)."""
    from .subagents import get_subagent, wrap_prompt
    from . import hermes_client

    spec = get_subagent(name)
    if spec is None:
        return {"ok": False, "error": f"unknown subagent: {name}"}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is required"}

    mode = _current_mode
    wrapped = wrap_prompt(spec, prompt)

    # stream_chat returns {"final_text": str, ...} after the turn finishes.
    # We don't need to subscribe to deltas for this synchronous endpoint.
    async def _swallow(event: dict[str, Any]) -> None:
        return None

    msgs = [{"role": "user", "content": wrapped}]
    result = await hermes_client.stream_chat(msgs, _swallow, mode=mode)
    return {"ok": True, "subagent": name,
            "final_text": (result or {}).get("final_text", "")}


@app.get("/api/connectors")
async def api_connectors() -> dict[str, Any]:
    """List MCP connector integrations and their current (masked) configuration.

    Each connector gets an extra `mcp_ok` field:
      true  — configured AND live MCP connection is healthy
      false — configured BUT MCP connection failed (needs credentials/setup)
      null  — configured but not via the MCP manager (e.g. HA uses native tools)
    """
    from .tools import connectors

    return connectors.get_connectors_status()


@app.post("/api/connectors/{connector_id}")
async def api_connector_update(connector_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Enable/disable and configure a single MCP connector integration."""
    from .tools import connectors

    enabled = bool(body.get("enabled"))
    fields = body.get("fields") or {}
    connectors.update_connector(connector_id, enabled, fields)
    return {"ok": True}


@app.get("/api/channels/telegram")
async def api_telegram_status() -> dict[str, Any]:
    """Return Telegram bot connection status."""
    from .channels import telegram_bot as tb

    cfg = tb.load_config()
    return {
        "ok": True,
        "connected": bool(cfg.get("token")),
        "username": cfg.get("username", ""),
        "first_name": cfg.get("first_name", ""),
    }


@app.post("/api/channels/telegram/connect")
async def api_telegram_connect(request: Request) -> dict[str, Any]:
    """Verify a Telegram bot token and save it."""
    from .channels import telegram_bot as tb

    body = await request.json()
    token = str(body.get("token", "")).strip()
    if not token:
        return {"ok": False, "error": "Token is required"}
    info = await tb.get_bot_info(token)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error", "Invalid token")}
    tb.save_config({
        "token": token,
        "username": info["username"],
        "first_name": info["first_name"],
    })
    return {"ok": True, "username": info["username"], "first_name": info["first_name"]}


@app.post("/api/channels/telegram/disconnect")
async def api_telegram_disconnect() -> dict[str, Any]:
    """Remove the saved Telegram bot config."""
    from .channels import telegram_bot as tb

    tb.save_config({})
    return {"ok": True}


@app.get("/api/hardware")
async def api_hardware() -> dict[str, Any]:
    """Return detected hardware info, Ollama status, installed models, and recommendations."""
    from .tools import ollama_manager as om

    loop = asyncio.get_event_loop()
    cpu = om.detect_cpu()
    ram = om.detect_ram_gb()
    gpu = await loop.run_in_executor(None, om.detect_gpu)
    running = await om.ollama_running()
    installed = await om.list_installed_models() if running else []
    recs = om.get_recommendations(gpu["vram_gb"], ram)

    # Sample CPU + RAM live (psutil). psutil.cpu_percent is blocking for
    # `interval`, so run it in the default executor to keep the loop free.
    def _sample() -> tuple[float, dict[str, float]]:
        import psutil  # local import: optional dep at runtime
        pct = psutil.cpu_percent(interval=None)  # non-blocking since first call
        vm = psutil.virtual_memory()
        return pct, {
            "percent": float(vm.percent),
            "used_gb": round(vm.used / (1024 ** 3), 1),
            "total_gb": round(vm.total / (1024 ** 3), 1),
        }

    try:
        cpu_pct, ram_stats = await loop.run_in_executor(None, _sample)
    except Exception:
        cpu_pct, ram_stats = 0.0, {"percent": 0.0, "used_gb": 0.0, "total_gb": round(ram, 1)}

    return {
        "ok": True,
        "cpu": cpu,
        "cpu_pct": cpu_pct,
        "ram": ram_stats,
        "ram_gb": round(ram, 1),
        "gpu": gpu,
        "ollama_running": running,
        "installed_models": installed,
        "recommended_models": recs,
    }


@app.post("/api/ollama/pull")
async def api_ollama_pull(request: Request) -> Any:
    """Stream Ollama model pull progress as NDJSON lines."""
    from starlette.responses import StreamingResponse
    from .tools import ollama_manager as om

    body = await request.json()
    model = body.get("model", "")

    async def _gen():
        async for line in om.stream_pull(model):
            yield line + "\n"

    return StreamingResponse(_gen(), media_type="text/plain")


@app.post("/api/ollama/set_model")
async def api_ollama_set_model(request: Request) -> dict[str, Any]:
    """Switch JARVIS's active LLM backend to a local Ollama model."""
    from .tools import ollama_manager as om

    body = await request.json()
    model = body.get("model", "")
    await om.set_ollama_model(model)
    return {"ok": True}


@app.get("/api/history")
async def api_history() -> dict[str, Any]:
    """Read-only access to the persisted conversation log."""
    return {"ok": True, "messages": _load_history()}


# ──────────────────────────────────────────────────────────────────────────
# Home Assistant API
# ──────────────────────────────────────────────────────────────────────────

@app.get("/api/home-assistant/config")
async def api_ha_config() -> dict[str, Any]:
    """Return current HA config (token masked)."""
    from .tools import home_assistant as ha
    cfg = ha.load_config()
    token = cfg.get("token", "")
    masked = ("*" * (len(token) - 8) + token[-8:]) if len(token) > 8 else "****"
    return {
        "ok": True,
        "url": cfg.get("url", ""),
        "token_masked": masked if token else "",
        "configured": bool(cfg.get("url") and cfg.get("token")),
    }


@app.post("/api/home-assistant/config")
async def api_ha_save_config(request: Request) -> dict[str, Any]:
    """Save URL + token, test connection, and also update the MCP connector entry."""
    from .tools import home_assistant as ha
    from .tools import connectors

    body = await request.json()
    url = str(body.get("url", "")).strip().rstrip("/")
    token = str(body.get("token", "")).strip()

    if not url or not token:
        return {"ok": False, "error": "url and token are required"}

    # Test first
    result = await ha.test_connection(url, token)
    if not result["ok"]:
        return result

    # Save to native config
    ha.save_config(url, token)

    # Also keep MCP connector in sync
    try:
        connectors.update_connector(
            "home-assistant",
            enabled=True,
            fields={"homeassistant_url": url, "homeassistant_token": token},
        )
    except Exception as exc:
        log.warning("HA MCP connector sync failed: %s", exc)

    return {**result, "ok": True, "saved": True}


@app.post("/api/home-assistant/test")
async def api_ha_test(request: Request) -> dict[str, Any]:
    """Test a HA URL + token without saving."""
    from .tools import home_assistant as ha

    body = await request.json()
    url = str(body.get("url", "")).strip()
    token = str(body.get("token", "")).strip()

    # Fall back to stored config if not provided
    if not url or not token:
        cfg = ha.load_config()
        url = url or cfg.get("url", "")
        token = token or cfg.get("token", "")

    if not url or not token:
        return {"ok": False, "error": "No URL or token provided and none stored"}

    return await ha.test_connection(url, token)


@app.get("/api/home-assistant/states")
async def api_ha_states(request: Request) -> dict[str, Any]:
    """Return entity states for the frontend entity browser."""
    from .tools import home_assistant as ha

    cfg = ha.load_config()
    url = request.query_params.get("url") or cfg.get("url", "")
    token = request.query_params.get("token") or cfg.get("token", "")

    if not url or not token:
        return {"ok": False, "error": "Home Assistant not configured"}

    return await ha.get_states_for_ui(url, token)


@app.get("/api/home-assistant/areas")
async def api_ha_areas(request: Request) -> dict[str, Any]:
    """Return area list for the frontend."""
    from .tools import home_assistant as ha

    cfg = ha.load_config()
    url = request.query_params.get("url") or cfg.get("url", "")
    token = request.query_params.get("token") or cfg.get("token", "")

    if not url or not token:
        return {"ok": False, "error": "Home Assistant not configured"}

    return await ha.get_areas_for_ui(url, token)


# ──────────────────────────────────────────────────────────────────────────
# WebSocket
# ──────────────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    global _current_mode, _current_turn_task
    await ws.accept()
    hub.add(ws)

    # Probe admin status once per connection — cheap (~1ms), lets the UI
    # announce Cosmo's privilege level on connect.
    admin_status: dict[str, Any] = {}
    try:
        admin_status = await api_admin_status()
    except Exception:
        pass
    admin_status = {k: v for k, v in admin_status.items() if k != "ok"}

    await ws.send_text(json.dumps({
        "type": "ready",
        "model": llm.get_active_model(),
        "backend": llm._pick_backend(),
        "mode": _current_mode,
        "admin": admin_status,
    }))

    # Replay any restored/prior conversation so the UI visually resumes
    # exactly where it left off after a reboot, app restart, or shutdown.
    prior = [m for m in conversation if m.get("role") in ("user", "assistant")]
    if prior:
        await ws.send_text(json.dumps({"type": "history", "messages": prior}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "invalid JSON"}))
                continue
            mtype = msg.get("type")
            if mtype == "user_text":
                text = msg.get("text", "")
                voice = bool(msg.get("voice"))
                _create_task(handle_user_turn(text, voice=voice, send=ws.send_text))
            elif mtype == "user_audio_pcm":
                import base64
                b64 = msg.get("data", "")
                try:
                    pcm = base64.b64decode(b64)
                    stt_result = await get_stt().transcribe_pcm(pcm)
                    text = stt_result.get("text", "").strip()
                    if text:
                        detected = stt_result.get("language", "en") or "en"
                        global _current_lang
                        _current_lang = detected
                        await hub.broadcast({"type": "stt_language", "lang": detected,
                                             "prob": stt_result.get("language_probability", 1.0)})
                        _create_task(handle_user_turn(text, voice=True, send=ws.send_text))
                except Exception as exc:
                    await ws.send_text(json.dumps({"type": "error", "message": f"STT failed: {exc}"}))
            elif mtype == "reset":
                async with _lock:
                    _reset_conversation()
                llm.reset_session()
                try:
                    HISTORY_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
                await hub.broadcast({"type": "reset"})
            elif mtype == "stop":
                if _current_turn_task and not _current_turn_task.done():
                    _current_turn_task.cancel()
                await llm.stop_run()
                await tts.cancel_speaking()
                await hub.broadcast({"type": "stopped"})
            elif mtype == "set_mode":
                mode = str(msg.get("mode", "")).strip()
                if mode in ("default", "wwf") and mode != _current_mode:
                    _current_mode = mode
                    async with _lock:
                        _reset_conversation()
                    await tts.cancel_speaking()
                    await hub.broadcast({"type": "mode_changed", "mode": mode})
                else:
                    await ws.send_text(json.dumps({"type": "error", "message": f"unknown mode: {mode}"}))
            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong", "t": time.time()}))
            else:
                await ws.send_text(json.dumps({"type": "error", "message": f"unknown type: {mtype}"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("websocket error")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass
    finally:
        hub.remove(ws)


# ──────────────────────────────────────────────────────────────────────────
# Wake-word loop (openwakeword + sounddevice) — fully optional
# ──────────────────────────────────────────────────────────────────────────


async def _capture_utterance(
    audio_q: "asyncio.Queue[bytes]",
    *,
    sample_rate: int,
    chunk: int,
    max_seconds: float,
    preroll: list[bytes],
) -> bytes:
    """Capture a spoken command after the wake word.

    If webrtcvad is installed, stop as soon as the speaker finishes (after
    JARVIS_VAD_SILENCE_MS of trailing silence) rather than always recording the
    full window — more natural and lower latency. Falls back to a fixed
    ``max_seconds`` window when webrtcvad is unavailable.

    ``preroll`` is audio already drained from the queue before this call.
    """
    try:
        import webrtcvad  # type: ignore
    except ImportError:
        captured = list(preroll)
        frames_needed = int(sample_rate * max_seconds / chunk) - len(captured)
        for _ in range(max(0, frames_needed)):
            try:
                captured.append(await asyncio.wait_for(audio_q.get(), timeout=1.0))
            except asyncio.TimeoutError:
                break
        return b"".join(captured)

    aggressiveness = max(0, min(3, int(os.environ.get("JARVIS_VAD_AGGRESSIVENESS", "2"))))
    vad = webrtcvad.Vad(aggressiveness)
    silence_limit_ms = int(os.environ.get("JARVIS_VAD_SILENCE_MS", "800"))
    start_timeout_s = float(os.environ.get("JARVIS_VAD_START_TIMEOUT", "3.0"))
    frame_ms = 30
    frame_bytes = int(sample_rate * frame_ms / 1000) * 2  # 16-bit mono

    loop = asyncio.get_running_loop()
    hard_deadline = loop.time() + max_seconds
    start_deadline = loop.time() + start_timeout_s
    speech_started = False
    trailing_silence_ms = 0
    pending = b"".join(preroll)
    offset = 0

    while True:
        while len(pending) - offset >= frame_bytes:
            frame = pending[offset:offset + frame_bytes]
            offset += frame_bytes
            try:
                is_speech = vad.is_speech(frame, sample_rate)
            except Exception:
                is_speech = True  # on error, assume speech (don't cut off)
            if is_speech:
                speech_started = True
                trailing_silence_ms = 0
            elif speech_started:
                trailing_silence_ms += frame_ms

        if speech_started and trailing_silence_ms >= silence_limit_ms:
            break
        if loop.time() >= hard_deadline:
            break
        if not speech_started and loop.time() >= start_deadline:
            break

        try:
            pending += await asyncio.wait_for(audio_q.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue

    return pending


async def wake_word_loop() -> None:
    """Listen for 'Hey Jarvis' offline via openwakeword, then capture + STT + chat.

    All heavy lifting runs on threads so we don't block the event loop.
    If any dependency is missing, this coroutine exits quietly.
    """
    if os.environ.get("JARVIS_DISABLE_WAKEWORD") == "1":
        log.info("wake-word loop disabled via env")
        return
    try:
        import numpy as np  # type: ignore
        import sounddevice as sd  # type: ignore
        from openwakeword.model import Model as WakeModel  # type: ignore
    except ImportError as exc:
        log.warning("wake-word disabled: missing dependency (%s)", exc)
        return

    wake_phrase = os.environ.get("JARVIS_WAKE_MODEL", "hey_jarvis")
    sample_rate = 16_000
    chunk = 1280  # 80ms @ 16kHz, openwakeword's expected frame size
    loop = asyncio.get_running_loop()

    # openwakeword ships with NO pretrained models until download_models() runs once.
    # On a fresh install the models dir is empty, so Model(...) loads nothing and the
    # loop silently never fires. Detect that and download on demand.
    try:
        import glob
        models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
        if not glob.glob(os.path.join(models_dir, "*.onnx")):
            log.info("wake-word models missing — downloading openwakeword pretrained models (one-time)")
            import openwakeword.utils  # type: ignore
            await asyncio.to_thread(openwakeword.utils.download_models)
    except Exception as exc:
        log.warning("wake-word model download failed (%s)", exc)

    try:
        # Force onnx: tflite-runtime isn't available on Windows, and letting
        # openwakeword guess emits a noisy warning before falling back anyway.
        wake_model = await asyncio.to_thread(
            WakeModel, wakeword_models=[wake_phrase], inference_framework="onnx"
        )
        if not getattr(wake_model, "models", None):
            log.warning("wake-word model %r loaded nothing — disabling", wake_phrase)
            return
    except Exception as exc:
        log.warning("wake-word model load failed (%s) — disabling", exc)
        return

    audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    def _audio_cb(indata, frames, time_info, status) -> None:  # pragma: no cover
        try:
            loop.call_soon_threadsafe(audio_q.put_nowait, bytes(indata))
        except asyncio.QueueFull:
            pass

    log.info("wake-word loop listening for '%s'", wake_phrase)
    try:
        stream = sd.RawInputStream(
            samplerate=sample_rate, blocksize=chunk,
            dtype="int16", channels=1, callback=_audio_cb,
        )
    except Exception as exc:
        log.warning("microphone unavailable (%s) — wake-word disabled", exc)
        return

    silence_threshold = int(os.environ.get("JARVIS_WAKE_THRESHOLD", "500"))  # /1000 * model score
    capture_seconds = float(os.environ.get("JARVIS_CAPTURE_SECONDS", "5"))

    with stream:
        while True:
            try:
                pcm = await audio_q.get()
            except asyncio.CancelledError:
                return
            try:
                audio = np.frombuffer(pcm, dtype=np.int16)
                scores = await asyncio.to_thread(wake_model.predict, audio)
                score = max(scores.values()) if isinstance(scores, dict) else 0.0
            except Exception as exc:
                log.debug("wake predict error: %s", exc)
                continue
            if score >= (silence_threshold / 1000.0):
                log.info("wake word detected (score=%.2f)", score)
                await hub.broadcast({"type": "wake", "score": float(score)})
                # Barge-in: stop any current speech immediately so JARVIS goes
                # quiet and listens to the new command being spoken.
                await tts.cancel_speaking()
                # Preserve any already-buffered audio — the command may have been
                # spoken immediately after the wake phrase (e.g. "hey jarvis are you online").
                # Draining would silently discard it.
                captured: list[bytes] = []
                while not audio_q.empty():
                    try:
                        captured.append(audio_q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                # VAD-based capture: stops when the speaker finishes (or at the
                # capture_seconds cap). Falls back to a fixed window without webrtcvad.
                pcm_blob = await _capture_utterance(
                    audio_q,
                    sample_rate=sample_rate,
                    chunk=chunk,
                    max_seconds=capture_seconds,
                    preroll=captured,
                )
                try:
                    # vad_filter=False: we know speech is present after a wake word;
                    # aggressive VAD would otherwise silently drop short utterances.
                    stt_result = await get_stt().transcribe_pcm(pcm_blob, vad_filter=False)
                    text = (stt_result.get("text") or "").strip()
                    # Update global language from wake-word STT detection
                    detected = stt_result.get("language", "en") or "en"
                    _current_lang = detected
                    await hub.broadcast({"type": "stt_language", "lang": detected,
                                         "prob": stt_result.get("language_probability", 1.0)})
                except Exception as exc:
                    log.exception("STT after wake failed: %s", exc)
                    text = ""
                if text:
                    log.info("wake-word STT: %r (lang=%s)", text, _current_lang)
                    _create_task(handle_user_turn(text, voice=True, send=hub.broadcast))
                # cooldown
                await asyncio.sleep(1.0)


async def _on_startup() -> None:
    log.info("JARVIS online — backend=hermes, model=%s, mode=%s",
             llm.get_active_model(), _current_mode)
    log.info("[AURA IDENTITY] routes registered")

    # Restore previous session history
    prev = _load_history()
    if prev:
        async with _lock:
            conversation.extend(prev)
        log.info("restored %d messages from previous session", len(prev))

    # Pre-warm the STT and TTS models so first requests don't stall
    async def _prewarm_stt() -> None:
        try:
            await get_stt().ensure_loaded()
            log.info("STT model ready")
        except Exception as exc:
            log.warning("STT pre-warm failed: %s", exc)
        
        try:
            await tts.ensure_piper_loaded()
            log.info("Piper TTS model ready")
        except Exception as exc:
            log.warning("Piper pre-warm failed: %s", exc)

    _create_task(_prewarm_stt())
    _create_task(wake_word_loop())
    async def _prewarm_wake_tts() -> None:
        try:
            await asyncio.sleep(1)
            await diagnostics.synthesize_tts_chain("Awake, Sir.", include_audio=True, purpose="wake_prewarm", mode="voice_turn")
        except Exception as exc:
            log.debug("TTS wake pre-warm skipped: %s", exc)
    _create_task(_prewarm_wake_tts())
    # Pre-warm the cloud LLM so the user's first typed question doesn't pay
    # the model's cold-start cost (typically 2-4s on Ollama Cloud).
    from .hermes_client import warmup_model
    async def _prewarm_llm() -> None:
        # Small delay so it doesn't compete with STT/TTS loads.
        await asyncio.sleep(2)
        await warmup_model()
    _create_task(_prewarm_llm())

    # Scheduler: fire saved prompts on their schedule, streaming to the HUD.
    async def _fire_scheduled(prompt: str) -> None:
        log.info("scheduler firing job: %r", prompt[:60])
        await handle_user_turn(prompt, voice=False, send=hub.broadcast)

    try:
        from .tools import scheduler
        _create_task(scheduler.run_loop(_fire_scheduled))
        log.info("scheduler loop started")
    except Exception as exc:
        log.warning("scheduler failed to start: %s", exc)

    # Telegram channel: answer messages from your phone as if typed in the HUD.
    try:
        from .tools import channels
        tg = channels.from_env()
        if tg is not None:
            async def _on_telegram(text: str, chat_id: int) -> None:
                final = {"text": ""}

                async def _capture(event: dict) -> None:
                    await hub.broadcast(event)  # mirror to the HUD too
                    if event.get("type") == "turn_end":
                        final["text"] = event.get("final_text") or ""

                await handle_user_turn(text, voice=False, send=_capture)
                reply = final["text"] or "(no response)"
                await asyncio.to_thread(tg.send, chat_id, reply)

            _create_task(tg.run_loop(_on_telegram))
            log.info("telegram channel loop started")
    except Exception as exc:
        log.warning("telegram channel failed to start: %s", exc)
