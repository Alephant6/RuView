-- Checks after publish_samples.py went through Telegraf into TimescaleDB
-- (.github/workflows/timescaledb.yml). Run as the superuser with
-- ON_ERROR_STOP; any failed check raises an exception.

-- Every table Telegraf fills has rows.
DO $$
DECLARE
    t text;
    n bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY['state', 'node', 'person', 'ha_binary', 'ha_sensor', 'ha_event',
                             'bridge', 'watchdog', 'recorder', 'recorder_keep', 'annotation'] LOOP
        EXECUTE format('SELECT count(*) FROM ingest.%I', t) INTO n;
        IF n = 0 THEN
            RAISE EXCEPTION 'ingest.% has no rows', t;
        END IF;
        RAISE NOTICE 'ingest.%: % rows', t, n;
    END LOOP;
END
$$;

-- Payload values land in the right columns, booleans as 1/0.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM ingest.state WHERE source = 'esp32' AND presence = 1 AND stale_source = 0
                   AND heart_rate_bpm > 0 AND numeric_vitals_authorized = 1 AND calibration_fresh = 1
                   AND motion_level = 'present_still' AND tick >= 1000 AND ts > 0) THEN
        RAISE EXCEPTION 'ingest.state: no live row with its fields';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.state WHERE stale_source = 1 AND heart_rate_bpm IS NULL
                   AND numeric_vitals_authorized = 0
                   AND abstention_reason = 'exactly_one_occupant_required') THEN
        RAISE EXCEPTION 'ingest.state: no stale row';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.node WHERE node_id = '2' AND stale = 0 AND csi_fps = 22.5
                   AND subcarrier_count = 128 AND position_2 = 0.7) THEN
        RAISE EXCEPTION 'ingest.node: row for node 2 missing or incomplete';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.person WHERE person_key = 'sq' AND label = 'sq' AND zone = 'bed'
                   AND occupied = 1 AND z = 0.9)
       OR NOT EXISTS (SELECT FROM ingest.person WHERE person_key = '2' AND occupied = 0) THEN
        RAISE EXCEPTION 'ingest.person: rows missing or incomplete';
    END IF;
    IF (SELECT count(*) FROM ingest.ha_binary
        WHERE (entity = 'someone_sleeping' AND value = 'ON' AND "on" = 1)
           OR (entity = 'room_active' AND value = 'OFF' AND "on" = 0)) <> 2 THEN
        RAISE EXCEPTION 'ingest.ha_binary: ON/OFF not stored as 1/0';
    END IF;
    IF EXISTS (SELECT FROM ingest.ha_sensor WHERE tags ->> 'device' NOT LIKE 'wifi_densepose_%') THEN
        RAISE EXCEPTION 'ingest.ha_sensor: stored another integration''s sensor';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.ha_sensor WHERE tags ->> 'entity' = 'heart_rate'
                   AND agg.ha_value(fields) = 61 AND NOT tags ? 'host' AND NOT tags ? 'topic') THEN
        RAISE EXCEPTION 'ingest.ha_sensor: heart_rate row missing, or host/topic tags kept';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.recorder WHERE (fields ->> 'recording')::int = 1
                   AND (fields ->> 'kinds_hz_csi')::float = 69) THEN
        RAISE EXCEPTION 'ingest.recorder: flattened fields missing';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.annotation WHERE time = '2026-09-21T14:13:20Z'
                   AND tags ->> 'label' = 'alcohol' AND fields ->> 'text' = 'ci sample') THEN
        RAISE EXCEPTION 'ingest.annotation: not stored at its press time';
    END IF;
    IF NOT EXISTS (SELECT FROM ingest.annotation WHERE time = '2026-09-21T14:23:20Z'
                   AND tags ->> 'kind' = 'calibration' AND tags ->> 'action' = 'done'
                   AND tags ->> 'source' = 'watchdog' AND (fields ->> 'frame_count')::float = 12000
                   AND (fields ->> 'source_node')::float = 1 AND fields ->> 'text' = 'ci calibration') THEN
        RAISE EXCEPTION 'ingest.annotation: watchdog calibration note missing or without its numbers';
    END IF;
END
$$;

-- Continuous aggregates: materialize everything so far, then read it back.
CALL refresh_continuous_aggregate('agg.state_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.state_1h', NULL, NULL);
CALL refresh_continuous_aggregate('agg.node_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.ha_sensor_1h', NULL, NULL);
DO $$
DECLARE
    s record;
BEGIN
    SELECT sum(n) AS n, sum(n_live) AS n_live, sum(hr_n) AS hr_n, max(hr_distinct) AS hr_distinct
      INTO s FROM agg.state_1h;
    IF s.n IS NULL OR s.n_live >= s.n OR s.hr_n > s.n_live OR s.hr_distinct < 2 THEN
        RAISE EXCEPTION 'agg.state_1h: unexpected counts %', s;
    END IF;
    IF NOT EXISTS (SELECT FROM agg.node_1m WHERE node_id = '2' AND csi_fps_avg = 22.5) THEN
        RAISE EXCEPTION 'agg.node_1m: node 2 missing';
    END IF;
    IF NOT EXISTS (SELECT FROM agg.ha_sensor_1h WHERE entity = 'heart_rate' AND value_med = 61) THEN
        RAISE EXCEPTION 'agg.ha_sensor_1h: heart_rate missing';
    END IF;
END
$$;

-- Roles: grafana reads but cannot write; telegraf writes but cannot read.
SET ROLE grafana;
SELECT count(*) AS grafana_reads_agg FROM agg.state_1m;
SELECT count(*) AS grafana_reads_ingest FROM ingest.state;
DO $$
BEGIN
    INSERT INTO ingest.state (time) VALUES (now());
    RAISE EXCEPTION 'grafana could insert into ingest.state';
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END
$$;
RESET ROLE;
SET ROLE telegraf;
DO $$
BEGIN
    PERFORM count(*) FROM ingest.state;
    RAISE EXCEPTION 'telegraf could read ingest.state';
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END
$$;
DO $$
BEGIN
    CREATE TABLE ingest.not_allowed (a int);
    RAISE EXCEPTION 'telegraf could create a table';
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END
$$;
RESET ROLE;
-- backup reads everything, TimescaleDB's catalog included, and cannot write.
SET ROLE backup;
SELECT count(*) AS backup_reads_hypertables FROM _timescaledb_catalog.hypertable;
SELECT count(*) AS backup_reads_ingest FROM ingest.state;
DO $$
BEGIN
    INSERT INTO ingest.annotation (time) VALUES (now());
    RAISE EXCEPTION 'backup could insert into ingest.annotation';
EXCEPTION WHEN insufficient_privilege THEN
    NULL;
END
$$;
RESET ROLE;
SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname IN ('telegraf', 'analytics', 'grafana', 'llm_ro', 'backup') ORDER BY 1;
