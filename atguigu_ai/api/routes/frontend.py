from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse


_FRONTEND_PAGES = {
    "",
    "login",
    "register",
    "forgot-password",
    "reset-password",
    "account",
    "chat",
}


def create_frontend_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def customer_frontend_root(request: Request) -> HTMLResponse:
        html = _template().replace("{{ page_name }}", "chat")
        return HTMLResponse(html)

    @router.get("/{page_name}", response_class=HTMLResponse)
    async def customer_frontend(page_name: str, request: Request) -> HTMLResponse:
        if page_name not in _FRONTEND_PAGES:
            return HTMLResponse("Not Found", status_code=404)
        html = _template().replace("{{ page_name }}", page_name or "chat")
        return HTMLResponse(html)

    return router


def _template() -> str:
    template_path = Path(__file__).resolve().parents[1] / "templates" / "customer_frontend.html"
    return template_path.read_text(encoding="utf-8")


__all__ = ["create_frontend_router"]
