from django.urls import path

from . import views


app_name = "forgery"

urlpatterns = [
    path("", views.latest_result, name="latest_result"),
]
