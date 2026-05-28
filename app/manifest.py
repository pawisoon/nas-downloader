from __future__ import annotations

import json
import os
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any

from .models import Job, Manifest, db


def sanitize_dest(dest: str) -> str:
    """Validate and NFC-normalize a relative destination path."""
    if "\x00" in dest:
        raise ValueError("NUL byte in dest path")
    if os.path.isabs(dest):
        raise ValueError("dest must be a relative path, not absolute")
    # Reject Windows-style absolute paths (C:\...)
    if len(dest) >= 2 and dest[1] == ":" and dest[0].isalpha():
        raise ValueError("dest must be a relative path, not absolute")
    parts = PurePosixPath(dest).parts
    if ".." in parts:
        raise ValueError("path traversal (..) not allowed in dest")
    return unicodedata.normalize("NFC", dest)


def validate_manifest(data: dict) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    for field in ("name", "destRoot", "files"):
        if field not in data:
            errors.append(f"missing required field: {field}")
    if errors:
        return errors

    if not isinstance(data["files"], list):
        errors.append("files must be an array")
        return errors

    seen_ids: set[str] = set()
    for i, f in enumerate(data["files"]):
        prefix = f"files[{i}]"
        for field in ("id", "url", "dest"):
            if field not in f:
                errors.append(f"{prefix}: missing required field: {field}")

        if "id" in f:
            if f["id"] in seen_ids:
                errors.append(f"{prefix}: duplicate id '{f['id']}'")
            seen_ids.add(f["id"])

        if "url" in f:
            if not isinstance(f["url"], str) or not f["url"].startswith(("http://", "https://")):
                errors.append(f"{prefix}: url must be http or https")

        if "dest" in f:
            try:
                sanitize_dest(f["dest"])
            except ValueError as exc:
                errors.append(f"{prefix}: invalid dest: {exc}")

    return errors


def parse_manifest(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)


def manifest_to_db(data: dict, raw_json: str) -> Manifest:
    """Parse a validated manifest dict and persist it to the DB."""
    defaults = data.get("defaults", {})

    manifest = Manifest(
        id=str(uuid.uuid4()),
        name=data["name"],
        dest_root=unicodedata.normalize("NFC", data["destRoot"]),
        raw_json=raw_json,
        concurrent=int(defaults.get("concurrent", 3)),
        retries=int(defaults.get("retries", 5)),
        retry_backoff_sec=int(defaults.get("retryBackoffSec", 10)),
        min_bytes=int(defaults.get("minBytes", 100_000)),
        expect_magic=defaults.get("expectMagic", {}),
        default_headers=defaults.get("headers", {}),
        status="pending",
    )
    db.session.add(manifest)
    db.session.flush()  # assign manifest.id before creating jobs

    for f in data["files"]:
        job = Job(
            id=str(uuid.uuid4()),
            manifest_id=manifest.id,
            file_id=str(f["id"]),
            url=f["url"],
            dest=sanitize_dest(f["dest"]),
            file_type=f.get("type"),
            extra_headers=f.get("headers", {}),
            expected_bytes=int(f.get("expectedBytes") or 0),
            status="pending",
        )
        db.session.add(job)

    db.session.commit()
    return manifest
