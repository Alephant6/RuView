-- WP1.1: a role for the nightly offsite backup (docker/backup).
--
-- pg_dump needs to read every table, TimescaleDB's own catalog and chunks
-- included, so the role is a member of pg_read_all_data. It cannot write.
-- migrate.sh enables its login when PG_BACKUP_PASSWORD is set.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup') THEN
        CREATE ROLE backup NOLOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO backup', current_database());
END
$$;

GRANT pg_read_all_data TO backup;
ALTER ROLE backup SET default_transaction_read_only = on;
