"""Seed the isolated acceptance database (never the user's dev database).

Run from backend/ with the acceptance stack's database:
  DATABASE_URL=postgres://arkray:arkray_dev_only_password@127.0.0.1:55532/arkray \
  DJANGO_SETTINGS_MODULE=config.settings.local python seed_account_lifecycle.py

Accounts (all with the password in PASSWORD):
  Super admins: anita@acme.test (Anita Admin), bina@acme.test (Bina Boss)
  Admins:       tara@acme.test (Tara Lead; team Rahul, Priya, Mohan, Sana invited),
                omar@acme.test (Omar Khan; team Neha)
  Users:        rahul (3 leads, 2 open deals, a task), priya (1 lead, 1 open deal), mohan (nothing),
                neha (Omar's, 1 lead), vikram (no team), sana (Tara's, invited)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
assert "55532" in os.environ["DATABASE_URL"], "this script only seeds the acceptance database"

import django  # noqa: E402

django.setup()

from tests.factories import (  # noqa: E402
    AdminFactory,
    InvitedUserFactory,
    LeadFactory,
    OpportunityFactory,
    TaskFactory,
    TeamAdminFactory,
    UserFactory,
)

PASSWORD = "correct-horse-battery-staple"


def person(factory, first, last, **kwargs):
    return factory(
        first_name=first,
        last_name=last,
        email=f"{first.lower()}@acme.test",
        password=PASSWORD,
        **kwargs,
    )


anita = person(AdminFactory, "Anita", "Admin")
bina = person(AdminFactory, "Bina", "Boss")
tara = person(TeamAdminFactory, "Tara", "Lead")
omar = person(TeamAdminFactory, "Omar", "Khan")
rahul = person(UserFactory, "Rahul", "Sharma", manager=tara)
priya = person(UserFactory, "Priya", "Patel", manager=tara)
mohan = person(UserFactory, "Mohan", "Das", manager=tara)
sana = person(InvitedUserFactory, "Sana", "Invited", manager=tara)
neha = person(UserFactory, "Neha", "Rao", manager=omar)
vikram = person(UserFactory, "Vikram", "Free")

for n in range(3):
    lead = LeadFactory(owner=rahul, first_name=f"RahulLead{n}", last_name="Customer")
    if n < 2:
        OpportunityFactory(lead=lead, stage_key="new", title=f"Rahul deal {n}")
    if n == 0:
        TaskFactory(lead=lead, owner=rahul, created_by=rahul, title="Rahul follow-up call")
lead = LeadFactory(owner=priya, first_name="PriyaLead", last_name="Customer")
OpportunityFactory(lead=lead, stage_key="new", title="Priya deal")
lead = LeadFactory(owner=neha, first_name="NehaLead", last_name="Customer")

print("seeded", [u.email for u in (anita, bina, tara, omar, rahul, priya, mohan, sana, neha, vikram)])
