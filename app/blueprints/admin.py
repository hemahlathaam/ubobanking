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
        MessageReceipt.query.filter(MessageReceipt.group_message_id.in_(gm_ids)).delete(synchronize_session=False)

    GroupMessage.query.filter_by(sender_id=user.id).delete(synchronize_session=False)
    GroupReadState.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    GroupMembership.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    for group in GroupChat.query.filter_by(creator_id=user.id).all():
        GroupMessage.query.filter_by(group_id=group.id).delete(synchronize_session=False)
        GroupReadState.query.filter_by(group_id=group.id).delete(synchronize_session=False)
        GroupMembership.query.filter_by(group_id=group.id).delete(synchronize_session=False)
        db.session.delete(group)

    AuditLog.query.filter(
        (AuditLog.actor_id == user.id) | (AuditLog.target_user_id == user.id)
    ).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    NotificationPreference.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    UserPermission.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ScheduledTransfer.query.filter(
        (ScheduledTransfer.sender_id == user.id) | (ScheduledTransfer.receiver_id == user.id)
    ).delete(synchronize_session=False)
    Beneficiary.query.filter(
        (Beneficiary.owner_id == user.id) | (Beneficiary.beneficiary_id == user.id)
    ).delete(synchronize_session=False)
    SupportMessage.query.filter_by(author_id=user.id).delete(synchronize_session=False)

    ticket_ids = [t.id for t in SupportTicket.query.filter_by(user_id=user.id).all()]
    if ticket_ids:
        SupportMessage.query.filter(SupportMessage.ticket_id.in_(ticket_ids)).delete(synchronize_session=False)
    SupportTicket.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    bill_ids = [b.id for b in BillPayment.query.filter_by(user_id=user.id).all()]
    if bill_ids:
        BillPaymentLedgerEntry.query.filter(BillPaymentLedgerEntry.bill_payment_id.in_(bill_ids)).delete(synchronize_session=False)
    BillPayment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    BillPaymentLedgerEntry.query.filter_by(account_id=user.id).delete(synchronize_session=False)
    UserRole.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    UserProfile.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    db.session.delete(user)
    db.session.commit()
    flash(f"{username} has been deleted.", "success")
    return redirect(url_for("admin.admin_panel"))


# ---------------- Privileged transfers ----------------

@bp.route("/admin/transfer", methods=["POST"])
@bp.route("/admin/privileged-transfer", methods=["POST"])
@permission_required("privileged_transfer")
def admin_transfer():
    if request.path == "/admin/privileged-transfer":
        if not request.form.get("reason", "").strip():
            flash("A transfer reason is required.", "error")
            return redirect(url_for("admin.admin_panel"))
        return request_privileged_transfer()

    source_username = (request.form.get("source_username") or request.form.get("from_username") or "").strip()
    receiver_username = (request.form.get("receiver_username") or request.form.get("to_username") or "").strip()
    reason = request.form.get("reason", "").strip()

    source = User.query.filter_by(username=source_username).first()
    receiver = User.query.filter_by(username=receiver_username).first()

    if not source or not source.is_enabled or not receiver or not receiver.is_enabled:
        flash("Both source and recipient must be enabled accounts.", "error")
        return redirect(url_for("admin.admin_panel"))
    if source.id == receiver.id:
        flash("Source and recipient must be different accounts.", "error")
        return redirect(url_for("admin.admin_panel"))
    if not reason:
        flash("A transfer reason is required.", "error")
        return redirect(url_for("admin.admin_panel"))

    try:
        amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        flash("Enter a valid amount.", "error")
        return redirect(url_for("admin.admin_panel"))

    if amount <= 0:
        flash("Amount must be greater than zero.", "error")
        return redirect(url_for("admin.admin_panel"))

    source_balance = Decimal(source.balance)
    if amount > source_balance:
        flash("Insufficient checking balance in the source account.", "error")
        return redirect(url_for("admin.admin_panel"))

    duplicate = Transaction.query.filter(
        Transaction.sender_id == source.id,
        Transaction.receiver_id == receiver.id,
        Transaction.amount == amount,
        Transaction.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60),
        Transaction.status == "completed",
    ).first()
    if duplicate:
        flash("A matching privileged transfer was submitted recently.", "error")
        return redirect(url_for("admin.admin_panel"))

    source.balance = source_balance - amount
    receiver.balance = Decimal(receiver.balance) + amount

    transaction = Transaction(
        sender_id=source.id,
        receiver_id=receiver.id,
        amount=amount,
        note=f"Privileged transfer: {reason[:180]}",
    )
    db.session.add(transaction)
    db.session.flush()
    add_ledger_entries(transaction)
    create_notification(
        receiver.id,
        "Privileged funds received",
        f"A management transfer credited {money(amount)} to your account.",
    )
    record_audit(
        "privileged_transfer",
        receiver.id,
        f"source={source.username}; amount={amount}; reason={reason[:180]}",
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("The privileged transfer could not be completed.", "error")
        return redirect(url_for("admin.admin_panel"))

    send_live_update(source.id, "balance_changed", checking_balance=str(source.balance))
    send_live_update(receiver.id, "money_received", transaction_id=transaction.id)
    flash("Privileged transfer completed and recorded.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/privileged-transfer/request", methods=["POST"])
@permission_required("privileged_transfer")
def request_privileged_transfer():
    source = User.query.filter_by(
        username=(request.form.get("source_username") or request.form.get("from_username") or "").strip()
    ).first()
    receiver = User.query.filter_by(
        username=(request.form.get("receiver_username") or request.form.get("to_username") or "").strip()
    ).first()
    reason = request.form.get("reason", "").strip()[:200]

    try:
        amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0.00")

    if (not source or not receiver or not source.is_enabled or not receiver.is_enabled
            or source.id == receiver.id or amount <= 0 or not reason):
        flash("Enabled, different accounts, a positive amount, and a reason are required.", "error")
        return redirect(url_for("admin.admin_panel"))

    duplicate = Transaction.query.filter(
        Transaction.sender_id == source.id,
        Transaction.receiver_id == receiver.id,
        Transaction.amount == amount,
        Transaction.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60),
        Transaction.status == "completed",
    ).first()
    pending_duplicate = PrivilegedTransferRequest.query.filter(
        PrivilegedTransferRequest.actor_id == current_user.id,
        PrivilegedTransferRequest.source_id == source.id,
        PrivilegedTransferRequest.receiver_id == receiver.id,
        PrivilegedTransferRequest.amount == amount,
        PrivilegedTransferRequest.status == "pending",
    ).first()

    if duplicate or pending_duplicate:
        flash("A matching privileged transfer is already recent or pending.", "error")
        return redirect(url_for("admin.admin_panel"))

    request_record = PrivilegedTransferRequest(
        actor_id=current_user.id,
        source_id=source.id,
        receiver_id=receiver.id,
        amount=amount,
        reason=reason,
    )
    db.session.add(request_record)
    db.session.flush()
    record_audit(
        "privileged_transfer_requested",
        receiver.id,
        f"request={request_record.id}; source={source.username}; reason={reason}",
    )
    db.session.commit()

    create_notification(
        current_user.id,
        "Transfer awaiting approval",
        f"Privileged transfer request #{request_record.id} is pending.",
    )
    db.session.commit()
    flash("Privileged transfer submitted for approval.", "success")
    return redirect(url_for("admin.admin_panel"))


def _approve_privileged_transfer(request_record):
    source = db.session.get(User, request_record.source_id)
    receiver = db.session.get(User, request_record.receiver_id)

    if (request_record.status != "pending" or not source or not receiver
            or not source.is_enabled or not receiver.is_enabled
            or request_record.amount > Decimal(source.balance)):
        return False

    duplicate = Transaction.query.filter(
        Transaction.sender_id == source.id,
        Transaction.receiver_id == receiver.id,
        Transaction.amount == request_record.amount,
        Transaction.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60),
        Transaction.status == "completed",
    ).first()
    if duplicate:
        return False

    source.balance = Decimal(source.balance) - request_record.amount
    receiver.balance = Decimal(receiver.balance) + request_record.amount

    transaction = Transaction(
        sender_id=source.id,
        receiver_id=receiver.id,
        amount=request_record.amount,
        note=f"Approved privileged transfer: {request_record.reason[:160]}",
    )
    db.session.add(transaction)
    db.session.flush()
    add_ledger_entries(transaction)

    request_record.status = "approved"
    request_record.approved_by_id = current_user.id
    request_record.transaction_id = transaction.id
    request_record.decided_at = datetime.now(timezone.utc)

    record_audit(
        "privileged_transfer_approved",
        receiver.id,
        f"request={request_record.id}; reason={request_record.reason}",
    )
    create_notification(
        receiver.id,
        "Approved funds received",
        f"An approved management transfer credited {money(request_record.amount)}.",
    )
    return True


@bp.route("/admin/privileged-transfer/<int:request_id>/approve", methods=["POST"])
@permission_required("approve_transfer")
def approve_privileged_transfer_route(request_id):
    request_record = db.session.get(PrivilegedTransferRequest, request_id)
    if not request_record:
        abort(404)
    if not _approve_privileged_transfer(request_record):
        db.session.rollback()
        flash("That request is no longer valid or the source lacks funds.", "error")
        return redirect(url_for("admin.admin_panel"))
    db.session.commit()
    flash("Privileged transfer approved.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/privileged-transfer/<int:request_id>/reject", methods=["POST"])
@permission_required("approve_transfer")
def reject_privileged_transfer(request_id):
    request_record = db.session.get(PrivilegedTransferRequest, request_id)
    if not request_record:
        abort(404)
    if request_record.status != "pending":
        flash("That request has already been decided.", "error")
        return redirect(url_for("admin.admin_panel"))

    request_record.status = "rejected"
    request_record.approved_by_id = current_user.id
    request_record.decided_at = datetime.now(timezone.utc)
    record_audit("privileged_transfer_rejected", request_record.receiver_id, f"request={request_id}")
    db.session.commit()
    flash("Privileged transfer rejected.", "success")
    return redirect(url_for("admin.admin_panel"))


@bp.route("/admin/check-deposit", methods=["POST"])
@permission_required("check_deposit")
def deposit_demo_check():
    source = User.query.filter_by(
        username=(request.form.get("source_username") or request.form.get("from_username") or "").strip()
    ).first()
    recipient = User.query.filter_by(
        username=(request.form.get("recipient_username") or request.form.get("to_username") or "").strip()
    ).first()
    check_number = request.form.get("check_number", "").strip()[:80]
    reference = request.form.get("reference", "").strip()[:200]

    try:
        amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0.00")

    if (not source or not recipient or not source.is_enabled or not recipient.is_enabled
            or source.id == recipient.id or amount <= 0 or not check_number or not reference):
        flash("Enabled, different accounts, a positive amount, check number, and reference are required.", "error")
        return redirect(url_for("admin.admin_panel"))

    if Decimal(source.balance) < amount:
        flash("The check writer has insufficient checking funds.", "error")
        return redirect(url_for("admin.admin_panel"))

    if CheckDeposit.query.filter_by(source_id=source.id, check_number=check_number).first():
        flash("That check number has already been deposited for this account.", "error")
        return redirect(url_for("admin.admin_panel"))

    source.balance = Decimal(source.balance) - amount
    recipient.balance = Decimal(recipient.balance) + amount

    transaction = Transaction(
        sender_id=source.id,
        receiver_id=recipient.id,
        amount=amount,
        note=f"Demo check {check_number}: {reference}"[:200],
        cancellable_until=datetime.now(timezone.utc),
    )
    db.session.add(transaction)
    db.session.flush()
    add_ledger_entries(transaction)

    deposit = CheckDeposit(
        actor_id=current_user.id,
        source_id=source.id,
        recipient_id=recipient.id,
        transaction_id=transaction.id,
        amount=amount,
        check_number=check_number,
        reference=reference,
    )
    db.session.add(deposit)
    create_notification(
        recipient.id,
        "Demo check deposited",
        f"{money(amount)} from @{source.username} was credited to your checking account.",
    )
    record_audit(
        "demo_check_deposited",
        recipient.id,
        f"source={source.username}; amount={amount}; check={check_number}; reference={reference}",
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("The demo check could not be posted.", "error")
        return redirect(url_for("admin.admin_panel"))

    send_live_update(recipient.id, "money_received", transaction_id=transaction.id, source="demo_check")
    flash("Demo check posted and credited atomically.", "success")
    return redirect(url_for("admin.admin_panel"))
