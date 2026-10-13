"""The one rule a production credential must pass, and the production stack's guard
(adversarial review of 73ff1e3, M4).

The production-shaped Compose stack (infrastructure/compose.production.yml) took the
PostgreSQL superuser's password from `${POSTGRES_PASSWORD:?}`. Compose fills that from the
shell and from the repository's `.env`, and `.env.example` (so every developer's `.env`) sets
it to the public development default; the operations guide sourced the operator's secrets
file without `set -a`, so its values weren't exported and Compose fell back to the `.env`.
The stack started, the superuser reachable from every container with a published password,
and nothing anywhere judged a password's strength. Now:

- the overlay reads its secrets only under production-only names (`ARKRAY_PROD_*`,
  COMPOSE_SECRETS below) that no development file defines, from a file given with
  `docker compose --env-file` (Compose then never reads `.env`);
- a one-shot service, `credential-guard`, runs `python -m config.credentials` before anything
  else starts and every service waits for it to succeed: it refuses a missing secret, a
  development or placeholder value, a weak one (`weakness`) and one value used for two
  secrets, naming the variable and never printing a value;
- production settings refuse a development or placeholder password in DATABASE_URL and the
  Redis URLs (`is_development_value`; config/settings/production.py), and `manage.py
  preflight` judges those passwords by the whole rule (config/preflight.py);
- infrastructure/postgres/roles.sql refuses role passwords shorter than MIN_LENGTH.

The rule, in this one place (tests/architecture/test_credentials.py). Every value is
generated, never chosen by a person, so the rule may be strict: not a value the repository
publishes for development or tests or a template's placeholder; at least MIN_LENGTH
characters, MIN_DISTINCT of them different, at least MIN_BITS_PER_CHARACTER of Shannon
entropy per character (how evenly they are used) and MIN_ESTIMATED_BITS by the size of the
character classes it uses (lower case, upper case, digits, anything else) times its length;
and none of the shapes people choose (review round 1, INFRA-4: `Summer2026Summer2026...`,
`arkray-crm-production-2026` and `123456789012...` had passed): a short unit repeated, a run
of SEQUENCE_RUN characters in alphabet, digit or keyboard order, words and numbers joined by
separators, or a common password word. Measured on 200,000 values of each generated kind
(32 and 64 hex characters, 32 and 43 URL-safe ones): none refused. A generated value passes:
`openssl rand -hex 32` or `python -c "import secrets; print(secrets.token_urlsafe(32))"`
(scripts/init-prod-env.sh writes a whole file of them).

No Django here: the guard runs before the database exists, with nothing but its environment.
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

MIN_LENGTH = 32
MIN_DISTINCT = 8
MIN_BITS_PER_CHARACTER = 2.5
MIN_ESTIMATED_BITS = 128
SEQUENCE_RUN = 8

# Published by the repository for development and tests (.env.example, docker-compose.yml's
# fallbacks, CI's database service, config/settings/test.py): never a production value.
DEVELOPMENT_VALUES = frozenset(
    {
        "arkray_dev_only_password",
        "dev-only-insecure-key-change-me-0000000000000000000000000000000000",
        "dev-only-erasure-ledger-key-not-for-production-0000",
    }
)
# What only templates, examples and development values carry (lower case): a random value
# holds none of them (each is 7 characters or more; the odds are nil).
_MARKERS = (
    "dev_only",
    "dev-only",
    "devonly",
    "change-me",
    "change_me",
    "changeme",
    "not-for-production",
    "not_for_production",
    "insecure",
    "placeholder",
    "example",
    "password",
    "passw0rd",
)
_PREFIXES = ("dev-", "dev_", "test-", "test_", "django-insecure-")
# Words people put in passwords (lower case, 6 letters or more, each with a letter hex never
# has, so a hex value can't hold one; a URL-safe one does once in about 10 million).
_COMMON_WORDS = (
    "arkray",
    "welcome",
    "summer",
    "winter",
    "spring",
    "autumn",
    "monkey",
    "dragon",
    "letmein",
    "qwerty",
    "secret",
    "production",
    "postgres",
    "django",
    "master",
    "shadow",
    "sunshine",
    "princess",
    "football",
    "baseball",
    "superman",
    "trustno",
    "iloveyou",
    "starwars",
)
# Orders people type in: the alphabet, digits, keyboard rows (and each backwards).
_ORDERS = ("abcdefghijklmnopqrstuvwxyz", "01234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")
# The characters a password may hold when it is written into a connection URL unencoded
# (RFC 3986's unreserved set): anything else breaks DATABASE_URL or a Redis URL, or must be
# percent-encoded by hand.
URL_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def is_development_value(value: str) -> bool:
    """Whether `value` is one the repository publishes, or shaped like a placeholder or a
    development value (`change-me-...`, `dev-...`, `..._dev_only_...`)."""
    lowered = value.strip().lower()
    return (
        value in DEVELOPMENT_VALUES
        or lowered.startswith(_PREFIXES)
        or any(marker in lowered for marker in _MARKERS)
    )


def estimated_bits(value: str) -> float:
    """Length x log2(the size of the character classes the value uses): a ceiling on what a
    guesser who knows the classes must try. 24 hex digits: 124; 24 digits alone: 80."""
    pool = 0
    pool += 26 if any("a" <= char <= "z" for char in value) else 0
    pool += 26 if any("A" <= char <= "Z" for char in value) else 0
    pool += 10 if any(char.isascii() and char.isdigit() for char in value) else 0
    pool += 33 if any(not (char.isascii() and char.isalnum()) for char in value) else 0
    return len(value) * math.log2(pool) if pool else 0.0


def bits_per_character(value: str) -> float:
    """Shannon entropy of the value's own characters: how evenly it uses them (a random
    32-character hex value: about 3.7; `aaaa...bcdef`: under 1.5)."""
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def is_repetition(value: str) -> bool:
    """Whether the value is a shorter unit repeated (the last copy may be cut short)."""
    return any(
        all(value[i] == value[i % unit] for i in range(len(value)))
        for unit in range(1, len(value) // 2 + 1)
    )


def longest_sequence(value: str) -> int:
    """The longest run of characters each following the one before in an order people
    type in (ascending or descending), case aside."""
    lowered, longest = value.lower(), 1
    for order in _ORDERS:
        for direction in (order, order[::-1]):
            run = 1
            for before, after in pairwise(lowered):
                at = direction.find(before)
                run = run + 1 if at >= 0 and direction[at + 1 : at + 2] == after else 1
                longest = max(longest, run)
    return longest


def is_words_and_numbers(value: str) -> bool:
    """Three or more words or numbers joined by separators (`arkray-crm-production-2026`):
    each part letters of one case (or capitalised) or digits only."""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return len(parts) >= 3 and all(
        (part.isalpha() and (part.islower() or part.isupper() or part.istitle())) or part.isdigit()
        for part in parts
    )


def weakness(value: str) -> str | None:
    """Why `value` can't be a production credential, or None when it can. The reason never
    quotes the value."""
    if not value:
        return "is empty"
    if is_development_value(value):
        return (
            "is a development or placeholder value (published in the repository, or shaped"
            " like one)"
        )
    if len(value) < MIN_LENGTH:
        return f"is shorter than {MIN_LENGTH} characters"
    if len(set(value)) < MIN_DISTINCT:
        return f"has fewer than {MIN_DISTINCT} different characters"
    if bits_per_character(value) < MIN_BITS_PER_CHARACTER:
        return (
            f"uses its characters too unevenly (under {MIN_BITS_PER_CHARACTER} bits each):"
            " generate it"
        )
    if estimated_bits(value) < MIN_ESTIMATED_BITS:
        return (
            f"is too predictable (under {MIN_ESTIMATED_BITS} bits: use letters and digits,"
            " or make it longer)"
        )
    if is_repetition(value):
        return "repeats a shorter part: generate it"
    if longest_sequence(value) >= SEQUENCE_RUN:
        return (
            f"holds {SEQUENCE_RUN} or more characters in alphabet, digit or keyboard order:"
            " generate it"
        )
    if is_words_and_numbers(value):
        return "is words and numbers joined together: generate it"
    if any(word in value.lower() for word in _COMMON_WORDS):
        return "holds a common password word: generate it"
    return None


@dataclass(frozen=True, slots=True)
class Secret:
    name: str  # the production-only variable the overlay interpolates
    what: str
    in_url: bool = False  # written into a connection URL: URL_SAFE characters only
    required: bool = True


# Every secret infrastructure/compose.production.yml reads, under the names only production
# defines (tests/architecture/test_compose_credentials.py: the overlay interpolates exactly
# these, the guard receives each, `.env.example` defines none).
COMPOSE_SECRETS = (
    Secret(
        "ARKRAY_PROD_DB_SUPERUSER_PASSWORD",
        "the PostgreSQL superuser's password (the role setup, on the local socket only)",
        in_url=True,
    ),
    Secret(
        "ARKRAY_PROD_DB_OWNER_PASSWORD",
        "arkray_owner's password (the migrate job only)",
        in_url=True,
    ),
    Secret(
        "ARKRAY_PROD_DB_APP_PASSWORD",
        "arkray_app's password (the web server and the workers)",
        in_url=True,
    ),
    Secret("ARKRAY_PROD_REDIS_PASSWORD", "the broker's and the cache's password", in_url=True),
    Secret("ARKRAY_PROD_DJANGO_SECRET_KEY", "DJANGO_SECRET_KEY"),
    Secret("ARKRAY_PROD_METRICS_TOKEN", "METRICS_TOKEN (the scraper's bearer token)"),
    Secret("ARKRAY_PROD_ERASURE_LEDGER_KEY", "ERASURE_LEDGER_KEY (the ledger's HMAC key)"),
    Secret(
        "ARKRAY_PROD_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY (worker-ai only; empty while the provider is none)",
        required=False,
    ),
)


def check_environment(environ: Mapping[str, str]) -> list[str]:
    """Every problem with the production stack's secrets in `environ`, one line each, naming
    variables only."""
    problems: list[str] = []
    given: dict[str, str] = {}
    for secret in COMPOSE_SECRETS:
        value = environ.get(secret.name, "")
        if not value:
            if secret.required:
                problems.append(f"{secret.name} is not set: {secret.what}")
            continue
        given[secret.name] = value
        if reason := weakness(value):
            problems.append(f"{secret.name} {reason}: {secret.what}")
        elif secret.in_url and not set(value) <= URL_SAFE:
            problems.append(
                f"{secret.name} has characters a connection URL reserves: use letters, digits"
                " and - . _ ~ only (openssl rand -hex 32)"
            )
    names = sorted(given)
    for index, name in enumerate(names):
        for other in names[index + 1 :]:
            if given[name] == given[other]:
                problems.append(
                    f"{name} and {other} are the same value: each secret must be its own, or one"
                    " leak opens both"
                )
    return problems


def main(environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    problems = check_environment(environ)
    for problem in problems:
        print(f"credential-guard: {problem}", file=sys.stderr)  # noqa: T201 — a one-shot job's output
    if problems:
        print(  # noqa: T201
            "credential-guard: refused; nothing starts until every production secret is"
            " generated for this deployment (scripts/init-prod-env.sh,"
            " docs/operations.md#production-shaped-stack)",
            file=sys.stderr,
        )
        return 1
    checked = sum(1 for secret in COMPOSE_SECRETS if environ.get(secret.name))
    print(  # noqa: T201
        f"credential-guard: {checked} production secrets checked: none is a development or"
        " placeholder value, weak, or used twice"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
