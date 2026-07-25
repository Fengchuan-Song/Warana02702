# Deviation/urls.py

from django.urls import path
from AISData.views import cached_detection_result

urlpatterns = [
    # path("", index, name='index'), # 原始注释掉的行
    path(
        'detectDeviation/',
        cached_detection_result,
        {'feature_id': 'detect-deviation'},
        name='detect_deviation',
    ),
]
