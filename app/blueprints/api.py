from flask import Blueprint, jsonify
from ..extensions import db

bp = Blueprint("api", __name__)


@bp.route("/healthz")
def healthz():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify(status="ok", database=db_ok), 200 if db_ok else 503
