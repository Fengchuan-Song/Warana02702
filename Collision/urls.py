from django.urls import path
from .views import detect_collision

urlpatterns = [
    path('detectCollision/', detect_collision, name='detect_collision'),
]
