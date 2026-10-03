# 0011. Single-tenant deployment

Status: Accepted
Date: 2026-09-30

## Context
Arkray CRM serves one organisation; the admin manages that organisation's users. There is no
requirement to host multiple independent companies.

## Decision
One deployment serves one organisation. There are no `tenant_id` columns. Organisation-level
settings (time zone, currency, AI enablement) are environment configuration in v1.

## Consequences
- Simpler schema, queries and authorization.
- Serving another organisation means another deployment, isolated by construction.
- Converting to multi-tenant later would add a tenant key to the scope. Because all data access
  already flows through `AccessScope`, that is where it would be enforced.

## Alternatives considered
- Multi-tenant from day one: tenant columns on every table and in every query, for a
  requirement that doesn't exist.
