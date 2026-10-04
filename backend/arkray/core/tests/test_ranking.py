"""core.ranking's query object. The ranking itself is exercised on real records by
arkray/search/tests (core may not import the CRM modules)."""

import pytest

from arkray.core.ranking import NOTHING_TO_INDEX, SearchQuery, top_matches
from arkray.core.text import TextRejected


class TestSearchQuery:
    def test_the_text_is_cleaned_and_the_words_are_the_needles(self):
        query = SearchQuery.parse("  Rahul  S   Sharma ")
        assert query.text == "Rahul S Sharma"
        assert query.terms == ("Rahul", "Sharma")

    @pytest.mark.parametrize("raw", ["", "   ", "a", "a b"])
    def test_nothing_to_search_is_refused(self, raw):
        with pytest.raises(ValueError):  # noqa: PT011 — the message is the API's, tested there
            SearchQuery.parse(raw)

    @pytest.mark.parametrize("raw", ["ab", "Om", "ab cd", "a bc de"])
    def test_one_word_needs_three_characters(self, raw):
        """The older-records pass looks a word up in a trigram index, which needs 3."""
        with pytest.raises(ValueError, match=NOTHING_TO_INDEX):
            SearchQuery.parse(raw)

    def test_short_words_rank_but_dont_narrow(self):
        """A trigram index can't look up a 2-letter word: it stays in the text (match tiers)
        but isn't one of the words that must match."""
        query = SearchQuery.parse("Om Prakash")
        assert (query.text, query.terms) == ("Om Prakash", ("Prakash",))

    def test_control_characters_are_refused(self):
        with pytest.raises(TextRejected):
            SearchQuery.parse("Rahul" + chr(0x202E))

    def test_it_is_immutable(self):
        query = SearchQuery.parse("Rahul")
        with pytest.raises(AttributeError):
            query.text = "Priya"


@pytest.mark.parametrize("limit", [0, 51])
def test_the_result_limit_is_bounded(limit):
    with pytest.raises(ValueError, match="limit"):
        top_matches(
            None,
            text="x",
            query=SearchQuery.parse("xyz"),
            label="x",
            newest=["-id"],
            limit=limit,
        )
