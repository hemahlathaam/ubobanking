from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, abort,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models import (
    SupportTicket, SupportMessage, BillPayment, BillPaymentLedgerEntry,
)
from ..utils import (
    money, record_audit, create_notification, permission_required,
)

bp = Blueprint("support", __name__)


# ---------------- Support tickets ----------------

@bp.route("/support", methods=["GET", "POST"])
@login_required
def support():
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
            return redirect(url_for("support.support_ticket", ticket_id=ticket.id))

    tickets = (
        SupportTicket.query
        .filter_by(user_id=current_user.id)
        .order_by(SupportTicket.updated_at.desc())
        .all()
    )
    if current_user.has_permission("support_manage"):
        tickets = SupportTicket.query.order_by(SupportTicket.updated_at.desc()).all()

    return render_template("support.html", tickets=tickets)


@bp.route("/support/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def support_ticket(ticket_id):
    ticket = db.session.get(SupportTicket, ticket_id)
    if not ticket:
        abort(404)
    if ticket.user_id != current_user.id and not current_user.has_permission("support_manage"):
        abort(403)

    if request.method == "POST":
        requested_status = request.form.get("status")
        if (requested_status in {"open", "answered", "closed"}
                and current_user.has_permission("support_manage")):
            ticket.status = requested_status
            ticket.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            flash("Ticket status updated.", "success")
            return redirect(url_for("support.support_ticket", ticket_id=ticket.id))

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
        return redirect(url_for("support.support_ticket", ticket_id=ticket.id))

    replies = (
        SupportMessage.query
        .filter_by(ticket_id=ticket.id)
        .order_by(SupportMessage.created_at.asc())
        .all()
    )
    return render_template("support_ticket.html", ticket=ticket, replies=replies)


# ---------------- Bill payments ----------------

@bp.route("/bills", methods=["GET", "POST"])
@login_required
def bills():
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
            create_notification(
                current_user.id, "Bill paid", f"{payee}: {money(amount)}"
            )
            record_audit(
                "bill_payment",
                current_user.id,
                f"payee={payee}; amount={amount}; reference={reference}",
            )
            db.session.commit()
            flash("Demo bill payment completed.", "success")

        return redirect(url_for("support.bills"))

    payments = (
        BillPayment.query
        .filter_by(user_id=current_user.id)
        .order_by(BillPayment.created_at.desc())
        .all()
    )
    return render_template("bills.html", payments=payments)
