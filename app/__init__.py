from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from flask import Flask
from flask_wtf.csrf import CSRFProtect

from .auth import init_password_if_needed, login_manager
from .models import db

log = logging.getLogger(__name__)
csrf = CSRFProtect()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)

    state_dir = os.environ.get("STATE_DIR", "/state")
    download_root = os.environ.get("DOWNLOAD_ROOT", "/data")

    app.config.update(
        {
            "STATE_DIR": state_dir,
            "DOWNLOAD_ROOT": download_root,
            "USERNAME": os.environ.get("USERNAME", "admin"),
            "MAX_CONCURRENT_GLOBAL": int(os.environ.get("MAX_CONCURRENT_GLOBAL", "5")),
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{state_dir}/nas-downloader.db",
            "SQLALCHEMY_ENGINE_OPTIONS": {
                "connect_args": {"check_same_thread": False},
            },
            "WTF_CSRF_ENABLED": True,
            "WTF_CSRF_TIME_LIMIT": None,
            "SECRET_KEY": _load_or_generate_secret(state_dir),
            "TAILWIND_CDN": not (Path(__file__).parent / "static" / "tailwind.min.css").exists(),
            "INITIAL_PASSWORD_GENERATED": False,
        }
    )

    if config:
        app.config.update(config)

    # Extensions
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "views.login"
    csrf.init_app(app)

    from .routes.api import api_bp
    from .routes.sse import EventBus, sse_bp
    from .routes.views import views_bp

    app.extensions["event_bus"] = EventBus()

    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(sse_bp)

    csrf.exempt(sse_bp)

    with app.app_context():
        db.create_all()
        _enable_wal()

    init_password_if_needed(app)

    if not app.config.get("TESTING"):
        from .worker import init_worker, sweep_stale_parts

        init_worker(app)
        try:
            sweep_stale_parts(Path(download_root))
        except Exception:
            pass

    return app


def _enable_wal() -> None:
    from sqlalchemy import text

    try:
        db.session.execute(text("PRAGMA journal_mode=WAL"))
        db.session.execute(text("PRAGMA synchronous=NORMAL"))
        db.session.commit()
    except Exception as exc:
        log.warning("WAL setup failed: %s", exc)


def _load_or_generate_secret(state_dir: str) -> str:
    key_file = Path(state_dir) / "secret_key"
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            return key_file.read_text().strip()
        key = secrets.token_hex(32)
        key_file.write_text(key)
        return key
    except Exception:
        return secrets.token_hex(32)
