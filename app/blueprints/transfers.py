from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, abort,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models import (
    User, Transaction, LedgerEntry, Beneficiary,
    ScheduledTransfer, Notification,
)
from ..utils import (
    money, record_audit, create_notification, add_ledger_entries,
    send_live_update, parse_datetime,
)

bp = Blueprint("transfers", __name__)


@bp.route("/transfer", methods=["GET", "POST"])
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

        if receiver is None:
            receiver_username = request.form.get("receiver_username", "").strip()
            receiver = User.query.filter_by(username=receiver_username).first()

        if not receiver or not receiver.is_enabled:
            flash("Recipient not found.", "error")
            return redirect(url_for("transfers.transfer"))

        if receiver.id == current_user.id:
            flash("You cannot transfer to yourself.", "error")
            return redirect(url_for("transfers.transfer"))

        try:
            amount = Decimal(request.form.get("amount", "")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            flash("Enter a valid amount.", "error")
            return redirect(url_for("transfers.transfer"))

        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return redirect(url_for("transfers.transfer"))

        if amount > Decimal(current_user.balance):
            flash("Insufficient checking balance.", "error")
            return redirect(url_for("transfers.transfer"))

        note = request.form.get("note", "").strip()[:200]

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        duplicate = Transaction.query.filter(
            Transaction.sender_id == current_user.id,
            Transaction.receiver_id == receiver.id,
            Transaction.amount == amount,
            Transaction.note == note,
            Transaction.created_at >= cutoff,
            Transaction.status == "completed",
        ).first()
        if duplicate:
            flash("A matching transfer was submitted recently.", "error")
            return redirect(url_for("transfers.transfer"))

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

        payload = {
            "transaction_id": transaction.id,
            "sender_id": transaction.sender_id,
            "receiver_id": transaction.receiver_id,
            "amount": str(transaction.amount),
        }
        send_live_update(current_user.id, "transfer_completed", **payload)
        send_live_update(receiver.id, "money_received", **payload)

        return redirect(url_for(
            "dashboard.transfer_receipt",
            transaction_id=transaction.id,
        ))

    return render_template(
        "transfer.html",
        beneficiaries=(
            Beneficiary.query
            .filter_by(owner_id=current_user.id)
            .order_by(Beneficiary.nickname.asc())
            .all()
        ),
    )


@bp.route("/transfer/<int:transaction_id>/cancel", methods=["POST"])
@login_required
def cancel_transfer(transaction_id):
    transaction = db.session.get(Transaction, transaction_id)
    if (
        not transaction
        or transaction.sender_id != current_user.id
        or transaction.status != "completed"
    ):
        abort(404)

    until = transaction.cancellable_until
    if until and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until and datetime.now(timezone.utc) > until:
        flash("The cancellation window has expired.", "error")
        return redirect(url_for("dashboard.transactions"))

    sender = db.session.get(User, transaction.sender_id)
    receiver = db.session.get(User, transaction.receiver_id)
    if not sender or not receiver or Decimal(receiver.balance) < transaction.amount:
        flash("Cancellation cannot restore the recipient balance safely.", "error")
        return redirect(url_for("dashboard.transactions"))

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
    return redirect(url_for("dashboard.transactions"))


@bp.route("/beneficiaries", methods=["GET", "POST"])
@login_required
def beneficiaries():
    if request.method == "POST":
        beneficiary = User.query.filter_by(
            username=request.form.get("username", "").strip()
        ).first()
        nickname = request.form.get("nickname", "").strip()[:80]

        if (not beneficiary or beneficiary.id == current_user.id
                or not beneficiary.is_enabled or not nickname):
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
            record_audit("beneficiary_added", beneficiary.id, f"nickname={nickname}")
            db.session.commit()
            flash("Beneficiary saved.", "success")
        return redirect(url_for("transfers.beneficiaries"))

    items = (
        Beneficiary.query
        .filter_by(owner_id=current_user.id)
        .order_by(Beneficiary.nickname.asc())
        .all()
    )
    return render_template("beneficiaries.html", beneficiaries=items)


@bp.route("/beneficiaries/<int:beneficiary_id>/delete", methods=["POST"])
@login_required
def delete_beneficiary(beneficiary_id):
    item = db.session.get(Beneficiary, beneficiary_id)
    if not item or item.owner_id != current_user.id:
        abort(404)
    db.session.delete(item)
    record_audit("beneficiary_removed", item.beneficiary_id, f"beneficiary={item.id}")
    db.session.commit()
    flash("Beneficiary removed.", "success")
    return redirect(url_for("transfers.beneficiaries"))


@bp.route("/scheduled-transfers", methods=["GET", "POST"])
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
        if (schedule.receiver.is_enabled
                and Decimal(current_user.balance) >= schedule.amount):
            current_user.balance = Decimal(current_user.balance) - schedule.amount
            schedule.receiver.balance = (
                Decimal(schedule.receiver.balance) + schedule.amount
            )
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

        if (not receiver or receiver.id == current_user.id
                or not receiver.is_enabled or amount <= 0 or interval < 0):
            flash("Choose an enabled recipient, a positive amount, and a valid schedule.", "error")
        else:
            db.session.add(ScheduledTransfer(
                sender_id=current_user.id,
                receiver_id=receiver.id,
                amount=amount,
                note=request.form.get("note", "").strip()[:200],
                next_run_at=run_at,
                interval_days=interval or None,
            ))
            record_audit(
                "scheduled_transfer_created",
                receiver.id,
                f"amount={amount}; interval_days={interval or 'once'}",
            )
            db.session.commit()
            flash("Scheduled transfer created.", "success")
        return redirect(url_for("transfers.scheduled_transfers"))

    schedules = (
        ScheduledTransfer.query
        .filter_by(sender_id=current_user.id)
        .order_by(ScheduledTransfer.next_run_at.asc())
        .all()
    )
    return render_template("scheduled_transfers.html", schedules=schedules)


@bp.route("/scheduled-transfers/<int:schedule_id>/cancel", methods=["POST"])
@login_required
def cancel_scheduled_transfer(schedule_id):
    schedule = db.session.get(ScheduledTransfer, schedule_id)
    if not schedule or schedule.sender_id != current_user.id:
        abort(404)
    schedule.active = False
    record_audit("scheduled_transfer_cancelled", schedule.receiver_id, f"schedule={schedule.id}")
    db.session.commit()
    flash("Scheduled transfer cancelled.", "success")
    return redirect(url_for("transfers.scheduled_transfers"))
