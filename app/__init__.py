import click
from flask import Flask, render_template

from .config import get_config
from .extensions import (
    db, login_manager, socketio, csrf, limiter, migrate,
)
from .utils import register_jinja_helpers


def create_app(config_object=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")

    cfg = config_object or get_config()
    app.config.from_object(cfg)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "SECRET_KEY must be set to a long random value before starting RUBO."
        )

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(
        app,
        async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
        cors_allowed_origins=app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "*"),
    )

    register_jinja_helpers(app)

    from .blueprints.auth import bp as auth_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.transfers import bp as transfers_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.chat import bp as chat_bp
    from .blueprints.developer import bp as developer_bp
    from .blueprints.support import bp as support_bp
    from .blueprints.brand import bp as brand_bp
    from .blueprints.api import bp as api_bp

    app.register_blueprint(brand_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(transfers_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(developer_bp)
    app.register_blueprint(support_bp)
    app.register_blueprint(api_bp)

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.cli.command("init-db")
    def init_db_command():
        from .bootstrap import bootstrap_database
        bootstrap_database(app)
        click.echo("Database initialized.")

    return app


app = create_app()
