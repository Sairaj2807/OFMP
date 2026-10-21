"""Outbound email. No provider is integrated yet: EmailSender is the
interface a real provider (SES, SendGrid, SMTP, ...) implements later."""
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None:
        ...


class LogEmailSender:
    """Development only: writes the message (including its link) to the log.
    Refuses to run in production, where links would leak into logs."""

    def __init__(self, environment: str):
        if environment == "production":
            raise RuntimeError("LogEmailSender must not be used in production; configure an email provider")

    async def send(self, to: str, subject: str, body: str) -> None:
        log.info("[dev email] to=%s subject=%s\n%s", to, subject, body)


class DisabledEmailSender:
    """Production without an email provider (EMAIL_PROVIDER=none): nothing is sent, and only
    the recipient and subject are logged, never the body (it carries one-time links). Use it
    with ALLOW_REGISTRATION=0; accounts are created with `python -m backend.app.cli create-user`."""

    async def send(self, to: str, subject: str, body: str) -> None:
        log.warning("email not sent (EMAIL_PROVIDER=none): to=%s subject=%s", to, subject)


class MemoryEmailSender:
    """Tests: collects messages in `outbox`."""

    def __init__(self):
        self.outbox = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "body": body})
