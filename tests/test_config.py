import pytest

import nas_lights as nl


def test_empty_request_keeps_current_config():
    assert nl.clean_config({}) == nl.DEFAULT_CONFIG


def test_does_not_modify_live_state():
    nl.clean_config({"brightness": 10, "scales": {"cpu_temp": [[40, "000000"], [80, "FFFFFF"]]}})
    assert nl.STATE["config"] == nl.DEFAULT_CONFIG


def test_scale_points_are_sorted_rounded_and_uppercased():
    out = nl.clean_config({"scales": {"drive_temp": [[50.04, "ff0000"], ["30", "0050ff"], [40.26, "00e070"]]}})
    assert out["scales"]["drive_temp"] == [[30.0, "0050FF"], [40.3, "00E070"], [50.0, "FF0000"]]
    # Other scales are left alone.
    assert out["scales"]["cpu_temp"] == nl.DEFAULT_CONFIG["scales"]["cpu_temp"]


def test_valid_full_config():
    req = {
        "health_color": "00aa00",
        "alert": {"color": "ff00ff", "effect": "Breathing"},
        "brightness": 80, "smoothness": 8, "interval": 10, "fade": 2.25, "response": "slow",
        "quiet": {"enabled": True, "start": "22:30", "end": "06:15", "action": "dim", "dim": 20},
    }
    out = nl.clean_config(req)
    assert out["health_color"] == "00AA00"
    assert out["alert"] == {"color": "FF00FF", "effect": "Breathing"}
    assert (out["brightness"], out["smoothness"], out["interval"], out["response"]) == (80, 8, 10, "slow")
    assert out["fade"] == pytest.approx(2.2)
    assert out["quiet"] == {"enabled": True, "start": "22:30", "end": "06:15", "action": "dim", "dim": 20}


@pytest.mark.parametrize("key, value, expected", [
    ("brightness", 150, 100), ("brightness", -5, 0),
    ("smoothness", 0, 1), ("smoothness", 99, 16),
    ("interval", 1, 2), ("interval", 3600, 60),
    ("fade", -1, 0.0), ("fade", 30, 5.0),
])
def test_numbers_are_clamped(key, value, expected):
    assert nl.clean_config({key: value})[key] == expected


def test_alert_effect_is_truncated():
    out = nl.clean_config({"alert": {"color": "FF0000", "effect": "X" * 100}})
    assert out["alert"]["effect"] == "X" * 40


def test_quiet_dim_is_clamped_and_enabled_is_bool():
    out = nl.clean_config({"quiet": {"enabled": 1, "start": "23:00", "end": "07:00", "action": "dim", "dim": 400}})
    assert out["quiet"]["enabled"] is True
    assert out["quiet"]["dim"] == 100


@pytest.mark.parametrize("req, message", [
    ({"scales": {"gpu_temp": [[1, "000000"], [2, "FFFFFF"]]}}, "unknown scale"),
    ({"scales": {"drive_temp": [[30, "000000"]]}}, "2 to 10 points"),
    ({"scales": {"drive_temp": [[i, "000000"] for i in range(11)]}}, "2 to 10 points"),
    ({"scales": {"drive_temp": "30:000000"}}, "2 to 10 points"),
    ({"scales": {"drive_temp": [[30, "000000"], [81, "FFFFFF"]]}}, "outside 0-80"),
    ({"scales": {"cpu_temp": [[-1, "000000"], [50, "FFFFFF"]]}}, "outside 0-110"),
    ({"scales": {"network": [[1, "000000"], [100001, "FFFFFF"]]}}, "outside 0-100000"),
    ({"scales": {"drive_temp": [[30, "#000000"], [40, "FFFFFF"]]}}, "bad color"),
    ({"scales": {"drive_temp": [[30, "00000G"], [40, "FFFFFF"]]}}, "bad color"),
    ({"scales": {"drive_temp": [[30, "000000"], [30.04, "FFFFFF"]]}}, "same value"),
    ({"health_color": "green"}, "bad health color"),
    ({"alert": {"color": "FF0000", "effect": "  "}}, "bad alert"),
    ({"alert": {"color": "red", "effect": "Static"}}, "bad alert"),
    ({"response": "instant"}, "fast, normal or slow"),
    ({"quiet": {"start": "24:00", "end": "07:00", "action": "off"}}, "HH:MM"),
    ({"quiet": {"start": "7:00", "end": "08:00", "action": "off"}}, "HH:MM"),
    ({"quiet": {"start": "23:00", "end": "07:60", "action": "off"}}, "HH:MM"),
    ({"quiet": {"start": "23:00", "end": "07:00", "action": "blink"}}, "off or dim"),
])
def test_invalid_config_is_rejected(req, message):
    with pytest.raises(ValueError, match=message):
        nl.clean_config(req)


def test_non_numeric_values_are_rejected():
    with pytest.raises(ValueError):
        nl.clean_config({"brightness": "bright"})
    with pytest.raises(ValueError):
        nl.clean_config({"scales": {"drive_temp": [["hot", "FF0000"], [40, "FFFFFF"]]}})
