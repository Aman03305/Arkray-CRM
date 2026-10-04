# ruff: noqa: T201, S608, SIM905
# A diagnostics command (prints its report), not application code; the SQL it builds only
# interpolates its own constants, never input.
# mypy: ignore-errors
"""Global search benchmark (docs/search.md#performance): EXPLAIN ANALYZE of the exact SQL
global search runs, and the whole `search.selectors.global_search()` call, at growth scale.

Not a test (pytest doesn't collect it). Run against the search benchmark database:

    docker exec arkray-postgres-1 psql -U arkray -d postgres \
        -c "CREATE DATABASE arkray_bench_search TEMPLATE arkray_bench_dash"
    DATABASE_URL=postgres://arkray:...@127.0.0.1:55432/arkray_bench_search \
        DJANGO_SETTINGS_MODULE=config.settings.test \
        uv run python tests/performance/bench_search.py --seed-text     # once, ~10 min
    uv run python manage.py migrate                                       # Phase 7 indexes
    uv run python tests/performance/bench_search.py [--verbose]

arkray_bench_dash (tests/performance/bench_dashboard.py) holds 501 users, 1,000,000 leads,
300,000 opportunities and 2,000,000 activities, but its text is synthetic ("Deal 57340",
"Follow up 343833", one sentence repeated in every note), which makes every word either
unique or universal: useless for measuring search. --seed-text rewrites, in place, the
columns search reads with a skewed vocabulary (frequent first names and products, rare
ones; 5 % Devanagari and 2 % accented names; 40 % of leads with a phone number), task and
meeting titles and meeting locations from templates, and note bodies of 1-8 sentences
(5 % long notes of 2-5 kB, 3 % with a Hindi sentence), then plants rare words at known
frequencies and VACUUM FULL ANALYZEs. It refuses to run on a database whose name doesn't
contain "bench_search".

The measurement covers the heaviest owner, a typical owner, a selected user (an admin's
USER scope) and the organisation (an admin's "all"): common, rare, prefix, no-match,
high-frequency, phone, two-character, Devanagari and multi-word queries.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django

django.setup()

from django.db import connection  # noqa: E402
from django.db.models import Count  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from arkray.core.access import AccessScope  # noqa: E402
from arkray.core.ranking import SearchQuery  # noqa: E402
from arkray.identity.models import Role, User  # noqa: E402
from arkray.leads.models import Lead  # noqa: E402

EXECUTION_TIME = re.compile(r"Execution Time: ([0-9.]+) ms")
INDEX_SCAN = re.compile(r"(?:Index|Index Only) Scan(?: Backward)? using (\w+)")
BITMAP_SCAN = re.compile(r"Bitmap Index Scan on (\w+)")
SEQ_SCAN = re.compile(r"Seq Scan on (\w+)")


def _array(values: list[str]) -> str:
    return "ARRAY[" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + "]::text[]"


FIRSTS = (
    "Rahul Priya Amit Anita Sanjay Deepak Pooja Neha Vikram Suresh Ramesh Sunita Kavita Rajesh "
    "Manoj Arjun Divya Meera Karthik Lakshmi Ravi Anil Sneha Rohit Swati Vijay Asha Nikhil "
    "Shreya Gaurav Ankit Ritu Harish Prakash Geeta Mohan Arun Varun Nisha Sachin Kiran "
    "Mahesh Pallavi Dinesh Rekha Tarun Jyoti Sameer Farhan Imran Ayesha Zoya Joseph Mary "
    "Thomas George Anand Bhavna Chetan Esha Gopal Hema Irfan Jatin Komal Lalit Madhuri "
    "Naveen Om Pankaj Qasim Rashmi Sagar Tanvi Uday Vandana Waseem Yash Zubin David Sarah "
    "Michael Emma Daniel Olivia"
).split()
LASTS = (
    "Sharma Patel Singh Kumar Gupta Rao Reddy Iyer Nair Mehta Shah Joshi Desai Verma Mishra "
    "Pillai Menon Chopra Kapoor Malhotra Bose Das Banerjee Mukherjee Chatterjee Agarwal "
    "Jain Saxena Srivastava Pandey Tiwari Yadav Naidu Kulkarni Deshpande Patil Pawar "
    "Shetty Hegde Kamath Fernandes D'Souza Khan Ahmed Qureshi Siddiqui Thomas Mathew "
    "Varghese Chauhan Rathore Bhat Kaul Dutta Ghosh Sen Roy Smith Brown Wilson Taylor"
).split()
FIRSTS_HI = "राहुल प्रिया अमित अनीता संजय दीपक पूजा नेहा विक्रम सुरेश".split()
LASTS_HI = "शर्मा पटेल सिंह कुमार गुप्ता राव वर्मा मिश्रा जोशी यादव".split()
ACCENTED = "José Zoë Renée François Müller Ångström Chloé Łukasz Søren Inês".split()
ORG_WORDS = (
    "Apollo Sunrise Lotus Fortis Medicare Lifeline Care City Global Metro Prime Unity "
    "Hope Shanti Sanjeevani Arogya Nova Pioneer Crescent Galaxy Horizon Royal Silver "
    "Golden Green Blue Star Sai Shree Om Ganesh Lakshmi Krishna Vision Trust Wellness "
    "Precision Accurate Healthway Medilink Pathcare Dr.Lal Thyrocare Metropolis Vijaya"
).split()
ORG_SUFFIXES = (
    "Diagnostics|Hospital|Clinic|Labs|Healthcare|Pathology|Medical Centre|Nursing Home|"
    "Pharma|Imaging|Diabetes Centre|Polyclinic|Path Lab|Multispeciality Hospital"
).split("|")
CITIES = (
    "Mumbai|Delhi|Bengaluru|Chennai|Kolkata|Hyderabad|Pune|Ahmedabad|Jaipur|Lucknow|"
    "Kochi|Indore|Nagpur|Surat|Bhopal|Chandigarh|Coimbatore|Vizag|Patna|Mysuru|Thane|"
    "Nashik|Vadodara|Ranchi|Guwahati|Dehradun|Madurai|Trichy|Mangaluru|Udaipur"
).split("|")
DOMAINS = "gmail.com yahoo.co.in outlook.com rediffmail.com hospital.example lab.example".split()
PRODUCTS = (
    "glucose meter|HbA1c analyser|urine analyser|haematology analyser|biochemistry analyser|"
    "reagent kit|test strips|lancets|control solution|diagnostic equipment|"
    "point-of-care analyser|lab automation line|electrolyte analyser|immunoassay system"
).split("|")
DEAL_TYPES = (
    "supply|annual contract|AMC renewal|upgrade|tender|pilot|bulk order|replacement|"
    "demo unit|service contract|rate contract|expansion"
).split("|")
TASKS = [
    "Follow up with {first}",
    "Send quotation to {org}",
    "Call {first} about the {product}",
    "Prepare proposal for {org}",
    "Schedule demo of the {product}",
    "Share brochure with {first}",
    "Collect payment from {org}",
    "Confirm order quantity",
    "Follow up on tender",
    "Update pricing sheet",
    "Arrange site visit in {city}",
    "Email product catalogue",
    "Check stock of {product}",
    "Remind {first} about renewal",
    "Follow up for quotation",
    "Send revised quotation",
    "Get purchase order from {org}",
    "Book installation engineer",
]
MEETINGS = [
    "Product discussion",
    "{product} demo",
    "Quarterly review with {org}",
    "Morning meeting with {first}",
    "Pricing negotiation",
    "Installation planning",
    "Training session on the {product}",
    "Contract renewal meeting",
    "Site visit",
    "Introductory call with {first}",
    "Tender pre-bid meeting",
    "Service review",
]
LOCATIONS = [
    "{city}",
    "{org}, {city}",
    "Online",
    "Head office",
    "Hospital lab, {city}",
    "",
    "",
]
SENTENCES = [
    "Spoke with {first} about the {product}.",
    "They prefer morning calls.",
    "Budget approval expected next month.",
    "Asked for a revised quotation with a volume discount.",
    "{first} mentioned that the current supplier is slow to deliver.",
    "The lab processes a few hundred samples a day.",
    "Decision maker is the purchase manager at {org}.",
    "Follow up after the tender results are announced.",
    "Interested in a demo of the {product} in {city}.",
    "Payment terms of 60 days requested.",
    "Competitor offered a lower price on test strips.",
    "Needs installation before the end of the quarter.",
    "Call back on Monday morning.",
    "Shared the brochure and the price list over email.",
    "Concerned about service response time in {city}.",
    "The existing {product} is more than seven years old.",
    "{first} will check the budget with the finance team.",
    "Requested references from other hospitals in {city}.",
    "Discussed reagent rental instead of an outright purchase.",
    "Wants the annual maintenance contract bundled in the price.",
    "Met at the diagnostics expo; good rapport.",
    "No decision until the new wing opens.",
    "Price is the main objection, quality is not.",
    "The pathologist asked about calibration frequency.",
]
HINDI_SENTENCES = [
    "राहुल जी से बात हुई, अगले हफ्ते डेमो चाहिए।",
    "कोटेशन ईमेल पर भेज दिया है।",
    "भुगतान अगले महीने होगा।",
]
# Planted rare words: (word, how many notes contain it). The two-letter one can't use a
# trigram index at all.
PLANTED = [("xylograph", 10), ("zebrafish", 1_000), ("periwinkle", 20_000), ("Qz", 5)]
# Words common long ago and absent recently (R59: the older pass reads every old match): in
# the oldest notes only. The rest of the text is uniform over time, so without these the
# shape never appears (performance review, P2-3).
LEGACY = [("legacymany", 190_000), ("legacyfifty", 50_000)]


def _pick(array: str, skew: float = 2.0) -> str:
    """A random element, skewed towards the start of the array (a few frequent values)."""
    return f"({array})[1 + floor(power(random(), {skew}) * array_length({array}, 1))::int]"


def _fill(template_sql: str) -> str:
    """Replace {first}/{org}/{product}/{city} in an SQL text expression (one pick per row)."""
    org = f"{_pick(_array(ORG_WORDS))} || ' ' || {_pick(_array(ORG_SUFFIXES), 1.5)}"
    for placeholder, value in (
        ("{first}", _pick(_array(FIRSTS))),
        ("{org}", org),
        ("{product}", _pick(_array(PRODUCTS))),
        ("{city}", _pick(_array(CITIES))),
    ):
        template_sql = f"replace({template_sql}, '{placeholder}', {value})"
    return template_sql


STEPS = ("leads", "opportunities", "tasks", "meetings", "notes", "planted", "legacy", "vacuum")


def seed_text(steps: tuple[str, ...] = STEPS) -> None:
    name = connection.settings_dict["NAME"]
    if "bench_search" not in name:
        raise SystemExit(f"--seed-text rewrites data: refusing to run on {name!r}")
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0")
        cursor.execute("SET max_parallel_maintenance_workers = 0", [])
        first, last = _pick(_array(FIRSTS)), _pick(_array(LASTS))
        org = f"{_pick(_array(ORG_WORDS))} || ' ' || {_pick(_array(ORG_SUFFIXES), 1.5)}"
        if "leads" in steps:
            _leads(cursor, first, last, org)
        if "opportunities" in steps:
            _opportunities(cursor, org)
        if "tasks" in steps:
            print("tasks ...", flush=True)
            task_title = _fill(_pick(_array(TASKS), 1.5))
            cursor.execute(
                f"UPDATE activities_activity SET title = {task_title} WHERE type = 'task'"
            )
        if "meetings" in steps:
            print("meetings ...", flush=True)
            meeting_title = _fill(_pick(_array(MEETINGS), 1.5))
            location = _fill(_pick(_array(LOCATIONS), 1))
            cursor.execute(
                f"UPDATE activities_activity SET title = {meeting_title}, location = {location}"
                " WHERE type = 'meeting'"
            )
        if "notes" in steps:
            _notes(cursor)
        if "planted" in steps:
            _plant(cursor)
        if "legacy" in steps:
            _plant_legacy(cursor)
    if "vacuum" in steps:
        print("VACUUM FULL ANALYZE ...", flush=True)
        with connection.cursor() as cursor:
            for table in ("leads_lead", "pipeline_opportunity", "activities_activity"):
                cursor.execute(f"VACUUM (FULL, ANALYZE) {table}")
    print(f"seeded {', '.join(steps)} in {time.perf_counter() - started:.0f} s")


def _leads(cursor, first: str, last: str, org: str) -> None:
    print("leads ...", flush=True)
    cursor.execute(
        f"""
            UPDATE leads_lead SET
                first_name = CASE
                    WHEN r < 0.05 THEN {_pick(_array(FIRSTS_HI), 1)}
                    WHEN r < 0.07 THEN {_pick(_array(ACCENTED), 1)}
                    WHEN r < 0.12 THEN ''
                    ELSE {first} END,
                last_name = CASE
                    WHEN r < 0.05 THEN {_pick(_array(LASTS_HI), 1)}
                    WHEN r < 0.12 THEN ''
                    ELSE {last} END,
                organization_name = CASE WHEN r2 < 0.8 OR r BETWEEN 0.07 AND 0.12
                    THEN {org} ELSE '' END,
                email = CASE WHEN r2 < 0.7
                    THEN 'contact' || substr(md5(id::text), 1, 8) || '@'
                        || {_pick(_array(DOMAINS), 1)}
                    ELSE '' END,
                city = {_pick(_array(CITIES))},
                phone = CASE WHEN r3 < 0.4 THEN '+91 ' || p ELSE '' END,
                phone_keys = CASE WHEN r3 < 0.4 THEN ARRAY['+91' || p] ELSE '{{}}' END
            FROM (
                SELECT id AS lead_id, random() AS r, random() AS r2, random() AS r3,
                    (7000000000 + floor(random() * 2999999999))::bigint::text AS p
                FROM leads_lead
            ) x
            WHERE leads_lead.id = x.lead_id
            """
    )
    # Every lead has a name or an organisation (leads_lead_name_present).


def _opportunities(cursor, org: str) -> None:
    print("opportunities ...", flush=True)
    cursor.execute(
        f"""
            UPDATE pipeline_opportunity SET title =
                {_pick(_array([p[0].upper() + p[1:] for p in PRODUCTS]))} || ' '
                || {_pick(_array(DEAL_TYPES), 1.5)}
                || CASE WHEN random() < 0.3 THEN ' for ' || {org} ELSE '' END
            """
    )


def _notes(cursor) -> None:
    print("notes ...", flush=True)
    sentence = _fill(_pick(_array(SENTENCES), 1.3))
    # The sentence count is computed from the row's id: generate_series' arguments must
    # refer to the row, or PostgreSQL computes them once and rewinds the same series for
    # every note (a first version made every note the same length). 5 % are long notes of
    # 20-59 sentences (2-5 kB), the rest 1-8 sentences.
    count = """CASE WHEN abs(hashtext(a.id::text)) % 100 < 5
                    THEN 20 + abs(hashtext(a.id::text || 'n')) % 40
                    ELSE 1 + abs(hashtext(a.id::text || 'm')) % 8 END"""
    body = f"""array_to_string(ARRAY(
        SELECT {sentence} FROM generate_series(1, {count}) AS g), ' ')
        || CASE WHEN random() < 0.03
                THEN ' ' || {_pick(_array(HINDI_SENTENCES), 1)} ELSE '' END"""
    cursor.execute(f"UPDATE activities_activity a SET description = {body} WHERE type = 'note'")


def _plant(cursor) -> None:
    print("planting rare words ...", flush=True)
    for word, notes in PLANTED:
        cursor.execute(
            """
                UPDATE activities_activity SET description = description || ' ' || %s || '.'
                WHERE id IN (SELECT id FROM activities_activity WHERE type = 'note'
                             ORDER BY md5(id::text || %s) LIMIT %s)
                """,
            [word, word, notes],
        )


def _plant_legacy(cursor) -> None:
    print("planting old words ...", flush=True)
    for word, notes in LEGACY:
        cursor.execute(
            """
                UPDATE activities_activity SET description = description || ' ' || %s || '.'
                WHERE id IN (SELECT id FROM activities_activity WHERE type = 'note'
                             ORDER BY created_at, id LIMIT %s)
                """,
            [word, notes],
        )


# --- measurement --------------------------------------------------------------------------------
QUERIES = [
    ("common word", "follow"),
    ("high-frequency word", "the"),
    ("frequent name", "Rahul"),
    ("prefix", "Rah"),
    ("two words", "follow quotation"),
    ("rare word", "xylograph"),
    ("medium word", "zebrafish"),
    ("frequent planted word", "periwinkle"),
    ("no match", "qqxzvbn"),
    ("two letters beside a word", "Qz quotation"),
    ("three words, one rare", "follow quotation xylograph"),
    ("three-letter rare", "Zoë"),
    ("phone digits", "98765"),
    ("city", "Mumbai"),
    ("Devanagari", "राहुल"),
    ("accented", "Renée"),
    ("email fragment", "contact1a"),
    ("five words", "spoke with about the analyser"),
    ("Devanagari conjunct", "शर्मा"),  # a virama splits it for pg_trgm
    ("letters PostgreSQL can't index", chr(0x11F04) * 3),  # Kawi: the gate stops the older pass
    ("Latin + Indic mark", "on" + chr(0x093C)),  # pg_trgm sees only "on": the gate stops it
    # Rare words whose trigrams are common (P2-3): the older pass rechecks every candidate.
    ("common trigrams", "station"),
    ("common trigrams, no match", "ationthe"),
    ("word and punctuation", "the-"),  # "the" and "words ending in he"
    # Common long ago, absent recently (R59; --seed-text step "legacy").
    ("old-common, recent-rare", "legacymany"),
    ("old, 50,000 matches", "legacyfifty"),
]


def explain(sql: str, params, runs: int = 3) -> tuple[float, set[str], str]:
    best, plan = float("inf"), ""
    with connection.cursor() as cursor:
        for _ in range(runs):
            cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}", params)
            text = "\n".join(row[0] for row in cursor.fetchall())
            elapsed = float(EXECUTION_TIME.search(text).group(1))
            if elapsed < best:
                best, plan = elapsed, text
    access = set(INDEX_SCAN.findall(plan)) | set(BITMAP_SCAN.findall(plan))
    access |= {f"SEQ {t}" for t in SEQ_SCAN.findall(plan)}
    return best, access, plan


def label(sql: str) -> str:
    """Which group a captured statement belongs to (by its table and type condition)."""
    table = sql.split(" FROM ", 1)[1].split()[0]
    if table == '"activities_activity"':
        for kind in ("task", "meeting", "note"):
            if f"\"type\" = '{kind}'" in sql:
                return kind + "s"
    return {'"leads_lead"': "leads", '"pipeline_opportunity"': "opportunities"}.get(table, "?")


def measure(
    who: str, scope: AccessScope, *, verbose: bool, runs: int = 3, plans: bool = True
) -> None:
    from arkray.search import selectors

    print(f"\n=== {who} ===")
    for name, q in QUERIES:
        query = SearchQuery.parse(q)
        with CaptureQueriesContext(connection) as captured:
            selectors.global_search(scope, query)
        statements = [c["sql"] for c in captured.captured_queries if c["sql"].startswith("SELECT")]
        timings = []
        for _ in range(runs):
            started = time.perf_counter()
            selectors.global_search(scope, query)
            timings.append((time.perf_counter() - started) * 1000)
        parts = []
        for sql in statements if plans else []:
            elapsed, access, plan = explain(sql, None, runs)
            parts.append(f"{label(sql)} {elapsed:.1f}ms [{', '.join(sorted(access))}]")
            if verbose:
                print(plan)
        print(
            f"{name:22} {q!r:34} total {min(timings):7.1f} ms (median "
            f"{statistics.median(timings):.1f}) | " + " | ".join(parts)
        )


TRIGRAM = {
    "leads": "leads_search_trgm",
    "opportunities": "pipeline_opp_search_trgm",
    "tasks": "activities_task_search_trgm",
    "meetings": "activities_meeting_search_trgm",
    "notes": "activities_note_search_trgm",
}


def check_plans(scopes: dict[str, AccessScope]) -> bool:
    """At production size: wherever the older pass runs, it reads the kind's trigram index
    (or, in one user's workspace, an index of that user's records), and in one user's
    workspace looks the activity trigrams up with the owner (P2-2: never every owner's
    candidates, rechecked); nothing is read sequentially; and the recent pass never sorts.
    Returns True if every check passed."""
    from arkray.search import selectors

    ok = True
    for who, scope in scopes.items():
        for _, q in QUERIES:
            with CaptureQueriesContext(connection) as captured:
                selectors.global_search(scope, SearchQuery.parse(q))
            for sql in (
                c["sql"] for c in captured.captured_queries if c["sql"].startswith("SELECT")
            ):
                group = label(sql)
                _, _, plan = explain(sql, None, 1)
                older = plan[plan.index("CTE search_older") :]
                recent = plan[plan.index("CTE search_recent") : plan.index("CTE search_older")]
                problems = []
                if "Seq Scan" in plan:
                    problems.append("sequential scan")
                # A sort is fine over one user's records read through their own index (a
                # user with fewer than RECENT records has them all read and sorted); never
                # over the organisation's.
                # (The words' fragments are sorted too, for the candidate count: not records.)
                sorts_records = any(
                    "substr(" not in key for key in re.findall(r"Sort Key: (.*)", recent)
                )
                if sorts_records and (scope.kind == "organization" or "owner_id" not in recent):
                    problems.append("recent pass sorts")
                heap = next((x for x in older.splitlines() if "Bitmap Heap Scan" in x), "")
                if heap and "never executed" not in heap:
                    used = set(BITMAP_SCAN.findall(older))
                    if not used or not all(
                        name == TRIGRAM[group]
                        or (scope.kind != "organization" and "_owner_" in name)
                        for name in used
                    ):
                        problems.append(f"older pass reads {sorted(used)}")
                    lookups = re.findall(rf"Bitmap Index Scan on {TRIGRAM[group]}.*\n.*", older)
                    if (
                        scope.kind != "organization"
                        and group not in ("leads", "opportunities")  # trigrams only
                        and any("owner_id = " not in lookup for lookup in lookups)
                    ):
                        problems.append("trigram lookup without the owner")
                if problems:
                    ok = False
                    print(f"FAIL {who:28} {q!r:34} {group:13} {'; '.join(problems)}")
    print("plan checks:", "PASS" if ok else "FAIL")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-text", action="store_true")
    parser.add_argument("--steps", default=",".join(STEPS), help="with --seed-text")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--runs", type=int, default=3, help="best of N (timings and plans)")
    parser.add_argument("--only", default="", help="comma-separated query names or words")
    parser.add_argument("--recent", type=int, help="override core.ranking.RECENT")
    parser.add_argument("--no-explain", action="store_true", help="wall-clock timings only")
    parser.add_argument(
        "--check-plans", action="store_true", help="check index use, exit 1 on failure"
    )
    args = parser.parse_args()
    if args.seed_text:
        seed_text(tuple(args.steps.split(",")))
        return
    with connection.cursor() as cursor:
        cursor.execute("SET max_parallel_workers_per_gather = 0")
    owners = list(
        Lead.objects.values("owner_id")
        .annotate(n=Count("id"))
        .order_by("-n")
        .values_list("owner_id", "n")
    )
    heavy, heavy_n = owners[0]
    typical, typical_n = owners[len(owners) // 2]
    admin = User.objects.filter(role=Role.ADMIN).first() or User.objects.first()
    print(f"heavy owner: {heavy_n} leads; typical owner: {typical_n} leads")
    if args.recent:
        from arkray.core import ranking

        ranking.RECENT = args.recent
        print(f"RECENT = {ranking.RECENT}")
    if args.only:
        wanted = set(args.only.split(","))
        QUERIES[:] = [(n, q) for n, q in QUERIES if n in wanted or q in wanted]
    if args.check_plans:
        scopes = {
            "heavy owner (SELF)": AccessScope.own(heavy),
            "typical owner (SELF)": AccessScope.own(typical),
            "selected heavy user (USER)": AccessScope.for_user(admin.pk, heavy),
            "organisation (ALL)": AccessScope.organization(admin.pk),
        }
        raise SystemExit(0 if check_plans(scopes) else 1)
    options = {"verbose": args.verbose, "runs": args.runs, "plans": not args.no_explain}
    measure("heavy owner (SELF)", AccessScope.own(heavy), **options)
    measure("typical owner (SELF)", AccessScope.own(typical), **options)
    measure("admin, selected heavy user (USER)", AccessScope.for_user(admin.pk, heavy), **options)
    measure("admin, organisation (ALL)", AccessScope.organization(admin.pk), **options)


if __name__ == "__main__":
    main()
