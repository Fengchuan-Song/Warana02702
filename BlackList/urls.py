from django.contrib import admin
from django.urls import path
from .views import detect_black_list

urlpatterns = [
    path('detectBlackList/', detect_black_list, name='detect_black_list'),
]