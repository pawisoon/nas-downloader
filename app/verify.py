from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Job, Manifest


@dataclasses.dataclass
class VerifyResult:
    ok: bool
    reason: str


def verify_job(job: Job, manifest: Manifest, download_root: Path) -> VerifyResult:
    path = download_root / manifest.dest_root / job.dest
    if not path.exists():
        return VerifyResult(ok=False, reason="missing")

    if job.expected_bytes:
        size = path.stat().st_size
        if abs(size - job.expected_bytes) > 1:
            return VerifyResult(
                ok=False, reason=f"size {size} != expected {job.expected_bytes}"
            )

    if job.file_type and job.file_type in (manifest.expect_magic or {}):
        magic_hex: str = manifest.expect_magic[job.file_type]
        expected = bytes.fromhex(magic_hex.replace(" ", ""))
        with open(path, "rb") as f:
            actual = f.read(len(expected))
        if actual != expected:
            return VerifyResult(
                ok=False,
                reason=f"magic mismatch: expected {magic_hex}, got {actual.hex()}",
            )

    return VerifyResult(ok=True, reason="")
