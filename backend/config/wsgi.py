import os

from django.core.wsgi import get_wsgi_application

# Serving HTTP must never fall back to DEBUG settings; development uses `manage.py runserver`.
# Only a fallback of last resort: importing this module imports the `config` package first,
# whose Celery app sets the development default when nothing else has (config/settings_module.py).
# gunicorn.conf.py sets the production default before the application is loaded, and the
# image sets the variable and ARKRAY_RUNTIME=production.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

application = get_wsgi_application()
