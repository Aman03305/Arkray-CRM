"""DATABASE_URL may name a transaction-mode pooler (PgBouncer, Supavisor; docs/deployment.md):
then nothing that lives on the session may be relied on (final remediation, Supabase and
Render compatibility)."""

from __future__ import annotations

import json

from tests.architecture.test_production_settings import load_production_settings

PRINT = (
    "__import__('json').dumps(["
    "settings.DATABASES['default']['OPTIONS'].get('options'),"
    " settings.DATABASES['default']['OPTIONS'].get('prepare_threshold', 'unset'),"
    " settings.DATABASES['default'].get('DISABLE_SERVER_SIDE_CURSORS', False)])"
)


def test_direct_connections_set_the_timeouts_at_connect_time():
    result = load_production_settings(PRINT)
    assert result.returncode == 0, result.stderr
    options, prepare, cursors = json.loads(result.stdout)
    assert "-c statement_timeout=10000" in options
    assert "-c lock_timeout=5000" in options
    assert prepare == "unset"  # psycopg's default: prepared after 5 uses
    assert cursors is False


def test_a_transaction_pooler_gets_no_session_state():
    result = load_production_settings(PRINT, DB_TRANSACTION_POOLER="true")
    assert result.returncode == 0, result.stderr
    # No startup options (the poolers refuse them), no server-side prepared statements, no
    # server-side cursors.
    assert json.loads(result.stdout) == [None, None, True]
