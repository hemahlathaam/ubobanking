import re
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, abort,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models import (
    User, Message, MessageReadState, GroupChat, GroupMembership,
    GroupMessage, GroupReadState, MessageReaction, GroupMessageReaction,
    MessageAttachment, MessageMention, MessageReceipt,
    Notification, NotificationPreference,
)
from ..utils import (
    record_audit, create_notification, send_live_update,
)

bp = Blueprint("chat", __name__)


def _group_membership(group_id, user_id=None):
    return GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id if user_id is None else user_id,
    ).first()


def _group_is_manager(group_id, user_id):
    m = _group_membership(group_id, user_id)
    return bool(m and m.role in {"owner", "moderator"})


# ---------------- Direct messages ----------------

@bp.route("/messages")
@login_required
def messages():
    users = (
        User.query
        .filter(User.id != current_user.id, User.is_enabled.is_(True))
        .order_by(User.display_name.asc())
        .all()
    )
    groups = (
        GroupChat.query.join(GroupMembership)
        .filter(GroupMembership.user_id == current_user.id)
        .order_by(GroupChat.name.asc())
        .all()
    )
    return render_template("messages.html", users=users, groups=groups)


@bp.route("/messages/<int:user_id>", methods=["GET", "POST"])
@login_required
def chat(user_id):
    other_user = db.session.get(User, user_id)
    if not other_user:
        abort(404)
    if not other_user.is_enabled:
        flash("This account is currently disabled.", "error")
        return redirect(url_for("chat.messages"))
    if other_user.id == current_user.id:
        flash("You cannot message yourself.", "error")
        return redirect(url_for("chat.messages"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if not content:
            flash("Message cannot be empty.", "error")
            return redirect(url_for("chat.chat", user_id=other_user.id))
        if len(content) > 1000:
            flash("Message must be 1000 characters or fewer.", "error")
            return redirect(url_for("chat.chat", user_id=other_user.id))

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
                return redirect(url_for("chat.chat", user_id=other_user.id))
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

        payload = {
            "message_id": message.id,
            "sender_id": message.sender_id,
            "receiver_id": message.receiver_id,
        }
        send_live_update(current_user.id, "message_sent", **payload)
        send_live_update(other_user.id, "new_message", **payload)
        return redirect(url_for("chat.chat", user_id=other_user.id))

    state = MessageReadState.query.filter_by(
        user_id=current_user.id, other_user_id=other_user.id
    ).first()
    if not state:
        state = MessageReadState(
            user_id=current_user.id, other_user_id=other_user.id
        )
        db.session.add(state)
    state.last_read_at = datetime.now(timezone.utc)
    db.session.commit()

    conversation = (
        Message.query
        .filter(
            ((Message.sender_id == current_user.id) & (Message.receiver_id == other_user.id))
            | ((Message.sender_id == other_user.id) & (Message.receiver_id == current_user.id))
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    for message in conversation:
        if message.sender_id == other_user.id and not MessageReceipt.query.filter_by(
            message_id=message.id, user_id=current_user.id
        ).first():
            db.session.add(MessageReceipt(message_id=message.id, user_id=current_user.id))
    db.session.commit()

    message_ids = [m.id for m in conversation]
    direct_reactions = (
        MessageReaction.query.filter(MessageReaction.message_id.in_(message_ids)).all()
        if message_ids else []
    )
    direct_receipts = (
        MessageReceipt.query.filter(MessageReceipt.message_id.in_(message_ids)).all()
        if message_ids else []
    )

    return render_template(
        "chat.html",
        other_user=other_user,
        conversation=conversation,
        attachments={
            m.id: MessageAttachment.query.filter_by(message_id=m.id).all()
            for m in conversation
        },
        reactions={
            mid: [r.emoji for r in direct_reactions if r.message_id == mid]
            for mid in message_ids
        },
        receipt_counts={
            mid: sum(1 for r in direct_receipts if r.message_id == mid)
            for mid in message_ids
        },
    )


@bp.route("/messages/<int:message_id>/react", methods=["POST"])
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
            message_id=message.id, user_id=current_user.id, emoji=emoji,
        ))
    db.session.commit()
    return redirect(url_for(
        "chat.chat",
        user_id=(
            message.receiver_id if message.sender_id == current_user.id
            else message.sender_id
        ),
    ))


# ---------------- Group chats ----------------

@bp.route("/groups", methods=["GET", "POST"])
@login_required
def groups():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 120:
            flash("Group name must be between 1 and 120 characters.", "error")
            return redirect(url_for("chat.groups"))

        group = GroupChat(name=name, creator_id=current_user.id)
        db.session.add(group)
        db.session.flush()
        db.session.add(GroupMembership(
            group_id=group.id, user_id=current_user.id, role="owner",
        ))
        record_audit("group_created", current_user.id, f"group={name[:120]}")
        db.session.commit()
        flash("Group chat created.", "success")
        return redirect(url_for("chat.group_chat", group_id=group.id))

    memberships = (
        GroupMembership.query
        .filter_by(user_id=current_user.id)
        .order_by(GroupMembership.joined_at.desc())
        .all()
    )
    return render_template(
        "messages.html",
        users=User.query.filter(
            User.id != current_user.id, User.is_enabled.is_(True)
        ).order_by(User.display_name.asc()).all(),
        groups=[m.group for m in memberships],
        group_page=True,
    )


@bp.route("/groups/<int:group_id>", methods=["GET", "POST"])
@login_required
def group_chat(group_id):
    group = db.session.get(GroupChat, group_id)
    if not group:
        abort(404)
    membership = _group_membership(group.id)
    if not membership:
        flash("You are not a member of that group.", "error")
        return redirect(url_for("chat.messages"))

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
                    return redirect(url_for("chat.group_chat", group_id=group.id))
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
        return redirect(url_for("chat.group_chat", group_id=group.id))

    state = GroupReadState.query.filter_by(
        group_id=group.id, user_id=current_user.id
    ).first()
    if not state:
        state = GroupReadState(group_id=group.id, user_id=current_user.id)
        db.session.add(state)
    state.last_read_at = datetime.now(timezone.utc)
    db.session.commit()

    conversation = (
        GroupMessage.query
        .filter_by(group_id=group.id)
        .order_by(GroupMessage.created_at.asc())
        .all()
    )
    for message in conversation:
        if message.sender_id != current_user.id and not MessageReceipt.query.filter_by(
            group_message_id=message.id, user_id=current_user.id
        ).first():
            db.session.add(MessageReceipt(
                group_message_id=message.id, user_id=current_user.id,
            ))
    db.session.commit()

    members = GroupMembership.query.filter_by(group_id=group.id).all()
    message_ids = [m.id for m in conversation]
    group_reactions = (
        GroupMessageReaction.query
        .filter(GroupMessageReaction.group_message_id.in_(message_ids))
        .all()
        if message_ids else []
    )
    group_receipts = (
        MessageReceipt.query
        .filter(MessageReceipt.group_message_id.in_(message_ids))
        .all()
        if message_ids else []
    )

    return render_template(
        "chat.html",
        group=group,
        conversation=conversation,
        members=members,
        is_owner=group.creator_id == current_user.id,
        is_moderator=_group_is_manager(group.id, current_user.id),
        attachments={
            m.id: MessageAttachment.query.filter_by(group_message_id=m.id).all()
            for m in conversation
        },
        reactions={
            mid: [r.emoji for r in group_reactions if r.group_message_id == mid]
            for mid in message_ids
        },
        receipt_counts={
            mid: sum(1 for r in group_receipts if r.group_message_id == mid)
            for mid in message_ids
        },
    )


@bp.route("/groups/<int:group_id>/members", methods=["POST"])
@login_required
def manage_group_members(group_id):
    group = db.session.get(GroupChat, group_id)
    if not group:
        abort(404)
    if not _group_is_manager(group.id, current_user.id):
        abort(403)

    username = request.form.get("username", "").strip()
    target = User.query.filter_by(username=username).first()
    action = request.form.get("action", "add")

    if action in {"promote", "demote"}:
        if group.creator_id != current_user.id:
            abort(403)
        membership = GroupMembership.query.filter_by(
            group_id=group.id, user_id=target.id if target else 0,
        ).first()
        if not membership or membership.user_id == group.creator_id:
            flash("Only a non-owner member can be changed.", "error")
        else:
            membership.role = "moderator" if action == "promote" else "member"
            db.session.commit()
            flash("Member role updated.", "success")
        return redirect(url_for("chat.group_chat", group_id=group.id))

    if action == "remove":
        membership = GroupMembership.query.filter_by(
            group_id=group.id, user_id=target.id if target else 0,
        ).first()
        if not membership or membership.user_id == group.creator_id:
            flash("The owner cannot be removed and the member was not found.", "error")
        else:
            db.session.delete(membership)
            db.session.commit()
            flash("Member removed.", "success")
        return redirect(url_for("chat.group_chat", group_id=group.id))

    if not target or not target.is_enabled:
        flash("Only enabled users can join a group.", "error")
        return redirect(url_for("chat.group_chat", group_id=group.id))
    if _group_membership(group.id, target.id):
        flash("That user is already a member.", "error")
        return redirect(url_for("chat.group_chat", group_id=group.id))

    db.session.add(GroupMembership(
        group_id=group.id, user_id=target.id, role="member",
    ))
    db.session.commit()
    flash("Member added.", "success")
    return redirect(url_for("chat.group_chat", group_id=group.id))


@bp.route("/groups/messages/<int:message_id>/react", methods=["POST"])
@login_required
def react_to_group_message(message_id):
    message = db.session.get(GroupMessage, message_id)
    if not message or not _group_membership(message.group_id):
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
    return redirect(url_for("chat.group_chat", group_id=message.group_id))


# ---------------- Notifications ----------------

@bp.route("/notifications")
@login_required
def notifications():
    items = (
        Notification.query
        .filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return render_template("notifications.html", notifications=items)


@bp.route("/notification-preferences", methods=["GET", "POST"])
@login_required
def notification_preferences():
    preferences = NotificationPreference.query.filter_by(
        user_id=current_user.id
    ).first()
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
        return redirect(url_for("chat.notification_preferences"))

    return render_template("notification_preferences.html", preferences=preferences)
