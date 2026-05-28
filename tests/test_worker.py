from __future__ import annotations

import time
import uuid

import pytest

from app.models import Job, Manifest, db
from app.worker import _fail_or_retry, init_worker, shutdown_worker, submit_manifest

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_worker():
    """Ensure worker globals are reset between tests."""
    yield
    shutdown_worker()


@pytest.fixture()
def worker_app(app):
    """App with worker initialised."""
    init_worker(app)
    return app


def _make_manifest(
    app,
    httpserver,
    *,
    files=None,
    retries=1,
    min_bytes=4,
    concurrent=3,
    expect_magic=None,
    retry_backoff_sec=0,
):
    with app.app_context():
        m = Manifest(
            id=str(uuid.uuid4()),
            name="Test",
            dest_root="Downloads",
            raw_json="{}",
            concurrent=concurrent,
            retries=retries,
            retry_backoff_sec=retry_backoff_sec,
            min_bytes=min_bytes,
            expect_magic=expect_magic or {},
            default_headers={},
            status="pending",
        )
        db.session.add(m)
        db.session.flush()

        if files is None:
            files = [
                {
                    "url": httpserver.url_for("/file.bin"),
                    "dest": "folder/file.bin",
                    "type": None,
                    "expected_bytes": 0,
                }
            ]

        jobs = []
        for f in files:
            j = Job(
                id=str(uuid.uuid4()),
                manifest_id=m.id,
                file_id=f.get("file_id", str(uuid.uuid4())),
                url=f["url"],
                dest=f["dest"],
                file_type=f.get("type"),
                extra_headers={},
                expected_bytes=f.get("expected_bytes", 0),
                status="pending",
            )
            db.session.add(j)
            jobs.append(j.id)

        db.session.commit()
        return m.id, jobs


def _wait_status(app, job_id, expected, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with app.app_context():
            j = db.session.get(Job, job_id)
            if j and j.status == expected:
                return j
        time.sleep(0.05)
    with app.app_context():
        j = db.session.get(Job, job_id)
        actual = j.status if j else "missing"
    raise TimeoutError(f"Job {job_id} status={actual!r}, want {expected!r}")


# ── successful download ────────────────────────────────────────────────────────


def test_successful_download(worker_app, data_dir, httpserver):
    content = b"hello world download test"
    httpserver.expect_request("/file.bin").respond_with_data(
        content, status=200, content_type="application/octet-stream"
    )

    manifest_id, [job_id] = _make_manifest(worker_app, httpserver, min_bytes=1)
    submit_manifest(manifest_id, worker_app)

    job = _wait_status(worker_app, job_id, "done")
    assert job.bytes_written == len(content)
    assert (data_dir / "Downloads" / "folder" / "file.bin").exists()
    assert (data_dir / "Downloads" / "folder" / "file.bin").read_bytes() == content


def test_part_file_removed_on_success(worker_app, data_dir, httpserver):
    content = b"data" * 20
    httpserver.expect_request("/file.bin").respond_with_data(content, status=200)
    manifest_id, [job_id] = _make_manifest(worker_app, httpserver, min_bytes=1)
    submit_manifest(manifest_id, worker_app)
    _wait_status(worker_app, job_id, "done")
    assert not (data_dir / "Downloads" / "folder" / "file.bin.part").exists()


# ── Range-based resume ────────────────────────────────────────────────────────


def test_range_resume(worker_app, data_dir, httpserver):
    first_part = b"ABCDE"
    second_part = b"FGHIJ"
    full_content = first_part + second_part

    # Write a .part file simulating partial download
    part_path = data_dir / "Downloads" / "folder" / "file.bin.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(first_part)

    # Server returns 206 for Range request
    def handler(req):
        from werkzeug.wrappers import Response as WResponse

        if "Range" in req.headers:
            return WResponse(second_part, status=206, content_type="application/octet-stream")
        return WResponse(full_content, status=200, content_type="application/octet-stream")

    httpserver.expect_request("/file.bin").respond_with_handler(handler)

    manifest_id, [job_id] = _make_manifest(
        worker_app,
        httpserver,
        files=[
            {
                "url": httpserver.url_for("/file.bin"),
                "dest": "folder/file.bin",
                "expected_bytes": len(full_content),
            }
        ],
        min_bytes=1,
    )
    submit_manifest(manifest_id, worker_app)
    _wait_status(worker_app, job_id, "done")

    result = (data_dir / "Downloads" / "folder" / "file.bin").read_bytes()
    assert result == full_content


def test_server_ignores_range_restarts(worker_app, data_dir, httpserver):
    full_content = b"FULL" * 10
    first_part = b"FULL"  # partial

    part_path = data_dir / "Downloads" / "folder" / "file.bin.part"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(first_part)

    # Server always returns 200 (ignores Range)
    httpserver.expect_request("/file.bin").respond_with_data(full_content, status=200)

    manifest_id, [job_id] = _make_manifest(worker_app, httpserver, min_bytes=1)
    submit_manifest(manifest_id, worker_app)
    _wait_status(worker_app, job_id, "done")

    result = (data_dir / "Downloads" / "folder" / "file.bin").read_bytes()
    assert result == full_content


# ── magic byte check ──────────────────────────────────────────────────────────


def test_magic_mismatch_fails_job(worker_app, httpserver):
    bad_content = b"\x00\x00\x00\x00" + b"x" * 20
    httpserver.expect_request("/vid.webm").respond_with_data(bad_content, status=200)

    manifest_id, [job_id] = _make_manifest(
        worker_app,
        httpserver,
        files=[
            {
                "url": httpserver.url_for("/vid.webm"),
                "dest": "folder/vid.webm",
                "type": "webm",
                "expected_bytes": 0,
            }
        ],
        min_bytes=1,
        retries=1,
        expect_magic={"webm": "1A 45 DF A3"},
    )
    submit_manifest(manifest_id, worker_app)
    job = _wait_status(worker_app, job_id, "failed", timeout=10)
    assert "magic" in (job.last_error or "").lower()


def test_correct_magic_succeeds(worker_app, httpserver):
    content = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"x" * 20
    httpserver.expect_request("/vid.webm").respond_with_data(content, status=200)

    manifest_id, [job_id] = _make_manifest(
        worker_app,
        httpserver,
        files=[
            {
                "url": httpserver.url_for("/vid.webm"),
                "dest": "folder/vid.webm",
                "type": "webm",
                "expected_bytes": 0,
            }
        ],
        min_bytes=1,
        expect_magic={"webm": "1A 45 DF A3"},
    )
    submit_manifest(manifest_id, worker_app)
    _wait_status(worker_app, job_id, "done")


# ── min_bytes check ───────────────────────────────────────────────────────────


def test_min_bytes_too_small_fails(worker_app, httpserver):
    httpserver.expect_request("/tiny.bin").respond_with_data(b"hi", status=200)

    manifest_id, [job_id] = _make_manifest(
        worker_app,
        httpserver,
        files=[
            {"url": httpserver.url_for("/tiny.bin"), "dest": "folder/tiny.bin", "expected_bytes": 0}
        ],
        min_bytes=1000,  # 2 bytes downloaded, 1000 required
        retries=1,
    )
    submit_manifest(manifest_id, worker_app)
    job = _wait_status(worker_app, job_id, "failed", timeout=10)
    assert job.last_error


# ── cancel ────────────────────────────────────────────────────────────────────


def test_cancel_before_start(worker_app, httpserver, data_dir):
    content = b"x" * 500
    httpserver.expect_request("/slow.bin").respond_with_data(content, status=200)

    manifest_id, [job_id] = _make_manifest(
        worker_app,
        httpserver,
        files=[
            {"url": httpserver.url_for("/slow.bin"), "dest": "folder/slow.bin", "expected_bytes": 0}
        ],
        min_bytes=1,
    )

    from app.worker import cancel_job

    cancel_job(job_id)
    submit_manifest(manifest_id, worker_app)

    # Job should end as skipped (cancel flag set before run)
    _wait_status(worker_app, job_id, "skipped", timeout=8)
    assert not (data_dir / "Downloads" / "folder" / "slow.bin").exists()


# ── retry unit tests ──────────────────────────────────────────────────────────


def test_fail_or_retry_schedules_retry(app):
    with app.app_context():
        m = Manifest(
            id=str(uuid.uuid4()),
            name="T",
            dest_root="D",
            raw_json="{}",
            concurrent=1,
            retries=3,
            retry_backoff_sec=60,
            min_bytes=1,
            expect_magic={},
            default_headers={},
            status="running",
        )
        db.session.add(m)
        db.session.flush()
        j = Job(
            id=str(uuid.uuid4()),
            manifest_id=m.id,
            file_id="f1",
            url="http://x",
            dest="a.bin",
            file_type=None,
            extra_headers={},
            expected_bytes=0,
            status="running",
            attempt_count=1,
        )
        db.session.add(j)
        db.session.commit()

        _fail_or_retry(j, m, "connection reset", attempt=1, retries=3, backoff_base=60)

        db.session.refresh(j)
        assert j.status == "pending"
        assert j.next_retry_at is not None
        assert j.last_error == "connection reset"


def test_fail_or_retry_exhausted_marks_failed(app):
    with app.app_context():
        m = Manifest(
            id=str(uuid.uuid4()),
            name="T",
            dest_root="D",
            raw_json="{}",
            concurrent=1,
            retries=3,
            retry_backoff_sec=60,
            min_bytes=1,
            expect_magic={},
            default_headers={},
            status="running",
        )
        db.session.add(m)
        db.session.flush()
        j = Job(
            id=str(uuid.uuid4()),
            manifest_id=m.id,
            file_id="f1",
            url="http://x",
            dest="a.bin",
            file_type=None,
            extra_headers={},
            expected_bytes=0,
            status="running",
            attempt_count=3,
        )
        db.session.add(j)
        db.session.commit()

        _fail_or_retry(j, m, "timeout", attempt=3, retries=3, backoff_base=60)

        db.session.refresh(j)
        assert j.status == "failed"
        assert j.last_error == "timeout"


# ── concurrency semaphore ─────────────────────────────────────────────────────


def test_concurrency_semaphore_created(worker_app, httpserver):
    from app.worker import _get_sem

    content = b"ok" * 10
    httpserver.expect_request("/file.bin").respond_with_data(content, status=200)

    manifest_id, [job_id] = _make_manifest(worker_app, httpserver, concurrent=2, min_bytes=1)
    submit_manifest(manifest_id, worker_app)
    _wait_status(worker_app, job_id, "done")

    # After submit, semaphore for this manifest should exist with count=2
    with worker_app.app_context():
        m = db.session.get(Manifest, manifest_id)
    sem = _get_sem(manifest_id, m.concurrent)
    # Semaphore internal count should be <= concurrent (released after download)
    assert sem._value <= m.concurrent
