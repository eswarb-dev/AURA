"""AURA Chrome MCP browser execution layer.

Provides Chrome DevTools Protocol control via chrome-devtools-mcp,
enabling separate-window browser tasks with deterministic execution.
"""
from __future__ import annotations

from .task_registry import BrowserTask, TaskWindowRegistry, create_task, remove_task, get_task, get_task_registry
from .browser_executor import BrowserMCPExecutor, get_browser_executor, get_browser_state, MCPState
from .chrome_devtools import ChromeDevToolsProtocol, create_browser_task_window, get_debug_endpoint

__all__ = [
    "BrowserTask",
    "TaskWindowRegistry",
    "create_task",
    "remove_task",
    "get_task",
    "get_task_registry",
    "BrowserMCPExecutor",
    "get_browser_executor",
    "get_browser_state",
    "MCPState",
    "ChromeDevToolsProtocol",
    "create_browser_task_window",
    "get_debug_endpoint",
]
