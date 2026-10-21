# Changelog

## 0.3.0 (unreleased)

Each lighting zone can show its own reading.

- Per-zone status lights. Each OpenRGB zone gets its own reading, or is off. Settings from 0.2 carry over: the single reading becomes the reading for every zone.
- Zone colors are sent with UPDATEZONELEDS, only for zones whose color changed. The effect is still written only when it changes.
- New readings: disk throughput (`/proc/diskstats`) and pushed values (`POST /api/push`, for Home Assistant and scripts).
- Scrubs and resilvers show as a slow pulse. They're read from a `zpool status` file that a host cron job refreshes.
- **Simulate pool problem** runs the real alert for 15 seconds (`POST /api/simulate`).
- Quiet hours "off" fades the zones to black instead of switching effects.
- `?theme=light` or `?theme=dark` picks a theme.
- `tools/fake_openrgb.py` and `tools/fake_host.py` let you develop without NAS hardware.
- Prebuilt images on GitHub Container Registry: `ghcr.io/johnlholloway/nas-lights` (amd64, arm64) and `ghcr.io/johnlholloway/nas-lights-openrgb` (amd64). The compose file uses them by default; building locally is still an option.
- TrueNAS SCALE setup is now paste-the-YAML, with no local build.
- Test suite (pytest, with a fake OpenRGB SDK server) and CI on every push and pull request.

## 0.2.0 (2026-10-20)

Status colors no longer flash on every update.

- The effect is only written to the controller when it changes. Some controllers, ASRock Polychrome among them, blank the LEDs briefly on every mode write, which showed up as a flash on each color change.
- Color changes fade over an adjustable time (default 1.5 s).
- Readings use a time-based moving average with Fast, Normal and Slow response, so short traffic bursts no longer swing the color.
- A hysteresis band stops a reading near a step boundary from bouncing between two colors.
- `GET /api/live` reports write counters for diagnosing flicker.

## 0.1.0 (2026-10-20)

First release.

- Web UI and status-light engine for OpenRGB on a NAS. Colors the case by hottest drive temperature, CPU temperature, network throughput or ZFS pool health.
- Editable color scales, a pool-problem alert, and quiet hours.
- Manual effects that show only what the hardware supports: color, speed, direction, and software brightness.
- Standard-library OpenRGB SDK client, an optional headless OpenRGB image, and a compose example.
