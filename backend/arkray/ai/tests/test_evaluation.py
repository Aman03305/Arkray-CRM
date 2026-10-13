"""The real-model evaluation suite's self-test (R69, docs/ai-evaluation.md#running-it).

No network and no key: the whole suite runs end to end through the real service path with
a scripted model. Well-behaved scripts must pass every grader (the graders accept correct
behaviour); adversarial scripts (a leaked canary, a foreign or wrong citation, an invented
figure, a forbidden tool, an obeyed injection) must fail exactly the graders that claim to
catch them; a masking regression must fail the minimisation grader; the spend cap must stop
a run before a call could cross it; and no run without the real model may report PASS.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest
from django.core.management import call_command

from arkray.ai import answers, formatting, llm, retrieval, service
from arkray.ai.evaluation import cases, database, dataset, runner
from arkray.ai.evaluation.pricing import PriceTable
from arkray.ai.evaluation.recording import DryRunProvider, Limits, RecordingProvider
from arkray.ai.llm import Completion, ScriptedProvider
from arkray.identity.models import User
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may build a real client, let alone send a request."""

    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("the self-test must never construct AnthropicProvider")

    monkeypatch.setattr(llm.AnthropicProvider, "__init__", refuse)


@pytest.fixture
def world() -> dataset.World:
    return dataset.seed()


def evaluate(
    world: dataset.World,
    tmp_path: Any,
    recorder: RecordingProvider | None = None,
    **options: Any,
) -> dict[str, Any]:
    chosen = runner.Options(report=tmp_path / "report.json", **options)
    recorder = recorder or RecordingProvider(ScriptedProvider(steps=[]), prices=PriceTable())
    with runner.evaluation_settings():
        return runner.evaluate(world, chosen, recorder)


def failures(result: dict[str, Any]) -> dict[str, list[str]]:
    return {c["id"]: c["failed_graders"] for c in result["cases"] if c["failed_graders"]}


def case(result: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(c for c in result["cases"] if c["id"] == case_id)


# --- the dataset and its marker ----------------------------------------------------------------
def test_the_seeded_world_is_synthetic_and_matches_its_marker(world):
    marker = database.read_marker()
    assert marker is not None
    assert marker["dataset"] == dataset.DATASET_VERSION
    assert marker["counts"] == world.counts() == database.record_counts()
    assert all(
        u.email.endswith("@" + database.SYNTHETIC_EMAIL_DOMAIN) for u in world.users.values()
    )
    with mock.patch.object(database, "database_name", return_value="arkray_ai_eval"):
        database.assert_seeded(dataset.DATASET_VERSION)  # nothing changed since seeding
        UserFactory(email="someone@hospital.example")  # a person the seeder didn't create
        with pytest.raises(database.DatabaseRefused, match="changed since seeding"):
            database.assert_seeded(dataset.DATASET_VERSION)


def test_the_guard_reads_a_seeded_database_as_the_evaluations_own(world):
    name = database.database_name()
    found = database.inspect(name)
    assert found.exists
    assert found.marker is not None
    assert found.foreign_users == 0
    assert found.counts == found.marker["counts"]
    assert database.decide("arkray_ai_eval", found) == "recreate"
    UserFactory(email="real.person@hospital.example")
    tampered = database.inspect(name)
    assert tampered.foreign_users == 1
    with pytest.raises(database.DatabaseRefused, match="synthetic"):
        database.decide("arkray_ai_eval", tampered)


# --- the self-test -----------------------------------------------------------------------------
def test_a_well_behaved_model_passes_every_grader_and_is_still_not_verified(world, tmp_path):
    result = evaluate(world, tmp_path)
    assert failures(result) == {}
    assert result["totals"]["cases"] == len(cases.CASES)
    assert {c["category"] for c in result["cases"]} == set(cases.CATEGORIES)
    # Never PASS without the real model, however good the run.
    assert result["status"] == runner.NOT_VERIFIED
    assert runner.exit_code(result) == 0
    # The model path really ran: every case expecting the model got it, and the router's
    # question reached no model at all.
    for entry in result["cases"]:
        if cases.BY_ID[entry["id"]].expect.mode == "llm":
            assert {t["mode"] for t in entry["turns"]} == {"llm"}, entry["id"]
    assert case(result, "minimisation.routed_sends_nothing")["calls"] == []
    # A projection of the real run's cost is part of every self-test report.
    assert result["estimate"]["calls"] == result["totals"]["model_calls"] > 30
    assert 0 < result["estimate"]["estimated_cost_usd"] < result["estimate"]["worst_case_usd"]
    json_path, summary = runner.report.write(result, tmp_path / "out" / "report.json")
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "NOT VERIFIED"
    text = summary.read_text(encoding="utf-8")
    assert "**Status: NOT VERIFIED**" in text
    assert "PASS**" not in text


def test_an_adversarial_model_fails_exactly_the_graders_aimed_at_it(world, tmp_path):
    result = evaluate(world, tmp_path, adversarial=True)
    detection = result["adversarial_detection"]
    assert len(detection) >= 12
    for case_id, found in detection.items():
        # Each planted failure is caught, and by nothing else: no grader fires at random.
        assert set(found["failed"]) == set(found["expected_to_fail"]), case_id
    assert result["adversarial_all_detected"] is True
    planted = {c.id for c in cases.CASES if c.bad is not None}
    assert set(failures(result)) == planted  # every other case still passes
    # Every kind of misbehaviour the brief names is among them.
    caught = {name for found in detection.values() for name in found["failed"]}
    assert {
        "canaries",
        "tool_safety",
        "numeric_grounding",
        "injection_marker",
        "invented_references",
        "citations_relevant",
    } <= caught
    assert result["status"] == runner.NOT_VERIFIED
    assert runner.exit_code(result) == 1  # mandatory failures were planted


def test_a_masking_regression_fails_the_minimisation_grader(world, tmp_path, monkeypatch):
    """If links, e-mail addresses, phone numbers or PANs ever went out unmasked, the
    payload grader would catch it."""
    monkeypatch.setattr(formatting, "user_text", lambda text: text)
    monkeypatch.setattr(service, "user_text", lambda text: text)
    monkeypatch.setattr(answers, "user_text", lambda text: text)
    monkeypatch.setattr(retrieval, "user_text_slice", lambda text, start, end: text[start:end])
    # A search hit's preview is masked on the whole note, then cut (adversarial review M5).
    monkeypatch.setattr(formatting, "user_text_slice", lambda text, start, end: text[start:end])
    result = evaluate(world, tmp_path, only=("minimisation.",))
    caught = {c["id"] for c in result["cases"] if "payload_minimised" in c["mandatory_failed"]}
    assert caught == {
        "minimisation.contact_details",  # the contact note's preview, with the lead
        "minimisation.masked_description",
        "minimisation.masked_contact_note",
        "minimisation.cut_preview",
        "minimisation.question_is_masked",
    }
    detail = " ".join(
        g["detail"]
        for c in result["cases"]
        for g in c["graders"]
        if g["name"] == "payload_minimised"
    )
    assert "ABCDE1234F" in detail
    assert dataset.CONTACT_EMAIL in detail


def test_a_dry_run_sends_nothing_and_shows_the_first_requests(world, tmp_path):
    recorder = RecordingProvider(DryRunProvider("claude-opus-5-5"), prices=PriceTable())
    result = evaluate(
        world,
        tmp_path,
        recorder,
        provider="anthropic",
        dry_run=True,
        only=("correctness.apollo", "minimisation.routed", "authorization.empty"),
    )
    assert result["mode"] == "anthropic-dry-run"
    assert result["status"] == runner.NOT_VERIFIED
    calls = [c for entry in result["cases"] for c in entry["calls"]]
    assert calls
    assert {c["outcome"] for c in calls} == {"not_sent"}
    assert result["totals"]["model_calls"] == 0
    first = case(result, "correctness.apollo_value_and_price")["calls"]
    assert len(first) == 1
    assert "request" in first[0]  # the payload, for review
    printed = runner.report.dry_run_text(result)
    assert "nothing was sent" in printed
    assert "What is the Apollo Hospitals opportunity worth" in printed
    assert "minimisation.routed_sends_nothing: nothing would be sent" in printed
    assert runner.exit_code(result) == 0


class Expensive:
    """Answers like a real provider would bill: input tokens as large as the request."""

    model_name = "claude-opus-5-5"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **request: Any) -> Completion:
        self.calls += 1
        size = len(json.dumps(request["messages"], default=str))
        return Completion(
            [{"type": "text", "text": "Noted."}],
            "end_turn",
            "Noted.",
            [],
            "claude-opus-5-5",
            input_tokens=size,
            output_tokens=8000,
        )


def test_the_spend_cap_stops_the_run_before_any_call_could_cross_it(world, tmp_path):
    inner = Expensive()
    recorder = RecordingProvider(
        inner, prices=PriceTable(), limits=Limits(max_calls=100, max_cost_usd=1.0), sends=True
    )
    with runner.evaluation_settings(), llm.override_provider(recorder):
        graded = runner.run_suite(world, list(cases.CASES), recorder, scripted=False)
    assert recorder.aborted.startswith("max_cost_usd")
    assert recorder.spent_usd <= 1.0
    assert inner.calls == recorder.calls_sent >= 1
    stopped = next(i for i, c in enumerate(recorder.calls) if c.outcome == "blocked")
    # The call that could have crossed the cap was never made, nor any after it.
    assert recorder.calls[stopped].error.startswith("max_cost_usd")
    assert {c.outcome for c in recorder.calls[stopped:]} == {"blocked"}
    assert len(graded) < len(cases.CASES)  # and the run stopped there


def test_the_call_cap_stops_the_run(world, tmp_path):
    inner = Expensive()
    recorder = RecordingProvider(
        inner, prices=PriceTable(), limits=Limits(max_calls=2, max_cost_usd=100.0), sends=True
    )
    with runner.evaluation_settings(), llm.override_provider(recorder):
        runner.run_suite(world, list(cases.CASES), recorder, scripted=False)
    assert inner.calls == 2
    assert recorder.aborted == "max_model_calls (2) reached"


# --- the command and its interlocks --------------------------------------------------------------
def run_command(tmp_path: Any, *arguments: str) -> tuple[int, dict[str, Any]]:
    path = tmp_path / "command" / "report.json"
    try:
        call_command("ai_eval", "--report", str(path), *arguments)
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, json.loads(path.read_text(encoding="utf-8"))


def test_the_real_run_without_its_interlocks_is_not_verified_and_touches_nothing(
    tmp_path, settings, monkeypatch
):
    monkeypatch.delenv(runner.ACK_VARIABLE, raising=False)
    with mock.patch.object(database, "prepare") as prepare:
        code, result = run_command(tmp_path, "--provider", "anthropic")
    assert code == 2
    assert result["status"] == "NOT VERIFIED"
    assert result["status_reasons"] == [
        "--allow-external-provider was not given",
        f"{runner.ACK_VARIABLE}={runner.ACK_VALUE} is not set",
        "AI_LLM_PROVIDER is not 'anthropic'",
        "ANTHROPIC_API_KEY is not set",
    ]
    prepare.assert_not_called()  # no database work, nothing seeded, nothing sent
    assert not User.objects.exists()


def test_every_interlock_is_needed(tmp_path, settings, monkeypatch):
    settings.AI_LLM_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    monkeypatch.setenv(runner.ACK_VARIABLE, runner.ACK_VALUE)
    everything = runner.Options(report=tmp_path / "r.json", allow_external_provider=True)
    assert runner.missing_interlocks(everything) == []
    assert runner.missing_interlocks(
        runner.Options(report=tmp_path / "r.json", allow_external_provider=False)
    ) == ["--allow-external-provider was not given"]
    monkeypatch.setenv(runner.ACK_VARIABLE, "yes")
    assert runner.missing_interlocks(everything) == [
        f"{runner.ACK_VARIABLE}={runner.ACK_VALUE} is not set"
    ]
    monkeypatch.setenv(runner.ACK_VARIABLE, runner.ACK_VALUE)
    settings.AI_LLM_BASE_URL = "http://gateway.internal"
    assert runner.missing_interlocks(everything) == ["AI_LLM_BASE_URL is not https://"]
    settings.AI_LLM_BASE_URL = "https://api.anthropic.com"
    capped = runner.Options(
        report=tmp_path / "r.json", allow_external_provider=True, max_cost_usd=0
    )
    assert runner.missing_interlocks(capped) == ["--max-cost-usd must be a positive amount"]


def test_with_every_interlock_the_database_guard_still_decides(tmp_path, settings, monkeypatch):
    settings.AI_LLM_PROVIDER = "anthropic"
    settings.ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"
    monkeypatch.setenv(runner.ACK_VARIABLE, runner.ACK_VALUE)
    with (
        mock.patch.object(llm, "get_provider") as provider,
        mock.patch.object(database, "database_name", return_value="arkray"),
    ):
        code, result = run_command(tmp_path, "--provider", "anthropic", "--allow-external-provider")
    assert code == 2
    assert result["status"] == "NOT VERIFIED"
    assert "ends in '_ai_eval'" in result["status_reasons"][0]
    provider.assert_not_called()
    assert not User.objects.exists()


def test_the_command_runs_the_self_test_and_writes_both_reports(tmp_path):
    with (
        mock.patch.object(database, "prepare"),
        mock.patch.object(database, "database_name", return_value="arkray_ai_eval"),
    ):
        code, result = run_command(tmp_path, "--case", "correctness.", "--case", "injection.")
    assert code == 0
    assert result["status"] == "NOT VERIFIED"
    assert result["totals"]["cases"] == 10
    assert (tmp_path / "command" / "report.md").exists()
