import os
import secrets
import csv
import io
import re
import hashlib

from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python versions without zoneinfo
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
    session,
    Response,
)

from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)

from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash


# =========================================================
# APP SETUP
# =========================================================

app = Flask(__name__)

secret_key = os.environ.get("SECRET_KEY", "").strip()
if not secret_key:
    raise RuntimeError(
        "SECRET_KEY must be set to a long random value before starting MiniBank."
    )
app.config["SECRET_KEY"] = secret_key

database_url = os.environ.get(
    "DATABASE_URL",
    "sqlite:///demo_bank.db",
)

if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql+psycopg://",
        1,
    )

elif database_url.startswith("postgresql://"):
    database_url = database_url.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1,
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Timestamps are stored as UTC-aware values.  Rendering and user-entered
# schedule times use Singapore time consistently throughout the demo.
try:
    SINGAPORE_TZ = ZoneInfo("Asia/Singapore") if ZoneInfo else None
except ZoneInfoNotFoundError:  # Windows hosts may not have an IANA tz database.
    SINGAPORE_TZ = None
if SINGAPORE_TZ is None:
    SINGAPORE_TZ = timezone(timedelta(hours=8), name="SGT")
DISPLAY_TIMEZONE = "SGT / Asia/Singapore (UTC+8)"


db = SQLAlchemy(app)

# Flask-SocketIO manages the live browser connections.
socketio = SocketIO(
    app,
    async_mode="threading",
)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "error"


def to_utc(value):
    """Normalize stored or submitted datetimes to an aware UTC value."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_singapore_time(value, pattern="%d %b %Y, %H:%M SGT"):
    """Format a datetime for display in Singapore time (Asia/Singapore)."""
    if not value:
        return ""
    return to_utc(value).astimezone(SINGAPORE_TZ).strftime(pattern)


app.jinja_env.filters["sgt"] = format_singapore_time


@app.context_processor
def inject_display_timezone():
    return {"display_timezone": DISPLAY_TIMEZONE}


# =========================================================
# DATABASE MODELS
# =========================================================

class User(UserMixin, db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    username = db.Column(
        db.String(80),
        unique=True,
        nullable=False,
        index=True,
    )

    display_name = db.Column(
        db.String(120),
        nullable=False,
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False,
    )

    balance = db.Column(
        db.Numeric(12, 2),
        default=Decimal("0.00"),
        nullable=False,
    )

    is_admin = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
    )

    account_type = db.Column(
        db.String(20),
        default="standard",
        nullable=False,
    )

    is_enabled = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
    )

    @property
    def is_active(self):
        return self.is_enabled

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(
            self.password_hash,
            password,
        )

    @property
    def has_management_access(self):
        return bool(self.roles.intersection({"developer", "admin"}))

    @property
    def roles(self):
        """Return composable roles while honoring legacy role columns."""
        assigned_roles = {
            item.role for item in UserRole.query.filter_by(user_id=self.id).all()
        }
        if self.account_type in {"developer", "admin"}:
            assigned_roles.add(self.account_type)
        if self.is_admin:
            assigned_roles.add("admin")
        return assigned_roles

    def has_permission(self, permission):
        if self.has_management_access and permission in {
            "balance_read",
            "check_deposit",
            "impersonate",
            "privileged_transfer",
            "approve_transfer",
            "audit_read",
            "manage_groups",
            "support_manage",
        }:
            return True
        return UserPermission.query.filter_by(
            user_id=self.id,
            permission=permission,
        ).first() is not None


class UserProfile(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        unique=True,
        nullable=False,
    )

    account_number = db.Column(
        db.String(16),
        unique=True,
        nullable=False,
        index=True,
    )

    profile_image_url = db.Column(
        db.String(1000),
        default="",
        nullable=False,
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
    )


class Transaction(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    sender_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    receiver_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    amount = db.Column(
        db.Numeric(12, 2),
        nullable=False,
    )

    note = db.Column(
        db.String(200),
        default="",
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    status = db.Column(db.String(20), nullable=False, default="completed")
    cancellable_until = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc) + timedelta(minutes=5),
        nullable=False,
    )

    sender = db.relationship(
        "User",
        foreign_keys=[sender_id],
    )

    receiver = db.relationship(
        "User",
        foreign_keys=[receiver_id],
    )


class Message(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    sender_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    receiver_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    content = db.Column(
        db.String(1000),
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    sender = db.relationship(
        "User",
        foreign_keys=[sender_id],
    )

    receiver = db.relationship(
        "User",
        foreign_keys=[receiver_id],
    )


class MessageReadState(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    other_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
    )

    last_read_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "other_user_id",
            name="unique_message_read_state",
        ),
    )


class GroupChat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    creator = db.relationship("User", foreign_keys=[creator_id])


class GroupMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group_chat.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="member")
    joined_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    group = db.relationship("GroupChat", foreign_keys=[group_id])
    user = db.relationship("User", foreign_keys=[user_id])
    __table_args__ = (
        db.UniqueConstraint("group_id", "user_id", name="unique_group_membership"),
    )


class GroupMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group_chat.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    group = db.relationship("GroupChat", foreign_keys=[group_id])
    sender = db.relationship("User", foreign_keys=[sender_id])


class GroupReadState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group_chat.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    last_read_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    __table_args__ = (
        db.UniqueConstraint("group_id", "user_id", name="unique_group_read_state"),
    )


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    details = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    actor = db.relationship("User", foreign_keys=[actor_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])


class UserRole(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role = db.Column(db.String(30), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("user_id", "role", name="unique_user_role"),
    )


class UserPermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    permission = db.Column(db.String(80), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("user_id", "permission", name="unique_user_permission"),
    )


class DeveloperApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    key_prefix = db.Column(db.String(12), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False, unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    user = db.relationship("User", foreign_keys=[user_id])


class DeveloperFeatureFlag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])


class PrivilegedTransferRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    source_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    decided_at = db.Column(db.DateTime(timezone=True), nullable=True)
    actor = db.relationship("User", foreign_keys=[actor_id])
    source = db.relationship("User", foreign_keys=[source_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])


class LedgerEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    debit = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    credit = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    transaction = db.relationship("Transaction", foreign_keys=[transaction_id])
    account = db.relationship("User", foreign_keys=[account_id])


class ScheduledTransfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    note = db.Column(db.String(200), nullable=False, default="")
    next_run_at = db.Column(db.DateTime(timezone=True), nullable=False)
    interval_days = db.Column(db.Integer, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class Beneficiary(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    beneficiary_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    nickname = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    owner = db.relationship("User", foreign_keys=[owner_id])
    beneficiary = db.relationship("User", foreign_keys=[beneficiary_id])
    __table_args__ = (
        db.UniqueConstraint("owner_id", "beneficiary_id", name="unique_beneficiary"),
    )


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.String(500), nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", foreign_keys=[user_id])


class NotificationPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    email_enabled = db.Column(db.Boolean, nullable=False, default=False)
    sms_enabled = db.Column(db.Boolean, nullable=False, default=False)
    email_address = db.Column(db.String(160), nullable=False, default="")
    phone_number = db.Column(db.String(40), nullable=False, default="")


class MessageReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    emoji = db.Column(db.String(16), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("message_id", "user_id", name="unique_message_reaction"),
    )


class GroupMessageReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_message_id = db.Column(db.Integer, db.ForeignKey("group_message.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    emoji = db.Column(db.String(16), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("group_message_id", "user_id", name="unique_group_reaction"),
    )


class MessageAttachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    group_message_id = db.Column(db.Integer, db.ForeignKey("group_message.id"), nullable=True)
    uploader_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    url = db.Column(db.String(1000), nullable=False)
    filename = db.Column(db.String(160), nullable=False, default="attachment")
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MessageMention(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    group_message_id = db.Column(db.Integer, db.ForeignKey("group_message.id"), nullable=True)
    mentioned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MessageReceipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    group_message_id = db.Column(db.Integer, db.ForeignKey("group_message.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    read_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SupportTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    subject = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="open")
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", foreign_keys=[user_id])


class SupportMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("support_ticket.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class BillPayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    payee = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    reference = db.Column(db.String(120), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="paid")
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", foreign_keys=[user_id])


class BillPaymentLedgerEntry(db.Model):
    """Double-entry clearing records for the local bill-payment simulator."""

    id = db.Column(db.Integer, primary_key=True)
    bill_payment_id = db.Column(
        db.Integer,
        db.ForeignKey("bill_payment.id"),
        nullable=False,
    )
    account_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    account_label = db.Column(
        db.String(80),
        nullable=False,
        default="Demo biller clearing",
    )
    debit = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    credit = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class CheckDeposit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    source_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transaction.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    check_number = db.Column(db.String(80), nullable=False)
    reference = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="posted")
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    actor = db.relationship("User", foreign_keys=[actor_id])
    source = db.relationship("User", foreign_keys=[source_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])
    transaction = db.relationship("Transaction", foreign_keys=[transaction_id])
    __table_args__ = (
        db.UniqueConstraint(
            "source_id",
            "check_number",
            name="unique_source_check_number",
        ),
    )


# =========================================================
# LOGIN
# =========================================================

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(
            User,
            int(user_id),
        )
    except (TypeError, ValueError):
        return None


# =========================================================
# ADMIN PROTECTION
# =========================================================

def admin_required(function):
    @wraps(function)
    @login_required
    def wrapped(*args, **kwargs):
        # Never allow an impersonated session to use management capabilities.
        # This prevents impersonating another management account from becoming
        # a privilege escalation path; the actor can always use the banner to
        # return to the management session.
        if is_impersonating() or not current_user.has_management_access:
            abort(403)

        return function(*args, **kwargs)

    return wrapped


def permission_required(permission):
    def decorator(function):
        @wraps(function)
        @login_required
        def wrapped(*args, **kwargs):
            if is_impersonating() or not current_user.has_permission(permission):
                abort(403)
            return function(*args, **kwargs)

        return wrapped
    return decorator


def developer_required(function):
    @wraps(function)
    @login_required
    def wrapped(*args, **kwargs):
        if is_impersonating() or "developer" not in current_user.roles:
            abort(403)
        return function(*args, **kwargs)

    return wrapped


def record_audit(action, target_user_id=None, details="", actor_id=None):
    actor_id = actor_id or session.get("impersonator_id", current_user.id)
    db.session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_user_id=target_user_id,
            details=str(details)[:2000],
        )
    )


def is_impersonating():
    return bool(session.get("impersonator_id"))


def get_impersonator():
    actor_id = session.get("impersonator_id")
    if not actor_id:
        return None
    actor = db.session.get(User, actor_id)
    if not actor or not actor.is_enabled or not actor.has_permission("impersonate"):
        return None
    return actor


def create_notification(user_id, title, body):
    notification = Notification(
        user_id=user_id,
        title=str(title)[:160],
        body=str(body)[:500],
    )
    db.session.add(notification)
    db.session.flush()
    send_live_update(user_id, "notification", notification_id=notification.id)
    return notification


def add_ledger_entries(transaction):
    db.session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=transaction.sender_id,
            debit=transaction.amount,
            credit=Decimal("0.00"),
        )
    )
    db.session.add(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=transaction.receiver_id,
            debit=Decimal("0.00"),
            credit=transaction.amount,
        )
    )


def parse_datetime(value, default=None):
    if not value:
        return default
    try:
        submitted = datetime.fromisoformat(value)
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=SINGAPORE_TZ)
        return submitted.astimezone(timezone.utc)
    except ValueError:
        return None


@app.before_request
def validate_impersonation_session():
    """Keep stale or unsafe impersonation sessions from surviving requests."""
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
        return redirect(url_for("login"))

    if not target_is_valid:
        # A disabled/deleted target must not leave the browser stranded in an
        # invalid session. Restore the still-valid management account instead.
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
        return redirect(url_for("admin_panel"))

    return None


def group_membership(group_id, user_id=None):
    return GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id if user_id is None else user_id,
    ).first()


def group_is_manager(group_id, user_id):
    membership = group_membership(group_id, user_id)
    return bool(membership and membership.role in {"owner", "moderator"})


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def generate_account_number():
    while True:
        number = "88" + "".join(
            secrets.choice("0123456789")
            for _ in range(10)
        )

        existing_profile = UserProfile.query.filter_by(
            account_number=number,
        ).first()

        if not existing_profile:
            return number


def apply_user_roles(user, roles):
    """Persist composable management roles and legacy compatibility fields."""
    selected = {role for role in roles if role in {"admin", "developer"}}
    UserRole.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    for role in sorted(selected):
        db.session.add(UserRole(user_id=user.id, role=role))
    if "admin" in selected:
        user.account_type = "admin"
        user.is_admin = True
    elif "developer" in selected:
        user.account_type = "developer"
        user.is_admin = False
    else:
        user.account_type = "standard"
        user.is_admin = False


def get_or_create_profile(user):
    profile = UserProfile.query.filter_by(
        user_id=user.id,
    ).first()

    if not profile:
        profile = UserProfile(
            user_id=user.id,
            account_number=generate_account_number(),
        )

        db.session.add(profile)
        db.session.commit()

    return profile


def user_room(user_id):
    return f"user_{int(user_id)}"


def send_live_update(user_id, update_type, **data):
    """Send an update only to browsers logged in as this user."""
    socketio.emit(
        "minibank_update",
        {
            "type": update_type,
            **data,
        },
        to=user_room(user_id),
    )


@socketio.on("connect")
def socket_connect():
    if not current_user.is_authenticated or not current_user.is_enabled:
        return False

    join_room(user_room(current_user.id))

    if current_user.has_management_access:
        join_room("admins")


# =========================================================
# TEMPLATE HELPERS
# =========================================================

@app.template_filter("money")
def money(value):
    try:
        return f"${Decimal(value):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return "$0.00"


@app.context_processor
def global_template_data():
    if not current_user.is_authenticated:
        return {
            "unread_message_count": 0,
            "unread_group_message_count": 0,
            "unread_notification_count": 0,
            "current_profile": None,
            "impersonator": None,
        }

    unread_count = 0

    senders = (
        db.session.query(Message.sender_id)
        .filter(
            Message.receiver_id == current_user.id
        )
        .distinct()
        .all()
    )

    for sender_row in senders:
        sender_id = sender_row[0]

        state = MessageReadState.query.filter_by(
            user_id=current_user.id,
            other_user_id=sender_id,
        ).first()

        unread_query = Message.query.filter(
            Message.sender_id == sender_id,
            Message.receiver_id == current_user.id,
        )

        if state:
            unread_query = unread_query.filter(
                Message.created_at > state.last_read_at
            )

        unread_count += unread_query.count()

    group_unread_count = 0
    memberships = GroupMembership.query.filter_by(user_id=current_user.id).all()
    for membership in memberships:
        state = GroupReadState.query.filter_by(
            user_id=current_user.id,
            group_id=membership.group_id,
        ).first()
        unread_query = GroupMessage.query.filter(
            GroupMessage.group_id == membership.group_id,
            GroupMessage.sender_id != current_user.id,
        )
        if state:
            unread_query = unread_query.filter(
                GroupMessage.created_at > state.last_read_at
            )
        group_unread_count += unread_query.count()

    unread_notification_count = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False,
    ).count()

    return {
        "unread_message_count": unread_count,
        "unread_group_message_count": group_unread_count,
        "unread_notification_count": unread_notification_count,
        "impersonator": get_impersonator(),
        "current_profile": get_or_create_profile(current_user),
    }


# =========================================================
# HOME AND LOGIN ROUTES
# =========================================================

@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get(
            "username",
            "",
        ).strip()

        password = request.form.get(
            "password",
            "",
        )

        user = User.query.filter_by(
            username=username,
        ).first()

        if (
            user
            and user.is_enabled
            and user.check_password(password)
        ):
            login_user(user)

            get_or_create_profile(user)

            return redirect(url_for("dashboard"))

        flash(
            "Incorrect username or password.",
            "error",
        )

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
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
        return redirect(url_for("admin_panel"))

    logout_user()

    return redirect(url_for("login"))


@app.route("/admin/impersonate/<int:user_id>", methods=["POST"])
@permission_required("impersonate")
def start_impersonation(user_id):
    target = db.session.get(User, user_id)
    if not target:
        abort(404)
    if not target.is_enabled:
        flash("Only enabled accounts can be impersonated.", "error")
        return redirect(url_for("admin_panel"))
    if target.id == current_user.id:
        flash("You are already signed in as this account.", "error")
        return redirect(url_for("admin_panel"))
    reason = request.form.get("reason", "").strip()[:200]
    if not reason:
        flash("An impersonation reason is required.", "error")
        return redirect(url_for("admin_panel"))

    # Resolve the proxy before changing the login session.  Flask-Login's
    # ``current_user`` proxy otherwise resolves to the target after login_user.
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
    return redirect(url_for("dashboard"))


@app.route("/admin/stop-impersonation", methods=["POST"])
@login_required
def stop_impersonation():
    actor_id = session.get("impersonator_id")
    actor = db.session.get(User, actor_id) if actor_id else None
    if not actor or not actor.is_enabled or not actor.has_permission("impersonate"):
        session.clear()
        logout_user()
        flash("The management session is no longer available.", "error")
        return redirect(url_for("login"))

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
    return redirect(url_for("admin_panel"))


# =========================================================
# DASHBOARD AND PROFILE
# =========================================================

@app.route("/dashboard")
@login_required
def dashboard():
    transactions = (
        Transaction.query
        .filter(
            (Transaction.sender_id == current_user.id)
            | (Transaction.receiver_id == current_user.id)
        )
        .order_by(Transaction.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "dashboard.html",
        transactions=transactions,
    )


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_profile = get_or_create_profile(current_user)

    if request.method == "POST":
        name = request.form.get(
            "display_name",
            "",
        ).strip()

        image = request.form.get(
            "profile_image_url",
            "",
        ).strip()

        if len(name) < 2 or len(name) > 120:
            flash(
                "Display name must be between 2 and 120 characters.",
                "error",
            )

            return redirect(url_for("profile"))

        if (
            len(image) > 1000
            or (
                image
                and not image.startswith(
                    ("http://", "https://")
                )
            )
        ):
            flash(
                "Profile picture must use a valid http or https URL.",
                "error",
            )

            return redirect(url_for("profile"))

        current_user.display_name = name
        user_profile.profile_image_url = image

        db.session.commit()

        flash(
            "Profile updated.",
            "success",
        )

        return redirect(url_for("profile"))

    return render_template(
        "profile.html",
        user_profile=user_profile,
    )


# =========================================================
# ACCOUNTS
# =========================================================

@app.route("/accounts")
@login_required
def accounts():
    return render_template("accounts.html")


# =========================================================
# USER-TO-USER TRANSFERS
# =========================================================

@app.route("/transfer", methods=["GET", "POST"])
@login_required
def transfer():
    if request.method == "POST":
        beneficiary_id = request.form.get("beneficiary_id", "").strip()
        receiver = None
        if beneficiary_id:
            try:
                beneficiary = Beneficiary.query.filter_by(
                    id=int(beneficiary_id),
                    owner_id=current_user.id,
                ).first()
            except (TypeError, ValueError):
                beneficiary = None
            if beneficiary:
                receiver = beneficiary.beneficiary
        receiver_username = request.form.get("receiver_username", "").strip()
        if receiver is None:
            receiver = User.query.filter_by(username=receiver_username).first()

        if not receiver or not receiver.is_enabled:
            flash(
                "Recipient not found.",
                "error",
            )

            return redirect(url_for("transfer"))

        if receiver.id == current_user.id:
            flash(
                "You cannot transfer to yourself.",
                "error",
            )

            return redirect(url_for("transfer"))

        try:
            amount = Decimal(
                request.form.get("amount", "")
            ).quantize(Decimal("0.01"))

        except (InvalidOperation, ValueError):
            flash(
                "Enter a valid amount.",
                "error",
            )

            return redirect(url_for("transfer"))

        if amount <= 0:
            flash(
                "Amount must be greater than zero.",
                "error",
            )

            return redirect(url_for("transfer"))

        if amount > Decimal(current_user.balance):
            flash(
                "Insufficient checking balance.",
                "error",
            )

            return redirect(url_for("transfer"))

        note = request.form.get(
            "note",
            "",
        ).strip()[:200]

        duplicate_cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        duplicate = Transaction.query.filter(
            Transaction.sender_id == current_user.id,
            Transaction.receiver_id == receiver.id,
            Transaction.amount == amount,
            Transaction.note == note,
            Transaction.created_at >= duplicate_cutoff,
            Transaction.status == "completed",
        ).first()
        if duplicate:
            flash("A matching transfer was submitted recently.", "error")
            return redirect(url_for("transfer"))

        current_user.balance = Decimal(current_user.balance) - amount
        receiver.balance = Decimal(receiver.balance) + amount
        transaction = Transaction(
            sender_id=current_user.id,
            receiver_id=receiver.id,
            amount=amount,
            note=note,
        )

        db.session.add(transaction)
        db.session.flush()
        add_ledger_entries(transaction)
        create_notification(
            receiver.id,
            "Money received",
            f"{current_user.username} sent you {money(amount)}.",
        )
        db.session.commit()

        transaction_data = {
            "transaction_id": transaction.id,
            "sender_id": transaction.sender_id,
            "receiver_id": transaction.receiver_id,
            "amount": str(transaction.amount),
        }

        send_live_update(
            current_user.id,
            "transfer_completed",
            **transaction_data,
        )
        send_live_update(
            receiver.id,
            "money_received",
            **transaction_data,
        )

        return redirect(
            url_for(
                "transfer_receipt",
                transaction_id=transaction.id,
            )
        )

    return render_template(
        "transfer.html",
        beneficiaries=Beneficiary.query.filter_by(
            owner_id=current_user.id
        ).order_by(Beneficiary.nickname.asc()).all(),
    )


@app.route("/admin/transfer", methods=["POST"])
@app.route("/admin/privileged-transfer", methods=["POST"])
@permission_required("privileged_transfer")
def admin_transfer():
    if request.path == "/admin/privileged-transfer":
        if not request.form.get("reason", "").strip():
            flash("A transfer reason is required.", "error")
            return redirect(url_for("admin_panel"))
        return request_privileged_transfer()
    source_username = (
        request.form.get("source_username")
        or request.form.get("from_username")
        or ""
    ).strip()
    receiver_username = (
        request.form.get("receiver_username")
        or request.form.get("to_username")
        or ""
    ).strip()
    reason = request.form.get("reason", "").strip()

    source = User.query.filter_by(username=source_username).first()
    receiver = User.query.filter_by(username=receiver_username).first()
    if not source or not source.is_enabled or not receiver or not receiver.is_enabled:
        flash("Both source and recipient must be enabled accounts.", "error")
        return redirect(url_for("admin_panel"))
    if source.id == receiver.id:
        flash("Source and recipient must be different accounts.", "error")
        return redirect(url_for("admin_panel"))
    if not reason:
        flash("A transfer reason is required.", "error")
        return redirect(url_for("admin_panel"))

    try:
        amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        flash("Enter a valid amount.", "error")
        return redirect(url_for("admin_panel"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "error")
        return redirect(url_for("admin_panel"))

    source_balance = Decimal(source.balance)
    if amount > source_balance:
        flash("Insufficient checking balance in the source account.", "error")
        return redirect(url_for("admin_panel"))
    duplicate = Transaction.query.filter(
        Transaction.sender_id == source.id,
        Transaction.receiver_id == receiver.id,
        Transaction.amount == amount,
        Transaction.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60),
        Transaction.status == "completed",
    ).first()
    if duplicate:
        flash("A matching privileged transfer was submitted recently.", "error")
        return redirect(url_for("admin_panel"))

    # One transaction covers both balance changes, the transaction, and the
    # audit event.  A failed commit is rolled back so no in-memory mutation is
    # accidentally persisted by a later request.
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
        return redirect(url_for("admin_panel"))

    send_live_update(source.id, "balance_changed", checking_balance=str(source.balance))
    send_live_update(receiver.id, "money_received", transaction_id=transaction.id)
    flash("Privileged transfer completed and recorded.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/check-deposit", methods=["POST"])
@permission_required("check_deposit")
def deposit_demo_check():
    source = User.query.filter_by(
        username=(
            request.form.get("source_username")
            or request.form.get("from_username")
            or ""
        ).strip()
    ).first()
    recipient = User.query.filter_by(
        username=(
            request.form.get("recipient_username")
            or request.form.get("to_username")
            or ""
        ).strip()
    ).first()
    check_number = request.form.get("check_number", "").strip()[:80]
    reference = request.form.get("reference", "").strip()[:200]
    try:
        amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        amount = Decimal("0.00")

    if (
        not source
        or not recipient
        or not source.is_enabled
        or not recipient.is_enabled
        or source.id == recipient.id
        or amount <= 0
        or not check_number
        or not reference
    ):
        flash(
            "Enabled, different accounts, a positive amount, check number, and reference are required.",
            "error",
        )
        return redirect(url_for("admin_panel"))
    if Decimal(source.balance) < amount:
        flash("The check writer has insufficient checking funds.", "error")
        return redirect(url_for("admin_panel"))
    if CheckDeposit.query.filter_by(
        source_id=source.id,
        check_number=check_number,
    ).first():
        flash("That check number has already been deposited for this account.", "error")
        return redirect(url_for("admin_panel"))

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
        return redirect(url_for("admin_panel"))
    send_live_update(
        recipient.id,
        "money_received",
        transaction_id=transaction.id,
        source="demo_check",
    )
    flash("Demo check posted and credited atomically.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/privileged-transfer/request", methods=["POST"])
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
    if (
        not source
        or not receiver
        or not source.is_enabled
        or not receiver.is_enabled
        or source.id == receiver.id
        or amount <= 0
        or not reason
    ):
        flash("Enabled, different accounts, a positive amount, and a reason are required.", "error")
        return redirect(url_for("admin_panel"))
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
        return redirect(url_for("admin_panel"))
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
    return redirect(url_for("admin_panel"))


def approve_privileged_transfer(request_record):
    source = db.session.get(User, request_record.source_id)
    receiver = db.session.get(User, request_record.receiver_id)
    if (
        request_record.status != "pending"
        or not source
        or not receiver
        or not source.is_enabled
        or not receiver.is_enabled
        or request_record.amount > Decimal(source.balance)
    ):
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


@app.route("/admin/privileged-transfer/<int:request_id>/approve", methods=["POST"])
@permission_required("approve_transfer")
def approve_privileged_transfer_route(request_id):
    request_record = db.session.get(PrivilegedTransferRequest, request_id)
    if not request_record:
        abort(404)
    if not approve_privileged_transfer(request_record):
        db.session.rollback()
        flash("That request is no longer valid or the source lacks funds.", "error")
        return redirect(url_for("admin_panel"))
    db.session.commit()
    flash("Privileged transfer approved.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/privileged-transfer/<int:request_id>/reject", methods=["POST"])
@permission_required("approve_transfer")
def reject_privileged_transfer(request_id):
    request_record = db.session.get(PrivilegedTransferRequest, request_id)
    if not request_record:
        abort(404)
    if request_record.status != "pending":
        flash("That request has already been decided.", "error")
        return redirect(url_for("admin_panel"))
    request_record.status = "rejected"
    request_record.approved_by_id = current_user.id
    request_record.decided_at = datetime.now(timezone.utc)
    record_audit(
        "privileged_transfer_rejected",
        request_record.receiver_id,
        f"request={request_id}",
    )
    db.session.commit()
    flash("Privileged transfer rejected.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/receipt/<int:transaction_id>")
@login_required
def transfer_receipt(transaction_id):
    transaction = db.session.get(
        Transaction,
        transaction_id,
    )

    if not transaction:
        abort(404)

    allowed_user_ids = {
        transaction.sender_id,
        transaction.receiver_id,
    }

    if current_user.id not in allowed_user_ids:
        abort(403)

    reference_number = (
        f"MB-{transaction.id:08d}"
    )

    return render_template(
        "receipt.html",
        transaction=transaction,
        reference_number=reference_number,
    )


# =========================================================
# TRANSACTION HISTORY
# =========================================================

@app.route("/transactions")
@login_required
def transactions():
    search = request.args.get(
        "search",
        "",
    ).strip()

    transaction_type = request.args.get(
        "type",
        "all",
    )

    query = Transaction.query.filter(
        (Transaction.sender_id == current_user.id)
        | (Transaction.receiver_id == current_user.id)
    )

    if transaction_type == "sent":
        query = query.filter(
            Transaction.sender_id == current_user.id
        )

    elif transaction_type == "received":
        query = query.filter(
            Transaction.receiver_id == current_user.id
        )

    if search:
        matching_users = User.query.filter(
            User.username.ilike(f"%{search}%")
            | User.display_name.ilike(f"%{search}%")
        ).all()

        matching_user_ids = [
            user.id
            for user in matching_users
        ]

        search_conditions = [
            Transaction.note.ilike(f"%{search}%")
        ]

        if matching_user_ids:
            search_conditions.extend(
                [
                    Transaction.sender_id.in_(
                        matching_user_ids
                    ),
                    Transaction.receiver_id.in_(
                        matching_user_ids
                    ),
                ]
            )

        query = query.filter(
            db.or_(*search_conditions)
        )

    transaction_list = (
        query
        .order_by(Transaction.created_at.desc())
        .all()
    )

    return render_template(
        "transactions.html",
        transactions=transaction_list,
        search=search,
        transaction_type=transaction_type,
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@app.route("/developer-tools", methods=["GET", "POST"])
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
            flash(f"Feature flag {name} is now {'enabled' if flag.enabled else 'disabled'}.", "success")
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
        return redirect(url_for("developer_tools"))

    flags = {
        name: DeveloperFeatureFlag.query.filter_by(name=name).first()
        for name in ("new_dashboard", "sandbox_payments", "webhook_preview")
    }
    return render_template(
        "developer_tools.html",
        api_keys=DeveloperApiKey.query.filter_by(user_id=current_user.id).order_by(
            DeveloperApiKey.created_at.desc()
        ).all(),
        flags=flags,
        recent_logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(25).all(),
        health={
            "users": User.query.count(),
            "transactions": Transaction.query.count(),
            "audit_events": AuditLog.query.count(),
            "database": "connected",
        },
    )


@app.route("/admin")
@admin_required
def admin_panel():
    users = User.query.order_by(
        User.username.asc()
    ).all()
    permission_map = {
        user.id: {
            item.permission
            for item in UserPermission.query.filter_by(user_id=user.id).all()
        }
        for user in users
    }

    return render_template(
        "admin.html",
        users=users,
        permission_map=permission_map,
        pending_transfer_requests=PrivilegedTransferRequest.query.filter_by(
            status="pending"
        ).order_by(PrivilegedTransferRequest.created_at.asc()).all(),
    )


@app.route("/admin/balances")
@permission_required("balance_read")
def management_balances():
    users = User.query.order_by(User.username.asc()).all()
    record_audit(
        "management_balance_viewed",
        details=f"accounts={len(users)}",
    )
    db.session.commit()
    return render_template("admin_balances.html", users=users)


@app.route("/admin/create-user", methods=["POST"])
@admin_required
def create_user():
    username = request.form.get(
        "username",
        "",
    ).strip()

    display_name = request.form.get(
        "display_name",
        "",
    ).strip()

    password = request.form.get(
        "password",
        "",
    )

    existing_user = User.query.filter_by(
        username=username,
    ).first()

    if len(username) < 3:
        flash(
            "Username must have at least 3 characters.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    if not display_name:
        flash(
            "Enter a display name.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    if len(password) < 8:
        flash(
            "Password must have at least 8 characters.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    if existing_user:
        flash(
            "That username already exists.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    try:
        balance = Decimal(
            request.form.get("balance", "0")
        ).quantize(Decimal("0.01"))

    except (InvalidOperation, ValueError):
        flash(
            "Invalid balance.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    if balance < 0:
        flash(
            "Balance cannot be negative.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    account_type = request.form.get("account_type", "standard").strip()
    if account_type not in {"standard", "developer", "admin"}:
        flash("Choose a valid account type.", "error")
        return redirect(url_for("admin_panel"))
    selected_roles = set(request.form.getlist("role")).intersection(
        {"admin", "developer"}
    )
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

    profile = UserProfile(
        user_id=user.id,
        account_number=generate_account_number(),
    )

    db.session.add(profile)
    for role in sorted(selected_roles.intersection({"admin", "developer"})):
        db.session.add(UserRole(user_id=user.id, role=role))
    record_audit(
        "user_created",
        user.id,
        f"username={username}; roles={','.join(sorted(selected_roles)) or 'standard'}",
    )
    db.session.commit()

    flash(
        "Account created.",
        "success",
    )

    return redirect(url_for("admin_panel"))


@app.route("/admin/roles/<int:user_id>", methods=["POST"])
@admin_required
def update_roles(user_id):
    user = db.session.get(User, user_id)
    if not user:
        abort(404)
    roles = set(request.form.getlist("role")).intersection(
        {"admin", "developer"}
    )
    if user.id == current_user.id and "admin" not in roles:
        flash("Keep the admin role on your own management account.", "error")
        return redirect(url_for("admin_panel"))
    apply_user_roles(user, roles)
    record_audit(
        "roles_updated",
        user.id,
        f"user={user.username}; roles={','.join(sorted(roles)) or 'standard'}",
    )
    db.session.commit()
    flash("Roles updated.", "success")
    return redirect(url_for("admin_panel"))


@app.route(
    "/admin/set-balance/<int:user_id>",
    methods=["POST"],
)
@admin_required
def set_balance(user_id):
    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        abort(404)

    try:
        balance = Decimal(
            request.form.get("balance", "")
        ).quantize(Decimal("0.01"))

    except (InvalidOperation, ValueError):
        flash(
            "Invalid balance.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    if balance < 0:
        flash(
            "Balance cannot be negative.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    user.balance = balance
    db.session.commit()

    send_live_update(
        user.id,
        "balance_changed",
        checking_balance=str(user.balance),
    )
    socketio.emit(
        "minibank_update",
        {"type": "admin_accounts_changed"},
        to="admins",
    )

    flash(
        "Checking balance updated.",
        "success",
    )

    return redirect(url_for("admin_panel"))


@app.route(
    "/admin/toggle-user/<int:user_id>",
    methods=["POST"],
)
@admin_required
def toggle_user(user_id):
    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        abort(404)

    if user.id == current_user.id:
        flash(
            "You cannot disable your own admin account.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    user.is_enabled = not user.is_enabled
    db.session.commit()

    status = (
        "enabled"
        if user.is_enabled
        else "disabled"
    )

    flash(
        f"{user.username} has been {status}.",
        "success",
    )

    return redirect(url_for("admin_panel"))


@app.route(
    "/admin/delete-user/<int:user_id>",
    methods=["POST"],
)
@admin_required
def delete_user(user_id):
    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        abort(404)

    if user.id == current_user.id:
        flash(
            "You cannot delete your own admin account.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    username = user.username

    transaction_ids = [
        transaction.id
        for transaction in Transaction.query.filter(
            (Transaction.sender_id == user.id)
            | (Transaction.receiver_id == user.id)
        ).all()
    ]
    if transaction_ids:
        CheckDeposit.query.filter(
            CheckDeposit.transaction_id.in_(transaction_ids)
        ).delete(synchronize_session=False)
        LedgerEntry.query.filter(LedgerEntry.transaction_id.in_(transaction_ids)).delete(
            synchronize_session=False
        )
    Transaction.query.filter(
        (Transaction.sender_id == user.id)
        | (Transaction.receiver_id == user.id)
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

    message_ids = [
        message.id
        for message in Message.query.filter(
            (Message.sender_id == user.id)
            | (Message.receiver_id == user.id)
        ).all()
    ]
    if message_ids:
        MessageReaction.query.filter(MessageReaction.message_id.in_(message_ids)).delete(
            synchronize_session=False
        )
        MessageAttachment.query.filter(MessageAttachment.message_id.in_(message_ids)).delete(
            synchronize_session=False
        )
        MessageMention.query.filter(MessageMention.message_id.in_(message_ids)).delete(
            synchronize_session=False
        )
        MessageReceipt.query.filter(MessageReceipt.message_id.in_(message_ids)).delete(
            synchronize_session=False
        )
    Message.query.filter(
        (Message.sender_id == user.id)
        | (Message.receiver_id == user.id)
    ).delete(synchronize_session=False)
    MessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    GroupMessageReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    MessageAttachment.query.filter_by(uploader_id=user.id).delete(synchronize_session=False)
    MessageMention.query.filter_by(mentioned_user_id=user.id).delete(synchronize_session=False)
    MessageReceipt.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    MessageReadState.query.filter(
        (MessageReadState.user_id == user.id)
        | (MessageReadState.other_user_id == user.id)
    ).delete(synchronize_session=False)

    group_message_ids = [
        message.id
        for message in GroupMessage.query.filter_by(sender_id=user.id).all()
    ]
    if group_message_ids:
        GroupMessageReaction.query.filter(
            GroupMessageReaction.group_message_id.in_(group_message_ids)
        ).delete(synchronize_session=False)
        MessageAttachment.query.filter(
            MessageAttachment.group_message_id.in_(group_message_ids)
        ).delete(synchronize_session=False)
        MessageMention.query.filter(
            MessageMention.group_message_id.in_(group_message_ids)
        ).delete(synchronize_session=False)
        MessageReceipt.query.filter(
            MessageReceipt.group_message_id.in_(group_message_ids)
        ).delete(synchronize_session=False)
    GroupMessage.query.filter_by(sender_id=user.id).delete(
        synchronize_session=False
    )
    GroupReadState.query.filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    GroupMembership.query.filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    owned_groups = GroupChat.query.filter_by(creator_id=user.id).all()
    for group in owned_groups:
        GroupMessage.query.filter_by(group_id=group.id).delete(
            synchronize_session=False
        )
        GroupReadState.query.filter_by(group_id=group.id).delete(
            synchronize_session=False
        )
        GroupMembership.query.filter_by(group_id=group.id).delete(
            synchronize_session=False
        )
        db.session.delete(group)

    AuditLog.query.filter(
        (AuditLog.actor_id == user.id)
        | (AuditLog.target_user_id == user.id)
    ).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    NotificationPreference.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    UserPermission.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ScheduledTransfer.query.filter(
        (ScheduledTransfer.sender_id == user.id)
        | (ScheduledTransfer.receiver_id == user.id)
    ).delete(synchronize_session=False)
    Beneficiary.query.filter(
        (Beneficiary.owner_id == user.id)
        | (Beneficiary.beneficiary_id == user.id)
    ).delete(synchronize_session=False)
    SupportMessage.query.filter_by(author_id=user.id).delete(synchronize_session=False)
    ticket_ids = [
        ticket.id
        for ticket in SupportTicket.query.filter_by(user_id=user.id).all()
    ]
    if ticket_ids:
        SupportMessage.query.filter(SupportMessage.ticket_id.in_(ticket_ids)).delete(
            synchronize_session=False
        )
    SupportTicket.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    bill_ids = [
        payment.id
        for payment in BillPayment.query.filter_by(user_id=user.id).all()
    ]
    if bill_ids:
        BillPaymentLedgerEntry.query.filter(
            BillPaymentLedgerEntry.bill_payment_id.in_(bill_ids)
        ).delete(synchronize_session=False)
    BillPayment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    BillPaymentLedgerEntry.query.filter_by(account_id=user.id).delete(
        synchronize_session=False
    )
    UserRole.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    UserProfile.query.filter_by(
        user_id=user.id,
    ).delete(synchronize_session=False)

    db.session.delete(user)
    db.session.commit()

    flash(
        f"{username} has been deleted.",
        "success",
    )

    return redirect(url_for("admin_panel"))


@app.route(
    "/admin/reset-password/<int:user_id>",
    methods=["POST"],
)
@admin_required
def reset_password(user_id):
    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        abort(404)

    password = request.form.get(
        "new_password",
        "",
    )

    if len(password) < 8:
        flash(
            "Password must have at least 8 characters.",
            "error",
        )

        return redirect(url_for("admin_panel"))

    user.set_password(password)
    db.session.commit()

    flash(
        f"Password changed for {user.username}.",
        "success",
    )

    return redirect(url_for("admin_panel"))


# =========================================================
# MESSAGES
# =========================================================

@app.route("/messages")
@login_required
def messages():
    users = (
        User.query
        .filter(
            User.id != current_user.id,
            User.is_enabled.is_(True),
        )
        .order_by(User.display_name.asc())
        .all()
    )

    return render_template(
        "messages.html",
        users=users,
        groups=(
            GroupChat.query.join(GroupMembership)
            .filter(GroupMembership.user_id == current_user.id)
            .order_by(GroupChat.name.asc())
            .all()
        ),
    )


@app.route("/groups", methods=["GET", "POST"])
@login_required
def groups():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 120:
            flash("Group name must be between 1 and 120 characters.", "error")
            return redirect(url_for("groups"))
        group = GroupChat(name=name, creator_id=current_user.id)
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMembership(
            group_id=group.id,
            user_id=current_user.id,
            role="owner",
        ))
        record_audit("group_created", current_user.id, f"group={name[:120]}")
        db.session.commit()
        flash("Group chat created.", "success")
        return redirect(url_for("group_chat", group_id=group.id))

    memberships = (
        GroupMembership.query.filter_by(user_id=current_user.id)
        .order_by(GroupMembership.joined_at.desc())
        .all()
    )
    return render_template(
        "messages.html",
        users=User.query.filter(
            User.id != current_user.id,
            User.is_enabled.is_(True),
        ).order_by(User.display_name.asc()).all(),
        groups=[membership.group for membership in memberships],
        group_page=True,
    )


@app.route("/groups/<int:group_id>", methods=["GET", "POST"])
@login_required
def group_chat(group_id):
    group = db.session.get(GroupChat, group_id)
    if not group:
        abort(404)
    membership = group_membership(group.id)
    if not membership:
        flash("You are not a member of that group.", "error")
        return redirect(url_for("messages"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if not content:
            flash("Message cannot be empty.", "error")
        elif len(content) > 1000:
            flash("Message must be 1000 characters or fewer.", "error")
        else:
            message = GroupMessage(
                group_id=group.id,
                sender_id=current_user.id,
                content=content,
            )
            db.session.add(message)
            db.session.flush()
            attachment_url = request.form.get("attachment_url", "").strip()
            if attachment_url:
                if not attachment_url.startswith(("http://", "https://")):
                    flash("Attachments must use an http or https URL.", "error")
                    db.session.rollback()
                    return redirect(url_for("group_chat", group_id=group.id))
                db.session.add(MessageAttachment(
                    group_message_id=message.id,
                    uploader_id=current_user.id,
                    url=attachment_url[:1000],
                    filename=request.form.get("attachment_name", "attachment").strip()[:160],
                ))
            for username in set(re.findall(r"@([A-Za-z0-9_.-]{3,80})", content)):
                mentioned = User.query.filter_by(username=username).first()
                if mentioned and mentioned.id != current_user.id:
                    db.session.add(MessageMention(
                        group_message_id=message.id,
                        mentioned_user_id=mentioned.id,
                    ))
                    create_notification(
                        mentioned.id,
                        "You were mentioned",
                        f"@{current_user.username} mentioned you in {group.name}.",
                    )
            db.session.commit()
            for member in GroupMembership.query.filter_by(group_id=group.id).all():
                send_live_update(
                    member.user_id,
                    "new_group_message",
                    group_id=group.id,
                    message_id=message.id,
                )
        return redirect(url_for("group_chat", group_id=group.id))

    state = GroupReadState.query.filter_by(
        group_id=group.id,
        user_id=current_user.id,
    ).first()
    if not state:
        state = GroupReadState(group_id=group.id, user_id=current_user.id)
        db.session.add(state)
    state.last_read_at = datetime.now(timezone.utc)
    db.session.commit()
    conversation = (
        GroupMessage.query.filter_by(group_id=group.id)
        .order_by(GroupMessage.created_at.asc())
        .all()
    )
    for message in conversation:
        if message.sender_id != current_user.id and not MessageReceipt.query.filter_by(
            group_message_id=message.id,
            user_id=current_user.id,
        ).first():
            db.session.add(MessageReceipt(
                group_message_id=message.id,
                user_id=current_user.id,
            ))
    db.session.commit()
    members = GroupMembership.query.filter_by(group_id=group.id).all()
    message_ids = [message.id for message in conversation]
    group_reactions = (
        GroupMessageReaction.query.filter(
            GroupMessageReaction.group_message_id.in_(message_ids)
        ).all()
        if message_ids
        else []
    )
    group_receipts = (
        MessageReceipt.query.filter(
            MessageReceipt.group_message_id.in_(message_ids)
        ).all()
        if message_ids
        else []
    )
    return render_template(
        "chat.html",
        group=group,
        conversation=conversation,
        members=members,
        is_owner=group.creator_id == current_user.id,
        is_moderator=group_is_manager(group.id, current_user.id),
        attachments={
            message.id: MessageAttachment.query.filter_by(
                group_message_id=message.id
            ).all()
            for message in conversation
        },
        reactions={
            message_id: [item.emoji for item in group_reactions
                         if item.group_message_id == message_id]
            for message_id in message_ids
        },
        receipt_counts={
            message_id: sum(
                item.group_message_id == message_id for item in group_receipts
            )
            for message_id in message_ids
        },
    )


@app.route("/groups/<int:group_id>/members", methods=["POST"])
@login_required
def manage_group_members(group_id):
    group = db.session.get(GroupChat, group_id)
    if not group:
        abort(404)
    if not group_is_manager(group.id, current_user.id):
        abort(403)
    username = request.form.get("username", "").strip()
    target = User.query.filter_by(username=username).first()
    action = request.form.get("action", "add")
    if action in {"promote", "demote"}:
        if group.creator_id != current_user.id:
            abort(403)
        membership = GroupMembership.query.filter_by(
            group_id=group.id,
            user_id=target.id if target else 0,
        ).first()
        if not membership or membership.user_id == group.creator_id:
            flash("Only a non-owner member can be changed.", "error")
        else:
            membership.role = "moderator" if action == "promote" else "member"
            db.session.commit()
            flash("Member role updated.", "success")
        return redirect(url_for("group_chat", group_id=group.id))

    if action == "remove":
        membership = GroupMembership.query.filter_by(
            group_id=group.id,
            user_id=target.id if target else 0,
        ).first()
        if not membership or membership.user_id == group.creator_id:
            flash("The owner cannot be removed and the member was not found.", "error")
        else:
            db.session.delete(membership)
            db.session.commit()
            flash("Member removed.", "success")
        return redirect(url_for("group_chat", group_id=group.id))

    if not target or not target.is_enabled:
        flash("Only enabled users can join a group.", "error")
        return redirect(url_for("group_chat", group_id=group.id))
    if group_membership(group.id, target.id):
        flash("That user is already a member.", "error")
        return redirect(url_for("group_chat", group_id=group.id))
    db.session.add(GroupMembership(group_id=group.id, user_id=target.id, role="member"))
    db.session.commit()
    flash("Member added.", "success")
    return redirect(url_for("group_chat", group_id=group.id))


@app.route(
    "/messages/<int:user_id>",
    methods=["GET", "POST"],
)
@login_required
def chat(user_id):
    other_user = db.session.get(
        User,
        user_id,
    )

    if not other_user:
        abort(404)

    if not other_user.is_enabled:
        flash(
            "This account is currently disabled.",
            "error",
        )

        return redirect(url_for("messages"))

    if other_user.id == current_user.id:
        flash(
            "You cannot message yourself.",
            "error",
        )

        return redirect(url_for("messages"))

    if request.method == "POST":
        content = request.form.get(
            "content",
            "",
        ).strip()

        if not content:
            flash(
                "Message cannot be empty.",
                "error",
            )

            return redirect(
                url_for(
                    "chat",
                    user_id=other_user.id,
                )
            )

        if len(content) > 1000:
            flash(
                "Message must be 1000 characters or fewer.",
                "error",
            )

            return redirect(
                url_for(
                    "chat",
                    user_id=other_user.id,
                )
            )

        message = Message(
            sender_id=current_user.id,
            receiver_id=other_user.id,
            content=content,
        )

        db.session.add(message)
        db.session.flush()
        attachment_url = request.form.get("attachment_url", "").strip()
        if attachment_url:
            if not attachment_url.startswith(("http://", "https://")):
                db.session.rollback()
                flash("Attachments must use an http or https URL.", "error")
                return redirect(url_for("chat", user_id=other_user.id))
            db.session.add(MessageAttachment(
                message_id=message.id,
                uploader_id=current_user.id,
                url=attachment_url[:1000],
                filename=request.form.get("attachment_name", "attachment").strip()[:160],
            ))
        for username in set(re.findall(r"@([A-Za-z0-9_.-]{3,80})", content)):
            mentioned = User.query.filter_by(username=username).first()
            if mentioned and mentioned.id != current_user.id:
                db.session.add(MessageMention(
                    message_id=message.id,
                    mentioned_user_id=mentioned.id,
                ))
                create_notification(
                    mentioned.id,
                    "You were mentioned",
                    f"@{current_user.username} mentioned you in a direct message.",
                )
        create_notification(
            other_user.id,
            "New message",
            f"@{current_user.username} sent you a message.",
        )
        db.session.commit()

        message_data = {
            "message_id": message.id,
            "sender_id": message.sender_id,
            "receiver_id": message.receiver_id,
        }
        send_live_update(
            current_user.id,
            "message_sent",
            **message_data,
        )
        send_live_update(
            other_user.id,
            "new_message",
            **message_data,
        )

        return redirect(
            url_for(
                "chat",
                user_id=other_user.id,
            )
        )

    state = MessageReadState.query.filter_by(
        user_id=current_user.id,
        other_user_id=other_user.id,
    ).first()

    if not state:
        state = MessageReadState(
            user_id=current_user.id,
            other_user_id=other_user.id,
        )

        db.session.add(state)

    state.last_read_at = datetime.now(timezone.utc)
    db.session.commit()

    conversation = (
        Message.query
        .filter(
            (
                (Message.sender_id == current_user.id)
                & (Message.receiver_id == other_user.id)
            )
            |
            (
                (Message.sender_id == other_user.id)
                & (Message.receiver_id == current_user.id)
            )
        )
        .order_by(Message.created_at.asc())
        .all()
    )
    for message in conversation:
        if message.sender_id == other_user.id and not MessageReceipt.query.filter_by(
            message_id=message.id,
            user_id=current_user.id,
        ).first():
            db.session.add(MessageReceipt(message_id=message.id, user_id=current_user.id))
    db.session.commit()
    message_ids = [message.id for message in conversation]
    direct_reactions = (
        MessageReaction.query.filter(
            MessageReaction.message_id.in_(message_ids)
        ).all()
        if message_ids
        else []
    )
    direct_receipts = (
        MessageReceipt.query.filter(
            MessageReceipt.message_id.in_(message_ids)
        ).all()
        if message_ids
        else []
    )

    return render_template(
        "chat.html",
        other_user=other_user,
        conversation=conversation,
        attachments={
            message.id: MessageAttachment.query.filter_by(
                message_id=message.id
            ).all()
            for message in conversation
        },
        reactions={
            message_id: [item.emoji for item in direct_reactions
                         if item.message_id == message_id]
            for message_id in message_ids
        },
        receipt_counts={
            message_id: sum(
                item.message_id == message_id for item in direct_receipts
            )
            for message_id in message_ids
        },
    )


@app.route("/messages/<int:message_id>/react", methods=["POST"])
@login_required
def react_to_message(message_id):
    message = db.session.get(Message, message_id)
    if not message or current_user.id not in {message.sender_id, message.receiver_id}:
        abort(404)
    emoji = request.form.get("emoji", "👍").strip()[:16]
    reaction = MessageReaction.query.filter_by(
        message_id=message.id, user_id=current_user.id
    ).first()
    if reaction:
        reaction.emoji = emoji
    else:
        db.session.add(MessageReaction(
            message_id=message.id,
            user_id=current_user.id,
            emoji=emoji,
        ))
    db.session.commit()
    return redirect(url_for("chat", user_id=message.receiver_id if message.sender_id == current_user.id else message.sender_id))


@app.route("/groups/messages/<int:message_id>/react", methods=["POST"])
@login_required
def react_to_group_message(message_id):
    message = db.session.get(GroupMessage, message_id)
    if not message or not group_membership(message.group_id):
        abort(404)
    emoji = request.form.get("emoji", "👍").strip()[:16]
    reaction = GroupMessageReaction.query.filter_by(
        group_message_id=message.id, user_id=current_user.id
    ).first()
    if reaction:
        reaction.emoji = emoji
    else:
        db.session.add(GroupMessageReaction(
            group_message_id=message.id,
            user_id=current_user.id,
            emoji=emoji,
        ))
    db.session.commit()
    return redirect(url_for("group_chat", group_id=message.group_id))


@app.route("/notifications")
@login_required
def notifications():
    items = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc()
    ).limit(100).all()
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update(
        {"is_read": True}, synchronize_session=False
    )
    db.session.commit()
    return render_template("notifications.html", notifications=items)


@app.route("/notification-preferences", methods=["GET", "POST"])
@login_required
def notification_preferences():
    abort(404)
    preferences = NotificationPreference.query.filter_by(user_id=current_user.id).first()
    if not preferences:
        preferences = NotificationPreference(user_id=current_user.id)
        db.session.add(preferences)
    if request.method == "POST":
        preferences.email_enabled = request.form.get("email_enabled") == "on"
        preferences.sms_enabled = request.form.get("sms_enabled") == "on"
        preferences.email_address = request.form.get("email_address", "").strip()[:160]
        preferences.phone_number = request.form.get("phone_number", "").strip()[:40]
        db.session.commit()
        flash("Notification preferences saved. Demo delivery is local only.", "success")
        return redirect(url_for("notification_preferences"))
    return render_template("notification_preferences.html", preferences=preferences)


@app.route("/scheduled-transfers", methods=["GET", "POST"])
@login_required
def scheduled_transfers():
    now = datetime.now(timezone.utc)
    due = ScheduledTransfer.query.filter(
        ScheduledTransfer.sender_id == current_user.id,
        ScheduledTransfer.active.is_(True),
        ScheduledTransfer.next_run_at <= now,
    ).all()
    for schedule in due:
        executed = False
        if (
            schedule.receiver.is_enabled
            and Decimal(current_user.balance) >= schedule.amount
        ):
            current_user.balance = Decimal(current_user.balance) - schedule.amount
            schedule.receiver.balance = Decimal(schedule.receiver.balance) + schedule.amount
            tx = Transaction(
                sender_id=current_user.id,
                receiver_id=schedule.receiver_id,
                amount=schedule.amount,
                note=f"Scheduled: {schedule.note}"[:200],
            )
            db.session.add(tx)
            db.session.flush()
            add_ledger_entries(tx)
            create_notification(
                schedule.receiver_id,
                "Scheduled payment received",
                f"{current_user.username} sent {money(schedule.amount)}.",
            )
            record_audit(
                "scheduled_transfer",
                schedule.receiver_id,
                f"schedule={schedule.id}; amount={schedule.amount}",
            )
            executed = True
        if executed:
            if schedule.interval_days:
                schedule.next_run_at = schedule.next_run_at + timedelta(
                    days=schedule.interval_days
                )
            else:
                schedule.active = False
    db.session.commit()
    if request.method == "POST":
        receiver = User.query.filter_by(
            username=request.form.get("receiver_username", "").strip()
        ).first()
        try:
            amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
            interval = int(request.form.get("interval_days", "0") or 0)
        except (InvalidOperation, ValueError, TypeError):
            amount, interval = Decimal("0.00"), 0
        run_at = parse_datetime(request.form.get("next_run_at")) or now
        if (
            not receiver
            or receiver.id == current_user.id
            or not receiver.is_enabled
            or amount <= 0
            or interval < 0
        ):
            flash("Choose an enabled recipient, a positive amount, and a valid schedule.", "error")
        else:
            db.session.add(
                ScheduledTransfer(
                    sender_id=current_user.id,
                    receiver_id=receiver.id,
                    amount=amount,
                    note=request.form.get("note", "").strip()[:200],
                    next_run_at=run_at,
                    interval_days=interval or None,
                )
            )
            record_audit(
                "scheduled_transfer_created",
                receiver.id,
                f"amount={amount}; interval_days={interval or 'once'}",
            )
            db.session.commit()
            flash("Scheduled transfer created.", "success")
        return redirect(url_for("scheduled_transfers"))
    schedules = ScheduledTransfer.query.filter_by(
        sender_id=current_user.id
    ).order_by(ScheduledTransfer.next_run_at.asc()).all()
    return render_template("scheduled_transfers.html", schedules=schedules)


@app.route("/scheduled-transfers/<int:schedule_id>/cancel", methods=["POST"])
@login_required
def cancel_scheduled_transfer(schedule_id):
    schedule = db.session.get(ScheduledTransfer, schedule_id)
    if not schedule or schedule.sender_id != current_user.id:
        abort(404)
    schedule.active = False
    record_audit(
        "scheduled_transfer_cancelled",
        schedule.receiver_id,
        f"schedule={schedule.id}",
    )
    db.session.commit()
    flash("Scheduled transfer cancelled.", "success")
    return redirect(url_for("scheduled_transfers"))


@app.route("/beneficiaries", methods=["GET", "POST"])
@login_required
def beneficiaries():
    if request.method == "POST":
        beneficiary = User.query.filter_by(
            username=request.form.get("username", "").strip()
        ).first()
        nickname = request.form.get("nickname", "").strip()[:80]
        if not beneficiary or beneficiary.id == current_user.id or not beneficiary.is_enabled or not nickname:
            flash("Choose an enabled account and provide a nickname.", "error")
        elif Beneficiary.query.filter_by(
            owner_id=current_user.id, beneficiary_id=beneficiary.id
        ).first():
            flash("That beneficiary already exists.", "error")
        else:
            db.session.add(Beneficiary(
                owner_id=current_user.id,
                beneficiary_id=beneficiary.id,
                nickname=nickname,
            ))
            record_audit(
                "beneficiary_added",
                beneficiary.id,
                f"nickname={nickname}",
            )
            db.session.commit()
            flash("Beneficiary saved.", "success")
        return redirect(url_for("beneficiaries"))
    items = Beneficiary.query.filter_by(owner_id=current_user.id).order_by(
        Beneficiary.nickname.asc()
    ).all()
    return render_template("beneficiaries.html", beneficiaries=items)


@app.route("/beneficiaries/<int:beneficiary_id>/delete", methods=["POST"])
@login_required
def delete_beneficiary(beneficiary_id):
    item = db.session.get(Beneficiary, beneficiary_id)
    if not item or item.owner_id != current_user.id:
        abort(404)
    db.session.delete(item)
    record_audit(
        "beneficiary_removed",
        item.beneficiary_id,
        f"beneficiary={item.id}",
    )
    db.session.commit()
    flash("Beneficiary removed.", "success")
    return redirect(url_for("beneficiaries"))


@app.route("/transfer/<int:transaction_id>/cancel", methods=["POST"])
@login_required
def cancel_transfer(transaction_id):
    transaction = db.session.get(Transaction, transaction_id)
    if (
        not transaction
        or transaction.sender_id != current_user.id
        or transaction.status != "completed"
    ):
        abort(404)
    cancellable_until = transaction.cancellable_until
    if cancellable_until and cancellable_until.tzinfo is None:
        cancellable_until = cancellable_until.replace(tzinfo=timezone.utc)
    if cancellable_until and datetime.now(timezone.utc) > cancellable_until:
        flash("The cancellation window has expired.", "error")
        return redirect(url_for("transactions"))
    sender = db.session.get(User, transaction.sender_id)
    receiver = db.session.get(User, transaction.receiver_id)
    if not sender or not receiver or Decimal(receiver.balance) < transaction.amount:
        flash("Cancellation cannot restore the recipient balance safely.", "error")
        return redirect(url_for("transactions"))
    sender.balance = Decimal(sender.balance) + transaction.amount
    receiver.balance = Decimal(receiver.balance) - transaction.amount
    transaction.status = "cancelled"
    db.session.add(LedgerEntry(
        transaction_id=transaction.id,
        account_id=sender.id,
        debit=Decimal("0.00"),
        credit=transaction.amount,
    ))
    db.session.add(LedgerEntry(
        transaction_id=transaction.id,
        account_id=receiver.id,
        debit=transaction.amount,
        credit=Decimal("0.00"),
    ))
    create_notification(
        receiver.id,
        "Transfer cancelled",
        f"{current_user.username} cancelled a transfer of {money(transaction.amount)}.",
    )
    db.session.commit()
    flash("Transfer cancelled and balances restored.", "success")
    return redirect(url_for("transactions"))


def statement_rows(user_id):
    return Transaction.query.filter(
        (Transaction.sender_id == user_id) | (Transaction.receiver_id == user_id)
    ).order_by(Transaction.created_at.desc()).all()


@app.route("/statements")
@login_required
def statements():
    return render_template("statements.html", transactions=statement_rows(current_user.id))


@app.route("/statements.csv")
@login_required
def statement_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "date", "direction", "counterparty", "amount", "status", "note"])
    for transaction in statement_rows(current_user.id):
        outgoing = transaction.sender_id == current_user.id
        writer.writerow([
            transaction.id,
            format_singapore_time(
                transaction.created_at,
                "%Y-%m-%d %H:%M:%S SGT",
            ),
            "debit" if outgoing else "credit",
            transaction.receiver.username if outgoing else transaction.sender.username,
            transaction.amount,
            transaction.status,
            transaction.note,
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=statement.csv"},
    )


@app.route("/statements.pdf")
@login_required
def statement_pdf():
    # Keep this dependency-free: deployments without a PDF renderer receive
    # the same export as CSV with an explicit fallback header.
    response = statement_csv()
    response.headers["X-PDF-Fallback"] = "CSV export used because PDF tooling is unavailable"
    response.headers["Content-Disposition"] = "attachment; filename=statement.csv"
    return response


@app.route("/admin/audit-logs")
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


@app.route("/admin/permissions/<int:user_id>", methods=["POST"])
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
    return redirect(url_for("admin_panel"))


@app.route("/support", methods=["GET", "POST"])
@login_required
def support():
    abort(404)
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()[:160]
        description = request.form.get("description", "").strip()
        if not subject or not description:
            flash("Subject and description are required.", "error")
        else:
            ticket = SupportTicket(
                user_id=current_user.id,
                subject=subject,
                description=description[:5000],
            )
            db.session.add(ticket)
            record_audit(
                "support_ticket_created",
                current_user.id,
                f"subject={subject}",
            )
            db.session.commit()
            flash("Support ticket created.", "success")
            return redirect(url_for("support_ticket", ticket_id=ticket.id))
    tickets = SupportTicket.query.filter_by(user_id=current_user.id).order_by(
        SupportTicket.updated_at.desc()
    ).all()
    if current_user.has_permission("support_manage"):
        tickets = SupportTicket.query.order_by(SupportTicket.updated_at.desc()).all()
    return render_template("support.html", tickets=tickets)


@app.route("/support/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def support_ticket(ticket_id):
    abort(404)
    ticket = db.session.get(SupportTicket, ticket_id)
    if not ticket:
        abort(404)
    if ticket.user_id != current_user.id and not current_user.has_permission("support_manage"):
        abort(403)
    if request.method == "POST":
        requested_status = request.form.get("status")
        if requested_status in {"open", "answered", "closed"} and current_user.has_permission("support_manage"):
            ticket.status = requested_status
            ticket.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            flash("Ticket status updated.", "success")
            return redirect(url_for("support_ticket", ticket_id=ticket.id))
        content = request.form.get("content", "").strip()
        if content:
            db.session.add(SupportMessage(
                ticket_id=ticket.id,
                author_id=current_user.id,
                content=content[:5000],
            ))
            if current_user.has_permission("support_manage"):
                ticket.status = "answered"
            ticket.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            flash("Reply added.", "success")
        return redirect(url_for("support_ticket", ticket_id=ticket.id))
    replies = SupportMessage.query.filter_by(ticket_id=ticket.id).order_by(
        SupportMessage.created_at.asc()
    ).all()
    return render_template("support_ticket.html", ticket=ticket, replies=replies)


@app.route("/bills", methods=["GET", "POST"])
@login_required
def bills():
    abort(404)
    if request.method == "POST":
        payee = request.form.get("payee", "").strip()[:120]
        reference = request.form.get("reference", "").strip()[:120]
        try:
            amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError, TypeError):
            amount = Decimal("0.00")
        if not payee or amount <= 0 or amount > Decimal(current_user.balance):
            flash("Enter a payee and a positive amount within your balance.", "error")
        else:
            current_user.balance = Decimal(current_user.balance) - amount
            bill = BillPayment(
                user_id=current_user.id,
                payee=payee,
                amount=amount,
                reference=reference,
            )
            db.session.add(bill)
            db.session.flush()
            db.session.add(BillPaymentLedgerEntry(
                bill_payment_id=bill.id,
                account_id=current_user.id,
                account_label="Demo checking",
                debit=amount,
                credit=Decimal("0.00"),
            ))
            db.session.add(BillPaymentLedgerEntry(
                bill_payment_id=bill.id,
                account_label="Demo biller clearing",
                debit=Decimal("0.00"),
                credit=amount,
            ))
            create_notification(current_user.id, "Bill paid", f"{payee}: {money(amount)}")
            record_audit(
                "bill_payment",
                current_user.id,
                f"payee={payee}; amount={amount}; reference={reference}",
            )
            db.session.commit()
            flash("Demo bill payment completed.", "success")
        return redirect(url_for("bills"))
    payments = BillPayment.query.filter_by(user_id=current_user.id).order_by(
        BillPayment.created_at.desc()
    ).all()
    return render_template("bills.html", payments=payments)


# =========================================================
# CREATE DATABASE AND ADMIN ACCOUNT
# =========================================================

with app.app_context():
    db.create_all()

    # Add columns required by the current ORM before any User queries run.
    # create_all() does not alter existing PostgreSQL tables.
    user_columns = {
        column["name"] for column in inspect(db.engine).get_columns("user")
    }
    if "account_type" not in user_columns:
        db.session.execute(
            text(
                'ALTER TABLE "user" ADD COLUMN account_type '
                "VARCHAR(20) NOT NULL DEFAULT 'standard'"
            )
        )
        db.session.commit()
        db.session.execute(
            text(
                'UPDATE "user" SET account_type = \'admin\' '
                "WHERE is_admin = 1"
            )
        )
        db.session.commit()

    # Migrate legacy savings balances exactly once before retiring the table.
    # The update and DROP occur in one transaction so a failed migration does
    # not destroy the legacy rows or create duplicate funds on restart.
    legacy_tables = set(inspect(db.engine).get_table_names())
    if "savings_account" in legacy_tables:
        try:
            legacy_rows = db.session.execute(
                text("SELECT user_id, balance FROM savings_account")
            ).mappings().all()
            for row in legacy_rows:
                user = db.session.get(User, row["user_id"])
                amount = Decimal(str(row["balance"] or "0")).quantize(
                    Decimal("0.01")
                )
                if user and amount:
                    user.balance = Decimal(str(user.balance or "0")) + amount
            db.session.execute(text("DROP TABLE savings_account"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    if "internal_transfer" in legacy_tables:
        db.session.execute(text("DROP TABLE internal_transfer"))
        db.session.commit()

    # Lightweight compatibility migrations for demo databases created before
    # the expanded feature set.  New installations are handled by create_all.
    group_columns = {
        column["name"] for column in inspect(db.engine).get_columns("group_membership")
    }
    if "role" not in group_columns:
        db.session.execute(text(
            "ALTER TABLE group_membership ADD COLUMN role VARCHAR(20) "
            "NOT NULL DEFAULT 'member'"
        ))
        db.session.commit()
        db.session.execute(text(
            "UPDATE group_membership SET role = 'owner' "
            "WHERE group_id IN (SELECT id FROM group_chat WHERE creator_id = user_id)"
        ))
        db.session.commit()
    transaction_columns = {
        column["name"] for column in inspect(db.engine).get_columns("transaction")
    }
    if "status" not in transaction_columns:
        db.session.execute(text(
            'ALTER TABLE "transaction" ADD COLUMN status VARCHAR(20) '
            "NOT NULL DEFAULT 'completed'"
        ))
        db.session.commit()
    if "cancellable_until" not in transaction_columns:
        db.session.execute(text(
            'ALTER TABLE "transaction" ADD COLUMN cancellable_until TIMESTAMP'
        ))
        db.session.commit()
        db.session.execute(
            text('UPDATE "transaction" SET cancellable_until = :cutoff'),
            {"cutoff": datetime.now(timezone.utc).isoformat()},
        )
        db.session.commit()

    legacy_users = User.query.filter(User.account_type.is_(None)).all()
    for user in legacy_users:
        user.account_type = "admin" if user.is_admin else "standard"
    if legacy_users:
        db.session.commit()

    admin_username = os.environ.get(
        "ADMIN_USERNAME",
        "admin",
    ).strip()

    admin_password = os.environ.get(
        "ADMIN_PASSWORD",
        "",
    )

    existing_admin = User.query.filter_by(
        username=admin_username,
    ).first()

    if not existing_admin and not admin_password:
        raise RuntimeError(
            "ADMIN_PASSWORD must be set when initializing a fresh MiniBank database."
        )

    # If ADMIN_PASSWORD exists in Render, it will create
    # the admin or reset the existing admin password.
    if admin_password:
        if existing_admin:
            existing_admin.set_password(admin_password)
            existing_admin.is_admin = True
            existing_admin.account_type = "admin"
            existing_admin.is_enabled = True

        else:
            existing_admin = User(
                username=admin_username,
                display_name="Administrator",
                balance=Decimal("10000.00"),
                is_admin=True,
                account_type="admin",
                is_enabled=True,
            )

            existing_admin.set_password(admin_password)

            db.session.add(existing_admin)
            db.session.flush()

            admin_profile = UserProfile(
                user_id=existing_admin.id,
                account_number=generate_account_number(),
            )

            db.session.add(admin_profile)

        db.session.commit()

    # Create missing profiles for old users
    users_without_profiles = (
        User.query
        .outerjoin(
            UserProfile,
            User.id == UserProfile.user_id,
        )
        .filter(UserProfile.id.is_(None))
        .all()
    )

    for user in users_without_profiles:
        profile = UserProfile(
            user_id=user.id,
            account_number=generate_account_number(),
        )

        db.session.add(profile)

    db.session.commit()


# =========================================================
# LOCAL DEVELOPMENT
# =========================================================

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
    )
