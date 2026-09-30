from jarvis.tools import tool_router, weather_tool


def _forecast_payload(temp=30):
    return {
        "current": {"temperature_2m": temp, "precipitation": 0, "weather_code": 1},
        "daily": {
            "temperature_2m_max": [34, 35],
            "temperature_2m_min": [24, 25],
            "precipitation_probability_max": [20, 30],
        },
    }


def _open_meteo_payload(temp=30):
    return {
        "current": _forecast_payload(temp)["current"],
        "daily": _forecast_payload(temp)["daily"],
    }


def setup_function():
    weather_tool._CACHE.clear()


def test_default_location_uses_default_only_when_location_missing(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    detection = tool_router.detect_tool_command("what's the weather now")
    assert detection["tool_name"] == "weather"
    assert detection["params"].get("city") is None

    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: _open_meteo_payload())
    result = tool_router.execute_aura_tool(detection["tool_name"], detection["action"], detection["params"], {})

    debug = result["metadata"]["weather"]
    assert result["ok"] is True
    assert debug["used_default_location"] is True
    assert "Coimbatore" in debug["resolved_name"]


def test_explicit_coimbatore_is_not_marked_default(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: _open_meteo_payload())
    result = weather_tool.execute("forecast_today", {"city": "Coimbatore", "query_text": "weather in coimbatore"}, {})
    debug = result["metadata"]["weather"]
    assert debug["used_default_location"] is False
    assert "Coimbatore" in debug["resolved_name"]


def test_explicit_chennai_does_not_use_coimbatore_coordinates(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: _open_meteo_payload())
    result = weather_tool.execute("forecast_today", {"city": "Chennai", "query_text": "weather in chennai"}, {})
    debug = result["metadata"]["weather"]
    assert debug["used_default_location"] is False
    assert "Chennai" in debug["resolved_name"]
    assert (round(debug["latitude"], 4), round(debug["longitude"], 4)) != (11.0168, 76.9558)


def test_explicit_bangalore_resolves_to_bengaluru(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: _open_meteo_payload())
    detection = tool_router.detect_tool_command("temperature in Bangalore")
    result = tool_router.execute_aura_tool(detection["tool_name"], detection["action"], detection["params"], {})
    debug = result["metadata"]["weather"]
    assert debug["used_default_location"] is False
    assert "Bengaluru" in debug["resolved_name"] or "Bangalore" in debug["resolved_name"]
    assert (round(debug["latitude"], 4), round(debug["longitude"], 4)) != (11.0168, 76.9558)


def test_too_broad_location_does_not_fetch_forecast(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    calls = []
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: calls.append(args) or {})
    result = weather_tool.execute("forecast_today", {"city": "Europe", "query_text": "weather in europe"}, {})
    debug = result["metadata"]["weather"]
    assert result["ok"] is False
    assert result["error_type"] == "weather_location_too_broad"
    assert debug["geocoder_status"] == "too_broad"
    assert calls == []
    assert "Europe, India" not in result["response_text"]


def test_unknown_explicit_location_does_not_fall_back_to_default(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: {"results": []})
    result = weather_tool.execute("forecast_today", {"city": "abcxyznotaplace", "query_text": "weather in abcxyznotaplace"}, {})
    debug = result["metadata"]["weather"]
    assert result["ok"] is False
    assert result["error_type"] == "weather_location_not_found"
    assert debug["used_default_location"] is False
    assert debug["geocoder_status"] == "not_found"


def test_weatherapi_missing_key_reports_config_missing(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "weatherapi")
    monkeypatch.delenv("AURA_WEATHERAPI_KEY", raising=False)
    result = weather_tool.execute("forecast_today", {"city": "Chennai", "query_text": "weather in chennai"}, {})
    assert result["ok"] is False
    assert result["error_type"] == "weather_provider_config_missing"


def test_auto_without_key_uses_open_meteo(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "auto")
    monkeypatch.delenv("AURA_WEATHERAPI_KEY", raising=False)
    monkeypatch.delenv("AURA_OPENWEATHER_API_KEY", raising=False)
    monkeypatch.setattr(weather_tool, "_read_json", lambda *args, **kwargs: _open_meteo_payload())
    result = weather_tool.execute("forecast_today", {"city": "Chennai", "query_text": "weather in chennai"}, {})
    assert result["ok"] is True
    assert result["metadata"]["weather"]["provider"] == "open_meteo"


def test_cache_does_not_cross_contaminate_locations(monkeypatch):
    monkeypatch.setenv("AURA_WEATHER_PROVIDER", "open_meteo")
    calls = []

    def fake_read_json(url, *args, **kwargs):
        calls.append(url)
        return _open_meteo_payload(temp=29 if "11.0168" in url else 32)

    monkeypatch.setattr(weather_tool, "_read_json", fake_read_json)
    first = weather_tool.execute("forecast_today", {"city": "Coimbatore", "query_text": "weather in coimbatore"}, {})
    second = weather_tool.execute("forecast_today", {"city": "Chennai", "query_text": "weather in chennai"}, {})
    assert first["metadata"]["weather"]["cache_hit"] is False
    assert second["metadata"]["weather"]["cache_hit"] is False
    assert first["metadata"]["weather"]["resolved_name"] != second["metadata"]["weather"]["resolved_name"]
    assert len(calls) == 2
