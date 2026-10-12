"""Ask Arkray's real-model evaluation (risk R69, docs/ai-evaluation.md).

    manage.py ai_eval --report out/ai-eval.json
        the self-test: the whole suite with a well-behaved scripted model (NOT VERIFIED)
    manage.py ai_eval --adversarial --report out/ai-eval-adversarial.json
        the graders' self-test: planted failures must be caught (exits 1 by design)
    manage.py ai_eval --provider anthropic --dry-run --report out/ai-eval-dry.json
        what would be sent to Anthropic, without sending anything (NOT VERIFIED)
    ARKRAY_AI_EVAL_ACK=send-synthetic-data-to-anthropic AI_LLM_PROVIDER=anthropic \\
    ANTHROPIC_API_KEY=... manage.py ai_eval --provider anthropic --allow-external-provider \\
        --max-cost-usd 5 --report out/ai-eval-real.json
        the real run (PASS or FAIL), once the owner has approved it

DATABASE_URL must name a database of the evaluation's own (`..._ai_eval`): it is created (or
dropped and created again, if it carries the evaluation's marker and nothing else), migrated
and seeded with synthetic data only; any other database is refused unchanged.

Exit codes: 0 PASS (or a self-test or dry run without failures), 1 a failed case, 2 not run
(an interlock missing, the database refused, or the run stopped at a cap).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.ai.evaluation import runner


class Command(BaseCommand):
    help = "Evaluate Ask Arkray end to end on a synthetic CRM (scripted, dry run, or Anthropic)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--report", required=True, type=Path, help="JSON report path.")
        parser.add_argument("--provider", choices=["scripted", "anthropic"], default="scripted")
        parser.add_argument(
            "--adversarial", action="store_true", help="Scripted: the adversarial scripts."
        )
        parser.add_argument("--dry-run", action="store_true", help="Anthropic: record, never send.")
        parser.add_argument(
            "--allow-external-provider",
            action="store_true",
            help="Required (with ARKRAY_AI_EVAL_ACK) to send synthetic data to Anthropic.",
        )
        parser.add_argument("--max-cost-usd", type=float, default=runner.DEFAULT_MAX_COST_USD)
        parser.add_argument("--max-model-calls", type=int, default=runner.DEFAULT_MAX_MODEL_CALLS)
        parser.add_argument("--pricing-file", type=Path, default=None)
        parser.add_argument(
            "--case", action="append", default=[], help="Only these case ids (or prefixes)."
        )
        parser.add_argument(
            "--include-payloads", action="store_true", help="Every request in the report."
        )
        defaults = runner.Thresholds()
        parser.add_argument("--min-pass-rate", type=float, default=defaults.min_pass_rate)
        parser.add_argument("--p50-latency-s", type=float, default=defaults.p50_latency_s)
        parser.add_argument("--p95-latency-s", type=float, default=defaults.p95_latency_s)
        parser.add_argument(
            "--max-cost-per-question-usd",
            type=float,
            default=defaults.max_cost_per_question_usd,
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["adversarial"] and options["provider"] != "scripted":
            raise CommandError("--adversarial is a scripted self-test.")
        if options["dry_run"] and options["provider"] != "anthropic":
            raise CommandError("--dry-run applies to --provider anthropic.")
        chosen = runner.Options(
            report=options["report"],
            provider=options["provider"],
            adversarial=options["adversarial"],
            dry_run=options["dry_run"],
            allow_external_provider=options["allow_external_provider"],
            max_cost_usd=options["max_cost_usd"],
            max_model_calls=options["max_model_calls"],
            pricing_file=options["pricing_file"],
            only=tuple(options["case"]),
            include_payloads=options["include_payloads"],
            thresholds=runner.Thresholds(
                min_pass_rate=options["min_pass_rate"],
                p50_latency_s=options["p50_latency_s"],
                p95_latency_s=options["p95_latency_s"],
                max_cost_per_question_usd=options["max_cost_per_question_usd"],
            ),
        )
        code = runner.main(chosen, lambda line: self.stdout.write(line))
        self.stdout.write(f"Report: {chosen.report} (summary: {chosen.report.with_suffix('.md')})")
        if code:
            raise SystemExit(code)
