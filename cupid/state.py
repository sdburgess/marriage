"""Tiny JSON-on-disk state store. Tracks seen slots and bookings so we
don't double-notify or double-book."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("STATE_PATH", "state.json"))
        self._data: dict[str, Any] = {
            "seen_slots": [],          # list of stable slot keys we've already notified on
            "bookings": [],            # list of dicts: kind, datetime, confirmation_number, booked_at
            "last_run_at": None,
        }
        if self.path.exists():
            try:
                self._data.update(json.loads(self.path.read_text()))
            except json.JSONDecodeError:
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, default=str))
        tmp.replace(self.path)

    @property
    def seen_slots(self) -> set[str]:
        return set(self._data["seen_slots"])

    def mark_seen(self, key: str) -> None:
        if key not in self._data["seen_slots"]:
            self._data["seen_slots"].append(key)
            # Cap so this doesn't grow forever.
            self._data["seen_slots"] = self._data["seen_slots"][-2000:]

    def already_booked(self, kind: str) -> dict | None:
        for b in self._data["bookings"]:
            if b.get("kind") == kind:
                return b
        return None

    def record_booking(self, *, kind: str, slot_dt: str, confirmation: str) -> None:
        from datetime import datetime, timezone

        self._data["bookings"].append({
            "kind": kind,
            "datetime": slot_dt,
            "confirmation_number": confirmation,
            "booked_at": datetime.now(timezone.utc).isoformat(),
        })

    def license_confirmation(self) -> str | None:
        b = self.already_booked("license")
        return b["confirmation_number"] if b else None
