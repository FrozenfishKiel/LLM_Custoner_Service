from __future__ import annotations

import base64
import binascii
import re
import threading
from dataclasses import dataclass
from typing import Callable, TypeVar

import anyio
from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type
from email_validator import EmailNotValidError, validate_email


_EMAIL_ERROR = "Invalid email address"
_PASSWORD_ERROR = "Password does not meet requirements"
_HASHING_CAPACITY_ERROR = "Password hashing capacity is unavailable"
_ASCII_WHITESPACE = " \t\n\r\v\f"
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_ARGON2_PATTERN = re.compile(
    r"^\$argon2id\$v=19\$m=(?P<memory>[0-9]+),t=(?P<time>[0-9]+),p=(?P<parallelism>[0-9]+)"
    r"\$(?P<salt>[A-Za-z0-9+/]+)\$(?P<digest>[A-Za-z0-9+/]+)$"
)

_WORKER_SLOTS = threading.BoundedSemaphore(4)
_ADMITTED_JOBS = threading.BoundedSemaphore(20)

_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4"
    "$l/qVfVpSvpBXVqJYziC4Iw"
    "$Z4a7g+WJsZOpVODl4cbYWaVtpZsmvGCuq59yfkhanEI"
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class EmailAddress:
    display: str
    normalized: str


class InvalidEmail(ValueError):
    pass


class InvalidPassword(ValueError):
    pass


class PasswordHashingOverloaded(RuntimeError):
    pass


def normalize_email(value: str) -> EmailAddress:
    if not isinstance(value, str):
        raise InvalidEmail(_EMAIL_ERROR) from None

    candidate = value.strip(_ASCII_WHITESPACE)
    if not candidate or _CONTROL_PATTERN.search(candidate):
        raise InvalidEmail(_EMAIL_ERROR) from None

    try:
        result = validate_email(
            candidate,
            allow_smtputf8=False,
            allow_display_name=False,
            check_deliverability=False,
        )
        display = result.ascii_email
    except (EmailNotValidError, UnicodeError, ValueError):
        raise InvalidEmail(_EMAIL_ERROR) from None

    if not display:
        raise InvalidEmail(_EMAIL_ERROR) from None
    local_part = display.rsplit("@", 1)[0]
    if len(local_part) > 64 or len(display) > 254:
        raise InvalidEmail(_EMAIL_ERROR) from None
    return EmailAddress(display=display, normalized=display.casefold())


class PasswordPolicy:
    def validate(self, password: str) -> None:
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise InvalidPassword(_PASSWORD_ERROR) from None
        if any(_invalid_password_character(character) for character in password):
            raise InvalidPassword(_PASSWORD_ERROR) from None
        return None


def _invalid_password_character(character: str) -> bool:
    codepoint = ord(character)
    return codepoint <= 0x1F or codepoint == 0x7F or 0xD800 <= codepoint <= 0xDFFF


def _decode_argon2_field(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Invalid Argon2 field") from None
    if base64.b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError("Invalid Argon2 field") from None
    return decoded


def _eligible_hash(encoded: object) -> bool:
    if not isinstance(encoded, str) or len(encoded) > 512:
        return False
    match = _ARGON2_PATTERN.fullmatch(encoded)
    if match is None:
        return False
    try:
        memory = int(match.group("memory"))
        time_cost = int(match.group("time"))
        parallelism = int(match.group("parallelism"))
        salt = _decode_argon2_field(match.group("salt"))
        digest = _decode_argon2_field(match.group("digest"))
    except ValueError:
        return False
    return (
        8 <= memory <= 65536
        and 1 <= time_cost <= 3
        and 1 <= parallelism <= 4
        and 8 <= len(salt) <= 32
        and 16 <= len(digest) <= 64
    )


class PasswordHasher:
    def __init__(self) -> None:
        self._policy = PasswordPolicy()
        self._argon2 = _Argon2Adapter()

    async def hash(self, password: str) -> str:
        self._policy.validate(password)
        return await _run_argon2(self._argon2.hash, password)

    async def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            self._policy.validate(password)
        except InvalidPassword:
            return False

        selected_hash = password_hash if _eligible_hash(password_hash) else _DUMMY_HASH
        try:
            verified = bool(await _run_argon2(self._argon2.verify, selected_hash, password))
            return verified if selected_hash == password_hash else False
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        if not _eligible_hash(password_hash):
            return False
        try:
            return bool(self._argon2.check_needs_rehash(password_hash))
        except (InvalidHashError, VerificationError, ValueError, TypeError):
            return False


class _Argon2Adapter:
    def __init__(self) -> None:
        self._hasher = Argon2PasswordHasher(
            memory_cost=65536,
            time_cost=3,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        return self._hasher.verify(password_hash, password)

    def check_needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


async def _run_argon2(function: Callable[..., _T], *args: object) -> _T:
    if not _ADMITTED_JOBS.acquire(blocking=False):
        raise PasswordHashingOverloaded(_HASHING_CAPACITY_ERROR) from None
    try:
        return await anyio.to_thread.run_sync(_run_with_worker_slot, function, *args)
    finally:
        _ADMITTED_JOBS.release()


def _run_with_worker_slot(function: Callable[..., _T], *args: object) -> _T:
    _WORKER_SLOTS.acquire()
    try:
        return function(*args)
    finally:
        _WORKER_SLOTS.release()


__all__ = [
    "EmailAddress",
    "InvalidEmail",
    "InvalidPassword",
    "PasswordHasher",
    "PasswordHashingOverloaded",
    "PasswordPolicy",
    "normalize_email",
]
