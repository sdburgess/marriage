"""Notification fan-out: Twilio SMS + SMTP email + Slack chat.postMessage.

Each channel is best-effort. If one fails the others still fire.
"""
from __future__ import annotations

import smtplib
import structlog
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx

from .config import Config, Secrets

log = structlog.get_logger()


@dataclass
class Notice:
    subject: str           # short, used as SMS body and email subject
    body: str              # long, used in email/Slack
    url: str | None = None # optional CTA link


class Notifier:
    def __init__(self, cfg: Config, secrets: Secrets):
        self.cfg = cfg
        self.secrets = secrets

    def send(self, n: Notice) -> None:
        # Fan out independently. We swallow per-channel exceptions so a
        # broken Slack token never silences SMS.
        for fn in (self._sms, self._email, self._slack):
            try:
                fn(n)
            except Exception as e:
                log.warning("notify.channel_failed", channel=fn.__name__, error=str(e))

    # ---- SMS ----
    def _sms(self, n: Notice) -> None:
        nums = self.cfg.notifications.sms_to
        s = self.secrets
        if not nums or not s.twilio_account_sid or not s.twilio_auth_token:
            return
        # Lazy import so missing twilio module doesn't break the world.
        from twilio.rest import Client

        client = Client(s.twilio_account_sid, s.twilio_auth_token)
        body = n.subject if not n.url else f"{n.subject}\n{n.url}"
        for to in nums:
            client.messages.create(from_=s.twilio_from_number, to=to, body=body[:1500])
            log.info("notify.sms_sent", to=to)

    # ---- Email ----
    def _email(self, n: Notice) -> None:
        addrs = [a for a in self.cfg.notifications.email_to if a]
        s = self.secrets
        if not addrs or not s.smtp_host or not s.smtp_from:
            return
        msg = EmailMessage()
        msg["Subject"] = f"[Cupid] {n.subject}"
        msg["From"] = s.smtp_from
        msg["To"] = ", ".join(addrs)
        text = n.body
        if n.url:
            text = f"{text}\n\n{n.url}"
        msg.set_content(text)
        with smtplib.SMTP(s.smtp_host, s.smtp_port) as smtp:
            smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
        log.info("notify.email_sent", to=addrs)

    # ---- Slack ----
    def _slack(self, n: Notice) -> None:
        if not self.cfg.notifications.slack_enabled:
            return
        s = self.secrets
        if not s.slack_bot_token or not s.slack_channel_id:
            return
        text = f"*{n.subject}*\n{n.body}"
        if n.url:
            text += f"\n<{n.url}|open>"
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {s.slack_bot_token}"},
            json={"channel": s.slack_channel_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError(f"slack: {r.text}")
        log.info("notify.slack_sent")

    # ---- Slack interactive: post Book/Skip message ----
    def post_slot_decision(
        self,
        *,
        callback_id: str,
        subject: str,
        slot_summary: str,
        timeout_seconds: int,
    ) -> str | None:
        """Post a Slack message with Book/Skip buttons. Returns the
        message ts (so we can edit it after the decision), or None if
        Slack isn't configured."""
        s = self.secrets
        if not s.slack_bot_token or not s.slack_channel_id:
            return None
        blocks = _slot_decision_blocks(
            callback_id=callback_id,
            subject=subject,
            slot_summary=slot_summary,
            timeout_seconds=timeout_seconds,
        )
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {s.slack_bot_token}"},
            json={
                "channel": s.slack_channel_id,
                "text": subject,
                "blocks": blocks,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack: {r.text}")
        return data.get("ts")

    def update_slack_message(self, ts: str, text: str) -> None:
        s = self.secrets
        if not s.slack_bot_token or not s.slack_channel_id or not ts:
            return
        try:
            httpx.post(
                "https://slack.com/api/chat.update",
                headers={"Authorization": f"Bearer {s.slack_bot_token}"},
                json={
                    "channel": s.slack_channel_id,
                    "ts": ts,
                    "text": text,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text},
                        }
                    ],
                },
                timeout=10,
            )
        except Exception as e:
            log.warning("notify.slack_update_failed", error=str(e))


def _slot_decision_blocks(
    *,
    callback_id: str,
    subject: str,
    slot_summary: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{subject}*\n{slot_summary}\n_auto-booking in {timeout_seconds}s if no one clicks_",
            },
        },
        {
            "type": "actions",
            "block_id": f"slot_decision:{callback_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Book it"},
                    "action_id": "book",
                    "value": callback_id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Skip"},
                    "action_id": "skip",
                    "value": callback_id,
                },
            ],
        },
    ]
