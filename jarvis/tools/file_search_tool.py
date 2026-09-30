"""Allowlisted local file search."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .tool_types import tool_response

SECRET_NAMES = {".env", "id_rsa", "id_ed25519"}
TEXT_EXTS = {".txt", ".md", ".csv"}


def _allowed_roots() -> list[Path]:
    raw = os.getenv("AURA_ALLOWED_FILE_ROOTS") or r"E:\AURA AGENT;C:\Users\choos\Downloads;C:\Users\choos\Documents"
    roots = []
    for item in raw.split(";"):
        try:
            path = Path(item.strip()).resolve()
            if path.exists():
                roots.append(path)
        except Exception:
            continue
    return roots[:5]


def _is_safe(path: Path, roots: list[Path]) -> bool:
    name = path.name.lower()
    if name in SECRET_NAMES or "token" in name or "password" in name or "secret" in name:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    try:
        resolved = path.resolve()
        return any(resolved == root or root in resolved.parents for root in roots)
    except Exception:
        return False


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    roots = _allowed_roots()
    if not roots:
        return tool_response(ok=False, tool="file_search", action=action, response_text="No safe file search roots are configured, Sir.", error_type="no_allowed_roots")
    query = str(params.get("query") or "").lower().strip()
    try:
        if action in {"find_file", "recent_files"}:
            matches: list[dict[str, Any]] = []
            for root in roots:
                for path in root.rglob("*"):
                    if len(matches) >= 20:
                        break
                    if not path.is_file() or not _is_safe(path, roots):
                        continue
                    if action == "recent_files" or not query or query in path.name.lower():
                        matches.append({"name": path.name, "path": str(path), "modified": path.stat().st_mtime})
                if len(matches) >= 20:
                    break
            matches.sort(key=lambda x: x["modified"], reverse=True)
            return tool_response(ok=True, tool="file_search", action=action, executed=True, result={"matches": matches[:10]}, response_text=f"I found {len(matches[:10])} matching files, Sir.")
        if action == "summarize_text_file":
            path = Path(str(params.get("path") or "")).resolve()
            if not _is_safe(path, roots) or path.suffix.lower() not in TEXT_EXTS:
                return tool_response(ok=False, tool="file_search", action=action, response_text="I cannot read that file safely, Sir.", error_type="file_not_allowed")
            text = path.read_text(encoding="utf-8", errors="replace")[:1200]
            return tool_response(ok=True, tool="file_search", action=action, executed=True, result={"path": str(path), "preview": text[:400]}, response_text=f"I can summarize this text file, Sir. It has about {len(text.split())} words in the preview.")
        if action == "open_file_location":
            return tool_response(ok=True, tool="file_search", action=action, executed=False, result={"frontend_action": {"type": "folder.open", "label": "Open file location", "value": str(params.get("path") or roots[0])}}, response_text="Opening the file location, Sir.")
    except Exception as exc:
        return tool_response(ok=False, tool="file_search", action=action, response_text="I could not search files right now, Sir.", error_type="file_search_failed", error_message=str(exc)[:160])
    return tool_response(ok=False, tool="file_search", action=action, response_text="That file action is not available yet, Sir.", error_type="unknown_action")
