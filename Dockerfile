# syntax=docker/dockerfile:1

# ── Stage 1: CSS build (BUILDPLATFORM, not target arch) ──────────────────────
FROM --platform=$BUILDPLATFORM node:20-slim AS css-builder
WORKDIR /css
COPY tailwind.config.js tailwind.src.css ./
COPY app/templates ./app/templates/
RUN npm init -y \
 && npm install -D tailwindcss@3 \
 && npx tailwindcss -i tailwind.src.css -o tailwind.min.css --minify

# ── Stage 2: Runtime (single stage, runs as root like chomik) ────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY --from=css-builder /css/tailwind.min.css /app/app/static/tailwind.min.css

RUN mkdir -p /state /data

ENV STATE_DIR=/state \
    DOWNLOAD_ROOT=/data \
    USERNAME=admin \
    MAX_CONCURRENT_GLOBAL=4 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"

CMD ["python", "-m", "app.wsgi"]
