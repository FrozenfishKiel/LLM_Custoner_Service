# -*- coding: utf-8 -*-
"""
atguigu_ai API模块

提供基于FastAPI的Web服务接口。
"""

from atguigu_ai.api.dependencies import AuthRouteDependencies
from atguigu_ai.api.production import build_production_auth_deps, create_production_app
from atguigu_ai.api.routes.chat import ChatRouteDependencies
from atguigu_ai.api.server import AtguiguServer, create_app

__all__ = [
    "AtguiguServer",
    "AuthRouteDependencies",
    "ChatRouteDependencies",
    "build_production_auth_deps",
    "create_app",
    "create_production_app",
]
