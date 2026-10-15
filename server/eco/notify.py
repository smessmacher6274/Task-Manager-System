"""Email notifications (Mailpit locally)."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from eco.config import Settings

log = logging.getLogger(__name__)

REMINDER_SUBJECT = "Eco reminder"
REMINDER_BODY = "Go fill in all tasks for the day."


def send_email(settings: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.send_message(msg)
    log.info("email sent to=%s subject=%s", to, subject)


def send_reminder(settings: Settings, to: str) -> None:
    send_email(settings, to, REMINDER_SUBJECT, REMINDER_BODY)


def send_push_failure(settings: Settings, to: str, errors: list[dict]) -> None:
    lines = [f"The Redmine push had {len(errors)} failure(s):", ""]
    for e in errors:
        lines.append(f"- {e.get('op')} item={e.get('item')} issue={e.get('issue_id', '-')}: {e.get('error')}")
    send_email(settings, settings.alert_email or to, "Eco: Redmine push FAILED", "\n".join(lines))
