"""Deterministic local responses for A.U.R.A Experimenta demo visitor questions."""
from __future__ import annotations

import os
import re
from typing import Any

from .expo_content import (
    EXPO_AURA_PROJECT,
    EXPO_AURA_RESPONSE,
    EXPO_DEPARTMENT_PROJECTS,
    EXPO_DEPARTMENT_PROJECTS_RESPONSE,
    EXPO_EDGE_STORM_PROJECT,
    EXPO_EDGE_STORM_RESPONSE,
    EXPO_ENGINEERS_WEEK_RESPONSE,
    EXPO_GAME_CENTRE_PROJECT,
    EXPO_GAME_CENTRE_RESPONSE,
    EXPO_TECH_HUB_PROJECT,
    EXPO_TECH_HUB_RESPONSE,
)


def expo_demo_enabled() -> bool:
    return os.environ.get("AURA_EXPO_DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("a.u.r.a", "aura").replace("engineers'", "engineers")).strip()


def _address_suffix(address: str) -> str:
    clean = (address or "Sir").strip() or "Sir"
    if clean.lower() in {"guest", "none", "unknown"}:
        return ""
    return f", {clean}"


def _contains_any(norm: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in norm for phrase in phrases)


def _result(intent: str, response_text: str, normalized_text: str, matched_phrase: str | None = None) -> dict[str, Any]:
    return {
        "matched": True,
        "intent": intent,
        "response_text": response_text,
        "provider_route": "deterministic_expo_demo",
        "debug": {
            "enabled": True,
            "matched": True,
            "intent": intent,
            "matched_phrase": matched_phrase,
            "normalized_text": normalized_text,
        },
    }


def _canonical_department_result(normalized_text: str, matched_phrase: str | None = None) -> dict[str, Any]:
    return {
        "matched": True,
        "intent": "expo_department_projects.explain",
        "response_text": EXPO_DEPARTMENT_PROJECTS,
        "provider_route": "deterministic_expo_department_projects",
        "debug": {
            "enabled": True,
            "matched": True,
            "intent": "expo_department_projects.explain",
            "matched_phrase": matched_phrase,
            "normalized_text": normalized_text,
            "canonical_content_used": True,
            "cloud_bypassed": True,
            "cloud_bypass_reason": "expo_demo_canonical_content",
        },
    }


DEPARTMENT_PROJECT_PHRASES: tuple[str, ...] = (
    "tell the guests about ai&ds dept projects",
    "tell guests about ai&ds department projects",
    "explain ai&ds dept projects",
    "explain our department projects",
    "tell about our dept projects",
    "what projects are from ai&ds",
    "what are the department projects",
    "what projects are we showcasing",
    "tell about aura edge storm game centre tech hub",
    "explain the project expo items",
    "explain experimenta ai&ds projects",
    "ai and data science department projects",
    "aids dept projects",
    "ai&ds projects",
    "dept projects",
    "department showcase",
)


def match_expo_department_projects(normalized_text: str) -> dict[str, Any] | None:
    if not expo_demo_enabled():
        return None
    norm = _norm(normalized_text)
    aliases = (
        norm,
        norm.replace("ai and data science", "ai&ds"),
        norm.replace("ai & ds", "ai&ds"),
        norm.replace("ai ds", "ai&ds"),
        norm.replace("department", "dept"),
    )
    for phrase in DEPARTMENT_PROJECT_PHRASES:
        if any(phrase in value for value in aliases):
            return _canonical_department_result(norm, phrase)
    return None


def match_expo_demo_response(normalized_text: str, effective_address: str = "Sir") -> dict[str, Any] | None:
    if not expo_demo_enabled():
        return None

    norm = _norm(normalized_text)
    suffix = _address_suffix(effective_address)

    cases: list[tuple[str, tuple[str, ...], str]] = [
        (
            "expo_demo.project_intro",
            ("hello aura introduce yourself", "introduce yourself", "tell me about yourself", "who are you"),
            f"Hello{suffix}. I am A.U.R.A, the Artificial Unified Response Assistant. I am a local desktop AI agent built to respond through voice, understand commands, use safe tools, monitor system status, access live information, and assist users through this interactive orb interface.",
        ),
        (
            "expo_demo.capabilities",
            ("what can you do", "explain your features", "what are your features", "what all can you do", "show your capabilities"),
            f"I can respond through voice, answer questions, check weather and news, monitor CPU and RAM, open safe applications, control the microphone, use persistent memory, assist with local tools, and interact through both laptop and mobile web control{suffix}.",
        ),
        (
            "expo_demo.project_explanation",
            ("tell me about this project", "explain this project", "what is this project", "why was aura built"),
            "A.U.R.A was built as a local-first desktop assistant. The goal is to combine voice interaction, safe tool access, system awareness, live information, memory, and a visual orb interface into one practical AI agent for daily use and project demonstrations.",
        ),
        (
            "expo_demo.agent_difference",
            ("what makes aura different from a normal chatbot", "how are you different from chatgpt", "what makes this different", "why is this not just a chatbot"),
            "A normal chatbot mainly replies with text. A.U.R.A is designed as a local desktop agent. It can listen, speak, trigger safe tools, check system state, remember useful context, respond through the orb interface, and be controlled from a mobile web page while still running on the laptop.",
        ),
        (
            "expo_demo.student_usefulness",
            ("how does aura help students", "what makes you useful for students", "how can students use this", "how is this useful"),
            "A.U.R.A can help students by answering questions, preparing explanations, checking live information, managing reminders, assisting with project demos, opening tools, monitoring the system, and giving a hands-free interactive experience during presentations.",
        ),
        (
            "expo_demo.engineers_week",
            ("tell us about engineers week", "tell us about engineers day", "what is engineers week", "what is this expo about", "what is this experimenta about", "explain engineers week expo", "explain engineers week experimenta"),
            EXPO_ENGINEERS_WEEK_RESPONSE,
        ),
        (
            "expo_demo.department_showcase",
            ("what projects are shown here", "tell me about your department projects", "explain the projects in this expo", "explain the projects in this experimenta", "what are the projects in your stall", "department projects"),
            EXPO_DEPARTMENT_PROJECTS_RESPONSE,
        ),
        (
            "expo_demo.aura_project",
            ("tell me about aura", "tell about aura", "explain aura", "what is artificial unified response assistant", "aura project"),
            EXPO_AURA_PROJECT,
        ),
        (
            "expo_demo.edge_storm",
            ("tell me about edge storm", "tell about edge storm", "explain edge storm", "what is edge storm", "edge storm project"),
            EXPO_EDGE_STORM_PROJECT,
        ),
        (
            "expo_demo.game_centre",
            ("tell me about game centre", "tell about game centre", "explain game centre", "what games are here", "game centre project"),
            EXPO_GAME_CENTRE_PROJECT,
        ),
        (
            "expo_demo.tech_hub",
            ("tell me about tech hub", "tell about tech hub", "explain tech hub", "what is tech hub", "tech hub project"),
            EXPO_TECH_HUB_PROJECT,
        ),
        (
            "expo_demo.tool_safety",
            ("how do you use local tools safely", "explain your safe tools", "what is your tool safety", "can you control the laptop"),
            "I can use local tools only through a safe routing layer. Safe actions can run directly, sensitive actions need confirmation, and dangerous actions are blocked. This prevents accidental destructive commands while still allowing useful desktop control.",
        ),
        (
            "expo_demo.memory_fallback",
            ("are you storing this conversation", "what do you remember", "what did eswar tell you about the expo", "what did eswar tell you about experimenta"),
            f"I can store useful conversation context through persistent memory when Supabase is available{suffix}. For this demo, I am designed to remember important project and Experimenta-related context safely.",
        ),
        (
            "expo_demo.general_talk",
            ("can we have a general talk", "can we have an general talk", "can we talk", "can we chat", "talk with me"),
            f"Of course{suffix}. We can have a general conversation. You can ask me about this project, today's Experimenta, my features, weather, news, system status, or how I work.",
        ),
        (
            "expo_demo.closing_summary",
            ("give a short summary of today's demonstration", "summarize this demo", "conclude the demo", "end the intro"),
            "Today's demonstration shows A.U.R.A as a local-first AI desktop agent that can speak, respond visually, use safe tools, access live information, remember useful context, and support interactive project presentations. Thank you for visiting our showcase.",
        ),
    ]

    for intent, phrases, response_text in cases:
        for phrase in phrases:
            if phrase in norm:
                return _result(intent, response_text, norm, phrase)
    return None


def cloud_failure_fallback(address: str = "Sir") -> str:
    return f"I could not reach cloud reasoning right now{_address_suffix(address)}. For the Experimenta demo, you can ask me about A.U.R.A, its features, weather, news, system status, memory, or the department projects."
