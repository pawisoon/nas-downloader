from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app import create_app

TEST_PASSWORD = "testpassword"
TEST_PASSWORD_HASH = PasswordHasher().hash(TEST_PASSWORD)

MINIMAL_MANIFEST = {
    "name": "Test Manifest",
    "destRoot": "Downloads",
    "defaults": {
        "retries": 2,
        "retryBackoffSec": 1,
        "concurrent": 2,
        "minBytes": 4,
        "expectMagic": {
            "webm": "1A 45 DF A3",
            "pdf": "25 50 44 46",
        },
    },
    "files": [
        {
            "id": "file-001",
            "url": "http://localhost:9999/test.webm",
            "dest": "Folder/test.webm",
            "type": "webm",
            "expectedBytes": 0,
        }
    ],
}


@pytest.fixture()
def app(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    flask_app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "STATE_DIR": str(state_dir),
            "DOWNLOAD_ROOT": str(data_dir),
            "PASSWORD_HASH": TEST_PASSWORD_HASH,
            "USERNAME": "admin",
            "SECRET_KEY": "test-secret",
        }
    )
    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(app, client):
    """Test client pre-authenticated as admin."""
    with client.session_transaction() as sess:
        sess["_user_id"] = "admin"
        sess["_fresh"] = True
    return client


@pytest.fixture()
def data_dir(app):
    return __import__("pathlib").Path(app.config["DOWNLOAD_ROOT"])
