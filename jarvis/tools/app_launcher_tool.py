"""Allowlisted app and workspace launcher."""
from __future__ import annotations

import os

from .tool_types import tool_response

ALLOWED_APPS = {"notepad", "calculator", "chrome", "vscode", "powershell", "file_explorer"}
PRESETS = {"coding", "aura_project", "college_work", "design"}


def execute(action: str, params: dict | None = None, context: dict | None = None) -> dict:
    params = params or {}
    if action == "open_app":
        app = str(params.get("app") or "").lower().replace("vs code", "vscode").strip()
        if app not in ALLOWED_APPS:
            return tool_response(ok=False, tool="app_launcher", action=action, response_text="That app is not in my safe launcher list yet, Sir.", error_type="app_not_allowlisted")
        return tool_response(ok=True, tool="app_launcher", action=action, executed=False, result={"frontend_action": {"type": "app.open", "label": app, "value": app}}, response_text=f"Opening {app}, Sir.")
    if action == "open_folder":
        folder = str(params.get("folder") or os.getenv("AURA_PROJECT_ROOT") or ((context or {}).get("root")) or "").strip()
        return tool_response(ok=True, tool="app_launcher", action=action, executed=False, result={"frontend_action": {"type": "folder.open", "label": "Open folder", "value": folder}}, response_text="Opening the folder, Sir.")
    if action == "open_workspace_preset":
        preset = str(params.get("preset") or "").lower().strip()
        if preset not in PRESETS:
            return tool_response(ok=False, tool="app_launcher", action=action, response_text="That workspace preset is not configured yet, Sir.", error_type="preset_not_configured")
        return tool_response(ok=True, tool="app_launcher", action=action, executed=False, result={"frontend_action": {"type": "app.open", "label": preset, "value": preset}}, response_text=f"Opening {preset.replace('_', ' ')} mode, Sir.")
    return tool_response(ok=False, tool="app_launcher", action=action, response_text="That launcher action is not available yet, Sir.", error_type="unknown_action")
