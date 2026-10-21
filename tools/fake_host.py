#!/usr/bin/env python3
"""Fake NAS readings, for developing NAS Lights on a machine that isn't a NAS.

Writes a small /sys, /proc and ZFS kstat tree under DIR and keeps it moving:
drive and CPU temperatures drift, and the NIC and disk counters grow.

    python tools/fake_host.py /tmp/fakehost            # runs until stopped
    HOST_SYS=/tmp/fakehost/sys HOST_PROC=/tmp/fakehost/proc \\
    ZFS_KSTAT=/tmp/fakehost/kstat ZPOOL_STATUS_FILE=/tmp/fakehost/zpool-status.txt \\
    OPENRGB_PORT=6742 NASL_DATA=/tmp/fakehost/data python app/nas_lights.py

Options: --scrub writes a `zpool status` showing a scrub in progress;
--degraded marks the pool DEGRADED.
"""
import argparse
import math
import os
import time


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                  # Windows: the file is open in a reader right now
            time.sleep(0.01)


ZPOOL = """  pool: tank
 state: {state}
  scan: {scan}
config:

\tNAME        STATE     READ WRITE CKSUM
\ttank        {state}     0     0     0
\t  mirror-0  {state}     0     0     0
\t    sda     ONLINE       0     0     0
\t    sdb     ONLINE       0     0     0
\t    sdc     ONLINE       0     0     0

errors: No known data errors
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--scrub", action="store_true")
    ap.add_argument("--degraded", action="store_true")
    a = ap.parse_args()
    d = a.dir
    hw = os.path.join(d, "sys/class/hwmon")
    write(f"{hw}/hwmon0/name", "k10temp\n")
    write(f"{hw}/hwmon0/temp1_label", "Tctl\n")
    for i in range(3):
        write(f"{hw}/hwmon{i + 1}/name", "drivetemp\n")
    net = os.path.join(d, "sys/class/net/enp8s0")
    write(f"{net}/operstate", "up\n")
    for n in ("sda", "sdb", "sdc", "nvme0n1"):
        os.makedirs(os.path.join(d, "sys/block", n), exist_ok=True)
    rx = tx = 10 ** 9
    sectors = [10 ** 7] * 4
    t0 = time.time()
    print(f"fake host readings in {d}", flush=True)
    while True:
        t = time.time() - t0
        write(f"{hw}/hwmon0/temp1_input", f"{int((52 + 6 * math.sin(t / 40)) * 1000)}\n")
        for i in range(3):
            write(f"{hw}/hwmon{i + 1}/temp1_input", f"{int((39 + i * 2 + 2 * math.sin(t / 90 + i)) * 1000)}\n")
        mbps = 40 + 35 * math.sin(t / 25)                     # 5 .. 75 Mbit/s
        rx += int(mbps * 1e6 / 8 * 0.7)
        tx += int(mbps * 1e6 / 8 * 0.3)
        write(f"{net}/statistics/rx_bytes", f"{rx}\n")
        write(f"{net}/statistics/tx_bytes", f"{tx}\n")
        mbs = (180 if a.scrub else 25) + 20 * math.sin(t / 15)  # MB/s across the pool disks
        lines = []
        for k, n in enumerate(("sda", "sdb", "sdc", "nvme0n1")):
            share = 0.31 if k < 3 else 0.07
            sectors[k] += int(mbs * 1e6 * share / 512)
            lines.append(f"   8       {k * 16} {n} 1000 0 {sectors[k]} 0 1000 0 {sectors[k] // 4} 0 0 0 0 0 0 0 0")
            lines.append(f"   8       {k * 16 + 1} {n}{'p1' if n.startswith('nvme') else '1'} 10 0 100 0 10 0 100 0 0 0 0 0 0 0 0")
        write(os.path.join(d, "proc/diskstats"), "\n".join(lines) + "\n")
        state = "DEGRADED" if a.degraded else "ONLINE"
        write(os.path.join(d, "kstat/tank/state"), state + "\n")
        pct = (t / 6) % 100
        scan = (f"scrub in progress since Tue Oct 20 16:26:23 2026\n\t5.4T / 12.6T scanned\n\t0B repaired, {pct:.2f}% done"
                if a.scrub else "scrub repaired 0B in 07:12:01 with 0 errors on Sun Oct 18 07:12:01 2026")
        write(os.path.join(d, "zpool-status.txt"), ZPOOL.format(state=state, scan=scan))
        time.sleep(1)


if __name__ == "__main__":
    main()
