# your_project/routing.py
from django.urls import path
from AISData.consumers import AisConsumer, ViolationConsumer
from home.consumers import CameraStreamConsumer

websocket_urlpatterns = [
    path('ws/ais/', AisConsumer.as_asgi()), # WebSocket 连接路径
    path('ws/violations/', ViolationConsumer.as_asgi()),
    path(
        'ws/cameras/<slug:camera_key>/stream/',
        CameraStreamConsumer.as_asgi(),
    ),
]
