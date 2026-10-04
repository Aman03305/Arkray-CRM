-- Arkray CRM: the production database and its two roles (docs/deployment.md#database,
-- docs/security.md#database-privileges). Run once, as a superuser or the managed service's
-- administrator; safe to run again:
--
--   psql "$ADMIN_DATABASE_URL" -v database=arkray \
--        -v owner_password="$OWNER_PASSWORD" -v app_password="$APP_PASSWORD" \
--        -f infrastructure/postgres/roles.sql
--
-- arkray_owner owns the schema and runs `migrate` (then `grant_app_privileges arkray_app`).
-- arkray_app runs the web servers and workers: no superuser, owns nothing, so the
-- append-only trigger binds it and it can never TRUNCATE the audit trail (R74); the web
-- server and workers refuse to start as anything more privileged.
\set ON_ERROR_STOP on

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
