"""A small in-process OpenRGB SDK server for tests.

It speaks the subset of the protocol that app/sdk.py uses. The encoders and
decoders here are written independently of sdk.py, so the tests check sdk.py
against the protocol layout rather than against itself.

Writes from clients are recorded in `FakeOpenRGB.log` and applied to the fake
device, so a later read sees the new mode and colors like a real server would.
"""
import copy
import socket
import struct
import threading

PKT_REQUEST_CONTROLLER_COUNT = 0
PKT_REQUEST_CONTROLLER_DATA = 1
PKT_REQUEST_PROTOCOL_VERSION = 40
PKT_SET_CLIENT_NAME = 50
PKT_REQUEST_PROFILE_LIST = 150
PKT_REQUEST_SAVE_PROFILE = 151
PKT_REQUEST_LOAD_PROFILE = 152
PKT_UPDATE_LEDS = 1050
PKT_UPDATE_MODE = 1101

FLAG_SPEED = 1 << 0
FLAG_DIR_LR = 1 << 1
FLAG_BRIGHTNESS = 1 << 4
FLAG_PER_LED_COLOR = 1 << 5
FLAG_MODE_COLOR = 1 << 6


def mode(name, value, flags=0, speed_min=0, speed_max=0, speed=0, brightness_min=0, brightness_max=0,
         brightness=0, colors_min=0, colors_max=0, direction=0, color_mode=0, colors=()):
    return {"name": name, "value": value, "flags": flags, "speed_min": speed_min, "speed_max": speed_max,
            "brightness_min": brightness_min, "brightness_max": brightness_max, "colors_min": colors_min,
            "colors_max": colors_max, "speed": speed, "brightness": brightness, "direction": direction,
            "color_mode": color_mode, "colors": list(colors)}


def asrock_like():
    """A controller shaped like an ASRock Polychrome board: a per-LED Direct
    mode, a Static mode with one mode color, and a cycle effect whose speed
    range runs backwards (255 is slowest, 0 is fastest)."""
    return {
        "type": 0,
        "name": "ASRock B650M Pro RS",
        "vendor": "ASRock",
        "description": "ASRock Polychrome USB Device",
        "version": "1.0",
        "serial": "",
        "location": "HID: /dev/hidraw0",
        "active_mode": 1,
        "modes": [
            mode("Direct", 0xFFFF, FLAG_PER_LED_COLOR, color_mode=1),
            mode("Static", 1, FLAG_MODE_COLOR | FLAG_PER_LED_COLOR, colors_min=1, colors_max=1,
                 color_mode=2, colors=["FFFFFF"]),
            mode("Breathing", 2, FLAG_SPEED | FLAG_MODE_COLOR | FLAG_BRIGHTNESS, speed_min=255, speed_max=0,
                 speed=128, brightness_min=0, brightness_max=255, brightness=255, colors_min=1, colors_max=1,
                 color_mode=2, colors=["FF0000"]),
            mode("Spectrum Cycle", 4, FLAG_SPEED | FLAG_DIR_LR, speed_min=255, speed_max=0, speed=128),
            mode("Off", 6),
        ],
        "zones": [
            {"name": "RGB LED Header", "type": 1, "leds_min": 1, "leds_max": 1, "leds_count": 1, "matrix": None},
            {"name": "Matrix", "type": 2, "leds_min": 2, "leds_max": 2, "leds_count": 2, "matrix": (1, 2, [0, 1])},
        ],
        "leds": ["Header LED", "Matrix LED 1", "Matrix LED 2"],
        "colors": ["000000", "000000", "000000"],
    }


# ---- encoding (server side)
def rgb_u32(h):
    return int(h[0:2], 16) | int(h[2:4], 16) << 8 | int(h[4:6], 16) << 16


def u32_rgb(v):
    return f"{v & 0xFF:02X}{v >> 8 & 0xFF:02X}{v >> 16 & 0xFF:02X}"


def pstr(s):
    raw = s.encode() + b"\0"
    return struct.pack("<H", len(raw)) + raw


def encode_mode(m, proto):
    out = pstr(m["name"]) + struct.pack("<iIII", m["value"], m["flags"], m["speed_min"], m["speed_max"])
    if proto >= 3:
        out += struct.pack("<II", m["brightness_min"], m["brightness_max"])
    out += struct.pack("<III", m["colors_min"], m["colors_max"], m["speed"])
    if proto >= 3:
        out += struct.pack("<I", m["brightness"])
    out += struct.pack("<II", m["direction"], m["color_mode"])
    out += struct.pack("<H", len(m["colors"])) + b"".join(struct.pack("<I", rgb_u32(c)) for c in m["colors"])
    return out


def encode_controller(c, proto):
    out = struct.pack("<i", c["type"]) + pstr(c["name"])
    if proto >= 1:
        out += pstr(c["vendor"])
    out += pstr(c["description"]) + pstr(c["version"]) + pstr(c["serial"]) + pstr(c["location"])
    out += struct.pack("<Hi", len(c["modes"]), c["active_mode"])
    out += b"".join(encode_mode(m, proto) for m in c["modes"])
    out += struct.pack("<H", len(c["zones"]))
    for z in c["zones"]:
        out += pstr(z["name"]) + struct.pack("<iIII", z["type"], z["leds_min"], z["leds_max"], z["leds_count"])
        if z["matrix"]:
            h, w, cells = z["matrix"]
            body = struct.pack("<II", h, w) + b"".join(struct.pack("<I", v) for v in cells)
            out += struct.pack("<H", len(body)) + body
        else:
            out += struct.pack("<H", 0)
    out += struct.pack("<H", len(c["leds"]))
    for i, name in enumerate(c["leds"]):
        out += pstr(name) + struct.pack("<I", i)
    out += struct.pack("<H", len(c["colors"])) + b"".join(struct.pack("<I", rgb_u32(x)) for x in c["colors"])
    return struct.pack("<I", len(out) + 4) + out


# ---- decoding (client writes)
class Reader:
    def __init__(self, b):
        self.b, self.o = b, 0

    def take(self, fmt):
        v = struct.unpack_from(fmt, self.b, self.o)
        self.o += struct.calcsize(fmt)
        return v if len(v) > 1 else v[0]

    def string(self):
        n = self.take("<H")
        s = self.b[self.o:self.o + n].rstrip(b"\0").decode()
        self.o += n
        return s


def decode_mode(r, proto):
    m = {"name": r.string()}
    m["value"], m["flags"], m["speed_min"], m["speed_max"] = r.take("<iIII")
    if proto >= 3:
        m["brightness_min"], m["brightness_max"] = r.take("<II")
    m["colors_min"], m["colors_max"], m["speed"] = r.take("<III")
    if proto >= 3:
        m["brightness"] = r.take("<I")
    m["direction"], m["color_mode"] = r.take("<II")
    m["colors"] = [u32_rgb(r.take("<I")) for _ in range(r.take("<H"))]
    return m


class FakeOpenRGB:
    """Threaded fake server on 127.0.0.1. Use as a context manager."""

    def __init__(self, controllers=None, proto=3, profiles=("Default",)):
        self.controllers = controllers if controllers is not None else [asrock_like()]
        self.server_proto = proto
        self.profiles = list(profiles)
        self.log = []                      # (packet name, device index, details)
        self.client_names = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        self._open = 0
        self._cv = threading.Condition()
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._sock.close()

    # ---- inspection helpers
    def wait_idle(self, timeout=5):
        """Block until every client connection has closed and been fully read."""
        with self._cv:
            if not self._cv.wait_for(lambda: self._open == 0, timeout):
                raise TimeoutError("fake OpenRGB server still has open connections")

    def writes(self, kind):
        return [e for e in self.log if e[0] == kind]

    def clear_log(self):
        self.log.clear()

    # ---- server
    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._cv:
                self._open += 1
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    @staticmethod
    def _exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _client(self, conn):
        proto = 0
        try:
            while True:
                hdr = self._exact(conn, 16)
                if hdr is None:
                    return
                magic, dev, pid, size = struct.unpack("<4sIII", hdr)
                assert magic == b"ORGB"
                body = self._exact(conn, size) if size else b""
                if body is None:
                    return

                def reply(data, pid=pid, dev=dev):
                    conn.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data)

                if pid == PKT_SET_CLIENT_NAME:
                    self.client_names.append(body.rstrip(b"\0").decode())
                elif pid == PKT_REQUEST_PROTOCOL_VERSION:
                    proto = min(struct.unpack("<I", body)[0], self.server_proto)
                    reply(struct.pack("<I", self.server_proto))
                elif pid == PKT_REQUEST_CONTROLLER_COUNT:
                    reply(struct.pack("<I", len(self.controllers)))
                elif pid == PKT_REQUEST_CONTROLLER_DATA:
                    want = struct.unpack("<I", body)[0] if body else 0
                    reply(encode_controller(self.controllers[dev], want))
                elif pid == PKT_UPDATE_MODE:
                    r = Reader(body)
                    r.take("<I")
                    idx = r.take("<i")
                    m = decode_mode(r, proto)
                    ctl = self.controllers[dev]
                    ctl["active_mode"] = idx
                    ctl["modes"][idx] = {**ctl["modes"][idx], **copy.deepcopy(m)}
                    self.log.append(("mode", dev, {"index": idx, **m}))
                elif pid == PKT_UPDATE_LEDS:
                    r = Reader(body)
                    r.take("<I")
                    cols = [u32_rgb(r.take("<I")) for _ in range(r.take("<H"))]
                    self.controllers[dev]["colors"] = cols
                    self.log.append(("leds", dev, cols))
                elif pid == PKT_REQUEST_PROFILE_LIST:
                    data = struct.pack("<H", len(self.profiles)) + b"".join(pstr(p) for p in self.profiles)
                    reply(struct.pack("<I", len(data) + 4) + data)
                elif pid in (PKT_REQUEST_SAVE_PROFILE, PKT_REQUEST_LOAD_PROFILE):
                    kind = "save" if pid == PKT_REQUEST_SAVE_PROFILE else "load"
                    self.log.append((kind, dev, body.rstrip(b"\0").decode()))
        except OSError:
            return
        finally:
            conn.close()
            with self._cv:
                self._open -= 1
                self._cv.notify_all()
