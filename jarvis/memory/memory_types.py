"""Typed memory structures used by A.U.R.A persistent memory."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryConfig:
    backend: str = "disabled"
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    owner_key: str = "eswar_local"
    write_enabled: bool = True
    read_enabled: bool = True
    local_fallback: bool = True
    max_context_chars: int = 4000
    store_raw_turns: bool = True
    redact_secrets: bool = True


@dataclass
class ConversationRecord:
    owner_key: str
    conversation_id: str
    title: str | None = None
    source: str = "main_orb"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class MessageRecord:
    owner_key: str
    conversation_id: str
    role: str
    content: str
    normalized_content: str = ""
    language: str = "en"
    input_source: str = "text"
    provider_route: str | None = None
    frontend_kind: str | None = None
    intent_detected: str | None = None
    tool_name: str | None = None
    failure_class: str | None = None
    actions_returned: int = 0
    actions_executed: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class MemoryFact:
    owner_key: str
    category: str
    fact: str
    confidence: float = 0.8
    importance: int = 3
    is_active: bool = True
    source_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def fact_text(self) -> str:
        return self.fact

    @property
    def fact_type(self) -> str:
        return self.category


@dataclass
class ConversationSummary:
    owner_key: str
    conversation_id: str
    summary: str
    message_start_id: str | None = None
    message_end_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class InteractionEvent:
    owner_key: str
    event_type: str
    event_status: str | None = None
    conversation_id: str | None = None
    tool_name: str | None = None
    intent_detected: str | None = None
    failure_class: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class MemoryContext:
    enabled: bool
    backend: str
    facts: list[MemoryFact] = field(default_factory=list)
    recent_summaries: list[ConversationSummary] = field(default_factory=list)
    recent_messages: list[MessageRecord] = field(default_factory=list)
    context_text: str = ""
    read_ok: bool = True
    write_ok: bool | None = None
    last_error: str | None = None
