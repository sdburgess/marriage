"""Main loop. Decides when to poll, calls the scraper, dispatches
notifications, and triggers the booker for matching slots."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

import pytz
import structlog

from .booker import Booker, BookResult
from .config import Config, ReleaseWindow, Secrets, Target
from .notify import Notice, Notifier
from .scraper import Scraper, Slot
from .state import State

log = structlog.get_logger()
NYC = pytz.timezone("America/New_York")

# Burst polling kicks in from -1m to +30m around each release window.
BURST_LEAD = timedelta(minutes=1)
BURST_TAIL = timedelta(minutes=30)

_DOW_INDEX = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def _prioritize(slots: list[Slot], target: Target) -> list[Slot]:
    """Order slots so the booker tries the preferred_date first (and the
    preferred_time within it), then falls back to other matching dates."""
    if target.preferred_only and target.preferred_date:
        slots = [s for s in slots if s.start.date() == target.preferred_date]

    def sort_key(s: Slot) -> tuple:
        is_preferred_day = (
            target.preferred_date is not None
            and s.start.date() == target.preferred_date
        )
        time_distance = 0
        if target.preferred_time:
            pref_minutes = target.preferred_time.hour * 60 + target.preferred_time.minute
            slot_minutes = s.start.hour * 60 + s.start.minute
            time_distance = abs(pref_minutes - slot_minutes)
        # Lower tuple = higher priority. Negate is_preferred_day so True wins.
        return (not is_preferred_day, time_distance, s.start)

    return sorted(slots, key=sort_key)


def _next_window_for(now: datetime, w: ReleaseWindow) -> datetime:
    """Return the next datetime (NYC tz) at which window ``w`` starts."""
    target_dow = _DOW_INDEX[w.dow]
    days_ahead = (target_dow - now.weekday()) % 7
    candidate = now.replace(
        hour=w.time.hour, minute=w.time.minute, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def in_burst_window(cfg: Config, kind: str, now: datetime | None = None) -> bool:
    """True if we're inside the burst polling window for ``kind``."""
    now = now or datetime.now(NYC)
    if now.tzinfo is None:
        now = NYC.localize(now)
    for w in cfg.polling.release_windows:
        if kind not in w.kinds:
            continue
        next_start = _next_window_for(now, w)
        # Also consider the most recent past one (we may be inside the tail).
        prev_start = next_start - timedelta(days=7)
        for start in (prev_start, next_start):
            if start - BURST_LEAD <= now <= start + BURST_TAIL:
                return True
    return False


class Scheduler:
    def __init__(self, cfg: Config, secrets: Secrets, state: State):
        self.cfg = cfg
        self.secrets = secrets
        self.state = state
        self.notifier = Notifier(cfg, secrets)
        self.booker = Booker(cfg, secrets)

    async def run_forever(self) -> None:
        log.info("scheduler.start", targets=len(self.cfg.targets))
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.error("scheduler.tick_failed", error=str(e))
            await asyncio.sleep(self._sleep_seconds())

    def _sleep_seconds(self) -> int:
        p = self.cfg.polling
        if p.constant_mode:
            base = p.base_interval_seconds
            jitter = random.randint(-p.jitter_seconds, p.jitter_seconds)
            return max(5, base + jitter)
        # Window-based mode: burst near release times, base otherwise.
        for t in self.cfg.targets:
            if self._target_done(t):
                continue
            if in_burst_window(self.cfg, t.kind):
                return p.burst_interval_seconds
        return p.base_interval_seconds

    def _target_done(self, t: Target) -> bool:
        return self.state.already_booked(t.kind) is not None and t.auto_book

    async def tick(self) -> None:
        async with Scraper(headless=True) as scraper:
            for target in self.cfg.targets:
                if self._target_done(target):
                    continue
                slots = await scraper.find_available(target)
                await self._handle_slots(target, slots)
        self.state._data["last_run_at"] = datetime.now(NYC).isoformat()
        self.state.save()

    async def _handle_slots(self, target: Target, slots: list[Slot]) -> None:
        if not slots:
            return

        # Notification dedup is independent of booking attempts. A slot
        # that we lost the race on may still be visible next tick -- we
        # want to keep retrying booking, but not re-text the humans.
        unannounced = [s for s in slots if s.key() not in self.state.seen_slots]
        if unannounced:
            for s in unannounced:
                self.state.mark_seen(s.key())
            self._notify_availability(target, unannounced)

        if not target.auto_book:
            return

        # Order slots by booking priority: preferred_date first, then
        # earliest start. If preferred_only is set, drop everything else.
        ordered = _prioritize(slots, target)
        if not ordered:
            return

        for s in ordered:
            if self.state.already_booked(target.kind):
                return
            log.info("scheduler.attempt_book", slot=s.key())
            result = await self.booker.book(s, target)
            await self._handle_book_result(target, s, result)
            if result.success or result.held_for_human:
                return

    def _notify_availability(self, target: Target, slots: list[Slot]) -> None:
        first = slots[0]
        url = (
            "https://clerkscheduler.cityofnewyork.us/s/MarriageCeremony"
            if target.kind == "ceremony"
            else "https://clerkscheduler.cityofnewyork.us/s/MarriageLicense"
        )
        # Highlight if any of the new slots match the preferred_date
        preferred_hits = [
            s for s in slots if target.preferred_date and s.start.date() == target.preferred_date
        ]
        prefix = "PREFERRED DATE OPENED" if preferred_hits else f"{target.kind.title()} slot"
        head = preferred_hits[0] if preferred_hits else first
        self.notifier.send(
            Notice(
                subject=f"{prefix}: {head.start:%a %b %-d, %-I:%M %p} ({head.borough})",
                body=(
                    f"Found {len(slots)} available {target.kind} slot(s).\n"
                    + "\n".join(
                        f"- {s.start:%a %b %-d %Y, %-I:%M %p} ({s.borough})"
                        for s in slots[:10]
                    )
                ),
                url=url,
            )
        )

    async def _handle_book_result(
        self, target: Target, slot: Slot, result: BookResult
    ) -> None:
        if result.success and result.confirmation_number:
            self.state.record_booking(
                kind=target.kind,
                slot_dt=slot.start.isoformat(),
                confirmation=result.confirmation_number,
            )
            self.state.save()
            self.notifier.send(
                Notice(
                    subject=f"BOOKED {target.kind}: {slot.start:%a %b %-d, %-I:%M %p}",
                    body=(
                        f"Confirmation: {result.confirmation_number}\n"
                        f"Borough: {slot.borough}\n"
                        f"Time: {slot.start:%A %B %-d, %Y at %-I:%M %p}"
                    ),
                )
            )
        elif result.held_for_human:
            self.notifier.send(
                Notice(
                    subject=f"ACTION NEEDED: {target.kind} held at submit step",
                    body=(
                        f"{result.detail}\n"
                        f"Slot: {slot.start:%A %B %-d, %Y at %-I:%M %p} ({slot.borough})\n"
                        "Open the city site and finalize manually."
                    ),
                    url="https://clerkscheduler.cityofnewyork.us/s/MarriageCeremony",
                )
            )
        else:
            log.warning("scheduler.book_failed", detail=result.detail, slot=slot.key())
