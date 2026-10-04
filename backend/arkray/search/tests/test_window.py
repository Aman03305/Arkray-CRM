"""The ranking window (core.ranking): whichever pass finds them, the ranked candidates are
exactly the WINDOW newest matches, so results never depend on the query plan. Checked
against a brute-force reference for many sizes of the recent pass and the window, which
makes the older pass, its gates and its boundary run in every combination.

And the SQL ranking composes holds no searched text: every value is a bound parameter."""

from __future__ import annotations

import random
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from arkray.core import ranking
from arkray.core.access import AccessScope
from arkray.core.ranking import SearchQuery
from arkray.leads import selectors as lead_selectors
from arkray.leads.models import Lead
from tests.factories import LeadFactory, UserFactory

pytestmark = pytest.mark.django_db

WORDS = ["alpha", "beta", "gamma", "delta", "alphabet", "betamax", "omega"]


def looked_up(terms: list[str]) -> list[str]:
    """What the index looks up for the terms (core.ranking._looked_up, for ASCII): every
    2-character word and the trigrams of longer ones, the first CANDIDATE_PATTERNS."""
    parts: set[str] = set()
    for term in terms:
        for word in re.findall(r"[A-Z0-9]{2,}", term):
            parts.update(word[i : i + 3] for i in range(max(len(word) - 2, 1)))
    return sorted(parts)[: ranking.CANDIDATE_PATTERNS]


def reference(owner, query: SearchQuery, window: int, recent: int, limit: int = 5) -> list[str]:
    """Brute force, in Python: every live lead of `owner` whose search text contains every
    term (ASCII data, so Python's and PostgreSQL's upper() agree), newest first, the newest
    `window` of those, then by tier, newest, id. Only the `recent` newest leads are searched
    when there are more and `window` of them match or contain what the index looks up."""
    leads = sorted(
        Lead.objects.filter(owner=owner, archived_at__isnull=True),
        key=lambda x: (x.created_at, x.pk),
        reverse=True,
    )
    terms = [t.upper() for t in query.terms]
    parts = looked_up(terms)
    if len(leads) > recent:
        near = [
            x
            for x in leads[:recent]
            if all(p in x.search_text for p in parts) or all(t in x.search_text for t in terms)
        ]
        if len(near) >= window:
            leads = leads[:recent]
    matches = [x for x in leads if all(t in x.search_text for t in terms)]
    text = query.text.upper()

    def tier(lead):
        label = lead.display_name.upper()
        if label == text:
            return 0
        if label.startswith(text):
            return 1
        return 2 if f" {text}" in label else 3

    ranked = sorted(
        matches[:window],
        key=lambda x: (tier(x), -x.created_at.timestamp(), [-b for b in x.pk.bytes]),
    )
    return [str(x.pk) for x in ranked[:limit]]


@pytest.fixture
def owner():
    owner = UserFactory()
    rng = random.Random(7)
    now = timezone.now()
    for n in range(80):
        words = rng.sample(WORDS, 2)
        LeadFactory(
            owner=owner,
            first_name=words[0].title(),
            last_name=words[1].title(),
            organization_name="",
            email="",
            created_at=now - timedelta(minutes=rng.randint(0, 40)),  # ties on purpose
            archived_at=now if n % 9 == 0 else None,
        )
    # Older, with punctuation: "alpha-" matches only these, while every "alpha" lead contains
    # what the index looks up for it (the older pass's candidate gate).
    for n in range(3):
        LeadFactory(
            owner=owner,
            first_name="Alpha-Omega",
            last_name=f"Old{n}",
            organization_name="",
            email="",
            created_at=now - timedelta(hours=2, minutes=n),
        )
    # Someone else's leads, matching everything, newer than all of the owner's.
    other = UserFactory()
    for word in WORDS:
        LeadFactory(owner=other, first_name=word, last_name=word, created_at=now)
    return owner


@pytest.mark.parametrize(
    ("recent", "window"),
    [(5, 3), (10, 4), (3, 50), (20, 20), (80, 5), (200, 100), (1, 1), (7, 1)],
)
@pytest.mark.parametrize(
    "q", ["alpha", "beta", "alphabet", "ega", "alpha beta", "Beta Alpha", "alpha-", "alpha-omega"]
)
def test_the_window_is_the_newest_matches_whichever_pass_finds_them(
    owner, monkeypatch, recent, window, q
):
    monkeypatch.setattr(ranking, "RECENT", recent)
    monkeypatch.setattr(ranking, "WINDOW", window)
    query = SearchQuery.parse(q)
    found = lead_selectors.search(AccessScope.own(owner.pk), query, limit=5)
    assert [str(x.pk) for x in found.items] == reference(owner, query, window, recent)


def test_the_sql_holds_no_searched_text(user_a):
    """Search words reach PostgreSQL only as bound parameters, never as SQL text."""
    from arkray.core.ranking import _window

    marker = "zq'); DROP TABLE leads_lead; --"
    query = SearchQuery.parse(f"needle {marker}")
    scoped = AccessScope.own(user_a.pk).apply(Lead.objects.all())
    window = _window(
        scoped,
        text="search_text",
        condition=lead_selectors.match_condition(query.terms),
        newest=("-created_at", "-id"),
        terms=query.terms,
    )
    sql, params = window.sql, window.params
    for term in query.terms:
        assert term not in sql
        assert term.upper() not in sql
    assert set(query.terms) <= set(params)  # each word, as typed ("zq');", "DROP", ...)
    assert user_a.pk in params  # the scope too
    outer = Lead.objects.filter(pk__in=window).query.sql_with_params()
    assert "needle" not in outer[0]
