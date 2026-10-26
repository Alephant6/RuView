-- What a RuView database holds, to compare a restored copy with the original
-- (.github/workflows/backup.yml):
--   psql -X -At -v ON_ERROR_STOP=1 -f digest.sql > digest.txt
-- The rows of every table and continuous aggregate in the RuView schemas (a
-- count and a hash), the roles without their passwords, the privileges, the
-- chunks and their compression, and the policies. Leaves out what differs
-- between two servers anyway: job run times, statistics, internal ids.

SET timezone = 'UTC';

\echo == rows
SELECT format('SELECT %L, count(*), md5(coalesce(string_agg(t::text, E''\n'' ORDER BY t::text), '''')) FROM %s AS t',
              c.oid::regclass, c.oid::regclass)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname IN ('public', 'ingest', 'agg') AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.oid::regclass::text
\gexec

\echo == roles
SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication,
       rolbypassrls, rolconnlimit, rolconfig
FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname;
SELECT r.rolname, m.rolname, a.admin_option, a.inherit_option, a.set_option
FROM pg_auth_members AS a
     JOIN pg_roles AS r ON r.oid = a.roleid
     JOIN pg_roles AS m ON m.oid = a.member
WHERE m.rolname !~ '^pg_' ORDER BY 1, 2;

\echo == privileges
SELECT datname, array(SELECT unnest(datacl)::text ORDER BY 1)
FROM pg_database WHERE datname = current_database();
SELECT nspname, array(SELECT unnest(nspacl)::text ORDER BY 1)
FROM pg_namespace WHERE nspname IN ('public', 'ingest', 'agg') ORDER BY 1;
SELECT c.oid::regclass, array(SELECT unnest(c.relacl)::text ORDER BY 1)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname IN ('public', 'ingest', 'agg') AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
ORDER BY c.oid::regclass::text;
SELECT pg_get_userbyid(defaclrole), defaclnamespace::regnamespace::text, defaclobjtype,
       array(SELECT unnest(defaclacl)::text ORDER BY 1)
FROM pg_default_acl ORDER BY 1, 2, 3;

\echo == timescaledb
SELECT hypertable_schema, hypertable_name, chunk_name, range_start, range_end, is_compressed
FROM timescaledb_information.chunks ORDER BY 1, 2, 4;
SELECT job_id, proc_name, hypertable_schema, hypertable_name, schedule_interval, config, scheduled
FROM timescaledb_information.jobs WHERE job_id >= 1000 ORDER BY job_id;
SELECT view_schema, view_name, materialized_only, compression_enabled
FROM timescaledb_information.continuous_aggregates ORDER BY 1, 2;
