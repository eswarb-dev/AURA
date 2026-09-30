"""High-level persistent memory manager for voice turns and diagnostics."""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .local_fallback_memory import LocalFallbackMemoryStore
from .memory_types import MemoryConfig, MemoryContext, MemoryFact, MessageRecord, utc_now_iso
from .redaction import redact_for_memory, redact_metadata_for_memory
from .supabase_memory import SupabaseMemoryStore

_MANAGER: "MemoryManager | None" = None
ROOT = Path(__file__).resolve().parents[2]


def _repo_env_value(name: str) -> str | None:
    path = ROOT / ".env"
    if not path.exists():
        return None
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
    except OSError:
        return None
    return None


def _env(name: str, default: str = "") -> str:
    if name.startswith("AURA_MEMORY_") or name.startswith("AURA_SUPABASE_"):
        repo_value = _repo_env_value(name)
        if repo_value is not None:
            return repo_value
    return os.getenv(name, default)


def _bool_env(name: str, default: bool) -> bool:
    return str(_env(name, str(default))).strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(_env(name, str(default))).strip())
    except Exception:
        return default


def load_config() -> MemoryConfig:
    return MemoryConfig(
        backend=str(_env("AURA_MEMORY_BACKEND", "disabled")).strip().lower() or "disabled",
        supabase_url=str(_env("AURA_SUPABASE_URL", "")).strip(),
        supabase_service_role_key=str(_env("AURA_SUPABASE_SERVICE_ROLE_KEY", "")).strip(),
        owner_key=str(_env("AURA_MEMORY_OWNER_KEY", "eswar_local")).strip() or "eswar_local",
        write_enabled=_bool_env("AURA_MEMORY_WRITE_ENABLED", True),
        read_enabled=_bool_env("AURA_MEMORY_READ_ENABLED", True),
        local_fallback=_bool_env("AURA_MEMORY_LOCAL_FALLBACK", True),
        max_context_chars=_int_env("AURA_MEMORY_MAX_CONTEXT_CHARS", 4000),
        store_raw_turns=_bool_env("AURA_MEMORY_STORE_RAW_TURNS", True),
        redact_secrets=_bool_env("AURA_MEMORY_REDACT_SECRETS", True),
    )


def _conversation_uuid(conversation_id: str | None) -> str:
    value = str(conversation_id or "").strip()
    if not value:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(value))
    except Exception:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aura-memory:{value}"))


class MemoryManager:
    def __init__(self, config: MemoryConfig | None = None):
        self.config = config or load_config()
        self.supabase = SupabaseMemoryStore(self.config)
        self.local = LocalFallbackMemoryStore(self.config)
        self.last_write_ok: bool | None = None
        self.last_read_ok: bool | None = None
        self.last_read_primary_ok: bool | None = None
        self.last_error: str | None = None
        self.last_write_status = "not_attempted"
        self.last_read_status = "not_attempted"
        self.last_facts_saved = 0

    def _active_store(self):
        if self.config.backend == "supabase" and self.supabase.is_enabled():
            return self.supabase
        if self.config.backend != "disabled" and self.config.local_fallback:
            return self.local
        return None

    def memory_status(self) -> dict[str, Any]:
        active = self._active_store()
        health = active.health() if active else {
            "enabled": False,
            "backend": "disabled",
            "read_enabled": self.config.read_enabled,
            "write_enabled": self.config.write_enabled,
            "supabase_configured": bool(self.config.supabase_url and self.config.supabase_service_role_key),
            "local_fallback_enabled": self.config.local_fallback,
            "last_error": self.last_error or self.supabase.last_error,
        }
        health["ok"] = True
        health["owner_key_present"] = bool(self.config.owner_key)
        health["last_write_ok"] = self.last_write_ok
        health["last_read_ok"] = self.last_read_ok
        health["last_write_status"] = self.last_write_status
        health["last_read_status"] = self.last_read_status
        health["last_error_safe"] = health.get("last_error")
        return health

    def load_context_for_turn(self, conversation_id: str | None, user_text: str) -> MemoryContext:
        active = self._active_store()
        if not active or not self.config.read_enabled:
            return MemoryContext(False, "disabled", read_ok=not self.config.read_enabled)
        cid = _conversation_uuid(conversation_id)
        if active is self.supabase:
            self.supabase.last_error = None
        ctx = active.build_memory_context(cid, redact_for_memory(user_text))
        primary_read_ok = bool(ctx.read_ok and not (active is self.supabase and self.supabase.last_error))
        if not primary_read_ok and active is self.supabase:
            ctx.read_ok = False
            ctx.last_error = self.supabase.last_error or ctx.last_error or "memory_read_failed"
        if self.config.local_fallback and active is not self.local and (not ctx.read_ok or (not ctx.facts and self.supabase.last_error)):
            fallback_ctx = self.local.build_memory_context(cid, user_text)
            if fallback_ctx.context_text or fallback_ctx.facts or not ctx.read_ok:
                fallback_ctx.backend = "local_fallback"
                fallback_ctx.last_error = ctx.last_error or self.supabase.last_error
                ctx = fallback_ctx
            self.last_read_primary_ok = primary_read_ok if active is self.supabase else bool(ctx.read_ok)
        self.last_read_ok = bool(ctx.read_ok)
        self.last_read_status = "success" if ctx.read_ok else "failed"
        self.last_error = None if ctx.read_ok else ctx.last_error
        return ctx

    def _write_message(self, record: MessageRecord) -> dict[str, Any]:
        active = self._active_store()
        if not active or not self.config.write_enabled:
            self.last_write_ok = False
            return {"ok": False, "error": "memory_disabled"}
        try:
            record.conversation_id = _conversation_uuid(record.conversation_id)
            active.ensure_conversation(record.conversation_id, None)
            result = active.append_message(record)
            primary_ok = bool(result.get("ok"))
            fallback_ok = False
            if not result.get("ok") and self.config.local_fallback and active is not self.local:
                result = self.local.append_message(record)
                fallback_ok = bool(result.get("ok"))
            result["primary_ok"] = primary_ok
            result["fallback_ok"] = fallback_ok
            result["backend"] = "supabase" if active is self.supabase else "local_fallback"
            result["effective_backend"] = result["backend"] if primary_ok else ("local_fallback" if fallback_ok else result["backend"])
            self.last_write_ok = primary_ok if active is self.supabase else bool(result.get("ok"))
            self.last_write_status = "success" if self.last_write_ok else "failed"
            self.last_error = None if self.last_write_ok else str(result.get("error") or self.supabase.last_error or "memory_write_failed")
            return result
        except Exception as exc:
            self.last_write_ok = False
            self.last_write_status = "failed"
            self.last_error = type(exc).__name__
            return {"ok": False, "error": self.last_error}

    def record_user_turn(self, *, conversation_id: str, content: str, normalized_content: str, input_source: str, frontend_kind: str | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        text = redact_for_memory(content) if self.config.store_raw_turns else "[RAW_TURN_STORAGE_DISABLED]"
        normalized = redact_for_memory(normalized_content)
        record = MessageRecord(
            owner_key=self.config.owner_key,
            conversation_id=_conversation_uuid(conversation_id),
            role="user",
            content=text,
            normalized_content=normalized,
            input_source=input_source,
            frontend_kind=frontend_kind,
            metadata=redact_metadata_for_memory(metadata or {}),
        )
        result = self._write_message(record)
        facts = self.maybe_extract_memory_facts(normalized or text)
        result["facts_saved"] = sum(1 for fact in facts if fact.get("primary_ok", fact.get("ok")))
        result["facts_fallback_saved"] = sum(1 for fact in facts if fact.get("fallback_ok"))
        result["facts"] = facts
        return result

    def record_assistant_turn(self, *, conversation_id: str, content: str, provider_route: str | None, frontend_kind: str | None, intent_detected: str | None, tool_name: str | None, failure_class: str | None, actions_returned: int = 0, actions_executed: list[str] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_content = redact_for_memory(content) if self.config.store_raw_turns else "[RAW_TURN_STORAGE_DISABLED]"
        record = MessageRecord(
            owner_key=self.config.owner_key,
            conversation_id=_conversation_uuid(conversation_id),
            role="assistant",
            content=safe_content,
            normalized_content=redact_for_memory(content),
            input_source="text",
            provider_route=provider_route,
            frontend_kind=frontend_kind,
            intent_detected=intent_detected,
            tool_name=tool_name,
            failure_class=failure_class,
            actions_returned=int(actions_returned or 0),
            actions_executed=actions_executed or [],
            metadata=redact_metadata_for_memory(metadata or {}),
        )
        return self._write_message(record)

    def record_tool_event(self, *, conversation_id: str | None, event_status: str, tool_name: str | None, intent_detected: str | None, failure_class: str | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        active = self._active_store()
        if not active or not self.config.write_enabled:
            self.last_write_ok = False
            return {"ok": False, "error": "memory_disabled"}
        event = {
            "conversation_id": _conversation_uuid(conversation_id),
            "event_type": "tool_command",
            "event_status": event_status,
            "tool_name": tool_name,
            "intent_detected": intent_detected,
            "failure_class": failure_class,
            "metadata": redact_metadata_for_memory(metadata or {}),
            "created_at": utc_now_iso(),
        }
        result = active.append_interaction_event(event)
        if not result.get("ok") and self.config.local_fallback and active is not self.local:
            result = self.local.append_interaction_event(event)
        self.last_write_ok = bool(result.get("ok"))
        self.last_error = None if self.last_write_ok else str(result.get("error"))
        return result

    def maybe_extract_memory_facts(self, text: str) -> list[dict[str, Any]]:
        norm = " ".join(str(text or "").strip().split())
        lowered = norm.lower()
        if not norm or "don't remember" in lowered or "dont remember" in lowered or "forget that" in lowered or "remove that memory" in lowered:
            return []
        patterns: list[tuple[str, str]] = [
            (r"\bmy name is\s+(.{1,80})$", "user_profile"),
            (r"\bcall me\s+(.{1,80})$", "user_profile"),
            (r"\bi prefer\s+(.{1,160})$", "response_preference"),
            (r"\bremember that\s+(.{1,220})$", "response_preference"),
        ]
        facts: list[MemoryFact] = []
        seen_fact_text: set[str] = set()
        if (
            ("tomorrow" in lowered or "tommorrow" in lowered)
            and "project expo" in lowered
            and ("showcase" in lowered or "college" in lowered)
        ):
            facts.append(MemoryFact(
                owner_key=self.config.owner_key,
                category="event_context",
                fact="Tomorrow, Eswar has a project expo and plans to showcase A.U.R.A to his college.",
                confidence=0.95,
                importance=5,
                metadata={
                    "fact_type": "event_context",
                    "importance": "high",
                    "source": "user_statement",
                    "date_reference": "tomorrow",
                    "project": "A.U.R.A",
                },
            ))
            seen_fact_text.add(facts[-1].fact.lower())
        elif (
            ("i am going to" in lowered or "i'm going to" in lowered or "i will" in lowered)
            and ("showcase" in lowered or "project" in lowered or "college" in lowered)
        ):
            facts.append(MemoryFact(
                owner_key=self.config.owner_key,
                category="event_context",
                fact=redact_for_memory(norm.strip(" .")),
                confidence=0.85,
                importance=4,
                metadata={"fact_type": "event_context", "source": "user_statement"},
            ))
            seen_fact_text.add(facts[-1].fact.lower())
        for pattern, category in patterns:
            match = re.search(pattern, norm, re.IGNORECASE)
            if match:
                fact_text = match.group(1).strip(" .")
                fact_key = fact_text.lower()
                if fact_key in seen_fact_text:
                    continue
                seen_fact_text.add(fact_key)
                cat = category
                low_fact = fact_text.lower()
                if "tamil" in low_fact:
                    cat = "language_preference"
                elif any(x in low_fact for x in ("aura should", "prometheus should", "route this locally", "about:blank")):
                    cat = "project_preference" if "should" in low_fact else "tool_behavior_preference"
                elif any(x in low_fact for x in ("safe", "secret", "privacy", "do not", "don't")):
                    cat = "safety_preference"
                facts.append(MemoryFact(owner_key=self.config.owner_key, category=cat, fact=redact_for_memory(fact_text)))
                break
        written: list[dict[str, Any]] = []
        for fact in facts:
            result = self.upsert_memory_fact(fact.category, fact.fact, fact.confidence, fact.importance, fact.metadata)
            written.append({
                "category": fact.category,
                "fact": fact.fact,
                "ok": bool(result.get("primary_ok", result.get("ok"))),
                "primary_ok": bool(result.get("primary_ok", result.get("ok"))),
                "fallback_ok": bool(result.get("fallback_ok")),
                "effective_backend": result.get("effective_backend"),
            })
        self.last_facts_saved = sum(1 for item in written if item.get("primary_ok"))
        return written

    def upsert_memory_fact(self, category: str, fact: str, confidence: float = 0.8, importance: int = 3, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        active = self._active_store()
        if not active or not self.config.write_enabled:
            self.last_write_ok = False
            return {"ok": False, "error": "memory_disabled"}
        record = MemoryFact(
            owner_key=self.config.owner_key,
            category=redact_for_memory(category or "user_profile")[:80],
            fact=redact_for_memory(fact),
            confidence=float(confidence or 0.8),
            importance=max(1, min(5, int(importance or 3))),
            metadata=redact_metadata_for_memory(metadata or {}),
        )
        result = active.upsert_memory_fact(record)
        primary_ok = bool(result.get("ok"))
        fallback_ok = False
        if not result.get("ok") and self.config.local_fallback and active is not self.local:
            result = self.local.upsert_memory_fact(record)
            fallback_ok = bool(result.get("ok"))
        result["primary_ok"] = primary_ok
        result["fallback_ok"] = fallback_ok
        result["backend"] = "supabase" if active is self.supabase else "local_fallback"
        result["effective_backend"] = result["backend"] if primary_ok else ("local_fallback" if fallback_ok else result["backend"])
        self.last_write_ok = primary_ok if active is self.supabase else bool(result.get("ok"))
        self.last_write_status = "success" if self.last_write_ok else "failed"
        self.last_error = None if self.last_write_ok else str(result.get("error") or self.supabase.last_error or "memory_write_failed")
        return result

    def list_facts(self, limit: int = 50) -> dict[str, Any]:
        active = self._active_store()
        facts = active.get_active_facts(self.config.owner_key, limit) if active and self.config.read_enabled else []
        return {"ok": True, "backend": self.memory_status().get("backend"), "facts": [self._fact_payload(f) for f in facts]}

    def update_fact(self, fact_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        active = self._active_store()
        if not active:
            return {"ok": False, "error": "memory_disabled"}
        if patch.get("is_active") is False:
            return active.deactivate_fact(fact_id, patch)
        if patch.get("fact"):
            return self.upsert_memory_fact(str(patch.get("category") or "user_profile"), str(patch.get("fact")), float(patch.get("confidence") or 0.8), int(patch.get("importance") or 3))
        return {"ok": False, "error": "no_supported_update"}

    def summarize_if_needed(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "skipped": True, "reason": "not_implemented_without_llm"}

    def _fact_payload(self, fact: MemoryFact) -> dict[str, Any]:
        data = asdict(fact)
        data["fact_text"] = fact.fact
        data["fact_type"] = fact.category
        data["importance_score"] = fact.importance
        data["importance"] = (fact.metadata or {}).get("importance") or ("high" if fact.importance >= 5 else "medium" if fact.importance >= 3 else "low")
        return data

    def context_preview(self, conversation_id: str | None = None, query: str | None = None) -> dict[str, Any]:
        ctx = self.load_context_for_turn(_conversation_uuid(conversation_id) if conversation_id else None, query or "")
        return {
            "ok": True,
            "query": redact_for_memory(query or ""),
            "enabled": ctx.enabled,
            "backend": ctx.backend,
            "context_text": redact_for_memory(ctx.context_text),
            "facts": [self._fact_payload(f) for f in ctx.facts],
            "recent_summaries": [asdict(s) for s in ctx.recent_summaries],
            "recent_message_count": len(ctx.recent_messages),
            "last_error": ctx.last_error,
        }

    def recall_from_memory(self, query: str, address: str = "Sir") -> dict[str, Any] | None:
        lowered = str(query or "").lower()
        if not any(phrase in lowered for phrase in ("what did i tell you about tomorrow", "what did i say about tomorrow", "remember about tomorrow")):
            return None
        ctx = self.load_context_for_turn(None, query)
        for fact in ctx.facts:
            fact_text = fact.fact.lower()
            if "tomorrow" in fact_text and "project expo" in fact_text and "college" in fact_text:
                return {
                    "ok": True,
                    "response_text": f"You told me that tomorrow you have a project expo and you are going to showcase A.U.R.A to your college, {address}.",
                    "provider": f"deterministic_memory_recall_{ctx.backend}",
                    "intent": "memory_recall.tomorrow",
                    "facts_used": 1,
                    "backend": ctx.backend,
                    "read_ok": ctx.read_ok,
                }
        return None

    def clear_local_fallback(self) -> dict[str, Any]:
        return self.local.clear_local_fallback()


def get_memory_manager() -> MemoryManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = MemoryManager()
    return _MANAGER
