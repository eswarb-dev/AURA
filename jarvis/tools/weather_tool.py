"""Weather tool with strict location resolution and provider abstraction."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .tool_types import tool_response

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 80: "rain showers",
    95: "thunderstorms",
}

REGION_ONLY_LOCATIONS = {
    "africa", "antarctica", "asia", "australia", "europe", "north america",
    "south america", "world", "earth", "global", "india", "tamil nadu",
    "karnataka", "kerala",
}

KNOWN_LOCATIONS = {
    "coimbatore": (11.0168, 76.9558, "Coimbatore, Tamil Nadu, India", "India", "Tamil Nadu"),
    "chennai": (13.0827, 80.2707, "Chennai, Tamil Nadu, India", "India", "Tamil Nadu"),
    "mumbai": (19.0760, 72.8777, "Mumbai, Maharashtra, India", "India", "Maharashtra"),
    "bangalore": (12.9716, 77.5946, "Bengaluru, Karnataka, India", "India", "Karnataka"),
    "bengaluru": (12.9716, 77.5946, "Bengaluru, Karnataka, India", "India", "Karnataka"),
}

LOCATION_MARKERS = (
    "weather in ", "weather at ", "weather for ",
    "forecast in ", "forecast at ", "forecast for ",
    "temperature in ", "temperature at ", "temperature for ",
    "rain chance in ", "rain chance at ", "rain chance for ",
    "rain in ", "rain at ", "rain for ",
)

_CACHE: dict[str, dict[str, Any]] = {}


@dataclass
class WeatherLocationResolution:
    location_raw: str | None
    location_normalized: str | None
    resolved_name: str | None
    country: str | None
    admin1: str | None
    latitude: float | None
    longitude: float | None
    used_default_location: bool
    geocoder_provider: str | None
    geocoder_status: str
    error: str | None


class WeatherError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(name, "")).strip() or default))
    except ValueError:
        return default


def _read_json(url: str, timeout: int | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout or _env_int("AURA_WEATHER_TIMEOUT_SECONDS", 8)) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _clean_location(value: str) -> str:
    value = re.sub(r"[?!.,;]+$", "", value.strip())
    value = re.sub(r"\b(today|tomorrow|now|please|sir|maam|ma'am)$", "", value, flags=re.IGNORECASE).strip()
    return " ".join(value.split())


def extract_weather_location(query_text: str) -> str | None:
    norm = " ".join(str(query_text or "").lower().replace("?", " ").replace(".", " ").split())
    for marker in LOCATION_MARKERS:
        if marker in norm:
            return _clean_location(norm.split(marker, 1)[1]).title() or None
    return None


def _default_city() -> str:
    return str(os.getenv("AURA_DEFAULT_CITY") or "Coimbatore").strip() or "Coimbatore"


def _default_country() -> str:
    return str(os.getenv("AURA_DEFAULT_COUNTRY") or "India").strip()


def _configured_default_coordinates() -> tuple[float, float] | None:
    lat = os.getenv("AURA_DEFAULT_LATITUDE")
    lon = os.getenv("AURA_DEFAULT_LONGITUDE")
    try:
        if lat and lon:
            return float(lat), float(lon)
    except ValueError:
        return None
    return None


def _known_resolution(raw: str | None, *, used_default: bool) -> WeatherLocationResolution | None:
    if not raw:
        return None
    known = KNOWN_LOCATIONS.get(raw.strip().lower())
    if not known:
        return None
    lat, lon, name, country, admin1 = known
    return WeatherLocationResolution(
        location_raw=None if used_default else raw,
        location_normalized=raw.strip().title(),
        resolved_name=name,
        country=country,
        admin1=admin1,
        latitude=lat,
        longitude=lon,
        used_default_location=used_default,
        geocoder_provider="local_allowlist",
        geocoder_status="ok",
        error=None,
    )


def _open_meteo_geocode(location: str, timeout: int) -> WeatherLocationResolution:
    query = urllib.parse.urlencode({"name": location, "count": 3, "language": "en", "format": "json"})
    data = _read_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}", timeout=timeout)
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return WeatherLocationResolution(location, location.title(), None, None, None, None, None, False, "open_meteo", "not_found", "location_not_found")
    first = results[0]
    name = str(first.get("name") or location).strip()
    country = str(first.get("country") or "").strip() or None
    admin1 = str(first.get("admin1") or "").strip() or None
    parts = [name, admin1, country]
    label = ", ".join([part for part in parts if part])
    return WeatherLocationResolution(
        location_raw=location,
        location_normalized=location.title(),
        resolved_name=label or name,
        country=country,
        admin1=admin1,
        latitude=float(first["latitude"]),
        longitude=float(first["longitude"]),
        used_default_location=False,
        geocoder_provider="open_meteo",
        geocoder_status="ok",
        error=None,
    )


def _openweather_geocode(location: str, timeout: int) -> WeatherLocationResolution:
    key = os.getenv("AURA_OPENWEATHER_API_KEY")
    if not key:
        raise WeatherError("weather_provider_config_missing", "AURA_OPENWEATHER_API_KEY is missing")
    query = urllib.parse.urlencode({"q": location, "limit": 3, "appid": key})
    data = _read_json(f"https://api.openweathermap.org/geo/1.0/direct?{query}", timeout=timeout)
    if not isinstance(data, list) or not data:
        return WeatherLocationResolution(location, location.title(), None, None, None, None, None, False, "openweather", "not_found", "location_not_found")
    first = data[0]
    name = str(first.get("name") or location).strip()
    country = str(first.get("country") or "").strip() or None
    admin1 = str(first.get("state") or "").strip() or None
    label = ", ".join([part for part in [name, admin1, country] if part])
    return WeatherLocationResolution(location, location.title(), label or name, country, admin1, float(first["lat"]), float(first["lon"]), False, "openweather", "ok", None)


def resolve_weather_location(query_text: str, provider: str = "open_meteo", params: dict[str, Any] | None = None) -> WeatherLocationResolution:
    params = params or {}
    timeout = _env_int("AURA_WEATHER_TIMEOUT_SECONDS", 8)
    raw = str(params.get("city") or params.get("location") or "").strip() or extract_weather_location(query_text)
    if raw:
        raw = _clean_location(raw)
        if raw.lower() in REGION_ONLY_LOCATIONS:
            return WeatherLocationResolution(raw, raw.title(), None, None, None, None, None, False, None, "too_broad", "city_required")
        known = _known_resolution(raw, used_default=False)
        if known:
            return known
        if provider == "openweather":
            return _openweather_geocode(raw, timeout)
        return _open_meteo_geocode(raw, timeout)

    if not _env_bool("AURA_WEATHER_ALLOW_DEFAULT_ONLY_WHEN_LOCATION_MISSING", True):
        return WeatherLocationResolution(None, None, None, None, None, None, None, False, None, "not_found", "default_location_disabled")

    city = _default_city()
    country = _default_country()
    coords = _configured_default_coordinates()
    known = _known_resolution(city, used_default=True)
    if known and not coords:
        return known
    if coords:
        lat, lon = coords
        label = f"{city}, {country}" if country else city
        return WeatherLocationResolution(None, city, label, country or None, None, lat, lon, True, "env_coordinates", "ok", None)
    resolved = _known_resolution(city, used_default=True)
    if resolved:
        return resolved
    lookup = f"{city}, {country}" if country else city
    resolved = _open_meteo_geocode(lookup, timeout)
    resolved.location_raw = None
    resolved.location_normalized = city
    resolved.used_default_location = True
    if resolved.geocoder_status == "ok":
        return resolved
    resolved.geocoder_status = "fallback_default"
    return resolved


def _provider_mode() -> str:
    value = str(os.getenv("AURA_WEATHER_PROVIDER") or "auto").strip().lower()
    return value if value in {"auto", "open_meteo", "weatherapi", "openweather"} else "auto"


def _active_provider(mode: str) -> tuple[str, str | None]:
    if mode == "weatherapi":
        return ("weatherapi", None) if os.getenv("AURA_WEATHERAPI_KEY") else ("weatherapi", "weatherapi_key_missing")
    if mode == "openweather":
        return ("openweather", None) if os.getenv("AURA_OPENWEATHER_API_KEY") else ("openweather", "openweather_key_missing")
    if mode == "open_meteo":
        return "open_meteo", None
    if os.getenv("AURA_WEATHERAPI_KEY"):
        return "weatherapi", None
    if os.getenv("AURA_OPENWEATHER_API_KEY"):
        return "openweather", None
    return "open_meteo", "no_keyed_provider_configured"


def provider_status() -> dict[str, Any]:
    mode = _provider_mode()
    active, missing = _active_provider(mode)
    provider_configured = not (mode in {"weatherapi", "openweather"} and missing)
    return {
        "enabled": True,
        "provider_configured": provider_configured,
        "provider_mode": mode,
        "active_provider": active,
        "weatherapi_configured": bool(os.getenv("AURA_WEATHERAPI_KEY")),
        "openweather_configured": bool(os.getenv("AURA_OPENWEATHER_API_KEY")),
        "default_location": f"{_default_city()}, {_default_country()}".rstrip(", "),
        "strict_location": _env_bool("AURA_WEATHER_STRICT_LOCATION", True),
        "cache_ttl_seconds": _env_int("AURA_WEATHER_CACHE_TTL_SECONDS", 600),
        "key_based_providers": {
            "weatherapi": {"configured": bool(os.getenv("AURA_WEATHERAPI_KEY"))},
            "openweather": {"configured": bool(os.getenv("AURA_OPENWEATHER_API_KEY"))},
        },
    }


def _debug(provider: str, resolution: WeatherLocationResolution, *, forecast_status: str, fallback_reason: str | None, cache_hit: bool) -> dict[str, Any]:
    return {
        "provider": provider,
        "location_raw": resolution.location_raw,
        "location_normalized": resolution.location_normalized,
        "resolved_name": resolution.resolved_name,
        "used_default_location": resolution.used_default_location,
        "latitude": resolution.latitude,
        "longitude": resolution.longitude,
        "geocoder_provider": resolution.geocoder_provider,
        "geocoder_status": resolution.geocoder_status,
        "forecast_status": forecast_status,
        "fallback_reason": fallback_reason,
        "cache_hit": cache_hit,
    }


def _cache_get(key: str, ttl: int) -> dict[str, Any] | None:
    entry = _CACHE.get(key)
    if not entry or time.time() - float(entry.get("stored_at") or 0) > ttl:
        return None
    return entry.get("value") if isinstance(entry.get("value"), dict) else None


def _cache_set(key: str, value: dict[str, Any]) -> None:
    _CACHE[key] = {"stored_at": time.time(), "value": value}


def _fetch_open_meteo(action: str, resolution: WeatherLocationResolution, timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode({
        "latitude": resolution.latitude,
        "longitude": resolution.longitude,
        "current": "temperature_2m,precipitation,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 2,
    })
    data = _read_json(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=timeout)
    return {"current": data.get("current", {}), "daily": data.get("daily", {})}


def _fetch_weatherapi(action: str, resolution: WeatherLocationResolution, timeout: int) -> dict[str, Any]:
    key = os.getenv("AURA_WEATHERAPI_KEY")
    if not key:
        raise WeatherError("weather_provider_config_missing", "AURA_WEATHERAPI_KEY is missing")
    query = urllib.parse.urlencode({"key": key, "q": f"{resolution.latitude},{resolution.longitude}", "days": 2, "aqi": "no", "alerts": "no"})
    data = _read_json(f"https://api.weatherapi.com/v1/forecast.json?{query}", timeout=timeout)
    current_raw = data.get("current", {}) if isinstance(data, dict) else {}
    days = ((data.get("forecast") or {}).get("forecastday") or []) if isinstance(data, dict) else []
    current = {
        "temperature_2m": current_raw.get("temp_c"),
        "precipitation": current_raw.get("precip_mm"),
        "weather_code": None,
        "condition_text": ((current_raw.get("condition") or {}).get("text")),
    }
    daily = {
        "temperature_2m_max": [(d.get("day") or {}).get("maxtemp_c") for d in days],
        "temperature_2m_min": [(d.get("day") or {}).get("mintemp_c") for d in days],
        "precipitation_probability_max": [(d.get("day") or {}).get("daily_chance_of_rain") for d in days],
    }
    return {"current": current, "daily": daily}


def _fetch_openweather(action: str, resolution: WeatherLocationResolution, timeout: int) -> dict[str, Any]:
    key = os.getenv("AURA_OPENWEATHER_API_KEY")
    if not key:
        raise WeatherError("weather_provider_config_missing", "AURA_OPENWEATHER_API_KEY is missing")
    query = urllib.parse.urlencode({"lat": resolution.latitude, "lon": resolution.longitude, "appid": key, "units": "metric"})
    data = _read_json(f"https://api.openweathermap.org/data/2.5/weather?{query}", timeout=timeout)
    main = data.get("main", {}) if isinstance(data, dict) else {}
    weather = (data.get("weather") or [{}])[0] if isinstance(data, dict) else {}
    current = {
        "temperature_2m": main.get("temp"),
        "precipitation": ((data.get("rain") or {}).get("1h") or 0) if isinstance(data, dict) else None,
        "weather_code": weather.get("id"),
        "condition_text": weather.get("description"),
    }
    daily = {
        "temperature_2m_max": [main.get("temp_max"), main.get("temp_max")],
        "temperature_2m_min": [main.get("temp_min"), main.get("temp_min")],
        "precipitation_probability_max": [None, None],
    }
    return {"current": current, "daily": daily}


def _condition_text(current: dict[str, Any]) -> str:
    if current.get("condition_text"):
        return str(current["condition_text"])
    try:
        return WEATHER_CODES.get(int(current.get("weather_code") or 0), "weather data")
    except (TypeError, ValueError):
        return "weather data"


def _response_text(action: str, resolution: WeatherLocationResolution, forecast: dict[str, Any]) -> str:
    label = resolution.resolved_name or resolution.location_normalized or _default_city()
    current = forecast.get("current") or {}
    daily = forecast.get("daily") or {}
    idx = 1 if action == "forecast_tomorrow" else 0
    temp = current.get("temperature_2m")
    rain = (daily.get("precipitation_probability_max") or [None, None])[idx]
    low = (daily.get("temperature_2m_min") or [None, None])[idx]
    high = (daily.get("temperature_2m_max") or [None, None])[idx]
    prefix = f"Using your default location, {label}" if resolution.used_default_location else f"{'Tomorrow' if idx else 'Today'} in {label}"
    if action == "temperature_now":
        prefix = f"Using your default location, {label}" if resolution.used_default_location else f"In {label}"
        return f"{prefix}, it is around {temp} degrees Celsius, Sir."
    if action == "rain_check":
        return f"Rain chance in {label} is around {rain} percent today, Sir."
    return f"{prefix}, it is {_condition_text(current)}, around {temp} degrees now, with {low} to {high} degrees and {rain} percent rain chance, Sir."


def _failure_response(action: str, provider: str, resolution: WeatherLocationResolution, error_type: str, message: str, fallback_reason: str | None = None) -> dict[str, Any]:
    if error_type == "weather_location_too_broad":
        text = f"{resolution.location_raw or 'That location'} is too broad for a local forecast, Sir. Please name a city, such as London, Paris, or Berlin."
    elif error_type == "weather_location_not_found":
        text = "I could not resolve that weather location, Sir. Please name a city-level location."
    elif error_type == "weather_provider_config_missing":
        text = "That weather provider is not configured, Sir. Add the provider key or switch AURA_WEATHER_PROVIDER to auto or open_meteo."
    elif error_type == "weather_provider_timeout":
        text = "The weather provider timed out, Sir."
    else:
        text = "I could not reach the weather service right now, Sir."
    return tool_response(
        ok=False,
        tool="weather",
        action=action,
        executed=False,
        response_text=text,
        error_type=error_type,
        error_message=message[:160],
        metadata={"weather": _debug(provider, resolution, forecast_status="failed", fallback_reason=fallback_reason, cache_hit=False)},
    )


def execute(action: str, params: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    context = context or {}
    query_text = str(params.get("query_text") or context.get("text") or context.get("normalized_text") or "")
    mode = _provider_mode()
    provider, auto_reason = _active_provider(mode)
    timeout = _env_int("AURA_WEATHER_TIMEOUT_SECONDS", 8)
    ttl = _env_int("AURA_WEATHER_CACHE_TTL_SECONDS", 600)

    if mode in {"weatherapi", "openweather"} and auto_reason:
        resolution = WeatherLocationResolution(params.get("city"), params.get("city"), None, None, None, None, None, False, None, "error", auto_reason)
        return _failure_response(action, provider, resolution, "weather_provider_config_missing", auto_reason, auto_reason)

    try:
        resolution = resolve_weather_location(query_text, "openweather" if provider == "openweather" else "open_meteo", params)
    except WeatherError as exc:
        resolution = WeatherLocationResolution(params.get("city"), params.get("city"), None, None, None, None, None, False, provider, "error", exc.error_type)
        return _failure_response(action, provider, resolution, exc.error_type, str(exc), exc.error_type)
    except TimeoutError as exc:
        resolution = WeatherLocationResolution(params.get("city"), params.get("city"), None, None, None, None, None, False, provider, "error", "timeout")
        return _failure_response(action, provider, resolution, "weather_provider_timeout", str(exc), "geocoder_timeout")
    except Exception as exc:
        resolution = WeatherLocationResolution(params.get("city"), params.get("city"), None, None, None, None, None, False, provider, "error", "geocoder_error")
        return _failure_response(action, provider, resolution, "weather_provider_error", str(exc), "geocoder_error")

    if resolution.geocoder_status == "too_broad":
        return _failure_response(action, provider, resolution, "weather_location_too_broad", resolution.error or "city_required", "city_required")
    if resolution.geocoder_status not in {"ok"} or resolution.latitude is None or resolution.longitude is None:
        return _failure_response(action, provider, resolution, "weather_location_not_found", resolution.error or "location_not_found", resolution.geocoder_status)

    cache_key = f"{provider}:{action}:{round(resolution.latitude, 4)}:{round(resolution.longitude, 4)}"
    cached = _cache_get(cache_key, ttl)
    if cached:
        forecast = cached
        cache_hit = True
    else:
        try:
            if provider == "weatherapi":
                forecast = _fetch_weatherapi(action, resolution, timeout)
            elif provider == "openweather":
                forecast = _fetch_openweather(action, resolution, timeout)
            else:
                forecast = _fetch_open_meteo(action, resolution, timeout)
            _cache_set(cache_key, forecast)
            cache_hit = False
        except WeatherError as exc:
            return _failure_response(action, provider, resolution, exc.error_type, str(exc), exc.error_type)
        except urllib.error.URLError as exc:
            err = "weather_provider_timeout" if "timed out" in str(exc).lower() else "weather_provider_error"
            return _failure_response(action, provider, resolution, err, str(exc), "forecast_fetch_failed")
        except TimeoutError as exc:
            return _failure_response(action, provider, resolution, "weather_provider_timeout", str(exc), "forecast_timeout")
        except Exception as exc:
            return _failure_response(action, provider, resolution, "weather_provider_error", str(exc), "forecast_error")

    return tool_response(
        ok=True,
        tool="weather",
        action=action,
        executed=True,
        result={**forecast, **asdict(resolution)},
        response_text=_response_text(action, resolution, forecast),
        metadata={"weather": _debug(provider, resolution, forecast_status="ok", fallback_reason=auto_reason, cache_hit=cache_hit)},
    )
