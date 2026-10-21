#!/usr/bin/env python3
"""NAS Lights: a web UI and status-light engine for OpenRGB.

Talks to an OpenRGB SDK server over the network (no OpenRGB install needed in
this container) and maps live NAS readings to light colors, one source per
OpenRGB zone:

  * hottest drive temperature   (Linux drivetemp hwmon)
  * CPU temperature             (k10temp / coretemp / zenpower hwmon)
  * network throughput          (host NIC counters)
  * disk throughput             (/proc/diskstats)
  * ZFS pool health             (/proc/spl/kstat/zfs/*/state)
  * a value pushed over HTTP    (Home Assistant, scripts)

A scrub or resilver (read from a `zpool status` file, see README.md) makes the
lights pulse slowly. Any ZFS pool that is not ONLINE overrides status colors
with an alert style.

The last manual setting is stored in NAS Lights' own settings file and is
re-applied on startup and whenever the OpenRGB server comes back after a restart.
(OpenRGB servers ignore profile-save requests from network clients.)

Configuration is by environment variable; see README.md.
Standard library only.
"""
import copy
import glob
import hmac
import json
import math
import os
import re
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sdk

__version__ = "0.3.0"


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
HOST_PROC = env("HOST_PROC", "/proc")
ZFS_KSTAT = env("ZFS_KSTAT", "/proc/spl/kstat/zfs")
ZPOOL_STATUS_FILE = env("ZPOOL_STATUS_FILE", "/host/zfs/zpool-status.txt")
NET_IFACE = env("NET_IFACE", "")                     # empty = auto-detect
CPU_SENSOR = env("CPU_SENSOR", "")                   # empty = auto-detect
DRIVE_SENSOR = env("DRIVE_SENSOR", "drivetemp")
DISK_DEVICES = env("DISK_DEVICES", "")               # empty = every physical disk
PUSH_TOKEN = env("NASL_PUSH_TOKEN", "")              # optional bearer token for /api/push
STATE_FILE = os.path.join(DATA_DIR, "settings.json")

LOCK = threading.RLock()
WAKE = threading.Event()                             # set to make the status loop run now
STOP = threading.Event()                             # set to end the background threads (tests)
HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

SOURCES = {
    "off": "Off",
    "drive_temp": "Hottest drive temperature",
    "cpu_temp": "CPU temperature",
    "network": "Network activity",
    "disk_io": "Disk throughput",
    "health": "Pool health",
    "push": "Pushed value",
}
SCALED = ("drive_temp", "cpu_temp", "network", "disk_io", "push")
LOG_SCALES = ("network", "disk_io")
UNITS = {"drive_temp": "°C", "cpu_temp": "°C", "network": "Mbps", "disk_io": "MB/s", "push": ""}
LIMITS = {"drive_temp": (0, 80), "cpu_temp": (0, 110), "network": (0, 100000), "disk_io": (0, 100000),
          "push": (-100000, 100000)}
SIMULATE_SECONDS = 15
PREVIEW_SECONDS = 15

DEFAULT_CONFIG = {
    "scales": {
        "drive_temp": [[30, "0050FF"], [36, "00C8FF"], [42, "00E070"], [46, "FFD400"], [50, "FF7A00"], [55, "FF0000"]],
        "cpu_temp": [[35, "0050FF"], [45, "00C8FF"], [60, "00E070"], [72, "FFD400"], [80, "FF7A00"], [90, "FF0000"]],
        "network": [[1, "0050FF"], [10, "00C8FF"], [50, "00E070"], [200, "FFD400"], [500, "FF7A00"], [900, "FF0000"]],
        "disk_io": [[1, "0050FF"], [10, "00C8FF"], [50, "00E070"], [150, "FFD400"], [400, "FF7A00"], [1000, "FF0000"]],
        "push": [[0, "0050FF"], [25, "00C8FF"], [50, "00E070"], [75, "FFD400"], [100, "FF0000"]],
    },
    "health_color": "00C853",
    "alert": {"color": "FF0000", "effect": "Breathing"},
    "scrub": {"color": "5B6CFF", "scope": "all", "period": 6},   # scope: all, health or off
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
    migrate(st)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    for k, v in (st.get("config") or {}).items():
        if k in cfg:
            cfg[k] = v
    for k, v in DEFAULT_CONFIG["scales"].items():  # scales added in later versions
        cfg["scales"].setdefault(k, copy.deepcopy(v))
    for k, v in DEFAULT_CONFIG["scrub"].items():
        cfg["scrub"].setdefault(k, v)
    st["config"] = cfg
    return st


def migrate(st):
    """0.2 drove the whole device from one `status_mode` reading. 0.3 maps a
    source to each zone: `zones` holds zone name -> source, with "*" for every
    zone not listed, and `status_mode` is just "none" (manual) or "zones"."""
    if isinstance(st.get("zones"), dict):
        if st["status_mode"] not in ("none", "zones"):
            st["status_mode"] = "zones"
        return
    legacy = st.get("status_mode", "none")
    st["zones"] = {"*": legacy if legacy in SOURCES and legacy != "off" else "drive_temp"}
    st["status_mode"] = "none" if legacy not in SOURCES else "zones"


STATE = load_state()
LIVE = {"readings": {}, "raw": {}, "pools": {}, "scans": {}, "zpool_file": None, "alert": False,
        "simulated": False, "simulate_until": 0, "quiet": False, "scrubbing": False, "preview_until": 0,
        "zones": [], "effect": None, "net_iface": None, "cpu_sensor": None, "disks": [], "error": None,
        "writes": {"mode": 0, "colors": 0, "fades": 0}}   # write counters, for diagnostics
PUSH = {}                                                  # zone name (lowercase) or "*" -> pushed value


def save_state():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        LIVE["error"] = f"could not save settings: {e}"


def zone_source(name):
    z = STATE["zones"]
    return z.get(name, z.get("*", "off"))


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


def device_zones(ctl):
    """The controller's zones. A device without zones is treated as one zone."""
    if ctl["zones"]:
        return ctl["zones"]
    return [{"index": 0, "name": ctl["name"], "leds_count": len(ctl["leds"]), "start": 0, "whole": True}]


def controller_info():
    """Device name, zones, effect list with capabilities, and the live effect."""
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
        "zones": [{"index": z["index"], "name": z["name"], "leds": z["leds_count"]} for z in device_zones(ctl)],
        "modes": [m["name"] for m in modes],
        "current": cur["name"] if cur else None,
        "effects": {"caps": {m["name"]: sdk.capabilities(m) for m in modes}, "current": cur["name"] if cur else None,
                    "speed_pct": speed_pct},
    }


def dim(color, brightness):
    """Software brightness: scale the color (for effects without hardware brightness)."""
    if not color or brightness is None:
        return color
    f = max(0.0, min(100.0, float(brightness))) / 100.0
    return "".join(f"{round(int(color[k:k + 2], 16) * f):02X}" for k in (0, 2, 4))


def _scale(pct, lo, hi):
    return round(lo + (hi - lo) * max(0, min(100, pct)) / 100.0)


FPS = 15
MODE_FIELDS = ("speed", "brightness", "direction", "color_mode", "colors")


def _lerp(a, b, t):
    ca, cb = _rgb(a), _rgb(b)
    return "".join(f"{round(ca[k] + (cb[k] - ca[k]) * t):02X}" for k in range(3))


def _same_mode(ctl, m):
    """True when the controller already runs mode `m` with the same settings."""
    i = ctl["active_mode"]
    if not 0 <= i < len(ctl["modes"]):
        return False
    active = ctl["modes"][i]
    return active["index"] == m["index"] and all(active[f] == m[f] for f in MODE_FIELDS)


def apply(mode, color=None, brightness=None, speed=None, direction=None):
    """Switch the whole device to an effect (manual control). Unsupported options are ignored.

    The effect itself is only re-sent when something about it changes. Some
    controllers (ASRock Polychrome, for one) blank the LEDs for a moment on every
    mode write, which shows up as a flash on each color update.
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

        same_mode = _same_mode(ctl, m)
        if not same_mode:
            c.update_mode(idx, m["index"], m)
            LIVE["writes"]["mode"] += 1
        if color and caps["per_led"] and not caps["mode_colors"] and ctl["leds"]:
            if not same_mode:
                time.sleep(0.05)
            c.update_leds(idx, [shown] * len(ctl["leds"]))
            LIVE["writes"]["colors"] += 1
        time.sleep(0.1)                          # let the server finish before the socket closes


def restore_manual():
    """Re-apply the last manual setting (or leave the lights alone if there is none)."""
    m = STATE.get("manual")
    if m:
        apply(m["mode"], m.get("color"), m.get("brightness"), m.get("speed"), m.get("direction"))


def pick_color_mode(modes, preferred):
    """A mode that can show a color: the preferred one, else Static, else Direct."""
    for name in (preferred, "Static", "Direct"):
        for m in modes:
            caps = sdk.capabilities(m)
            if m["name"].lower() == str(name).lower() and (caps["per_led"] or caps["mode_colors"]):
                return m
    return None


def _mul(color, f):
    return "".join(f"{round(int(color[k:k + 2], 16) * f):02X}" for k in (0, 2, 4))


def _ease(t):
    return t * t * (3 - 2 * t)


class Renderer:
    """Drives the lights while status lights, a preview or a simulated alert are showing.

    The status loop hands it a target every few seconds: an effect name, plus a
    color and a pulse flag per zone. The renderer keeps one SDK connection open
    and, about 15 times a second, works out each zone's color (fading from the
    old color, pulsing during a scrub) and sends UPDATEZONELEDS only for zones
    whose color actually changed. The effect is written only when it differs from
    what the controller reports, so a steady status never re-sends the mode.
    """

    REFRESH = 30          # seconds between re-reading the controller, to notice outside changes

    def __init__(self):
        self.lock = threading.Lock()
        self.target = None
        self.cur = {}         # zone index -> [from color or None, to color, start time, fade seconds]
        self.written = {}     # zone index -> color last sent
        self.conn = self.ctl = self.idx = None
        self.read_at = 0
        self.error = None

    @property
    def active(self):
        return self.target is not None

    def show(self, target):
        """target: {"effect": name, "colors": [hex or None per zone], "pulse": [bool], "fade": s, "period": s}.
        A color of None leaves that zone as it is (no reading yet)."""
        now = time.monotonic()
        with self.lock:
            changed = self.target is None or self.target["effect"] != target["effect"]
            for zi, col in enumerate(target["colors"]):
                if col is None:
                    continue
                prev = self.cur.get(zi)
                if prev and prev[1] == col:
                    continue
                start = self._base(zi, now) if prev else None     # None: fade from what the LEDs show
                self.cur[zi] = [start, col, now, 0.0 if changed else float(target.get("fade", 0))]
                if prev and self.cur[zi][3] > 0:
                    LIVE["writes"]["fades"] += 1
            self.target = target

    def release(self):
        """Stop driving the lights (manual control takes over)."""
        with self.lock:
            self.target = None
            self.cur, self.written = {}, {}
            self._close()

    def reconnect(self):
        """Re-read the controller and rewrite every zone (after an OpenRGB restart)."""
        with self.lock:
            self._close()

    def _close(self):
        if self.conn:
            self.conn.close()
        self.conn = self.ctl = None

    def _base(self, zi, now):
        fr, to, t0, dur = self.cur[zi]
        if fr is None or dur <= 0 or now - t0 >= dur:
            return to
        return _lerp(fr, to, _ease((now - t0) / dur))

    def _read(self):
        self.ctl = self.conn.controller(self.idx)
        self.read_at = time.monotonic()
        # Start from what the LEDs really show, so a zone that already has the right color is
        # not rewritten, and one changed from outside (another client, an OpenRGB restart) is.
        cols = self.ctl["colors"]
        self.written = {z["index"]: cols[z["start"]] for z in device_zones(self.ctl) if z["start"] < len(cols)}

    def frame(self):
        with LOCK:
            with self.lock:
                t = self.target
                if t is None:
                    return
                if self.conn is None:
                    self.conn = connect()
                    self.idx = device_index(self.conn)
                    self._read()
                elif time.monotonic() - self.read_at > self.REFRESH:
                    self._read()
                ctl, now = self.ctl, time.monotonic()
                zones = device_zones(ctl)
                m = pick_color_mode(ctl["modes"], t["effect"])
                if m is None:
                    raise ValueError(f"{ctl['name']} has no effect that can show a color")
                caps = sdk.capabilities(m)
                per_led = caps["per_led"] and not caps["mode_colors"]
                m = copy.deepcopy(m)
                same_effect = _same_mode(ctl, {**m, "color_mode": sdk.COLOR_MODE_PER_LED} if per_led else m)
                for zi, entry in self.cur.items():           # resolve "fade from what's showing"
                    if entry[0] is None:
                        z = zones[zi] if zi < len(zones) else None
                        live = ctl["colors"][z["start"]] if (z and same_effect and ctl["colors"]) else "000000"
                        entry[0], entry[2] = live, now
                        entry[3] = float(t.get("fade", 0)) if same_effect else 0.0
                period = max(1.0, float(t.get("period", 6)))
                pulse_f = 0.3 + 0.7 * (0.5 + 0.5 * math.cos(2 * math.pi * (time.time() % period) / period))
                want = {}
                for z in zones:
                    zi = z["index"]
                    if zi in self.cur:
                        c = self._base(zi, now)
                        if zi < len(t["pulse"]) and t["pulse"][zi]:
                            c = _mul(c, pulse_f)
                        want[zi] = c

                if per_led:
                    m["color_mode"] = sdk.COLOR_MODE_PER_LED
                else:                                         # one color for the device, set through the mode
                    first = next((self.cur[k][1] for k in sorted(self.cur)), "000000")
                    m["color_mode"] = sdk.COLOR_MODE_SPECIFIC
                    m["colors"] = [first] * max(1, m["colors_min"])
                if not _same_mode(ctl, m):
                    self.conn.update_mode(self.idx, m["index"], m)
                    LIVE["writes"]["mode"] += 1
                    ctl["modes"][m["index"]] = m
                    ctl["active_mode"] = m["index"]
                    self.written = {}
                    time.sleep(0.05)
                LIVE["effect"] = m["name"]
                if not per_led:
                    return
                for z in zones:
                    zi = z["index"]
                    c = want.get(zi)
                    if c is None or self.written.get(zi) == c:
                        continue
                    if z.get("whole"):
                        self.conn.update_leds(self.idx, [c] * z["leds_count"])
                    else:
                        self.conn.update_zone_leds(self.idx, zi, [c] * z["leds_count"])
                    self.written[zi] = c
                    LIVE["writes"]["colors"] += 1

    def run(self):
        while not STOP.is_set():
            if self.target is None:
                time.sleep(0.2)
                continue
            try:
                self.frame()
                self.error = None
            except Exception as e:                            # reconnect on the next frame
                self.error = str(e)[:200]
                with self.lock:
                    self._close()
                time.sleep(2)
                continue
            time.sleep(1.0 / FPS)


RENDER = Renderer()


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


# Whole physical disks. Partitions, loop and RAM devices, zvols (zd*), device-mapper
# and md arrays are skipped so nothing is counted twice.
DISK_NAME = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
_disk = {"prev": None}


def disk_names(stats):
    if DISK_DEVICES:
        want = {d.strip() for d in DISK_DEVICES.split(",") if d.strip()}
        return sorted(n for n in stats if n in want)
    try:                                                   # /sys/block lists whole devices only
        whole = set(os.listdir(os.path.join(HOST_SYS, "block")))
    except OSError:
        whole = None
    return sorted(n for n in stats if DISK_NAME.match(n) and (whole is None or n in whole))


def diskstats():
    """Bytes read + written per block device since boot, from /proc/diskstats."""
    out = {}
    with open(os.path.join(HOST_PROC, "diskstats")) as f:
        for line in f:
            p = line.split()
            if len(p) >= 10:
                out[p[2]] = (int(p[5]) + int(p[9])) * 512     # sectors are always 512 bytes here
    return out


def disk_mbps():
    """Disk throughput (read + write, all physical disks) since the previous call, in MB/s."""
    try:
        stats = diskstats()
    except (OSError, ValueError):
        return None
    names = disk_names(stats)
    LIVE["disks"] = names
    if not names:
        return None
    total, now = sum(stats[n] for n in names), time.monotonic()
    rate = None
    prev = _disk["prev"]
    if prev and prev[2] == names and now > prev[0]:
        rate = max(0.0, (total - prev[1]) / (now - prev[0]) / 1e6)
    _disk["prev"] = (now, total, names)
    return rate


def pool_states():
    out = {}
    for p in glob.glob(os.path.join(ZFS_KSTAT, "*/state")):
        try:
            out[p.replace("\\", "/").split("/")[-2]] = _read(p)
        except OSError:
            pass
    return out


ZPOOL_STALE = 15 * 60
SCAN_ACTIVE = re.compile(r"^\s*scan:\s*(scrub|resilver) in progress", re.M)
SCAN_PCT = re.compile(r"([\d.]+)% done")


def parse_zpool_status(text):
    """Pool states and running scans from `zpool status` output.

    Returns {pool: {"state": str, "scan": "scrub" | "resilver" | None, "percent": float | None}}.
    """
    pools = {}
    for block in re.split(r"^\s*pool:\s*", text, flags=re.M)[1:]:
        name = block.split("\n", 1)[0].strip()
        st = re.search(r"^\s*state:\s*(\S+)", block, re.M)
        scan = SCAN_ACTIVE.search(block)
        pct = SCAN_PCT.search(block) if scan else None
        pools[name] = {"state": st.group(1) if st else "UNKNOWN", "scan": scan.group(1) if scan else None,
                       "percent": float(pct.group(1)) if pct else None}
    return pools


def zpool_file():
    """Read ZPOOL_STATUS_FILE. Returns (pools, status) with status ok, missing or stale."""
    try:
        age = time.time() - os.path.getmtime(ZPOOL_STATUS_FILE)
        with open(ZPOOL_STATUS_FILE, encoding="utf-8", errors="replace") as f:
            text = f.read(1 << 20)
    except OSError:
        return {}, "missing"
    if age > ZPOOL_STALE:
        return {}, "stale"
    return parse_zpool_status(text), "ok"


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
    raw = {"drive_temp": drive_temp(), "cpu_temp": cpu_temp(), "network": net_mbps(), "disk_io": disk_mbps()}
    out = {}
    for k, v in raw.items():
        sv = smooth(k, v)
        out[k] = round(sv, 1) if sv is not None else None
    LIVE["raw"] = {k: (round(v, 1) if v is not None else None) for k, v in raw.items()}
    return out


# ---------------------------------------------------------------- push values
def push_entry(zone_name):
    """The value pushed for a zone (or for every zone), unless it has expired."""
    for key in (zone_name.lower(), "*"):
        e = PUSH.get(key)
        if e and (not e["until"] or time.time() < e["until"]):
            return e
    return None


# ---------------------------------------------------------------- color math
def _rgb(h):
    return [int(h[k:k + 2], 16) for k in (0, 2, 4)]


def _scale_pos(mode, value):
    """Where a value sits on a scale, as stop index + fraction (0 .. len(stops)-1)."""
    stops = sorted(STATE["config"]["scales"][mode], key=lambda s: s[0])
    f = (lambda v: math.log10(max(float(v), 0.1))) if mode in LOG_SCALES else float
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
def zone_color(src, zone_name, vals, scrubbing):
    """(color, pulse, value) for one zone. color None means no reading yet."""
    cfg = STATE["config"]
    scrub = cfg["scrub"]
    pulse = scrubbing and scrub["scope"] == "all" and src != "off"
    if src == "off":
        return "000000", False, None
    if src == "health":
        if scrubbing and scrub["scope"] in ("all", "health"):
            return scrub["color"], True, None
        return cfg["health_color"], False, None
    if src == "push":
        e = push_entry(zone_name)
        if e is None:
            return "000000", False, None
        col = e["color"] or scale_color("push", e["value"])
        return col, pulse or e["pulse"], e["value"]
    v = vals.get(src)
    if v is None:
        return None, pulse, None
    return status_color(src, v), pulse, v


def build_target(zones, vals, alert, quiet, scrubbing):
    """What the lights should show now: an effect plus a color and pulse per zone."""
    cfg = STATE["config"]
    bri = int(cfg.get("brightness", 100))
    effect, colors, pulse, info = "Static", [], [], []
    for z in zones:
        src = zone_source(z["name"])
        if alert:                                   # problems always show, on every zone, even in quiet hours
            col, pul, val = cfg["alert"]["color"], False, None
            effect = cfg["alert"]["effect"]
        elif quiet and cfg["quiet"]["action"] == "off":
            col, pul, val = "000000", False, None
        else:
            col, pul, val = zone_color(src, z["name"], vals, scrubbing)
        b = int(cfg["quiet"].get("dim", 15)) if (quiet and not alert) else bri
        colors.append(dim(col, b) if col else None)
        pulse.append(pul)
        info.append({"index": z["index"], "name": z["name"], "leds": z["leds"], "source": src,
                     "color": col, "pulse": pul, "value": val})
    return {"effect": effect, "colors": colors, "pulse": pulse, "fade": float(cfg.get("fade", 1.5)),
            "period": float(cfg["scrub"].get("period", 6))}, info


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
    zones = []
    while not STOP.is_set():
        cfg = STATE["config"]
        try:
            fresh = watch.changed()
            if fresh or not zones:                          # startup, or OpenRGB came back
                if fresh:
                    time.sleep(1.0)
                zones = controller_info()["zones"]
                if fresh:
                    RENDER.reconnect()
                    with LOCK:
                        if STATE["status_mode"] == "none" and not RENDER.active:
                            restore_manual()

            pools = pool_states()
            zfile, LIVE["zpool_file"] = zpool_file()
            for name, p in zfile.items():
                pools.setdefault(name, p["state"])
            scans = {k: {"kind": p["scan"], "percent": p["percent"]} for k, p in zfile.items() if p["scan"]}
            now = time.time()
            simulated = now < LIVE["simulate_until"]
            if simulated:
                pools["simulated"] = "DEGRADED"
            vals = readings()
            alert = any(s != "ONLINE" for s in pools.values())
            quiet = in_quiet_hours()
            LIVE.update(pools=pools, scans=scans, scrubbing=bool(scans), alert=alert, simulated=simulated,
                        quiet=quiet, readings=vals)

            target, info = build_target(zones, vals, alert, quiet, bool(scans))
            LIVE["zones"] = info
            if now < LIVE["preview_until"]:
                pass                                        # a preview owns the lights for now
            elif STATE["status_mode"] != "none" or simulated:
                RENDER.show(target)
            elif RENDER.active:                             # preview or simulation over in manual mode
                with LOCK:
                    RENDER.release()
                    LIVE["preview_until"] = 0
                    if STATE["status_mode"] == "none":
                        restore_manual()
            LIVE["error"] = RENDER.error if RENDER.active else None
        except Exception as e:                              # keep running; show the problem in the UI
            LIVE["error"] = str(e)[:200]
            zones = []
        wait = max(2, int(cfg.get("interval", 5)))
        for until in (LIVE["preview_until"], LIVE["simulate_until"]):
            if until > time.time():
                wait = min(wait, until - time.time() + 0.05)
        WAKE.wait(wait)
        WAKE.clear()


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
                    raise ValueError(f"{SOURCES[mode]}: {v:g} is outside {lo}-{hi}")
                if not HEX.match(str(col)):
                    raise ValueError("bad color " + str(col))
                cleaned.append([round(v, 1), str(col).upper()])
            cleaned.sort(key=lambda s: s[0])
            if len({s[0] for s in cleaned}) != len(cleaned):
                raise ValueError(f"{SOURCES[mode]}: two points share the same value")
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
    if "scrub" in c:
        s = c["scrub"]
        if not HEX.match(str(s.get("color", ""))):
            raise ValueError("bad scrub color")
        if s.get("scope") not in ("all", "health", "off"):
            raise ValueError("scrub scope must be all, health or off")
        out["scrub"] = {"color": s["color"].upper(), "scope": s["scope"],
                        "period": max(2, min(20, int(s.get("period", 6))))}
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


def clean_zones(z):
    if not isinstance(z, dict) or not 1 <= len(z) <= 64:
        raise ValueError("zones must map zone names to sources")
    out = {}
    for name, src in z.items():
        name = str(name)[:80]
        if src not in SOURCES:
            raise ValueError(f"unknown source '{src}' for {name}")
        out[name] = src
    out.setdefault("*", "off")
    return out


# ---------------------------------------------------------------- web
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "index.html")


def live_state():
    return {"status_mode": STATE["status_mode"], "zone_sources": STATE["zones"], "live": LIVE,
            "push": {k: v for k, v in PUSH.items()}}


def full_state():
    try:
        info = controller_info()
        info["openrgb_error"] = None
    except Exception as e:
        info = {"device": None, "zones": [], "modes": [], "current": None,
                "effects": {"caps": {}, "current": None, "speed_pct": None},
                "openrgb_error": f"Cannot reach OpenRGB at {OPENRGB_HOST}:{OPENRGB_PORT} ({e})"}
    alert_effects = [k for k, v in info["effects"]["caps"].items() if (v["per_led"] or v["mode_colors"]) and k != "Direct"]
    settings = {k: v for k, v in STATE.items() if k != "zones"}   # the mapping is sent as zone_sources
    return {**settings, **info, **live_state(), "sources": SOURCES, "units": UNITS, "limits": LIMITS,
            "log_scales": LOG_SCALES, "alert_effects": alert_effects or ["Static"], "defaults": DEFAULT_CONFIG,
            "push_token": bool(PUSH_TOKEN), "zpool_status_file": ZPOOL_STATUS_FILE, "version": __version__}


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
            return self._json_out(200, live_state())
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
        if not isinstance(req, dict):
            return self._err(400, "send a JSON object")
        path = self.path.split("?")[0]
        try:
            if path == "/api/status":
                return self._set_status(req)
            if path == "/api/zones":
                return self._set_zones(req)
            if path == "/api/config":
                return self._set_config(req)
            if path == "/api/preview":
                return self._preview(req)
            if path == "/api/simulate":
                return self._simulate(req)
            if path == "/api/push":
                return self._push(req)
            if path == "/api/set":
                return self._set_manual(req)
        except (ConnectionError, OSError) as e:
            return self._err(502, f"OpenRGB unreachable: {e}")
        except (ValueError, LookupError, TypeError) as e:
            return self._err(400, str(e))
        return self._err(404, "not found")

    def _status_changed(self):
        LIVE["preview_until"] = 0
        save_state()
        WAKE.set()

    def _set_status(self, req):
        """"none" for manual, "zones" for the per-zone mapping, or a source for every zone (0.2 style)."""
        k = str(req.get("status_mode", ""))
        if k not in SOURCES and k not in ("none", "zones"):
            return self._err(400, "unknown status mode")
        with LOCK:
            if k in SOURCES:
                STATE["zones"] = {"*": k}
            STATE["status_mode"] = "none" if k == "none" else "zones"
            self._status_changed()
            if k == "none":                               # back to the last manual setting
                RENDER.release()
                restore_manual()
            return self._json_out(200, full_state())

    def _set_zones(self, req):
        zones = clean_zones(req.get("zones"))
        with LOCK:
            STATE["zones"] = zones
            if req.get("enable", True):
                STATE["status_mode"] = "zones"
            self._status_changed()
            return self._json_out(200, full_state())

    def _set_config(self, req):
        try:
            new = clean_config(req)
        except (TypeError, KeyError) as e:
            return self._err(400, f"bad settings: {e}")
        with LOCK:
            STATE["config"] = new
            save_state()
            WAKE.set()
            return self._json_out(200, full_state())

    def _preview(self, req):
        mode = str(req.get("mode", ""))
        cfg = STATE["config"]
        if mode == "alert":
            effect, color = cfg["alert"]["effect"], cfg["alert"]["color"]
        elif mode == "health":
            effect, color = "Static", cfg["health_color"]
        elif mode in DEFAULT_CONFIG["scales"]:
            effect, color = "Static", scale_color(mode, float(req.get("value")))
        else:
            return self._err(400, "unknown preview")
        n = len(controller_info()["zones"])
        col = dim(color, int(cfg.get("brightness", 100)))
        RENDER.show({"effect": effect, "colors": [col] * n, "pulse": [False] * n, "fade": 0.4})
        LIVE["preview_until"] = time.time() + PREVIEW_SECONDS
        WAKE.set()
        return self._json_out(200, {"previewing": [effect, color], "seconds": PREVIEW_SECONDS})

    def _simulate(self, req):
        """Pretend a pool is DEGRADED for a few seconds, so the real alert path runs end to end."""
        secs = SIMULATE_SECONDS if req.get("stop") is None else 0
        LIVE["simulate_until"] = time.time() + secs if secs else 0
        LIVE["preview_until"] = 0
        WAKE.set()
        return self._json_out(200, {"simulating": bool(secs), "seconds": secs})

    def _push(self, req):
        """Set the value shown on zones whose source is "push"."""
        if PUSH_TOKEN:
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), ("Bearer " + PUSH_TOKEN).encode()):
                return self._err(401, "bad or missing push token")
        zone = str(req.get("zone", "*")).strip() or "*"
        if zone.isdigit():
            zones = controller_info()["zones"]
            zone = next((z["name"] for z in zones if z["index"] == int(zone)), None)
            if zone is None:
                return self._err(400, "no zone with that index")
        key = zone.lower() if zone != "*" else "*"
        if req.get("clear"):
            PUSH.pop(key, None)
        else:
            color = str(req.get("color") or "").lstrip("#")
            value = req.get("value")
            if color and not HEX.match(color):
                return self._err(400, "bad color")
            if not color and value is None:
                return self._err(400, "send a value or a color")
            if value is not None:
                value = float(value)
                if not math.isfinite(value):
                    return self._err(400, "bad value")
            ttl = max(0, min(7 * 86400, int(req.get("ttl", 0))))
            PUSH[key] = {"zone": zone, "value": value, "color": color.upper() or None,
                         "pulse": bool(req.get("pulse")), "until": time.time() + ttl if ttl else 0,
                         "at": time.time()}
        WAKE.set()
        return self._json_out(200, {"ok": True, "push": PUSH.get(key)})

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
            LIVE.update(preview_until=0, simulate_until=0)
            RENDER.release()
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
            WAKE.set()
            return self._json_out(200, full_state())

    def log_message(self, fmt, *args):
        pass


def main():
    print(f"NAS Lights {__version__} on http://{BIND}:{PORT} -> OpenRGB {OPENRGB_HOST}:{OPENRGB_PORT}, "
          f"device {OPENRGB_DEVICE}, data {DATA_DIR}", flush=True)
    threading.Thread(target=RENDER.run, daemon=True, name="render").start()
    threading.Thread(target=status_loop, daemon=True, name="status").start()
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
