"""Phase 7 review: one regression test per confirmed backend finding (docs/search.md).
Each failed on the code before its fix."""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from arkray.activities.models import ActivityType
from arkray.activities.selectors import search as activity_search
from arkray.core import ranking
from arkray.core.access import AccessScope
from arkray.core.ranking import NOTHING_TO_INDEX, SearchQuery, indexable
from tests.factories import LeadFactory, MeetingFactory, NoteFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/workspaces/{}/search"


def _older_pass(user, monkeypatch, word: str) -> str:
    """The plan (EXPLAIN ANALYZE) of the older pass of a notes search for `word`, after a
    recent pass of 2 records with no match: the gate's other conditions hold."""
    monkeypatch.setattr(ranking, "RECENT", 2)
    lead = LeadFactory(owner=user)
    for _ in range(4):
        NoteFactory(lead=lead, description="plain words")
    query = SearchQuery.parse(word)
    with CaptureQueriesContext(connection) as captured:
        activity_search(AccessScope.own(user.pk), query, ActivityType.NOTE, limit=5)
    (sql,) = [q["sql"] for q in captured.captured_queries if "search_older" in q["sql"]]
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (ANALYZE, COSTS OFF) {sql}")
        plan = "\n".join(row[0] for row in cursor.fetchall())
    return plan[plan.index("CTE search_older") : plan.index("CTE search_older") + 2000]


class TestWordsAnIndexCanLookUp:
    """P1 (both backend reviewers): words of 3+ characters that give pg_trgm no trigrams
    ("---", "★★★", "a-b") made the older pass read every record: 1-19 s at 2M activities,
    500s at the statement timeout. Such words now only rank; a query of nothing else is a
    400; and the older pass's gate checks every word the way PostgreSQL indexes it."""

    @pytest.mark.parametrize(
        "q",
        [
            "---",
            "...",
            "★★★",
            "a-b",
            "x-q",
            "%%%",
            "___",
            "😀😀😀",
            "€€€",
            "¹²³",
            "a-b c-d",
            "ab",
            # P2-1 (performance review): two letters and a mark NFC can't merge into them
            # were 3 characters in a row, looked up by pg_trgm as "words ending in on":
            # 0.8-1.9 s. Generic combining marks, enclosing marks, variation selectors and
            # joiners don't count; marks of a letter's own script (Indic vowel signs,
            # viramas) do.
            "on" + chr(0x0308),
            "ng" + chr(0x0331),
            "on" + chr(0x20DD),
            "ab" + chr(0xFE0F),
            "on" + chr(0x200D),
            chr(0x094D) * 3,
        ],
    )
    def test_nothing_indexable_is_a_400(self, user_a, q):
        response = signed_in(user_a).get(SEARCH.format("me"), {"q": q})
        assert response.status_code == 400, q
        assert response.json()["error"]["details"]["q"] == [NOTHING_TO_INDEX]

    @pytest.mark.parametrize(
        ("q", "terms"),
        [
            ("--- quotation", ("quotation",)),
            ("a-b Rahul", ("Rahul",)),
            ("राम", ("राम",)),  # vowel signs (Mc) are word characters
            ("राहुल", ("राहुल",)),
            ("शर्मा", ("शर्मा",)),  # a virama (Mn) inside the word
            ("डेमो", ("डेमो",)),
            ("👩" + chr(0x200D) + "💻 lead", ("lead",)),  # an emoji only ranks
            ("98765-43210", ("98765-43210",)),
            ("O'Brien", ("O'Brien",)),
            ("ab12", ("ab12",)),
        ],
    )
    def test_indexable_words_are_searched_and_the_rest_only_rank(self, q, terms):
        assert SearchQuery.parse(q).terms == terms

    def test_every_character_pg_trgm_indexes_is_alnum_to_postgresql_and_back(self):
        """The SQL gate's premise: `[[:alnum:]]` is exactly pg_trgm's word character."""
        chars = [
            chr(c)
            for c in range(0x20, 0x30000)
            if unicodedata.category(chr(c)) not in ("Cn", "Cs", "Co", "Cc")
        ]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM unnest(%s::text[]) AS c "
                "WHERE (show_trgm(c) <> '{}') <> (c ~ '[[:alnum:]]')",
                [chars],
            )
            assert cursor.fetchone()[0] == 0

    @pytest.mark.parametrize(
        "word",
        [
            # Kawi letters (Unicode 15) are letters to Python, unknown to PostgreSQL's tables.
            chr(0x11F04) * 3,
            # P2-1: a Devanagari nukta after Latin letters counts to Python (an Indic mark),
            # but splits the word for pg_trgm: only the padded "on " would be looked up.
            "on" + chr(0x093C),
        ],
    )
    def test_a_word_postgresql_cant_look_up_well_never_runs_the_older_pass(
        self, user_a, monkeypatch, word
    ):
        """Python accepts the word, PostgreSQL's index would only get padded fragments
        matching almost everything: the gate keeps the older pass from reading every note."""
        assert indexable(word)
        older = _older_pass(user_a, monkeypatch, word)
        scans = [
            line for line in older.splitlines() if re.search(r"Scan .*on activities_activity", line)
        ]
        # PostgreSQL folds the constant gate at plan time (no scan at all), or never runs it.
        folded = "One-Time Filter: false" in older
        assert folded or (scans and all("never executed" in s for s in scans)), older

    @pytest.mark.parametrize("word", ["शर्मा", "श्री", "quotation"])
    def test_words_with_selective_fragments_still_run_the_older_pass(
        self, user_a, monkeypatch, word
    ):
        """Indian scripts' marks split pg_trgm's words into 2-letter fragments; those are
        selective enough (4-15 ms per group measured), so old matches are still found."""
        # Older than the 4 notes _older_pass adds: only the older pass can find it.
        old = NoteFactory(lead=LeadFactory(owner=user_a), description=f"{word} visit")
        older = _older_pass(user_a, monkeypatch, word)
        scans = [
            line for line in older.splitlines() if re.search(r"Scan .*on activities_activity", line)
        ]
        assert scans, older
        assert all("never executed" not in s for s in scans), older
        found = activity_search(
            AccessScope.own(user_a.pk), SearchQuery.parse(word), ActivityType.NOTE, limit=5
        )
        assert old in found.items

    @pytest.mark.parametrize(
        ("word", "gate"),
        [
            ("quotation", True),
            ("ab12", True),
            ("शर्मा", True),
            ("गुप्ता", True),
            ("श्री", True),
            ("on" + chr(0x093C), False),  # only "on", padded
            ("क्क", False),  # one-letter fragments
            ("on-on", False),
            (chr(0x11F04) * 3, False),
        ],
    )
    def test_the_gate_asks_for_a_fragment_the_index_can_narrow_by(self, word, gate):
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s ~ %s", [word, ranking._INDEXABLE_SQL])
            assert cursor.fetchone()[0] is gate

    def test_the_gate_is_in_the_sql_with_the_words_as_parameters(self, user_a):
        from arkray.core.ranking import _window
        from arkray.leads import selectors as lead_selectors
        from arkray.leads.models import Lead

        query = SearchQuery.parse("quotation")
        window = _window(
            AccessScope.own(user_a.pk).apply(Lead.objects.all()),
            text="search_text",
            condition=lead_selectors.match_condition(query.terms),
            newest=("-created_at", "-id"),
            terms=query.terms,
        )
        assert "%s ~ %s" in window.sql
        assert ranking._INDEXABLE_SQL in window.params


class TestInput:
    def test_the_length_limit_holds_after_normalisation(self, user_a):
        """P3: U+FB2C becomes three code points under NFC; 100 of them were accepted."""
        q = chr(0xFB2C) * 40
        client = signed_in(user_a)
        assert client.get(SEARCH.format("me"), {"q": q}).status_code == 400
        assert client.get("/api/v1/workspaces/me/leads", {"q": q}).status_code == 400

    def test_one_letter_words_get_global_searchs_own_message(self, user_a):
        """P3: "a b" was refused with the Leads list's 2-character message."""
        response = signed_in(user_a).get(SEARCH.format("me"), {"q": "a b"})
        assert response.json()["error"]["details"]["q"] == [NOTHING_TO_INDEX]

    def test_a_repeated_word_doesnt_use_up_the_five_words(self, user_a):
        """P3: "rahul" five times then "sharma" searched rahul only (the Leads list too)."""
        assert SearchQuery.parse("rahul Rahul RAHUL rahul rahul sharma").terms == (
            "rahul",
            "sharma",
        )
        meant = LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
        LeadFactory(owner=user_a, first_name="Rahul", last_name="Verma")
        response = signed_in(user_a).get(
            "/api/v1/workspaces/me/leads", {"q": "rahul rahul rahul rahul rahul sharma"}
        )
        assert [row["id"] for row in response.json()["results"]] == [str(meant.pk)]


class TestNotePreviewCluster:
    def test_a_preview_never_starts_or_ends_inside_a_character(self, user_a):
        """P3: a preview started 60 characters before the match could begin with a
        Devanagari vowel sign (U+093F) cut off from its consonant."""
        NoteFactory(lead=LeadFactory(owner=user_a), description="कि" * 40 + " needle " + "कि" * 200)
        body = signed_in(user_a).get(SEARCH.format("me"), {"q": "needle"}).json()
        (row,) = body["notes"]["results"]
        assert not unicodedata.category(row["preview"][0]).startswith("M"), row["preview"][:3]
        assert row["preview"].endswith("कि"), row["preview"][-3:]
        assert row["preview_truncated"]
        assert row["preview_starts_mid_text"]


class TestActivitiesRankByTheirOwnDate:
    """Unconfirmed in review, then reasoned and fixed: the recent pass walked all activities
    in created order to find one type's newest, reading 1/share of the type's rows (every
    activity for a type that is rare). It now reads that type's schedule index: exactly the
    type's newest records, ordered by its own date (due, start, creation)."""

    def test_order_is_the_activitys_date_latest_first_with_undated_tasks_first(self, user_a):
        lead = LeadFactory(owner=user_a)
        now = timezone.now()
        # Created in an order that differs from their dates (newest created: `soon`, `past`).
        undated = TaskFactory(lead=lead, title="Order check", due_at=None)
        later = TaskFactory(lead=lead, title="Order check", due_at=now + timedelta(days=9))
        soon = TaskFactory(lead=lead, title="Order check", due_at=now + timedelta(days=1))
        future = MeetingFactory(
            lead=lead,
            title="Order meeting",
            starts_at=now + timedelta(days=3),
            ends_at=now + timedelta(days=3) + timedelta(hours=1),
        )
        past = MeetingFactory(
            lead=lead,
            title="Order meeting",
            starts_at=now - timedelta(days=3),
            ends_at=now - timedelta(days=3) + timedelta(hours=1),
        )
        body = signed_in(user_a).get(SEARCH.format("me"), {"q": "order"}).json()
        assert [r["id"] for r in body["tasks"]["results"]] == [
            str(undated.pk),
            str(later.pk),
            str(soon.pk),
        ]
        assert [r["id"] for r in body["meetings"]["results"]] == [str(future.pk), str(past.pk)]


class TestRecentPassReadsEachTextOnce:
    """P2-4 (performance review): the recent pass checked `upper(description) LIKE ...` once
    per word, reading and upper-casing each of 5,000 notes (up to 10,000 characters) once per
    word: 1.1-3.2 s with five words. It now checks them all with one LIKE ALL."""

    def test_the_recent_pass_checks_every_word_in_one_like_all(self, user_a):
        NoteFactory(lead=LeadFactory(owner=user_a), description="alpha beta gamma")
        query = SearchQuery.parse("alpha beta gamma")
        with CaptureQueriesContext(connection) as captured:
            found = activity_search(AccessScope.own(user_a.pk), query, ActivityType.NOTE, limit=5)
        assert len(found.items) == 1
        (sql,) = [q["sql"] for q in captured.captured_queries if "search_older" in q["sql"]]
        recent = sql[sql.index("WITH search_recent") : sql.index("search_older")]
        assert recent.count("LIKE ALL (ARRAY[") == 1, recent
        assert recent.count('UPPER("activities_activity"."description")') == 1, recent

    @pytest.mark.parametrize(
        "terms",
        [("100%",), ("a_b",), ("k\\s",), ("straße",), ("ÉCOLE", "été"), ("x%y", "_z_")],
    )
    def test_like_all_matches_exactly_what_the_conditions_match(self, user_a, terms):
        """The recent pass's LIKE ALL finds exactly the notes one `contains` per word finds
        (Django's escaping of LIKE's wildcards, PostgreSQL's upper() on both sides)."""
        from django.db.models import Q, Value
        from django.db.models.functions import Upper

        from arkray.activities.models import SEARCH_TEXT, Activity

        lead = LeadFactory(owner=user_a)
        for body in [
            "100% sure",
            "100 percent",
            "a_b test",
            "axb test",
            "back\\slash",
            "backslash",
            "Straße",
            "STRASSE",
            "école été",
            "ecole ete",
            "x%y _z_",
            "xy z",
        ]:
            NoteFactory(lead=lead, description=body)
        notes = Activity.objects.filter(type=ActivityType.NOTE).annotate(
            search_text=SEARCH_TEXT[ActivityType.NOTE]
        )
        condition = Q()
        for term in terms:
            condition &= Q(search_text__contains=Upper(Value(term)))
        expected = set(notes.filter(condition).values_list("pk", flat=True))
        assert expected, terms
        # Built directly: some of these words only rank in a real query ("a_b").
        query = SearchQuery(text=" ".join(terms), terms=terms)
        found = activity_search(AccessScope.own(user_a.pk), query, ActivityType.NOTE, limit=50)
        assert {note.pk for note in found.items} == expected


class TestOlderPassOnlyWhenTheIndexNarrows:
    """Found re-measuring after P2-1/P2-2: a word followed by punctuation ("the-") is looked
    up by pg_trgm as "the" plus "words ending in he", which nearly every note has, while the
    whole word matches almost nothing: the gate (fewer than WINDOW recent matches) opened and
    the older pass rechecked nearly every note, 1.2 s organisation-wide at 2M activities (as
    "ationthe", 0.46 s: rare as a whole, its trigrams common). The gate now also counts the
    recent records the index would return for the words; when those aren't rare, the words
    are searched among the recent records only."""

    def test_common_fragments_keep_the_older_pass_from_running(self, user_a, monkeypatch):
        lead = LeadFactory(owner=user_a)
        old = NoteFactory(lead=lead, description="the- old")  # only the older pass reaches it
        for _ in range(4):
            NoteFactory(lead=lead, description="the usual")
        monkeypatch.setattr(ranking, "RECENT", 4)
        monkeypatch.setattr(ranking, "WINDOW", 2)
        query = SearchQuery.parse("the-")
        with CaptureQueriesContext(connection) as captured:
            found = activity_search(AccessScope.own(user_a.pk), query, ActivityType.NOTE, limit=5)
        assert found.items == []  # 4 recent notes would be returned by the index: not rare
        (sql,) = [q["sql"] for q in captured.captured_queries if "search_older" in q["sql"]]
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN (ANALYZE, COSTS OFF) {sql}")
            plan = "\n".join(row[0] for row in cursor.fetchall())
        older = plan[plan.index("CTE search_older") : plan.index("CTE search_older") + 2000]
        scans = [s for s in older.splitlines() if re.search(r"Scan .*on activities_activity", s)]
        assert scans, older
        assert all("never executed" in s for s in scans), older
        # A distinctive word still reaches the old note.
        assert activity_search(
            AccessScope.own(user_a.pk), SearchQuery.parse("the- old"), ActivityType.NOTE, limit=5
        ).items == [old]

    def test_the_candidate_count_reads_postgresqls_own_words(self, user_a):
        """What the recent pass looks for: PostgreSQL's word fragments ([[:alnum:]], pg_trgm's
        word characters), 2-letter ones whole, longer ones as trigrams."""
        from arkray.core.ranking import CANDIDATE_PATTERNS, _looked_up

        sql, params = _looked_up("t", ["the-", "on" + chr(0x093C), "शर्मा"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT " + sql[sql.index("ARRAY(") : -2], params)
            patterns = cursor.fetchone()[0]
        assert sorted(patterns) == sorted(["%THE%", "%ON%", "%शर%", "%मा%"])
        assert CANDIDATE_PATTERNS == 8
