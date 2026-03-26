from django.contrib import admin
from django.urls import path
from .views import detect_high_speed

urlpatterns = [
    path("detectHighSpeedBoat/", detect_high_speed, name='detect_high_speed'),
]