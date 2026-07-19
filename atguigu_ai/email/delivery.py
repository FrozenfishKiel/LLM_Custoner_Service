from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as _SMTPMessage

import anyio


@dataclass(frozen=True)
class EmailMessage:
    purpose: str
    recipient: str
    url: str = field(repr=False)


class EmailDeliveryUnavailable(RuntimeError):
    def __init__(self, message: str = "Email delivery is unavailable") -> None:
        super().__init__(message)


def _validate_delivery_target(recipient: str, url: str) -> None:
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("recipient must be a non-blank string")
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must start with http:// or https://")


class FakeEmailDelivery:
    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    async def send_verification_email(self, recipient: str, url: str) -> None:
        _validate_delivery_target(recipient, url)
        self.outbox.append(EmailMessage("verify_email", recipient, url))

    async def send_password_reset_email(self, recipient: str, url: str) -> None:
        _validate_delivery_target(recipient, url)
        self.outbox.append(EmailMessage("reset_password", recipient, url))


class SMTPEmailDelivery:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool,
        timeout: int = 10,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._use_tls = use_tls
        self._timeout = timeout

    async def send_verification_email(self, recipient: str, url: str) -> None:
        await self._send("Verify your email address", recipient, url)

    async def send_password_reset_email(self, recipient: str, url: str) -> None:
        await self._send("Reset your password", recipient, url)

    async def _send(self, subject: str, recipient: str, url: str) -> None:
        _validate_delivery_target(recipient, url)

        try:
            await anyio.to_thread.run_sync(lambda: self._send_sync(subject, recipient, url))
        except (OSError, smtplib.SMTPException, TimeoutError):
            raise EmailDeliveryUnavailable() from None

    def _send_sync(self, subject: str, recipient: str, url: str) -> None:
        message = _SMTPMessage()
        message["From"] = self._from_address
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(f"{subject}\n\n{url}\n")

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message, from_addr=self._from_address, to_addrs=[recipient])
