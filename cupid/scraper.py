"""Playwright-driven reader for the NYC City Clerk scheduler.

The scheduler is a Salesforce Experience Cloud SPA at
https://clerkscheduler.cityofnewyork.us/s/MarriageLicense .

Salesforce sites mutate their internal Aura/LWC structure occasionally,
so this module is intentionally written so a single recording session can
update the SELECTORS dict without touching control flow elsewhere.

Run `python -m cupid record` to open a real browser, walk the booking
flow yourself, and have observed selectors logged to stderr for review.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .config import Borough, Kind, Mode, Target

log = structlog.get_logger()

BASE_URL = "https://clerkscheduler.cityofnewyork.us"
LICENSE_URL = f"{BASE_URL}/s/MarriageLicense"
CEREMONY_URL = f"{BASE_URL}/s/MarriageCeremony"

# Selectors live here so a recording session can update them in one place.
# Where you see ROLE/NAME pairs, prefer Playwright's get_by_role/get_by_label
# which are resilient to Salesforce's dynamic class names. The strings below
# are starting guesses observed in past Salesforce Experience flows; verify
# them with `python -m cupid record` before trusting auto-book.
SELECTORS: dict[str, str] = {
    # Top-level service / mode selection on the landing page
    "service_dropdown_label": "What service would you like to schedule?",
    "service_in_person_license": "In-Person Marriage License Appointment",
    "service_virtual_license": "Project Cupid Virtual Marriage License",
    "service_in_person_ceremony": "In-Person Marriage Ceremony Appointment",
    "borough_dropdown_label": "Select Office",
    "next_button_text": "Next",
    # Calendar grid -- Salesforce uses a custom calendar with role=button per day
    "calendar_day_role": "button",
    "available_day_class_substring": "slds-is-selectable",
    "time_slot_role": "radio",
    # Confirmation / form
    "first_name_label": "First Name",
    "last_name_label": "Last Name",
    "email_label": "Email",
    "phone_label": "Phone",
    "submit_button_text": "Submit",
    "confirmation_number_regex": r"Confirmation\s*(?:Number|#)\s*:?\s*([A-Z0-9-]{6,})",
    # CAPTCHA detection -- if any of these appear on the page, fall back to
    # "drive to confirmation, human submits" rather than auto-clicking submit.
    "captcha_iframe_substrings": [
        "recaptcha",
        "hcaptcha",
        "turnstile",
        "captcha",
    ],
}


@dataclass(frozen=True)
class Slot:
    kind: Kind
    mode: Mode
    borough: Borough
    start: datetime          # local NYC time
    raw_label: str           # whatever the page rendered, useful for logs

    def key(self) -> str:
        return f"{self.kind}|{self.mode}|{self.borough}|{self.start.isoformat()}"


def _service_selector(target: Target) -> str:
    if target.kind == "license" and target.mode == "in_person":
        return SELECTORS["service_in_person_license"]
    if target.kind == "license" and target.mode == "virtual":
        return SELECTORS["service_virtual_license"]
    if target.kind == "ceremony":
        return SELECTORS["service_in_person_ceremony"]
    raise ValueError(f"unsupported target: {target.kind}/{target.mode}")


class Scraper:
    """Async context-managed Playwright wrapper.

    Use as:
        async with Scraper(headless=True) as s:
            slots = await s.find_available(target)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        slow_mo_ms: int = 0,
        storage_state: str | None = None,
    ):
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.storage_state = storage_state
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None

    async def __aenter__(self) -> "Scraper":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo_ms
        )
        self._ctx = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            storage_state=self.storage_state,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _new_page(self) -> Page:
        assert self._ctx
        page = await self._ctx.new_page()
        # Salesforce loads a lot. Be patient.
        page.set_default_timeout(20_000)
        return page

    async def find_available(self, target: Target) -> list[Slot]:
        """Walk the scheduler far enough to enumerate visible time slots
        for `target` within its date window. Does not click submit."""
        page = await self._new_page()
        try:
            url = LICENSE_URL if target.kind == "license" else CEREMONY_URL
            await page.goto(url, wait_until="networkidle")

            await self._select_service(page, target)
            slots: list[Slot] = []
            for borough in target.boroughs:
                slots.extend(await self._read_borough(page, target, borough))
            return [s for s in slots if _slot_matches(s, target)]
        finally:
            await page.close()

    # ----- private helpers -----

    async def _select_service(self, page: Page, target: Target) -> None:
        """Pick the right service from the landing dropdown.

        Salesforce renders this as a custom listbox, not a native <select>.
        We rely on visible text. Adjust SELECTORS if recording shows
        different copy.
        """
        label = _service_selector(target)
        # Try clicking the dropdown then the option. If the page already
        # routes service selection differently, this is the first thing
        # that needs updating from a recording session.
        try:
            await page.get_by_text(SELECTORS["service_dropdown_label"]).first.click()
        except Exception:
            log.debug("scraper.no_service_dropdown_visible")
        await page.get_by_text(label, exact=False).first.click()
        await self._click_next(page)

    async def _read_borough(
        self, page: Page, target: Target, borough: Borough
    ) -> list[Slot]:
        # Pick borough
        try:
            await page.get_by_text(SELECTORS["borough_dropdown_label"]).first.click()
        except Exception:
            log.debug("scraper.no_borough_dropdown")
        await page.get_by_text(borough, exact=True).first.click()
        await self._click_next(page)

        # Read every available day in the calendar that falls in the target window.
        # Salesforce renders day cells as <button> with classes like
        # `slds-day` and `slds-is-selectable` for available days.
        slots: list[Slot] = []
        async for day_label in self._iter_available_days(page, target):
            day_btn = page.get_by_role(
                "button", name=day_label, exact=False
            ).first
            await day_btn.click()
            # After clicking a day, the time radio group renders. Read it.
            try:
                await page.wait_for_selector(
                    f"[role='{SELECTORS['time_slot_role']}']", timeout=5_000
                )
            except Exception:
                continue
            time_buttons = await page.get_by_role(
                SELECTORS["time_slot_role"]
            ).all()
            for tb in time_buttons:
                label_text = (await tb.get_attribute("aria-label")) or (
                    await tb.text_content()
                ) or ""
                start_dt = _parse_slot_datetime(day_label, label_text)
                if not start_dt:
                    continue
                slots.append(
                    Slot(
                        kind=target.kind,
                        mode=target.mode,
                        borough=borough,
                        start=start_dt,
                        raw_label=label_text.strip(),
                    )
                )
        return slots

    async def _iter_available_days(
        self, page: Page, target: Target
    ) -> Iterable[str]:
        """Yield aria-labels of day buttons that look available within window.

        We page through months until we go past target.latest_date. Each
        month, we collect every day button whose class contains
        ``slds-is-selectable`` and whose label parses inside the window.
        """
        next_month_btn = page.get_by_role("button", name=re.compile(r"next month", re.I))
        seen_months: set[str] = set()
        for _ in range(18):  # 18 months is more than enough
            # Read the month header to detect when we've paged past the window.
            header = await page.get_by_role("heading").first.text_content() or ""
            if header in seen_months:
                break
            seen_months.add(header)

            buttons = await page.locator(
                f"button.{SELECTORS['available_day_class_substring']}"
            ).all()
            for b in buttons:
                label = await b.get_attribute("aria-label")
                if not label:
                    continue
                d = _parse_day_label(label)
                if not d:
                    continue
                if target.earliest_date <= d <= target.latest_date:
                    if d.strftime("%a")[:3] in target.weekdays:
                        yield label

            # If we're already past latest_date in this month, stop.
            if _month_past(header, target.latest_date):
                break
            try:
                await next_month_btn.click()
                await page.wait_for_timeout(300)
            except Exception:
                break

    async def _click_next(self, page: Page) -> None:
        try:
            await page.get_by_role(
                "button", name=SELECTORS["next_button_text"]
            ).first.click()
            await page.wait_for_load_state("networkidle")
        except Exception:
            # Some flows skip Next when only one option exists; ignore.
            pass

    async def captcha_present(self, page: Page) -> bool:
        html = await page.content()
        return any(s in html.lower() for s in SELECTORS["captcha_iframe_substrings"])


# ---- pure helpers (testable without a browser) ----


_DAY_LABEL_RE = re.compile(
    r"(?P<dow>\w+),?\s+(?P<month>\w+)\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
)


def _parse_day_label(label: str) -> date | None:
    """Parse aria-label like 'Tuesday, June 9, 2026'."""
    m = _DAY_LABEL_RE.search(label)
    if not m:
        return None
    try:
        return datetime.strptime(
            f"{m['month']} {m['day']} {m['year']}", "%B %d %Y"
        ).date()
    except ValueError:
        return None


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)


def _parse_slot_datetime(day_label: str, time_label: str) -> datetime | None:
    d = _parse_day_label(day_label)
    if not d:
        return None
    m = _TIME_RE.search(time_label)
    if not m:
        return None
    h = int(m.group(1))
    minute = int(m.group(2))
    ampm = (m.group(3) or "").upper()
    if ampm == "PM" and h < 12:
        h += 12
    if ampm == "AM" and h == 12:
        h = 0
    return datetime.combine(d, time(h, minute))


def _month_past(header: str, latest: date) -> bool:
    """True if calendar header is later than the month containing `latest`."""
    try:
        # Header is usually "June 2026" or similar.
        h = datetime.strptime(header.strip(), "%B %Y").date()
    except ValueError:
        return False
    return (h.year, h.month) > (latest.year, latest.month)


def _slot_matches(slot: Slot, target: Target) -> bool:
    if not (target.earliest_date <= slot.start.date() <= target.latest_date):
        return False
    t = slot.start.time()
    if not (target.earliest_time <= t <= target.latest_time):
        return False
    if slot.start.strftime("%a")[:3] not in target.weekdays:
        return False
    return True


# ---- CLI helper for recording sessions ----


async def record_session(target_kind: Kind = "license") -> None:
    """Open a non-headless browser so you can walk the flow manually
    while we log every clicked element and emitted XHR. Use this to
    update the SELECTORS dict above."""
    url = LICENSE_URL if target_kind == "license" else CEREMONY_URL
    print(f"Opening {url} -- walk through the booking flow.")
    print("Selector hints will be printed as you click. Ctrl-C to exit.")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def on_request(req):
            if req.resource_type in {"xhr", "fetch"}:
                print(f"XHR {req.method} {req.url}")

        page.on("request", on_request)

        await page.goto(url)
        await page.evaluate(
            """
            document.addEventListener('click', (e) => {
                const el = e.target;
                const desc = [
                    el.tagName,
                    el.getAttribute('role'),
                    el.getAttribute('aria-label'),
                    el.id,
                    el.className,
                    (el.textContent || '').trim().slice(0, 60),
                ].filter(Boolean).join(' | ');
                console.log('CLICK', desc);
            }, true);
            """
        )
        page.on("console", lambda msg: print(msg.text))
        # Block forever until user closes browser.
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        await ctx.close()
        await browser.close()


def run_record(target_kind: Kind = "license") -> None:
    asyncio.run(record_session(target_kind))
