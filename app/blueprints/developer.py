import secrets
import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, abort,
)
from flask_login import current_user

from ..extensions import db
from ..models import (
    User, UserProfile, DeveloperApiKey, DeveloperFeatureFlag,
    AuditLog, Transaction,
)
from ..utils import (
    record_audit, developer_required, generate_account_number,
)

bp = Blueprint("developer", __name__)


@bp.route("/developer-tools", methods=["GET", "POST"])
@developer_required
def developer_tools():
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "create_api_key":
            name = request.form.get("name", "").strip()[:80]
            if not name:
                flash("Enter a name for the API key.", "error")
            else:
                raw_key = f"mb_dev_{secrets.token_urlsafe(32)}"
                api_key = DeveloperApiKey(
                    user_id=current_user.id,
                    name=name,
                    key_prefix=raw_key[:12],
                    key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                )
                db.session.add(api_key)
                record_audit("developer_api_key_created", details=f"name={name}")
                db.session.commit()
                flash(f"Copy this API key now; it will not be shown again: {raw_key}", "success")

        elif action == "revoke_api_key":
            api_key = db.session.get(DeveloperApiKey, request.form.get("key_id", type=int))
            if not api_key or api_key.user_id != current_user.id:
                abort(404)
            api_key.is_active = False
            record_audit("developer_api_key_revoked", details=f"key_id={api_key.id}")
            db.session.commit()
            flash("API key revoked.", "success")

        elif action == "toggle_flag":
            name = request.form.get("flag_name", "").strip()[:80]
            if name not in {"new_dashboard", "sandbox_payments", "webhook_preview"}:
                abort(400)
            flag = DeveloperFeatureFlag.query.filter_by(name=name).first()
            if not flag:
                flag = DeveloperFeatureFlag(name=name)
                db.session.add(flag)
            flag.enabled = not flag.enabled
            flag.updated_by_id = current_user.id
            flag.updated_at = datetime.now(timezone.utc)
            record_audit("developer_feature_flag_changed", details=f"{name}={flag.enabled}")
            db.session.commit()
            flash(
                f"Feature flag {name} is now {'enabled' if flag.enabled else 'disabled'}.",
                "success",
            )

        elif action == "create_sandbox_user":
            suffix = secrets.token_hex(3)
            password = f"Sandbox-{secrets.token_urlsafe(8)}"
            sandbox_user = User(
                username=f"sandbox_{suffix}",
                display_name="Sandbox User",
                balance=Decimal("1000.00"),
                account_type="standard",
                is_enabled=True,
            )
            sandbox_user.set_password(password)
            db.session.add(sandbox_user)
            db.session.flush()
            record_audit(
                "developer_sandbox_user_created",
                target_user_id=sandbox_user.id,
                details=f"username={sandbox_user.username}",
            )
            db.session.commit()
            flash(
                f"Sandbox credentials — username: {sandbox_user.username}, password: {password}",
                "success",
            )
        else:
            abort(400)

        return redirect(url_for("developer.developer_tools"))

    flags = {
        name: DeveloperFeatureFlag.query.filter_by(name=name).first()
        for name in ("new_dashboard", "sandbox_payments", "webhook_preview")
    }

    return render_template(
        "developer_tools.html",
        api_keys=(
            DeveloperApiKey.query
            .filter_by(user_id=current_user.id)
            .order_by(DeveloperApiKey.created_at.desc())
            .all()
        ),
        flags=flags,
        recent_logs=(
            AuditLog.query
            .order_by(AuditLog.created_at.desc())
            .limit(25)
            .all()
        ),
        health={
            "users": User.query.count(),
            "transactions": Transaction.query.count(),
            "audit_events": AuditLog.query.count(),
            "database": "connected",
        },
    )
