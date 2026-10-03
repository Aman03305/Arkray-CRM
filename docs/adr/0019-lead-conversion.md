# 0019. Lead conversion creates an opportunity; "Converted" requires one

Status: Accepted
Date: 2026-09-30

## Context
Phase 2 shipped a "Converted" lead status with no side effects, deliberately: there was
nothing to convert into. Arkray CRM has no Company, Account, Contact or Product entity, so
the classic "convert to account + contact + opportunity" does not apply. Phase 3 adds
opportunities and must give the status a meaning without hidden magic, partial states or
duplicate opportunities from double clicks.

## Decision
- **"Converted" means the lead has entered the opportunity process: it has at least one
  opportunity** (archived and closed ones count: conversion is a historical fact).
- An explicit **Convert** operation (`POST …/leads/{id}/convert`, in the pipeline module)
  creates the opportunity and sets the lead's status to the first active *converted*
  status in **one transaction**, locking the lead `FOR UPDATE` first. Converting an
  already converted lead is refused; the lead's version is required; an `Idempotency-Key`
  makes retries replay the first conversion.
- The generic status change into a converted status, and creating a lead as Converted,
  are **refused unless the lead already has an opportunity**: the pipeline module vetoes
  them from its `LeadStatusChanged` / `LeadCreated` subscribers, so `leads` never imports
  `pipeline`.
- Leaving Converted stays possible (mistakes must be fixable); converting again creates
  another opportunity. Nothing else is created.

## Consequences
- Every Converted lead has an opportunity; reports can rely on it.
- The status endpoint answers 422 "Use Convert…" for a lead without one; the UI offers a
  Convert dialog on the lead page and lands on the new opportunity.
- One conversion writes three audit events (opportunity created via conversion, lead
  status changed, lead converted), each describing a distinct fact.

## Alternatives considered
- Conversion *requires* an existing opportunity (no Convert operation): two steps where
  one is expected, and nothing prevents "Converted" leads with none from the status menu.
- Automatically creating an opportunity when the status changes: hidden magic with no way
  to enter the opportunity's title and value.
- Allowing Converted without an opportunity (Phase 2 behaviour): the status would keep
  meaning nothing.
