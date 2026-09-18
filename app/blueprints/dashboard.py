from decimal import Decimal, InvalidOperation
from flask import (
    Blueprint, render_template, redirect, url_for,
    request, flash, Response, abort,
)
from flask_login import login_required, current_user

from ..extensions import db
from ..models import Transaction, UserProfile
from ..utils import (
    generate_account_number, format_singapore_time, money,
)

bp = Blueprint("dashboard", __name__)


def _get_or_create_profile(user):
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    if not profile:
        profile = UserProfile(
            user_id=user.id,
            account_number=generate_account_number(),
        )
        db.session.add(profile)
        db.session.commit()
    return profile


@bp.route("/dashboard")
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
    return render_template("dashboard.html", transactions=transactions)


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user_profile = _get_or_create_profile(current_user)

    if request.method == "POST":
        name = request.form.get("display_name", "").strip()
        image = request.form.get("profile_image_url", "").strip()

        if len(name) < 2 or len(name) > 120:
            flash("Display name must be between 2 and 120 characters.", "error")
            return redirect(url_for("dashboard.profile"))

        if len(image) > 1000 or (image and not image.startswith(("http://", "https://"))):
            flash("Profile picture must use a valid http or https URL.", "error")
            return redirect(url_for("dashboard.profile"))

        current_user.display_name = name
        user_profile.profile_image_url = image
        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("dashboard.profile"))

    return render_template("profile.html", user_profile=user_profile)


@bp.route("/accounts")
@login_required
def accounts():
    return render_template("accounts.html")


@bp.route("/transactions")
@login_required
def transactions():
    search = request.args.get("search", "").strip()
    transaction_type = request.args.get("type", "all")

    query = Transaction.query.filter(
        (Transaction.sender_id == current_user.id)
        | (Transaction.receiver_id == current_user.id)
    )

    if transaction_type == "sent":
        query = query.filter(Transaction.sender_id == current_user.id)
    elif transaction_type == "received":
        query = query.filter(Transaction.receiver_id == current_user.id)

    if search:
        from ..models import User
        matching_users = User.query.filter(
            User.username.ilike(f"%{search}%")
            | User.display_name.ilike(f"%{search}%")
        ).all()
        matching_ids = [u.id for u in matching_users]

        conditions = [Transaction.note.ilike(f"%{search}%")]
        if matching_ids:
            conditions.extend([
                Transaction.sender_id.in_(matching_ids),
                Transaction.receiver_id.in_(matching_ids),
            ])
        query = query.filter(db.or_(*conditions))

    transaction_list = query.order_by(Transaction.created_at.desc()).all()
    return render_template(
        "transactions.html",
        transactions=transaction_list,
        search=search,
        transaction_type=transaction_type,
    )


@bp.route("/receipt/<int:transaction_id>")
@login_required
def transfer_receipt(transaction_id):
    transaction = db.session.get(Transaction, transaction_id)
    if not transaction:
        abort(404)
    if current_user.id not in {transaction.sender_id, transaction.receiver_id}:
        abort(403)

    reference_number = f"MB-{transaction.id:08d}"
    return render_template(
        "receipt.html",
        transaction=transaction,
        reference_number=reference_number,
    )


def _statement_rows(user_id):
    return (
        Transaction.query
        .filter(
            (Transaction.sender_id == user_id)
            | (Transaction.receiver_id == user_id)
        )
        .order_by(Transaction.created_at.desc())
        .all()
    )


@bp.route("/statements")
@login_required
def statements():
    return render_template(
        "statements.html",
        transactions=_statement_rows(current_user.id),
    )


@bp.route("/statements.csv")
@login_required
def statement_csv():
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "date", "direction", "counterparty", "amount", "status", "note"])

    for t in _statement_rows(current_user.id):
        outgoing = t.sender_id == current_user.id
        writer.writerow([
            t.id,
            format_singapore_time(t.created_at, "%Y-%m-%d %H:%M:%S SGT"),
            "debit" if outgoing else "credit",
            t.receiver.username if outgoing else t.sender.username,
            t.amount,
            t.status,
            t.note,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=statement.csv"},
    )


@bp.route("/statements.pdf")
@login_required
def statement_pdf():
    response = statement_csv()
    response.headers["X-PDF-Fallback"] = "CSV export used because PDF tooling is unavailable"
    response.headers["Content-Disposition"] = "attachment; filename=statement.csv"
    return response
