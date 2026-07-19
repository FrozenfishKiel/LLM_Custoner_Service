import inspect
import smtplib
import ssl

import pytest

from atguigu_ai.email import (
    EmailDeliveryUnavailable,
    EmailMessage,
    FakeEmailDelivery,
    SMTPEmailDelivery,
)


def test_public_email_exports_are_exact():
    import atguigu_ai.email as email_module

    assert email_module.__all__ == [
        "EmailDeliveryUnavailable",
        "EmailMessage",
        "FakeEmailDelivery",
        "SMTPEmailDelivery",
    ]
    assert inspect.iscoroutinefunction(FakeEmailDelivery.send_verification_email)
    assert inspect.iscoroutinefunction(FakeEmailDelivery.send_password_reset_email)
    assert inspect.iscoroutinefunction(SMTPEmailDelivery.send_verification_email)
    assert inspect.iscoroutinefunction(SMTPEmailDelivery.send_password_reset_email)


@pytest.mark.asyncio
async def test_fake_delivery_records_sanitized_messages():
    delivery = FakeEmailDelivery()
    await delivery.send_verification_email("User@example.com", "https://example.test/verify?token=secret-token")
    await delivery.send_password_reset_email("User@example.com", "https://example.test/reset?token=reset-token")

    assert [message.purpose for message in delivery.outbox] == ["verify_email", "reset_password"]
    assert delivery.outbox[0].recipient == "User@example.com"
    assert delivery.outbox[0].url == "https://example.test/verify?token=secret-token"
    assert "secret-token" not in repr(delivery.outbox[0])


@pytest.mark.parametrize("recipient", ["", "   ", None, 42])
@pytest.mark.asyncio
async def test_fake_delivery_rejects_invalid_recipient(recipient):
    with pytest.raises(ValueError):
        await FakeEmailDelivery().send_verification_email(recipient, "https://example.test/verify")


@pytest.mark.parametrize("url", ["", "ftp://example.test/x", "javascript:alert(1)", None])
@pytest.mark.asyncio
async def test_fake_delivery_rejects_invalid_public_url(url):
    with pytest.raises(ValueError):
        await FakeEmailDelivery().send_password_reset_email("user@example.com", url)


@pytest.mark.asyncio
async def test_smtp_delivery_maps_dependency_errors_without_secret_text(monkeypatch):
    captured = {}

    class FailingSMTP:
        def __init__(self, host, port, timeout):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            raise smtplib.SMTPException("smtp-password-leaked")

    monkeypatch.setattr(smtplib, "SMTP", FailingSMTP)
    delivery = SMTPEmailDelivery(
        host="smtp.example.test",
        port=587,
        username="smtp-user",
        password="smtp-password-leaked",
        from_address="noreply@example.test",
        use_tls=True,
    )

    with pytest.raises(EmailDeliveryUnavailable) as captured_error:
        await delivery.send_verification_email("user@example.com", "https://example.test/verify?token=abc")

    assert str(captured_error.value) == "Email delivery is unavailable"
    assert captured_error.value.__cause__ is None
    assert "smtp-password-leaked" not in repr(captured_error.value)
    assert captured == {"host": "smtp.example.test", "port": 587, "timeout": 10}


@pytest.mark.asyncio
async def test_smtp_delivery_uses_verified_tls_context(monkeypatch):
    captured = {}

    class RecordingSMTP:
        def __init__(self, host, port, timeout):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self, context=None):
            captured["context"] = context

        def login(self, username, password):
            captured["username"] = username
            captured["password"] = password

        def send_message(self, message, from_addr, to_addrs):
            captured["subject"] = message["Subject"]
            captured["from_addr"] = from_addr
            captured["to_addrs"] = to_addrs

    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    delivery = SMTPEmailDelivery(
        host="smtp.example.test",
        port=587,
        username="smtp-user",
        password="smtp-password",
        from_address="noreply@example.test",
        use_tls=True,
    )

    await delivery.send_password_reset_email("user@example.com", "https://example.test/reset?token=abc")

    assert captured["host"] == "smtp.example.test"
    assert captured["port"] == 587
    assert captured["timeout"] == 10
    assert isinstance(captured["context"], ssl.SSLContext)
    assert captured["context"].check_hostname is True
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["username"] == "smtp-user"
    assert captured["password"] == "smtp-password"
    assert captured["subject"] == "Reset your password"
    assert captured["from_addr"] == "noreply@example.test"
    assert captured["to_addrs"] == ["user@example.com"]
