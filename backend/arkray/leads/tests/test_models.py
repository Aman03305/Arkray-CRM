"""Database-level invariants of the leads tables: they hold even for writes that bypass the
services (raw SQL, a future bug)."""

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from arkray.leads.models import Lead, LeadSource, LeadStatus, StatusCategory
from tests.factories import LeadFactory

pytestmark = pytest.mark.django_db


def violates(constraint: str, sql: str, params=()) -> None:
    # Foreign keys are checked at commit (DEFERRABLE); tests never commit.
    with (  # noqa: PT012 — the statement under test needs the preceding SET
        pytest.raises(IntegrityError, match=constraint),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(sql, params)


class TestSeededConfiguration:
    def test_the_initial_statuses_in_order_with_one_default(self):
        rows = list(LeadStatus.objects.order_by("position").values_list("key", "name", "category"))
        assert rows == [
            ("new", "New", StatusCategory.OPEN),
            ("contacted", "Contacted", StatusCategory.OPEN),
            ("qualified", "Qualified", StatusCategory.QUALIFIED),
            ("unqualified", "Unqualified", StatusCategory.UNQUALIFIED),
            ("converted", "Converted", StatusCategory.CONVERTED),
        ]
        assert list(LeadStatus.objects.filter(is_default=True).values_list("key", flat=True)) == [
            "new"
        ]

    def test_the_initial_sources_in_order(self):
        assert list(LeadSource.objects.order_by("position").values_list("name", flat=True)) == [
            "Website",
            "Referral",
            "Campaign",
            "Cold Call",
            "Email",
            "Event",
            "Partner",
            "Other",
        ]


class TestConstraints:
    def test_a_lead_needs_a_person_or_organisation(self):
        lead = LeadFactory()
        violates(
            "leads_lead_name_present",
            "UPDATE leads_lead SET first_name='', last_name='', organization_name='' WHERE id=%s",
            [lead.pk],
        )

    def test_unknown_status_and_source_are_refused_by_foreign_keys(self):
        lead = LeadFactory()
        violates("status_key", "UPDATE leads_lead SET status_key='won' WHERE id=%s", [lead.pk])
        violates("source_key", "UPDATE leads_lead SET source_key='tv' WHERE id=%s", [lead.pk])

    def test_rating_country_and_version_are_checked(self):
        lead = LeadFactory()
        violates(
            "leads_lead_rating_valid", "UPDATE leads_lead SET rating='tepid' WHERE id=%s", [lead.pk]
        )
        violates(
            "leads_lead_country_format", "UPDATE leads_lead SET country='in' WHERE id=%s", [lead.pk]
        )
        violates(
            "leads_lead_version_positive", "UPDATE leads_lead SET version=0 WHERE id=%s", [lead.pk]
        )

    def test_an_app_server_clock_slightly_behind_never_fails_an_edit(self, monkeypatch):
        """Review regression: a CHECK updated_at >= created_at turned milliseconds of clock
        skew between app servers into a 500 on edit. Timestamps are informative, not a rule."""
        from datetime import timedelta

        from django.utils import timezone

        from arkray.core.access import AccessScope
        from arkray.leads import services

        lead = LeadFactory()
        behind = lead.created_at - timedelta(milliseconds=50)
        monkeypatch.setattr(timezone, "now", lambda: behind)
        updated = services.update_lead(
            actor=lead.owner,
            scope=AccessScope.own(lead.owner_id),
            lead_id=lead.pk,
            version=1,
            changes={"city": "Pune"},
        )
        assert updated.city == "Pune"

    def test_owner_must_be_a_real_user(self):
        lead = LeadFactory()
        violates(
            "owner_id",
            "UPDATE leads_lead SET owner_id='5a1e4d2c-0000-4000-8000-00000000abcd' WHERE id=%s",
            [lead.pk],
        )

    def test_statuses_in_use_cannot_be_deleted(self):
        LeadFactory(status_id="contacted")
        violates("status_key", "DELETE FROM leads_lead_status WHERE key='contacted'")

    def test_only_one_default_status_and_it_must_be_active(self):
        violates(
            "leads_lead_status_one_default",
            "UPDATE leads_lead_status SET is_default=true WHERE key='contacted'",
        )
        violates(
            "leads_lead_status_default_is_active",
            "UPDATE leads_lead_status SET is_active=false WHERE key='new'",
        )

    def test_configuration_keys_have_a_fixed_format(self):
        violates(
            "leads_lead_source_key_format",
            "INSERT INTO leads_lead_source (id, key, name, position, is_active)"
            " VALUES (gen_random_uuid(), 'Bad Key', 'Bad', 1, true)",
        )


class TestDerivedColumns:
    @pytest.mark.parametrize(
        ("first", "last", "organization", "display"),
        [
            ("Rahul", "Sharma", "Apollo", "Rahul Sharma"),
            ("Rahul", "", "Apollo", "Rahul"),
            ("", "Sharma", "", "Sharma"),
            ("", "", "Apollo Diagnostics", "Apollo Diagnostics"),
            ("முருகன்", "", "", "முருகன்"),
        ],
    )
    def test_display_name_is_derived_by_the_database(self, first, last, organization, display):
        lead = LeadFactory(first_name=first, last_name=last, organization_name=organization)
        assert Lead.objects.values_list("display_name", flat=True).get(pk=lead.pk) == display

    def test_display_name_follows_every_edit_and_cannot_be_written(self):
        lead = LeadFactory(first_name="Rahul", last_name="Sharma")
        Lead.objects.filter(pk=lead.pk).update(last_name="Verma")
        assert Lead.objects.values_list("display_name", flat=True).get(pk=lead.pk) == "Rahul Verma"
        with (
            pytest.raises(DatabaseError, match="generated"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("UPDATE leads_lead SET display_name='Someone' WHERE id=%s", [lead.pk])

    def test_search_text_holds_names_organisation_email_and_phone_digits(self):
        lead = LeadFactory(
            first_name="Priya",
            last_name="Patel",
            organization_name="Apollo",
            email="priya@apollo.example",
            phone="+91 98765 43210",
            mobile="(022) 2345-6789",
            description="secret notes stay out of search",
            city="Pune",
        )
        text = Lead.objects.values_list("search_text", flat=True).get(pk=lead.pk)
        for fragment in [
            "PRIYA",
            "PATEL",
            "APOLLO",
            "PRIYA@APOLLO.EXAMPLE",
            "919876543210",
            "02223456789",
        ]:
            assert fragment in text
        assert "SECRET" not in text
        assert "PUNE" not in text

    def test_phone_keys_follow_the_numbers_on_every_save(self):
        lead = LeadFactory(phone="+91 98765 43210", mobile="+91-98765-43210")
        assert lead.phone_keys == ["+919876543210"]
        lead.alternate_phone = "022 2345 6789"
        lead.save(update_fields=["alternate_phone"])
        lead.refresh_from_db()
        assert lead.phone_keys == ["+919876543210", "02223456789"]

    def test_str_never_contains_contact_data(self):
        lead = LeadFactory(first_name="Priya", email="priya@apollo.example")
        assert "Priya" not in str(lead)
        assert "apollo" not in str(lead)
