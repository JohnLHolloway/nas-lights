#!/usr/bin/env python3
"""NAS Lights: a web UI and status-light engine for OpenRGB.

Talks to an OpenRGB SDK server over the network (no OpenRGB install needed in
this container) and maps live NAS readings to light colors:

  * hottest drive temperature   (Linux drivetemp hwmon)
  * CPU temperature             (k10temp / coretemp / zenpower hwmon)
  * network throughput          (host NIC counters)
  * ZFS pool health             (/proc/spl/kstat/zfs/*/state)

Any ZFS pool that is not ONLINE overrides status colors with an alert style.
The last manual setting is stored in NAS Lights' own settings file and is
re-applied on startup and whenever the OpenRGB server comes back after a restart.
(OpenRGB servers ignore profile-save requests from network clients.)

Configuration is by environment variable; see README.md.
Standard library only.
"""
import copy
import glob
import json
import math
import os
import re
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sdk

__version__ = "0.2.0"


# ---------------------------------------------------------------- configuration
def env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


BIND = env("NASL_BIND", "0.0.0.0")
PORT = int(env("NASL_PORT", "6780"))
OPENRGB_HOST = env("OPENRGB_HOST", "127.0.0.1")
OPENRGB_PORT = int(env("OPENRGB_PORT", "6742"))
OPENRGB_DEVICE = env("OPENRGB_DEVICE", "0")          # index, or part of the device name
DATA_DIR = env("NASL_DATA", "/data")
HOST_SYS = env("HOST_SYS", "/host/sys" if os.path.isdir("/host/sys/class") else "/sys")
ZFS_KSTAT = env("ZFS_KSTAT", "/proc/spl/kstat/zfs")
NET_IFACE = env("NET_IFACE", "")                     # empty = auto-detect
CPU_SENSOR = env("CPU_SENSOR", "")                   # empty = auto-detect
DRIVE_SENSOR = env("DRIVE_SENSOR", "drivetemp")
STATE_FILE = os.path.join(DATA_DIR, "settings.json")

LOCK = threading.RLock()
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

STATUS_MODES = {
    "none": "Manual (no status)",
    "drive_temp": "Hottest drive temperature",
    "cpu_temp": "CPU temperature",
    "network": "Network activity",
    "health": "Health only",
}
UNITS = {"drive_temp": "°C", "cpu_temp": "°C", "network": "Mbps"}
LIMITS = {"drive_temp": (0, 80), "cpu_temp": (0, 110), "network": (0, 100000)}

DEFAULT_CONFIG = {
    "scales": {
        "drive_temp": [[30, "0050FF"], [36, "00C8FF"], [42, "00E070"], [46, "FFD400"], [50, "FF7A00"], [55, "FF0000"]],
        "cpu_temp": [[35, "0050FF"], [45, "00C8FF"], [60, "00E070"], [72, "FFD400"], [80, "FF7A00"], [90, "FF0000"]],
        "network": [[1, "0050FF"], [10, "00C8FF"], [50, "00E070"], [200, "FFD400"], [500, "FF7A00"], [900, "FF0000"]],
    },
    "health_color": "00C853",
    "alert": {"color": "FF0000", "effect": "Breathing"},
    "brightness": 100,
    "smoothness": 4,               # color steps between two scale points
    "interval": 5,                 # seconds between readings
    "fade": 1.5,                   # seconds to blend between status colors (0 = instant)
    "response": "normal",          # reading smoothing: fast, normal or slow
    "quiet": {"enabled": False, "start": "23:00", "end": "07:00", "action": "off", "dim": 15},
}


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("color", "FFFFFF")
    st.setdefault("brightness", 100)
    st.setdefault("speed", 50)
    st.setdefault("status_mode", "none")
    st.setdefault("manual", None)             # last manual effect, re-applied after restarts
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in (st.get("config") or {}).items():
        if k in cfg:
            cfg[k] = v
    st["config"] = cfg
    return st


STATE = load_state()
LIVE = {"reading": None, "unit": "", "applied": None, "pools": {}, "alert": False, "quiet": False,
        "preview_until": 0, "readings": {}, "net_iface": None, "cpu_sensor": None, "error": None,
        "writes": {"mode": 0, "colors": 0, "fades": 0}}   # write counters, for diagnostics


def save_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        LIVE["error"] = f"could not save settings: {e}"


# ---------------------------------------------------------------- OpenRGB
def connect():
    return sdk.SDK(OPENRGB_HOST, OPENRGB_PORT, name="NAS Lights")


def device_index(c):
    if OPENRGB_DEVICE.isdigit():
        return int(OPENRGB_DEVICE)
    want = OPENRGB_DEVICE.lower()
    for i in range(c.count()):
        if want in c.controller(i)["name"].lower():
            return i
    raise LookupError(f"no OpenRGB device matches '{OPENRGB_DEVICE}'")


def controller_info():
    """Device name, effect list with capabilities, and the live effect."""
    with connect() as c:
        idx = device_index(c)
        ctl = c.controller(idx)
    modes = ctl["modes"]
    cur = modes[ctl["active_mode"]] if 0 <= ctl["active_mode"] < len(modes) else None
    speed_pct = None
    if cur and (cur["flags"] & sdk.FLAG_SPEED) and cur["speed_min"] != cur["speed_max"]:
        speed_pct = round((cur["speed"] - cur["speed_min"]) * 100 / (cur["speed_max"] - cur["speed_min"]))
    return {
        "device": ctl["name"],
        "modes": [m["name"] for m in modes],
        "current": cur["name"] if cur else None,
        "effects": {"caps": {m["name"]: sdk.capabilities(m) for m in modes}, "current": cur["name"] if cur else None,
                    "speed_pct": speed_pct},
    }


def dim(color, brightness):
    """Software brightness: scale the color (for effects without hardware brightness)."""
    if not color or brightness is None:
        return color
    f = max(0, min(100, int(brightness))) / 100.0
    return "".join(f"{round(int(color[k:k + 2], 16) * f):02X}" for k in (0, 2, 4))


def _scale(pct, lo, hi):
    return round(lo + (hi - lo) * max(0, min(100, pct)) / 100.0)


FADE_FPS = 15
MODE_FIELDS = ("speed", "brightness", "direction", "color_mode", "colors")


def _lerp(a, b, t):
    ca, cb = _rgb(a), _rgb(b)
    return "".join(f"{round(ca[k] + (cb[k] - ca[k]) * t):02X}" for k in range(3))


def apply(mode, color=None, brightness=None, speed=None, direction=None, fade=0.0):
    """Switch the device to an effect. Unsupported options are ignored.

    The effect itself is only re-sent when something about it changes. Some
    controllers (ASRock Polychrome, for one) blank the LEDs for a moment on every
    mode write, which shows up as a flash on each color update. When the effect
    is unchanged and `fade` > 0, the color is blended over `fade` seconds.
    """
    with connect() as c:
        idx = device_index(c)
        ctl = c.controller(idx)
        byname = {m["name"].lower(): m for m in ctl["modes"]}
        m = byname.get(str(mode).lower())
        if m is None:
            raise ValueError(f"effect '{mode}' is not available on {ctl['name']}")
        caps = sdk.capabilities(m)
        m = copy.deepcopy(m)
        if speed is not None and caps["speed"]:
            m["speed"] = _scale(int(speed), m["speed_min"], m["speed_max"])
        hw_bri = caps["brightness"] and brightness is not None
        if hw_bri:
            m["brightness"] = _scale(int(brightness), m["brightness_min"], m["brightness_max"])
        shown = color if hw_bri else dim(color, brightness)
        if direction in sdk.DIRECTION_VALUES and direction in caps["directions"]:
            m["direction"] = sdk.DIRECTION_VALUES[direction]
        if color and caps["mode_colors"]:
            m["color_mode"] = sdk.COLOR_MODE_SPECIFIC
            m["colors"] = [shown] * max(1, m["colors_min"])
        elif color and caps["per_led"]:
            m["color_mode"] = sdk.COLOR_MODE_PER_LED

        active = ctl["modes"][ctl["active_mode"]] if 0 <= ctl["active_mode"] < len(ctl["modes"]) else None
        same_mode = (active is not None and active["index"] == m["index"]
                     and all(active[f] == m[f] for f in MODE_FIELDS))
        if not same_mode:
            c.update_mode(idx, m["index"], m)
            LIVE["writes"]["mode"] += 1

        if color and caps["per_led"] and not caps["mode_colors"] and ctl["leds"]:
            n = len(ctl["leds"])
            start = ctl["colors"][0] if (same_mode and ctl["colors"]) else None
            if start and fade and fade > 0 and start != shown:
                frames = max(2, int(fade * FADE_FPS))
                for f in range(1, frames + 1):
                    c.update_leds(idx, [_lerp(start, shown, f / frames)] * n)
                    time.sleep(fade / frames)
                LIVE["writes"]["fades"] += 1
            else:
                if not same_mode:
                    time.sleep(0.05)
                c.update_leds(idx, [shown] * n)
                LIVE["writes"]["colors"] += 1
        time.sleep(0.1)                          # let the server finish before the socket closes


def restore_manual():
    """Re-apply the last manual setting (or leave the lights alone if there is none)."""
    m = STATE.get("manual")
    if m:
        apply(m["mode"], m.get("color"), m.get("brightness"), m.get("speed"), m.get("direction"))


def color_effect(preferred):
    """Pick a usable color effect name: the preferred one, else Static/Direct."""
    try:
        caps = controller_info()["effects"]["caps"]
    except Exception:
        return preferred
    for name in (preferred, "Static", "Direct"):
        for k, v in caps.items():
            if k.lower() == name.lower() and (v["per_led"] or v["mode_colors"]):
                return k
    return preferred


# ---------------------------------------------------------------- readings
def _read(path):
    with open(path) as f:
        return f.read().strip()


def _hwmon_dirs():
    return glob.glob(os.path.join(HOST_SYS, "class/hwmon/hwmon*"))


def hwmon_temps(names, label_filter=None):
    vals = []
    for h in _hwmon_dirs():
        try:
            if _read(h + "/name") not in names:
                continue
            for t in glob.glob(h + "/temp*_input"):
                lp = t.replace("_input", "_label")
                label = _read(lp) if os.path.exists(lp) else ""
                if label_filter and label and not label_filter(label):
                    continue
                vals.append(int(_read(t)) / 1000.0)
        except (OSError, ValueError):
            continue
    return vals


CPU_CANDIDATES = [
    ("k10temp", lambda l: l == "Tctl"),
    ("zenpower", lambda l: l in ("Tdie", "Tctl")),
    ("coretemp", lambda l: l.startswith("Package")),
    ("cpu_thermal", None),
]


def cpu_temp():
    if CPU_SENSOR:
        v = hwmon_temps({CPU_SENSOR})
        LIVE["cpu_sensor"] = CPU_SENSOR if v else None
        return max(v) if v else None
    for name, flt in CPU_CANDIDATES:
        v = hwmon_temps({name}, flt)
        if v:
            LIVE["cpu_sensor"] = name
            return max(v)
    return None


def drive_temp():
    v = hwmon_temps(set(DRIVE_SENSOR.split(",")))
    return max(v) if v else None


SKIP_IFACES = ("lo", "docker", "veth", "br-", "virbr", "tailscale", "kube", "cni", "flannel", "wg", "tun", "tap", "vnet", "zt")


def pick_iface():
    if NET_IFACE:
        return NET_IFACE
    best, best_bytes = None, -1
    for d in glob.glob(os.path.join(HOST_SYS, "class/net/*")):
        name = os.path.basename(d)
        if name.startswith(SKIP_IFACES):
            continue
        try:
            if _read(d + "/operstate") != "up":
                continue
            b = int(_read(d + "/statistics/rx_bytes"))
        except (OSError, ValueError):
            continue
        if b > best_bytes:
            best, best_bytes = name, b
    return best


_net = {"prev": None, "iface": None, "checked": 0}


def net_mbps():
    """Host throughput (rx+tx) since the previous call, in Mbit/s."""
    if not _net["iface"] or time.time() - _net["checked"] > 600:
        _net["iface"], _net["checked"] = pick_iface(), time.time()
    iface = _net["iface"]
    LIVE["net_iface"] = iface
    if not iface:
        return None
    base = os.path.join(HOST_SYS, "class/net", iface, "statistics")
    try:
        total = int(_read(base + "/rx_bytes")) + int(_read(base + "/tx_bytes"))
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    rate = None
    if _net["prev"] and _net["prev"][2] == iface:
        dt = now - _net["prev"][0]
        if dt > 0:
            rate = max(0.0, (total - _net["prev"][1]) * 8 / dt / 1e6)
    _net["prev"] = (now, total, iface)
    return rate


def pool_states():
    out = {}
    for p in glob.glob(os.path.join(ZFS_KSTAT, "*/state")):
        try:
            out[p.split("/")[-2]] = _read(p)
        except OSError:
            pass
    return out


RESPONSE_TAU = {"fast": 0.0, "normal": 15.0, "slow": 45.0}   # smoothing time constant, seconds
_ema = {}


def smooth(key, value):
    """Exponential moving average with a time constant, so readings drift instead of jump."""
    if value is None:
        return None
    tau = RESPONSE_TAU.get(STATE["config"].get("response", "normal"), 15.0)
    now = time.monotonic()
    prev = _ema.get(key)
    if prev is None or tau <= 0:
        v = value
    else:
        a = 1.0 - math.exp(-(now - prev[1]) / tau)
        v = prev[0] + a * (value - prev[0])
    _ema[key] = (v, now)
    return v


def readings():
    raw = {"drive_temp": drive_temp(), "cpu_temp": cpu_temp(), "network": net_mbps()}
    out = {}
    for k, v in raw.items():
        sv = smooth(k, v)
        out[k] = round(sv, 1) if sv is not None else None
    LIVE["raw"] = {k: (round(v, 1) if v is not None else None) for k, v in raw.items()}
    return out


# ---------------------------------------------------------------- color math
def _rgb(h):
    return [int(h[k:k + 2], 16) for k in (0, 2, 4)]


def _scale_pos(mode, value):
    """Where a value sits on a scale, as stop index + fraction (0 .. len(stops)-1)."""
    stops = sorted(STATE["config"]["scales"][mode], key=lambda s: s[0])
    f = (lambda v: math.log10(max(float(v), 0.1))) if mode == "network" else float
    x = f(value)
    pts = [f(s[0]) for s in stops]
    if len(pts) == 1 or x <= pts[0]:
        return 0.0, stops
    if x >= pts[-1]:
        return float(len(pts) - 1), stops
    i = max(k for k in range(len(pts) - 1) if pts[k] <= x)
    span = pts[i + 1] - pts[i]
    return i + ((x - pts[i]) / span if span else 0.0), stops


def _pos_color(pos, stops):
    cols = [s[1].upper() for s in stops]
    if pos <= 0:
        return cols[0]
    if pos >= len(cols) - 1:
        return cols[-1]
    i = int(pos)
    return _lerp(cols[i], cols[i + 1], pos - i)


def scale_color(mode, value, steps=None):
    """Color for a value, quantized to the configured number of steps between points."""
    steps = steps or int(STATE["config"].get("smoothness", 4))
    pos, stops = _scale_pos(mode, value)
    return _pos_color(round(pos * steps) / steps, stops)


_held = {}


def status_color(mode, value):
    """Like scale_color, with hysteresis: a reading must move 3/4 of a step away
    from the current step before the color changes, so noise near a boundary
    does not make the light bounce between two colors."""
    steps = int(STATE["config"].get("smoothness", 4))
    pos, stops = _scale_pos(mode, value)
    q = round(pos * steps) / steps
    held = _held.get(mode)
    if held is not None and abs(pos - held) < 0.75 / steps:
        q = held
    _held[mode] = q
    return _pos_color(q, stops)


def in_quiet_hours():
    q = STATE["config"]["quiet"]
    if not q.get("enabled"):
        return False
    now = time.localtime()
    m = now.tm_hour * 60 + now.tm_min
    s = int(q["start"][:2]) * 60 + int(q["start"][3:])
    e = int(q["end"][:2]) * 60 + int(q["end"][3:])
    return (s <= m < e) if s < e else (m >= s or m < e)


# ---------------------------------------------------------------- engine
class ServerWatch:
    """Holds one idle SDK connection so an OpenRGB restart is noticed immediately.

    `changed()` returns True once after (re)connecting to a server that has
    finished detecting devices, i.e. when the lights need to be re-applied.
    """

    def __init__(self):
        self.conn = None
        self.pending = True

    def _drop(self):
        if self.conn:
            self.conn.close()
        self.conn = None
        self.pending = True

    def changed(self):
        if self.conn is not None:
            try:
                ready, _, _ = select.select([self.conn.s], [], [], 0)
                if ready and not self.conn.s.recv(65536):     # closed: the server went away
                    self._drop()
            except OSError:
                self._drop()
        if self.conn is None:
            try:
                self.conn = connect()
            except OSError:
                self.conn = None
                raise
        if self.pending:
            with connect() as c:
                if c.count() == 0:                          # still detecting devices; try next round
                    return False
            self.pending = False
            return True
        return False


def status_loop():
    watch = ServerWatch()
    while True:
        cfg = STATE["config"]
        try:
            mode = STATE.get("status_mode", "none")
            if watch.changed():                             # startup, or OpenRGB came back
                time.sleep(1.0)
                with LOCK:
                    LIVE["applied"] = None
                    if mode == "none" and not LIVE["preview_until"]:
                        restore_manual()

            pools = pool_states()
            vals = readings()
            alert = any(s != "ONLINE" for s in pools.values())
            quiet = in_quiet_hours()
            LIVE.update(pools=pools, alert=alert, quiet=quiet, readings=vals,
                        reading=vals.get(mode), unit=UNITS.get(mode, ""))

            if mode == "none" and LIVE["preview_until"] and time.time() >= LIVE["preview_until"]:
                with LOCK:                                  # preview over: restore the manual setting
                    if STATE.get("status_mode") == "none":
                        restore_manual()
                    LIVE["preview_until"] = 0

            if mode != "none" and time.time() >= LIVE["preview_until"]:
                bri = int(cfg.get("brightness", 100))
                if alert:                                   # problems always show, even in quiet hours
                    target = [cfg["alert"]["effect"], cfg["alert"]["color"], bri]
                elif quiet and cfg["quiet"]["action"] == "off":
                    target = ["Off", None, None]
                else:
                    if quiet:
                        bri = int(cfg["quiet"].get("dim", 15))
                    if mode == "health":
                        target = ["Static", cfg["health_color"], bri]
                    elif LIVE["reading"] is not None:
                        target = ["Static", status_color(mode, LIVE["reading"]), bri]
                    else:
                        target = None
                if target and target != LIVE["applied"]:
                    with LOCK:
                        if STATE.get("status_mode") == mode:
                            eff = target[0] if target[0] == "Off" else color_effect(target[0])
                            apply(eff, target[1], target[2], fade=float(cfg.get("fade", 1.5)))
                            LIVE["applied"] = target
            LIVE["error"] = None
        except Exception as e:                              # keep running; show the problem in the UI
            LIVE["error"] = str(e)[:200]
            LIVE["applied"] = None
        time.sleep(max(2, int(cfg.get("interval", 5))))


# ---------------------------------------------------------------- validation
def clean_config(c):
    out = copy.deepcopy(STATE["config"])
    if "scales" in c:
        for mode, stops in c["scales"].items():
            if mode not in DEFAULT_CONFIG["scales"]:
                raise ValueError("unknown scale " + str(mode))
            if not isinstance(stops, list) or not 2 <= len(stops) <= 10:
                raise ValueError("each scale needs 2 to 10 points")
            lo, hi = LIMITS[mode]
            cleaned = []
            for v, col in stops:
                v = float(v)
                if not lo <= v <= hi:
                    raise ValueError(f"{STATUS_MODES[mode]}: {v:g} is outside {lo}-{hi}")
                if not HEX.match(str(col)):
                    raise ValueError("bad color " + str(col))
                cleaned.append([round(v, 1), str(col).upper()])
            cleaned.sort(key=lambda s: s[0])
            if len({s[0] for s in cleaned}) != len(cleaned):
                raise ValueError(f"{STATUS_MODES[mode]}: two points share the same value")
            out["scales"][mode] = cleaned
    if "health_color" in c:
        if not HEX.match(str(c["health_color"])):
            raise ValueError("bad health color")
        out["health_color"] = str(c["health_color"]).upper()
    if "alert" in c:
        a = c["alert"]
        if not HEX.match(str(a.get("color", ""))) or not str(a.get("effect", "")).strip():
            raise ValueError("bad alert style")
        out["alert"] = {"color": a["color"].upper(), "effect": str(a["effect"])[:40]}
    if "brightness" in c:
        out["brightness"] = max(0, min(100, int(c["brightness"])))
    if "smoothness" in c:
        out["smoothness"] = max(1, min(16, int(c["smoothness"])))
    if "interval" in c:
        out["interval"] = max(2, min(60, int(c["interval"])))
    if "fade" in c:
        out["fade"] = round(max(0.0, min(5.0, float(c["fade"]))), 1)
    if "response" in c:
        if c["response"] not in RESPONSE_TAU:
            raise ValueError("response must be fast, normal or slow")
        out["response"] = c["response"]
    if "quiet" in c:
        q = c["quiet"]
        if not HHMM.match(str(q.get("start", ""))) or not HHMM.match(str(q.get("end", ""))):
            raise ValueError("quiet hours need HH:MM times")
        if q.get("action") not in ("off", "dim"):
            raise ValueError("quiet action must be off or dim")
        out["quiet"] = {"enabled": bool(q.get("enabled")), "start": q["start"], "end": q["end"],
                        "action": q["action"], "dim": max(0, min(100, int(q.get("dim", 15))))}
    return out


# ---------------------------------------------------------------- web
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "index.html")


def full_state():
    try:
        info = controller_info()
        info["openrgb_error"] = None
    except Exception as e:
        info = {"device": None, "modes": [], "current": None,
                "effects": {"caps": {}, "current": None, "speed_pct": None},
                "openrgb_error": f"Cannot reach OpenRGB at {OPENRGB_HOST}:{OPENRGB_PORT} ({e})"}
    alert_effects = [k for k, v in info["effects"]["caps"].items() if (v["per_led"] or v["mode_colors"]) and k != "Direct"]
    return {**info, **STATE, "status_modes": STATUS_MODES, "units": UNITS, "limits": LIMITS,
            "alert_effects": alert_effects or ["Static"], "defaults": DEFAULT_CONFIG, "live": LIVE,
            "version": __version__}


class Handler(BaseHTTPRequestHandler):
    server_version = "NASLights/" + __version__

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json_out(self, code, obj):
        return self._send(code, json.dumps(obj))

    def _err(self, code, msg):
        return self._json_out(code, {"error": msg})

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(min(n, 16384)) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(PAGE_FILE, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._err(500, "index.html missing")
        if path == "/api/state":
            with LOCK:
                return self._json_out(200, full_state())
        if path == "/api/live":
            return self._json_out(200, {"status_mode": STATE["status_mode"], "live": LIVE})
        if path == "/healthz":
            return self._json_out(200, {"ok": True, "version": __version__})
        return self._err(404, "not found")

    def do_POST(self):
        # Simple CSRF guard: browsers send this header only for same-origin fetches with JSON.
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self._err(415, "send JSON")
        try:
            req = self._body()
        except ValueError:
            return self._err(400, "bad JSON")
        path = self.path.split("?")[0]
        try:
            if path == "/api/status":
                return self._set_status(req)
            if path == "/api/config":
                return self._set_config(req)
            if path == "/api/preview":
                return self._preview(req)
            if path == "/api/set":
                return self._set_manual(req)
        except (ConnectionError, OSError) as e:
            return self._err(502, f"OpenRGB unreachable: {e}")
        except (ValueError, LookupError) as e:
            return self._err(400, str(e))
        return self._err(404, "not found")

    def _set_status(self, req):
        k = str(req.get("status_mode", ""))
        if k not in STATUS_MODES:
            return self._err(400, "unknown status mode")
        with LOCK:
            STATE["status_mode"] = k
            LIVE.update(applied=None, preview_until=0)
            save_state()
            if k == "none":                               # back to the last manual setting
                restore_manual()
            return self._json_out(200, full_state())

    def _set_config(self, req):
        try:
            new = clean_config(req)
        except (TypeError, KeyError) as e:
            return self._err(400, f"bad settings: {e}")
        with LOCK:
            STATE["config"] = new
            LIVE["applied"] = None
            save_state()
            return self._json_out(200, full_state())

    def _preview(self, req):
        mode = str(req.get("mode", ""))
        cfg = STATE["config"]
        if mode == "alert":
            target = (color_effect(cfg["alert"]["effect"]), cfg["alert"]["color"])
        elif mode == "health":
            target = (color_effect("Static"), cfg["health_color"])
        elif mode in DEFAULT_CONFIG["scales"]:
            target = (color_effect("Static"), scale_color(mode, float(req.get("value"))))
        else:
            return self._err(400, "unknown preview")
        with LOCK:
            apply(target[0], target[1], int(cfg.get("brightness", 100)))
            LIVE["applied"] = None
            LIVE["preview_until"] = time.time() + 15
        return self._json_out(200, {"previewing": target, "seconds": 15})

    def _set_manual(self, req):
        mode = str(req.get("mode", "")).strip()
        color = str(req.get("color", "")).strip().lstrip("#")
        bri, spd, direction = req.get("brightness"), req.get("speed"), req.get("direction")
        if color and not HEX.match(color):
            return self._err(400, "bad color")
        bri = None if bri is None else max(0, min(100, int(bri)))
        spd = None if spd is None else max(0, min(100, int(spd)))
        with LOCK:
            STATE["status_mode"] = "none"                 # manual control takes over
            LIVE.update(applied=None, preview_until=0)
            apply(mode, color or None, bri, spd, direction)
            STATE["manual"] = {"mode": mode, "color": (color or STATE.get("color") or "FFFFFF").upper(),
                               "brightness": bri if bri is not None else STATE.get("brightness", 100),
                               "speed": spd if spd is not None else STATE.get("speed", 50),
                               "direction": direction}
            if color:
                STATE["color"] = color.upper()
            if bri is not None:
                STATE["brightness"] = bri
            if spd is not None:
                STATE["speed"] = spd
            save_state()
            return self._json_out(200, full_state())

    def log_message(self, fmt, *args):
        pass


def main():
    print(f"NAS Lights {__version__} on http://{BIND}:{PORT} -> OpenRGB {OPENRGB_HOST}:{OPENRGB_PORT}, "
          f"device {OPENRGB_DEVICE}, data {DATA_DIR}", flush=True)
    threading.Thread(target=status_loop, daemon=True, name="status").start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
