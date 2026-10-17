"""A Super admin with a second factor (secret written to mfa-secret.txt) and a user to act on."""
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
assert "55532" in os.environ["DATABASE_URL"]
import django  # noqa: E402

django.setup()
from tests.factories import AdminFactory, UserFactory  # noqa: E402
from tests.mfa_helpers import enrol  # noqa: E402

mira = AdminFactory(first_name="Mira", last_name="Stepup", email="mira@acme.test", password="correct-horse-battery-staple")
quinn = UserFactory(first_name="Quinn", last_name="Target", email="quinn@acme.test", password="correct-horse-battery-staple")
UserFactory(first_name="Ravi", last_name="Second", email="ravi@acme.test", password="correct-horse-battery-staple")
enrolment = enrol(mira)
open(os.environ["SECRET_FILE"], "w").write(enrolment.secret)
print("seeded mira + quinn + ravi; secret written")
