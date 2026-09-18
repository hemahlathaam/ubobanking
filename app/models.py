from datetime import datetime, timezone, timedelta
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db


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
