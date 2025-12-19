from django.urls import path
from . import views

urlpatterns = [
    path('detectIllegalStaying/', views.detectIllegalStaying, name='detectIllegalStaying'),
]