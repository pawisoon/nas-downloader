from __future__ import annotations

from datetime import UTC, datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _now() -> datetime:
    return datetime.now(UTC)


class Manifest(db.Model):
    __tablename__ = "manifest"

    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    dest_root = db.Column(db.String(1024), nullable=False)
    raw_json = db.Column(db.Text, nullable=False)
    concurrent = db.Column(db.Integer, default=3, nullable=False)
    retries = db.Column(db.Integer, default=5, nullable=False)
    retry_backoff_sec = db.Column(db.Integer, default=10, nullable=False)
    min_bytes = db.Column(db.Integer, default=100_000, nullable=False)
    expect_magic = db.Column(db.JSON, default=dict, nullable=False)
    default_headers = db.Column(db.JSON, default=dict, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    jobs = db.relationship("Job", backref="manifest", lazy="dynamic", cascade="all, delete-orphan")
    log_entries = db.relationship(
        "LogEntry",
        backref="manifest_rel",
        lazy="dynamic",
        foreign_keys="LogEntry.manifest_id",
        cascade="all, delete-orphan",
    )

    def job_counts(self) -> dict[str, int]:
        from sqlalchemy import func

        rows = (
            db.session.query(Job.status, func.count(Job.id))
            .filter(Job.manifest_id == self.id)
            .group_by(Job.status)
            .all()
        )
        return {status: count for status, count in rows}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "dest_root": self.dest_root,
            "concurrent": self.concurrent,
            "retries": self.retries,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "job_counts": self.job_counts(),
        }


class Job(db.Model):
    __tablename__ = "job"
    __table_args__ = (
        db.Index("ix_job_manifest_status", "manifest_id", "status"),
        db.Index("ix_job_next_retry", "next_retry_at"),
    )

    id = db.Column(db.String(36), primary_key=True)
    manifest_id = db.Column(
        db.String(36),
        db.ForeignKey("manifest.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id = db.Column(db.String(64), nullable=False)
    url = db.Column(db.String(2048), nullable=False)
    dest = db.Column(db.String(1024), nullable=False)
    file_type = db.Column(db.String(20), nullable=True)
    extra_headers = db.Column(db.JSON, default=dict, nullable=False)
    expected_bytes = db.Column(db.BigInteger, default=0, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text, nullable=True)
    next_retry_at = db.Column(db.DateTime(timezone=True), nullable=True)
    bytes_written = db.Column(db.BigInteger, default=0, nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    log_entries = db.relationship(
        "LogEntry",
        backref="job_rel",
        lazy="dynamic",
        foreign_keys="LogEntry.job_id",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        folder = "/".join(self.dest.replace("\\", "/").split("/")[:-1]) or "/"
        return {
            "id": self.id,
            "manifest_id": self.manifest_id,
            "file_id": self.file_id,
            "url": self.url,
            "dest": self.dest,
            "folder": folder,
            "file_type": self.file_type,
            "expected_bytes": self.expected_bytes,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "bytes_written": self.bytes_written,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class LogEntry(db.Model):
    __tablename__ = "log_entry"
    __table_args__ = (
        db.Index("ix_log_job_time", "job_id", "created_at"),
        db.Index("ix_log_manifest_time", "manifest_id", "created_at"),
        db.Index("ix_log_time", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    job_id = db.Column(
        db.String(36),
        db.ForeignKey("job.id", ondelete="SET NULL"),
        nullable=True,
    )
    manifest_id = db.Column(
        db.String(36),
        db.ForeignKey("manifest.id", ondelete="SET NULL"),
        nullable=True,
    )
    level = db.Column(db.String(10), default="INFO", nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "manifest_id": self.manifest_id,
            "level": self.level,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
