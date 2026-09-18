from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate

db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
migrate = Migrate()

login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "error"
