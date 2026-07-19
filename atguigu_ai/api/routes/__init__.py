from atguigu_ai.api.routes.auth import create_auth_router
from atguigu_ai.api.routes.chat import ChatRouteDependencies, create_chat_router
from atguigu_ai.api.routes.frontend import create_frontend_router

__all__ = [
    "ChatRouteDependencies",
    "create_auth_router",
    "create_chat_router",
    "create_frontend_router",
]
