import secrets
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import session, abort, current_app

from .extensions import db
from .branding import get_org, all_orgs, PARTNERSHIP_LINE

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


# ---------- Timezone ----------

def get_singapore_tz():
    if ZoneInfo:
        try:
            return ZoneInfo("Asia/Singapore")
        except ZoneInfoNotFoundError:
            pass
    return timezone(timedelta(hours=8), name="SGT")


SINGAPORE_TZ = get_singapore_tz()
DISPLAY_TIMEZONE = "SGT / Asia/Singapore (UTC+8)"


def to_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_singapore_time(value, pattern="%d %b %Y, %H:%M SGT"):
    if not value:
        return ""
    return to_utc(value).astimezone(SINGAPORE_TZ).strftime(pattern)


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


# ---------- Money ----------

def money(value):
    try:
        return f"${Decimal(value):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return "$0.00"


def to_decimal(value, default="0.00"):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


# ---------- Account numbers ----------

def generate_account_number():
    from .models import UserProfile
    while True:
        number = "88" + "".join(secrets.choice("0123456789") for _ in range(10))
        if not UserProfile.query.filter_by(account_number=number).first():
            return number


# ---------- User roles ----------

def apply_user_roles(user, roles):
    """Persist composable management roles and legacy compatibility fields."""
    from .models import UserRole
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


# ---------- Impersonation ----------

def is_impersonating():
    return bool(session.get("impersonator_id"))


def get_impersonator():
    from .models import User
    actor_id = session.get("impersonator_id")
    if not actor_id:
        return None
    actor = db.session.get(User, actor_id)
    if not actor or not actor.is_enabled or not actor.has_permission("impersonate"):
        return None
    return actor


# ---------- Decorators ----------

def admin_required(fn):
    from flask_login import login_required, current_user
    @wraps(fn)
    @login_required
    def wrapped(*a, **kw):
        if is_impersonating() or not current_user.has_management_access:
            abort(403)
        return fn(*a, **kw)
    return wrapped


def permission_required(permission):
    def deco(fn):
        from flask_login import login_required, current_user
        @wraps(fn)
        @login_required
        def wrapped(*a, **kw):
            if is_impersonating() or not current_user.has_permission(permission):
                abort(403)
            return fn(*a, **kw)
        return wrapped
    return deco


def developer_required(fn):
    from flask_login import login_required, current_user
    @wraps(fn)
    @login_required
    def wrapped(*a, **kw):
        if is_impersonating() or "developer" not in current_user.roles:
            abort(403)
        return fn(*a, **kw)
    return wrapped


# ---------- Audit ----------

def record_audit(action, target_user_id=None, details="", actor_id=None):
    from .models import AuditLog
    from flask_login import current_user
    actor_id = actor_id or session.get("impersonator_id", current_user.id)
    db.session.add(AuditLog(
        actor_id=actor_id,
        action=action,
        target_user_id=target_user_id,
        details=str(details)[:2000],
    ))


# ---------- Notifications + ledger ----------

def create_notification(user_id, title, body):
    from .models import Notification
    n = Notification(user_id=user_id, title=str(title)[:160], body=str(body)[:500])
    db.session.add(n)
    db.session.flush()
    send_live_update(user_id, "notification", notification_id=n.id)
    return n


def add_ledger_entries(transaction):
    from .models import LedgerEntry
    db.session.add(LedgerEntry(
        transaction_id=transaction.id,
        account_id=transaction.sender_id,
        debit=transaction.amount,
        credit=Decimal("0.00"),
    ))
    db.session.add(LedgerEntry(
        transaction_id=transaction.id,
        account_id=transaction.receiver_id,
        debit=Decimal("0.00"),
        credit=transaction.amount,
    ))


# ---------- Socket.IO ----------

def user_room(user_id):
    return f"user_{int(user_id)}"


def send_live_update(user_id, update_type, **data):
    from .extensions import socketio
    socketio.emit(
        "rubo_update",
        {"type": update_type, **data},
        to=user_room(user_id),
    )


# ---------- Jinja / context ----------

def _current_org():
    slug = session.get("org") or current_app.config.get("DEFAULT_ORG", "rubo")
    return get_org(slug)


def register_jinja_helpers(app):
    from flask_login import current_user

    app.jinja_env.filters["sgt"] = format_singapore_time
    app.jinja_env.filters["money"] = money

    @app.context_processor
    def inject_globals():
        org = _current_org()
        base = {
            "org": org,
            "all_orgs": all_orgs(),
            "partnership_line": PARTNERSHIP_LINE,
            "display_timezone": DISPLAY_TIMEZONE,
        }

        if not current_user.is_authenticated:
            base.update({
                "unread_message_count": 0,
                "unread_group_message_count": 0,
                "unread_notification_count": 0,
                "current_profile": None,
                "impersonator": None,
            })
            return base

        from .models import (
            Message, MessageReadState, GroupMembership,
            GroupMessage, GroupReadState, Notification, UserProfile,
        )

        unread = 0
        senders = db.session.query(Message.sender_id).filter(
            Message.receiver_id == current_user.id
        ).distinct().all()
        for (sid,) in senders:
            state = MessageReadState.query.filter_by(
                user_id=current_user.id, other_user_id=sid
            ).first()
            q = Message.query.filter(
                Message.sender_id == sid,
                Message.receiver_id == current_user.id,
            )
            if state:
                q = q.filter(Message.created_at > state.last_read_at)
            unread += q.count()

        group_unread = 0
        for m in GroupMembership.query.filter_by(user_id=current_user.id).all():
            st = GroupReadState.query.filter_by(
                user_id=current_user.id, group_id=m.group_id
            ).first()
            q = GroupMessage.query.filter(
                GroupMessage.group_id == m.group_id,
                GroupMessage.sender_id != current_user.id,
            )
            if st:
                q = q.filter(GroupMessage.created_at > st.last_read_at)
            group_unread += q.count()

        notif_unread = Notification.query.filter_by(
            user_id=current_user.id, is_read=False
        ).count()

        profile = UserProfile.query.filter_by(user_id=current_user.id).first()

        base.update({
            "unread_message_count": unread,
            "unread_group_message_count": group_unread,
            "unread_notification_count": notif_unread,
            "current_profile": profile,
            "impersonator": get_impersonator(),
        })
        return base
