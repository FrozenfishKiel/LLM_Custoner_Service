from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response, status

from atguigu_ai.auth import AccountIdentity, AuthService, CreatedSession, RedisSessionStore
from atguigu_ai.rate_limit import RateLimitDecision, RateLimitRule, RateLimitStoreUnavailable


@dataclass(frozen=True)
class AuthRouteDependencies:
    service: AuthService
    sessions: RedisSessionStore
    session_cookie_name: str = "auth_session"
    csrf_cookie_name: str = "auth_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = True
    rate_limiter: Any | None = None
    client_ip_resolver: Callable[[Request], str] | None = None


def issue_auth_cookies(
    response: Response,
    session: CreatedSession,
    csrf_token: str,
    secure: bool,
    *,
    session_cookie_name: str = "auth_session",
    csrf_cookie_name: str = "auth_csrf",
) -> None:
    response.set_cookie(
        session_cookie_name,
        session.token,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path="/",
    )
    response.set_cookie(
        csrf_cookie_name,
        csrf_token,
        httponly=False,
        secure=secure,
        samesite="Lax",
        path="/",
    )


def clear_auth_cookies(
    response: Response,
    secure: bool,
    *,
    session_cookie_name: str = "auth_session",
    csrf_cookie_name: str = "auth_csrf",
) -> None:
    secure_attr = "; Secure" if secure else ""
    response.headers.append(
        "set-cookie",
        f"{session_cookie_name}=; HttpOnly; Max-Age=0; Path=/; SameSite=Lax{secure_attr}",
    )
    response.headers.append(
        "set-cookie",
        f"{csrf_cookie_name}=; Max-Age=0; Path=/; SameSite=Lax{secure_attr}",
    )


def require_csrf(request: Request, deps: AuthRouteDependencies) -> None:
    header_value = request.headers.get(deps.csrf_header_name)
    cookie_value = request.cookies.get(deps.csrf_cookie_name)
    if not header_value or not cookie_value or header_value != cookie_value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


def client_ip(request: Request, deps: AuthRouteDependencies) -> str:
    if deps.client_ip_resolver is not None:
        return deps.client_ip_resolver(request)
    return request.client.host if request.client is not None else "unknown"


async def check_rate_limit(
    rate_limiter: Any | None,
    rule: RateLimitRule,
    subject: str,
) -> RateLimitDecision | None:
    if rate_limiter is None:
        return None
    try:
        decision = await rate_limiter.check(rule, subject)
    except RateLimitStoreUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rate limit service is unavailable",
        ) from None
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": str(decision.remaining),
                "X-RateLimit-Reset": str(decision.reset_after_seconds),
            },
        )
    return decision


async def resolve_authenticated_identity(
    request: Request,
    deps: AuthRouteDependencies,
) -> AccountIdentity | None:
    session_token = request.cookies.get(deps.session_cookie_name)
    if not session_token:
        return None
    return await deps.sessions.resolve(session_token)
