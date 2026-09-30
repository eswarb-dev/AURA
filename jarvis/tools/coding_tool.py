"""Coding and GitHub helper with read-only safe defaults."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .tool_types import confirmation_required, tool_response

ROOT = Path(__file__).resolve().parents[2]


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=8, check=False)


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if action == "git_status":
            proc = _git(["status", "--short"])
            lines = [line for line in proc.stdout.splitlines() if ".env" not in line][:40]
            return tool_response(ok=proc.returncode == 0, tool="coding", action=action, executed=True, result={"status": lines}, response_text=f"Git status has {len(lines)} visible changed entries, Sir.")
        if action == "git_branch":
            proc = _git(["branch", "--show-current"])
            branch = proc.stdout.strip() or "unknown"
            return tool_response(ok=proc.returncode == 0, tool="coding", action=action, executed=True, result={"branch": branch}, response_text=f"You are on branch {branch}, Sir.")
        if action == "list_project_files":
            files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts][:80]
            return tool_response(ok=True, tool="coding", action=action, executed=True, result={"files": files}, response_text=f"I found {len(files)} project files in the first scan, Sir.")
        if action == "open_project_in_vscode":
            return tool_response(ok=True, tool="coding", action=action, executed=False, result={"frontend_action": {"type": "app.open", "label": "vscode", "value": "vscode"}}, response_text="Opening this project in VS Code, Sir.")
        if action == "npm_build_status_command_preview":
            return tool_response(ok=True, tool="coding", action=action, executed=True, result={"command": "cd jarvis\\web; npm run build"}, response_text="The build command is ready, Sir.")
        if action == "python_compile_command_preview":
            return tool_response(ok=True, tool="coding", action=action, executed=True, result={"command": ".\\.venv\\Scripts\\python.exe -m py_compile jarvis\\main.py jarvis\\diagnostics.py"}, response_text="The Python compile command is ready, Sir.")
        if action in {"git_commit", "git_push", "run_npm_build", "run_python_compile"}:
            return confirmation_required("coding", action)
    except Exception as exc:
        return tool_response(ok=False, tool="coding", action=action, response_text="Coding helper failed locally, Sir.", error_type="coding_tool_failed", error_message=str(exc)[:160])
    return tool_response(ok=False, tool="coding", action=action, response_text="That coding helper action is not available yet, Sir.", error_type="unknown_action")
