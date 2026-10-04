"""Re-queue dead outbox events once their cause is fixed (docs/runbooks.md#dead-events).

    manage.py outbox_requeue --id 123 --id 456          these events
    manage.py outbox_requeue --topic ai.index_source    every dead event of a topic
    manage.py outbox_requeue --queue email --since 2026-10-01T00:00:00+05:30

Without `--yes` it is a dry run: it lists what would be re-queued and changes nothing. A
re-queued event is pending again, due now, with a fresh attempt budget; handlers are
idempotent, so work that partly happened before is safe to run again. Skipped and reported:
events whose work is already pending or running again (the same topic and dedupe key), and
events whose payload was blanked after its retention. For account emails prefer the Users
page's *Resend invitation*: a re-queued email event mints a fresh link, sent now.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils.dateparse import parse_datetime

from arkray.core import outbox
from arkray.core.models import OutboxEvent, OutboxStatus

DEFAULT_LIMIT = 1000


class Command(BaseCommand):
    help = "Re-queue dead outbox events (a dry run unless --yes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--id", dest="ids", type=int, action="append", default=[])
        parser.add_argument("--topic")
        parser.add_argument("--queue")
        parser.add_argument(
            "--since", help="ISO 8601 with a time zone: events that died at or after it"
        )
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        parser.add_argument("--yes", action="store_true", help="re-queue (otherwise a dry run)")

    def handle(self, *args: Any, **options: Any) -> None:
        ids, topic, queue, since = options["ids"], options["topic"], options["queue"], None
        if not (ids or topic or queue or options["since"]):
            raise CommandError("Name the events: --id, --topic, --queue and/or --since.")
        if options["since"]:
            since = parse_datetime(options["since"])
            if since is None or since.tzinfo is None:
                raise CommandError("--since must be an ISO 8601 date-time with a time zone.")
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")
        dead = OutboxEvent.objects.filter(status=OutboxStatus.DEAD)
        if ids:
            dead = dead.filter(pk__in=ids)
        if topic:
            dead = dead.filter(topic=topic)
        if queue:
            dead = dead.filter(queue=queue)
        if since:
            dead = dead.filter(finished_at__gte=since)
        selected = list(
            dead.order_by("pk").values("pk", "topic", "queue", "finished_at", "last_error")[
                : options["limit"]
            ]
        )
        for event in selected:
            self.stdout.write(
                f"{event['pk']:>10}  {event['topic']:<32} {event['queue']:<10}"
                f" died {event['finished_at']:%Y-%m-%d %H:%M}  {event['last_error'][:80]}"
            )
        if not options["yes"]:
            self.stdout.write(f"{len(selected)} dead event(s) match. Dry run: add --yes.")
            return
        result = outbox.requeue_dead([event["pk"] for event in selected])
        self.stdout.write(
            f"Re-queued {len(result.requeued)}; skipped {len(result.superseded)} already"
            f" pending again and {len(result.redacted)} whose payload is gone."
        )
