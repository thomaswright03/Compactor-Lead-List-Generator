"""Production entry point: gunicorn wsgi:app"""

import os

from leadgen import awake
from leadgen.envfile import load_dotenv
from leadgen.web import create_app

load_dotenv()
app = create_app()
# Notice a sleep during Utah working hours (on Render, by the deployed commit) and get the
# first page ready, in the background (awake.py).
awake.start(os.environ.get("RENDER_GIT_COMMIT", "")[:7])
