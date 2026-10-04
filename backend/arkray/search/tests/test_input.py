"""Search input at the API boundary: bounded, strict, never a 500, never echoed in errors,
and never interpreted: quotes, LIKE wildcards, backslashes, SQL and regex syntax are just
characters to find."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in

from .conftest import GROUPS, ids, search, search_url

pytestmark = pytest.mark.django_db

ZWJ, ZWNJ = chr(0x200D), chr(0x200C)


@pytest.fixture
def client(user_a):
    return signed_in(user_a)


def raw_get(client, query_string: bytes):
    """A request whose query string is exactly these bytes (not re-encoded by the client)."""
    return client.get("/api/v1/workspaces/me/search", QUERY_STRING=query_string.decode("latin-1"))


class TestBounds:
    @pytest.mark.parametrize(
        "q",
        [
            "",
            " ",
            "x",
            "a b",  # nothing of 2+ characters to search
            "ab",  # no word of 3+ characters: a trigram index can't look it up
            "ab cd ef",
            "Om",
            "a" * 101,
            "rahul " * 17,  # 102 characters
        ],
    )
    def test_out_of_bounds_queries_are_a_400(self, client, q):
        response = client.get(search_url(q))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"
        assert "q" in response.json()["error"]["details"]

    def test_a_missing_query_is_a_400(self, client):
        assert client.get(search_url()).status_code == 400

    def test_unknown_parameters_are_a_400(self, client):
        for extra in ({"owner": "x"}, {"limit": "500"}, {"type": "note"}, {"cursor": "x"}):
            response = client.get(search_url("rahul", **extra))
            assert response.status_code == 400, extra

    def test_a_repeated_q_takes_one_value_and_stays_bounded(self, client):
        response = client.get("/api/v1/workspaces/me/search?q=ra&q=" + "z" * 200)
        assert response.status_code in (200, 400)

    def test_a_two_letter_word_ranks_but_doesnt_narrow(self, client, user_a):
        newer = LeadFactory(owner=user_a, first_name="Ravi", last_name="Prakash")
        meant = LeadFactory(
            owner=user_a,
            first_name="Om",
            last_name="Prakash",
            created_at=newer.created_at - timedelta(days=1),
        )
        body = search(client, "Om Prakash")
        assert body["terms"] == ["Prakash"]
        assert ids(body, "leads") == [str(meant.pk), str(newer.pk)]  # exact match first
        response = client.get(search_url("Om"))
        assert response.status_code == 400
        assert response.json()["error"]["details"]["q"] == [
            "Search for a word with at least 3 letters or digits in a row."
        ]

    def test_100_characters_and_more_than_five_words_are_fine(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Alpha", last_name="Beta")
        q = "alpha beta gamma delta epsilon zeta"  # the 6th word isn't searched
        assert search(client, q)["terms"] == ["alpha", "beta", "gamma", "delta", "epsilon"]
        assert search(client, "x" * 100)["query"] == "x" * 100
        assert ids(search(client, "a b c d e alpha"), "leads") == [str(lead.pk)]

    def test_errors_never_echo_the_query(self, client):
        marker = "SECRETQUERYMARKER"
        for q in (marker + chr(0x202E), marker * 10, marker + chr(0)):
            response = client.get(search_url(q))
            assert response.status_code == 400
            assert marker.lower() not in response.content.decode().lower()


class TestUnicode:
    @pytest.mark.parametrize(
        "char",
        [
            chr(0),  # NUL
            chr(7),
            chr(0x1B),
            chr(0x7F),
            chr(0x85),
            chr(0x200B),  # zero-width space
            chr(0x200E),  # LRM
            chr(0x202E),  # RLO (Trojan Source)
            chr(0x2066),  # LRI
            chr(0x2028),
            chr(0xFEFF),
            chr(0xE0041),  # tag character
            chr(0xE000),  # private use
            chr(0xFFFE),
        ],
    )
    def test_invisible_and_control_characters_are_a_400_not_a_500(self, client, char):
        response = client.get(search_url(f"Rahul{char}Sharma"))
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "raw",
        [
            b"q=Rahul%ED%A0%80",  # an encoded lone surrogate (invalid UTF-8)
            b"q=%ED%A0%80%ED%B0%80",  # an encoded surrogate pair
            b"q=%FF%FE%FD",  # not UTF-8 at all
            b"q=%C0%AE%C0%AE",  # overlong encoding
            b"q=Rahul%00",  # NUL
            b"q=%E2%80%AE%E2%80%AEab",  # RLO
            b"q=%",  # broken percent-encoding
            b"q=%zz%zz",
        ],
    )
    def test_malformed_encodings_never_cause_a_500(self, client, raw):
        assert raw_get(client, raw).status_code in (200, 400)

    def test_a_lone_surrogate_is_refused_wherever_it_could_appear(self, client):
        """A URL can't carry one (Django decodes query strings with "replace": the encoded
        forms above arrive as U+FFFD), so the serializer is checked with the str itself."""
        from arkray.search.api.serializers import SearchQuerySerializer

        serializer = SearchQuerySerializer(data={"q": "Rahul" + chr(0xD800)})
        assert not serializer.is_valid()
        assert "q" in serializer.errors
        assert raw_get(client, b"q=Rahul%ED%A0%80").status_code == 200  # U+FFFD: a symbol

    @pytest.mark.parametrize(
        ("stored", "query"),
        [
            ("राहुल शर्मा", "राहुल"),  # Devanagari
            ("राहुल शर्मा", "शर्मा"),
            ("क्" + ZWNJ + "षमा", "क्" + ZWNJ + "ष"),  # ZWNJ is part of the word
            ("ശ്രീ" + ZWJ + "ജ", "ശ്രീ" + ZWJ),  # Malayalam needs ZWJ
            ("Zoë", "Zoe" + chr(0x308)),  # combining diaeresis typed, precomposed stored
            ("José", "JOSÉ"),
            ("李小龍先生", "李小龍"),
            ("محمد علي", "محمد"),
            ("Team 👩" + ZWJ + "💻 lead", "👩" + ZWJ + "💻 lead"),  # an emoji only ranks
        ],
    )
    def test_text_in_any_script_is_found(self, client, user_a, stored, query):
        lead = LeadFactory(owner=user_a, first_name=stored, last_name="")
        assert ids(search(client, query), "leads") == [str(lead.pk)]

    def test_notes_in_any_script_are_found(self, client, user_a):
        note = NoteFactory(lead=LeadFactory(owner=user_a), description="अगले हफ्ते डेमो चाहिए।")
        assert ids(search(client, "डेमो"), "notes") == [str(note.pk)]


class TestNothingIsInterpreted:
    PAYLOADS = [
        "'",
        "''",
        '"',
        "%",
        "%%",
        "_",
        "__",
        "\\",
        "\\\\",
        "\\%",
        "' OR '1'='1",
        "'; DROP TABLE leads_lead; --",
        "1; SELECT pg_sleep(5)",
        "$$ select $$",
        "E'\\x41'",
        "::text",
        "~* .*",
        "a|b",
        "(?i)rahul",
        "[a-z]+",
        "^.*$",
        "<->",
        "@@ to_tsquery",
        "%' AND 1=1 --",
        "/* comment */",
    ]

    @pytest.mark.parametrize("q", PAYLOADS, ids=range(len(PAYLOADS)))
    def test_hostile_strings_are_a_200_or_400_and_match_nothing(self, client, user_a, q):
        LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
        response = client.get(search_url(q))
        assert response.status_code in (200, 400)
        if response.status_code == 200:
            body = response.json()
            assert all(body[group]["results"] == [] for group in GROUPS), q

    def test_wildcards_match_only_themselves(self, client, user_a):
        literal = LeadFactory(owner=user_a, first_name="Offer", last_name="50%_off")
        LeadFactory(owner=user_a, first_name="Offer", last_name="500 off")
        LeadFactory(owner=user_a, first_name="Offer", last_name="50xxoff")
        assert ids(search(client, "50%_off"), "leads") == [str(literal.pk)]
        assert ids(search(client, "0%_off"), "leads") == [str(literal.pk)]
        assert ids(search(client, "0__off"), "leads") == []  # "_" is not "any character"
        assert ids(search(client, "0%%off"), "leads") == []  # "%" is not "anything"
        # "50%" has only two digits in a row: it ranks (the literal match first), not narrows.
        assert ids(search(client, "offer 50%"), "leads")[0] == str(literal.pk)

    def test_quotes_and_backslashes_are_found_literally(self, client, user_a):
        quoted = LeadFactory(owner=user_a, first_name="O'Brien", last_name="D\\Souza")
        TaskFactory(lead=quoted, title="It's 'quoted' \\ here")
        OpportunityFactory(lead=quoted, title="50% off \\ deal")
        assert ids(search(client, "o'brien"), "leads") == [str(quoted.pk)]
        assert ids(search(client, "d\\souza"), "leads") == [str(quoted.pk)]
        assert ids(search(client, "'quoted'"), "tasks") != []
        assert ids(search(client, "\\ deal"), "opportunities") != []

    def test_the_tables_survive(self, client, user_a):
        lead = LeadFactory(owner=user_a, first_name="Survivor")
        client.get(search_url("'; DROP TABLE leads_lead; --"))
        assert ids(search(client, "survivor"), "leads") == [str(lead.pk)]
