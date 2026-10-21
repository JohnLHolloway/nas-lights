import math

import pytest

import nas_lights as nl


# ---- _lerp
@pytest.mark.parametrize("t, expected", [(0, "000000"), (1, "FF8040"), (0.5, "804020")])
def test_lerp_endpoints_and_midpoint(t, expected):
    assert nl._lerp("000000", "FF8040", t) == expected


def test_lerp_runs_backwards_and_is_uppercase():
    assert nl._lerp("ff0000", "0000ff", 0.25) == "BF0040"


# ---- scale_color
def test_scale_color_hits_stops_exactly():
    for value, color in nl.DEFAULT_CONFIG["scales"]["drive_temp"]:
        assert nl.scale_color("drive_temp", value) == color


@pytest.mark.parametrize("value, expected", [(-10, "0050FF"), (0, "0050FF"), (55, "FF0000"), (80, "FF0000")])
def test_scale_color_clamps_outside_the_scale(value, expected):
    assert nl.scale_color("drive_temp", value) == expected


def test_scale_color_blends_between_stops():
    # 39 °C is halfway between the 36 (00C8FF) and 42 (00E070) stops.
    assert nl.scale_color("drive_temp", 39) == nl._lerp("00C8FF", "00E070", 0.5) == "00D4B8"


def test_scale_color_quantizes_to_steps():
    # With one step between stops, halfway rounds up to the next stop.
    assert nl.scale_color("drive_temp", 39, steps=1) == "00E070"
    # With 4 steps, 37 °C (1/6 of the way) lands on the 1/4 step.
    assert nl.scale_color("drive_temp", 37, steps=4) == nl._lerp("00C8FF", "00E070", 0.25)


def test_scale_color_uses_configured_smoothness():
    nl.STATE["config"]["smoothness"] = 1
    assert nl.scale_color("drive_temp", 39) == "00E070"


def test_scale_color_sorts_unsorted_stops():
    nl.STATE["config"]["scales"]["cpu_temp"] = [[90, "FF0000"], [35, "0000FF"]]
    assert nl.scale_color("cpu_temp", 20) == "0000FF"
    assert nl.scale_color("cpu_temp", 95) == "FF0000"


# ---- network scale is logarithmic
@pytest.mark.parametrize("mbps, pos", [
    (0, 0.0),                    # log of 0 is clamped, so idle is the first stop
    (1, 0.0),
    (math.sqrt(10), 0.5),        # halfway between 1 and 10 on a log axis
    (10, 1.0),
    (50, 2.0),
    (100, 2.5),                  # halfway between 50 and 200 on a log axis
    (900, 5.0),
    (5000, 5.0),
])
def test_network_positions_are_log_scale(mbps, pos):
    got, _ = nl._scale_pos("network", mbps)
    assert got == pytest.approx(pos)


def test_temperature_positions_are_linear():
    got, _ = nl._scale_pos("drive_temp", 37.5)       # a quarter of the way from 36 to 42
    assert got == pytest.approx(1.25)


# ---- status_color hysteresis
def drive_pos(pos):
    """Drive temperature at a scale position between the 36 and 42 °C stops."""
    return 36 + (pos - 1) * 6


def test_status_color_matches_scale_color_on_first_reading():
    assert nl.status_color("drive_temp", 39) == nl.scale_color("drive_temp", 39)


def test_status_color_holds_near_a_step_boundary():
    held = nl.status_color("drive_temp", drive_pos(1.0))
    assert held == "00C8FF"
    # 1.15 rounds to the 1.25 step, but is less than 3/4 step (0.1875) from 1.0.
    assert nl.scale_color("drive_temp", drive_pos(1.15)) != held
    assert nl.status_color("drive_temp", drive_pos(1.15)) == held


def test_status_color_moves_after_three_quarters_of_a_step():
    nl.status_color("drive_temp", drive_pos(1.0))
    moved = nl.status_color("drive_temp", drive_pos(1.2))
    assert moved == nl._lerp("00C8FF", "00E070", 0.25)
    # And now it holds the new step when the reading drifts back a little.
    assert nl.scale_color("drive_temp", drive_pos(1.1)) == "00C8FF"
    assert nl.status_color("drive_temp", drive_pos(1.1)) == moved


def test_status_color_jumps_straight_to_a_far_reading():
    nl.status_color("drive_temp", 30)
    assert nl.status_color("drive_temp", 60) == "FF0000"


def test_status_color_hysteresis_is_per_mode():
    nl.status_color("drive_temp", drive_pos(1.0))
    assert nl.status_color("cpu_temp", 50) == nl.scale_color("cpu_temp", 50)


# ---- software brightness
@pytest.mark.parametrize("bri, expected", [(100, "FF8040"), (50, "804020"), (0, "000000"), (150, "FF8040")])
def test_dim(bri, expected):
    assert nl.dim("FF8040", bri) == expected


def test_dim_passes_through_missing_values():
    assert nl.dim(None, 50) is None
    assert nl.dim("FF8040", None) == "FF8040"
