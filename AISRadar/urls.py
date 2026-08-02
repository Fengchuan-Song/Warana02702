from django.urls import path

from . import views


app_name = "ais_radar"

urlpatterns = [
    path("", views.health, name="health"),
    path("fused-targets/", views.fused_targets, name="fused_targets"),
    path("match/", views.match, name="match"),
]
