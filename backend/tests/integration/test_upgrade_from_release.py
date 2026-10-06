"""Upgrading from the supported previous release keeps every record, and a rollback by
migration is refused instead of deleting what was recorded since (final audit ARCH-1/ARCH-2,
R108; docs/deployment.md#rollback, docs/database.md#reversibility).

A database at the release candidate's schema (v1.0 RC, 64bb641) is filled through that
release's own models (the historical models at its leaf nodes) with realistic data and its
edge cases, then migrated forwards through every later state an installation may be on
(0c31aee, 5b05177, 989830b), with the data those states could hold added on the way, to the
latest schema. Every step is checked against checksums of the rows as they were, so "the
backfill is not an edit" is verified, not assumed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone

from arkray.core.access import AccessScope
from tests.integration.migration_states import (
    ADR_0027,
    ADR_0028,
    ENHANCEMENTS,
    RELEASE_CANDIDATE,
    build,
    executor,
    latest,
    migrate,
    restore_latest,
    state_apps,
)

pytestmark = pytest.mark.django_db(transaction=True)

BASE = datetime(2025, 1, 6, 4, 30, tzinfo=UTC)
CRM_TZ = ZoneInfo(settings.CRM_TIME_ZONE)
PLACEHOLDER = "Unknown customer"
CPT = "Rs 18 per test"

# Leads whose names the 0006 backfill must cope with: first name, last name, organisation.
EDGE_LEADS = [
    ("  Rahul ", " Verma  ", "  Apollo Diagnostics  "),  # padding: kept exactly as it was
    ("Zoë", "Ñúñez-Ångström", "Śrī Sāī Diagnostics 🧪 Pvt. Ltd."),
    ("डॉ. अनीता", "शर्मा", ""),  # Devanagari, no organisation
    ("Jose" + chr(0x301), "D" + chr(0x2019) + "Souza", ""),  # a combining accent, a curly quote
    ("A" * 100, "B" * 100, ""),  # a 201-character name: cut to 200 (the lead keeps it whole)
    ("", "", "C" * 200),  # an organisation alone, at its maximum length
    ("", "", "Metro Hospital"),
    ("Neha", "", ""),
    ("   ", "", ""),  # spaces only (past validation): this aborted the migration before
    ("", "", "   "),  # an organisation of spaces only: kept as it was (it never aborted)
    (chr(9), "", ""),  # a tab, which TRIM leaves: kept as it was
]
SPACES_ONLY = 8  # EDGE_LEADS index of the lead that used to abort the migration

# The 0006 backfill as released (0c31aee), as a query: what it wrote for each opportunity.
RELEASED_BACKFILL = """
SELECT o.id::text,
       (o.created_at AT TIME ZONE %s)::date,
       LEFT(COALESCE(NULLIF(l.organization_name, ''), l.display_name), 200),
       LEFT(l.display_name, 200)
  FROM pipeline_opportunity AS o
  JOIN leads_lead AS l ON l.id = o.lead_id
"""

# The schema an upgraded database ends with is the one a new installation gets. Allowed
# differences: none. The same operations run in both cases, and none of them depends on the
# data (0006's callable default is dropped again once the rows are filled).
ALLOWED_SCHEMA_DIFFERENCES: dict[str, set[tuple[object, ...]]] = {}

CATALOG = {
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attgenerated
          FROM pg_attribute AS a
          JOIN pg_class AS c ON c.oid = a.attrelid
          LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')
           AND a.attnum > 0 AND NOT a.attisdropped
    """,
    "constraints": """
        SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid), convalidated
          FROM pg_constraint WHERE connamespace = 'public'::regnamespace
    """,
    "indexes": """
        SELECT i.indexrelid::regclass::text, pg_get_indexdef(i.indexrelid), i.indisvalid
          FROM pg_index AS i JOIN pg_class AS c ON c.oid = i.indexrelid
         WHERE c.relnamespace = 'public'::regnamespace
    """,
    "triggers": """
        SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid), tgenabled
          FROM pg_trigger WHERE NOT tgisinternal
    """,
    "functions": """
        SELECT p.proname, pg_get_functiondef(p.oid)
          FROM pg_proc AS p
         WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f'
           AND NOT EXISTS (SELECT 1 FROM pg_depend AS d WHERE d.objid = p.oid AND d.deptype = 'e')
    """,
    "extensions": "SELECT extname FROM pg_extension",
    "storage": """
        SELECT relname, reloptions::text FROM pg_class
         WHERE relnamespace = 'public'::regnamespace AND reloptions IS NOT NULL
    """,
}

# What each migration after the release refuses to undo, as `migrate <app> <target>` would
# ask for it. The first migration each backwards plan reaches refuses (test_migrations.py).
ROLLBACKS = [
    ("pipeline", "0005_search_indexes"),
    ("pipeline", "0008_opportunity_expected_cpt"),
    ("identity", "0003_workspace_access_window"),
    ("activities", "0009_activities_autovacuum"),
    ("audit", "0002_audit_event_append_only"),
    ("leads", "0005_owner_index_covers_archive"),
]


def rows(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def catalog():
    return {name: set(rows(sql)) for name, sql in CATALOG.items()}


def applied():
    return set(rows("SELECT app, name FROM django_migrations"))


def model_columns(apps):
    """Each table of a release's models, with the columns that release's code knew."""
    return {
        model._meta.db_table: [field.column for field in model._meta.concrete_fields]
        for model in apps.get_models(include_auto_created=True)
    }


def checksums(columns):
    """Per table: the row count and an md5 of every row's text (those columns only)."""
    result = {}
    for table, names in sorted(columns.items()):
        row = ", ".join(f'"{name}"' for name in names)
        result[table] = rows(
            f"SELECT count(*), md5(coalesce(string_agg(t.r, E'\\n' ORDER BY t.r), ''))"  # noqa: S608
            f' FROM (SELECT ROW({row})::text AS r FROM "{table}") AS t'
        )[0]
    return result


# --- the release candidate's data, through its own models ------------------------------------
def seed_release(apps):
    User = apps.get_model("identity", "User")
    Lead = apps.get_model("leads", "Lead")
    Pipeline = apps.get_model("pipeline", "Pipeline")
    Stage = apps.get_model("pipeline", "Stage")
    Opportunity = apps.get_model("pipeline", "Opportunity")
    StageHistory = apps.get_model("pipeline", "StageHistory")
    Activity = apps.get_model("activities", "Activity")
    TimelineEntry = apps.get_model("activities", "TimelineEntry")
    AuditEvent = apps.get_model("audit", "AuditEvent")

    password = make_password("Arkray-upgrade-drill-1")

    def user(email, first, last, role="sales_user", **extra):
        values = {"status": "active", "is_active": True, "activated_at": BASE, **extra}
        return User.objects.create(
            email=email,
            first_name=first,
            last_name=last,
            role=role,
            password=password,
            created_at=BASE,
            **values,
        )

    admin = user("anita.admin@arkray.test", "Anita", "Admin", role="admin")
    rahul = user("rahul.sharma@arkray.test", "Rahul", "Sharma")
    priya = user("priya.patel@arkray.test", "Priya", "Patel")
    user(
        "vikram.rao@arkray.test",
        "Vikram",
        "Rao",
        status="deactivated",
        is_active=False,
        deactivated_at=BASE + timedelta(days=30),
    )
    sellers = [rahul, priya]

    # The seeded pipeline, renamed by an administrator (the key decides, not the name), a
    # stage of their own, and a second pipeline with a "negotiation" key of its own.
    sales = Pipeline.objects.get(key="sales")
    Pipeline.objects.filter(pk=sales.pk).update(name="Sales")
    Stage.objects.create(
        pipeline=sales, key="demo", name="Demo", position=35, probability=60, category="open"
    )
    service = Pipeline.objects.create(key="service", name="Service Contracts", created_at=BASE)
    for key, name, position, probability, category in [
        ("new", "New", 10, 10, "open"),
        ("negotiation", "Negotiation", 20, 60, "open"),
        ("won", "Won", 30, 100, "won"),
        ("lost", "Lost", 40, 0, "lost"),
    ]:
        Stage.objects.create(
            pipeline=service,
            key=key,
            name=name,
            position=position,
            probability=probability,
            category=category,
        )
    stages = {(s.pipeline_id, s.key): s for s in Stage.objects.all()}

    statuses = ["new", "contacted", "qualified", "unqualified", "converted"]
    sources = ["website", "referral", "cold_call", None, "event"]
    surnames = ["Iyer", "Khan", "Menon", "Gupta", "Das", "Reddy", "Singh", "Joseph"]
    hospitals = ["Apollo Diagnostics", "Metro Hospital", "", "Lifeline Labs", "City Clinic"]
    leads = []
    for i in range(200):
        if i < len(EDGE_LEADS):
            first, last, organization = EDGE_LEADS[i]
        else:
            first, last = f"Lead{i}", surnames[i % len(surnames)]
            organization = hospitals[i % len(hospitals)]
        owner = sellers[(i // 3) % 2]
        leads.append(
            Lead(
                first_name=first,
                last_name=last,
                organization_name=organization,
                email=f"lead{i}@hospital.example" if i % 3 else "",
                status_id=statuses[i % len(statuses)],
                source_id=sources[i % len(sources)],
                rating=["hot", "warm", "cold", None][i % 4],
                owner=owner,
                created_by=admin if i % 7 == 0 else owner,
                description="Interested in a glucose analyser." if i % 5 == 0 else "",
                created_at=BASE + timedelta(hours=i),
                archived_at=BASE + timedelta(days=40) if i >= 100 and i % 17 == 0 else None,
                version=1 + i % 3,
            )
        )
    leads = Lead.objects.bulk_create(leads)

    # Opportunities: every stage, won, lost, archived, two on one lead, a second pipeline.
    plan = ["new", "qualified", "proposal", "negotiation", "demo", "won", "lost", "negotiation"]
    opportunities, history = [], []
    for i in range(90):
        lead = leads[i if i < 80 else i - 80]
        pipeline = service if i % 10 == 9 else sales
        key = plan[i % len(plan)]
        if pipeline is service:
            key = {"qualified": "new", "proposal": "new", "demo": "new"}.get(key, key)
        stage = stages[(pipeline.pk, key)]
        created = BASE + timedelta(days=i, hours=i % 24)
        if i == 0:  # 20:00 UTC is 01:30 the next day in India
            created = datetime(2026, 3, 31, 20, 0, tzinfo=UTC)
        closed = stage.category != "open"
        opportunities.append(
            Opportunity(
                title=f"{lead.organization_name or lead.first_name or 'Lead'} analyser {i}"[:200]
                or "Analyser",
                lead=lead,
                owner_id=lead.owner_id,
                pipeline=pipeline,
                stage=stage,
                status=stage.category,
                value=Decimal(f"{(i + 1) * 12345}.50"),
                probability=stage.probability,
                expected_close_date=date(2026, 12, 31) if i % 2 else None,
                description="Wants two analysers for the new wing." if i % 4 == 0 else "",
                lost_reason="Budget moved to next year" if stage.category == "lost" else "",
                closed_at=created + timedelta(days=5) if closed else None,
                created_by=admin if i % 6 == 0 else lead.owner,
                created_at=created,
                archived_at=created + timedelta(days=60) if i % 9 == 4 else None,
                version=1 + i % 4,
            )
        )
    opportunities = Opportunity.objects.bulk_create(opportunities)
    for i, opportunity in enumerate(opportunities):
        opportunity.updated_at = opportunity.created_at + timedelta(days=1, minutes=i)
    Opportunity.objects.bulk_update(opportunities, ["updated_at"])
    for opportunity in opportunities:
        first = stages[(opportunity.pipeline_id, "new")]
        history.append(
            StageHistory(
                opportunity=opportunity,
                to_stage=first,
                to_stage_name=first.name,
                to_status="open",
                value=opportunity.value,
                probability=first.probability,
                actor_id=opportunity.created_by_id,
                occurred_at=opportunity.created_at,
            )
        )
        if opportunity.stage_id != first.pk:
            history.append(
                StageHistory(
                    opportunity=opportunity,
                    from_stage=first,
                    from_stage_name=first.name,
                    from_status="open",
                    to_stage=opportunity.stage,
                    to_stage_name=opportunity.stage.name,
                    to_status=opportunity.status,
                    value=opportunity.value,
                    probability=opportunity.probability,
                    lost_reason=opportunity.lost_reason,
                    actor_id=opportunity.owner_id,
                    occurred_at=opportunity.created_at + timedelta(days=2),
                )
            )
    StageHistory.objects.bulk_create(history)

    # Activities with their timeline, as the release's services wrote them.
    by_lead = {}
    for opportunity in opportunities:
        by_lead.setdefault(opportunity.lead_id, opportunity)
    activities, timeline, audit = [], [], []
    for i, lead in enumerate(leads[:60]):
        opportunity = by_lead.get(lead.pk) if i % 2 == 0 else None
        at = lead.created_at + timedelta(days=3)
        common = {
            "lead": lead,
            "opportunity": opportunity,
            "owner_id": lead.owner_id,
            "created_by_id": lead.owner_id,
            "created_at": at,
        }
        activities += [
            Activity(
                type="task",
                title="Send the quotation",
                status="open",
                priority="high",
                due_at=at + timedelta(days=7),
                **common,
            ),
            Activity(
                type="task",
                title="Call back",
                status="completed",
                priority="normal",
                completed_at=at + timedelta(days=1),
                completed_by_id=lead.owner_id,
                **common,
            ),
            Activity(
                type="meeting",
                title="Demo of the analyser",
                status="scheduled",
                starts_at=at + timedelta(days=10),
                ends_at=at + timedelta(days=10, hours=1),
                location=("Ward 4, " + (lead.organization_name or "the clinic"))[:100],
                meeting_url="https://meet.example.test/demo" if i % 2 else "",
                **common,
            ),
            Activity(
                type="note",
                description=f"Spoke to {lead.first_name or 'them'}: wants a demo. ₹ quoted.",
                archived_at=at + timedelta(days=20) if i % 11 == 0 else None,
                **common,
            ),
        ]
    activities = Activity.objects.bulk_create(activities)
    kinds = {"task": "task.created", "meeting": "meeting.scheduled", "note": "note.added"}
    for lead in leads:
        timeline.append(
            TimelineEntry(
                lead=lead,
                kind="lead.created",
                actor_id=lead.created_by_id,
                occurred_at=lead.created_at,
                data={"status": lead.status_id, "owner_id": str(lead.owner_id)},
            )
        )
        audit.append(
            AuditEvent(
                actor_type="user",
                actor_id=lead.created_by_id,
                action="lead.created",
                target_type="lead",
                target_id=str(lead.pk),
                occurred_at=lead.created_at,
                request_id=f"req-{lead.pk.hex[:12]}",
                ip_address="10.0.0.7",
                metadata={"status": lead.status_id},
            )
        )
    for opportunity in opportunities:
        timeline.append(
            TimelineEntry(
                lead_id=opportunity.lead_id,
                opportunity=opportunity,
                kind="opportunity.created",
                actor_id=opportunity.created_by_id,
                occurred_at=opportunity.created_at,
                data={"stage": "New", "status": "open"},
            )
        )
        if opportunity.status != "open":
            timeline.append(
                TimelineEntry(
                    lead_id=opportunity.lead_id,
                    opportunity=opportunity,
                    kind=f"opportunity.{opportunity.status}",
                    actor_id=opportunity.owner_id,
                    occurred_at=opportunity.closed_at,
                    data={"from_stage": "New", "to_stage": opportunity.stage.name},
                )
            )
        audit.append(
            AuditEvent(
                actor_type="user",
                actor_id=opportunity.created_by_id,
                action="opportunity.created",
                target_type="opportunity",
                target_id=str(opportunity.pk),
                occurred_at=opportunity.created_at,
                metadata={"stage": "new", "value": str(opportunity.value)},
            )
        )
    for activity in activities:
        timeline.append(
            TimelineEntry(
                lead_id=activity.lead_id,
                opportunity_id=activity.opportunity_id,
                activity=activity,
                kind=kinds[activity.type],
                actor_id=activity.created_by_id,
                occurred_at=activity.created_at,
                data={},
            )
        )
    audit.append(AuditEvent(actor_type="system", action="outbox.purged", metadata={"deleted": 12}))
    TimelineEntry.objects.bulk_create(timeline)
    AuditEvent.objects.bulk_create(audit)
    return {
        "admin": admin.pk,
        "rahul": rahul.pk,
        "sales": sales.pk,
        "service": service.pk,
        "spaces_only": leads[SPACES_ONLY].pk,
    }


def negotiating(owner_id, *, pipeline_id, count):
    """Open, unarchived deals of `owner_id` in the pipeline's negotiation stage."""
    return [
        pk
        for (pk,) in rows(
            "SELECT o.id FROM pipeline_opportunity AS o JOIN pipeline_stage AS s"
            " ON s.id = o.stage_id WHERE o.owner_id = %s AND o.pipeline_id = %s"
            " AND s.key = 'negotiation' AND o.archived_at IS NULL ORDER BY o.created_at",
            [owner_id, pipeline_id],
        )[:count]
    ]


# --- the tests ------------------------------------------------------------------------------
def test_the_pinned_release_is_a_state_the_migrations_can_be_in():
    """Each app at the pinned migration needs no later migration of any app."""
    graph = executor().loader.graph
    pinned = dict(RELEASE_CANDIDATE)
    assert set(pinned) == {app for app, _ in graph.leaf_nodes()}
    for state in (RELEASE_CANDIDATE, ENHANCEMENTS, ADR_0027, ADR_0028):
        needed = {node for target in state for node in graph.forwards_plan(target)}
        for app, name in state:
            assert max(n for a, n in needed if a == app) == name, (app, name)


def test_upgrading_from_the_release_keeps_every_record():
    try:
        _upgrade_and_check()
    finally:
        restore_latest()


def _upgrade_and_check():
    build(RELEASE_CANDIDATE)
    apps = state_apps(RELEASE_CANDIDATE)
    ids = seed_release(apps)
    released = model_columns(apps)
    before = checksums(released)
    backfill = {
        pk: (opportunity_date, account, customer)
        for pk, opportunity_date, account, customer in rows(
            RELEASED_BACKFILL, [settings.CRM_TIME_ZONE]
        )
    }
    created = dict(rows("SELECT id::text, created_at FROM pipeline_opportunity"))
    assert len(backfill) == 90

    # --- 0c31aee: the product enhancements -------------------------------------------------
    migrate(ENHANCEMENTS)
    assert checksums(released) == before  # nothing the release knew changed, not even updated_at
    after = {
        pk: (opportunity_date, account, customer)
        for pk, opportunity_date, account, customer in rows(
            "SELECT id::text, opportunity_date, account_name, customer_name"
            " FROM pipeline_opportunity"
        )
    }
    replaced = {pk for pk, (_, account, customer) in backfill.items() if "" in (account, customer)}
    assert replaced == {  # the spaces-only lead's deals: they used to abort it
        pk
        for (pk,) in rows(
            "SELECT id::text FROM pipeline_opportunity WHERE lead_id = %s", [ids["spaces_only"]]
        )
    }
    assert len(replaced) == 2  # that lead has two
    for pk, (opportunity_date, account, customer) in backfill.items():
        if pk in replaced:
            assert after[pk] == (opportunity_date, PLACEHOLDER, PLACEHOLDER)
        else:  # byte for byte what the released backfill wrote
            assert after[pk] == (opportunity_date, account, customer)
        assert opportunity_date == created[pk].astimezone(CRM_TZ).date()
    names = {account for _, account, _ in after.values()} | {c for *_, c in after.values()}
    assert "  Apollo Diagnostics  " in names  # padding kept
    assert "A" * 100 + " " + "B" * 99 in names  # cut to 200
    assert "C" * 200 in names
    assert "Śrī Sāī Diagnostics 🧪 Pvt. Ltd." in names
    assert "   " in names  # never aborted: left as they were
    assert chr(9) in names
    assert rows(
        "SELECT opportunity_date FROM pipeline_opportunity WHERE created_at = %s",
        [datetime(2026, 3, 31, 20, 0, tzinfo=UTC)],
    ) == [(date(2026, 4, 1),)]  # the business day in India, not UTC's
    assert rows(
        "SELECT DISTINCT contact_phone, contact_email, address, instrument_name, work_load,"
        " custom_fields::text, negotiated_price, negotiated_at FROM pipeline_opportunity"
    ) == [("", "", "", "", "", "{}", None, None)]
    # The seeded pipeline is the organisation's default; only its negotiation stage is one.
    assert rows(
        "SELECT key, name, owner_id, created_by_id, version, is_default FROM pipeline_pipeline"
        " ORDER BY key"
    ) == [
        ("sales", "Sales", None, None, 1, True),
        ("service", "Service Contracts", None, None, 1, False),
    ]
    assert rows(
        "SELECT p.key, s.key FROM pipeline_stage AS s JOIN pipeline_pipeline AS p"
        " ON p.id = s.pipeline_id WHERE s.is_negotiation"
    ) == [("sales", "negotiation")]
    for table in (
        "pipeline_negotiation_price",
        "pipeline_custom_field",
        "identity_support_session",
        "activities_attachment",
    ):
        assert rows(f"SELECT count(*) FROM {table}") == [(0,)], table  # noqa: S608
    assert rows(
        "SELECT DISTINCT password_change_required, password_changed_at FROM identity_user"
    ) == [(False, None)]
    assert rows("SELECT count(*) FROM activities_activity WHERE edited_at IS NOT NULL") == [(0,)]
    assert rows("SELECT count(*) FROM audit_event WHERE support_session_id IS NOT NULL") == [(0,)]

    # Negotiated price history as 0c31aee recorded it, for a deal already in negotiation.
    apps = state_apps(ENHANCEMENTS)
    NegotiationPrice = apps.get_model("pipeline", "NegotiationPrice")
    Opportunity = apps.get_model("pipeline", "Opportunity")
    deal = Opportunity.objects.get(
        pk=negotiating(ids["rahul"], pipeline_id=ids["sales"], count=1)[0]
    )
    when = timezone.now() - timedelta(days=2)
    for n, (price, source) in enumerate(
        [(Decimal("1234567.89"), "stage_entry"), (Decimal("1100000.50"), "revision")]
    ):
        NegotiationPrice.objects.create(
            opportunity=deal,
            price=price,
            currency="INR",
            stage=deal.stage,
            stage_name="Negotiation",
            source=source,
            opportunity_version=deal.version + n + 1,
            actor_id=ids["rahul"],
            occurred_at=when + timedelta(hours=n),
        )
    Opportunity.objects.filter(pk=deal.pk).update(
        negotiated_price=Decimal("1100000.50"),
        negotiated_at=when + timedelta(hours=1),
        version=deal.version + 2,
        updated_at=when + timedelta(hours=1),
    )
    enhanced = model_columns(apps)
    history = checksums(enhanced)

    # --- 5b05177 (ADR-0027), 989830b (ADR-0028), then the latest -------------------------
    migrate(ADR_0027)
    assert checksums(enhanced) == history
    migrate(ADR_0028)
    assert checksums(enhanced) == history
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE pipeline_opportunity SET expected_cpt = %s WHERE id = %s", [CPT, deal.pk]
        )
    expected = model_columns(state_apps(ADR_0028))
    history = checksums(expected)
    migrate(latest())
    assert checksums(expected) == history  # history, negotiated price, Expected CPT: unchanged
    assert checksums(released)["leads_lead"] == before["leads_lead"]
    assert rows(
        "SELECT price, agreed_cpt, source FROM pipeline_negotiation_price ORDER BY occurred_at"
    ) == [
        (Decimal("1234567.89"), "", "stage_entry"),  # recorded before the agreed CPT was asked
        (Decimal("1100000.50"), "", "revision"),
    ]
    upgraded = catalog()
    assert all(validated for *_, validated in upgraded["constraints"])
    assert all(valid for *_, valid in upgraded["indexes"])

    _work_on_the_upgraded_data(ids, deal.pk)
    _rollback_is_refused_and_changes_nothing()

    # --- the upgraded schema is the one a new installation gets ----------------------------
    build(latest())
    fresh = catalog()
    for name in CATALOG:
        difference = upgraded[name] ^ fresh[name]
        assert difference == ALLOWED_SCHEMA_DIFFERENCES.get(name, set()), (name, difference)
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def _work_on_the_upgraded_data(ids, priced_deal):
    """Deals that were in negotiation before the upgrade go on working through today's
    services; new history is recorded with exact decimals."""
    from arkray.activities import services as activities
    from arkray.identity.models import SupportSession, User
    from arkray.pipeline import services as pipeline
    from arkray.pipeline.models import NegotiationPrice, Opportunity, Stage

    rahul = User.objects.get(pk=ids["rahul"])
    own = AccessScope.own(rahul.pk)
    stage = {s.key: s for s in Stage.objects.filter(pipeline_id=ids["sales"])}
    unpriced = [
        pk for pk in negotiating(rahul.pk, pipeline_id=ids["sales"], count=4) if pk != priced_deal
    ]
    assert len(unpriced) >= 2
    leaving, staying = Opportunity.objects.filter(pk__in=unpriced[:2]).order_by("created_at")

    # A legacy negotiation deal (no price) moves on without one...
    moved = pipeline.move_opportunity(
        actor=rahul,
        scope=own,
        opportunity_id=leaving.pk,
        version=leaving.version,
        stage_id=stage["proposal"].pk,
    )
    assert (moved.stage_id, moved.version) == (stage["proposal"].pk, leaving.version + 1)
    # ... and another is edited, and gets its first agreed terms, then a revision.
    edited = pipeline.update_opportunity(
        actor=rahul,
        scope=own,
        opportunity_id=staying.pk,
        version=staying.version,
        changes={"expected_cpt": "Rs 20 per test"},
    )
    for price in (Decimal("1234567.89"), Decimal("1100000.50")):
        edited = pipeline.record_negotiated_price(
            actor=rahul,
            scope=own,
            opportunity_id=staying.pk,
            version=edited.version,
            price=price,
            agreed_cpt=CPT,
        )
    assert list(
        NegotiationPrice.objects.filter(opportunity_id=staying.pk)
        .order_by("id")
        .values_list("price", "agreed_cpt", "source")
    ) == [
        (Decimal("1234567.89"), CPT, "revision"),
        (Decimal("1100000.50"), CPT, "revision"),
    ]
    assert (edited.expected_cpt, edited.negotiated_price) == (
        "Rs 20 per test",
        Decimal("1100000.50"),
    )
    # Entering negotiation now asks for the terms.
    pipeline.move_opportunity(
        actor=rahul,
        scope=own,
        opportunity_id=moved.pk,
        version=moved.version,
        stage_id=stage["negotiation"].pk,
        negotiated_price=Decimal("1234567.89"),
        agreed_cpt=CPT,
    )
    note = activities.create_activity(
        actor=rahul,
        scope=own,
        activity_type="note",
        fields={"description": "Agreed ₹12,34,567.89 with the lab head."},
        opportunity_id=staying.pk,
    ).activity
    edited_note = activities.update_activity(
        actor=rahul,
        scope=own,
        activity_id=note.pk,
        version=note.version,
        changes={"description": "Agreed ₹11,00,000.50 with the lab head."},
    )
    assert edited_note.edited_at is not None
    SupportSession.objects.create(
        admin_id=ids["admin"],
        target=rahul,
        reason="Upgrade check",
        session_digest="ab" * 32,
        expires_at=timezone.now() + timedelta(minutes=30),
    )


def _rollback_is_refused_and_changes_nothing():
    from django.apps import apps

    everything = model_columns(apps)
    before = (applied(), catalog(), checksums(everything))
    assert rows("SELECT count(*) FROM pipeline_negotiation_price") == [(5,)]
    for target in ROLLBACKS:
        with pytest.raises(IrreversibleError, match=r"docs/deployment.md#rollback"):
            migrate([target])
        assert (applied(), catalog(), checksums(everything)) == before, target
    # Forwards again: nothing to do, and nothing in the way.
    assert executor().migration_plan(latest()) == []
    migrate(latest())


def test_an_empty_database_refuses_the_same_rollbacks():
    """The rule doesn't depend on whether anything was recorded yet."""
    try:
        before = (applied(), catalog())
        for target in ROLLBACKS:
            with pytest.raises(IrreversibleError, match="restore the database backup"):
                migrate([target])
            assert (applied(), catalog()) == before, target
    finally:
        restore_latest()


# --- a previous release running against the latest schema -----------------------------------
# Columns each release's code never names that are NOT NULL without a database default: its
# INSERTs into those tables fail on the latest schema. So the release candidate can't run beside
# (or after) this one, a stopped deploy is required across it, and the later releases can
# (docs/deployment.md#upgrading).
CANNOT_INSERT = {
    RELEASE_CANDIDATE: {
        "identity_user": {"password_change_required"},
        "pipeline_pipeline": {"version"},
        "pipeline_stage": {"is_negotiation"},
        "pipeline_opportunity": {
            "opportunity_date",
            "account_name",
            "customer_name",
            "contact_phone",
            "contact_email",
            "address",
            "instrument_name",
            "work_load",
            "custom_fields",
        },
    },
    ENHANCEMENTS: {},
    ADR_0027: {},
    ADR_0028: {},
}


def unnamed_required_columns(release):
    required = {}
    for table, known in model_columns(state_apps(release)).items():
        missing = {
            column
            for (column,) in rows(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'"
                " AND table_name = %s AND is_nullable = 'NO' AND column_default IS NULL"
                " AND is_generated = 'NEVER' AND is_identity = 'NO'",
                [table],
            )
        } - set(known)
        if missing:
            required[table] = missing
    return required


@pytest.mark.usefixtures("crm_configuration")
def test_what_each_previous_release_can_still_insert_into_the_latest_schema():
    from tests.factories import LeadFactory, UserFactory, default_stage

    for release, expected in CANNOT_INSERT.items():
        assert unnamed_required_columns(release) == expected, release

    # Their own INSERTs, through their own models.
    owner = UserFactory()
    lead = LeadFactory(owner=owner)
    stage = default_stage("negotiation")

    def insert_opportunity(release):
        return (
            state_apps(release)
            .get_model("pipeline", "Opportunity")
            .objects.create(
                title="Analyser",
                lead_id=lead.pk,
                owner_id=owner.pk,
                created_by_id=owner.pk,
                pipeline_id=stage.pipeline_id,
                stage_id=stage.pk,
                status="open",
                value=Decimal("100.00"),
                probability=stage.probability,
                **(
                    {}
                    if release is RELEASE_CANDIDATE
                    else {
                        "opportunity_date": date(2026, 10, 6),
                        "account_name": "Apollo",
                        "customer_name": "Apollo",
                    }
                ),
            )
        )

    with pytest.raises(IntegrityError, match="null value"), transaction.atomic():
        insert_opportunity(RELEASE_CANDIDATE)
    with pytest.raises(IntegrityError, match="password_change_required"), transaction.atomic():
        state_apps(RELEASE_CANDIDATE).get_model("identity", "User").objects.create(
            email="new@arkray.test", first_name="New", status="invited", password="!"
        )
    opportunity = insert_opportunity(ENHANCEMENTS)  # no Expected CPT: the database default
    assert rows(
        "SELECT expected_cpt FROM pipeline_opportunity WHERE id = %s", [opportunity.pk]
    ) == [("",)]
    state_apps(ADR_0028).get_model("pipeline", "NegotiationPrice").objects.create(
        opportunity_id=opportunity.pk,
        price=Decimal("1234567.89"),
        currency="INR",
        stage_id=stage.pk,
        stage_name=stage.name,
        source="stage_entry",
        opportunity_version=1,
        actor_id=owner.pk,
    )  # no agreed CPT: the database default
    assert rows("SELECT price, agreed_cpt FROM pipeline_negotiation_price") == [
        (Decimal("1234567.89"), "")
    ]
