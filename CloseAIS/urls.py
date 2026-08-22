from django.urls import path

from . import views


app_name = "close_ais"

urlpatterns = [
    path("", views.latest_result, name="latest_result"),
]
