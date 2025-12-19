"""
ASGI config for WanAna02702 project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
from . import routing # 导入同级目录的 routing.py
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "WanAna02702.settings")

application = ProtocolTypeRouter({
    # Django 的默认 HTTP 路由
    'http': get_asgi_application(),
    
    # WebSocket 路由
    'websocket': AuthMiddlewareStack(
        URLRouter(
            routing.websocket_urlpatterns
        )
    ),
})
