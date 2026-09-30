"""Append-only local fallback memory store."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .memory_types import ConversationSummary, MemoryConfig, MemoryContext, MemoryFact, MessageRecord, utc_now_iso
from .redaction import redact_for_memory, redact_metadata_for_memory

ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = ROOT / "diagnostics" / "memory"
CONVERSATIONS_PATH = MEMORY_DIR / "conversations.jsonl"
MESSAGES_PATH = MEMORY_DIR / "messages.jsonl"
FACTS_PATH = MEMORY_DIR / "facts.jsonl"
EVENTS_PATH = MEMORY_DIR / "events.jsonl"


def _to_dict(record: Any) -> dict[str, Any]:
    return asdict(record) if is_dataclass(record) else dict(record or {})


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


class LocalFallbackMemoryStore:
    def __init__(self, config: MemoryConfig):
        self.config = config
        self.last_error: str | None = None

    def is_enabled(self) -> bool:
        return self.config.local_fallback and self.config.backend != "disabled"

    def health(self) -> dict[str, Any]:
        facts = [f for f in _read_jsonl(FACTS_PATH) if f.get("is_active", True)]
        conversations = _read_jsonl(CONVERSATIONS_PATH)
        return {
            "enabled": self.is_enabled(),
            "backend": "local_fallback" if self.is_enabled() else "disabled",
            "read_enabled": self.config.read_enabled,
            "write_enabled": self.config.write_enabled,
            "supabase_configured": False,
            "local_fallback_enabled": self.config.local_fallback,
            "last_error": self.last_error,
            "active_fact_count": len(facts),
            "recent_conversation_count": len(conversations[-50:]),
        }

    def ensure_conversation(self, conversation_id: str | None, title: str | None = None) -> str:
        cid = str(conversation_id or "").strip() or f"local-{utc_now_iso()}"
        if self.config.write_enabled:
            _append(CONVERSATIONS_PATH, {
                "conversation_id": cid,
                "owner_key": self.config.owner_key,
                "title": redact_for_memory(title or ""),
                "source": "main_orb",
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            })
        return cid

    def append_message(self, record: MessageRecord) -> dict[str, Any]:
        data = _to_dict(record)
        data["content"] = redact_for_memory(data.get("content", ""))
        data["normalized_content"] = redact_for_memory(data.get("normalized_content", ""))
        data["metadata"] = redact_metadata_for_memory(data.get("metadata") or {})
        result = _append(MESSAGES_PATH, data) if self.config.write_enabled else {"ok": False, "error": "write_disabled"}
        self.last_error = None if result.get("ok") else str(result.get("error"))
        return result

    def append_interaction_event(self, event: dict[str, Any]) -> dict[str, Any]:
        data = redact_metadata_for_memory(event)
        data["owner_key"] = self.config.owner_key
        data["created_at"] = utc_now_iso()
        result = _append(EVENTS_PATH, data) if self.config.write_enabled else {"ok": False, "error": "write_disabled"}
        self.last_error = None if result.get("ok") else str(result.get("error"))
        return result

    def upsert_memory_fact(self, fact: MemoryFact) -> dict[str, Any]:
        data = _to_dict(fact)
        data["fact"] = redact_for_memory(data.get("fact", ""))
        result = _append(FACTS_PATH, data) if self.config.write_enabled else {"ok": False, "error": "write_disabled"}
        self.last_error = None if result.get("ok") else str(result.get("error"))
        return result

    def deactivate_fact(self, fact_id: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        return _append(FACTS_PATH, {
            "id": fact_id,
            "owner_key": self.config.owner_key,
            "is_active": False,
            "metadata": redact_metadata_for_memory(patch or {}),
            "updated_at": utc_now_iso(),
        })

    def get_recent_messages(self, conversation_id: str, limit: int = 12) -> list[MessageRecord]:
        if not self.config.read_enabled:
            return []
        items = [m for m in _read_jsonl(MESSAGES_PATH) if str(m.get("conversation_id")) == str(conversation_id)]
        return [MessageRecord(**{k: v for k, v in item.items() if k in MessageRecord.__dataclass_fields__}) for item in items[-limit:]]

    def get_active_facts(self, owner_key: str, limit: int = 30) -> list[MemoryFact]:
        if not self.config.read_enabled:
            return []
        active: dict[str, dict[str, Any]] = {}
        for item in _read_jsonl(FACTS_PATH):
            if str(item.get("owner_key")) != str(owner_key):
                continue
            key = str(item.get("id") or item.get("fact") or "")
            if item.get("is_active", True):
                active[key] = item
            elif key in active:
                active.pop(key, None)
        return [MemoryFact(**{k: v for k, v in item.items() if k in MemoryFact.__dataclass_fields__}) for item in list(active.values())[-limit:]]

    def get_recent_summaries(self, owner_key: str, limit: int = 5) -> list[ConversationSummary]:
        return []

    def build_memory_context(self, conversation_id: str | None, current_input: str) -> MemoryContext:
        try:
            cid = str(conversation_id or "")
            facts = self.get_active_facts(self.config.owner_key, 30)
            messages = self.get_recent_messages(cid, 12) if cid else []
            lines: list[str] = []
            if facts:
                lines.append("Facts:")
                lines.extend(f"- {f.category}: {f.fact}" for f in facts)
            if messages:
                lines.append("Recent relevant turns:")
                lines.extend(f"- {m.role}: {m.normalized_content or m.content}" for m in messages[-6:])
            context = redact_for_memory("\n".join(lines))[: self.config.max_context_chars]
            return MemoryContext(True, "local_fallback", facts=facts, recent_messages=messages, context_text=context)
        except Exception as exc:
            self.last_error = type(exc).__name__
            return MemoryContext(False, "local_fallback", read_ok=False, last_error=self.last_error)

    def clear_local_fallback(self) -> dict[str, Any]:
        try:
            MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            for path in (CONVERSATIONS_PATH, MESSAGES_PATH, FACTS_PATH, EVENTS_PATH):
                path.write_text("", encoding="utf-8")
            return {"ok": True}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {"ok": False, "error": self.last_error}
