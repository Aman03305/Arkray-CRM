"""Field rules for leads (arkray.leads.phones, arkray.leads.validation)."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from arkray.core.errors import InvalidInputError
from arkray.leads.countries import COUNTRY_CODES
from arkray.leads.phones import InvalidPhone, clean_phone, phone_key, search_digits
from arkray.leads.validation import NAME_REQUIRED, clean_fields, require_identity


class TestPhones:
    @pytest.mark.parametrize(
        ("typed", "key"),
        [
            ("+91 98765 43210", "+919876543210"),
            ("+91-98765-43210", "+919876543210"),
            ("0091 98765 43210", "+919876543210"),  # "00" international prefix
            ("098765 43210", "09876543210"),  # national: no country is assumed
            ("(022) 2345 6789", "02223456789"),
            ("+44 20 7946 0958", "+442079460958"),
            ("+1 (555) 010-9999 ext. 12", "+15550109999x12"),
            ("+1 555 010 9999 x 7", "+15550109999x7"),
            ("+91 22 2345 6789;ext=45", "+912223456789x45"),
            ("022 2345 6789 #3", "02223456789x3"),
            ("1800.123.456", "1800123456"),
        ],
    )
    def test_numbers_are_kept_as_typed_and_keyed_canonically(self, typed, key):
        assert clean_phone(typed) == typed  # never reformatted: "+", spacing, extension kept
        assert phone_key(typed) == key

    def test_same_number_different_spelling_same_key(self):
        assert (
            phone_key("+91 98765 43210")
            == phone_key("+91-9876-543210")
            == phone_key("0091 9876543210")
        )

    def test_extensions_distinguish_people_on_one_switchboard(self):
        assert phone_key("+91 22 2345 6789 ext 1") != phone_key("+91 22 2345 6789 ext 2")

    @pytest.mark.parametrize(
        "typed",
        [
            "1234",  # too short
            "+1 234 567 890 123 456 789",  # too long (18 digits)
            "98765 43210 +",  # "+" only at the start
            "++91 98765 43210",
            "1-800-FLOWERS",
            "call me",
            "98765 43210 ext 12345678",  # extension too long
            "+91 98765 43210; DROP TABLE",
        ],
    )
    def test_invalid_numbers_are_refused(self, typed):
        with pytest.raises(InvalidPhone):
            clean_phone(typed)

    def test_blank_is_allowed_and_has_no_key(self):
        assert clean_phone("") == ""
        assert phone_key("") == ""

    @pytest.mark.parametrize(
        ("term", "digits"),
        [
            ("98765-43210", "9876543210"),
            ("+91", "91"),
            ("(022)", "022"),
            ("rahul", None),
            ("5", None),
        ],
    )
    def test_phone_like_search_terms_are_reduced_to_digits(self, term, digits):
        assert search_digits(term) == digits


class TestCleanFields:
    def test_every_rule_reports_its_own_field(self):
        with pytest.raises(InvalidInputError) as caught:
            clean_fields(
                {
                    "email": "not-an-email",
                    "phone": "call me",
                    "postal_code": "40/001",
                    "country": "XX",
                    "rating": "lukewarm",
                    "first_name": "a" * 101,
                }
            )
        assert set(caught.value.details) == {
            "email",
            "phone",
            "postal_code",
            "country",
            "rating",
            "first_name",
        }

    def test_values_are_stored_in_canonical_form(self):
        cleaned = clean_fields(
            {
                "first_name": "  Priya  ",
                "organization_name": "Apollo   Hospitals",
                "email": " Priya.Patel@Apollo.Example ",
                "country": "in",
                "postal_code": "SW1A 1AA",
                "rating": "",
                "source": "",
                "description": "Line 1  \r\nLine 2\n\n",
            }
        )
        assert cleaned == {
            "first_name": "Priya",
            "organization_name": "Apollo Hospitals",
            "email": "Priya.Patel@Apollo.Example",  # contact data: case kept as typed
            "country": "IN",
            "postal_code": "SW1A 1AA",
            "rating": None,
            "source": None,
            "description": "Line 1\nLine 2",
        }

    @pytest.mark.parametrize(
        "email",
        [
            "x?to=sales%40client.com&bcc=spy%40evil.example&body=P9XSS&x=@client.com",
            "a&b@client.example",
            "a=b@client.example",
            "a%40b@client.example",
            "a#b@client.example",
            '"quoted"@client.example',
        ],
    )
    def test_mailto_header_delimiters_are_refused(self, email):
        """Phase 9 review: the email standard allows these before the "@", but in a
        mailto link they add recipients (Bcc) or text to the colleague's draft."""
        with pytest.raises(InvalidInputError) as raised:
            clean_fields({"email": email})
        assert "email" in raised.value.details

    def test_email_is_optional_and_international_domains_are_accepted(self):
        assert clean_fields({"email": ""}) == {"email": ""}
        assert clean_fields({"email": "info@münchen.example"}) == {"email": "info@münchen.example"}

    def test_every_country_code_is_iso_alpha_2(self):
        assert len(COUNTRY_CODES) == 249
        assert all(len(c) == 2 and c.isascii() and c.isupper() for c in COUNTRY_CODES)
        assert {"IN", "US", "GB", "JP", "AE", "SG"} <= COUNTRY_CODES

    def test_last_contacted_must_be_aware_past_and_plausible(self):
        now = timezone.now()
        assert clean_fields({"last_contacted_at": now}) == {"last_contacted_at": now}
        assert clean_fields({"last_contacted_at": None}) == {"last_contacted_at": None}
        for bad in [
            now + timedelta(hours=1),
            datetime(1999, 12, 31, tzinfo=UTC),
            datetime(2026, 1, 1),  # noqa: DTZ001 — naive on purpose
            "2026-01-01",
        ]:
            with pytest.raises(InvalidInputError):
                clean_fields({"last_contacted_at": bad})

    def test_a_small_clock_skew_is_tolerated(self):
        slightly_ahead = timezone.now() + timedelta(minutes=2)
        assert clean_fields({"last_contacted_at": slightly_ahead})

    def test_description_is_bounded(self):
        assert clean_fields({"description": "x" * 5000})
        with pytest.raises(InvalidInputError):
            clean_fields({"description": "x" * 5001})

    @pytest.mark.parametrize("field", ["owner", "owner_id", "status", "created_by", "archived_at"])
    def test_system_fields_are_never_accepted(self, field):
        with pytest.raises(InvalidInputError):
            clean_fields({field: "x"})

    def test_non_text_values_are_refused(self):
        with pytest.raises(InvalidInputError):
            clean_fields({"first_name": 42, "description": ["x"]})


class TestIdentityRequirement:
    @pytest.mark.parametrize(
        ("first", "last", "organization"),
        [("Rahul", "", ""), ("", "Sharma", ""), ("", "", "Apollo Diagnostics"), ("முருகன்", "", "")],
    )
    def test_a_person_or_an_organisation_is_enough(self, first, last, organization):
        require_identity(first, last, organization)

    def test_nobody_at_all_is_refused(self):
        with pytest.raises(InvalidInputError) as caught:
            require_identity("", "", "")
        assert caught.value.details == {"first_name": [NAME_REQUIRED]}
