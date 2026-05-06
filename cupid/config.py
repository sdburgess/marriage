"""Config schemas. PII lives in config.yaml; secrets in env vars."""
from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Borough = Literal["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
Kind = Literal["license", "ceremony"]
Mode = Literal["in_person", "virtual"]
Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Target(BaseModel):
    kind: Kind
    mode: Mode = "in_person"
    boroughs: list[Borough]
    earliest_date: date
    latest_date: date
    earliest_time: time
    latest_time: time
    weekdays: list[Weekday] = Field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu", "Fri"])
    auto_book: bool = False
    license_number: str = ""

    # If true, before auto-booking, post the slot to Slack with Book/Skip
    # buttons. If neither is clicked within `slack_confirm_timeout_seconds`
    # (top-level config), auto-book happens anyway so we don't lose the
    # slot to a missed phone. If the slot matches preferred_date, this
    # flag is ignored and we book immediately -- you've already chosen.
    slack_confirm: bool = False

    # Optional. If set, this is your preferred date (e.g., a date that's
    # currently booked by someone else and you're waiting for a cancellation).
    # When found, the booker will go for it before any other matching slot.
    # The scraper still considers earliest_date..latest_date as the search
    # window, so set those to a wider range if you also want backup options.
    preferred_date: date | None = None
    preferred_time: time | None = None      # optional preferred time-of-day
    preferred_only: bool = False            # if true, only book preferred_date

    @field_validator("latest_date")
    @classmethod
    def _date_range(cls, v: date, info):
        earliest = info.data.get("earliest_date")
        if earliest and v < earliest:
            raise ValueError("latest_date must be >= earliest_date")
        return v


class Applicant(BaseModel):
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    suffix: str = ""
    date_of_birth: date | None = None
    place_of_birth_city: str = ""
    place_of_birth_state: str = ""
    place_of_birth_country: str = ""
    sex: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    email: str = ""
    id_type: str = ""
    id_number: str = ""
    id_state_or_country: str = ""
    parent_a_first: str = ""
    parent_a_last: str = ""
    parent_a_birth_country: str = ""
    parent_b_first: str = ""
    parent_b_last: str = ""
    parent_b_birth_country: str = ""
    prior_marriages: int = 0
    new_last_name: str = ""


class Applicants(BaseModel):
    partner_a: Applicant
    partner_b: Applicant


class Notifications(BaseModel):
    sms_to: list[str] = Field(default_factory=list)
    email_to: list[str] = Field(default_factory=list)
    slack_enabled: bool = False


class ReleaseWindow(BaseModel):
    dow: Weekday
    time: time
    kinds: list[Kind]


class Polling(BaseModel):
    # When constant_mode is on (the default for cancellation-watch), the
    # scheduler ignores release_windows and just polls every
    # base_interval_seconds (with a small random jitter) all day.
    constant_mode: bool = True
    base_interval_seconds: int = 45        # respectful but fast
    jitter_seconds: int = 10               # +/- this much, randomly
    burst_interval_seconds: int = 5        # only used when constant_mode=false
    release_windows: list[ReleaseWindow] = Field(default_factory=list)


class Server(BaseModel):
    """Web dashboard + Slack interactivity webhook host."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    slack_confirm_timeout_seconds: int = 90  # auto-book if no Slack response


class Config(BaseModel):
    targets: list[Target]
    applicants: Applicants
    notifications: Notifications
    polling: Polling = Polling()
    server: Server = Server()


class Secrets(BaseModel):
    """Pulled from env. Never logged in full."""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    slack_bot_token: str = ""
    slack_channel_id: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    cupid_login_email: str = ""
    cupid_login_password: str = ""
    twocaptcha_api_key: str = ""

    slack_signing_secret: str = ""
    web_username: str = ""
    web_password: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            twilio_from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
            slack_channel_id=os.getenv("SLACK_CHANNEL_ID", ""),
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_from=os.getenv("SMTP_FROM", ""),
            cupid_login_email=os.getenv("CUPID_LOGIN_EMAIL", ""),
            cupid_login_password=os.getenv("CUPID_LOGIN_PASSWORD", ""),
            twocaptcha_api_key=os.getenv("TWOCAPTCHA_API_KEY", ""),
            slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
            web_username=os.getenv("WEB_USERNAME", ""),
            web_password=os.getenv("WEB_PASSWORD", ""),
        )


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path or os.getenv("CONFIG_PATH", "config.yaml"))
    with p.open() as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
