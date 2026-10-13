-- Arkray CRM: the production database and its two roles (docs/deployment.md#database,
-- docs/security.md#database-privileges). Run once, as a superuser or the managed service's
-- administrator; safe to run again:
--
--   OWNER_PASSWORD=... APP_PASSWORD=... \
--     psql "$ADMIN_DATABASE_URL" -v database=arkray -f infrastructure/postgres/roles.sql
--
-- The passwords come from the environment (psql 15 or newer: \getenv), never from psql's
-- command line, where any local user could read them in the process list (secret-exposure
-- audit). They must be generated for the environment: at least 32 characters
-- (config/credentials.py's MIN_LENGTH, which the production stack's guard applies with the
-- rest of its rule) and different from each other; anything else is refused before a role
-- is created (adversarial review of 73ff1e3, M4). Both are stored as SCRAM verifiers.
--
-- The production-shaped stack (infrastructure/compose.production.yml) runs this in its
-- db-roles job over the server's local socket, the only place its pg_hba.conf admits the
-- superuser; by hand there: `docker compose ... run --rm db-roles`.
--
-- arkray_owner owns the schema and runs `migrate` (then `grant_app_privileges arkray_app`).
-- arkray_app runs the web servers and workers: no superuser, owns nothing, so the
-- append-only trigger binds it and it can never TRUNCATE the audit trail (R74); the web
-- server and workers refuse to start as anything more privileged.
\set ON_ERROR_STOP on

\getenv owner_password OWNER_PASSWORD
\getenv app_password APP_PASSWORD
\if :{?owner_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'set OWNER_PASSWORD (the arkray_owner role''s password)'; END $$;
\endif
\if :{?app_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'set APP_PASSWORD (the arkray_app role''s password)'; END $$;
\endif

-- CREATE ROLE ... PASSWORD carries the password in the statement's text: keep this session's
-- statements out of the server log even where log_statement is ddl or all. A managed
-- service's administrator may not be allowed to change it: then say so and go on.
DO $$
BEGIN
    PERFORM set_config('log_statement', 'none', false);
EXCEPTION WHEN insufficient_privilege THEN
    RAISE WARNING 'could not set log_statement = none: make sure the server does not log DDL while this runs';
END $$;

-- Generated for this environment (M4): long enough, and not one password for both roles.
-- pg_stat_statements keeps this statement's shape only, never its values.
SELECT length(:'owner_password') >= 32 AND length(:'app_password') >= 32
    AND :'owner_password' <> :'app_password' AS passwords_acceptable \gset
\if :passwords_acceptable
\else
DO $$ BEGIN RAISE EXCEPTION 'OWNER_PASSWORD and APP_PASSWORD must each be generated for this environment: at least 32 characters, and different (config/credentials.py)'; END $$;
\endif

-- SCRAM verifiers only (pg_hba.conf accepts nothing else), whatever the server's default.
SET password_encryption = 'scram-sha-256';

SELECT format('CREATE ROLE arkray_owner LOGIN PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arkray_owner') \gexec
SELECT format(
    'CREATE ROLE arkray_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
    :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arkray_app') \gexec

SELECT format('CREATE DATABASE %I OWNER arkray_owner', :'database')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database') \gexec

\connect :database

-- Extensions need a superuser (pgvector isn't a trusted extension); the migrations' own
-- CREATE EXTENSION IF NOT EXISTS then find them in place.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
-- Slow-statement review without values (needs shared_preload_libraries=pg_stat_statements).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

ALTER SCHEMA public OWNER TO arkray_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO arkray_app;
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'database') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO arkray_owner, arkray_app', :'database')
\gexec

-- R63: PostgreSQL would log a failing statement's text, values included (Django binds
-- values on the client), and a failing row's values in the DETAIL line of a constraint
-- violation ("Failing row contains (...)", "Key (email)=(...)": whole-software audit).
-- On a managed service set both in the parameter group instead.
SELECT format('ALTER DATABASE %I SET log_min_error_statement = %L', :'database', 'panic') \gexec
SELECT format('ALTER DATABASE %I SET log_error_verbosity = %L', :'database', 'terse') \gexec
