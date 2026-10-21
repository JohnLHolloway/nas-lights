import copy
import os
import tempfile

import pytest

# nas_lights reads its settings file at import time; keep it away from /data.
os.environ["NASL_DATA"] = tempfile.mkdtemp(prefix="nasl-test-")

import nas_lights  # noqa: E402
from fake_openrgb import FakeOpenRGB  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state():
    """Default config and empty smoothing/hysteresis memory for every test."""
    nas_lights.STATE["config"] = copy.deepcopy(nas_lights.DEFAULT_CONFIG)
    nas_lights._held.clear()
    nas_lights._ema.clear()
    nas_lights.LIVE["writes"] = {"mode": 0, "colors": 0, "fades": 0}
    yield


@pytest.fixture
def server(monkeypatch):
    """A fake OpenRGB server that nas_lights is pointed at. Sleeps in apply()
    are skipped; tests call server.wait_idle() before checking writes."""
    with FakeOpenRGB() as srv:
        monkeypatch.setattr(nas_lights, "OPENRGB_HOST", "127.0.0.1")
        monkeypatch.setattr(nas_lights, "OPENRGB_PORT", srv.port)
        monkeypatch.setattr(nas_lights, "OPENRGB_DEVICE", "0")
        monkeypatch.setattr(nas_lights.time, "sleep", lambda s: None)
        yield srv
