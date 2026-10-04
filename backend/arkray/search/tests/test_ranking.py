"""Ranking (core.ranking, docs/search.md#ranking): match tier against the label, then newest,
then id; at most 5 per kind with a "more" flag; only the newest WINDOW candidates ranked;
the same data always in the same order. And the note preview: bounded, around the match."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.core import ranking
from arkray.search.selectors import LIMIT
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in

from .conftest import ids, search

pytestmark = pytest.mark.django_db
NOW = timezone.now()


@pytest.fixture
def client(user_a):
    return signed_in(user_a)


def lead(owner, first, last="", *, age_days=0, **extra):
    return LeadFactory(
        owner=owner,
        first_name=first,
        last_name=last,
        created_at=NOW - timedelta(days=age_days),
        **extra,
    )


class TestTiers:
    def test_exact_then_prefix_then_word_then_elsewhere(self, client, user_a):
        """The newest record is the weakest match, the oldest the strongest: tiers win."""
        elsewhere = lead(user_a, "Zed", "Zed", email="ravi@example.test", age_days=0)
        inside = lead(user_a, "Bravi", "Zed", age_days=1)  # "ravi" inside a word
        word = lead(user_a, "Dr", "Ravi Kumar", age_days=2)
        prefix = lead(user_a, "Ravi", "Shankar", age_days=3)
        exact = lead(user_a, "Ravi", "", age_days=4)
        assert ids(search(client, "ravi"), "leads") == [
            str(exact.pk),
            str(prefix.pk),
            str(word.pk),
            str(elsewhere.pk),
            str(inside.pk),
        ]

    def test_the_whole_query_is_compared_including_short_words(self, client, user_a):
        """ "Rahul S" searches the word "Rahul", but "Rahul S…" is the better match."""
        other = lead(user_a, "Rahul", "Verma", age_days=0)
        meant = lead(user_a, "Rahul", "Sharma", age_days=5)
        assert ids(search(client, "Rahul S"), "leads") == [str(meant.pk), str(other.pk)]

    def test_case_never_changes_the_tier(self, client, user_a):
        older_exact = lead(user_a, "ACME", "", age_days=3, organization_name="")
        newer_prefix = lead(user_a, "acme", "Corp", age_days=0)
        assert ids(search(client, "Acme"), "leads") == [str(older_exact.pk), str(newer_prefix.pk)]

    def test_titles_rank_the_same_way(self, client, user_a):
        owner_lead = lead(user_a, "Title", "Owner")
        word = OpportunityFactory(lead=owner_lead, title="Annual reagent contract")
        prefix = OpportunityFactory(lead=owner_lead, title="Reagent supply")
        exact = OpportunityFactory(lead=owner_lead, title="REAGENT")
        assert ids(search(client, "reagent"), "opportunities") == [
            str(exact.pk),
            str(prefix.pk),
            str(word.pk),
        ]


class TestTieBreaks:
    def test_newest_first_then_id(self, client, user_a):
        same_time = NOW - timedelta(days=1)
        a = lead(user_a, "Tie", "One")
        b = LeadFactory(owner=user_a, first_name="Tie", last_name="Two", created_at=same_time)
        c = LeadFactory(owner=user_a, first_name="Tie", last_name="Three", created_at=same_time)
        tied = sorted([b, c], key=lambda x: x.pk, reverse=True)
        assert ids(search(client, "tie"), "leads") == [str(a.pk), *(str(x.pk) for x in tied)]

    def test_the_same_data_always_gives_the_same_order(self, client, user_a):
        for n in range(12):
            LeadFactory(owner=user_a, first_name="Stable", last_name=f"N{n % 3}", created_at=NOW)
        first = search(client, "stable")
        for _ in range(5):
            assert search(client, "stable") == first


class TestLimits:
    def test_at_most_five_per_kind_with_a_more_flag(self, client, user_a):
        owner_lead = lead(user_a, "Bulk", "Owner")
        for n in range(LIMIT + 1):
            lead(user_a, "Bulk", f"Lead{n}", age_days=n + 1)
            TaskFactory(lead=owner_lead, title=f"Bulk task {n}")
        body = search(client, "bulk")
        assert len(body["leads"]["results"]) == LIMIT
        assert body["leads"]["has_more"] is True
        assert len(body["tasks"]["results"]) == LIMIT
        assert body["tasks"]["has_more"] is True

    def test_exactly_five_is_not_more(self, client, user_a):
        for n in range(LIMIT):
            lead(user_a, "Five", f"N{n}")
        body = search(client, "five")
        assert (len(body["leads"]["results"]), body["leads"]["has_more"]) == (LIMIT, False)

    def test_only_the_newest_window_of_matches_is_ranked(self, client, user_a, monkeypatch):
        """The bound that keeps a common word cheap: an exact match older than the WINDOW
        newest matches is not ranked (more specific words find it)."""
        monkeypatch.setattr(ranking, "WINDOW", 3)
        old_exact = lead(user_a, "Window", "", age_days=10)
        newer = [lead(user_a, "Window", f"Later{n}", age_days=n) for n in range(3)]
        body = search(client, "window")
        assert str(old_exact.pk) not in ids(body, "leads")
        assert set(ids(body, "leads")) == {str(x.pk) for x in newer}
        monkeypatch.setattr(ranking, "WINDOW", 4)
        assert ids(search(client, "window"), "leads")[0] == str(old_exact.pk)


class TestNotePreview:
    def test_a_short_note_is_shown_whole(self, client, user_a):
        NoteFactory(lead=lead(user_a, "Note", "Lead"), description="Prefers morning calls.")
        (row,) = search(client, "morning")["notes"]["results"]
        assert row["preview"] == "Prefers morning calls."
        assert (row["preview_truncated"], row["preview_starts_mid_text"]) == (False, False)

    def test_a_long_note_shows_240_characters_around_the_first_word(self, client, user_a):
        body = "a" * 1000 + " MARKERWORD " + "b" * 1000
        NoteFactory(lead=lead(user_a, "Note", "Lead"), description=body)
        (row,) = search(client, "markerword")["notes"]["results"]
        assert len(row["preview"]) == 240
        assert "MARKERWORD" in row["preview"]
        assert row["preview"].index("MARKERWORD") == 60  # 60 characters of context before it
        assert (row["preview_truncated"], row["preview_starts_mid_text"]) == (True, True)

    def test_a_match_near_the_start_shows_the_start(self, client, user_a):
        NoteFactory(lead=lead(user_a, "Note", "Lead"), description="Quotation " + "x" * 500)
        (row,) = search(client, "quotation")["notes"]["results"]
        assert row["preview"].startswith("Quotation")
        assert (row["preview_truncated"], row["preview_starts_mid_text"]) == (True, False)

    def test_the_preview_follows_the_first_search_word(self, client, user_a):
        body = "alpha " + "x" * 400 + " omega"
        NoteFactory(lead=lead(user_a, "Note", "Lead"), description=body)
        row = search(client, "omega alpha")["notes"]["results"][0]
        assert "omega" in row["preview"]
        assert "alpha" not in row["preview"]

    def test_the_whole_body_never_leaves_the_database(self, user_a):
        """The note's body is neither selected nor loaded: only the cut preview is."""
        from arkray.activities.models import ActivityType
        from arkray.activities.selectors import search as activity_search
        from arkray.core.access import AccessScope
        from arkray.core.ranking import SearchQuery

        NoteFactory(lead=lead(user_a, "Note", "Lead"), description="body " * 1000 + "needle")
        (note,) = activity_search(
            AccessScope.own(user_a.pk), SearchQuery.parse("needle"), ActivityType.NOTE, limit=5
        ).items
        assert "description" in note.get_deferred_fields()
        assert len(note.text_preview) <= 241
