from __future__ import annotations

import logging

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from ..auth import User, delete_initial_password_file, verify_password
from ..models import Job, Manifest

log = logging.getLogger(__name__)
views_bp = Blueprint("views", __name__)


@views_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("views.manifests"))
    return redirect(url_for("views.login"))


@views_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("views.manifests"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        expected = current_app.config.get("USERNAME", "admin")
        if username == expected and verify_password(password):
            user = User(username)
            login_user(user, remember=True)
            if current_app.config.get("INITIAL_PASSWORD_GENERATED"):
                delete_initial_password_file(current_app._get_current_object())
            nxt = request.args.get("next") or url_for("views.manifests")
            return redirect(nxt)
        flash("Invalid credentials.", "error")

    return render_template("login.html")


@views_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("views.login"))


@views_bp.route("/manifests")
@login_required
def manifests():
    items = Manifest.query.order_by(Manifest.created_at.desc()).all()
    return render_template("manifests.html", manifests=items)


@views_bp.route("/manifests/<manifest_id>")
@login_required
def manifest_detail(manifest_id: str):
    manifest = Manifest.query.get_or_404(manifest_id)
    jobs = Job.query.filter_by(manifest_id=manifest_id).order_by(Job.dest).all()

    # Group jobs by folder
    folders: dict[str, list] = {}
    for job in jobs:
        parts = job.dest.replace("\\", "/").split("/")
        folder = "/".join(parts[:-1]) if len(parts) > 1 else ""
        folders.setdefault(folder, []).append(job)

    return render_template(
        "manifest_detail.html",
        manifest=manifest,
        folders=folders,
        jobs=jobs,
    )
