"""Minimal OpenRGB SDK client (Python standard library only).

Implements the parts of the OpenRGB network protocol that NAS Lights uses:
protocol negotiation, controller count, controller data (modes, zones, LEDs),
mode updates, LED and per-zone color updates, and server-side profile save/load.

Protocol reference: the SDK documentation in the OpenRGB repository,
https://codeberg.org/OpenRGB/OpenRGB
"""
import socket
import struct

PKT_REQUEST_CONTROLLER_COUNT = 0
PKT_REQUEST_CONTROLLER_DATA = 1
PKT_REQUEST_PROTOCOL_VERSION = 40
PKT_SET_CLIENT_NAME = 50
PKT_REQUEST_PROFILE_LIST = 150
PKT_REQUEST_SAVE_PROFILE = 151
PKT_REQUEST_LOAD_PROFILE = 152
PKT_UPDATE_LEDS = 1050
PKT_UPDATE_ZONE_LEDS = 1051
PKT_UPDATE_MODE = 1101

# Highest protocol version whose controller layout this client understands.
# Version 3 added per-mode brightness; version 4 added zone segments.
CLIENT_MAX_PROTOCOL = 3

FLAG_SPEED = 1 << 0
FLAG_DIR_LR = 1 << 1
FLAG_DIR_UD = 1 << 2
FLAG_DIR_HV = 1 << 3
FLAG_BRIGHTNESS = 1 << 4
FLAG_PER_LED_COLOR = 1 << 5
FLAG_MODE_COLOR = 1 << 6
FLAG_RANDOM_COLOR = 1 << 7

COLOR_MODE_NONE, COLOR_MODE_PER_LED, COLOR_MODE_SPECIFIC, COLOR_MODE_RANDOM = 0, 1, 2, 3


class SDK:
    """One short-lived connection to an OpenRGB SDK server."""

    def __init__(self, host="127.0.0.1", port=6742, name="NAS Lights", timeout=10):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self._send(PKT_SET_CLIENT_NAME, name.encode() + b"\0")
        self._send(PKT_REQUEST_PROTOCOL_VERSION, struct.pack("<I", CLIENT_MAX_PROTOCOL))
        _, body = self._recv(PKT_REQUEST_PROTOCOL_VERSION)
        server = struct.unpack("<I", body[:4])[0] if len(body) >= 4 else 0
        self.proto = min(server, CLIENT_MAX_PROTOCOL)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- framing
    def _send(self, pid, data=b"", dev=0):
        self.s.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data)

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OpenRGB server closed the connection")
            buf += chunk
        return buf

    def _recv(self, want_pid):
        while True:
            magic, _dev, pid, size = struct.unpack("<4sIII", self._exact(16))
            body = self._exact(size)
            if magic != b"ORGB":
                raise ConnectionError("bad packet header from OpenRGB server")
            if pid == want_pid:
                return _dev, body

    # ---- requests
    def count(self):
        self._send(PKT_REQUEST_CONTROLLER_COUNT)
        _, body = self._recv(PKT_REQUEST_CONTROLLER_COUNT)
        return struct.unpack("<I", body[:4])[0]

    def controller(self, idx=0):
        self._send(PKT_REQUEST_CONTROLLER_DATA, struct.pack("<I", self.proto), dev=idx)
        _, body = self._recv(PKT_REQUEST_CONTROLLER_DATA)
        return parse_controller(body, self.proto)

    def update_mode(self, idx, mode_index, mode):
        payload = struct.pack("<i", mode_index) + pack_mode(mode, self.proto)
        self._send(PKT_UPDATE_MODE, struct.pack("<I", len(payload) + 4) + payload, dev=idx)

    def update_leds(self, idx, colors):
        payload = struct.pack("<H", len(colors)) + b"".join(struct.pack("<I", _rgb_to_u32(c)) for c in colors)
        self._send(PKT_UPDATE_LEDS, struct.pack("<I", len(payload) + 4) + payload, dev=idx)

    def update_zone_leds(self, idx, zone, colors):
        """Set the colors of one zone's LEDs; other zones are left alone."""
        payload = (struct.pack("<IH", zone, len(colors))
                   + b"".join(struct.pack("<I", _rgb_to_u32(c)) for c in colors))
        self._send(PKT_UPDATE_ZONE_LEDS, struct.pack("<I", len(payload) + 4) + payload, dev=idx)

    def save_profile(self, name):
        self._send(PKT_REQUEST_SAVE_PROFILE, name.encode() + b"\0")

    def load_profile(self, name):
        self._send(PKT_REQUEST_LOAD_PROFILE, name.encode() + b"\0")

    def profiles(self):
        self._send(PKT_REQUEST_PROFILE_LIST)
        _, body = self._recv(PKT_REQUEST_PROFILE_LIST)
        r = _Reader(body)
        r.u32()
        return [r.string() for _ in range(r.u16())]


# ---- (de)serialization helpers
def _rgb_to_u32(h):
    return int(h[0:2], 16) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)


def _u32_to_rgb(v):
    return f"{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}{(v >> 16) & 0xFF:02X}"


class _Reader:
    def __init__(self, b):
        self.b, self.o = b, 0

    def _take(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.o)[0]
        self.o += struct.calcsize(fmt)
        return v

    def u16(self):
        return self._take("<H")

    def u32(self):
        return self._take("<I")

    def i32(self):
        return self._take("<i")

    def string(self):
        n = self.u16()
        raw = self.b[self.o:self.o + n]
        self.o += n
        return raw.rstrip(b"\0").decode("utf-8", "replace")


def parse_controller(b, proto):
    r = _Reader(b)
    r.u32()                                   # data size
    c = {"type": r.i32(), "name": r.string()}
    c["vendor"] = r.string() if proto >= 1 else ""
    c["description"] = r.string()
    c["version"] = r.string()
    c["serial"] = r.string()
    c["location"] = r.string()

    n_modes = r.u16()
    c["active_mode"] = r.i32()
    modes = []
    for i in range(n_modes):
        m = {"index": i, "name": r.string(), "value": r.i32(), "flags": r.u32(),
             "speed_min": r.u32(), "speed_max": r.u32(),
             "brightness_min": 0, "brightness_max": 0, "brightness": 0}
        if proto >= 3:
            m["brightness_min"] = r.u32()
            m["brightness_max"] = r.u32()
        m["colors_min"] = r.u32()
        m["colors_max"] = r.u32()
        m["speed"] = r.u32()
        if proto >= 3:
            m["brightness"] = r.u32()
        m["direction"] = r.u32()
        m["color_mode"] = r.u32()
        m["colors"] = [_u32_to_rgb(r.u32()) for _ in range(r.u16())]
        modes.append(m)
    c["modes"] = modes

    zones = []
    start = 0
    for i in range(r.u16()):
        z = {"index": i, "name": r.string(), "type": r.i32(), "leds_min": r.u32(), "leds_max": r.u32(),
             "leds_count": r.u32(), "start": start}
        start += z["leds_count"]
        matrix_len = r.u16()
        if matrix_len:
            r.o += matrix_len                 # height, width and map; not needed here
        zones.append(z)
    c["zones"] = zones

    c["leds"] = []
    for _ in range(r.u16()):
        c["leds"].append(r.string())
        r.u32()
    c["colors"] = [_u32_to_rgb(r.u32()) for _ in range(r.u16())]
    return c


def _pstr(s):
    raw = s.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def pack_mode(m, proto):
    out = _pstr(m["name"]) + struct.pack("<iIII", m["value"], m["flags"], m["speed_min"], m["speed_max"])
    if proto >= 3:
        out += struct.pack("<II", m["brightness_min"], m["brightness_max"])
    out += struct.pack("<III", m["colors_min"], m["colors_max"], m["speed"])
    if proto >= 3:
        out += struct.pack("<I", m["brightness"])
    out += struct.pack("<II", m["direction"], m["color_mode"])
    cols = m.get("colors") or []
    out += struct.pack("<H", len(cols)) + b"".join(struct.pack("<I", _rgb_to_u32(c)) for c in cols)
    return out


def capabilities(m):
    """What a mode lets you adjust, in plain terms."""
    f = m["flags"]
    directions = []
    if f & FLAG_DIR_LR:
        directions += ["Left", "Right"]
    if f & FLAG_DIR_UD:
        directions += ["Up", "Down"]
    if f & FLAG_DIR_HV:
        directions += ["Horizontal", "Vertical"]
    return {
        "name": m["name"],
        "speed": bool(f & FLAG_SPEED),
        "brightness": bool(f & FLAG_BRIGHTNESS) and m["brightness_min"] != m["brightness_max"],
        "directions": directions,
        "per_led": bool(f & FLAG_PER_LED_COLOR),
        "mode_colors": bool(f & FLAG_MODE_COLOR),
        "colors_min": m["colors_min"],
        "colors_max": m["colors_max"],
        "random": bool(f & FLAG_RANDOM_COLOR),
    }


DIRECTION_VALUES = {"Left": 0, "Right": 1, "Up": 2, "Down": 3, "Horizontal": 4, "Vertical": 5}


if __name__ == "__main__":
    import json
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    with SDK(host) as s:
        print(f"protocol {s.proto}, {s.count()} controller(s)")
        for i in range(s.count()):
            c = s.controller(i)
            print(f"\n[{i}] {c['name']} ({c['description']}), {len(c['leds'])} LEDs, active: {c['modes'][c['active_mode']]['name']}")
            for z in c["zones"]:
                print(f"    zone {z['index']}: {z['name']}, {z['leds_count']} LEDs")
            for m in c["modes"]:
                caps = {k: v for k, v in capabilities(m).items() if v not in (False, [], 0) and k != "name"}
                print(f"    {m['name']:<16} {json.dumps(caps)}")
