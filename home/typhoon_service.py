import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
from django.core.cache import cache
from opencc import OpenCC


HKO_TYPHOON_LIST_URL = "https://www.weather.gov.hk/wxinfo/currwx/tc_list.xml"
HKO_ALLOWED_HOSTS = {"www.weather.gov.hk", "www.hko.gov.hk"}
HKO_CACHE_KEY = "hko:current_typhoons:v2"
HKO_STALE_CACHE_KEY = "hko:current_typhoons:stale:v2"
HKO_CACHE_SECONDS = 10 * 60
HKO_STALE_CACHE_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT = (5, 15)
HTTP_HEADERS = {
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5",
    "User-Agent": "WanAna02702-Typhoon-Layer/1.0",
}
HK_TO_SIMPLIFIED = OpenCC("hk2s")


class TyphoonDataError(RuntimeError):
    pass


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    if element is None:
        return None
    return next(
        (item for item in element if _local_name(item.tag) == name),
        None,
    )


def _children(element, name):
    if element is None:
        return []
    return [item for item in element if _local_name(item.tag) == name]


def _text(element, name, default=""):
    item = _child(element, name)
    if item is None or item.text is None:
        return default
    return item.text.strip()


def _simplify_chinese(value):
    return HK_TO_SIMPLIFIED.convert(value) if value else value


def _parse_coordinate(value, positive_suffix):
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([NSEW])\s*",
        value or "",
        flags=re.IGNORECASE,
    )
    if not match:
        raise TyphoonDataError(f"无法解析台风坐标：{value!r}")
    coordinate = float(match.group(1))
    suffix = match.group(2).upper()
    if suffix != positive_suffix:
        coordinate *= -1
    return coordinate


def _parse_wind(value):
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else None


def _parse_index(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_track_point(element):
    latitude = _text(element, "Latitude")
    longitude = _text(element, "Longitude")
    if not latitude or not longitude:
        return None

    maximum_wind = _text(element, "MaximumWind")
    return {
        "index": _parse_index(_text(element, "Index")),
        "time": _text(element, "Time") or None,
        "latitude": _parse_coordinate(latitude, "N"),
        "longitude": _parse_coordinate(longitude, "E"),
        "intensity": _text(element, "Intensity") or None,
        "maximum_wind": _parse_wind(maximum_wind),
        "maximum_wind_text": maximum_wind or None,
    }


def parse_typhoon_list(xml_content):
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise TyphoonDataError("香港天文台台风列表 XML 格式无效") from exc

    typhoons = []
    for element in root.iter():
        if _local_name(element.tag) != "TropicalCyclone":
            continue
        typhoon_id = _text(element, "TropicalCycloneID")
        track_url = _text(element, "TropicalCycloneURL")
        if not typhoon_id or not track_url:
            continue
        typhoons.append(
            {
                "id": typhoon_id,
                "name_zh": _simplify_chinese(
                    _text(element, "TropicalCycloneChineseName")
                ),
                "name_en": _text(element, "TropicalCycloneEnglishName"),
                "track_url": track_url,
            }
        )
    return typhoons


def parse_typhoon_track(xml_content, summary):
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise TyphoonDataError(
            f"台风 {summary['id']} 的轨迹 XML 格式无效"
        ) from exc

    bulletin_header = next(
        (
            item
            for item in root.iter()
            if _local_name(item.tag) == "BulletinHeader"
        ),
        None,
    )
    weather_report = next(
        (
            item
            for item in root.iter()
            if _local_name(item.tag) == "WeatherReport"
        ),
        None,
    )
    if weather_report is None:
        raise TyphoonDataError(f"台风 {summary['id']} 缺少轨迹数据")

    past = [
        point
        for point in (
            _parse_track_point(item)
            for item in _children(weather_report, "PastInformation")
        )
        if point is not None
    ]
    analyses = [
        point
        for point in (
            _parse_track_point(item)
            for item in _children(weather_report, "AnalysisInformation")
        )
        if point is not None
    ]
    forecast = [
        point
        for point in (
            _parse_track_point(item)
            for item in _children(weather_report, "ForecastInformation")
        )
        if point is not None
    ]

    current = analyses[-1] if analyses else (past[-1] if past else None)
    if current is None:
        raise TyphoonDataError(f"台风 {summary['id']} 缺少当前位置")

    return {
        "id": summary["id"],
        "name_zh": summary["name_zh"],
        "name_en": summary["name_en"],
        "display_name": (
            summary["name_zh"]
            or summary["name_en"]
            or _text(weather_report, "TropicalCycloneName")
            or summary["id"]
        ),
        "bulletin_time": _text(bulletin_header, "BulletinTime") or None,
        "past": past,
        "current": current,
        "forecast": forecast,
    }


def _validate_track_url(value):
    track_url = urljoin(HKO_TYPHOON_LIST_URL, value)
    parsed = urlparse(track_url)
    if parsed.hostname not in HKO_ALLOWED_HOSTS:
        raise TyphoonDataError("香港天文台返回了不受信任的轨迹地址")
    if parsed.scheme == "http":
        # The live list still publishes an HTTP track link. Upgrade it instead
        # of sending an unencrypted request; the same resource supports HTTPS.
        return parsed._replace(scheme="https").geturl()
    if parsed.scheme != "https":
        raise TyphoonDataError("香港天文台返回了不受信任的轨迹地址")
    return track_url


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


def _request_xml(url, trust_environment=True):
    request_kwargs = {
        "headers": HTTP_HEADERS,
        "timeout": HTTP_TIMEOUT,
    }
    if trust_environment:
        response = requests.get(url, **request_kwargs)
    else:
        # Daphne may inherit an invalid HTTP(S)_PROXY or Windows system proxy.
        # A dedicated session with trust_env disabled performs a real direct
        # connection without changing proxy behaviour elsewhere in the project.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, **request_kwargs)

    response.raise_for_status()
    return response.content


def _download_xml(url):
    try:
        return _request_xml(url)
    except requests.exceptions.ProxyError as proxy_exc:
        try:
            return _request_xml(url, trust_environment=False)
        except requests.RequestException as direct_exc:
            raise TyphoonDataError(
                "无法连接台风数据源：代理连接失败，"
                f"自动直连也失败：{direct_exc}"
            ) from direct_exc
    except requests.RequestException as exc:
        raise TyphoonDataError(f"无法连接台风数据源：{exc}") from exc


def get_current_typhoons(force_refresh=False):
    if not force_refresh:
        cached = _safe_cache_get(HKO_CACHE_KEY)
        if cached is not None:
            return cached

    try:
        summaries = parse_typhoon_list(_download_xml(HKO_TYPHOON_LIST_URL))
        typhoons = []
        errors = []
        for summary in summaries:
            try:
                track_url = _validate_track_url(summary["track_url"])
                typhoons.append(
                    parse_typhoon_track(_download_xml(track_url), summary)
                )
            except TyphoonDataError as exc:
                errors.append(str(exc))

        if summaries and not typhoons:
            raise TyphoonDataError("当前台风轨迹均加载失败")

        result = {
            "source": {
                "name": "香港天文台",
                "url": "https://www.weather.gov.hk/sc/wxinfo/currwx/tc_gis.htm",
            },
            "typhoons": typhoons,
            "partial_errors": errors,
            "stale": False,
        }
        _safe_cache_set(HKO_CACHE_KEY, result, HKO_CACHE_SECONDS)
        _safe_cache_set(
            HKO_STALE_CACHE_KEY,
            result,
            HKO_STALE_CACHE_SECONDS,
        )
        return result
    except TyphoonDataError:
        stale = _safe_cache_get(HKO_STALE_CACHE_KEY)
        if stale is None:
            raise
        stale_result = dict(stale)
        stale_result["stale"] = True
        return stale_result
