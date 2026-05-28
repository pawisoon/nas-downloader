# nas-downloader

A self-hostable web app that ingests a JSON **manifest** describing files to download and a target folder structure, then downloads everything to a mounted volume with a queue, retries, live progress, and a final integrity check.

Built for Synology DSM 7 NAS deployment via Portainer, but runs anywhere Docker runs.

![Screenshot placeholder](docs/screenshot.png)

---

## Features

- Upload a manifest JSON → one download job per file, all queued automatically
- Streaming HTTP downloads with `Range`-based resume from `.part` files
- Per-manifest concurrency limit + global cap (`MAX_CONCURRENT_GLOBAL`)
- Exponential-backoff retries (configurable per manifest)
- Live progress via Server-Sent Events — no page refresh needed
- Pause / resume / cancel per job or entire manifest
- Post-download integrity check: file size ±1 byte + magic-byte prefix
- Single-user auth (argon2 hash, auto-generated password on first boot)
- Dark mode UI by default; Tailwind + Alpine.js, no build step for development
- Multi-arch Docker image (`linux/amd64`, `linux/arm64`, `linux/arm/v7`)

---

## Run on Synology via Portainer

### 1 — Create the directories on your NAS

SSH into your NAS and run:

```bash
mkdir -p /volume1/downloads
mkdir -p /volume1/docker/nas-downloader/state
```

### 2 — Add a new stack in Portainer

Paste the following compose snippet in **Stacks → Add stack → Web editor**:

```yaml
services:
  nas-downloader:
    image: ghcr.io/pawisoon/nas-downloader:latest
    container_name: nas-downloader
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      USERNAME: admin
      PASSWORD_HASH: ""     # auto-generated on first start if empty
      TZ: Europe/Warsaw
      MAX_CONCURRENT_GLOBAL: "4"
    volumes:
      - /volume1/downloads:/data
      - /volume1/docker/nas-downloader/state:/state
```

Click **Deploy the stack**.

### 3 — Get your initial password

```bash
docker logs nas-downloader 2>&1 | grep "INITIAL PASSWORD"
```

Or read it from:

```bash
cat /volume1/docker/nas-downloader/state/initial_password.txt
```

The file is deleted automatically after your first successful login.

### 4 — Open the UI

Navigate to `http://<nas-ip>:8080` and sign in.

---

## Manifest schema

```json
{
  "name": "Psychologia I rok 2025/2026",
  "destRoot": "Psychologia - studia licencjackie I rok akademicki",
  "defaults": {
    "headers": {
      "Referer": "https://example.com/",
      "User-Agent": "Mozilla/5.0 ..."
    },
    "retries": 5,
    "retryBackoffSec": 10,
    "concurrent": 3,
    "minBytes": 100000,
    "expectMagic": {
      "webm": "1A 45 DF A3",
      "pdf": "25 50 44 46"
    }
  },
  "files": [
    {
      "id": "unique-stable-id",
      "url": "https://example.com/lecture.webm",
      "dest": "Wstęp/Komunikacja werbalna/1.1 Komunikacja.webm",
      "type": "webm",
      "headers": { "Referer": "https://example.com/custom" },
      "expectedBytes": 3117220232
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Human-readable manifest name |
| `destRoot` | yes | Root folder under `/data` for all files |
| `defaults.headers` | no | HTTP headers merged into every request |
| `defaults.retries` | no | Max retry attempts per file (default 5) |
| `defaults.retryBackoffSec` | no | Base for exponential backoff in seconds (default 10) |
| `defaults.concurrent` | no | Max simultaneous downloads for this manifest (default 3) |
| `defaults.minBytes` | no | Reject files smaller than this (default 100 000) |
| `defaults.expectMagic` | no | Map of `type → hex magic bytes` for integrity check |
| `files[].id` | yes | Stable ID — used for dedup/resume across runs |
| `files[].url` | yes | HTTP or HTTPS URL |
| `files[].dest` | yes | Relative path under `destRoot` (UTF-8 / NFC) |
| `files[].type` | no | File type key into `expectMagic` |
| `files[].headers` | no | Per-file headers merged over defaults |
| `files[].expectedBytes` | no | Expected file size; checked post-download ±1 byte |

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USERNAME` | `admin` | Login username |
| `PASSWORD_HASH` | *(auto)* | argon2 hash of password. Auto-generated if empty. |
| `TZ` | UTC | Container timezone |
| `MAX_CONCURRENT_GLOBAL` | `4` | Hard cap on simultaneous downloads across all manifests |
| `STATE_DIR` | `/state` | SQLite DB, logs, secret key |
| `DOWNLOAD_ROOT` | `/data` | Download destination root |

To generate a password hash manually:

```bash
python3 -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('yourpassword'))"
```

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/manifests` | Upload manifest JSON (multipart or raw JSON body) |
| `GET` | `/api/manifests/{id}` | Manifest metadata + job counts |
| `POST` | `/api/manifests/{id}/start` | Start or resume downloads |
| `POST` | `/api/manifests/{id}/pause` | Pause all running jobs |
| `POST` | `/api/manifests/{id}/verify` | Run integrity check on completed files |
| `GET` | `/api/manifests/{id}/jobs` | Paged job list (`?status=`, `?page=`, `?per_page=`) |
| `POST` | `/api/jobs/{id}/retry` | Re-queue a failed/corrupt job |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a pending/running job |
| `GET` | `/api/events?manifest={id}` | SSE stream of live job events |
| `GET` | `/healthz` | Health check (no auth required) |

---

## Development

```bash
git clone https://github.com/pawisoon/nas-downloader
cd nas-downloader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Run with auto-generated password (printed to terminal)
STATE_DIR=./state DOWNLOAD_ROOT=./data flask --app app.wsgi:application run --debug
```

Run tests:

```bash
pytest tests/ -v
```

Lint:

```bash
ruff check app/ tests/
ruff format app/ tests/
```

---

## Security

> **Warning:** nas-downloader is a single-user tool designed for use on a trusted local network. Do **not** expose port 8080 directly to the public internet. Always place it behind a reverse proxy (e.g. Nginx Proxy Manager on your NAS) with TLS if remote access is needed.

---

## Limitations

- HTTP/HTTPS downloads only (no FTP, SFTP, S3, torrent)
- Single instance per container — no clustering or distributed queue
- No multi-user support; authentication is single-user only
- Requires the source server to support `Range` requests for resume (gracefully falls back to restart if not supported)
