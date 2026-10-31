-- Application login roles (architecture.md §17, §22; K23).
--
-- Run as kalkan_owner (the database owner; CREATEROLE locally, cloudsqlsuperuser on Cloud SQL):
--   psql "<kalkan_owner connection>" -v ON_ERROR_STOP=1 \
--     -v api_password="$DB_API_PASSWORD" -v worker_password="$DB_WORKER_PASSWORD" \
--     -f backend/sql/bootstrap_roles.sql
--
-- Idempotent: creates missing roles and resets their passwords. kalkan_api and kalkan_worker
-- are ordinary LOGIN roles: not superusers, no BYPASSRLS, cannot create roles or databases.
-- Table privileges and RLS policies are granted by the Alembic migrations.

\set ON_ERROR_STOP on

SELECT format('CREATE ROLE %I LOGIN NOCREATEDB NOCREATEROLE', role_name)
FROM (VALUES ('kalkan_api'), ('kalkan_worker')) AS roles(role_name)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name)
\gexec

SELECT format('ALTER ROLE kalkan_api WITH LOGIN PASSWORD %L', :'api_password')
\gexec
SELECT format('ALTER ROLE kalkan_worker WITH LOGIN PASSWORD %L', :'worker_password')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO kalkan_api, kalkan_worker', current_database())
\gexec
GRANT USAGE ON SCHEMA public TO kalkan_api, kalkan_worker;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname IN ('kalkan_api', 'kalkan_worker')
          AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication)
    ) THEN
        RAISE EXCEPTION 'kalkan_api and kalkan_worker must not have elevated attributes';
    END IF;
END
$$;
