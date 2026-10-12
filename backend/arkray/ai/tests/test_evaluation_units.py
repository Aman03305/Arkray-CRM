"""The evaluation suite's parts on their own (no database): the database guard's
decisions, prices and costs, the verdict, the budget and cost-control graders on runs made
up to break them, and the usage the real provider reports (R69, docs/ai-evaluation.md)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from django.conf import settings

from arkray.ai import llm
from arkray.ai.evaluation import cases, graders, runner
from arkray.ai.evaluation.database import DatabaseRefused, Inspection, check_name, decide
from arkray.ai.evaluation.pricing import PRICES, Price, PriceTable, Usage
from arkray.ai.evaluation.recording import ModelCall
from arkray.core.access import AccessScope

COUNTS = {"users": 5, "lead": 6, "opportunity": 6, "note": 8, "task": 3, "meeting": 1}
MARKER = {"dataset": "arkray-ai-eval", "counts": COUNTS, "seeded_at": "2026-10-12T00:00:00"}


# --- the database guard --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name", ["arkray", "arkray_prod", "arkray_aieval", "_ai_eval", "test_arkray", "ai_eval"]
)
def test_only_an_evaluation_database_name_is_accepted(name):
    with pytest.raises(DatabaseRefused, match="_ai_eval"):
        check_name(name)


def test_the_guard_creates_uses_or_recreates_only_what_is_provably_its_own():
    name = "arkray_ai_eval"
    assert decide(name, Inspection(exists=False)) == "create"
    assert decide(name, Inspection(exists=True)) == "use"  # migrated or not, but empty
    assert decide(name, Inspection(exists=True, marker=MARKER, counts=dict(COUNTS))) == "recreate"
    refused = [
        Inspection(exists=True, data_rows=1),  # someone's data, no marker
        Inspection(exists=True, marker=MARKER, counts=dict(COUNTS), foreign_users=1),
        Inspection(exists=True, marker=MARKER, counts={**COUNTS, "lead": 7}),  # changed
    ]
    for found in refused:
        with pytest.raises(DatabaseRefused, match="left as it is"):
            decide(name, found)
    with pytest.raises(DatabaseRefused):
        decide("arkray", Inspection(exists=False))  # the name is checked first


# --- prices --------------------------------------------------------------------------------------
def test_a_call_is_priced_from_all_four_usage_counts():
    table = PriceTable()
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    # Claude Opus 5.5 (the default AI_CHAT_MODEL): $4 + $20 + $0.20 + $5.
    assert table.cost("claude-opus-5-5", usage).usd == pytest.approx(29.20)
    assert table.cost("claude-opus-5-5", usage).assumed is False
    unknown = table.cost("some-fallback-model", usage)
    assert unknown.assumed is True
    ceiling = table.ceiling()
    assert unknown.usd == pytest.approx(
        ceiling.input + ceiling.output + ceiling.cache_read + ceiling.cache_write
    )


def test_the_default_model_is_in_the_price_table():
    assert settings.AI_CHAT_MODEL in PRICES


def test_a_pricing_file_overrides_and_is_validated(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(
        json.dumps(
            {"claude-opus-5-5": {"input": 1, "output": 2, "cache_write": 1.25, "cache_read": 0.1}}
        ),
        encoding="utf-8",
    )
    table = PriceTable.from_file(path)
    assert table.prices["claude-opus-5-5"] == Price(1.0, 2.0, 1.25, 0.1)
    assert table.prices["claude-sonnet-5-5"] == PRICES["claude-sonnet-5-5"]
    path.write_text(json.dumps({"m": {"input": -1, "output": 2}}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-negative"):
        PriceTable.from_file(path)


def test_the_real_provider_reports_cache_writes_too():
    """The usage a run is priced from includes the prompt-cache writes (1.25x input)."""
    import anthropic

    provider = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
    provider._anthropic = anthropic
    provider.model_name = "claude-opus-5-5"
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Done.")],
        stop_reason="end_turn",
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=45,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=3400,
        ),
        _request_id="req_test",
    )
    provider._client = mock.Mock()
    provider._client.beta.messages.create = lambda **_: response
    completion = provider._complete_once(
        system="", tools=[], messages=[], timeout=5, allow_tools=False
    )
    assert (
        completion.input_tokens,
        completion.output_tokens,
        completion.cache_read_tokens,
        completion.cache_write_tokens,
    ) == (120, 45, 3000, 3400)


# --- the verdict ---------------------------------------------------------------------------------
def graded(passed: bool = True, mandatory: bool = False) -> runner.Graded:
    case = cases.BY_ID["correctness.agreed_cpt"]
    name = "canaries" if mandatory else "figures"
    run = graders.CaseRun(case, AccessScope.organization(uuid.uuid4()), [], [], [], {})
    return runner.Graded(run, [graders.Result(name, passed)])


GOOD_METRICS = {
    "question_latency_p50_s": 4.0,
    "question_latency_p95_s": 12.0,
    "cost_per_model_question_usd": 0.04,
}
CATEGORY = {"correctness": {"rate": 1.0, "threshold": 0.8, "passed": 1, "cases": 1}}


def test_only_the_real_model_can_pass():
    thresholds = runner.Thresholds()
    good = [graded()]
    assert runner.verdict(
        "anthropic", good, GOOD_METRICS, CATEGORY, thresholds, aborted="", expected_cases=1
    ) == ("PASS", [])
    for mode in ("scripted", "scripted-adversarial", "anthropic-dry-run"):
        status, _ = runner.verdict(
            mode, good, GOOD_METRICS, CATEGORY, thresholds, aborted="", expected_cases=1
        )
        assert status == "NOT VERIFIED"


def test_a_failure_a_slow_answer_or_a_costly_one_fails_the_real_run():
    thresholds = runner.Thresholds()

    def status(items: list[runner.Graded], measured: dict[str, Any], aborted: str = "") -> str:
        return runner.verdict(
            "anthropic", items, measured, CATEGORY, thresholds, aborted=aborted, expected_cases=1
        )[0]

    assert status([graded(passed=False, mandatory=True)], GOOD_METRICS) == "FAIL"
    assert status([graded()], {**GOOD_METRICS, "question_latency_p95_s": 25.0}) == "FAIL"
    assert status([graded()], {**GOOD_METRICS, "cost_per_model_question_usd": 0.5}) == "FAIL"
    # Stopped at a cap without a failure so far: not a pass, not a fail.
    assert status([graded()], GOOD_METRICS, aborted="max_cost_usd") == "NOT VERIFIED"
    assert status([graded(passed=False, mandatory=True)], GOOD_METRICS, aborted="x") == "FAIL"
    low = {"correctness": {**CATEGORY["correctness"], "rate": 0.5}}

    def with_categories(aborted: str) -> str:
        return runner.verdict(
            "anthropic",
            [graded()],
            GOOD_METRICS,
            low,
            thresholds,
            aborted=aborted,
            expected_cases=1,
        )[0]

    assert with_categories("") == "FAIL"
    # The case a cap cut short fails its quality graders through no fault of the model.
    assert with_categories("max_cost_usd") == "NOT VERIFIED"


def test_exit_codes():
    def code(status: str, mode: str, mandatory: int = 0, failed: int = 0) -> int:
        totals = {"mandatory_failures": mandatory, "failed": failed}
        return runner.exit_code({"status": status, "mode": mode, "totals": totals})

    assert code("PASS", "anthropic") == 0
    assert code("FAIL", "anthropic", mandatory=1) == 1
    assert code("NOT VERIFIED", "anthropic") == 2  # interlocks, the guard, or a cap
    assert code("NOT VERIFIED", "scripted") == 0
    assert code("NOT VERIFIED", "scripted", failed=1) == 1
    assert code("NOT VERIFIED", "scripted-adversarial", mandatory=3, failed=3) == 1
    assert code("NOT VERIFIED", "anthropic-dry-run", failed=4) == 0


def test_percentiles_are_nearest_rank():
    values = [float(v) for v in range(1, 21)]
    assert runner.percentile(values, 0.5) == 10.0
    assert runner.percentile(values, 0.95) == 19.0
    assert runner.percentile([], 0.95) == 0.0


# --- graders on runs made up to break them --------------------------------------------------------
LIMITS = {
    "AI_MAX_TOOL_ROUNDS": 2,
    "AI_MAX_TOOL_CALLS_PER_ROUND": 2,
    "AI_TOOL_RESULTS_MAX_CHARS": 100,
    "AI_CONTEXT_MAX_CHARS": 50,
    "AI_CHAT_MAX_TOKENS": 500,
    "AI_LLM_TIMEOUT_S": 20.0,
    "AI_LLM_MAX_RETRIES": 1,
    "AI_QUESTION_BUDGET_S": 30.0,
    "AI_MAX_QUESTIONS_PER_USER_PER_DAY": 3,
    "AI_BREAKER_FAILURES": 3,
}


def call(
    turn: int = 0,
    *,
    allow_tools: bool = True,
    results: list[tuple[str, bool]] | None = None,
    tool_calls: int = 0,
    output_tokens: int = 10,
    outcome: str = "ok",
) -> ModelCall:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    if results:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": c, "is_error": e}
                    for i, (c, e) in enumerate(results)
                ],
            }
        )
    return ModelCall(
        "case",
        turn,
        {"system": "", "tools": [], "messages": messages, "allow_tools": allow_tools, "timeout": 5},
        100,
        outcome,
        tool_calls=[{"id": f"t{i}", "name": "list_leads", "input": {}} for i in range(tool_calls)],
        usage=Usage(output_tokens=output_tokens),
    )


def run_of(
    calls: list[ModelCall], *, case_id: str = "correctness.agreed_cpt", **turn: Any
) -> graders.CaseRun:
    return graders.CaseRun(
        cases.BY_ID[case_id],
        AccessScope.organization(uuid.uuid4()),
        [graders.Turn("q", **{"status": "answered", "mode": "llm", **turn})],
        calls,
        [],
        dict(LIMITS),
    )


def test_the_budget_grader_catches_every_overrun():
    within = run_of([call(), call(results=[("x" * 60, False)]), call(allow_tools=False)])
    assert graders.budgets(within).passed
    overruns = {
        "too many calls": [call(), call(), call(allow_tools=False), call(allow_tools=False)],
        "tools after the last round": [call(), call(), call(allow_tools=True)],
        "tool output": [call(), call(results=[("x" * 101, False)])],
        "output tokens": [call(output_tokens=501)],
        "passages": [
            call(),
            call(results=[(json.dumps({"passages": [{"untrusted_text": "y" * 51}]}), False)]),
        ],
        "calls beyond the round limit run": [
            call(tool_calls=3),
            call(results=[("a", False), ("b", False), ("c", False)]),
        ],
    }
    for name, calls in overruns.items():
        assert not graders.budgets(run_of(calls)).passed, name
    slow = run_of([call()], elapsed_s=LIMITS["AI_QUESTION_BUDGET_S"] + graders.TIME_SLACK_S + 1)
    assert not graders.budgets(slow).passed


def test_the_cost_control_checks_fail_when_the_control_does_not_hold():
    cap = cases.BY_ID["cost_controls.daily_cap"]
    refused = graders.Turn("a", status="refused", refused="cap")
    routed = graders.Turn("b", status="answered", mode="router")
    held = graders.CaseRun(
        cap, AccessScope.organization(uuid.uuid4()), [refused, routed], [], [], LIMITS
    )
    assert graders.CHECKS["daily_cap"](held).passed
    leaked = graders.CaseRun(
        cap, held.scope, [refused, routed], [call(turn=0)], [], LIMITS
    )  # a model call despite the cap
    assert not graders.CHECKS["daily_cap"](leaked).passed

    breaker_case = cases.BY_ID["cost_controls.breaker"]
    last = graders.Turn(
        "d", status="answered", mode="retrieval", answer={"notices": ["unavailable"]}
    )
    turns = [graders.Turn("a"), graders.Turn("b"), graders.Turn("c"), last]
    injected = [call(turn=i, outcome="injected") for i in range(3)]
    opened = graders.CaseRun(breaker_case, held.scope, turns, injected, [], LIMITS)
    assert graders.CHECKS["breaker"](opened).passed
    sent = graders.CaseRun(breaker_case, held.scope, turns, [*injected, call(turn=3)], [], LIMITS)
    assert not graders.CHECKS["breaker"](sent).passed

    routed_case = cases.BY_ID["minimisation.routed_sends_nothing"]
    answered = graders.Turn("q", status="answered", mode="router")
    assert graders.CHECKS["routed"](
        graders.CaseRun(routed_case, held.scope, [answered], [], [], LIMITS)
    ).passed
    assert not graders.CHECKS["routed"](
        graders.CaseRun(routed_case, held.scope, [answered], [call()], [], LIMITS)
    ).passed

    cut = call()
    cut.stop_reason, cut.text = "max_tokens", "Here is a detailed summ"
    truncated_case = cases.BY_ID["cost_controls.output_cap"]
    stored = graders.Turn(
        "q",
        status="answered",
        mode="llm",
        answer={"blocks": [{"parts": [{"text": "Here is a detailed summ"}]}]},
    )
    assert not graders.CHECKS["truncated"](
        graders.CaseRun(truncated_case, held.scope, [stored], [cut], [], LIMITS)
    ).passed


def test_the_payload_grader_refuses_unreviewed_and_denied_fields():
    run = run_of([call(results=[(json.dumps({"ref": "lead:1", "email": "x"}), False)])])
    with mock.patch.object(graders.tools, "definitions", return_value=[]):
        result = graders.payload_minimised(run)
    assert not result.passed
    assert "unreviewed result fields ['email']" in result.detail
    assert "denied result fields ['email']" in result.detail


def test_every_case_is_well_formed():
    ids = [c.id for c in cases.CASES]
    assert len(ids) == len(set(ids))
    for case in cases.CASES:
        assert case.category in cases.CATEGORIES
        assert case.id.startswith(case.category + ".")
        assert case.user in {"asha", "vikram", "dev", "rohan", "meena"}
        assert case.bad_fails <= graders.MANDATORY | {
            "figures",
            "keywords",
            "required_citations",
            "citations_relevant",
            "nothing_found",
        }
        assert bool(case.bad) == bool(case.bad_fails), case.id


@pytest.mark.parametrize(
    ("text", "says_nothing_found"),
    [
        ("I couldn't find Fortis Hospital in this workspace.", True),
        ("Apollo Speciality Pune isn't in your workspace.", True),
        ("There's no record of a zebra farm.", True),
        ("That customer doesn't appear in the CRM.", True),
        ("You have no upcoming meetings with Apollo Hospitals.", True),
        ("The Apollo Hospitals deal is worth ₹18,50,000.", False),
        ("Dr Ravi Mehta wants an on-site demo.", False),
    ],
)
def test_the_nothing_found_grader_reads_the_usual_phrasings(text, says_nothing_found):
    assert bool(graders.NOTHING_FOUND.search(text)) is says_nothing_found
