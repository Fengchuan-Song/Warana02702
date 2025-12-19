# DoubleDragging/urls.py
from django.urls import path
from .views import detect_double_dragging

urlpatterns = [
    path('detectDoubleDragging/', detect_double_dragging, name='detect_double_dragging'),
]