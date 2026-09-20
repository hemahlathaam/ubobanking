from flask import Blueprint, jsonify

bp = Blueprint("api", __name__)


@bp.route("/healthz")
def healthz():
    """Lightweight liveness check — no DB touch, always 200."""
    return jsonify(status="ok"), 200
