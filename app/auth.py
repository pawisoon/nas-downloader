from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask_login import LoginManager, UserMixin

if TYPE_CHECKING:
    from flask import Flask

log = logging.getLogger(__name__)
_ph = PasswordHasher()
login_manager = LoginManager()


class User(UserMixin):
    def __init__(self, username: str) -> None:
        self.id = username
        self.username = username


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    from flask import current_app

    if user_id == current_app.config.get("USERNAME", "admin"):
        return User(user_id)
    return None


def verify_password(plain: str) -> bool:
    from flask import current_app

    stored = current_app.config.get("PASSWORD_HASH", "")
    if not stored:
        return False
    try:
        _ph.verify(stored, plain)
        return True
    except VerifyMismatchError:
        time.sleep(0.5)  # constant-time on failure
        return False
    except Exception:
        return False


def init_password_if_needed(app: Flask) -> None:
    import os

    # Config dict (e.g. from tests) takes precedence over env var
    cfg_hash = app.config.get("PASSWORD_HASH", "").strip()
    env_hash = os.environ.get("PASSWORD_HASH", "").strip()
    existing = cfg_hash or env_hash
    if existing:
        app.config["PASSWORD_HASH"] = existing
        app.config["INITIAL_PASSWORD_GENERATED"] = False
        return

    password = secrets.token_urlsafe(16)
    hashed = _ph.hash(password)
    app.config["PASSWORD_HASH"] = hashed
    app.config["INITIAL_PASSWORD_GENERATED"] = True

    state_dir = Path(app.config.get("STATE_DIR", "/state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    init_file = state_dir / "initial_password.txt"
    init_file.write_text(f"nas-downloader initial password: {password}\n")

    log.warning("=" * 60)
    log.warning("INITIAL PASSWORD: %s", password)
    log.warning("Saved to: %s", init_file)
    log.warning("Delete after first login (done automatically).")
    log.warning("=" * 60)


def delete_initial_password_file(app: Flask) -> None:
    state_dir = Path(app.config.get("STATE_DIR", "/state"))
    (state_dir / "initial_password.txt").unlink(missing_ok=True)
    app.config["INITIAL_PASSWORD_GENERATED"] = False
