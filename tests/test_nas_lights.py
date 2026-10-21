"""Tests for NAS Lights, run against the fake OpenRGB server in tools/.

    python -m unittest discover -s tests -v

Standard library only.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path[:0] = [os.path.join(ROOT, "app"), os.path.join(ROOT, "tools")]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TMP = tempfile.mkdtemp(prefix="nasl-test-")
SDK_PORT, WEB_PORT = free_port(), free_port()
os.environ.update(OPENRGB_HOST="127.0.0.1", OPENRGB_PORT=str(SDK_PORT), NASL_DATA=os.path.join(TMP, "data"),
                  ZPOOL_STATUS_FILE=os.path.join(TMP, "zpool-status.txt"), ZFS_KSTAT=os.path.join(TMP, "kstat"),
                  HOST_SYS=os.path.join(TMP, "sys"), HOST_PROC=os.path.join(TMP, "proc"))

import fake_openrgb  # noqa: E402
import nas_lights as nl  # noqa: E402


def start_fake_server():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", SDK_PORT))
    srv.listen()

    def accept():
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=fake_openrgb.serve_client, args=(conn, False), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()


start_fake_server()
DEV = fake_openrgb.DEV


def zone_colors():
    return [z["color"] for z in DEV.snapshot()["zones"]]


class ZpoolStatus(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
            return f.read()

    def test_scrub_in_progress(self):
        pools = nl.parse_zpool_status(self.read("zpool-status-scrub.txt"))
        self.assertEqual(set(pools), {"apps", "boot-pool", "tank"})
        self.assertEqual(pools["tank"]["scan"], "scrub")
        self.assertAlmostEqual(pools["tank"]["percent"], 43.76)
        self.assertIsNone(pools["apps"]["scan"])             # a finished scrub is not running
        self.assertTrue(all(p["state"] == "ONLINE" for p in pools.values()))

    def test_resilver_and_degraded(self):
        pools = nl.parse_zpool_status(self.read("zpool-status-resilver.txt"))
        self.assertEqual(pools["tank"], {"state": "DEGRADED", "scan": "resilver", "percent": 12.5})

    def test_paused_scrub_is_not_running(self):
        text = "  pool: tank\n state: ONLINE\n  scan: scrub paused since Tue Oct 20 16:26:23 2026\n"
        self.assertIsNone(nl.parse_zpool_status(text)["tank"]["scan"])

    def test_missing_and_stale_file(self):
        path = nl.ZPOOL_STATUS_FILE
        if os.path.exists(path):
            os.remove(path)
        self.assertEqual(nl.zpool_file(), ({}, "missing"))
        with open(path, "w") as f:
            f.write(self.read("zpool-status-scrub.txt"))
        self.assertEqual(nl.zpool_file()[1], "ok")
        old = time.time() - nl.ZPOOL_STALE - 60
        os.utime(path, (old, old))
        self.assertEqual(nl.zpool_file(), ({}, "stale"))
        os.remove(path)


class Migration(unittest.TestCase):
    def test_single_reading_becomes_every_zone(self):
        st = {"status_mode": "network"}
        nl.migrate(st)
        self.assertEqual(st, {"status_mode": "zones", "zones": {"*": "network"}})

    def test_manual_stays_manual(self):
        st = {"status_mode": "none"}
        nl.migrate(st)
        self.assertEqual(st["status_mode"], "none")
        self.assertIn(st["zones"]["*"], nl.SOURCES)

    def test_already_migrated(self):
        st = {"status_mode": "zones", "zones": {"Addressable Header 1": "health", "*": "off"}}
        nl.migrate(st)
        self.assertEqual(st["zones"]["Addressable Header 1"], "health")


class DiskIO(unittest.TestCase):
    def test_counts_whole_disks_only(self):
        os.makedirs(nl.HOST_PROC, exist_ok=True)
        for d in ("sda", "nvme0n1", "loop0", "zd0"):
            os.makedirs(os.path.join(nl.HOST_SYS, "block", d), exist_ok=True)
        stats = {"sda": 1, "sda1": 1, "nvme0n1": 1, "nvme0n1p1": 1, "loop0": 1, "zd0": 1, "md127": 1, "dm-0": 1}
        self.assertEqual(nl.disk_names(stats), ["nvme0n1", "sda"])

    def test_rate(self):
        path = os.path.join(nl.HOST_PROC, "diskstats")
        os.makedirs(nl.HOST_PROC, exist_ok=True)
        os.makedirs(os.path.join(nl.HOST_SYS, "block", "sda"), exist_ok=True)

        def write(sectors):
            with open(path, "w") as f:
                f.write(f"   8       0 sda 10 0 {sectors} 0 10 0 {sectors} 0 0 0 0 0 0 0 0\n"
                        f"   8       1 sda1 10 0 {sectors} 0 10 0 {sectors} 0 0 0 0 0 0 0 0\n")
        nl._disk["prev"] = None
        write(0)
        self.assertIsNone(nl.disk_mbps())
        nl._disk["prev"] = (nl._disk["prev"][0] - 1.0,) + nl._disk["prev"][1:]   # pretend a second passed
        write(100_000)                                          # 2 x 100k sectors x 512 B = 102.4 MB
        self.assertAlmostEqual(nl.disk_mbps(), 102.4, delta=2)


ZONES = [{"index": i, "name": n, "leds": c} for i, (n, _, c) in enumerate(fake_openrgb.ZONES)]


class RendererTest(unittest.TestCase):
    """The per-zone renderer against the fake ASRock controller."""

    def setUp(self):
        nl.STATE["status_mode"] = "none"                        # keep the app's own renderer idle
        nl.LIVE.update(simulate_until=0, preview_until=0)
        nl.RENDER.release()
        self.r = nl.Renderer()
        with DEV.lock:
            DEV.active = 1                                      # Static
            DEV.colors = ["000000"] * len(DEV.colors)
            DEV.writes = {"mode": 0, "leds": 0, "zone_leds": 0}

    def target(self, colors, effect="Static", pulse=None, fade=0.0):
        return {"effect": effect, "colors": colors, "pulse": pulse or [False] * len(colors), "fade": fade, "period": 6}

    def frames(self, n=1):
        for _ in range(n):
            self.r.frame()
        time.sleep(0.15)                                        # let the server process the writes

    def test_zones_get_their_own_colors_without_a_mode_write(self):
        self.r.show(self.target(["FF0000", "00FF00", "000000", "0000FF"]))
        self.frames()
        self.assertEqual(zone_colors(), ["FF0000", "00FF00", "000000", "0000FF"])
        w = DEV.snapshot()["writes"]
        self.assertEqual(w["mode"], 0)                          # Static was already active
        self.assertEqual(w["leds"], 0)
        self.assertEqual(w["zone_leds"], 3)                     # zone 2 was already black

    def test_only_changed_zones_are_written(self):
        self.r.show(self.target(["FF0000", "00FF00", "000000", "0000FF"]))
        self.frames()
        before = DEV.snapshot()["writes"]["zone_leds"]
        self.r.show(self.target(["FF0000", "00FF00", "000000", "FFFFFF"]))
        self.frames(3)
        self.assertEqual(DEV.snapshot()["writes"]["zone_leds"] - before, 1)
        self.assertEqual(zone_colors()[3], "FFFFFF")

    def test_steady_target_writes_nothing(self):
        self.r.show(self.target(["FF0000"] * 4))
        self.frames()
        before = DEV.snapshot()["writes"]
        self.r.show(self.target(["FF0000"] * 4))
        self.frames(5)
        self.assertEqual(DEV.snapshot()["writes"], before)

    def test_fade_blends_over_several_writes(self):
        self.r.show(self.target(["000000"] * 4))
        self.frames()
        self.r.show(self.target(["FF0000", "000000", "000000", "000000"], fade=0.4))
        seen = set()
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            self.r.frame()
            time.sleep(0.05)
            seen.add(DEV.snapshot()["zones"][0]["color"])
        self.assertIn("FF0000", seen)
        self.assertGreater(len(seen), 3)                        # intermediate shades, not a jump
        self.assertEqual(DEV.snapshot()["writes"]["mode"], 0)

    def test_pulse_keeps_the_mode_and_varies_brightness(self):
        self.r.show(self.target(["5B6CFF", "000000", "000000", "000000"], pulse=[True, False, False, False]))
        seen = set()
        for _ in range(6):
            self.r.frame()
            time.sleep(0.25)
            seen.add(DEV.snapshot()["zones"][0]["color"])
        self.assertGreater(len(seen), 2)
        self.assertEqual(DEV.snapshot()["writes"]["mode"], 0)

    def test_alert_effect_is_written_once(self):
        self.r.show(self.target(["FF0000"] * 4, effect="Breathing"))
        self.frames(5)
        snap = DEV.snapshot()
        self.assertEqual(snap["mode"], "Breathing")
        self.assertEqual(snap["writes"]["mode"], 1)
        self.assertEqual(zone_colors(), ["FF0000"] * 4)         # colors rewritten after the blanking mode write
        self.r.show(self.target(["00FF00"] * 4))
        self.frames(3)
        snap = DEV.snapshot()
        self.assertEqual((snap["mode"], snap["writes"]["mode"]), ("Static", 2))
        self.assertEqual(zone_colors(), ["00FF00"] * 4)

    def test_outside_change_is_repaired(self):
        self.r.show(self.target(["FF0000"] * 4))
        self.frames()
        with DEV.lock:
            DEV.colors = ["000000"] * len(DEV.colors)           # e.g. another client or an OpenRGB restart
        self.r.read_at = 0                                   # force the periodic re-read
        self.frames(2)
        self.assertEqual(zone_colors(), ["FF0000"] * 4)


class Targets(unittest.TestCase):
    def setUp(self):
        nl.STATE["zones"] = {"RGB LED 1 Header": "drive_temp", "Addressable Header 1": "network",
                             "Addressable Header 2": "push", "*": "off"}
        nl.STATE["config"] = nl.clean_config({})
        nl.STATE["config"]["brightness"] = 100
        nl.STATE["config"]["scrub"]["scope"] = "all"
        nl._held.clear()
        nl.PUSH.clear()

    def test_mapping(self):
        t, info = nl.build_target(ZONES, {"drive_temp": 30, "network": None}, False, False, False)
        self.assertEqual(t["effect"], "Static")
        self.assertEqual(t["colors"][0], "0050FF")               # 30 °C is the first point
        self.assertIsNone(t["colors"][1])                        # no network reading yet: leave as is
        self.assertEqual(t["colors"][2:], ["000000", "000000"])  # nothing pushed, and off
        self.assertEqual([z["source"] for z in info], ["drive_temp", "network", "push", "off"])

    def test_push_value_and_color(self):
        nl.PUSH["addressable header 2"] = {"value": 100.0, "color": None, "pulse": False, "until": 0}
        t, _ = nl.build_target(ZONES, {}, False, False, False)
        self.assertEqual(t["colors"][2], "FF0000")
        nl.PUSH["*"] = {"value": None, "color": "FF00AA", "pulse": True, "until": 0}
        nl.PUSH["addressable header 2"]["until"] = time.time() - 1      # expired: falls back to "*"
        t, _ = nl.build_target(ZONES, {}, False, False, False)
        self.assertEqual((t["colors"][2], t["pulse"][2]), ("FF00AA", True))

    def test_scrub_pulses_lit_zones(self):
        nl.STATE["zones"]["Addressable Header 3"] = "health"
        t, _ = nl.build_target(ZONES, {"drive_temp": 40}, False, False, True)
        self.assertEqual(t["pulse"], [True, True, False, True])  # nothing pushed: the zone stays dark
        self.assertEqual(t["colors"][3], nl.STATE["config"]["scrub"]["color"])
        nl.STATE["config"]["scrub"]["scope"] = "health"
        t, _ = nl.build_target(ZONES, {"drive_temp": 40}, False, False, True)
        self.assertEqual(t["pulse"], [False, False, False, True])

    def test_alert_takes_every_zone(self):
        t, _ = nl.build_target(ZONES, {"drive_temp": 40}, True, True, False)
        self.assertEqual(t["effect"], "Breathing")
        self.assertEqual(t["colors"], ["FF0000"] * 4)

    def test_quiet_off_is_black_not_a_mode_change(self):
        nl.STATE["config"]["quiet"]["action"] = "off"
        t, _ = nl.build_target(ZONES, {"drive_temp": 40}, False, True, False)
        self.assertEqual((t["effect"], t["colors"]), ("Static", ["000000"] * 4))


class Api(unittest.TestCase):
    """The HTTP API, with the status loop and renderer running."""

    @classmethod
    def setUpClass(cls):
        nl.STATE["config"]["interval"] = 2
        srv = nl.ThreadingHTTPServer(("127.0.0.1", WEB_PORT), nl.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=nl.RENDER.run, daemon=True).start()
        threading.Thread(target=nl.status_loop, daemon=True).start()

    def call(self, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}{path}", headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_for(self, check, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if check():
                return True
            time.sleep(0.2)
        return False

    def test_zones_push_and_simulate(self):
        code, st = self.call("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual([z["name"] for z in st["zones"]], [z[0] for z in fake_openrgb.ZONES])

        code, err = self.call("/api/zones", {"zones": {"RGB LED 1 Header": "bogus"}})
        self.assertEqual(code, 400)
        code, st = self.call("/api/zones", {"zones": {"Addressable Header 1": "push", "*": "off"}})
        self.assertEqual((code, st["status_mode"]), (200, "zones"))

        code, _ = self.call("/api/push", {"zone": "addressable header 1", "color": "#12AB34"})
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: zone_colors() == ["000000", "12AB34", "000000", "000000"]),
                        zone_colors())
        modes_before = DEV.snapshot()["writes"]["mode"]

        code, sim = self.call("/api/simulate", {})
        self.assertEqual((code, sim["seconds"]), (200, nl.SIMULATE_SECONDS))
        self.assertTrue(self.wait_for(lambda: DEV.snapshot()["mode"] == "Breathing"))
        code, live = self.call("/api/live")
        self.assertTrue(live["live"]["alert"] and live["live"]["simulated"])
        self.assertEqual(live["live"]["pools"].get("simulated"), "DEGRADED")
        self.assertTrue(self.wait_for(lambda: zone_colors() == ["FF0000"] * 4))

        self.call("/api/simulate", {"stop": True})
        self.assertTrue(self.wait_for(lambda: DEV.snapshot()["mode"] == "Static"
                                      and zone_colors()[1] == "12AB34"))
        self.assertEqual(DEV.snapshot()["writes"]["mode"] - modes_before, 2)

    def test_status_mode_back_compat(self):
        code, st = self.call("/api/status", {"status_mode": "health"})
        self.assertEqual((code, st["status_mode"], st["zone_sources"]), (200, "zones", {"*": "health"}))
        code, st = self.call("/api/status", {"status_mode": "nonsense"})
        self.assertEqual(code, 400)

    def test_push_token(self):
        nl.PUSH_TOKEN = "s3cret"
        try:
            code, _ = self.call("/api/push", {"value": 5})
            self.assertEqual(code, 401)
            code, _ = self.call("/api/push", {"value": 5}, {"Authorization": "Bearer s3cret"})
            self.assertEqual(code, 200)
        finally:
            nl.PUSH_TOKEN = ""
        self.assertEqual(self.call("/api/push", {"zone": "*"})[0], 400)       # nothing to show

    def test_rejects_non_json(self):
        req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}/api/push", data=b"value=1",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 415)


if __name__ == "__main__":
    unittest.main()
