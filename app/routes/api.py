from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required

from ..manifest import manifest_to_db, parse_manifest, validate_manifest
from ..models import Job, LogEntry, Manifest, db
from ..verify import verify_job
from ..worker import cancel_job, pause_manifest, resume_manifest, retry_job, submit_manifest

log = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__)


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


# ── manifests ─────────────────────────────────────────────────────────────────


@api_bp.route("/api/manifests", methods=["POST"])
@login_required
def upload_manifest():
    raw: str | bytes | None = None

    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("manifest_file")
        if not f:
            return _err("No file provided")
        raw = f.read()
    elif request.is_json:
        raw = request.get_data()
    else:
        raw = request.get_data()

    if not raw:
        return _err("Empty body")

    try:
        data = parse_manifest(raw)
    except Exception as exc:
        return _err(f"Invalid JSON: {exc}")

    errors = validate_manifest(data)
    if errors:
        return _err("; ".join(errors))

    try:
        manifest = manifest_to_db(data, raw if isinstance(raw, str) else raw.decode())
    except Exception as exc:
        log.exception("manifest_to_db failed")
        return _err(str(exc), 500)

    return jsonify(
        {"id": manifest.id, "name": manifest.name, "job_count": manifest.jobs.count()}
    ), 201


@api_bp.route("/api/manifests/<manifest_id>")
@login_required
def get_manifest(manifest_id: str):
    m = Manifest.query.get_or_404(manifest_id)
    return jsonify(m.to_dict())


@api_bp.route("/api/manifests/<manifest_id>/start", methods=["POST"])
@login_required
def start_manifest(manifest_id: str):
    m = Manifest.query.get_or_404(manifest_id)
    if m.status == "running":
        return jsonify({"status": "already running"})

    if m.status == "paused":
        resume_manifest(manifest_id)
        return jsonify({"status": "resumed"})

    try:
        submit_manifest(manifest_id, current_app._get_current_object())
    except RuntimeError as exc:
        return _err(str(exc))
    return jsonify({"status": "started"})


@api_bp.route("/api/manifests/<manifest_id>/pause", methods=["POST"])
@login_required
def pause_manifest_route(manifest_id: str):
    Manifest.query.get_or_404(manifest_id)
    pause_manifest(manifest_id)
    return jsonify({"status": "paused"})


@api_bp.route("/api/manifests/<manifest_id>", methods=["DELETE"])
@login_required
def delete_manifest(manifest_id: str):
    """Delete manifest + all its jobs + log entries. Does NOT touch files on disk."""
    m = Manifest.query.get_or_404(manifest_id)
    # Cancel any running jobs first
    running = Job.query.filter_by(manifest_id=manifest_id, status="running").all()
    for j in running:
        cancel_job(j.id)
    # Delete log entries (no FK cascade configured for these)
    LogEntry.query.filter_by(manifest_id=manifest_id).delete()
    # Job rows cascade-delete via FK
    db.session.delete(m)
    db.session.commit()
    return jsonify({"deleted": manifest_id})


@api_bp.route("/api/manifests/<manifest_id>/headers", methods=["PATCH"])
@login_required
def patch_manifest_headers(manifest_id: str):
    """Merge new defaults.headers into the stored manifest.

    Body: JSON object {"headers": {"Cookie": "...", "Referer": "..."}}.
    Existing keys are overwritten; unspecified keys are kept. Useful for
    rotating session cookies without re-uploading the whole manifest.
    """
    m = Manifest.query.get_or_404(manifest_id)
    body = request.get_json(silent=True) or {}
    new_headers = body.get("headers")
    if not isinstance(new_headers, dict):
        return _err('body must be {"headers": {...}}')
    current = dict(m.default_headers or {})
    current.update(new_headers)
    m.default_headers = current
    db.session.commit()
    return jsonify({"headers": current})


@api_bp.route("/api/manifests/<manifest_id>/retry-failed", methods=["POST"])
@login_required
def retry_failed_jobs(manifest_id: str):
    """Bulk-retry every failed/skipped/corrupt job in this manifest."""
    Manifest.query.get_or_404(manifest_id)
    jobs = Job.query.filter(
        Job.manifest_id == manifest_id,
        Job.status.in_(["failed", "skipped", "corrupt"]),
    ).all()
    app_obj = current_app._get_current_object()
    for j in jobs:
        retry_job(j.id, app_obj)
    return jsonify({"queued": len(jobs)})


@api_bp.route("/api/manifests/<manifest_id>/verify", methods=["POST"])
@login_required
def verify_manifest_route(manifest_id: str):
    m = Manifest.query.get_or_404(manifest_id)
    download_root = Path(current_app.config.get("DOWNLOAD_ROOT", "/data"))
    jobs = Job.query.filter(
        Job.manifest_id == manifest_id,
        Job.status.in_(["done", "verified", "corrupt"]),
    ).all()

    results = {"total": len(jobs), "verified": 0, "corrupt": 0, "missing": 0}
    event_bus = current_app.extensions.get("event_bus")

    for job in jobs:
        result = verify_job(job, m, download_root)
        if result.ok:
            job.status = "verified"
            results["verified"] += 1
        else:
            job.status = "corrupt"
            if "missing" in result.reason:
                results["missing"] += 1
            else:
                results["corrupt"] += 1
            job.last_error = result.reason

        if event_bus:
            event_bus.publish(
                manifest_id,
                {
                    "type": "verify_result",
                    "job_id": job.id,
                    "ok": result.ok,
                    "reason": result.reason,
                    "status": job.status,
                },
            )

    db.session.commit()
    return jsonify(results)


@api_bp.route("/api/manifests/<manifest_id>/jobs")
@login_required
def list_jobs(manifest_id: str):
    Manifest.query.get_or_404(manifest_id)
    status_filter = request.args.get("status")
    page = int(request.args.get("page", 1))
    per_page = min(int(request.args.get("per_page", 100)), 500)

    q = Job.query.filter_by(manifest_id=manifest_id)
    if status_filter:
        q = q.filter_by(status=status_filter)
    q = q.order_by(Job.dest)

    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        {
            "items": [j.to_dict() for j in pagination.items],
            "total": pagination.total,
            "page": page,
            "pages": pagination.pages,
        }
    )


@api_bp.route("/api/manifests/<manifest_id>/logs")
@login_required
def manifest_logs(manifest_id: str):
    Manifest.query.get_or_404(manifest_id)
    limit = min(int(request.args.get("limit", 100)), 1000)
    entries = (
        LogEntry.query.filter_by(manifest_id=manifest_id)
        .order_by(LogEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify([e.to_dict() for e in reversed(entries)])


# ── jobs ──────────────────────────────────────────────────────────────────────


@api_bp.route("/api/jobs/<job_id>/retry", methods=["POST"])
@login_required
def retry_job_route(job_id: str):
    Job.query.get_or_404(job_id)
    retry_job(job_id, current_app._get_current_object())
    return jsonify({"status": "queued"})


@api_bp.route("/api/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def cancel_job_route(job_id: str):
    Job.query.get_or_404(job_id)
    cancel_job(job_id)
    return jsonify({"status": "cancelled"})


@api_bp.route("/api/jobs/<job_id>/logs")
@login_required
def job_logs(job_id: str):
    Job.query.get_or_404(job_id)
    limit = min(int(request.args.get("limit", 100)), 500)
    entries = (
        LogEntry.query.filter_by(job_id=job_id)
        .order_by(LogEntry.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify([e.to_dict() for e in reversed(entries)])


# ── health ────────────────────────────────────────────────────────────────────


@api_bp.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})
