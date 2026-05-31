from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import requests as http_requests
from sqlalchemy import update as sa_update

from .models import Job, LogEntry, Manifest, db

if TYPE_CHECKING:
    from flask import Flask

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_manifest_sems: dict[str, threading.Semaphore] = {}
_job_flags: dict[str, tuple[threading.Event, threading.Event]] = {}
_flags_lock = threading.Lock()
_app_ref: Flask | None = None
_BACKOFF_CAP = 300


def shutdown_worker() -> None:
    """Stop worker threads. Used in tests to reset global state."""
    global _executor, _app_ref
    if _executor:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
    _app_ref = None
    with _flags_lock:
        _manifest_sems.clear()
        _job_flags.clear()


def init_worker(app: Flask) -> None:
    global _executor, _app_ref
    _app_ref = app
    max_workers = int(app.config.get("MAX_CONCURRENT_GLOBAL", 5))
    _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dl")

    _recover_orphaned_jobs(app)

    t = threading.Thread(target=_retry_scheduler, daemon=True, name="retry-sched")
    t.start()


def _recover_orphaned_jobs(app: Flask) -> None:
    with app.app_context():
        count = Job.query.filter_by(status="running").update(
            {"status": "pending", "next_retry_at": None}
        )
        db.session.commit()
        if count:
            log.info("Recovered %d orphaned jobs → pending", count)


def _retry_scheduler() -> None:
    while True:
        time.sleep(5)
        if _app_ref is None:
            continue
        try:
            with _app_ref.app_context():
                now = datetime.now(UTC)
                jobs = Job.query.filter(
                    Job.status == "pending",
                    Job.next_retry_at.isnot(None),
                    Job.next_retry_at <= now,
                ).all()
                for job in jobs:
                    job.next_retry_at = None
                    db.session.commit()
                    _enqueue(job.id)
        except Exception as exc:
            log.error("Retry scheduler: %s", exc)


# ── public API ────────────────────────────────────────────────────────────────


def submit_manifest(manifest_id: str, app: Flask) -> None:
    with app.app_context():
        manifest = db.session.get(Manifest, manifest_id)
        if not manifest:
            raise ValueError(f"Manifest {manifest_id} not found")

        pending = Job.query.filter_by(manifest_id=manifest_id, status="pending").all()

        download_root = Path(app.config.get("DOWNLOAD_ROOT", "/data"))
        free = shutil.disk_usage(str(download_root)).free
        needed = sum(j.expected_bytes for j in pending if j.expected_bytes)
        if needed and free < int(needed * 1.05):
            raise RuntimeError(
                f"Insufficient disk space: {free / 1e9:.1f} GB free, need ~{needed / 1e9:.1f} GB"
            )

        manifest.status = "running"
        db.session.commit()

        _get_sem(manifest_id, manifest.concurrent)
        for job in pending:
            _enqueue(job.id)


def pause_manifest(manifest_id: str) -> None:
    if _app_ref is None:
        return
    with _app_ref.app_context():
        manifest = db.session.get(Manifest, manifest_id)
        if manifest:
            manifest.status = "paused"
            db.session.commit()
        running = Job.query.filter_by(manifest_id=manifest_id, status="running").all()
        for job in running:
            flags = _get_flags(job.id)
            if flags:
                flags[1].clear()  # block the download thread


def resume_manifest(manifest_id: str) -> None:
    if _app_ref is None:
        return
    with _app_ref.app_context():
        manifest = db.session.get(Manifest, manifest_id)
        if manifest:
            manifest.status = "running"
            db.session.commit()
        # Unblock any paused download threads
        with _flags_lock:
            for job_id, (_, pause_ev) in _job_flags.items():
                pause_ev.set()
        # Submit any still-pending jobs
        pending = Job.query.filter_by(manifest_id=manifest_id, status="pending").all()
        for job in pending:
            _enqueue(job.id)


def cancel_job(job_id: str) -> None:
    # Signal in-flight thread if running
    flags = _get_flags(job_id)
    if flags:
        flags[0].set()  # cancel
        flags[1].set()  # unblock pause so thread can observe cancel
    # Also mark pending jobs skipped directly (job not yet picked up by executor)
    if _app_ref is not None:
        try:
            with _app_ref.app_context():
                job = db.session.get(Job, job_id)
                if job and job.status == "pending":
                    job.status = "skipped"
                    db.session.commit()
        except Exception:
            pass


def retry_job(job_id: str, app: Flask) -> None:
    with app.app_context():
        job = db.session.get(Job, job_id)
        if job and job.status in ("failed", "skipped", "corrupt"):
            job.status = "pending"
            job.next_retry_at = None
            job.last_error = None
            db.session.commit()
            _enqueue(job_id)


def sweep_stale_parts(download_root: Path) -> None:
    for part in download_root.rglob("*.part"):
        try:
            stat = part.stat()
            age_h = (time.time() - stat.st_mtime) / 3600
            log.info(
                "Stale .part: %s (%.1f MB, %.0fh old) – will resume if job exists",
                part,
                stat.st_size / 1e6,
                age_h,
            )
        except OSError:
            pass


# ── internals ────────────────────────────────────────────────────────────────


def _get_sem(manifest_id: str, concurrent: int) -> threading.Semaphore:
    with _flags_lock:
        if manifest_id not in _manifest_sems:
            _manifest_sems[manifest_id] = threading.Semaphore(concurrent)
        return _manifest_sems[manifest_id]


def _get_flags(
    job_id: str,
) -> tuple[threading.Event, threading.Event] | None:
    with _flags_lock:
        return _job_flags.get(job_id)


def _set_flags(job_id: str) -> tuple[threading.Event, threading.Event]:
    cancel = threading.Event()
    pause = threading.Event()
    pause.set()  # not paused initially
    with _flags_lock:
        _job_flags[job_id] = (cancel, pause)
    return cancel, pause


def _clear_flags(job_id: str) -> None:
    with _flags_lock:
        _job_flags.pop(job_id, None)


def _enqueue(job_id: str) -> None:
    if _executor is None:
        log.error("Worker not initialised; cannot enqueue %s", job_id)
        return
    _executor.submit(_run_job, job_id)


def _emit(manifest_id: str, event: dict) -> None:
    if _app_ref is None:
        return
    try:
        bus = _app_ref.extensions.get("event_bus")
        if bus:
            bus.publish(manifest_id, event)
    except Exception as exc:
        log.debug("EventBus: %s", exc)


def _db_log(
    app: Flask,
    level: str,
    message: str,
    job_id: str | None = None,
    manifest_id: str | None = None,
) -> None:
    try:
        entry = LogEntry(level=level, message=message, job_id=job_id, manifest_id=manifest_id)
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        log.debug("DB log failed: %s", exc)


def _backoff(attempt: int, base: int) -> int:
    return min(base * (2 ** max(0, attempt - 1)), _BACKOFF_CAP)


# ── download thread ───────────────────────────────────────────────────────────


def _run_job(job_id: str) -> None:
    if _app_ref is None:
        return

    # Read manifest concurrency before we push ctx (avoid holding ctx while blocked)
    ctx = _app_ref.app_context()
    ctx.push()
    try:
        job = db.session.get(Job, job_id)
        if not job:
            return
        manifest = db.session.get(Manifest, job.manifest_id)
        if not manifest:
            return
        concurrent = manifest.concurrent
        manifest_id = manifest.id
    finally:
        ctx.pop()

    sem = _get_sem(manifest_id, concurrent)
    cancel_flag, pause_event = _set_flags(job_id)

    sem.acquire()
    try:
        ctx = _app_ref.app_context()
        ctx.push()
        try:
            _do_download(job_id, manifest_id, cancel_flag, pause_event)
        finally:
            ctx.pop()
    finally:
        sem.release()
        _clear_flags(job_id)


def _do_download(
    job_id: str,
    manifest_id: str,
    cancel_flag: threading.Event,
    pause_event: threading.Event,
) -> None:
    job = db.session.get(Job, job_id)
    manifest = db.session.get(Manifest, manifest_id)
    if not job or not manifest:
        return

    if cancel_flag.is_set() or job.status == "skipped":
        job.status = "skipped"
        db.session.commit()
        _emit(manifest_id, {"type": "job_skipped", "job_id": job_id})
        return

    # Skip if dest file already exists with correct size.
    # Lets you re-upload a manifest (e.g. with fresh cookies) without
    # re-downloading files already present on disk.
    download_root = Path(_app_ref.config.get("DOWNLOAD_ROOT", "/data"))  # type: ignore[union-attr]
    dest_path = download_root / manifest.dest_root / job.dest
    if dest_path.exists() and dest_path.is_file():
        existing_size = dest_path.stat().st_size
        ok = (job.expected_bytes and abs(existing_size - job.expected_bytes) <= 1) or (
            not job.expected_bytes and existing_size >= (manifest.min_bytes or 0)
        )
        if ok:
            job.status = "done"
            job.bytes_written = existing_size
            job.completed_at = datetime.now(UTC)
            db.session.commit()
            _emit(
                manifest_id,
                {"type": "job_done", "job_id": job_id, "dest": job.dest, "skipped_existing": True},
            )
            return

    job.status = "running"
    job.attempt_count = (job.attempt_count or 0) + 1
    job.started_at = datetime.now(UTC)
    db.session.commit()
    _emit(manifest_id, {"type": "job_started", "job_id": job_id, "url": job.url})

    part_path = dest_path.parent / (dest_path.name + ".part")

    attempt = job.attempt_count
    retries = manifest.retries
    backoff_base = manifest.retry_backoff_sec
    min_bytes = manifest.min_bytes
    expect_magic = manifest.expect_magic or {}
    file_type = job.file_type
    expected_bytes = job.expected_bytes
    url = job.url
    headers = {**(manifest.default_headers or {}), **(job.extra_headers or {})}

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail_or_retry(job, manifest, f"mkdir failed: {exc}", attempt, retries, backoff_base)
        return

    resume_offset = part_path.stat().st_size if part_path.exists() else 0
    if resume_offset:
        headers["Range"] = f"bytes={resume_offset}-"

    error: str | None = None

    try:
        resp = http_requests.get(url, headers=headers, stream=True, timeout=(15, 60))

        if resp.status_code == 416:
            # Part file is already the full size or beyond – just rename
            resume_offset = 0
            headers.pop("Range", None)
            resp = http_requests.get(url, headers=headers, stream=True, timeout=(15, 60))
            part_path.write_bytes(b"")

        if resp.status_code not in (200, 206):
            resp.raise_for_status()

        # 200 with Range header means server ignored Range → start fresh
        if resume_offset and resp.status_code == 200:
            resume_offset = 0
            part_path.write_bytes(b"")

        mode = "ab" if resume_offset else "wb"
        bytes_written = resume_offset

        last_db_write = time.monotonic()
        last_emit = time.monotonic()
        speed_window: list[tuple[float, int]] = []

        with open(part_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue

                if cancel_flag.is_set():
                    error = "cancelled"
                    break

                pause_event.wait()  # blocks when paused

                if cancel_flag.is_set():
                    error = "cancelled"
                    break

                f.write(chunk)
                bytes_written += len(chunk)
                now = time.monotonic()

                speed_window.append((now, bytes_written))
                if len(speed_window) > 8:
                    speed_window = speed_window[-8:]

                if now - last_emit >= 1.0:
                    speed_bps = 0
                    if len(speed_window) >= 2:
                        dt = speed_window[-1][0] - speed_window[0][0]
                        db_bytes = speed_window[-1][1] - speed_window[0][1]
                        if dt > 0:
                            speed_bps = int(db_bytes / dt)
                    eta_sec = 0
                    if speed_bps and expected_bytes:
                        remaining = max(0, expected_bytes - bytes_written)
                        eta_sec = int(remaining / speed_bps)

                    _emit(
                        manifest_id,
                        {
                            "type": "progress",
                            "job_id": job_id,
                            "bytes_written": bytes_written,
                            "total_bytes": expected_bytes,
                            "speed_bps": speed_bps,
                            "eta_sec": eta_sec,
                        },
                    )
                    last_emit = now

                if now - last_db_write >= 2.0:
                    db.session.execute(
                        sa_update(Job).where(Job.id == job_id).values(bytes_written=bytes_written)
                    )
                    db.session.commit()
                    last_db_write = now

            f.flush()
            os.fsync(f.fileno())

        if error == "cancelled":
            part_path.unlink(missing_ok=True)
            j = db.session.get(Job, job_id)
            if j:
                j.status = "skipped"
                db.session.commit()
            _emit(manifest_id, {"type": "job_skipped", "job_id": job_id})
            return

        # ── integrity checks ──────────────────────────────────────────────
        actual_size = part_path.stat().st_size
        if actual_size < min_bytes:
            raise ValueError(f"downloaded file too small: {actual_size} < {min_bytes} bytes")

        if file_type and file_type in expect_magic:
            magic_hex = expect_magic[file_type]
            expected_magic = bytes.fromhex(magic_hex.replace(" ", ""))
            with open(part_path, "rb") as f:
                actual_magic = f.read(len(expected_magic))
            if actual_magic != expected_magic:
                raise ValueError(f"magic mismatch: expected {magic_hex}, got {actual_magic.hex()}")

        os.replace(part_path, dest_path)  # atomic

        j = db.session.get(Job, job_id)
        if j:
            j.status = "done"
            j.completed_at = datetime.now(UTC)
            j.bytes_written = actual_size
            j.last_error = None
            db.session.commit()

        _emit(
            manifest_id,
            {"type": "job_done", "job_id": job_id, "dest": str(job.dest), "bytes": actual_size},
        )
        _check_manifest_complete(manifest_id)

    except Exception as exc:
        error_msg = str(exc)
        log.error("Job %s failed (attempt %d): %s", job_id, attempt, error_msg)
        j = db.session.get(Job, job_id)
        if j:
            _fail_or_retry(j, manifest, error_msg, attempt, retries, backoff_base)


def _fail_or_retry(
    job: Job,
    manifest: Manifest,
    error_msg: str,
    attempt: int,
    retries: int,
    backoff_base: int,
) -> None:
    if attempt < retries:
        delay = _backoff(attempt, backoff_base)
        next_retry = datetime.now(UTC) + timedelta(seconds=delay)
        job.status = "pending"
        job.next_retry_at = next_retry
        job.last_error = error_msg
        db.session.commit()
        _emit(
            job.manifest_id,
            {
                "type": "job_retry_scheduled",
                "job_id": job.id,
                "next_retry_at": next_retry.isoformat(),
                "attempt": attempt,
                "error": error_msg,
            },
        )
    else:
        job.status = "failed"
        job.last_error = error_msg
        db.session.commit()
        _emit(
            job.manifest_id,
            {
                "type": "job_failed",
                "job_id": job.id,
                "error": error_msg,
                "attempt": attempt,
            },
        )
        _check_manifest_complete(job.manifest_id)


def _check_manifest_complete(manifest_id: str) -> None:
    terminal = {"done", "failed", "skipped", "verified", "corrupt"}
    non_terminal = Job.query.filter(
        Job.manifest_id == manifest_id,
        Job.status.notin_(list(terminal)),
    ).count()
    if non_terminal == 0:
        m = db.session.get(Manifest, manifest_id)
        if m and m.status not in ("paused",):
            m.status = "done"
            db.session.commit()
            _emit(manifest_id, {"type": "manifest_done", "manifest_id": manifest_id})
