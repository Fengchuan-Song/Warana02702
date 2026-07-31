# your_project/routing.py
from django.urls import path
from AISData.consumers import AisConsumer # 替换为您的应用名
from home.consumers import CameraStreamConsumer

websocket_urlpatterns = [
    path('ws/ais/', AisConsumer.as_asgi()), # WebSocket 连接路径
    path(
        'ws/cameras/<slug:camera_key>/stream/',
        CameraStreamConsumer.as_asgi(),
    ),
]
