from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from atguigu_ai.api.dependencies import (
    AuthRouteDependencies,
    check_rate_limit,
    require_csrf,
    resolve_authenticated_identity,
)
from atguigu_ai.auth import AccountStatus
from atguigu_ai.auth.business_identity import (
    BusinessIdentityResolver,
    BusinessUserBindingUnavailable,
    BusinessUserNotBound,
)
from atguigu_ai.rate_limit import RateLimitRule


_IDENTITY_METADATA_KEYS = {
    "sender",
    "sender_id",
    "session_id",
    "account_id",
    "user_id",
    "role",
    "account_role",
    "status",
    "account_status",
}

CHAT_MESSAGES_ACCOUNT_RULE = RateLimitRule("chat.messages.account", "chat", 30, 60)
CHAT_RESET_ACCOUNT_RULE = RateLimitRule("chat.reset.account", "chat", 10, 60)


@dataclass(frozen=True)
class ChatRouteDependencies:
    agent: Any
    business_identity_resolver: BusinessIdentityResolver


def create_chat_router(
    chat_deps: ChatRouteDependencies,
    auth_deps: AuthRouteDependencies | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/chat/messages")
    async def chat_message(request: Request) -> list[dict[str, Any]]:
        deps = _auth_deps(request, auth_deps)
        account_identity = await _require_account_identity(request, deps)
        require_csrf(request, deps)
        identity = await _require_business_identity(account_identity, chat_deps)
        await check_rate_limit(deps.rate_limiter, CHAT_MESSAGES_ACCOUNT_RULE, identity.account_id)
        payload = await _message_payload(request)
        sender_id = _tracker_key(identity.account_id)
        response = await chat_deps.agent.handle_message(
            payload["message"],
            sender_id=sender_id,
            metadata=_trusted_metadata(identity, payload.get("metadata")),
        )
        return [
            {
                "recipient_id": sender_id,
                "text": message.get("text"),
                "buttons": message.get("buttons"),
                "image": message.get("image"),
                "custom": message.get("custom"),
            }
            for message in response.messages
        ]

    @router.post("/api/chat/reset", status_code=status.HTTP_204_NO_CONTENT)
    async def chat_reset(request: Request) -> Response:
        deps = _auth_deps(request, auth_deps)
        account_identity = await _require_account_identity(request, deps)
        require_csrf(request, deps)
        identity = await _require_business_identity(account_identity, chat_deps)
        await check_rate_limit(deps.rate_limiter, CHAT_RESET_ACCOUNT_RULE, identity.account_id)
        await chat_deps.agent.reset_tracker(_tracker_key(identity.account_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _auth_deps(
    request: Request,
    explicit_deps: AuthRouteDependencies | None,
) -> AuthRouteDependencies:
    deps = explicit_deps or getattr(request.app.state, "auth_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable",
        )
    return deps


async def _require_account_identity(
    request: Request,
    auth_deps: AuthRouteDependencies,
):
    try:
        account_identity = await resolve_authenticated_identity(request, auth_deps)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is unavailable",
        ) from None
    if account_identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if account_identity.status is not AccountStatus.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active")
    return account_identity


async def _require_business_identity(
    account_identity,
    chat_deps: ChatRouteDependencies,
):
    try:
        return await chat_deps.business_identity_resolver.resolve(account_identity)
    except BusinessUserNotBound:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Business user binding is missing") from None
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active") from None
    except BusinessUserBindingUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat authorization service is unavailable",
        ) from None


def _tracker_key(account_id: str) -> str:
    return f"account:{account_id}"


def _trusted_metadata(identity, client_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _sanitize_client_metadata(client_metadata or {})
    metadata.update(
        {
            "account_id": identity.account_id,
            "user_id": identity.user_id,
            "account_role": identity.role.value,
            "account_status": identity.account_status.value,
        }
    )
    return metadata


def _sanitize_client_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_client_metadata(item)
            for key, item in value.items()
            if str(key).lower() not in _IDENTITY_METADATA_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_client_metadata(item) for item in value]
    return value


async def _message_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (JSONDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid message payload")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message is required")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Metadata must be an object")
    return {"message": message, "metadata": metadata}


__all__ = ["ChatRouteDependencies", "create_chat_router"]
