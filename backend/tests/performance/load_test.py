# ruff: noqa: T201
# mypy: ignore-errors
# A diagnostics command (prints its report), not application code; random choices pick
# workload steps, not secrets.
"""Phase 10 load test: a controlled, mixed, closed-loop workload against the running stack
(gunicorn, real PostgreSQL, workers), over HTTP (docs/reliability.md#load-test).

    uv run python tests/performance/load_test.py --base http://127.0.0.1:8100 \\
        --password ... --users users.txt --admin admin@... --vus 8 --seconds 120 \\
        --metrics-token ...

Each virtual user signs in through the real API (CSRF, Argon2, session), then loops without
think time over a weighted mix of reads (dashboard, lists, details, timelines, the board,
search, routed and retrieval Ask) and writes (a note, a stage move, a lead edit). Reported:
per-request p50/p95/p99, errors, throughput; sampled every 5 s from /health/metrics: the
database connections in use and the outbox backlog. Ask Arkray's per-user rate limit answers
a user asking without pause with 429: counted as throttled (the designed outcome), not as an
error, and kept out of the latencies. The numbers describe this machine and this data, not
a universal capacity.

Use 127.0.0.1, not localhost: gunicorn's sync workers close the connection after each
response, and on Windows each new connection to "localhost" first tries IPv6 (::1), which
Docker Desktop doesn't forward, costing about 2 s a request in the client.
"""

from __future__ import annotations

import argparse
import heapq
import random
import statistics
import threading
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

import httpx

API = "/api/v1"
SEARCH_WORDS = ["follow", "quotation", "analyser", "Rahul", "Mumbai", "periwinkle", "price"]
QUESTIONS_ROUTED = [
    "What is my pipeline value?",
    "How many leads do I have?",
    "What are my overdue tasks?",
]
QUESTIONS_SEMANTIC = ["What concerns did customers raise about pricing?", "Who asked for a demo?"]


class VirtualUser:
    def __init__(self, base: str, email: str, password: str, organisation: bool) -> None:
        self.client = httpx.Client(
            base_url=base, timeout=30, headers={"Origin": base, "Referer": base + "/"}
        )
        self.workspace = "all" if organisation else "me"
        self.client.get(f"{API}/auth/csrf")
        # Every virtual user signs in from this one address, and sign-in is limited per
        # address (20 a minute): wait as told and try again, as a person would.
        for _ in range(5):
            response = self.client.post(
                f"{API}/auth/login",
                json={"email": email, "password": password},
                headers={"X-CSRFToken": self.csrf()},
            )
            if response.status_code != 429:
                break
            time.sleep(float(response.headers.get("Retry-After", "15")) + random.random())
        response.raise_for_status()
        self.leads: list[str] = []
        self.opportunities: list[tuple[str, int, str]] = []
        self.stages: list[str] = []
        self.cursor: str | None = None

    def csrf(self) -> str:
        for name in ("__Host-arkray_csrftoken", "arkray_csrftoken"):
            if name in self.client.cookies:
                return self.client.cookies[name]
        return ""

    def ws(self, path: str) -> str:
        return f"{API}/workspaces/{self.workspace}/{path}"

    def prime(self) -> None:
        page = self.client.get(self.ws("leads"), params={"page_size": 50}).json()
        self.leads = [row["id"] for row in page["results"]]
        if page.get("next"):
            self.cursor = parse_qs(urlsplit(page["next"]).query)["cursor"][0]
        board = self.client.get(self.ws("pipeline-board")).json()
        self.stages = [
            c["stage"]["id"] for c in board["columns"] if c["stage"].get("category") == "open"
        ]
        for column in board["columns"]:
            for card in column["cards"][:3]:
                self.opportunities.append((card["id"], card["version"], column["stage"]["id"]))

    # --- steps ------------------------------------------------------------------------------
    def dashboard(self):
        return self.client.get(self.ws("dashboard"))

    def lead_list(self):
        return self.client.get(self.ws("leads"), params={"page_size": 25})

    def lead_page_two(self):
        return self.client.get(self.ws("leads"), params={"page_size": 50, "cursor": self.cursor})

    def lead_search(self):
        return self.client.get(
            self.ws("leads"), params={"q": random.choice(SEARCH_WORDS), "page_size": 25}
        )

    def lead_detail(self):
        return self.client.get(self.ws(f"leads/{random.choice(self.leads)}"))

    def lead_timeline(self):
        return self.client.get(self.ws(f"leads/{random.choice(self.leads)}/timeline"))

    def board(self):
        return self.client.get(self.ws("pipeline-board"))

    def opportunity_list(self):
        return self.client.get(self.ws("opportunities"), params={"page_size": 25})

    def activities(self):
        return self.client.get(self.ws("activities"), params={"page_size": 25})

    def global_search(self):
        return self.client.get(self.ws("search"), params={"q": random.choice(SEARCH_WORDS)})

    def ask_routed(self):
        return self._post(self.ws("ask"), {"question": random.choice(QUESTIONS_ROUTED)})

    def ask_semantic(self):
        return self._post(self.ws("ask"), {"question": random.choice(QUESTIONS_SEMANTIC)})

    def add_note(self):
        return self._post(
            self.ws("activities"),
            {
                "type": "note",
                "lead": random.choice(self.leads),
                "description": "Load test note: called, discussed pricing.",
            },
        )

    def move(self):
        if not self.opportunities or len(self.stages) < 2:
            return self.dashboard()
        index = random.randrange(len(self.opportunities))
        opportunity, version, stage = self.opportunities[index]
        target = random.choice([s for s in self.stages if s != stage])
        response = self._post(
            self.ws(f"opportunities/{opportunity}/move"), {"stage": target, "version": version}
        )
        if response.status_code == 200:
            self.opportunities[index] = (opportunity, response.json()["version"], target)
        elif response.status_code == 409:  # someone else moved it: take the current version
            current = self.client.get(self.ws(f"opportunities/{opportunity}")).json()
            self.opportunities[index] = (opportunity, current["version"], current["stage"]["id"])
            response.status_code = 299  # an expected optimistic-concurrency outcome
        return response

    def edit_lead(self):
        lead = random.choice(self.leads)
        current = self.client.get(self.ws(f"leads/{lead}"))
        if current.status_code != 200:
            return current
        response = self.client.patch(
            self.ws(f"leads/{lead}"),
            json={
                "version": current.json()["version"],
                "city": random.choice(["Pune", "Mumbai", "Delhi"]),
            },
            headers={"X-CSRFToken": self.csrf()},
        )
        if response.status_code == 409:
            response.status_code = 299
        return response

    def _post(self, path: str, body: dict) -> httpx.Response:
        return self.client.post(path, json=body, headers={"X-CSRFToken": self.csrf()})


MIX = [
    ("dashboard", 15),
    ("lead_list", 14),
    ("lead_page_two", 5),
    ("lead_search", 5),
    ("lead_detail", 10),
    ("lead_timeline", 5),
    ("board", 9),
    ("opportunity_list", 5),
    ("activities", 9),
    ("global_search", 8),
    ("ask_routed", 3),
    ("ask_semantic", 2),
    ("add_note", 5),
    ("move", 3),
    ("edit_lead", 2),
]


def percentile(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def run(args) -> None:
    with open(args.users, encoding="utf-8") as listing:
        emails = [line.strip() for line in listing if line.strip()]
    stop = threading.Event()
    samples: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, list[int]] = defaultdict(list)
    throttled: dict[str, int] = defaultdict(int)
    slowest: list[tuple[float, str, str]] = []
    lock = threading.Lock()
    names = [name for name, weight in MIX for _ in range(weight)]

    ready = threading.Semaphore(0)
    signed_in: list[int] = []

    def worker(index: int) -> None:
        organisation = index % 6 == 5 and args.admin
        email = args.admin if organisation else emails[index % len(emails)]
        try:
            user = VirtualUser(args.base, email, args.password, organisation=bool(organisation))
            user.prime()
            signed_in.append(index)
        finally:
            ready.release()
        while not stop.is_set():
            name = random.choice(names)
            started = time.perf_counter()
            target = ""
            try:
                response = getattr(user, name)()
                status = response.status_code
                target = f"{user.workspace} {response.request.url.query.decode()[:60]}"
            except httpx.HTTPError:
                status = 0
            except (KeyError, ValueError, IndexError):  # an error, not a dead thread
                status = -1
            elapsed = (time.perf_counter() - started) * 1000
            with lock:
                if status == 429:
                    throttled[name] += 1
                    continue
                samples[name].append(elapsed)
                heapq.heappush(slowest, (elapsed, name, target))
                if len(slowest) > 8:
                    heapq.heappop(slowest)
                if status <= 0 or status >= 400:
                    errors[name].append(status)

    gauges: list[str] = []

    def monitor() -> None:
        while not stop.is_set():
            try:
                text = httpx.get(
                    f"{args.base}/health/metrics",
                    headers={"Authorization": f"Bearer {args.metrics_token}"},
                    timeout=5,
                ).text
                connections = sum(
                    float(line.split()[-1])
                    for line in text.splitlines()
                    if line.startswith("arkray_db_connections{") and 'state="idle"' not in line
                )
                total = sum(
                    float(line.split()[-1])
                    for line in text.splitlines()
                    if line.startswith("arkray_db_connections{")
                )
                backlog = sum(
                    float(line.split()[-1])
                    for line in text.splitlines()
                    if line.startswith("arkray_outbox_events{") and 'status="pending"' in line
                )
                gauges.append(
                    f"busy {connections:.0f} / open {total:.0f} connections,"
                    f" outbox pending {backlog:.0f}"
                )
            except httpx.HTTPError:
                gauges.append("metrics unavailable")
            stop.wait(5)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.vus)]
    for thread in threads:
        thread.start()
    for _ in threads:  # everyone signed in (or failed to) before the warm-up starts
        ready.acquire(timeout=300)
    print(f"{len(signed_in)} of {args.vus} virtual users signed in")
    time.sleep(args.warmup)
    with lock:
        samples.clear()
        errors.clear()
        throttled.clear()
        slowest.clear()
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    started = time.perf_counter()
    time.sleep(args.seconds)
    stop.set()
    elapsed = time.perf_counter() - started
    for thread in threads:
        thread.join(timeout=60)

    total = sum(len(v) for v in samples.values())
    failed = sum(len(v) for v in errors.values())
    print(f"\n{args.vus} virtual users, {args.seconds} s measured after {args.warmup} s warm-up")
    rate = 100 * failed / max(total, 1)
    print(f"{total} requests, {total / elapsed:.1f} req/s, {failed} errors ({rate:.2f} %)")
    print(f"throttled by design (429): {dict(throttled)}")
    print(f"{'request':18} {'n':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}   errors")
    for name, _ in MIX:
        values = sorted(samples.get(name, []))
        if not values:
            continue
        print(
            f"{name:18} {len(values):6d} {statistics.median(values):8.1f}"
            f" {percentile(values, 0.95):8.1f} {percentile(values, 0.99):8.1f}"
            f" {values[-1]:8.1f}   {sorted(set(errors.get(name, [])))}"
        )
    print("\nslowest:")
    for elapsed_ms, name, target in sorted(slowest, reverse=True):
        print(f"  {elapsed_ms:8.1f} ms  {name:16} {target}")
    print("\nsampled every 5 s:")
    for line in gauges:
        print("  " + line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--users", required=True, help="file: one salesperson email per line")
    parser.add_argument(
        "--admin", default="", help="an admin email (every 6th user, organisation-wide)"
    )
    parser.add_argument("--password", required=True)
    parser.add_argument("--vus", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--metrics-token", default="")
    run(parser.parse_args())
