import os

from django.core.wsgi import get_wsgi_application

# Serving HTTP must never fall back to DEBUG settings; development uses `manage.py runserver`.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_wsgi_application()
