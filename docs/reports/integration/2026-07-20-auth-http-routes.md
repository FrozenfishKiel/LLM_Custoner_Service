# Auth HTTP Routes Verification

Date: 2026-07-19

Status: APPROVED

## Scope

This slice exposes the authentication service through FastAPI:

- `POST /api/auth/register`
- `POST /api/auth/verify-email`
- `POST /api/auth/resend-verification`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `POST /api/auth/forgot-password`
- `POST /api/auth/reset-password`
- `POST /api/auth/change-password`
- `GET /api/account/me`

Session cookie: `auth_session`, HttpOnly, SameSite=Lax, Secure for HTTPS.

CSRF cookie/header: `auth_csrf` and `X-CSRF-Token`; authenticated state-changing routes require an exact cookie/header match.

## Verification Results

| Evidence | Result |
| --- | --- |
| `auth-http-routes-unit.txt` | 41 passed |
| `auth-http-routes-integration.txt` | 8 passed |
| `auth-http-routes-regression.txt` | 234 passed |
| `auth-http-routes-full-suite.txt` | 295 passed |
| `auth-http-routes-compileall.txt` | passed with no output |
| `auth-http-routes-whitespace.txt` | `git diff --check` passed |
| `auth-http-routes-dbsize.txt` | MySQL temp DB count 0; Redis DB15 0 |
| `auth-http-routes-secret-scan.txt` | no real private keys, API tokens, SMTP passwords, credential-bearing DB URLs, or real session/CSRF secrets |
| `auth-http-routes-independent-qa.md` | APPROVED |

## Coverage Notes

The route integration suite uses real FastAPI ASGI calls with MySQL and Redis. It covers registration persistence, resend-verification for pending/missing/active/disabled accounts, email verification token reuse, login cookies, `/api/account/me`, logout CSRF and cookie clearing, change-password CSRF and session invalidation, forgot/reset password enumeration safety with a real reset token, sanitized Redis outage behavior, and cleanup of temporary MySQL databases and Redis DB15.

## Residual Risks / Later Slices

- Browser pages are not part of this slice.
- Account deletion is not part of this slice.
- Chat authorization is not part of this slice.
- Rate limiting and deployment SMTP environment wiring remain later work.
- Existing unit route tests still emit Starlette/httpx TestClient deprecation warnings; the real HTTP integration suite uses `httpx.AsyncClient` with `ASGITransport`.
