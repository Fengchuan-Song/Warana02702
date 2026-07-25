from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from .marine_weather_service import (
    MarineWeatherDataError,
    get_guangdong_marine_weather,
)
from .typhoon_service import TyphoonDataError, get_current_typhoons


@ensure_csrf_cookie
def index(request):
    return render(request, 'Demo_v10.html')


@never_cache
@require_GET
def current_typhoons(request):
    try:
        result = get_current_typhoons()
    except TyphoonDataError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "typhoons": [],
            },
            status=502,
        )

    return JsonResponse(
        {
            "success": True,
            **result,
        }
    )


@never_cache
@require_GET
def guangdong_marine_weather(request):
    try:
        result = get_guangdong_marine_weather()
    except MarineWeatherDataError as exc:
        return JsonResponse(
            {
                "success": False,
                "message": str(exc),
                "points": [],
            },
            status=502,
        )

    return JsonResponse(
        {
            "success": True,
            **result,
        }
    )
