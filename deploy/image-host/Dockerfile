# The excel-codex image host (src/excel_codex_bridge/image_host.py).
# Built from the repository root:  docker build -f deploy/image-host/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[host]" && cd / && rm -rf /build

RUN useradd --system --uid 10001 --home-dir /data imagehost \
    && mkdir /data && chown imagehost /data
USER imagehost
WORKDIR /data

# Only a reverse proxy on this machine reaches the port (it is published on
# 127.0.0.1), and it sets X-Forwarded-For to the real client, which the
# per-address quotas go by.
ENV IMAGE_HOST_DIR=/data \
    IMAGE_HOST_LISTEN=0.0.0.0:8080 \
    FORWARDED_ALLOW_IPS=*
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
CMD ["python", "-m", "excel_codex_bridge.image_host"]
