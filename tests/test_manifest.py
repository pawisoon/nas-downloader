from __future__ import annotations

import json
import unicodedata

import pytest

from app.manifest import parse_manifest, sanitize_dest, validate_manifest

# ── sanitize_dest ─────────────────────────────────────────────────────────────


def test_sanitize_dest_valid():
    assert sanitize_dest("Folder/file.webm") == "Folder/file.webm"


def test_sanitize_dest_nfc_normalization():
    # Decomposed Polish chars → NFC
    for char in "ąćęłńóśźż":
        decomposed = unicodedata.normalize("NFD", char)
        normalized = sanitize_dest(decomposed)
        assert normalized == unicodedata.normalize("NFC", decomposed)
        assert unicodedata.is_normalized("NFC", normalized)


def test_sanitize_dest_all_polish_diacritics():
    path = "Wstęp/Komunikacja_werbalna/ćwiczenia_ą_ź_ż_ó_ę_ś_ń_ł.pdf"
    result = sanitize_dest(path)
    assert unicodedata.is_normalized("NFC", result)


def test_sanitize_dest_rejects_dotdot():
    with pytest.raises(ValueError, match="traversal"):
        sanitize_dest("../etc/passwd")


def test_sanitize_dest_rejects_dotdot_nested():
    with pytest.raises(ValueError, match="traversal"):
        sanitize_dest("folder/../../../etc/passwd")


def test_sanitize_dest_rejects_absolute():
    with pytest.raises(ValueError, match="absolute"):
        sanitize_dest("/etc/passwd")


def test_sanitize_dest_rejects_windows_absolute():
    with pytest.raises(ValueError, match="absolute"):
        sanitize_dest("C:\\Windows\\system32")


def test_sanitize_dest_rejects_nul_byte():
    with pytest.raises(ValueError, match="NUL"):
        sanitize_dest("folder/fi\x00le.pdf")


# ── validate_manifest ─────────────────────────────────────────────────────────


VALID = {
    "name": "Test",
    "destRoot": "Downloads",
    "files": [
        {"id": "f1", "url": "https://example.com/a.webm", "dest": "Folder/a.webm"},
    ],
}


def test_validate_manifest_valid():
    assert validate_manifest(VALID) == []


def test_validate_manifest_missing_name():
    data = {k: v for k, v in VALID.items() if k != "name"}
    errors = validate_manifest(data)
    assert any("name" in e for e in errors)


def test_validate_manifest_missing_dest_root():
    data = {k: v for k, v in VALID.items() if k != "destRoot"}
    errors = validate_manifest(data)
    assert any("destRoot" in e for e in errors)


def test_validate_manifest_missing_files():
    data = {k: v for k, v in VALID.items() if k != "files"}
    errors = validate_manifest(data)
    assert any("files" in e for e in errors)


def test_validate_manifest_missing_file_id():
    data = {"name": "X", "destRoot": "D", "files": [{"url": "https://x.com/a", "dest": "a"}]}
    errors = validate_manifest(data)
    assert any("id" in e for e in errors)


def test_validate_manifest_duplicate_ids():
    data = {
        "name": "X",
        "destRoot": "D",
        "files": [
            {"id": "dup", "url": "https://x.com/a", "dest": "a.webm"},
            {"id": "dup", "url": "https://x.com/b", "dest": "b.webm"},
        ],
    }
    errors = validate_manifest(data)
    assert any("duplicate" in e for e in errors)


def test_validate_manifest_bad_url_scheme():
    data = {
        "name": "X",
        "destRoot": "D",
        "files": [{"id": "f1", "url": "ftp://x.com/a", "dest": "a.webm"}],
    }
    errors = validate_manifest(data)
    assert any("http" in e for e in errors)


def test_validate_manifest_traversal_in_dest():
    data = {
        "name": "X",
        "destRoot": "D",
        "files": [{"id": "f1", "url": "https://x.com/a", "dest": "../../../etc/passwd"}],
    }
    errors = validate_manifest(data)
    assert any("traversal" in e for e in errors)


# ── parse_manifest ────────────────────────────────────────────────────────────


def test_parse_manifest_bytes():
    raw = json.dumps(VALID).encode()
    assert parse_manifest(raw)["name"] == "Test"


def test_parse_manifest_str():
    assert parse_manifest(json.dumps(VALID))["name"] == "Test"


def test_parse_manifest_invalid_json():
    with pytest.raises(Exception):
        parse_manifest(b"not json {")


# ── manifest_to_db ────────────────────────────────────────────────────────────


def test_manifest_to_db_creates_rows(app):
    from app.manifest import manifest_to_db
    from app.models import Job

    with app.app_context():
        data = {
            "name": "Polish Test",
            "destRoot": "Psychologia – I rok",
            "defaults": {"retries": 3, "concurrent": 2, "minBytes": 10},
            "files": [
                {
                    "id": "vid-001",
                    "url": "https://example.com/lecture.webm",
                    "dest": "Wstęp/Komunikacja/lecture.webm",
                    "type": "webm",
                    "expectedBytes": 5000000,
                }
            ],
        }
        manifest = manifest_to_db(data, json.dumps(data))
        assert manifest.id
        assert manifest.name == "Polish Test"
        assert manifest.retries == 3
        job = Job.query.filter_by(manifest_id=manifest.id).first()
        assert job is not None
        assert job.file_id == "vid-001"
        assert job.expected_bytes == 5000000
        assert unicodedata.is_normalized("NFC", job.dest)
