from __future__ import annotations

from tests.conftest import TEST_PASSWORD


def test_login_success(client):
    r = client.post("/login", data={"username": "admin", "password": TEST_PASSWORD})
    assert r.status_code == 302
    assert "/manifests" in r.headers["Location"]


def test_login_wrong_password(client):
    r = client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 200
    assert b"Invalid" in r.data


def test_login_wrong_username(client):
    r = client.post("/login", data={"username": "hacker", "password": TEST_PASSWORD})
    assert r.status_code == 200
    assert b"Invalid" in r.data


def test_protected_route_requires_login(client):
    r = client.get("/manifests")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_logged_in_can_access_manifests(auth_client):
    r = auth_client.get("/manifests")
    assert r.status_code == 200


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert b"ok" in r.data


def test_logout(auth_client):
    r = auth_client.post("/logout")
    assert r.status_code == 302
    r2 = auth_client.get("/manifests")
    assert r2.status_code == 302
    assert "/login" in r2.headers["Location"]


def test_initial_password_file_deleted_after_login(app, tmp_path):
    from pathlib import Path

    from argon2 import PasswordHasher

    # Simulate first-boot: no PASSWORD_HASH in env
    state_dir = Path(app.config["STATE_DIR"])
    init_file = state_dir / "initial_password.txt"

    # Write a fake initial_password.txt
    raw_pw = "mysecret123"
    init_file.write_text(f"nas-downloader initial password: {raw_pw}\n")
    app.config["PASSWORD_HASH"] = PasswordHasher().hash(raw_pw)
    app.config["INITIAL_PASSWORD_GENERATED"] = True

    with app.test_client() as c:
        c.post("/login", data={"username": "admin", "password": raw_pw})
        assert not init_file.exists()


def test_password_env_var_is_hashed(tmp_path, monkeypatch):
    """PASSWORD env var should be hashed on startup and accepted for login."""
    from app import create_app

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("PASSWORD", "envplain123")
    monkeypatch.delenv("PASSWORD_HASH", raising=False)

    flask_app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "STATE_DIR": str(state_dir),
            "DOWNLOAD_ROOT": str(tmp_path),
            "USERNAME": "admin",
            "SECRET_KEY": "test-secret",
        }
    )

    stored = flask_app.config.get("PASSWORD_HASH", "")
    assert stored.startswith("$argon2"), "PASSWORD should have been hashed"

    with flask_app.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "envplain123"})
        assert r.status_code == 302

    # Wrong password rejected
    with flask_app.test_client() as c:
        r = c.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 200
        assert b"Invalid" in r.data


def test_password_hash_takes_precedence_over_password(tmp_path, monkeypatch):
    """If both PASSWORD_HASH and PASSWORD are set, PASSWORD_HASH wins."""
    from argon2 import PasswordHasher

    from app import create_app

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    real_hash = PasswordHasher().hash("realone")
    monkeypatch.setenv("PASSWORD_HASH", real_hash)
    monkeypatch.setenv("PASSWORD", "decoy")

    flask_app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "STATE_DIR": str(state_dir),
            "DOWNLOAD_ROOT": str(tmp_path),
            "USERNAME": "admin",
            "SECRET_KEY": "test-secret",
        }
    )

    with flask_app.test_client() as c:
        r1 = c.post("/login", data={"username": "admin", "password": "realone"})
        assert r1.status_code == 302
    with flask_app.test_client() as c:
        r2 = c.post("/login", data={"username": "admin", "password": "decoy"})
        assert r2.status_code == 200  # rejected
