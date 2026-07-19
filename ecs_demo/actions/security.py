from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


class ActionSecurityError(RuntimeError):
    def __init__(self, message: str = "Action identity is unavailable") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class ActionUserContext:
    account_id: str
    user_id: str
    role: str
    request_id: str


SENSITIVE_METADATA_KEYS = {
    "password",
    "token",
    "session",
    "secret",
    "csrf",
    "raw_token",
    "session_id",
    "client_secret",
}


def current_action_user(
    tracker,
    *,
    account_id: object | None = None,
    user_id: object | None = None,
    account_role: object | None = None,
    request_id: object | None = None,
    allow_demo_identity_fallback: bool = False,
    **_: object,
) -> ActionUserContext:
    trusted_account_id = _non_blank(account_id)
    trusted_user_id = _non_blank(user_id)
    if trusted_user_id is None and allow_demo_identity_fallback:
        trusted_user_id = _non_blank(tracker.get_slot("user_id"))
    if trusted_account_id is None and allow_demo_identity_fallback:
        trusted_account_id = "demo-account"
    if trusted_account_id is None or trusted_user_id is None:
        raise ActionSecurityError()
    return ActionUserContext(
        account_id=trusted_account_id,
        user_id=trusted_user_id,
        role=_non_blank(account_role) or "consumer",
        request_id=_non_blank(request_id) or f"action-{uuid4()}",
    )


def owned_order_query(session, order_model, *, user_id: str, order_id: str):
    return session.query(order_model).filter(
        order_model.order_id == order_id,
        order_model.user_id == user_id,
    )


def audit_metadata(**values: object) -> dict[str, object]:
    clean: dict[str, object] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(sensitive in lowered for sensitive in SENSITIVE_METADATA_KEYS):
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


def record_action_audit(
    session,
    *,
    context: ActionUserContext,
    event_type: str,
    target_type: str,
    target_id: str,
    result: str,
    metadata: dict[str, object] | None = None,
) -> None:
    from atguigu_ai.auth import AccountRepository, AccountRole, AuditResult

    repository = AccountRepository(session)
    repository.record_audit(
        request_id=context.request_id,
        actor_account_id=context.account_id,
        actor_role=AccountRole(context.role),
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        result=AuditResult(result),
        metadata=metadata,
    )


def _non_blank(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
