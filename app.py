import os
import secrets

from decimal import Decimal, InvalidOperation
from functools import wraps
from datetime import datetime, timezone

from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    abort,
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
from werkzeug.security import generate_password_hash, check_password_hash


# =========================================================
# APP SETUP
# =========================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-this-secret",
)

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


class SavingsAccount(db.Model):
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

    balance = db.Column(
        db.Numeric(12, 2),
        default=Decimal("0.00"),
        nullable=False,
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
    )


class InternalTransfer(db.Model):
    id = db.Column(
        db.Integer,
        primary_key=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False,
        index=True,
    )

    from_account = db.Column(
        db.String(20),
        nullable=False,
    )

    to_account = db.Column(
        db.String(20),
        nullable=False,
    )

    amount = db.Column(
        db.Numeric(12, 2),
        nullable=False,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
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
        if not current_user.is_admin:
            abort(403)

        return function(*args, **kwargs)

    return wrapped


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


def get_or_create_savings(user):
    savings = SavingsAccount.query.filter_by(
        user_id=user.id,
    ).first()

    if not savings:
        savings = SavingsAccount(
            user_id=user.id,
            balance=Decimal("0.00"),
        )

        db.session.add(savings)
        db.session.commit()

    return savings


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

    if current_user.is_admin:
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
            "current_profile": None,
            "current_savings": None,
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

    return {
        "unread_message_count": unread_count,
        "current_profile": get_or_create_profile(current_user),
        "current_savings": get_or_create_savings(current_user),
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
            get_or_create_savings(user)

            return redirect(url_for("dashboard"))

        flash(
            "Incorrect username or password.",
            "error",
        )

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()

    return redirect(url_for("login"))


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
        savings=get_or_create_savings(current_user),
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
    savings = get_or_create_savings(current_user)

    history = (
        InternalTransfer.query
        .filter_by(user_id=current_user.id)
        .order_by(InternalTransfer.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        "accounts.html",
        savings=savings,
        internal_transfers=history,
    )


@app.route("/accounts/move-money", methods=["POST"])
@login_required
def move_money():
    savings = get_or_create_savings(current_user)

    source = request.form.get(
        "from_account",
        "",
    ).strip()

    destination = request.form.get(
        "to_account",
        "",
    ).strip()

    valid_accounts = {
        "checking",
        "savings",
    }

    if (
        source not in valid_accounts
        or destination not in valid_accounts
        or source == destination
    ):
        flash(
            "Choose two different valid accounts.",
            "error",
        )

        return redirect(url_for("accounts"))

    try:
        amount = Decimal(
            request.form.get("amount", "")
        ).quantize(Decimal("0.01"))

    except (InvalidOperation, ValueError):
        flash(
            "Enter a valid amount.",
            "error",
        )

        return redirect(url_for("accounts"))

    if amount <= 0:
        flash(
            "Amount must be greater than zero.",
            "error",
        )

        return redirect(url_for("accounts"))

    checking_balance = Decimal(current_user.balance)
    savings_balance = Decimal(savings.balance)

    if source == "checking":
        if amount > checking_balance:
            flash(
                "Insufficient checking balance.",
                "error",
            )

            return redirect(url_for("accounts"))

        current_user.balance = checking_balance - amount
        savings.balance = savings_balance + amount

    else:
        if amount > savings_balance:
            flash(
                "Insufficient savings balance.",
                "error",
            )

            return redirect(url_for("accounts"))

        savings.balance = savings_balance - amount
        current_user.balance = checking_balance + amount

    internal_transfer = InternalTransfer(
        user_id=current_user.id,
        from_account=source,
        to_account=destination,
        amount=amount,
    )

    db.session.add(internal_transfer)
    db.session.commit()

    send_live_update(
        current_user.id,
        "balance_changed",
        checking_balance=str(current_user.balance),
        savings_balance=str(savings.balance),
    )

    flash(
        f"Moved {money(amount)} from "
        f"{source.title()} to {destination.title()}.",
        "success",
    )

    return redirect(url_for("accounts"))


# =========================================================
# USER-TO-USER TRANSFERS
# =========================================================

@app.route("/transfer", methods=["GET", "POST"])
@login_required
def transfer():
    if request.method == "POST":
        receiver_username = request.form.get(
            "receiver_username",
            "",
        ).strip()

        receiver = User.query.filter_by(
            username=receiver_username,
        ).first()

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

        current_user.balance = (
            Decimal(current_user.balance) - amount
        )

        receiver.balance = (
            Decimal(receiver.balance) + amount
        )

        note = request.form.get(
            "note",
            "",
        ).strip()[:200]

        transaction = Transaction(
            sender_id=current_user.id,
            receiver_id=receiver.id,
            amount=amount,
            note=note,
        )

        db.session.add(transaction)
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

    return render_template("transfer.html")


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

@app.route("/admin")
@admin_required
def admin_panel():
    users = User.query.order_by(
        User.username.asc()
    ).all()

    return render_template(
        "admin.html",
        users=users,
    )


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

    user = User(
        username=username,
        display_name=display_name,
        balance=balance,
        is_admin=(
            request.form.get("is_admin") == "on"
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

    savings = SavingsAccount(
        user_id=user.id,
        balance=Decimal("0.00"),
    )

    db.session.add(profile)
    db.session.add(savings)
    db.session.commit()

    flash(
        "Account created.",
        "success",
    )

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

    Transaction.query.filter(
        (Transaction.sender_id == user.id)
        | (Transaction.receiver_id == user.id)
    ).delete(synchronize_session=False)

    Message.query.filter(
        (Message.sender_id == user.id)
        | (Message.receiver_id == user.id)
    ).delete(synchronize_session=False)

    MessageReadState.query.filter(
        (MessageReadState.user_id == user.id)
        | (MessageReadState.other_user_id == user.id)
    ).delete(synchronize_session=False)

    InternalTransfer.query.filter_by(
        user_id=user.id,
    ).delete(synchronize_session=False)

    SavingsAccount.query.filter_by(
        user_id=user.id,
    ).delete(synchronize_session=False)

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
    )


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

    return render_template(
        "chat.html",
        other_user=other_user,
        conversation=conversation,
    )


# =========================================================
# CREATE DATABASE AND ADMIN ACCOUNT
# =========================================================

with app.app_context():
    db.create_all()

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

    # If ADMIN_PASSWORD exists in Render, it will create
    # the admin or reset the existing admin password.
    if admin_password:
        if existing_admin:
            existing_admin.set_password(admin_password)
            existing_admin.is_admin = True
            existing_admin.is_enabled = True

        else:
            existing_admin = User(
                username=admin_username,
                display_name="Administrator",
                balance=Decimal("10000.00"),
                is_admin=True,
                is_enabled=True,
            )

            existing_admin.set_password(admin_password)

            db.session.add(existing_admin)
            db.session.flush()

            admin_profile = UserProfile(
                user_id=existing_admin.id,
                account_number=generate_account_number(),
            )

            admin_savings = SavingsAccount(
                user_id=existing_admin.id,
                balance=Decimal("0.00"),
            )

            db.session.add(admin_profile)
            db.session.add(admin_savings)

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

    # Create missing savings accounts for old users
    users_without_savings = (
        User.query
        .outerjoin(
            SavingsAccount,
            User.id == SavingsAccount.user_id,
        )
        .filter(SavingsAccount.id.is_(None))
        .all()
    )

    for user in users_without_savings:
        savings = SavingsAccount(
            user_id=user.id,
            balance=Decimal("0.00"),
        )

        db.session.add(savings)

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
