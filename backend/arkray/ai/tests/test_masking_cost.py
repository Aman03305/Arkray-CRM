"""What masking costs a routed question (review round 1, AI-1).

A routed question ("What are my open tasks?") is answered inside the web request, whatever
the language-model provider: its tool masks the whole text of every row it lists
(tools.full_texts, adversarial review M5). A sales user's own task descriptions, 10,000
characters each of a phone word and blanks, cost 3-7 s of masking per task before the
patterns were made linear (17.8 s for five tasks; ten passed gunicorn's 30 s timeout).
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from arkray.ai import formatting, tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from tests.factories import LeadFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

CRAFTED = {
    "a phone word, then spaces": "Please call" + " " * 9_980 + "x",
    "a PIN word, then tabs": "PIN" + "\t" * 9_990 + "x",
    "an Aadhaar word, then no-break spaces": "aadhaar no" + chr(0x00A0) * 9_980 + "x",
    # Review round 2 (AI2-4): 18 characters each under NFKC.
    "an expanding ligature": chr(0xFDFA) * 9_990,
}


@pytest.fixture(autouse=True)
def publish():
    with mock.patch("arkray.ai.tasks.answer_question.apply_async") as publish:
        yield publish


@pytest.fixture
def masking_time(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Seconds spent searching each text for what to mask (the memo's misses)."""
    spent: dict[str, float] = {}
    real = formatting._search

    def timed(text: str) -> Any:
        started = time.perf_counter()
        try:
            return real(text)
        finally:
            spent[text] = spent.get(text, 0.0) + time.perf_counter() - started

    monkeypatch.setattr(formatting, "_search", timed)
    return spent


def recorded_searches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text searched for what to mask, in order."""
    searched: list[str] = []
    real = formatting._search

    def search(text: str) -> Any:
        searched.append(text)
        return real(text)

    monkeypatch.setattr(formatting, "_search", search)
    return searched


def test_within_a_memo_each_text_is_searched_once(monkeypatch: pytest.MonkeyPatch):
    searched = recorded_searches(monkeypatch)
    text = "Ring 98111 55555 about the analyser"
    with formatting.masking_memo():
        assert formatting.user_text(text) == "Ring [phone] about the analyser"
        assert formatting.user_text_slice(text, 0, 7) == "Ring [phone]"
        assert formatting.clip(text, 240, start=3)["text"] == "g [phone] about the analyser"
    assert searched == [text]
    formatting.user_text(text)  # outside a memo: searched again, nothing kept
    assert searched == [text, text]


def test_a_tool_call_searches_a_text_once_however_many_rows_quote_it(user_a, monkeypatch):
    searched = recorded_searches(monkeypatch)
    lead = LeadFactory(owner=user_a)
    text = CRAFTED["a phone word, then spaces"]
    for number in range(3):
        TaskFactory(lead=lead, title=f"Task {number}", description=text)
    ctx = ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, "list_tasks", {"filter": "open"})
    assert not outcome.is_error
    assert searched.count(text) == 1


@pytest.mark.parametrize("description", CRAFTED.values(), ids=CRAFTED.keys())
def test_a_routed_question_over_ten_crafted_tasks_masks_in_well_under_a_second(
    user_a, masking_time, description
):
    lead = LeadFactory(owner=user_a)
    # Ten different texts (review round 2: ten copies of one text were one search, the memo).
    descriptions = [f"{number} {description}"[:10_000] for number in range(10)]
    for number, text in enumerate(descriptions):
        TaskFactory(lead=lead, title=f"Task {number}", description=text)
    started = time.perf_counter()
    response = signed_in(user_a).post(
        "/api/v1/workspaces/me/ask", {"question": "What are my open tasks?"}, format="json"
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"]["provenance"]["mode"] == "router"
    assert set(descriptions) <= set(masking_time)  # every task's whole text, each once
    spent = sum(masking_time.values())
    assert spent < 1.0, (spent, len(masking_time))
    assert elapsed < 15, elapsed  # the whole request (the database included), generously
