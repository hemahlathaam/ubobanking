from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, session, abort,
)
from flask_login import (
    login_user, logout_user, login_required, current_user,
)

from ..extensions import db, limiter
from ..models import User, UserProfile
from ..utils import (
    record_audit, get_impersonator, permission_required,
    generate_account_number,
)

bp = Blueprint("auth", __name__)


@bp.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))
    return render_template("home.html")


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("20/minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()

        if user and user.is_enabled and user.check_password(password):
            login_user(user)

            if not UserProfile.query.filter_by(user_id=user.id).first():
                db.session.add(UserProfile(
                    user_id=user.id,
                    account_number=generate_account_number(),
                ))
                db.session.commit()

            return redirect(url_for("dashboard.dashboard"))

        flash("Incorrect username or password.", "error")

    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    actor = get_impersonator()
    if actor:
        record_audit(
            "impersonation_ended",
            current_user.id,
            "Logged out of impersonated session",
            actor_id=actor.id,
        )
        db.session.commit()
        session.clear()
        login_user(actor, fresh=True)
        flash("Impersonation ended.", "success")
        return redirect(url_for("admin.admin_panel"))

    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/admin/impersonate/<int:user_id>", methods=["POST"])
@permission_required("impersonate")
def start_impersonation(user_id):
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if not target.is_enabled:
        flash("Only enabled accounts can be impersonated.", "error")
        return redirect(url_for("admin.admin_panel"))
    if target.id == current_user.id:
        flash("You are already signed in as this account.", "error")
        return redirect(url_for("admin.admin_panel"))

    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("An impersonation reason is required.", "error")
        return redirect(url_for("admin.admin_panel"))

    actor_id = current_user.id
    actor = db.session.get(User, actor_id)
    session.clear()
    login_user(target, fresh=True)
    session["impersonator_id"] = actor_id
    session["impersonation_started_at"] = datetime.now(timezone.utc).isoformat()

    record_audit(
        "impersonation_started",
        target.id,
        f"Management user {actor.username} started an impersonation session: {reason}",
        actor_id=actor_id,
    )
    db.session.commit()

    flash(f"Now viewing the account for {target.username}.", "warning")
    return redirect(url_for("dashboard.dashboard"))


@bp.route("/admin/stop-impersonation", methods=["POST"])
@login_required
def stop_impersonation():
    actor_id = session.get("impersonator_id")
    actor = db.session.get(User, actor_id) if actor_id else None
    if not actor or not actor.is_enabled or not actor.has_permission("impersonate"):
        session.clear()
        logout_user()
        flash("The management session is no longer available.", "error")
        return redirect(url_for("auth.login"))

    impersonated_id = current_user.id
    record_audit(
        "impersonation_ended",
        impersonated_id,
        "Management user ended impersonation.",
        actor_id=actor.id,
    )
    db.session.commit()
    session.clear()
    login_user(actor, fresh=True)
    flash("Impersonation ended.", "success")
    return redirect(url_for("admin.admin_panel"))


# ---------- before_request to keep impersonation sessions valid ----------

@bp.before_app_request
def validate_impersonation_session():
    actor_id = session.get("impersonator_id")
    if not actor_id:
        return None

    actor = db.session.get(User, actor_id)
    target_is_valid = (
        current_user.is_authenticated
        and current_user.is_enabled
        and current_user.id != actor_id
    )

    if not actor or not actor.is_enabled or not actor.has_permission("impersonate"):
        session.clear()
        logout_user()
        flash("The management session is no longer available.", "error")
        return redirect(url_for("auth.login"))

    if not target_is_valid:
        target_id = session.get("_user_id")
        target = db.session.get(User, target_id) if target_id else None
        if target and target.id != actor.id:
            record_audit(
                "impersonation_ended",
                target.id,
                "Impersonation ended because the target account was no longer enabled.",
                actor_id=actor.id,
            )
            db.session.commit()
        session.clear()
        logout_user()
        login_user(actor, fresh=True)
        flash("Impersonation ended because the account is no longer enabled.", "warning")
        return redirect(url_for("admin.admin_panel"))

    return None
