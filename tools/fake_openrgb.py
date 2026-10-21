#!/usr/bin/env python3
"""A fake OpenRGB SDK server, for developing and testing NAS Lights without RGB hardware.

It pretends to be the ASRock B850M-C Polychrome USB controller NAS Lights was
built on: one 1-LED header and three 80-LED addressable headers, no hardware
brightness, and a reversed speed range. It keeps the LED colors and the active
mode in memory and counts every write.

    python tools/fake_openrgb.py                 # SDK on 127.0.0.1:6742
    python tools/fake_openrgb.py --http 6743     # also serve the state as JSON

GET http://127.0.0.1:6743/ returns the active mode, each zone's colors and the
write counters. Standard library only.
"""
import argparse
import json
import os
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import sdk  # noqa: E402

PER_LED, SPEED = sdk.FLAG_PER_LED_COLOR, sdk.FLAG_SPEED
SLOW, FAST = 0xFF, 0x00                     # reversed: the hardware reports 255 as slowest


def _mode(name, value, flags, color_mode, speed=0):
    has_speed = bool(flags & SPEED)
    return {"name": name, "value": value, "flags": flags,
            "speed_min": SLOW if has_speed else 0, "speed_max": FAST if has_speed else 0,
            "brightness_min": 0, "brightness_max": 0, "brightness": 0,
            "colors_min": 0, "colors_max": 0, "speed": speed if has_speed else 0,
            "direction": 0, "color_mode": color_mode, "colors": []}


MODES = [
    _mode("Off", 0, 0, sdk.COLOR_MODE_NONE),
    _mode("Static", 1, PER_LED, sdk.COLOR_MODE_PER_LED),
    _mode("Breathing", 2, SPEED | PER_LED, sdk.COLOR_MODE_PER_LED, 0x80),
    _mode("Strobe", 3, SPEED | PER_LED, sdk.COLOR_MODE_PER_LED, 0x80),
    _mode("Spectrum Cycle", 4, SPEED, sdk.COLOR_MODE_NONE, 0x80),
    _mode("Random", 5, SPEED, sdk.COLOR_MODE_NONE, 0x80),
    _mode("Wave", 7, SPEED, sdk.COLOR_MODE_NONE, 0x80),
    _mode("Rainbow", 14, SPEED, sdk.COLOR_MODE_NONE, 0x80),
]
ZONES = [("RGB LED 1 Header", 0, 1), ("Addressable Header 1", 1, 80),
         ("Addressable Header 2", 1, 80), ("Addressable Header 3", 1, 80)]


class Device:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 1
        self.modes = [dict(m) for m in MODES]
        self.colors = ["000000"] * sum(z[2] for z in ZONES)
        self.writes = {"mode": 0, "leds": 0, "zone_leds": 0}
        self.clients = 0

    def zone_range(self, zi):
        start = sum(z[2] for z in ZONES[:zi])
        return start, start + ZONES[zi][2]

    def pack(self, proto):
        s = sdk._pstr
        out = struct.pack("<i", 6)                                   # device type: motherboard
        out += s("ASRock B850M-C") + (s("ASRock") if proto >= 1 else b"")
        out += s("ASRock Polychrome USB Device") + s("fake 1.0") + s("") + s("HID: /dev/fake")
        out += struct.pack("<Hi", len(self.modes), self.active)
        for m in self.modes:
            out += sdk.pack_mode(m, proto)
        out += struct.pack("<H", len(ZONES))
        for name, ztype, n in ZONES:
            out += s(name) + struct.pack("<iIIIH", ztype, n, n, n, 0)
        out += struct.pack("<H", len(self.colors))
        for name, _, n in ZONES:
            for k in range(n):
                out += s(f"{name} LED {k + 1}") + struct.pack("<I", 0)
        out += struct.pack("<H", len(self.colors))
        out += b"".join(struct.pack("<I", sdk._rgb_to_u32(c)) for c in self.colors)
        return struct.pack("<I", len(out) + 4) + out

    def snapshot(self):
        with self.lock:
            zones = []
            for zi, (name, _, n) in enumerate(ZONES):
                a, b = self.zone_range(zi)
                cols = self.colors[a:b]
                zones.append({"name": name, "leds": n, "color": cols[0], "uniform": len(set(cols)) == 1})
            return {"mode": self.modes[self.active]["name"], "zones": zones,
                    "writes": dict(self.writes), "clients": self.clients}


DEV = Device()


def _colors(body, off):
    n = struct.unpack_from("<H", body, off)[0]
    return [sdk._u32_to_rgb(struct.unpack_from("<I", body, off + 2 + 4 * k)[0]) for k in range(n)]


def serve_client(conn, verbose):
    proto = 0
    with DEV.lock:
        DEV.clients += 1

    def send(pid, data, dev=0):
        conn.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data)

    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    try:
        while True:
            magic, dev, pid, size = struct.unpack("<4sIII", exact(16))
            body = exact(size) if size else b""
            if magic != b"ORGB":
                return
            if pid == sdk.PKT_REQUEST_PROTOCOL_VERSION:
                proto = min(struct.unpack("<I", body[:4])[0], 3)
                send(pid, struct.pack("<I", 3))
            elif pid == sdk.PKT_REQUEST_CONTROLLER_COUNT:
                send(pid, struct.pack("<I", 1))
            elif pid == sdk.PKT_REQUEST_CONTROLLER_DATA:
                p = struct.unpack("<I", body[:4])[0] if len(body) >= 4 else proto
                with DEV.lock:
                    send(pid, DEV.pack(p), dev)
            elif pid == sdk.PKT_REQUEST_PROFILE_LIST:
                send(pid, struct.pack("<IH", 6, 0))
            elif pid == sdk.PKT_UPDATE_MODE:
                idx = struct.unpack_from("<i", body, 4)[0]
                with DEV.lock:
                    m = DEV.modes[idx]
                    r = sdk._Reader(body[8:])
                    r.string()
                    r.i32()
                    r.u32(), r.u32(), r.u32()
                    if proto >= 3:
                        r.u32(), r.u32()
                    r.u32(), r.u32()
                    m["speed"] = r.u32()
                    if proto >= 3:
                        m["brightness"] = r.u32()
                    m["direction"] = r.u32()
                    m["color_mode"] = r.u32()
                    m["colors"] = [sdk._u32_to_rgb(r.u32()) for _ in range(r.u16())]
                    DEV.active = idx
                    DEV.colors = ["000000"] * len(DEV.colors)     # like the real board: a mode write blanks
                    DEV.writes["mode"] += 1
                if verbose:
                    print(f"mode -> {m['name']}", flush=True)
            elif pid == sdk.PKT_UPDATE_LEDS:
                cols = _colors(body, 4)
                with DEV.lock:
                    DEV.colors[:len(cols)] = cols
                    DEV.writes["leds"] += 1
                if verbose:
                    print(f"leds -> {sorted(set(cols))}", flush=True)
            elif pid == sdk.PKT_UPDATE_ZONE_LEDS:
                zi = struct.unpack_from("<I", body, 4)[0]
                cols = _colors(body, 8)
                with DEV.lock:
                    a, b = DEV.zone_range(zi)
                    DEV.colors[a:a + min(len(cols), b - a)] = cols[:b - a]
                    DEV.writes["zone_leds"] += 1
                if verbose:
                    print(f"zone {zi} -> {sorted(set(cols))}", flush=True)
    except (ConnectionError, OSError):
        pass
    finally:
        with DEV.lock:
            DEV.clients -= 1
        conn.close()


class StateHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps(DEV.snapshot(), indent=1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6742)
    ap.add_argument("--http", type=int, default=0, help="also serve the device state as JSON on this port")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every write")
    a = ap.parse_args()
    if a.http:
        threading.Thread(target=ThreadingHTTPServer((a.host, a.http), StateHandler).serve_forever,
                         daemon=True).start()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((a.host, a.port))
    srv.listen()
    print(f"fake OpenRGB on {a.host}:{a.port}" + (f", state on http://{a.host}:{a.http}/" if a.http else ""), flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_client, args=(conn, a.verbose), daemon=True).start()


if __name__ == "__main__":
    main()
