"""Shared response helpers for A.U.R.A deterministic tools."""
from __future__ import annotations

from typing import Any, Literal, TypedDict

RiskLevel = Literal["safe", "confirm_required", "blocked"]


class ToolResponse(TypedDict):
    ok: bool
    tool: str
    action: str
    risk_level: RiskLevel
    requires_confirmation: bool
    executed: bool
    result: dict[str, Any]
    response_text: str
    error_type: str | None
    error_message: str | None
    metadata: dict[str, Any]


def tool_response(
    *,
    ok: bool,
    tool: str,
    action: str,
    risk_level: RiskLevel = "safe",
    executed: bool = False,
    result: dict[str, Any] | None = None,
    response_text: str = "",
    error_type: str | None = None,
    error_message: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResponse:
    return {
        "ok": ok,
        "tool": tool,
        "action": action,
        "risk_level": risk_level,
        "requires_confirmation": risk_level == "confirm_required",
        "executed": executed,
        "result": result or {},
        "response_text": response_text,
        "error_type": error_type,
        "error_message": error_message,
        "metadata": metadata or {},
    }


def confirmation_required(tool: str, action: str) -> ToolResponse:
    return tool_response(
        ok=False,
        tool=tool,
        action=action,
        risk_level="confirm_required",
        executed=False,
        response_text="That action requires confirmation, Sir.",
        error_type="confirmation_required",
    )


def blocked(tool: str, action: str, message: str = "I cannot do that safely, Sir.") -> ToolResponse:
    return tool_response(
        ok=False,
        tool=tool,
        action=action,
        risk_level="blocked",
        executed=False,
        response_text=message,
        error_type="blocked_action",
    )
