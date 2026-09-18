from flask import Blueprint, redirect, request, url_for, current_app, session

from ..branding import ORGS, DEFAULT_ORG

bp = Blueprint("brand", __name__)


@bp.route("/switch-org/<slug>", methods=["GET", "POST"])
def switch_org(slug):
    slug = (slug or "").lower()
    if slug not in ORGS:
        slug = DEFAULT_ORG

    nxt = request.args.get("next") or request.form.get("next") or url_for("dashboard.dashboard")
    if not nxt.startswith("/"):
        nxt = url_for("dashboard.dashboard")

    resp = redirect(nxt)
    session["org"] = slug
    resp.set_cookie(
        current_app.config["ORG_COOKIE_NAME"],
        slug,
        max_age=current_app.config["ORG_COOKIE_MAX_AGE"],
        samesite="Lax",
        httponly=False,
    )
    return resp
