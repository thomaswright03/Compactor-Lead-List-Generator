"""Production entry point: gunicorn wsgi:app"""

from leadgen.envfile import load_dotenv
from leadgen.web import create_app

load_dotenv()
app = create_app()
