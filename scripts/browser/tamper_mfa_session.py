"""Make a browser session's last second-factor verification old (as if 20 minutes had passed)."""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
assert "55532" in os.environ["DATABASE_URL"]
import django  # noqa: E402

django.setup()
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402

key = sys.argv[1]
store = SessionStore(session_key=key)
data = store.load()
assert "_arkray_mfa_at" in data, f"this session has no verification to age: {sorted(data)}"
store["_arkray_mfa_at"] = time.time() - 20 * 60
store.save()
print("aged")
