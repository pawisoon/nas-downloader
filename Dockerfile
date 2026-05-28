# syntax=docker/dockerfile:1

# ── Stage 1: CSS build (runs on BUILD host, not target arch) ─────────────────
FROM --platform=$BUILDPLATFORM node:20-slim AS css-builder
WORKDIR /css
COPY tailwind.config.js tailwind.src.css ./
COPY app/templates ./app/templates/
RUN npm init -y \
 && npm install -D tailwindcss@3 \
 && npx tailwindcss -i tailwind.src.css -o tailwind.min.css --minify

# ── Stage 2: Python deps (target arch for compiled extensions) ────────────────
FROM python:3.12-slim AS python-builder
WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 3: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# gosu lets the entrypoint drop privileges from root → app cleanly
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 app \
 && useradd -u 1000 -g app -s /bin/sh -m app

COPY --from=python-builder /venv /venv
COPY app/ /app/app/
COPY --from=css-builder /css/tailwind.min.css /app/app/static/tailwind.min.css
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

WORKDIR /app

RUN mkdir -p /state /data \
 && chown -R app:app /state /data /app

# NOTE: no `USER app` here — entrypoint starts as root so it can remap
# the app user to host PUID/PGID, then `gosu app` drops privileges.

ENV STATE_DIR=/state \
    DOWNLOAD_ROOT=/data \
    USERNAME=admin \
    MAX_CONCURRENT_GLOBAL=4

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD /venv/bin/python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/venv/bin/gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "app.wsgi:application"]
