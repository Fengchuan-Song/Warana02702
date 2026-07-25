import requests
from django.core.cache import cache


OPEN_METEO_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
MARINE_WEATHER_CACHE_KEY = "open_meteo:guangdong_marine:v1"
MARINE_WEATHER_STALE_CACHE_KEY = "open_meteo:guangdong_marine:stale:v1"
MARINE_WEATHER_CACHE_SECONDS = 15 * 60
MARINE_WEATHER_STALE_CACHE_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT = (5, 20)
HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "WanAna02702-Marine-Weather-Layer/1.0",
}

# Sampling points follow the Guangdong coastline from west to east. They are
# deliberately placed offshore because wave and current models select sea cells.
GUANGDONG_COASTAL_POINTS = [
    {"id": "qiongzhou_east", "name": "琼州海峡东口", "latitude": 20.4, "longitude": 110.4},
    {"id": "zhanjiang", "name": "湛江近海", "latitude": 20.9, "longitude": 110.8},
    {"id": "maoming", "name": "茂名近海", "latitude": 21.1, "longitude": 111.4},
    {"id": "yangjiang", "name": "阳江近海", "latitude": 21.4, "longitude": 112.2},
    {"id": "taishan", "name": "台山近海", "latitude": 21.5, "longitude": 112.9},
    {"id": "pearl_west", "name": "珠江口西", "latitude": 21.7, "longitude": 113.4},
    {"id": "pearl_east", "name": "珠江口东", "latitude": 21.9, "longitude": 114.0},
    {"id": "daya_bay", "name": "大亚湾外海", "latitude": 22.1, "longitude": 114.7},
    {"id": "shanwei", "name": "汕尾近海", "latitude": 22.3, "longitude": 115.3},
    {"id": "jieyang", "name": "揭阳近海", "latitude": 22.5, "longitude": 116.0},
    {"id": "shantou", "name": "汕头近海", "latitude": 22.8, "longitude": 116.7},
    {"id": "nanao", "name": "南澳外海", "latitude": 23.0, "longitude": 117.2},
]


class MarineWeatherDataError(RuntimeError):
    pass


def _safe_cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _safe_cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        pass


def _request_json(url, params, trust_environment=True):
    request_kwargs = {
        "params": params,
        "headers": HTTP_HEADERS,
        "timeout": HTTP_TIMEOUT,
    }
    if trust_environment:
        response = requests.get(url, **request_kwargs)
    else:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, **request_kwargs)

    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise MarineWeatherDataError(
            f"Open-Meteo 返回错误：{payload.get('reason', '未知错误')}"
        )
    return payload


def _download_json(url, params):
    try:
        return _request_json(url, params)
    except requests.exceptions.ProxyError:
        try:
            return _request_json(url, params, trust_environment=False)
        except requests.RequestException as direct_exc:
            raise MarineWeatherDataError(
                "气象数据代理连接失败，"
                f"自动直连也失败：{direct_exc}"
            ) from direct_exc
    except requests.RequestException as exc:
        raise MarineWeatherDataError(
            f"无法连接气象数据源：{exc}"
        ) from exc


def _as_list(payload):
    return payload if isinstance(payload, list) else [payload]


def _number(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def build_marine_weather_payload(weather_payload, marine_payload):
    weather_locations = _as_list(weather_payload)
    marine_locations = _as_list(marine_payload)
    expected_count = len(GUANGDONG_COASTAL_POINTS)
    if (
        len(weather_locations) != expected_count
        or len(marine_locations) != expected_count
    ):
        raise MarineWeatherDataError("气象数据源返回的采样点数量不完整")

    points = []
    for definition, weather_location, marine_location in zip(
        GUANGDONG_COASTAL_POINTS,
        weather_locations,
        marine_locations,
    ):
        weather = weather_location.get("current") or {}
        marine = marine_location.get("current") or {}
        points.append(
            {
                **definition,
                "time": weather.get("time") or marine.get("time"),
                "wind": {
                    "speed": _number(weather.get("wind_speed_10m")),
                    "direction": _number(weather.get("wind_direction_10m")),
                    "gust": _number(weather.get("wind_gusts_10m")),
                },
                "wave": {
                    "height": _number(marine.get("wave_height")),
                    "direction": _number(marine.get("wave_direction")),
                },
                "current": {
                    "speed": _number(
                        marine.get("ocean_current_velocity")
                    ),
                    "direction": _number(
                        marine.get("ocean_current_direction")
                    ),
                },
                "sea_surface_temperature": _number(
                    marine.get("sea_surface_temperature")
                ),
            }
        )

    return {
        "source": {
            "name": "Open-Meteo",
            "url": "https://open-meteo.com/en/docs/marine-weather-api",
            "attribution": "Open-Meteo / DWD / Météo-France",
        },
        "units": {
            "wind_speed": "m/s",
            "wave_height": "m",
            "current_speed": "m/s",
            "direction": "°",
            "sea_surface_temperature": "°C",
        },
        "points": points,
        "stale": False,
        "notice": "数值模式数据仅供参考，不用于航行决策",
    }


def get_guangdong_marine_weather(force_refresh=False):
    if not force_refresh:
        cached = _safe_cache_get(MARINE_WEATHER_CACHE_KEY)
        if cached is not None:
            return cached

    coordinates = {
        "latitude": ",".join(
            str(point["latitude"]) for point in GUANGDONG_COASTAL_POINTS
        ),
        "longitude": ",".join(
            str(point["longitude"]) for point in GUANGDONG_COASTAL_POINTS
        ),
    }
    weather_params = {
        **coordinates,
        "current": (
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        ),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Shanghai",
    }
    marine_params = {
        **coordinates,
        "current": (
            "wave_height,wave_direction,ocean_current_velocity,"
            "ocean_current_direction,sea_surface_temperature"
        ),
        "wind_speed_unit": "ms",
        "timezone": "Asia/Shanghai",
        "cell_selection": "sea",
    }

    try:
        result = build_marine_weather_payload(
            _download_json(OPEN_METEO_WEATHER_URL, weather_params),
            _download_json(OPEN_METEO_MARINE_URL, marine_params),
        )
        _safe_cache_set(
            MARINE_WEATHER_CACHE_KEY,
            result,
            MARINE_WEATHER_CACHE_SECONDS,
        )
        _safe_cache_set(
            MARINE_WEATHER_STALE_CACHE_KEY,
            result,
            MARINE_WEATHER_STALE_CACHE_SECONDS,
        )
        return result
    except MarineWeatherDataError:
        stale = _safe_cache_get(MARINE_WEATHER_STALE_CACHE_KEY)
        if stale is None:
            raise
        stale_result = dict(stale)
        stale_result["stale"] = True
        return stale_result
