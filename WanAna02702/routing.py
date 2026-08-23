# your_project/routing.py
from django.conf import settings
from django.urls import path
from AISData.consumers import AisConsumer, PredictAisConsumer, ViolationConsumer
from home.consumers import CameraStreamConsumer

def default_ais_consumer():
    return (
        PredictAisConsumer
        if settings.AIS_DEFAULT_SOURCE == "predict"
        else AisConsumer
    )


DefaultAisConsumer = default_ais_consumer()

websocket_urlpatterns = [
    path('ws/ais/', DefaultAisConsumer.as_asgi()), # WebSocket 连接路径
    # Retained as a compatibility alias; the frontend always uses /ws/ais/.
    path('ws/ais-predict/', PredictAisConsumer.as_asgi()),
    path('ws/violations/', ViolationConsumer.as_asgi()),
    path(
        'ws/cameras/<slug:camera_key>/stream/',
        CameraStreamConsumer.as_asgi(),
    ),
]
