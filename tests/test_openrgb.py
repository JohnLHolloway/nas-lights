"""sdk.SDK and nas_lights.apply() against a fake OpenRGB server."""
import pytest

import nas_lights as nl
import sdk
from fake_openrgb import FakeOpenRGB, asrock_like


# ---- sdk.SDK
def test_handshake_sends_client_name_and_negotiates_protocol():
    with FakeOpenRGB(proto=4) as srv:
        with sdk.SDK("127.0.0.1", srv.port, name="pytest") as c:
            assert c.proto == sdk.CLIENT_MAX_PROTOCOL == 3
        srv.wait_idle()
        assert srv.client_names == ["pytest"]


def test_count_and_controller_parsing():
    with FakeOpenRGB(controllers=[asrock_like(), asrock_like()]) as srv, sdk.SDK("127.0.0.1", srv.port) as c:
        assert c.count() == 2
        ctl = c.controller(0)
    assert ctl["name"] == "ASRock B650M Pro RS"
    assert ctl["vendor"] == "ASRock"
    assert ctl["description"] == "ASRock Polychrome USB Device"
    assert ctl["location"] == "HID: /dev/hidraw0"
    assert ctl["active_mode"] == 1
    assert [m["name"] for m in ctl["modes"]] == ["Direct", "Static", "Breathing", "Spectrum Cycle", "Off"]
    assert [m["index"] for m in ctl["modes"]] == [0, 1, 2, 3, 4]
    breathing = ctl["modes"][2]
    assert (breathing["speed_min"], breathing["speed_max"], breathing["speed"]) == (255, 0, 128)
    assert (breathing["brightness_min"], breathing["brightness_max"], breathing["brightness"]) == (0, 255, 255)
    assert breathing["colors"] == ["FF0000"]
    assert ctl["modes"][1]["color_mode"] == sdk.COLOR_MODE_SPECIFIC
    # Zones, including one with a matrix map that must be skipped correctly.
    assert [(z["name"], z["leds_count"]) for z in ctl["zones"]] == [("RGB LED Header", 1), ("Matrix", 2)]
    assert ctl["leds"] == ["Header LED", "Matrix LED 1", "Matrix LED 2"]
    assert ctl["colors"] == ["000000"] * 3


def test_old_protocol_has_no_brightness_fields():
    with FakeOpenRGB(proto=2) as srv, sdk.SDK("127.0.0.1", srv.port) as c:
        assert c.proto == 2
        ctl = c.controller(0)
    breathing = ctl["modes"][2]
    assert breathing["brightness_max"] == 0
    assert breathing["speed"] == 128 and breathing["colors"] == ["FF0000"]
    assert ctl["leds"][-1] == "Matrix LED 2"


def test_color_byte_order_round_trips():
    ctl = asrock_like()
    ctl["colors"] = ["123456", "ABCDEF", "FF0000"]
    with FakeOpenRGB(controllers=[ctl]) as srv, sdk.SDK("127.0.0.1", srv.port) as c:
        assert c.controller(0)["colors"] == ["123456", "ABCDEF", "FF0000"]
        c.update_leds(0, ["0000FF", "00FF00", "FF0000"])
    srv.wait_idle()
    assert srv.writes("leds") == [("leds", 0, ["0000FF", "00FF00", "FF0000"])]


def test_update_mode_round_trips():
    with FakeOpenRGB() as srv, sdk.SDK("127.0.0.1", srv.port) as c:
        m = c.controller(0)["modes"][2]
        m.update(speed=10, brightness=100, colors=["00FF00"])
        c.update_mode(0, 2, m)
    srv.wait_idle()
    (_, dev, written), = srv.writes("mode")
    assert dev == 0
    assert written["index"] == 2 and written["name"] == "Breathing"
    assert (written["speed"], written["brightness"], written["colors"]) == (10, 100, ["00FF00"])


def test_profiles():
    with FakeOpenRGB(profiles=["Default", "Night"]) as srv, sdk.SDK("127.0.0.1", srv.port) as c:
        assert c.profiles() == ["Default", "Night"]
        c.load_profile("Night")
    srv.wait_idle()
    assert srv.writes("load") == [("load", 0, "Night")]


def test_capabilities():
    ctl = asrock_like()
    caps = {m["name"]: sdk.capabilities(m) for m in ctl["modes"]}
    assert caps["Direct"]["per_led"] and not caps["Direct"]["mode_colors"]
    assert caps["Breathing"]["speed"] and caps["Breathing"]["brightness"]
    assert caps["Spectrum Cycle"]["directions"] == ["Left", "Right"]
    assert not any(caps["Off"][k] for k in ("speed", "brightness", "per_led", "mode_colors"))


# ---- nas_lights.apply(): effect writes
def test_mode_is_written_only_when_the_effect_changes(server):
    nl.apply("Static", "00FF00")
    server.wait_idle()
    assert len(server.writes("mode")) == 1
    assert server.controllers[0]["modes"][1]["colors"] == ["00FF00"]

    server.clear_log()
    nl.apply("Static", "00FF00")                     # identical: nothing to send
    server.wait_idle()
    assert server.writes("mode") == []
    assert nl.LIVE["writes"]["mode"] == 1

    nl.apply("Static", "0000FF")                     # the mode color is part of the effect
    server.wait_idle()
    assert len(server.writes("mode")) == 1

    server.clear_log()
    nl.apply("Breathing", "0000FF", brightness=100)  # a different effect
    server.wait_idle()
    (_, _, written), = server.writes("mode")
    assert written["index"] == 2
    assert server.controllers[0]["active_mode"] == 2


def test_effect_names_are_case_insensitive_and_unknown_ones_fail(server):
    nl.apply("spectrum cycle")
    server.wait_idle()
    assert server.controllers[0]["active_mode"] == 3
    with pytest.raises(ValueError, match="not available"):
        nl.apply("Disco")


def test_device_can_be_picked_by_name(server, monkeypatch):
    other = asrock_like()
    other["name"] = "Corsair Vengeance RGB"
    server.controllers.insert(0, other)
    monkeypatch.setattr(nl, "OPENRGB_DEVICE", "asrock")
    nl.apply("Off")
    server.wait_idle()
    assert [dev for _, dev, _ in server.writes("mode")] == [1]


# Status-color fades moved from apply() to the per-zone Renderer in 0.3; see
# RendererTest in test_nas_lights.py. Manual changes through apply() are instant.
def test_manual_apply_is_instant(server):
    nl.apply("Direct", "FF0000")
    nl.apply("Direct", "00FF00")
    server.wait_idle()
    assert [c[0] for _, _, c in server.writes("leds")] == ["FF0000", "00FF00"]
    assert len(server.writes("mode")) == 1


def test_software_brightness_dims_per_led_colors(server):
    nl.apply("Direct", "FF8040", brightness=50)
    server.wait_idle()
    assert server.writes("leds")[-1][2] == ["804020"] * 3


# ---- nas_lights.apply(): speed
@pytest.mark.parametrize("pct, raw", [(0, 255), (100, 0), (25, 191), (50, 128)])
def test_speed_range_is_reversed(server, pct, raw):
    """ASRock reports speed_min=255 (slowest) and speed_max=0 (fastest)."""
    nl.apply("Spectrum Cycle", speed=pct)
    server.wait_idle()
    (_, _, written), = server.writes("mode")
    assert written["speed"] == raw


def test_controller_info_reports_reversed_speed_as_percent(server):
    nl.apply("Spectrum Cycle", speed=100)
    server.wait_idle()
    info = nl.controller_info()
    assert info["current"] == "Spectrum Cycle"
    assert info["effects"]["speed_pct"] == 100


def test_hardware_brightness_and_direction(server):
    nl.apply("Breathing", "FFFFFF", brightness=50)
    nl.apply("Spectrum Cycle", direction="Right")
    nl.apply("Spectrum Cycle", direction="Up")       # not supported by this effect: ignored
    server.wait_idle()
    breathing, cycle = (w for _, _, w in server.writes("mode"))
    assert breathing["brightness"] == 128 and breathing["colors"] == ["FFFFFF"]
    assert cycle["direction"] == sdk.DIRECTION_VALUES["Right"]
