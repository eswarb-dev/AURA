"""Supabase-backed persistent memory store."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .memory_types import ConversationSummary, MemoryConfig, MemoryContext, MemoryFact, MessageRecord
from .redaction import redact_for_memory, redact_metadata_for_memory


class SupabaseMemoryStore:
    def __init__(self, config: MemoryConfig):
        self.config = config
        self.client: Any = None
        self.last_error: str | None = None
        self.configured = bool(config.supabase_url and config.supabase_service_role_key)
        if self.configured and config.backend == "supabase":
            try:
                from supabase import Client, create_client
                self.client = create_client(config.supabase_url, config.supabase_service_role_key)
                assert self.client is not None
            except Exception as exc:
                self.client = None
                self.last_error = type(exc).__name__

    def is_enabled(self) -> bool:
        return bool(self.client and self.config.backend == "supabase")

    def health(self) -> dict[str, Any]:
        active_fact_count = 0
        recent_conversation_count = 0
        if self.is_enabled() and self.config.read_enabled:
            try:
                facts = self.client.table("aura_memory_facts").select("id").eq("owner_key", self.config.owner_key).eq("is_active", True).limit(500).execute().data
                conversations = self.client.table("aura_conversations").select("id").eq("owner_key", self.config.owner_key).limit(100).execute().data
                active_fact_count = len(facts) if isinstance(facts, list) else 0
                recent_conversation_count = len(conversations) if isinstance(conversations, list) else 0
            except Exception as exc:
                self.last_error = type(exc).__name__
        return {
            "enabled": self.is_enabled(),
            "backend": "supabase" if self.is_enabled() else "disabled",
            "read_enabled": self.config.read_enabled,
            "write_enabled": self.config.write_enabled,
            "supabase_configured": self.configured,
            "local_fallback_enabled": self.config.local_fallback,
            "last_error": self.last_error,
            "active_fact_count": active_fact_count,
            "recent_conversation_count": recent_conversation_count,
        }

    def _safe_execute(self, fn):
        try:
            return {"ok": True, "data": fn().execute().data}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {"ok": False, "error": self.last_error}

    def ensure_conversation(self, conversation_id: str | None, title: str | None = None) -> str:
        if not self.is_enabled() or not self.config.write_enabled:
            return str(conversation_id or "")
        payload = {
            "owner_key": self.config.owner_key,
            "title": redact_for_memory(title or "") or None,
            "source": "main_orb",
        }
        if conversation_id:
            payload["id"] = conversation_id
        result = self._safe_execute(lambda: self.client.table("aura_conversations").upsert(payload))
        data = result.get("data") if isinstance(result.get("data"), list) else []
        return str((data[0] or {}).get("id") or conversation_id or "") if data else str(conversation_id or "")

    def append_message(self, record: MessageRecord) -> dict[str, Any]:
        if not self.is_enabled() or not self.config.write_enabled:
            return {"ok": False, "error": "supabase_disabled"}
        payload = asdict(record)
        payload["content"] = redact_for_memory(payload.get("content", ""))
        payload["normalized_content"] = redact_for_memory(payload.get("normalized_content", ""))
        payload["metadata"] = redact_metadata_for_memory(payload.get("metadata") or {})
        return self._safe_execute(lambda: self.client.table("aura_messages").insert(payload))

    def append_interaction_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self.is_enabled() or not self.config.write_enabled:
            return {"ok": False, "error": "supabase_disabled"}
        payload = redact_metadata_for_memory(event)
        payload["owner_key"] = self.config.owner_key
        return self._safe_execute(lambda: self.client.table("aura_interaction_events").insert(payload))

    def upsert_memory_fact(self, fact: MemoryFact) -> dict[str, Any]:
        if not self.is_enabled() or not self.config.write_enabled:
            return {"ok": False, "error": "supabase_disabled"}
        payload = asdict(fact)
        payload["fact"] = redact_for_memory(payload.get("fact", ""))
        payload["metadata"] = redact_metadata_for_memory(payload.get("metadata") or {})
        return self._safe_execute(lambda: self.client.table("aura_memory_facts").insert(payload))

    def get_recent_messages(self, conversation_id: str, limit: int = 12) -> list[MessageRecord]:
        if not self.is_enabled() or not self.config.read_enabled or not conversation_id:
            return []
        result = self._safe_execute(lambda: self.client.table("aura_messages").select("*").eq("conversation_id", conversation_id).order("created_at", desc=True).limit(limit))
        items = result.get("data") if result.get("ok") and isinstance(result.get("data"), list) else []
        return [MessageRecord(**{k: v for k, v in item.items() if k in MessageRecord.__dataclass_fields__}) for item in reversed(items)]

    def get_active_facts(self, owner_key: str, limit: int = 30) -> list[MemoryFact]:
        if not self.is_enabled() or not self.config.read_enabled:
            return []
        result = self._safe_execute(lambda: self.client.table("aura_memory_facts").select("*").eq("owner_key", owner_key).eq("is_active", True).order("updated_at", desc=True).limit(limit))
        items = result.get("data") if result.get("ok") and isinstance(result.get("data"), list) else []
        return [MemoryFact(**{k: v for k, v in item.items() if k in MemoryFact.__dataclass_fields__}) for item in items]

    def get_recent_summaries(self, owner_key: str, limit: int = 5) -> list[ConversationSummary]:
        if not self.is_enabled() or not self.config.read_enabled:
            return []
        result = self._safe_execute(lambda: self.client.table("aura_conversation_summaries").select("*").eq("owner_key", owner_key).order("created_at", desc=True).limit(limit))
        items = result.get("data") if result.get("ok") and isinstance(result.get("data"), list) else []
        return [ConversationSummary(**{k: v for k, v in item.items() if k in ConversationSummary.__dataclass_fields__}) for item in items]

    def build_memory_context(self, conversation_id: str | None, current_input: str) -> MemoryContext:
        try:
            facts = self.get_active_facts(self.config.owner_key, 30)
            summaries = self.get_recent_summaries(self.config.owner_key, 5)
            messages = self.get_recent_messages(str(conversation_id or ""), 12)
            lines: list[str] = []
            if facts:
                lines.append("Facts:")
                lines.extend(f"- {f.category}: {f.fact}" for f in facts)
            if summaries:
                lines.append("Recent summaries:")
                lines.extend(f"- {s.summary}" for s in summaries)
            if messages:
                lines.append("Recent relevant turns:")
                lines.extend(f"- {m.role}: {m.normalized_content or m.content}" for m in messages[-6:])
            context = redact_for_memory("\n".join(lines))[: self.config.max_context_chars]
            return MemoryContext(True, "supabase", facts=facts, recent_summaries=summaries, recent_messages=messages, context_text=context)
        except Exception as exc:
            self.last_error = type(exc).__name__
            return MemoryContext(False, "supabase", read_ok=False, last_error=self.last_error)

    def deactivate_fact(self, fact_id: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.is_enabled() or not self.config.write_enabled:
            return {"ok": False, "error": "supabase_disabled"}
        payload = redact_metadata_for_memory(patch or {})
        payload["is_active"] = False
        return self._safe_execute(lambda: self.client.table("aura_memory_facts").update(payload).eq("id", fact_id).eq("owner_key", self.config.owner_key))
