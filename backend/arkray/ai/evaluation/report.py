"""The evaluation's reports: JSON (everything, for tooling and the record) and a Markdown
summary beside it (for people), written to the path given on the command line
(docs/ai-evaluation.md#reports).

Both carry only the synthetic dataset's text, never a key: the key is read by the SDK from
settings and never appears in a payload, a report or a log line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.conf import settings

from . import graders
from .pricing import PRICES_AS_OF, PRICES_SOURCE, PriceTable

if TYPE_CHECKING:
    from .dataset import World
    from .runner import Graded, Options

SCHEMA = "arkray-ai-eval/1"
EXCERPT = 600  # characters of each answer kept in the report


def _settings() -> dict[str, Any]:
    return {
        "AI_CHAT_MODEL": settings.AI_CHAT_MODEL,
        "AI_CHAT_EFFORT": settings.AI_CHAT_EFFORT,
        "AI_EMBEDDING_PROVIDER": settings.AI_EMBEDDING_PROVIDER,
        "AI_LLM_PROVIDER": settings.AI_LLM_PROVIDER,
        **graders.limits_now(),
    }


def _options(options: Options) -> dict[str, Any]:
    return {
        "provider": options.provider,
        "adversarial": options.adversarial,
        "dry_run": options.dry_run,
        "max_cost_usd": options.max_cost_usd,
        "max_model_calls": options.max_model_calls,
        "only": list(options.only),
        "thresholds": {
            "min_pass_rate": options.thresholds.min_pass_rate,
            "cost_controls_pass_rate": options.thresholds.pass_rate("cost_controls"),
            "p50_latency_s": options.thresholds.p50_latency_s,
            "p95_latency_s": options.thresholds.p95_latency_s,
            "max_cost_per_question_usd": options.thresholds.max_cost_per_question_usd,
        },
    }


def _case(g: Graded, include_payloads: bool) -> dict[str, Any]:
    run = g.run
    calls = run.calls
    entry: dict[str, Any] = {
        "id": run.case.id,
        "category": run.case.category,
        "about": run.case.about,
        "asker": run.case.user,
        "workspace": run.case.workspace,
        "passed": g.passed,
        "failed_graders": g.failed,
        "mandatory_failed": [r.name for r in g.mandatory_failures],
        "graders": [
            {"name": r.name, "passed": r.passed, "mandatory": r.mandatory, "detail": r.detail}
            for r in g.results
        ],
        "turns": [
            {
                "question": t.question,
                "status": t.status,
                "mode": t.mode,
                "error_code": t.error_code,
                "refused": t.refused,
                "elapsed_s": round(t.elapsed_s, 3),
                "answer": graders.narrative(t.answer)[:EXCERPT],
                "notices": t.answer.get("notices", []),
                "tools": t.answer.get("provenance", {}).get("tools", []),
            }
            for t in run.turns
        ],
        "calls": [
            {
                "turn": c.turn,
                "outcome": c.outcome,
                "error": c.error,
                "allow_tools": c.request.get("allow_tools"),
                "stop_reason": c.stop_reason,
                "tool_calls": [t["name"] for t in c.tool_calls],
                "model": c.model,
                "latency_s": round(c.latency_s, 3),
                "request_chars": c.request_chars,
                "input_tokens": c.usage.input_tokens,
                "output_tokens": c.usage.output_tokens,
                "cache_read_tokens": c.usage.cache_read_tokens,
                "cache_write_tokens": c.usage.cache_write_tokens,
                "cost_usd": c.cost_usd,
                "price_assumed": c.price_assumed,
                "provider_request_id": c.provider_request_id,
                **({"request": c.request} if include_payloads else {}),
            }
            for c in calls
        ],
        "cost_usd": round(sum(c.cost_usd for c in calls), 6),
        "latency_s": round(sum(t.elapsed_s for t in run.turns), 3),
    }
    if run.error:
        entry["harness_error"] = run.error
    return entry


def build(
    *,
    options: Options,
    status: str,
    reasons: list[str],
    graded: list[Graded],
    measured: dict[str, Any],
    by_category: dict[str, dict[str, Any]],
    graders_total: dict[str, dict[str, Any]],
    detection: dict[str, dict[str, Any]] | None,
    projection: dict[str, Any] | None,
    prices: PriceTable,
    world: World,
    aborted: str,
    started: datetime,
    duration_s: float,
) -> dict[str, Any]:
    include = options.include_payloads or options.mode == "anthropic-dry-run"
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "status_reasons": reasons,
        "mode": options.mode,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_s": round(duration_s, 1),
        "aborted": aborted,
        "options": _options(options),
        "settings": _settings(),
        "dataset": {"counts": world.counts()},
        "pricing": {
            "as_of": PRICES_AS_OF,
            "source": PRICES_SOURCE,
            "usd_per_million_tokens": {m: p.as_dict() for m, p in prices.prices.items()},
        },
        "totals": {
            "cases": len(graded),
            "passed": sum(1 for g in graded if g.passed),
            "failed": sum(1 for g in graded if not g.passed),
            "mandatory_failures": sum(len(g.mandatory_failures) for g in graded),
            **measured,
        },
        "categories": by_category,
        "graders": graders_total,
        "cases": [_case(g, include) for g in graded],
    }
    if detection is not None:
        result["adversarial_detection"] = detection
        result["adversarial_all_detected"] = all(d["detected"] for d in detection.values())
    if projection is not None:
        result["estimate"] = projection
    return result


def not_run(options: Options, reasons: list[str], *, started: datetime) -> dict[str, Any]:
    """The report of a run that never started (an interlock or the database guard)."""
    return {
        "schema": SCHEMA,
        "status": "NOT VERIFIED",
        "status_reasons": reasons,
        "mode": options.mode,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_s": 0.0,
        "aborted": "not started",
        "options": _options(options),
        "settings": _settings(),
        "totals": {"cases": 0, "passed": 0, "failed": 0, "mandatory_failures": 0},
        "categories": {},
        "graders": {},
        "cases": [],
    }


def write(result: dict[str, Any], path: Path) -> tuple[Path, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = path.with_suffix(".md")
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary.write_text(markdown(result), encoding="utf-8")
    return path, summary


def headline(result: dict[str, Any]) -> str:
    totals = result["totals"]
    line = (
        f"{result['status']} ({result['mode']}): {totals.get('passed', 0)}/"
        f"{totals.get('cases', 0)} cases passed, {totals.get('mandatory_failures', 0)} mandatory "
        f"failures"
    )
    if "cost_usd" in totals:
        line += f", {totals['model_calls']} model calls, ${totals['cost_usd']:.4f}"
    if result.get("adversarial_detection") is not None:
        line += f"; planted failures detected: {result['adversarial_all_detected']}"
    reasons = result.get("status_reasons") or []
    return line + ("\n  " + "\n  ".join(reasons) if reasons else "")


def _row(values: list[Any]) -> str:
    return "| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in values) + " |"


def markdown(result: dict[str, Any]) -> str:
    totals = result["totals"]
    out = [
        "# Ask Arkray real-model evaluation",
        "",
        f"**Status: {result['status']}** ({result['mode']}, started {result['started_at']}, "
        f"{result['duration_s']} s)",
        "",
    ]
    out += [f"- {reason}" for reason in result.get("status_reasons", [])]
    s = result.get("settings", {})
    out += [
        "",
        f"Model `{s.get('AI_CHAT_MODEL')}`, effort `{s.get('AI_CHAT_EFFORT')}`, embeddings "
        f"`{s.get('AI_EMBEDDING_PROVIDER')}`; prices as of "
        f"{result.get('pricing', {}).get('as_of', '-')}.",
        "",
        "## Totals",
        "",
        _row(["cases", "passed", "failed", "mandatory failures", "model calls", "cost (USD)"]),
        _row(["---"] * 6),
        _row(
            [
                totals.get("cases", 0),
                totals.get("passed", 0),
                totals.get("failed", 0),
                totals.get("mandatory_failures", 0),
                totals.get("model_calls", 0),
                f"{totals.get('cost_usd', 0):.4f}",
            ]
        ),
        "",
    ]
    if "input_tokens" in totals:
        out += [
            f"Tokens: input {totals['input_tokens']:,}, output {totals['output_tokens']:,}, "
            f"cache read {totals['cache_read_tokens']:,}, cache write "
            f"{totals['cache_write_tokens']:,}. Cost per model-answered question: "
            f"${totals['cost_per_model_question_usd']:.4f}"
            + (" (some calls priced at the table's ceiling)" if totals["price_assumed"] else "")
            + ".",
            f"Latency per model-answered question: p50 {totals['question_latency_p50_s']} s, "
            f"p95 {totals['question_latency_p95_s']} s (per call p50 "
            f"{totals['call_latency_p50_s']} s, p95 {totals['call_latency_p95_s']} s).",
            "",
        ]
    if "estimate" in result:
        e = result["estimate"]
        out += [
            "## Estimate for a real run",
            "",
            f"{e['calls']} calls on `{e['model']}`, about {e['estimated_input_tokens']:,} input "
            f"tokens, {e['assumed_output_tokens_per_call']} output tokens assumed per call: "
            f"about **${e['estimated_cost_usd']:.2f}**; worst case ${e['worst_case_usd']:.2f}. "
            f"{e.get('note', '')}",
            "",
        ]
    dry = result["mode"] == "anthropic-dry-run"
    if dry:
        out += [
            "Dry run: nothing was sent. Each case's first request is in the JSON report "
            "(`cases[].calls[].request`); its answer came from retrieval, so only the "
            "mandatory graders are meaningful here.",
            "",
        ]
    if result.get("categories") and not dry:
        out += [
            "## Categories",
            "",
            _row(["category", "cases", "passed", "rate", "needed", "mandatory failures", "met"]),
            _row(["---"] * 7),
        ]
        for name, c in result["categories"].items():
            out.append(
                _row(
                    [
                        name,
                        c["cases"],
                        c["passed"],
                        f"{c['rate']:.0%}",
                        f"{c['threshold']:.0%}",
                        c["mandatory_failures"],
                        "yes" if c["met"] else "**no**",
                    ]
                )
            )
        out.append("")
    if result.get("adversarial_detection"):
        out += ["## Planted failures (adversarial self-test)", ""]
        for case, d in result["adversarial_detection"].items():
            mark = "detected" if d["detected"] else "**NOT detected**"
            out.append(f"- `{case}`: {mark} (expected {', '.join(d['expected_to_fail'])})")
        out.append("")
    if result.get("cases"):
        out += [
            "## Cases",
            "",
            _row(["case", "result", "failed graders", "mode", "calls", "cost (USD)", "time (s)"]),
            _row(["---"] * 7),
        ]
        for c in result["cases"]:
            modes = ",".join(sorted({t["mode"] or t["status"] for t in c["turns"]}))
            out.append(
                _row(
                    [
                        c["id"],
                        _verdict(c, dry),
                        ", ".join(c["failed_graders"]) or "-",
                        modes,
                        len(c["calls"]),
                        f"{c['cost_usd']:.4f}",
                        c["latency_s"],
                    ]
                )
            )
        failures = [
            (c["id"], g)
            for c in result["cases"]
            for g in c["graders"]
            if not g["passed"] and (g["mandatory"] or not dry)
        ]
        if failures:
            out += ["", "### Failures", ""]
            out += [
                f"- `{case}` {g['name']}{' (mandatory)' if g['mandatory'] else ''}: {g['detail']}"
                for case, g in failures
            ]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _verdict(case: dict[str, Any], dry: bool) -> str:
    if dry:
        return "**FAIL**" if case["mandatory_failed"] else "recorded"
    return "pass" if case["passed"] else "**FAIL**"


def dry_run_text(result: dict[str, Any]) -> str:
    """What a dry run would have sent: the first request of each case, abridged."""
    lines = ["What would be sent (first request of each case; nothing was sent):"]
    shown_prompt = False
    for case in result["cases"]:
        for call in case["calls"]:
            if call["turn"] != 0 or "request" not in call:
                continue
            request = call["request"]
            if not shown_prompt:
                lines.append("--- system prompt (every request) ---")
                lines.append(request["system"])
                shown_prompt = True
            first = request["messages"][0]["content"] if request["messages"] else []
            texts = [b.get("text", "") for b in first if isinstance(b, dict)]
            tools = ", ".join(t["name"] for t in request["tools"])
            lines.append(f"--- {case['id']} ({case['asker']}, {case['workspace']}) ---")
            lines.append(f"tools: {tools}")
            lines.extend(texts)
            lines.append(f"({call['request_chars']:,} characters)")
            break
        else:
            lines.append(f"--- {case['id']}: nothing would be sent ---")
    return "\n".join(lines)
