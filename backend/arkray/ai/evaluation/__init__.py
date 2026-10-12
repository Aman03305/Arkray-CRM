"""Ask Arkray's real-model evaluation suite (risk R69, docs/ai-evaluation.md).

Every security property of Ask Arkray is independent of the model (scope-bound tools, safe
rendering, numeric grounding: docs/rag-architecture.md), and every one is tested with a
scripted model. What a real model does with them (which tools it chooses, whether its
figures are right, whether it obeys a planted instruction, what it costs and how long it
takes) has never been measured: no key, and no authorisation to send CRM data to a
provider. This package is that measurement, ready to run the day both exist:

- `dataset`: a synthetic CRM (salespeople, an administrator, an empty workspace, known
  figures, planted cross-user canaries and prompt injections, near-duplicate names), seeded
  through the CRM services into a database of its own (`database`: refused unless its name
  ends in `_ai_eval` and it holds nothing the evaluation didn't write).
- `cases`: the questions, each asked as a given user in a given workspace through the real
  service path (`service.submit` / `service.answer`), with what a correct answer contains and
  scripts for a well-behaved and an adversarial scripted model.
- `recording`: a wrapper around whichever provider runs (scripted, Anthropic, or a dry run
  that sends nothing) that records every payload, completion, latency, token count and cost,
  and enforces the run's hard caps on model calls and spend.
- `graders`: automated checks per case (figures, citations, authorisation canaries,
  minimisation of what is sent, injection, hallucination, tool-use safety, budgets).
- `runner` / `report`: the run, its interlocks and its JSON and Markdown reports. Only a run
  against the real model can PASS; anything else is NOT VERIFIED.

The management command is `manage.py ai_eval` (arkray/ai/management/commands/ai_eval.py).
"""
