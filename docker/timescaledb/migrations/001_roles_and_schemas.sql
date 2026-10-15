-- WP1.1: roles and schemas of the RuView TimescaleDB.
--
--   ingest  L1 raw rows that Telegraf writes (14 to 90 days, some forever)
--   agg     L2 continuous aggregates, 1 minute and 1 hour (kept forever)
--   public  L3/L4 facts that analytics writes (episodes, daily summaries, ...)
--   llm     read-only views for Claude (role llm_ro), added with WP1.2
--
-- Roles start without login; migrate.sh enables each one whose password is
-- set in docker/.env (PG_TELEGRAF_PASSWORD, ...). Objects belong to the
-- superuser that runs the migrations.

CREATE EXTENSION IF NOT EXISTS timescaledb;

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['telegraf', 'analytics', 'grafana', 'llm_ro'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END
$$;

-- Only the RuView roles may connect to this database.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO telegraf, analytics, grafana, llm_ro',
                   current_database());
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS agg;
CREATE SCHEMA IF NOT EXISTS llm;
COMMENT ON SCHEMA ingest IS 'L1: raw rows written by Telegraf (outputs.postgresql)';
COMMENT ON SCHEMA agg IS 'L2: continuous aggregates per minute and per hour';
COMMENT ON SCHEMA llm IS 'Read-only views for Claude questions (role llm_ro)';

-- telegraf: insert raw rows, nothing else (no CREATE: tables come from here).
GRANT USAGE ON SCHEMA ingest TO telegraf;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest GRANT INSERT ON TABLES TO telegraf;

-- analytics: read L1/L2, write the facts in public.
GRANT USAGE ON SCHEMA ingest, agg, public TO analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest GRANT SELECT ON TABLES TO analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA agg GRANT SELECT ON TABLES TO analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO analytics;

-- grafana: read everything except the llm views, in read-only sessions.
GRANT USAGE ON SCHEMA ingest, agg, public TO grafana;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest GRANT SELECT ON TABLES TO grafana;
ALTER DEFAULT PRIVILEGES IN SCHEMA agg GRANT SELECT ON TABLES TO grafana;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
ALTER ROLE grafana SET default_transaction_read_only = on;
ALTER ROLE grafana SET statement_timeout = '60s';

-- llm_ro: only the curated views in schema llm, read-only, short queries.
GRANT USAGE ON SCHEMA llm TO llm_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA llm GRANT SELECT ON TABLES TO llm_ro;
ALTER ROLE llm_ro SET default_transaction_read_only = on;
ALTER ROLE llm_ro SET statement_timeout = '30s';
