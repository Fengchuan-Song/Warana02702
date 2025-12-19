# your_project/routing.py
from django.urls import path
from AISData.consumers import AisConsumer # 替换为您的应用名

websocket_urlpatterns = [
    path('ws/ais/', AisConsumer.as_asgi()), # WebSocket 连接路径
]