from decimal import Decimal
from sqlalchemy import inspect, text

from .extensions import db
from .models import User, UserProfile
from .utils import generate_account_number


def bootstrap_database(app):
    """Create tables, migrate legacy columns, seed admin. Runs once at startup."""
    with app.app_context():
        db.create_all()

        user_columns = {c["name"] for c in inspect(db.engine).get_columns("user")}
        if "account_type" not in user_columns:
            db.session.execute(text(
                'ALTER TABLE "user" ADD COLUMN account_type '
                "VARCHAR(20) NOT NULL DEFAULT 'standard'"
            ))
            db.session.commit()

        admin_username = app.config["ADMIN_USERNAME"]
        admin_password = app.config["ADMIN_PASSWORD"]
        admin = User.query.filter_by(username=admin_username).first()

        if not admin and not admin_password:
            raise RuntimeError("ADMIN_PASSWORD must be set on a fresh database.")

        if admin_password:
            if admin:
                admin.set_password(admin_password)
                admin.is_admin = True
                admin.account_type = "admin"
                admin.is_enabled = True
            else:
                admin = User(
                    username=admin_username,
                    display_name="Administrator",
                    balance=Decimal("10000.00"),
                    is_admin=True,
                    account_type="admin",
                    is_enabled=True,
                )
                admin.set_password(admin_password)
                db.session.add(admin)
                db.session.flush()
                db.session.add(UserProfile(
                    user_id=admin.id,
                    account_number=generate_account_number(),
                ))
            db.session.commit()

        missing = (
            User.query
            .outerjoin(UserProfile, User.id == UserProfile.user_id)
            .filter(UserProfile.id.is_(None))
            .all()
        )
        for u in missing:
            db.session.add(UserProfile(
                user_id=u.id,
                account_number=generate_account_number(),
            ))
        db.session.commit()
