"""Shared coordinator between the scheduler loop and the web/Slack
handlers. All instances live in the same process so this is just a
plain object guarded by an asyncio lock where it matters.

Concretely it owns:

* the live ``Config`` (so the dashboard can edit it without restarting)
* a ``paused`` flag the scheduler honors
* a per-tick activity log the dashboard surfaces
* a registry of pending Slack-confirm slots that the /slack/actions
  webhook resolves
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config, Secrets, Target, load_config
from .scraper import Slot
from .state import State


@dataclass
class PendingConfirm:
    """A slot waiting for a Slack Book/Skip click."""

    callback_id: str
    slot: Slot
    target: Target
    created_at: float                      # monotonic
    timeout_seconds: int
    decision: asyncio.Future                # resolves to "book" | "skip" | "timeout"


@dataclass
class ActivityEntry:
    when: datetime
    level: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)


class Controller:
    def __init__(self, cfg: Config, secrets: Secrets, state: State, config_path: str):
        self.cfg = cfg
        self.secrets = secrets
        self.state = state
        self.config_path = config_path

        self.paused: bool = False
        self.activity: deque[ActivityEntry] = deque(maxlen=200)
        self._pending: dict[str, PendingConfirm] = {}
        self._lock = asyncio.Lock()

    # ---- activity log ----
    def log_activity(self, level: str, message: str, **extra: Any) -> None:
        self.activity.appendleft(
            ActivityEntry(
                when=datetime.now(timezone.utc),
                level=level,
                message=message,
                extra=extra,
            )
        )

    # ---- config reload ----
    async def reload_config(self) -> Config:
        async with self._lock:
            self.cfg = load_config(self.config_path)
            self.log_activity("info", "config reloaded")
            return self.cfg

    # ---- pause / resume ----
    def pause(self) -> None:
        self.paused = True
        self.log_activity("info", "scheduler paused")

    def resume(self) -> None:
        self.paused = False
        self.log_activity("info", "scheduler resumed")

    # ---- pending Slack confirmations ----
    def register_pending(
        self, slot: Slot, target: Target, timeout_seconds: int
    ) -> PendingConfirm:
        callback_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        pc = PendingConfirm(
            callback_id=callback_id,
            slot=slot,
            target=target,
            created_at=time.monotonic(),
            timeout_seconds=timeout_seconds,
            decision=loop.create_future(),
        )
        self._pending[callback_id] = pc
        return pc

    def get_pending(self, callback_id: str) -> PendingConfirm | None:
        return self._pending.get(callback_id)

    def resolve_pending(self, callback_id: str, decision: str) -> bool:
        pc = self._pending.pop(callback_id, None)
        if not pc:
            return False
        if not pc.decision.done():
            pc.decision.set_result(decision)
        return True

    def list_pending(self) -> list[PendingConfirm]:
        return list(self._pending.values())
