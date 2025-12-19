"""
URL configuration for WanAna02702 project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.auth import views as auth_view
from django.urls import path, include


urlpatterns = [
    # path("", auth_view.LoginView.as_view(), name='login'),
    path("", include('home.urls')),
    path("admin/", admin.site.urls),

    # 融合模型
    path("VISAIS/", include('VISAIS.urls')),            # 可见光-AIS数据融合
    path("AISRadar/", include('AISRadar.urls')),        # AIS-Radar数据融合
    path("UAVVISAIS/", include('UAVVISAIS.urls')),      # 无人机段视觉-AIS数据融合
    path("VISINF/", include('VISINF.urls')),            # 可见光-红外数据融合
    path("SARAIS/", include('SARAIS.urls')),            # SAR-AIS数据融合

    # 违法事件预警
    path("Smuggling/", include('Smuggling.urls')),                      # 海上走私
    path("HighSpeedBoat/", include('HighSpeedBoat.urls')),              # 高速快艇
    path("Forgery/", include('Forgery.urls')),                          # 身份伪造
    path("Overload/", include('Overload.urls')),                        # 船舶超载
    path("IllegalFishing/", include('IllegalFishing.urls')),            # 非法捕捞
    path("IllegalSandMining/", include('IllegalSandMining.urls')),      # 盗采海砂
    path("IllegalFarming/", include('IllegalFarming.urls')),            # 非法养殖
    path("IllegalpollutionDis/", include('IllegalPolutionDis.urls')),   # 非法排污
    path("IllegalStaying/", include('IllegalStaying.urls')),            # 非法驻留
    path("AbnormalWandering/", include('AbnormalWandering.urls')),      # 异常徘徊
    path("AbnormalParking/", include('AbnormalParking.urls')),          # 异常停泊
    path("IllegalAnchored/", include('IllegalAnchored.urls')),          # 非法抛锚
    path("CrossingBoundary/", include('CrossingBoundary.urls')),        # 海上围栏越界
    path("DoubleDragging/", include('DoubleDragging.urls')),            # 双拖
    path("AbnormalTransfer/", include('AbnormalTransfer.urls')),        # 异常接驳
    path("Deviation/", include('Deviation.urls')),                      # 航道偏离
    path("Collision/", include('Collision.urls')),                      # 船舶碰撞
    path("BlackList/", include('BlackList.urls')),                      # 黑名单预警
    path("LowSpeed/", include('LowSpeed.urls')),                        # 低速预警
    path("IllegalBerthing/", include('IllegalBerthing.urls')),          # 非法搭靠
]
