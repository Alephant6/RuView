-- WP1.1: L2 continuous aggregates, per minute and per hour, kept forever.
--
-- Each pair (_1m, _1h) has the same columns, computed from L1 (so hourly
-- medians are exact). TimescaleDB refreshes them every few minutes over the
-- last 3 days, well inside the 14 days of L1, so an aggregate never loses the
-- raw rows it was built from; older raw rows are then dropped by retention
-- while the aggregates stay. Recent buckets are computed on the fly
-- (materialized_only = false), so dashboards see the current minute too.
--
-- Vitals statistics use only live ticks (stale_source = 0): a server whose
-- ESP32 source went offline keeps re-sending its last values.
-- hr_distinct / br_distinct: very few distinct values over many samples
-- means frozen vitals (the September 83.37 / 19.28 incident).

-- The number in a built-in publisher payload: bpm, n_persons, level_pct,
-- energy, score_pct, dbm or score, whichever the entity sends.
CREATE OR REPLACE FUNCTION agg.ha_value(fields jsonb) RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce((fields ->> 'bpm')::double precision,
                    (fields ->> 'n_persons')::double precision,
                    (fields ->> 'level_pct')::double precision,
                    (fields ->> 'energy')::double precision,
                    (fields ->> 'score_pct')::double precision,
                    (fields ->> 'dbm')::double precision,
                    (fields ->> 'score')::double precision)
$$;

DO $$
DECLARE
    b record;
    live constant text := 'FILTER (WHERE coalesce(stale_source, 0) = 0)';
BEGIN
    FOR b IN SELECT * FROM (VALUES ('1m', '1 minute'::interval), ('1h', '1 hour'::interval))
                           AS v(suffix, width) LOOP
        -- ── room-level state ────────────────────────────────────────────
        EXECUTE format($sql$
            CREATE MATERIALIZED VIEW IF NOT EXISTS agg.state_%1$s
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(%2$L, time) AS bucket,
                   count(*)                                                     AS n,
                   count(*) %3$s                                                AS n_live,
                   avg(presence) %3$s                                           AS presence,
                   avg(n_persons) %3$s                                          AS n_persons_avg,
                   max(n_persons) %3$s                                          AS n_persons_max,
                   count(*) FILTER (WHERE motion_level = 'absent')              AS n_absent,
                   count(*) FILTER (WHERE motion_level = 'present_still')       AS n_still,
                   count(*) FILTER (WHERE motion_level = 'present_moving')      AS n_moving,
                   count(*) FILTER (WHERE motion_level = 'active')              AS n_active,
                   avg(motion_energy) %3$s                                      AS motion_energy_avg,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY motion_energy) %3$s AS motion_energy_med,
                   max(motion_energy) %3$s                                      AS motion_energy_max,
                   count(heart_rate_bpm) %3$s                                   AS hr_n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY heart_rate_bpm) %3$s AS hr_med,
                   avg(heart_rate_bpm) %3$s                                     AS hr_avg,
                   min(heart_rate_bpm) %3$s                                     AS hr_min,
                   max(heart_rate_bpm) %3$s                                     AS hr_max,
                   stddev_samp(heart_rate_bpm) %3$s                             AS hr_sd,
                   count(DISTINCT heart_rate_bpm) %3$s                          AS hr_distinct,
                   avg(hr_conf) %3$s                                            AS hr_conf_avg,
                   count(breathing_rate_bpm) %3$s                               AS br_n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY breathing_rate_bpm) %3$s AS br_med,
                   avg(breathing_rate_bpm) %3$s                                 AS br_avg,
                   min(breathing_rate_bpm) %3$s                                 AS br_min,
                   max(breathing_rate_bpm) %3$s                                 AS br_max,
                   stddev_samp(breathing_rate_bpm) %3$s                         AS br_sd,
                   count(DISTINCT breathing_rate_bpm) %3$s                      AS br_distinct,
                   avg(br_conf) %3$s                                            AS br_conf_avg,
                   avg(signal_quality) %3$s                                     AS signal_quality_avg,
                   avg(numeric_vitals_authorized)                               AS vitals_authorized,
                   avg(calibration_fresh)                                       AS calibration_fresh,
                   mode() WITHIN GROUP (ORDER BY abstention_reason)             AS abstention_reason,
                   mode() WITHIN GROUP (ORDER BY room_class)                    AS room_class,
                   max(fall_detected)                                           AS fall_detected,
                   min(fresh_nodes)                                             AS fresh_nodes_min,
                   avg(fresh_nodes)                                             AS fresh_nodes_avg
            FROM ingest.state
            GROUP BY 1
            WITH NO DATA
        $sql$, b.suffix, b.width, live);

        -- ── per node ────────────────────────────────────────────────────
        EXECUTE format($sql$
            CREATE MATERIALIZED VIEW IF NOT EXISTS agg.node_%1$s
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(%2$L, time) AS bucket,
                   node_id,
                   count(*)              AS n,
                   avg(stale)            AS stale,
                   avg(csi_fps)          AS csi_fps_avg,
                   min(csi_fps)          AS csi_fps_min,
                   max(csi_fps)          AS csi_fps_max,
                   avg(rssi_dbm)         AS rssi_avg,
                   min(rssi_dbm)         AS rssi_min,
                   max(rssi_dbm)         AS rssi_max,
                   avg(person_score)     AS person_score_avg,
                   max(subcarrier_count) AS subcarrier_count
            FROM ingest.node
            GROUP BY 1, 2
            WITH NO DATA
        $sql$, b.suffix, b.width);

        -- ── per node and entity, from the server's built-in publisher ───
        EXECUTE format($sql$
            CREATE MATERIALIZED VIEW IF NOT EXISTS agg.ha_sensor_%1$s
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT time_bucket(%2$L, time) AS bucket,
                   tags ->> 'device'            AS device,
                   tags ->> 'entity'            AS entity,
                   count(*)                     AS n,
                   avg(agg.ha_value(fields))    AS value_avg,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY agg.ha_value(fields)) AS value_med,
                   min(agg.ha_value(fields))    AS value_min,
                   max(agg.ha_value(fields))    AS value_max,
                   avg((fields ->> 'confidence')::double precision) AS confidence_avg
            FROM ingest.ha_sensor
            GROUP BY 1, 2, 3
            WITH NO DATA
        $sql$, b.suffix, b.width);
    END LOOP;
END
$$;

-- Refresh: per-minute views every 5 minutes, per-hour views every 30 minutes,
-- both over the last 3 days (late rows included).
DO $$
DECLARE
    v text;
BEGIN
    FOREACH v IN ARRAY ARRAY['agg.state_1m', 'agg.node_1m', 'agg.ha_sensor_1m'] LOOP
        PERFORM add_continuous_aggregate_policy(v::regclass, start_offset => INTERVAL '3 days',
            end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '5 minutes',
            if_not_exists => true);
    END LOOP;
    FOREACH v IN ARRAY ARRAY['agg.state_1h', 'agg.node_1h', 'agg.ha_sensor_1h'] LOOP
        PERFORM add_continuous_aggregate_policy(v::regclass, start_offset => INTERVAL '3 days',
            end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes',
            if_not_exists => true);
    END LOOP;
    -- Compress the per-minute history once it is a month old (after the
    -- 3-day refresh window, so refreshes never touch compressed chunks).
    FOREACH v IN ARRAY ARRAY['agg.state_1m', 'agg.node_1m', 'agg.ha_sensor_1m'] LOOP
        EXECUTE format('ALTER MATERIALIZED VIEW %s SET (timescaledb.compress = true)', v);
        PERFORM add_compression_policy(v::regclass, compress_after => INTERVAL '30 days',
                                       if_not_exists => true);
    END LOOP;
END
$$;

GRANT SELECT ON ALL TABLES IN SCHEMA agg TO analytics, grafana;

-- Notes on the columns that are not self-explanatory (shown by \d+).
DO $$
DECLARE
    v text;
BEGIN
    FOREACH v IN ARRAY ARRAY['agg.state_1m', 'agg.state_1h'] LOOP
        EXECUTE format($c$
            COMMENT ON COLUMN %1$s.n IS 'server ticks in the bucket (at most 10 per second)';
            COMMENT ON COLUMN %1$s.n_live IS 'ticks with a live CSI source (stale_source = 0); presence, motion and vitals statistics use only these';
            COMMENT ON COLUMN %1$s.presence IS 'share of live ticks with presence';
            COMMENT ON COLUMN %1$s.hr_n IS 'live ticks with a published heart rate; withheld values are not counted';
            COMMENT ON COLUMN %1$s.hr_distinct IS 'distinct heart-rate values; very few over many ticks means frozen vitals';
            COMMENT ON COLUMN %1$s.br_distinct IS 'distinct breathing-rate values; very few over many ticks means frozen vitals';
            COMMENT ON COLUMN %1$s.vitals_authorized IS 'share of ticks where the server authorized numeric vitals (NULL: no edge_vitals gate data)';
            COMMENT ON COLUMN %1$s.calibration_fresh IS 'share of ticks with a fresh empty-room calibration';
            COMMENT ON COLUMN %1$s.abstention_reason IS 'most common reason given when vitals were withheld (ticks without a reason are ignored)';
        $c$, v);
    END LOOP;
END
$$;
