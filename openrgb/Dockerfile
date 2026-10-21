# Headless OpenRGB SDK server, built from the official OpenRGB AppImage.
# Optional: use this if your NAS has no OpenRGB yet. Any OpenRGB server works.
#
# The official .deb packages need a hidapi build that Debian does not ship,
# and the AppImage needs glibc 2.39+, hence Debian 13 (trixie).
FROM debian:trixie-slim

ARG OPENRGB_VERSION=1.1
ARG OPENRGB_APPIMAGE=OpenRGB_1.1_x86_64_8e16de2.AppImage
ARG OPENRGB_URL=https://codeberg.org/OpenRGB/OpenRGB/releases/download/release_${OPENRGB_VERSION}/${OPENRGB_APPIMAGE}

LABEL org.opencontainers.image.title="OpenRGB server (headless)" \
      org.opencontainers.image.description="OpenRGB ${OPENRGB_VERSION} SDK server for NAS Lights" \
      org.opencontainers.image.source="https://github.com/JohnLHolloway/nas-lights"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      libglib2.0-0t64 libfontconfig1 libfreetype6 libharfbuzz0b libdbus-1-3 \
      libgl1 libegl1 libopengl0 libglx0 libxkbcommon0 libusb-1.0-0 libudev1 \
 && curl -fsSL -o /tmp/openrgb.AppImage "$OPENRGB_URL" \
 && chmod +x /tmp/openrgb.AppImage \
 && cd /opt && /tmp/openrgb.AppImage --appimage-extract >/dev/null \
 && mv /opt/squashfs-root /opt/openrgb \
 && rm -f /tmp/openrgb.AppImage \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

ENV QT_QPA_PLATFORM=offscreen
EXPOSE 6742

# --server-host 0.0.0.0 is required inside a container (the default is localhost only).
ENTRYPOINT ["/opt/openrgb/AppRun"]
CMD ["--server", "--server-host", "0.0.0.0", "--server-port", "6742"]
