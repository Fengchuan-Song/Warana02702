from django.urls import path
from .views import detect_low_speed

urlpatterns = [
    path('detectLowSpeed/', detect_low_speed, name='detect_low_speed'),
]