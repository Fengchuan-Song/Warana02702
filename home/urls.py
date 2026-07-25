from django.contrib import admin
from django.urls import path
from .views import current_typhoons, guangdong_marine_weather, index

urlpatterns = [
    path("", index, name='index'),
    path(
        "api/typhoons/current/",
        current_typhoons,
        name="current_typhoons",
    ),
    path(
        "api/environment/guangdong-marine/",
        guangdong_marine_weather,
        name="guangdong_marine_weather",
    ),
]
