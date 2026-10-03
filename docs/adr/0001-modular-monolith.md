# 0001. Modular monolith on Django + Django REST Framework

Status: Accepted
Date: 2026-09-30

## Context
Arkray CRM is a new product for a single organisation with a small team. It needs strong
consistency (financial figures, audit trails), fast delivery, and code future engineers can
understand. The requirements explicitly ask for a modular monolith and proven technology.

## Decision
- One Django 5.2 LTS project with DRF, organised as modules (`core`, `audit`, `identity`,
  `leads`, `pipeline`, `activities`, `dashboard`, `search`, `ai`) under `backend/arkray/`.
- Strict layering enforced by `import-linter`: a module imports only modules below it;
  `core` imports none. Cross-module calls go through the target's `selectors`/`services`.
  Upward communication uses outbox topics.
- Inside a module: `models` (persistence), `selectors` (reads), `services` (writes and
  business rules), `api` (thin HTTP adapters), `handlers` (outbox).
- Python 3.12, dependencies locked with uv.

## Consequences
- One deploy, one database, one transaction boundary: consistency is simple.
- A module can later be extracted behind its services/selectors interface if a measured need
  appears (for example, the `ai` module's workers).
- Boundaries are machine-checked (the check caught a real violation in Phase 0), not just
  documented.

## Alternatives considered
- **Microservices**: distributed transactions, network failure modes and operational cost with
  no scaling need to justify them.
- **FastAPI / Node backend**: fewer batteries (auth, sessions, migrations, a mature ORM);
  Django's maturity is the point.
- **A separate `users` module**: it would split ownership of the User table between two
  modules; folded into `identity`.
