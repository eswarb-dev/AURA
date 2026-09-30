"""Browser task window registry - runtime-only tracking of AURA browser tasks.

This is browser session state only. It is NOT persisted to Supabase or AURA memory.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BrowserTask:
    browser_task_id: str
    target_id: str = ""
    page_id: str = ""
    window_id: str = ""
    current_url: str = ""
    created_at: float = 0.0
    last_used_at: float = 0.0
    window_state: str = "normal"
    window_bounds: dict[str, int] | None = None


class TaskWindowRegistry:
    """Runtime-only registry mapping browser_task_id -> page/target/window."""

    _instance: TaskWindowRegistry | None = None

    def __new__(cls) -> TaskWindowRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tasks: dict[str, BrowserTask] = {}
        return cls._instance

    def create(self, target_id: str = "", page_id: str = "", window_id: str = "", url: str = "") -> BrowserTask:
        task = BrowserTask(
            browser_task_id=str(uuid.uuid4())[:12],
            target_id=target_id,
            page_id=page_id,
            window_id=window_id,
            current_url=url,
            created_at=time.time(),
            last_used_at=time.time(),
        )
        self._tasks[task.browser_task_id] = task
        return task

    def get(self, browser_task_id: str) -> BrowserTask | None:
        task = self._tasks.get(browser_task_id)
        if task:
            task.last_used_at = time.time()
        return task

    def remove(self, browser_task_id: str) -> bool:
        if browser_task_id in self._tasks:
            del self._tasks[browser_task_id]
            return True
        return False

    def cleanup_stale(self, max_age_seconds: float = 3600.0) -> int:
        now = time.time()
        stale = [
            bid for bid, t in self._tasks.items()
            if now - t.last_used_at > max_age_seconds
        ]
        for bid in stale:
            del self._tasks[bid]
        return len(stale)

    def get_active(self) -> list[BrowserTask]:
        return sorted(self._tasks.values(), key=lambda t: t.last_used_at, reverse=True)

    def clear(self) -> None:
        self._tasks.clear()


_task_registry: TaskWindowRegistry | None = None


def get_task_registry() -> TaskWindowRegistry:
    global _task_registry
    if _task_registry is None:
        _task_registry = TaskWindowRegistry()
    return _task_registry


def create_task(target_id: str = "", page_id: str = "", window_id: str = "", url: str = "") -> BrowserTask:
    return get_task_registry().create(target_id, page_id, window_id, url)


def get_task(browser_task_id: str) -> BrowserTask | None:
    return get_task_registry().get(browser_task_id)


def remove_task(browser_task_id: str) -> bool:
    return get_task_registry().remove(browser_task_id)
