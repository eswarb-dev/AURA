"""Safe local command action descriptions for A.U.R.A."""
from __future__ import annotations

from .tool_types import blocked, confirmation_required, tool_response

SAFE_ACTIONS = {
    "volume_up": ("volume.up", "Volume up, Sir."),
    "volume_down": ("volume.down", "Volume down, Sir."),
    "volume_mute": ("volume.mute", "Volume muted, Sir."),
    "brightness_info": (None, "I can show brightness information, Sir."),
    "lock_laptop": ("laptop.lock", "Locking the laptop, Sir."),
    "shutdown_laptop": (None, "Shutting down the laptop, Sir."),
    "restart_laptop": (None, "Restarting the laptop, Sir."),
    "clean_temp_files": (None, "Cleaning temporary files, Sir."),
    "close_app": (None, "Closing the app, Sir."),
    "system_status": (None, "Checking system status, Sir."),
    "open_app": (None, "Opening the app, Sir."),
    "screenshot": (None, "Taking a screenshot, Sir."),
    "read_clipboard": (None, "Reading clipboard, Sir."),
    "list_reminders": (None, "Listing reminders, Sir."),
    "create_reminder": (None, "Creating a reminder, Sir."),
    "battery_status": (None, "Checking battery status, Sir."),
    "cpu_ram_status": (None, "Checking CPU and RAM, Sir."),
    "disk_status": (None, "Checking disk status, Sir."),
    "network_status": (None, "Checking network status, Sir."),
    "process_summary": (None, "Listing running processes, Sir."),
}
CONFIRM_ACTIONS = {"shutdown_laptop", "restart_laptop", "close_app", "kill_process", "clean_temp_files", "delete_reminder"}
BLOCKED_ACTIONS = {"arbitrary_shell", "delete_personal_files", "format_drive", "expose_secrets"}


def execute(action: str, params: dict | None = None, context: dict | None = None) -> dict:
    if action in BLOCKED_ACTIONS:
        return blocked("local_command", action)
    if action in CONFIRM_ACTIONS:
        return confirmation_required("local_command", action)
    if action not in SAFE_ACTIONS:
        return blocked("local_command", action, "That local command is not in my safe command list yet, Sir.")
    action_type, text = SAFE_ACTIONS[action]
    result = {"frontend_action": {"type": action_type, "label": text} if action_type else None}
    return tool_response(ok=True, tool="local_command", action=action, executed=action_type is None, result=result, response_text=text)
