from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, Response, status

from atguigu_ai.auth import AccountIdentity, AuthService, CreatedSession, RedisSessionStore


@dataclass(frozen=True)
class AuthRouteDependencies:
    service: AuthService
    sessions: RedisSessionStore
    session_cookie_name: str = "auth_session"
    csrf_cookie_name: str = "auth_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = True


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


async def resolve_authenticated_identity(
    request: Request,
    deps: AuthRouteDependencies,
) -> AccountIdentity | None:
    session_token = request.cookies.get(deps.session_cookie_name)
    if not session_token:
        return None
    return await deps.sessions.resolve(session_token)
