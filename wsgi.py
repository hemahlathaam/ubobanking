import os
from dotenv import load_dotenv

load_dotenv()

from app import create_app
from app.bootstrap import bootstrap_database

app = create_app()
bootstrap_database(app)
