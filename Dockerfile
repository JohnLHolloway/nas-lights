# NAS Lights web UI + status-light engine.
# Standard-library Python only; talks to an OpenRGB SDK server over the network.
FROM python:3.13-slim

LABEL org.opencontainers.image.title="NAS Lights" \
      org.opencontainers.image.description="Web UI and status lights for OpenRGB on a NAS: drive temp, CPU temp, network activity and ZFS health as colors." \
      org.opencontainers.image.source="https://github.com/JohnLHolloway/nas-lights" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NASL_DATA=/data

WORKDIR /app
COPY app/ /app/

# Run as an unprivileged user. Reading /sys and /proc needs no privileges.
RUN useradd --system --uid 1000 --home /data nasl \
 && mkdir -p /data && chown nasl /data
USER nasl
VOLUME ["/data"]

EXPOSE 6780
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('NASL_PORT','6780'), timeout=4)"

ENTRYPOINT ["python", "/app/nas_lights.py"]
