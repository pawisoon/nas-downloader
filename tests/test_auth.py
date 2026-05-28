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
