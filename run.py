import os
from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.extensions import socketio
from app.bootstrap import bootstrap_database

app = create_app()
bootstrap_database(app)

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        allow_unsafe_werkzeug=True,
    )
