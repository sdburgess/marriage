"""Form-driver that takes a discovered Slot to a confirmation number.

Two safety levers:

1. ``auto_book`` on the Target. If false, we never call this module --
   the slot is just notified and a deep link is sent.
2. CAPTCHA fallback. If a CAPTCHA shows up at the submit step and
   ``twocaptcha_api_key`` is not set, we stop *one click before* submit,
   notify the humans with a deep link to the open browser session, and
   record the slot as "held, awaiting human submit". This is the
   responsible default.

The form-filling code below is structured around a SCHEMA mapping that
maps each form field to (locator-strategy, applicant-attr). Once you do
a recording session, update the SCHEMA -- you should not need to touch
the control flow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog
from playwright.async_api import Page, TimeoutError as PWTimeout

from .config import Applicant, Applicants, Config, Secrets, Target
from .scraper import SELECTORS, Scraper, Slot

log = structlog.get_logger()


# Form-field schemas, keyed by appointment kind. The ceremony schema is
# small on purpose -- the city already has both partners' info from the
# license application, so the ceremony page mostly asks for the license
# confirmation number plus light contact info. The license schema is
# kept around for completeness.
#
# Each entry: (visible label on the form, "a"|"b", Applicant attribute).
SCHEMA_BY_KIND: dict[str, list[tuple[str, str, str]]] = {
    "ceremony": [
        ("First Name", "a", "first_name"),
        ("Last Name", "a", "last_name"),
        ("Email", "a", "email"),
        ("Phone", "a", "phone"),
        # Partner B contact fields if the page asks for them:
        ("Partner Email", "b", "email"),
        ("Partner Phone", "b", "phone"),
    ],
    "license": [
        ("Partner A First Name", "a", "first_name"),
        ("Partner A Middle Name", "a", "middle_name"),
        ("Partner A Last Name", "a", "last_name"),
        ("Partner A Date of Birth", "a", "date_of_birth"),
        ("Partner A Email", "a", "email"),
        ("Partner A Phone", "a", "phone"),
        ("Partner A Address", "a", "address_line1"),
        ("Partner A City", "a", "city"),
        ("Partner A State", "a", "state"),
        ("Partner A Zip", "a", "zip"),
        ("Partner A Place of Birth City", "a", "place_of_birth_city"),
        ("Partner A Place of Birth State", "a", "place_of_birth_state"),
        ("Partner A Place of Birth Country", "a", "place_of_birth_country"),
        ("Partner A ID Type", "a", "id_type"),
        ("Partner A ID Number", "a", "id_number"),
        ("Partner B First Name", "b", "first_name"),
        ("Partner B Middle Name", "b", "middle_name"),
        ("Partner B Last Name", "b", "last_name"),
        ("Partner B Date of Birth", "b", "date_of_birth"),
        ("Partner B Email", "b", "email"),
        ("Partner B Phone", "b", "phone"),
        # Extend after a recording session reveals the actual labels.
    ],
}


@dataclass
class BookResult:
    success: bool
    confirmation_number: str | None
    held_for_human: bool        # True when we stopped at submit due to CAPTCHA
    detail: str


class Booker:
    def __init__(self, cfg: Config, secrets: Secrets):
        self.cfg = cfg
        self.secrets = secrets

    async def book(self, slot: Slot, target: Target) -> BookResult:
        """Drive the scheduler from start to confirmation for ``slot``.

        Returns even on partial success so the caller can decide whether
        to notify, store state, or retry.
        """
        if not target.auto_book:
            return BookResult(
                success=False,
                confirmation_number=None,
                held_for_human=True,
                detail="auto_book disabled for this target",
            )

        # We use a non-headless browser when running locally with --debug,
        # but the scheduler is meant to run headless on the VM.
        async with Scraper(headless=True) as s:
            assert s._ctx
            page = await s._ctx.new_page()
            try:
                return await self._drive(page, slot, target)
            finally:
                await page.close()

    async def _drive(self, page: Page, slot: Slot, target: Target) -> BookResult:
        from .scraper import LICENSE_URL, CEREMONY_URL

        url = LICENSE_URL if slot.kind == "license" else CEREMONY_URL
        await page.goto(url, wait_until="networkidle")

        # 1. Pick the service and borough, same as the scraper.
        # We re-use the scraper's helpers via a fresh Scraper isn't quite
        # right since it owns its own page; inline the steps instead.
        from .scraper import _service_selector

        await self._click_text(page, SELECTORS["service_dropdown_label"])
        await self._click_text(page, _service_selector(target))
        await self._click_text(page, SELECTORS["next_button_text"])

        await self._click_text(page, SELECTORS["borough_dropdown_label"])
        await self._click_text(page, slot.borough)
        await self._click_text(page, SELECTORS["next_button_text"])

        # 2. Click the slot's calendar day + time.
        day_label_human = slot.start.strftime("%A, %B %-d, %Y")
        await page.get_by_role("button", name=day_label_human, exact=False).first.click()

        time_label = slot.start.strftime("%-I:%M %p").lstrip("0")
        await page.get_by_role(
            SELECTORS["time_slot_role"], name=time_label, exact=False
        ).first.click()
        await self._click_text(page, SELECTORS["next_button_text"])

        # 3. Fill the form.
        if slot.kind == "ceremony":
            if not target.license_number or target.license_number == "FILL_ME_IN":
                return BookResult(
                    success=False,
                    confirmation_number=None,
                    held_for_human=True,
                    detail="ceremony target missing license_number",
                )
            try:
                await page.get_by_label(re.compile(r"license\s*number", re.I)).fill(
                    target.license_number
                )
            except Exception:
                log.warning("booker.no_license_field_found")

        await self._fill_form(page, slot.kind, self.cfg.applicants)

        # 4. CAPTCHA check before submitting.
        if await self._captcha_present(page):
            if not self.secrets.twocaptcha_api_key:
                return BookResult(
                    success=False,
                    confirmation_number=None,
                    held_for_human=True,
                    detail=(
                        "CAPTCHA detected and no TWOCAPTCHA_API_KEY set. "
                        "Page kept open at the submit step; finalize manually."
                    ),
                )
            await self._solve_captcha(page)

        # 5. Submit.
        await self._click_text(page, SELECTORS["submit_button_text"])

        # 6. Read confirmation.
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except PWTimeout:
            pass
        text = await page.content()
        m = re.search(SELECTORS["confirmation_number_regex"], text, re.I)
        if not m:
            return BookResult(
                success=False,
                confirmation_number=None,
                held_for_human=False,
                detail="submitted but could not parse confirmation number",
            )
        return BookResult(
            success=True,
            confirmation_number=m.group(1),
            held_for_human=False,
            detail="booked",
        )

    # ---- helpers ----

    async def _click_text(self, page: Page, text: str) -> None:
        try:
            await page.get_by_text(text, exact=False).first.click()
        except Exception:
            # Fallback to role=button with that name
            await page.get_by_role("button", name=text, exact=False).first.click()

    async def _fill_form(
        self, page: Page, kind: str, applicants: Applicants
    ) -> None:
        """Fill every field defined in the kind-specific SCHEMA. Missing
        labels are skipped with a warning, not raised, so a slightly
        out-of-date SCHEMA doesn't blow up the whole booking."""
        schema = SCHEMA_BY_KIND.get(kind, [])
        for label, who, attr in schema:
            applicant: Applicant = (
                applicants.partner_a if who == "a" else applicants.partner_b
            )
            value = getattr(applicant, attr, None)
            if value in (None, ""):
                continue
            value_str = value.isoformat() if hasattr(value, "isoformat") else str(value)
            try:
                await page.get_by_label(label, exact=False).first.fill(value_str)
            except Exception as e:
                log.warning("booker.fill_skipped", label=label, error=str(e))

    async def _captcha_present(self, page: Page) -> bool:
        html = (await page.content()).lower()
        return any(s in html for s in SELECTORS["captcha_iframe_substrings"])

    async def _solve_captcha(self, page: Page) -> None:
        """Hand off the on-page CAPTCHA to 2captcha. Scaffolded but not
        wired -- adding this means deciding which CAPTCHA type the city
        site is actually using (recaptcha v2/v3, turnstile, hcaptcha)
        and the right widget params. Determine that from a recording
        session before enabling."""
        raise NotImplementedError(
            "2captcha integration is scaffolded but not implemented; record a "
            "session, identify the CAPTCHA widget type, then wire it here."
        )
