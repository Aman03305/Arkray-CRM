"""Retrieval quality with the real embedding model (BAAI/bge-small-en-v1.5), on Arkray-like
notes: the questions the brief names must find the right note first, unrelated questions
must find nothing above the similarity threshold, and isolation holds with real vectors.

Runs when the verified model files are present (`manage.py ai_fetch_model`; the CI job
fetches them), skipped otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings

from arkray.ai import retrieval
from arkray.ai.embeddings import _embedders
from arkray.ai.model_files import verify
from tests.ai_fixtures import index, note, own
from tests.factories import LeadFactory

MODEL_DIR = Path(settings.AI_EMBEDDING_MODEL_DIR)
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(bool(verify(MODEL_DIR)), reason="embedding model files not present"),
]

NOTES = {
    "price": (
        "Dr Mehta said the HbA1c analyser is too expensive compared with the competitor's quote."
    ),
    "install": "Installation of the glucose meters at the Pune clinic is booked for next Tuesday.",
    "demo": "Asked for a live demonstration of the lab automation line and the brochure.",
    "amc": "They want the annual maintenance contract renewed with a ten percent discount.",
    "competitor": "Roche visited them last week and offered free reagents for a year.",
    "service": "Complained that our engineer took four days to respond to the breakdown.",
    "decision": "The purchase committee meets on the 15th; the CFO has the final say.",
    "training": "Nurses need training on the new point-of-care devices before go-live.",
}
# (question, the notes that answer it). "Is a competitor involved?" has two right answers:
# the price note mentions a competitor's quote; the Roche note names one without saying
# "competitor" (bge-small ranks it 4th: recorded in docs/rag-architecture.md#retrieval-quality).
QUESTIONS = [
    ("What concerns were raised about cost?", {"price"}),
    ("When is the installation scheduled?", {"install"}),
    ("Did anyone ask for a demo?", {"demo"}),
    ("What did they say about the maintenance contract?", {"amc"}),
    ("Is a competitor involved?", {"price", "competitor"}),
    ("Any complaints about our service?", {"service"}),
    ("Who makes the purchasing decision?", {"decision"}),
    ("What training do they need?", {"training"}),
]
OFF_TOPIC = [
    "What is the capital of France?",
    "recipe for chocolate cake",
    "How do I reset my password?",
]


@pytest.fixture(autouse=True)
def _real_model(settings):
    settings.AI_EMBEDDING_PROVIDER = "local"
    settings.AI_RETRIEVAL_MIN_SIMILARITY = 0.55  # the calibrated default
    _embedders.clear()
    yield
    _embedders.clear()


@pytest.fixture
def corpus(user_a):
    lead = LeadFactory(owner=user_a)
    created = {key: note(user_a, lead, text) for key, text in NOTES.items()}
    index()
    return created


def test_each_question_finds_a_right_note_first(user_a, corpus):
    for question, expected in QUESTIONS:
        found = retrieval.retrieve(own(user_a), question)
        assert found.passages, question
        right = {corpus[key].pk for key in expected}
        assert found.passages[0].source_id in right, question  # recall@1: 8/8


@pytest.mark.parametrize("question", OFF_TOPIC)
def test_a_clearly_off_topic_question_finds_nothing(user_a, corpus, question):
    assert retrieval.retrieve(own(user_a), question).passages == []


def test_isolation_holds_with_real_vectors(user_a, user_b, corpus):
    note(user_b, LeadFactory(owner=user_b), NOTES["price"])  # identical text, other user
    index()
    found = retrieval.retrieve(own(user_a), "What concerns were raised about cost?")
    assert {p.owner_id for p in found.passages} == {user_a.pk}
