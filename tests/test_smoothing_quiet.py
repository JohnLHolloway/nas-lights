import math
import time
from types import SimpleNamespace

import pytest

import nas_lights as nl


@pytest.fixture
def clock(monkeypatch):
    """A controllable time.monotonic for nas_lights."""
    now = [1000.0]
    monkeypatch.setattr(nl.time, "monotonic", lambda: now[0])

    def advance(seconds):
        now[0] += seconds
    return advance


# ---- smooth()
def test_fast_response_follows_readings_immediately(clock):
    nl.STATE["config"]["response"] = "fast"
    assert nl.smooth("t", 10) == 10
    clock(5)
    assert nl.smooth("t", 50) == 50


def test_first_reading_is_taken_as_is(clock):
    assert nl.smooth("t", 42.0) == 42.0


@pytest.mark.parametrize("response, tau", [("normal", 15.0), ("slow", 45.0)])
def test_ema_uses_time_constant(clock, response, tau):
    nl.STATE["config"]["response"] = response
    nl.smooth("t", 10.0)
    clock(15)
    expected = 10 + (1 - math.exp(-15 / tau)) * 10
    assert nl.smooth("t", 20.0) == pytest.approx(expected)


def test_slow_lags_more_than_normal(clock):
    out = {}
    for response in ("normal", "slow"):
        nl._ema.clear()
        nl.STATE["config"]["response"] = response
        nl.smooth("t", 0.0)
        clock(5)
        out[response] = nl.smooth("t", 100.0)
    assert 0 < out["slow"] < out["normal"] < 100


def test_ema_converges_over_time(clock):
    nl.smooth("t", 0.0)
    for _ in range(40):
        clock(5)
        v = nl.smooth("t", 100.0)
    assert v == pytest.approx(100.0, abs=0.01)


def test_smoothing_is_per_key(clock):
    nl.smooth("a", 0.0)
    clock(5)
    assert nl.smooth("b", 70.0) == 70.0


def test_none_reading_passes_through_and_keeps_history(clock):
    nl.smooth("t", 10.0)
    clock(15)
    assert nl.smooth("t", None) is None
    assert nl._ema["t"][0] == 10.0


def test_unknown_response_falls_back_to_normal(clock):
    nl.STATE["config"]["response"] = "glacial"
    nl.smooth("t", 10.0)
    clock(15)
    assert nl.smooth("t", 20.0) == pytest.approx(10 + (1 - math.exp(-1)) * 10)


# ---- quiet hours
def set_quiet(monkeypatch, start, end, now, enabled=True):
    nl.STATE["config"]["quiet"] = {"enabled": enabled, "start": start, "end": end, "action": "off", "dim": 15}
    h, m = map(int, now.split(":"))
    monkeypatch.setattr(nl.time, "localtime", lambda *a: SimpleNamespace(tm_hour=h, tm_min=m))


@pytest.mark.parametrize("now, quiet", [
    ("22:59", False), ("23:00", True), ("23:59", True), ("00:00", True),
    ("03:30", True), ("06:59", True), ("07:00", False), ("12:00", False),
])
def test_quiet_window_crossing_midnight(monkeypatch, now, quiet):
    set_quiet(monkeypatch, "23:00", "07:00", now)
    assert nl.in_quiet_hours() is quiet


@pytest.mark.parametrize("now, quiet", [
    ("08:59", False), ("09:00", True), ("12:30", True), ("16:59", True), ("17:00", False), ("23:00", False),
])
def test_quiet_window_within_a_day(monkeypatch, now, quiet):
    set_quiet(monkeypatch, "09:00", "17:00", now)
    assert nl.in_quiet_hours() is quiet


def test_quiet_hours_disabled(monkeypatch):
    set_quiet(monkeypatch, "00:00", "23:59", "12:00", enabled=False)
    assert nl.in_quiet_hours() is False


def test_quiet_hours_use_local_time():
    nl.STATE["config"]["quiet"] = {"enabled": True, "start": "00:00", "end": "23:59", "action": "dim", "dim": 15}
    now = time.localtime()
    expected = not (now.tm_hour == 23 and now.tm_min == 59)
    assert nl.in_quiet_hours() is expected
