from __future__ import annotations

from pathlib import Path

from app.verify import verify_job

# ── helpers ───────────────────────────────────────────────────────────────────


class _FakeManifest:
    dest_root = "Downloads"
    expect_magic: dict = {}


class _FakeJob:
    dest = "test.bin"
    file_type: str | None = None
    expected_bytes: int = 0


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ── tests ─────────────────────────────────────────────────────────────────────


def test_verify_missing_file(tmp_path):
    m = _FakeManifest()
    j = _FakeJob()
    result = verify_job(j, m, tmp_path)
    assert not result.ok
    assert "missing" in result.reason


def test_verify_size_exact_match(tmp_path):
    content = b"hello world"
    m = _FakeManifest()
    j = _FakeJob()
    j.expected_bytes = len(content)
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok


def test_verify_size_off_by_one_ok(tmp_path):
    content = b"hello world"  # 11 bytes
    m = _FakeManifest()
    j = _FakeJob()
    j.expected_bytes = len(content) + 1  # off by 1
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok


def test_verify_size_mismatch(tmp_path):
    content = b"hello world"
    m = _FakeManifest()
    j = _FakeJob()
    j.expected_bytes = len(content) + 1000
    _write(tmp_path / m.dest_root / j.dest, content)
    result = verify_job(j, m, tmp_path)
    assert not result.ok
    assert "size" in result.reason


def test_verify_skip_size_when_zero(tmp_path):
    content = b"hello"
    m = _FakeManifest()
    j = _FakeJob()
    j.expected_bytes = 0  # skip size check
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok


def test_verify_magic_ok(tmp_path):
    # webm magic: 1A 45 DF A3
    content = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"\x00" * 100
    m = _FakeManifest()
    m.expect_magic = {"webm": "1A 45 DF A3"}
    j = _FakeJob()
    j.file_type = "webm"
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok


def test_verify_magic_mismatch(tmp_path):
    content = b"\x00\x00\x00\x00" + b"x" * 100
    m = _FakeManifest()
    m.expect_magic = {"webm": "1A 45 DF A3"}
    j = _FakeJob()
    j.file_type = "webm"
    _write(tmp_path / m.dest_root / j.dest, content)
    result = verify_job(j, m, tmp_path)
    assert not result.ok
    assert "magic" in result.reason


def test_verify_no_magic_check_when_type_absent(tmp_path):
    content = b"\x00\x00\x00\x00" + b"x" * 100
    m = _FakeManifest()
    m.expect_magic = {"webm": "1A 45 DF A3"}
    j = _FakeJob()
    j.file_type = None  # no type → no magic check
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok


def test_verify_pdf_magic(tmp_path):
    # PDF magic: %PDF = 25 50 44 46
    content = b"%PDF-1.7" + b"\n" * 100
    m = _FakeManifest()
    m.expect_magic = {"pdf": "25 50 44 46"}
    j = _FakeJob()
    j.file_type = "pdf"
    j.dest = "doc.pdf"
    _write(tmp_path / m.dest_root / j.dest, content)
    assert verify_job(j, m, tmp_path).ok
