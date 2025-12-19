# Deviation/urls.py

from django.urls import path
from .views import detect_deviation

urlpatterns = [
    # path("", index, name='index'), # 原始注释掉的行
    path('detectDeviation/', detect_deviation, name='detect_deviation'),
]