"""Local system monitor tool."""
from __future__ import annotations

import shutil
import socket
import subprocess
from typing import Any

from .tool_types import tool_response


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:
        return None


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    psutil = _psutil()
    try:
        if action == "battery_status":
            if psutil and hasattr(psutil, "sensors_battery"):
                battery = psutil.sensors_battery()
                if battery:
                    pct = round(float(battery.percent))
                    plugged = "charging" if battery.power_plugged else "on battery"
                    return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"percent": pct, "plugged": battery.power_plugged}, response_text=f"Battery is at {pct} percent and {plugged}, Sir.")
            return tool_response(ok=False, tool="system_monitor", action=action, response_text="Battery status is not available on this device, Sir.", error_type="battery_unavailable")
        if action == "cpu_ram_status":
            if psutil:
                cpu = round(float(psutil.cpu_percent(interval=0.2)))
                ram = round(float(psutil.virtual_memory().percent))
                return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"cpu_percent": cpu, "ram_percent": ram}, response_text=f"CPU is at {cpu} percent and RAM is at {ram} percent, Sir.")
            return tool_response(ok=False, tool="system_monitor", action=action, executed=False, response_text="I could not read system usage right now, Sir.", error_type="missing_dependency", error_message="System monitor dependency is missing: psutil")
        if action == "disk_status":
            usage = shutil.disk_usage(str((context or {}).get("root") or "."))
            free_gb = round(usage.free / (1024 ** 3), 1)
            total_gb = round(usage.total / (1024 ** 3), 1)
            return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"free_gb": free_gb, "total_gb": total_gb}, response_text=f"Disk has {free_gb} GB free out of {total_gb} GB, Sir.")
        if action == "network_status":
            host = socket.gethostname()
            return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"hostname": host}, response_text=f"Network adapter check is available locally on {host}, Sir.")
        if action == "process_summary":
            if psutil:
                names = sorted({p.info.get("name") for p in psutil.process_iter(["name"]) if p.info.get("name")})[:8]
                return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"sample": names}, response_text=f"I found running apps including {', '.join(names[:5])}, Sir.")
            process = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Process | Select-Object -First 8 -ExpandProperty ProcessName"], capture_output=True, text=True, timeout=5, check=False)
            names = [x.strip() for x in process.stdout.splitlines() if x.strip()][:8]
            return tool_response(ok=True, tool="system_monitor", action=action, executed=True, result={"sample": names}, response_text=f"I found running apps including {', '.join(names[:5])}, Sir.")
    except Exception as exc:
        return tool_response(ok=False, tool="system_monitor", action=action, response_text="I could not read system status right now, Sir.", error_type="system_monitor_failed", error_message=str(exc)[:160])
    return tool_response(ok=False, tool="system_monitor", action=action, response_text="That system monitor action is not available yet, Sir.", error_type="unknown_action")
