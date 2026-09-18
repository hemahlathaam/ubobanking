import os
from datetime import timedelta


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.environ.get("DATABASE_URL", "sqlite:///rubo.db")
    )

    # Sessions
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    # CSRF
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None

    # Rate limiting
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "600/hour"

    # SocketIO
    SOCKETIO_ASYNC_MODE = "threading"
    SOCKETIO_CORS_ALLOWED_ORIGINS = "*"

    # Admin bootstrap
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    # Branding
    DEFAULT_ORG = os.environ.get("DEFAULT_ORG", "rubo").lower()
    ORG_COOKIE_NAME = "rubo_org"
    ORG_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

    # Timezone
    DISPLAY_TIMEZONE = "SGT / Asia/Singapore (UTC+8)"
    SINGAPORE_TZ_NAME = "Asia/Singapore"


class DevConfig(Config):
    DEBUG = True


class ProdConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SOCKETIO_ASYNC_MODE = "gevent"
    PREFERRED_URL_SCHEME = "https"


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


def get_config():
    env = os.environ.get("FLASK_ENV", "development").lower()
    return {
        "production": ProdConfig,
        "testing": TestConfig,
    }.get(env, DevConfig)
