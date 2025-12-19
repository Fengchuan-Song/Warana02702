from django.urls import path
from . import views

urlpatterns = [
    path('detectAbnormalParking/', views.detectAbnormalParking, name='detectAbnormalParking'),
]