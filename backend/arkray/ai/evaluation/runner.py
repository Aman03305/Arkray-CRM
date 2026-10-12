"""Running the evaluation (docs/ai-evaluation.md#running-it).

Modes:

    scripted               the self-test: every case with its well-behaved script. Every
                           grader must pass. Status NOT VERIFIED (no real model was asked).
    scripted, adversarial  every case with its adversarial script: the graders each script
                           targets must fail. Status NOT VERIFIED; exits non-zero (mandatory
                           failures are planted).
    anthropic, dry run     the real service path with a provider that sends nothing: prints
                           and records what would be sent. Status NOT VERIFIED.
    anthropic              the real model, through the configured AnthropicProvider. Only
                           this can PASS, and only behind every interlock:
                             --allow-external-provider
                             ARKRAY_AI_EVAL_ACK=send-synthetic-data-to-anthropic
                             AI_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY set (the ai
                             worker's configuration), an https AI_LLM_BASE_URL
                             --max-cost-usd and --max-model-calls (hard caps, checked before
                             every call; the run stops when the next call could cross one)
                           Missing any of them: NOT VERIFIED, exit code 2, nothing seeded or
                           sent.

Each question goes through `service.submit` and, unless the router answered it,
`service.answer` (what the ai worker's task runs), inline: no broker or worker is involved,
and the ScriptedProvider/Anthropic choice is the only difference between the modes.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test.utils import override_settings

from arkray.ai import breaker, llm, service
from arkray.ai.llm import ChatProvider, ScriptedProvider
from arkray.ai.models import Question, QuestionStatus
from arkray.core.errors import RateLimitedError

from . import database, dataset, graders, report
from .cases import CASES, CATEGORIES, Case
from .pricing import PriceTable, estimate_tokens
from .recording import (
    DryRunProvider,
    Limits,
    ModelCall,
    RecordingProvider,
    capture_tool_runs,
)

ACK_VARIABLE = "ARKRAY_AI_EVAL_ACK"
ACK_VALUE = "send-synthetic-data-to-anthropic"
DEFAULT_MAX_COST_USD = 5.0
DEFAULT_MAX_MODEL_CALLS = 150
# Assumed output tokens per call when estimating a run that hasn't happened (effort low,
# adaptive thinking included): see docs/ai-evaluation.md#cost-and-duration.
ASSUMED_OUTPUT_TOKENS = 700

NOT_VERIFIED = "NOT VERIFIED"
PASS = "PASS"  # noqa: S105 — a verdict, not a password
FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The go-live bar (docs/ai-evaluation.md#acceptance-thresholds). Every mandatory grader
    must pass in every case regardless."""

    min_pass_rate: float = 0.8  # per category: cases passing every grader
    p50_latency_s: float = 10.0
    p95_latency_s: float = 20.0
    max_cost_per_question_usd: float = 0.10

    def pass_rate(self, category: str) -> float:
        return 1.0 if category == "cost_controls" else self.min_pass_rate


@dataclass(frozen=True, slots=True)
class Options:
    report: Path
    provider: str = "scripted"  # scripted | anthropic
    adversarial: bool = False
    dry_run: bool = False
    allow_external_provider: bool = False
    max_cost_usd: float = DEFAULT_MAX_COST_USD
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    pricing_file: Path | None = None
    only: tuple[str, ...] = ()  # case ids or id prefixes
    include_payloads: bool = False
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def mode(self) -> str:
        if self.provider == "anthropic":
            return "anthropic-dry-run" if self.dry_run else "anthropic"
        return "scripted-adversarial" if self.adversarial else "scripted"


def selected(options: Options) -> list[Case]:
    if not options.only:
        return list(CASES)
    return [c for c in CASES if any(c.id == o or c.id.startswith(o) for o in options.only)]


def missing_interlocks(options: Options) -> list[str]:
    """What the real run still needs; empty when everything is in place."""
    missing: list[str] = []
    if not options.allow_external_provider:
        missing.append("--allow-external-provider was not given")
    if os.environ.get(ACK_VARIABLE) != ACK_VALUE:
        missing.append(f"{ACK_VARIABLE}={ACK_VALUE} is not set")
    if settings.AI_LLM_PROVIDER != "anthropic":
        missing.append("AI_LLM_PROVIDER is not 'anthropic'")
    if not settings.ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY is not set")
    if not settings.AI_LLM_KEY_HOLDER:
        missing.append("AI_LLM_KEY_HOLDER is false (this process may not call the provider)")
    if not str(settings.AI_LLM_BASE_URL).startswith("https://"):
        missing.append("AI_LLM_BASE_URL is not https://")
    if not options.max_cost_usd > 0 or not math.isfinite(options.max_cost_usd):
        missing.append("--max-cost-usd must be a positive amount")
    if options.max_model_calls < 1:
        missing.append("--max-model-calls must be at least 1")
    return missing


@contextmanager
def evaluation_settings() -> Iterator[None]:
    """Ask Arkray on for the run (the evaluation database only): answering and indexing.
    The hashing embedder (tests) needs its lower similarity floor, as tests.ai_fixtures."""
    extra: dict[str, Any] = {}
    if settings.AI_EMBEDDING_PROVIDER == "hashing":
        extra["AI_RETRIEVAL_MIN_SIMILARITY"] = 0.05
    with override_settings(AI_ENABLED=True, AI_INDEXING_ENABLED=True, **extra):
        yield


# --- running cases --------------------------------------------------------------------------------
def run_case(
    world: dataset.World,
    case: Case,
    recorder: RecordingProvider,
    *,
    script: list[Any] | None,
) -> graders.CaseRun:
    """Ask the case's questions as its user, in its workspace. `script`: the scripted
    model's steps (None: the recorder's provider as it is, for the real or dry run)."""
    scope = world.scope(case.user, case.workspace)
    actor = world.users[case.user]
    turns: list[graders.Turn] = []
    error = ""
    with override_settings(**case.settings_dict):
        limits = graders.limits_now()
        breaker.reset()
        recorder.begin(case.id, 0, ScriptedProvider(steps=script) if script is not None else None)
        if case.expect.check == "breaker":
            recorder.inject_failures(settings.AI_BREAKER_FAILURES)
        conversation = None
        try:
            for index, text in enumerate(case.questions):
                recorder.begin(case.id, index)
                turn = graders.Turn(text)
                turns.append(turn)
                started = time.perf_counter()
                try:
                    question = service.submit(actor, scope, text, conversation)
                except RateLimitedError as exc:
                    turn.status, turn.refused = "refused", str(exc)
                    turn.elapsed_s = time.perf_counter() - started
                    continue
                conversation = question.conversation_id
                if question.status == QuestionStatus.PENDING:
                    service.answer(question.pk)
                stored = Question.objects.get(pk=question.pk)
                turn.elapsed_s = time.perf_counter() - started
                turn.status = str(stored.status)
                turn.mode, turn.error_code, turn.answer = (
                    stored.mode,
                    stored.error_code,
                    stored.answer,
                )
                turn.visible = service.visible_answer(stored.answer, scope) if stored.answer else {}
        except Exception as exc:  # noqa: BLE001 — recorded as the case's (mandatory) failure
            error = f"{type(exc).__name__}: {exc}"
        finally:
            recorder.inject_failures(0)
            breaker.reset()
    return graders.CaseRun(
        case,
        scope,
        turns,
        recorder.calls_of(case.id),
        recorder.tool_runs_of(case.id),
        limits,
        error,
    )


@dataclass
class Graded:
    run: graders.CaseRun
    results: list[graders.Result]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def mandatory_failures(self) -> list[graders.Result]:
        return [r for r in self.results if r.mandatory and not r.passed]

    @property
    def failed(self) -> list[str]:
        return [r.name for r in self.results if not r.passed]


def run_suite(
    world: dataset.World,
    cases: list[Case],
    recorder: RecordingProvider,
    *,
    scripted: bool,
    adversarial: bool = False,
    say: Callable[[str], None] = lambda _: None,
) -> list[Graded]:
    graded: list[Graded] = []
    with llm.override_provider(recorder), capture_tool_runs(recorder):
        for case in cases:
            if recorder.aborted:
                break
            script = None
            if scripted:
                chosen = case.bad if adversarial and case.bad is not None else case.good
                script = chosen(world)
            run = run_case(world, case, recorder, script=script)
            results = graders.grade(run, world)
            graded.append(Graded(run, results))
            if isinstance(recorder.inner, DryRunProvider):
                say(f"  dry  {case.id}: {len(run.calls)} request(s) recorded, nothing sent")
            else:
                say(f"  {'pass' if all(r.passed for r in results) else 'FAIL':4} {case.id}")
    return graded


# --- metrics and the verdict ----------------------------------------------------------------------
def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (0 for no values)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def sent_calls(graded: list[Graded]) -> list[ModelCall]:
    return [c for g in graded for c in g.run.calls if c.outcome in {"ok", "error"}]


def metrics(graded: list[Graded]) -> dict[str, Any]:
    calls = sent_calls(graded)
    questions = [
        turn.elapsed_s
        for g in graded
        for index, turn in enumerate(g.run.turns)
        if any(c.turn == index and c.outcome in {"ok", "error"} for c in g.run.calls)
    ]
    cost = sum(c.cost_usd for c in calls)
    return {
        "questions": sum(len(g.run.turns) for g in graded),
        "model_questions": len(questions),
        "model_calls": len(calls),
        "model_call_errors": sum(1 for c in calls if c.outcome == "error"),
        "input_tokens": sum(c.usage.input_tokens for c in calls),
        "output_tokens": sum(c.usage.output_tokens for c in calls),
        "cache_read_tokens": sum(c.usage.cache_read_tokens for c in calls),
        "cache_write_tokens": sum(c.usage.cache_write_tokens for c in calls),
        "cost_usd": round(cost, 4),
        "cost_per_model_question_usd": round(cost / len(questions), 4) if questions else 0.0,
        "price_assumed": any(c.price_assumed for c in calls),
        "question_latency_p50_s": round(percentile(questions, 0.5), 3),
        "question_latency_p95_s": round(percentile(questions, 0.95), 3),
        "call_latency_p50_s": round(percentile([c.latency_s for c in calls], 0.5), 3),
        "call_latency_p95_s": round(percentile([c.latency_s for c in calls], 0.95), 3),
        "models": sorted({c.model for c in calls if c.model}),
    }


def categories(graded: list[Graded], thresholds: Thresholds) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        mine = [g for g in graded if g.run.case.category == category]
        if not mine:
            continue
        passed = sum(1 for g in mine if g.passed)
        rate = passed / len(mine)
        mandatory = sum(len(g.mandatory_failures) for g in mine)
        found[category] = {
            "cases": len(mine),
            "passed": passed,
            "rate": round(rate, 3),
            "threshold": thresholds.pass_rate(category),
            "mandatory_failures": mandatory,
            "met": rate >= thresholds.pass_rate(category) and not mandatory,
        }
    return found


def grader_totals(graded: list[Graded]) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for g in graded:
        for result in g.results:
            entry = totals.setdefault(
                result.name, {"passed": 0, "failed": 0, "mandatory": result.mandatory}
            )
            entry["passed" if result.passed else "failed"] += 1
    return dict(sorted(totals.items()))


def verdict(
    mode: str,
    graded: list[Graded],
    measured: dict[str, Any],
    by_category: dict[str, dict[str, Any]],
    thresholds: Thresholds,
    *,
    aborted: str,
    expected_cases: int,
) -> tuple[str, list[str]]:
    """(status, reasons). Only the real model can PASS. A dry run's answers come from
    retrieval (nothing was sent), so only its mandatory graders (what would have been sent,
    the controls) mean anything."""
    reasons: list[str] = []
    mandatory = [f"{g.run.case.id}: {r.name}" for g in graded for r in g.mandatory_failures]
    if mandatory:
        reasons.append(f"{len(mandatory)} mandatory grader failures: " + "; ".join(mandatory))
    for category, entry in by_category.items() if mode != "anthropic-dry-run" else []:
        if entry["rate"] < entry["threshold"]:
            reasons.append(
                f"{category}: {entry['passed']}/{entry['cases']} cases passed "
                f"(needs {entry['threshold']:.0%})"
            )
    if mode != "anthropic":
        why = {
            "scripted": "a scripted model answered (self-test); no real model was evaluated",
            "scripted-adversarial": "an adversarial scripted model answered (grader self-test)",
            "anthropic-dry-run": "dry run: nothing was sent to the provider",
        }[mode]
        return NOT_VERIFIED, [why, *reasons]
    if mandatory:
        return FAIL, reasons
    if aborted or len(graded) < expected_cases:
        # A case cut short by a cap fails its quality graders through no fault of the
        # model: an incomplete run is neither a pass nor (without a mandatory failure) a fail.
        stopped = f"the run stopped early: {aborted or 'not every case ran'}"
        return NOT_VERIFIED, [stopped, *reasons]
    if measured["question_latency_p95_s"] > thresholds.p95_latency_s:
        reasons.append(f"p95 latency {measured['question_latency_p95_s']} s")
    if measured["question_latency_p50_s"] > thresholds.p50_latency_s:
        reasons.append(f"p50 latency {measured['question_latency_p50_s']} s")
    if measured["cost_per_model_question_usd"] > thresholds.max_cost_per_question_usd:
        reasons.append(f"cost per question ${measured['cost_per_model_question_usd']}")
    return (FAIL, reasons) if reasons else (PASS, [])


def estimate(
    calls: list[ModelCall], prices: PriceTable, model: str, *, output_tokens: int
) -> dict[str, Any]:
    """What these calls would cost on `model`, from their payload sizes (not from a
    provider's usage): the tool list and system prompt (the cached prefix) written once per
    distinct prefix and read from the cache after, the rest uncached, `output_tokens` per
    call. A projection, labelled as one."""
    price, _ = prices.price(model)
    seen: set[str] = set()
    input_cost = write_cost = read_cost = 0.0
    tokens = 0
    for call in calls:
        prefix = len(str(call.request.get("system", ""))) + len(str(call.request.get("tools", "")))
        prefix_tokens = estimate_tokens(prefix)
        total = estimate_tokens(call.request_chars)
        tokens += total
        key = f"{prefix}"
        if key in seen:
            read_cost += prefix_tokens * price.cache_read
        else:
            seen.add(key)
            write_cost += prefix_tokens * price.cache_write
        input_cost += max(0, total - prefix_tokens) * price.input
    output_cost = len(calls) * output_tokens * price.output
    usd = (input_cost + write_cost + read_cost + output_cost) / 1_000_000
    return {
        "model": model,
        "calls": len(calls),
        "estimated_input_tokens": tokens,
        "assumed_output_tokens_per_call": output_tokens,
        "estimated_cost_usd": round(usd, 4),
        "basis": "payload characters / 3 + 600 tokens per call; prefix cached after first use",
    }


def worst_case(questions: int, prices: PriceTable, model: str) -> float:
    """An upper bound for `questions` model-answered questions: every tool round used, the
    whole tool-output budget in every call, every output budget spent, nothing cached."""
    calls = int(settings.AI_MAX_TOOL_ROUNDS) + 1
    prefix_chars = 12_000  # system prompt and tool list, generously
    input_chars = prefix_chars + int(settings.AI_TOOL_RESULTS_MAX_CHARS) + 4_000
    per_call = prices.worst_case(
        model,
        input_tokens=estimate_tokens(input_chars),
        max_output_tokens=int(settings.AI_CHAT_MAX_TOKENS),
    )
    return round(questions * calls * per_call, 2)


def adversarial_detection(graded: list[Graded]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for g in graded:
        case = g.run.case
        if case.bad is None:
            continue
        failed = set(g.failed)
        found[case.id] = {
            "expected_to_fail": sorted(case.bad_fails),
            "failed": sorted(failed),
            "detected": case.bad_fails <= failed,
        }
    return found


# --- the whole run --------------------------------------------------------------------------------
def build_recorder(options: Options, prices: PriceTable) -> RecordingProvider:
    if options.provider == "anthropic" and options.dry_run:
        return RecordingProvider(DryRunProvider(settings.AI_CHAT_MODEL), prices=prices)
    if options.provider == "anthropic":
        inner: ChatProvider | None = llm.get_provider()
        if inner is None:
            raise RuntimeError("The configured provider isn't available (AI_LLM_PROVIDER/key).")
        return RecordingProvider(
            inner,
            prices=prices,
            limits=Limits(options.max_model_calls, options.max_cost_usd),
            sends=True,
        )
    return RecordingProvider(ScriptedProvider(steps=[]), prices=prices)


def evaluate(
    world: dataset.World,
    options: Options,
    recorder: RecordingProvider,
    *,
    say: Callable[[str], None] = lambda _: None,
    started: datetime | None = None,
) -> dict[str, Any]:
    """Run the selected cases and build the report (not yet written)."""
    started = started or datetime.now(UTC)
    clock = time.perf_counter()
    cases = selected(options)
    graded = run_suite(
        world,
        cases,
        recorder,
        scripted=options.provider == "scripted",
        adversarial=options.adversarial,
        say=say,
    )
    measured = metrics(graded)
    by_category = categories(graded, options.thresholds)
    status, reasons = verdict(
        options.mode,
        graded,
        measured,
        by_category,
        options.thresholds,
        aborted=recorder.aborted,
        expected_cases=len(cases),
    )
    prices = recorder.prices
    model = settings.AI_CHAT_MODEL
    projection: dict[str, Any] | None = None
    if options.mode != "anthropic":
        projection = estimate(
            [c for g in graded for c in g.run.calls if c.outcome in {"ok", "error", "not_sent"}],
            prices,
            model,
            output_tokens=ASSUMED_OUTPUT_TOKENS,
        )
        projection["worst_case_usd"] = worst_case(
            sum(
                1
                for g in graded
                for i, _ in enumerate(g.run.turns)
                if any(c.turn == i for c in g.run.calls)
            ),
            prices,
            model,
        )
        if options.mode == "anthropic-dry-run":
            projection["note"] = "first requests only (later rounds need the model's replies)"
    return report.build(
        options=options,
        status=status,
        reasons=reasons,
        graded=graded,
        measured=measured,
        by_category=by_category,
        graders_total=grader_totals(graded),
        detection=adversarial_detection(graded) if options.mode == "scripted-adversarial" else None,
        projection=projection,
        prices=prices,
        world=world,
        aborted=recorder.aborted,
        started=started,
        duration_s=time.perf_counter() - clock,
    )


def exit_code(result: dict[str, Any]) -> int:
    """0: PASS, or a self-test/dry run without failures; 1: any failure; 2: not run."""
    if result["status"] == PASS:
        return 0
    if result["status"] == FAIL or result["totals"]["mandatory_failures"]:
        return 1
    if result["mode"] in {"scripted", "scripted-adversarial"} and result["totals"]["failed"]:
        return 1
    if result["mode"] == "anthropic" or result.get("aborted"):
        return 2
    return 0


def main(options: Options, say: Callable[[str], None]) -> int:
    """The management command's body: interlocks, database, seed, run, report."""
    started = datetime.now(UTC)
    prices = PriceTable.from_file(options.pricing_file)
    if options.provider == "anthropic" and not options.dry_run:
        missing = missing_interlocks(options)
        if missing:
            result = report.not_run(options, missing, started=started)
            report.write(result, options.report)
            say(f"{NOT_VERIFIED}: the real-model run needs: " + "; ".join(missing))
            return 2
    try:
        database.prepare(say)
    except database.DatabaseRefused as exc:
        result = report.not_run(options, [str(exc)], started=started)
        report.write(result, options.report)
        say(f"{NOT_VERIFIED}: {exc}")
        return 2
    with evaluation_settings():
        say("Seeding the synthetic CRM and indexing it ...")
        world = dataset.seed()
        database.assert_seeded(dataset.DATASET_VERSION)
        recorder = build_recorder(options, prices)
        say(f"Running {len(selected(options))} cases ({options.mode}) ...")
        result = evaluate(world, options, recorder, say=say, started=started)
    report.write(result, options.report)
    if options.mode == "anthropic-dry-run":
        say(report.dry_run_text(result))
    say(report.headline(result))
    return exit_code(result)
