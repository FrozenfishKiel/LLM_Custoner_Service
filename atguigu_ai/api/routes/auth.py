from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from atguigu_ai.api.dependencies import (
    AuthRouteDependencies,
    check_rate_limit,
    clear_auth_cookies,
    client_ip,
    issue_auth_cookies,
    require_csrf,
    resolve_authenticated_identity,
)
from atguigu_ai.auth import (
    AuthServiceUnavailable,
    DuplicateRegistration,
    InvalidCredentials,
    InvalidPassword,
    normalize_email,
)
from atguigu_ai.rate_limit import RateLimitRule


REGISTER_IP_RULE = RateLimitRule("auth.register.ip", "auth", 5, 3600)
LOGIN_IP_EMAIL_RULE = RateLimitRule("auth.login.ip_email", "auth", 5, 900)
LOGIN_IP_RULE = RateLimitRule("auth.login.ip", "auth", 30, 900)
FORGOT_PASSWORD_IP_EMAIL_RULE = RateLimitRule("auth.forgot_password.ip_email", "auth", 5, 3600)
FORGOT_PASSWORD_IP_RULE = RateLimitRule("auth.forgot_password.ip", "auth", 20, 3600)
RESEND_VERIFICATION_IP_EMAIL_RULE = RateLimitRule("auth.resend_verification.ip_email", "auth", 5, 3600)
VERIFY_EMAIL_IP_RULE = RateLimitRule("auth.verify_email.ip", "auth", 60, 900)
RESET_PASSWORD_IP_RULE = RateLimitRule("auth.reset_password.ip", "auth", 20, 900)
CHANGE_PASSWORD_ACCOUNT_RULE = RateLimitRule("auth.change_password.account", "auth", 5, 900)


class EmailPasswordRequest(BaseModel):
    email: str
    password: str


class TokenRequest(BaseModel):
    token: str


class EmailRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def create_auth_router(deps: AuthRouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.post("/api/auth/register", status_code=status.HTTP_202_ACCEPTED)
    async def register(request: Request, payload: EmailPasswordRequest) -> dict[str, bool]:
        ip = client_ip(request, deps)
        await check_rate_limit(deps.rate_limiter, REGISTER_IP_RULE, ip)
        try:
            await deps.service.register(payload.email, payload.password)
            return {"accepted": True}
        except DuplicateRegistration:
            return {"accepted": True}
        except InvalidPassword as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

    @router.post("/api/auth/verify-email")
    async def verify_email(request: Request, payload: TokenRequest) -> dict[str, bool]:
        ip = client_ip(request, deps)
        await check_rate_limit(deps.rate_limiter, VERIFY_EMAIL_IP_RULE, ip)
        try:
            await deps.service.verify_email(payload.token)
            return {"accepted": True}
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

    @router.post("/api/auth/resend-verification")
    async def resend_verification(request: Request, payload: EmailRequest) -> dict[str, bool]:
        ip = client_ip(request, deps)
        await check_rate_limit(
            deps.rate_limiter,
            RESEND_VERIFICATION_IP_EMAIL_RULE,
            _ip_email_subject(ip, payload.email),
        )
        try:
            await deps.service.resend_verification(payload.email)
            return {"accepted": True}
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

    @router.post("/api/auth/login")
    async def login(request: Request, payload: EmailPasswordRequest, response: Response) -> dict[str, str]:
        ip = client_ip(request, deps)
        await check_rate_limit(deps.rate_limiter, LOGIN_IP_EMAIL_RULE, _ip_email_subject(ip, payload.email))
        await check_rate_limit(deps.rate_limiter, LOGIN_IP_RULE, ip)
        try:
            accepted = await deps.service.login(payload.email, payload.password)
        except InvalidCredentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from None
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

        csrf_token = secrets.token_urlsafe(32)
        issue_auth_cookies(
            response,
            accepted.session,
            csrf_token,
            deps.cookie_secure,
            session_cookie_name=deps.session_cookie_name,
            csrf_cookie_name=deps.csrf_cookie_name,
        )
        return _identity_response(accepted.identity)

    @router.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request, response: Response) -> Response:
        require_csrf(request, deps)
        session_token = request.cookies.get(deps.session_cookie_name)
        if session_token:
            try:
                await deps.service.logout(session_token)
            except AuthServiceUnavailable:
                raise _service_unavailable() from None
        clear_auth_cookies(
            response,
            deps.cookie_secure,
            session_cookie_name=deps.session_cookie_name,
            csrf_cookie_name=deps.csrf_cookie_name,
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.post("/api/auth/forgot-password")
    async def forgot_password(request: Request, payload: EmailRequest) -> dict[str, bool]:
        ip = client_ip(request, deps)
        await check_rate_limit(
            deps.rate_limiter,
            FORGOT_PASSWORD_IP_EMAIL_RULE,
            _ip_email_subject(ip, payload.email),
        )
        await check_rate_limit(deps.rate_limiter, FORGOT_PASSWORD_IP_RULE, ip)
        try:
            await deps.service.forgot_password(payload.email)
            return {"accepted": True}
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

    @router.post("/api/auth/reset-password")
    async def reset_password(request: Request, payload: ResetPasswordRequest) -> dict[str, bool]:
        ip = client_ip(request, deps)
        await check_rate_limit(deps.rate_limiter, RESET_PASSWORD_IP_RULE, ip)
        try:
            await deps.service.reset_password(payload.token, payload.new_password)
            return {"accepted": True}
        except InvalidPassword as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
        except AuthServiceUnavailable:
            raise _service_unavailable() from None

    @router.post("/api/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
    async def change_password(request: Request, payload: ChangePasswordRequest, response: Response) -> Response:
        require_csrf(request, deps)
        identity = await _require_identity(request, deps)
        await check_rate_limit(deps.rate_limiter, CHANGE_PASSWORD_ACCOUNT_RULE, identity.account_id)
        try:
            await deps.service.change_password(
                identity.account_id,
                payload.current_password,
                payload.new_password,
            )
        except InvalidCredentials:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from None
        except InvalidPassword as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
        except AuthServiceUnavailable:
            raise _service_unavailable() from None
        clear_auth_cookies(
            response,
            deps.cookie_secure,
            session_cookie_name=deps.session_cookie_name,
            csrf_cookie_name=deps.csrf_cookie_name,
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/api/account/me")
    async def account_me(request: Request) -> dict[str, str]:
        identity = await _require_identity(request, deps)
        return _identity_response(identity)

    return router


async def _require_identity(request: Request, deps: AuthRouteDependencies):
    try:
        identity = await resolve_authenticated_identity(request, deps)
    except Exception:
        raise _service_unavailable() from None
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return identity


def _identity_response(identity) -> dict[str, str]:
    return {
        "account_id": identity.account_id,
        "role": identity.role.value,
        "status": identity.status.value,
    }


def _ip_email_subject(ip: str, email: str) -> str:
    normalized = normalize_email(email).normalized
    email_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{ip}:{email_digest}"


def _service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service is unavailable",
    )
