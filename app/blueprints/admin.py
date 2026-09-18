from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, abort,
)
from flask_login import login_required, current_user

from ..extensions import db, socketio
from ..models import (
    User, UserProfile, Transaction, LedgerEntry, AuditLog,
    UserRole, UserPermission, PrivilegedTransferRequest,
    CheckDeposit, Message, MessageReaction, MessageAttachment,
    MessageMention, MessageReceipt, MessageReadState,
    GroupChat, GroupMembership, GroupMessage, GroupReadState,
    GroupMessageReaction, Notification, NotificationPreference,
    ScheduledTransfer, Beneficiary, SupportTicket, SupportMessage,
    BillPayment, BillPaymentLedgerEntry, CheckDeposit as _CheckDeposit,
)
from ..utils import (
    money, record_audit, create_notification, add_ledger_entries,
    send_live_update, admin_required, permission_required,
    generate_account_number, apply_user_roles, to_decimal,
)

bp = Blueprint("admin", __name__)


# ---------------- Admin panel ----------------

@bp.route("/admin")
@admin_required
def admin_panel():
    users = User.query.order_by(User.username.asc()).all()
    permission_map = {
        u.id: {
            item.permission
            for item in UserPermission.query.filter_by(user_id=u.id).all()
        }
        for u in users
    }
    return render_template(
        "admin.html",
        users=users,
        permission_map=permission_map,
        pending_transfer_requests=(
            PrivilegedTransferRequest.query
            .filter_by(status="pending")
            .order_by(PrivilegedTransferRequest.created_at.asc())
            .all()
        ),
    )


@bp.route("/admin/balances")
@permission_required("balance_read")
def management_balances():
    users = User.query.order_by(User.username.asc()).all()
    record_audit("management_balance_viewed", details=f"accounts={len(users)}")
    db.session.commit()
    return render_template("admin_balances.html", users=users)


@bp.route("/admin/audit-logs")
@permission_required("audit_read")
def audit_logs():
    query = AuditLog.query
    search = request.args.get("q", "").strip()
    action = request.args.get("action", "").strip()
    if action:
        query = query.filter(AuditLog.action == action)
    if search:
        query = query.filter(
            AuditLog.details.ilike(f"%{search}%")
            | AuditLog.action.ilike(f"%{search}%")
        )
    logs = query.order_by(AuditLog.created_at.desc()).limit(200).all()
    return render_template("audit_logs.html", logs=logs, search=search, action=action)


# ---------------- User management ----------------

@bp.route("/admin/create-user", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip()
    password = request.form.get("password", "")

    if len(username) < 3:
        flash("Username must have at least 3 characters.", "error")
        return redirect(url_for("admin.admin_panel"))
    if not display_name:
        flash("Enter a display name.", "error")
        return redirect(url_for("admin.admin_panel"))
    if len(password) < 8:
        flash("Password must have at least 8 characters.", "error")
        return redirect(url_for("admin.admin_panel"))
    if User.query.filter_by(username=username).first():
        flash("That username already exists.", "error")
        return redirect(url_for("admin.admin_panel"))

    try:
        balance = Decimal(request.form.get("balance", "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        flash("Invalid balance.", "error")
        return redirect(url_for("admin.admin_panel"))

    if balance < 0:
        flash("Balance cannot be negative.", "error")
        return redirect(url_for("admin.admin_panel"))

    account_type = request.form.get("account_type", "standard").strip()
    if account_type not in {"standard", "developer", "admin"}:
        flash("Choose a valid account type.", "error")
        return redirect(url_for("admin.admin_panel"))

    selected_roles = set(request.form.getlist("role")).intersection({"admin", "developer"})
    if not selected_roles and account_type in {"developer", "admin"}:
        selected_roles.add(account_type)

    user = User(
        username=username,
        display_name=display_name,
        balance=balance,
        is_admin="admin" in selected_roles,
        account_type=(
            "admin" if "admin" in selected_roles
            else "developer" if "developer" in selected_roles
            else "standard"
        ),
        is_enabled=True,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()

    db.session.add(UserProfile(
        user_id=user.id,
        account_number=generate_account_number(),
    ))
    for role in sorted(selected_roles):
        db.session.add(UserRole(user_id=user.id, role=role))

    record_audit(
        "user_created",
        user.id,
        f"username={username}; roles={','.join(sorted(selected_roles)) or 'standard'}",
    )
    db.session.commit()
    flash("Account created.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/roles/<int:user_id>", methods=["POST"])
@admin_required
def update_roles(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    roles = set(request.form.getlist("role")).intersection({"admin", "developer"})
    if user.id == current_user.id and "admin" not in roles:
        flash("Keep the admin role on your own management account.", "error")
        return redirect(url_for("admin.admin_panel"))

    apply_user_roles(user, roles)
    record_audit(
        "roles_updated",
        user.id,
        f"user={user.username}; roles={','.join(sorted(roles)) or 'standard'}",
    )
    db.session.commit()
    flash("Roles updated.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/set-balance/<int:user_id>", methods=["POST"])
@admin_required
def set_balance(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    try:
        balance = Decimal(request.form.get("balance", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        flash("Invalid balance.", "error")
        return redirect(url_for("admin.admin_panel"))

    if balance < 0:
        flash("Balance cannot be negative.", "error")
        return redirect(url_for("admin.admin_panel"))

    user.balance = balance
    db.session.commit()
    send_live_update(user.id, "balance_changed", checking_balance=str(user.balance))
    socketio.emit("rubo_update", {"type": "admin_accounts_changed"}, to="admins")
    flash("Checking balance updated.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/toggle-user/<int:user_id>", methods=["POST"])
@admin_required
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot disable your own admin account.", "error")
        return redirect(url_for("admin.admin_panel"))

    user.is_enabled = not user.is_enabled
    db.session.commit()
    flash(
        f"{user.username} has been {'enabled' if user.is_enabled else 'disabled'}.",
        "success",
    )
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/reset-password/<int:user_id>", methods=["POST"])
@admin_required
def reset_password(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    password = request.form.get("new_password", "")
    if len(password) < 8:
        flash("Password must have at least 8 characters.", "error")
        return redirect(url_for("admin.admin_panel"))
    user.set_password(password)
    db.session.commit()
    flash(f"Password changed for {user.username}.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/permissions/<int:user_id>", methods=["POST"])
@admin_required
def update_permissions(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)

    allowed = {
        "balance_read", "check_deposit", "impersonate", "privileged_transfer",
        "approve_transfer", "audit_read", "manage_groups", "support_manage",
    }
    UserPermission.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    granted = []
    for permission in request.form.getlist("permission"):
        if permission in allowed:
            db.session.add(UserPermission(user_id=user.id, permission=permission))
            granted.append(permission)

    record_audit(
        "permissions_updated",
        user.id,
        f"user={user.username}; permissions={','.join(sorted(granted)) or 'none'}",
    )
    db.session.commit()
    flash("Permissions updated.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/delete-user/<int:user_id>", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own admin account.", "error")
        return redirect(url_for("admin.admin_panel"))

    username = user.username

    tx_ids = [t.id for t in Transaction.query.filter(
        (Transaction.sender_id == user.id) | (Transaction.receiver_id == user.id)
    ).all()]
    if tx_ids:
        CheckDeposit.query.filter(CheckDeposit.transaction_id.in_(tx_ids)).delete(synchronize_session=False)
        LedgerEntry.query.filter(LedgerEntry.transaction_id.in_(tx_ids)).delete(synchronize_session=False)

    Transaction.query.filter(
        (Transaction.sender_id == user.id) | (Transaction.receiver_id == user.id)
    ).delete(synchronize_session=False)
    LedgerEntry.query.filter_by(account_id=user.id).delete(synchronize_session=False)
    CheckDeposit.query.filter(
        (CheckDeposit.actor_id == user.id)
        | (CheckDeposit.source_id == user.id)
        | (CheckDeposit.recipient_id == user.id)
    ).delete(synchronize_session=False)
    PrivilegedTransferRequest.query.filter(
        (PrivilegedTransferRequest.actor_id == user.id)
        | (PrivilegedTransferRequest.source_id == user.id)
        | (PrivilegedTransferRequest.receiver_id == user.id)
        | (PrivilegedTransferRequest.approved_by_id == user.id)
    ).delete(synchronize_session=False)

    msg_ids = [m.id for m in Message.query.filter(
        (Message.sender_id == user.id) | (Message.receiver_id == user.id)
    ).all()]
    if msg_ids:
        MessageReaction.query.filter(MessageReaction.message_id.in_(msg_ids)).delete(synchronize_session=False)
        MessageAttachment.query.filter(MessageAttachment.message_id.in_(msg_ids)).delete(synchronize_session=False)
        MessageMention.query.filter(MessageMention.message_id.in_(msg_ids)).delete(synchronize_session=False)
        MessageReceipt.query.filter(MessageReceipt.message_id.in_(msg_ids)).delete(synchronize_session=False)

    Message.query.filter(
        (Message.sender_id == user.id) | (Message.receiver_id == user.id)
    ).delete(synchronize_session=False)
    MessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    GroupMessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    MessageAttachment.query.filter_by(uploader_id=user.id).delete(synchronize_session=False)
    MessageMention.query.filter_by(mentioned_user_id=user.id).delete(synchronize_session=False)
    MessageReceipt.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    MessageReadState.query.filter(
        (MessageReadState.user_id == user.id) | (MessageReadState.other_user_id == user.id)
    ).delete(synchronize_session=False)

    gm_ids = [m.id for m in GroupMessage.query.filter_by(sender_id=user.id).all()]
    if gm_ids:
        GroupMessageReaction.query.filter(GroupMessageReaction.group_message_id.in_(gm_ids)).delete(synchronize_session=False)
        MessageAttachment.query.filter(MessageAttachment.group_message_id.in_(gm_ids)).delete(synchronize_session=False)
        MessageMention.query.filter(MessageMention.group_message_id.in_(gm_ids)).delete(synchronize_session=False)
        MessageReceipt.query.filter(MessageReceipt.group_message_id.in_(gm_ids)).delete
