-- SYNTHETIC rows for the dashboard test (.github/workflows/grafana.yml):
-- 35 days of a two-person household in every ingest table, so that each
-- panel has something to show. Not measured data.
--
--   - Local day (America/New_York): asleep 23-07 (one night in three both in
--     bed), up 07-09, out 09-12 on weekdays, at the desk 12-18, living room
--     18-23 with both at home.
--   - Empty-room calibration only in the last 3 days, so heart and breathing
--     rates are published only then, with one person still in the room.
--   - The CSI source drops out for 20 minutes two hours ago and for 3 hours
--     ten days ago (the server re-sends its last frame); node 2 drops out for
--     45 minutes 30 hours ago.
--   - Rows are denser in the last 2 days, as the dashboards' default ranges.
--
--   psql -v ON_ERROR_STOP=1 -f seed.sql

SET timezone = 'UTC';
\set tz '''America/New_York'''

CREATE FUNCTION pg_temp.phase(t timestamptz) RETURNS text LANGUAGE sql AS $$
    SELECT CASE
        WHEN h >= 23 OR h < 7 THEN 'sleep'
        WHEN h < 9 THEN 'morning'
        WHEN h < 12 AND dow < 6 THEN 'away'
        WHEN h < 18 THEN 'study'
        ELSE 'evening' END
    FROM (SELECT extract(hour FROM t AT TIME ZONE 'America/New_York') AS h,
                 extract(isodow FROM t AT TIME ZONE 'America/New_York') AS dow) AS l
$$;
CREATE FUNCTION pg_temp.csi_offline(t timestamptz) RETURNS boolean LANGUAGE sql AS $$
    SELECT (t >= now() - interval '130 minutes' AND t < now() - interval '110 minutes')
        OR (t >= now() - interval '10 days 3 hours' AND t < now() - interval '10 days')
$$;
CREATE FUNCTION pg_temp.node2_down(t timestamptz) RETURNS boolean LANGUAGE sql AS $$
    SELECT t >= now() - interval '30 hours' AND t < now() - interval '29 hours 15 minutes'
$$;
-- Every row older than 2 days is `older` apart, newer ones `newer` apart.
CREATE FUNCTION pg_temp.times(older interval, newer interval, horizon interval = '35 days')
RETURNS SETOF timestamptz LANGUAGE sql AS $$
    SELECT generate_series(date_trunc('minute', now() - horizon), now() - interval '2 days', older)
    UNION ALL
    SELECT generate_series(date_trunc('minute', now() - interval '2 days') + newer, now(), newer)
$$;

BEGIN;

-- ── ruview/state, every 10 s ───────────────────────────────────────────────
CREATE TEMP TABLE tick ON COMMIT DROP AS
SELECT t, e, off, ph, cal,
       CASE WHEN off THEN 'present_still'
            WHEN ph = 'sleep' THEN 'present_still'
            WHEN ph = 'morning' THEN 'present_moving'
            WHEN ph = 'away' THEN 'absent'
            WHEN ph = 'study' THEN CASE WHEN sin(e / 53.0) > 0.6 THEN 'present_moving' ELSE 'present_still' END
            WHEN sin(e / 41.0) > 0.8 THEN 'active'
            WHEN sin(e / 41.0) > 0.0 THEN 'present_moving'
            ELSE 'present_still' END AS motion,
       CASE WHEN ph = 'away' AND NOT off THEN 0
            WHEN ph = 'evening' OR (ph = 'sleep' AND floor((e - 10800) / 86400)::int % 3 = 0) THEN 2
            ELSE 1 END AS n
FROM generate_series(now() - interval '35 days', now(), interval '10 seconds') AS t,
     LATERAL (SELECT extract(epoch FROM t) AS e, pg_temp.csi_offline(t) AS off,
                     pg_temp.phase(t) AS ph, t >= now() - interval '3 days' AS cal) AS x;

INSERT INTO ingest.state (time, source, motion_level, presence, confidence, motion_energy,
        breathing_rate_bpm, heart_rate_bpm, signal_quality, hr_conf, br_conf, vitals_present,
        n_persons, calibrated, cal_person_count, room_class, room_conf, stale_source,
        active_nodes, fresh_nodes, fall_detected, abstention_reason, numeric_vitals_authorized,
        calibration_fresh, tick, ts)
SELECT t,
       CASE WHEN off THEN 'esp32:offline' ELSE 'esp32' END,
       motion,
       (motion <> 'absent')::int,
       round((0.8 + 0.1 * sin(e / 97.0))::numeric, 3),
       CASE WHEN off THEN 0.42
            ELSE round((CASE motion WHEN 'absent' THEN 0.02 WHEN 'present_still' THEN 0.15
                                    WHEN 'present_moving' THEN 0.9 ELSE 2.4 END
                        * (1 + 0.3 * sin(e / 7.0)))::numeric, 3) END,
       CASE WHEN off THEN 19.28 WHEN auth THEN round((CASE ph WHEN 'sleep' THEN 14 ELSE 16 END
                                                      + 1.5 * sin(e / 1700.0) + 0.6 * sin(e / 29.0))::numeric, 2) END,
       CASE WHEN off THEN 83.37 WHEN auth THEN round((CASE ph WHEN 'sleep' THEN 58 ELSE 70 END
                                                      + 4 * sin(e / 1900.0) + 1.5 * sin(e / 37.0))::numeric, 2) END,
       round((0.75 + 0.15 * sin(e / 300.0))::numeric, 3),
       CASE WHEN auth THEN round((0.7 + 0.2 * sin(e / 600.0))::numeric, 3) END,
       CASE WHEN auth THEN round((0.75 + 0.15 * sin(e / 500.0))::numeric, 3) END,
       (motion <> 'absent')::int,
       n,
       cal::int,
       CASE WHEN cal THEN 0 END,
       CASE WHEN motion = 'absent' THEN 'empty' WHEN ph = 'sleep' THEN 'bedroom'
            WHEN ph = 'study' THEN 'study' ELSE 'living_room' END,
       0.7,
       off::int,
       3,
       CASE WHEN off THEN 0 WHEN pg_temp.node2_down(t) THEN 2 ELSE 3 END,
       (t >= now() - interval '17 days' AND t < now() - interval '17 days' + interval '10 seconds')::int,
       CASE WHEN off OR auth OR motion = 'absent' THEN NULL
            WHEN NOT cal THEN 'explicit_calibration_required'
            WHEN n <> 1 THEN 'exactly_one_occupant_required'
            ELSE 'vital_quality_gate_failed' END,
       (auth OR off)::int,
       cal::int,
       e * 10,
       e
FROM tick, LATERAL (SELECT NOT off AND cal AND n = 1 AND motion = 'present_still' AS auth) AS a;

-- ── ruview/nodes/<id>, every minute ────────────────────────────────────────
INSERT INTO ingest.node (time, node_id, rssi_dbm, csi_fps, stale, last_seen_ms, motion_level,
                         person_score, subcarrier_count)
SELECT t, k::text,
       round((CASE k WHEN 1 THEN -58 WHEN 2 THEN -63 ELSE -71 END + 2.5 * sin(e / (60.0 + k))) ::numeric, 1),
       CASE WHEN down THEN 0 ELSE round((28.5 + 1.5 * sin(e / (900.0 * k)))::numeric, 2) END,
       down::int,
       CASE WHEN down THEN 60000 ELSE 40 END,
       CASE WHEN down THEN NULL WHEN here THEN 'present_still' ELSE 'absent' END,
       CASE WHEN down THEN NULL WHEN here THEN 0.8 ELSE 0.1 END,
       CASE WHEN down THEN NULL ELSE 64 END
FROM generate_series(now() - interval '35 days', now(), interval '1 minute') AS t,
     generate_series(1, 3) AS k,
     LATERAL (SELECT extract(epoch FROM t) AS e,
                     pg_temp.csi_offline(t) OR (k = 2 AND pg_temp.node2_down(t)) AS down,
                     CASE pg_temp.phase(t) WHEN 'sleep' THEN 1 WHEN 'study' THEN 2
                                           WHEN 'away' THEN 0 ELSE 3 END = k AS here) AS x;

-- ── ruview/person/<slot>, every 30 s, last 2 days ──────────────────────────
INSERT INTO ingest.person (time, person_key, zone, label, occupied, track_id, confidence, x, y, z)
SELECT t, k::text,
       CASE WHEN occ THEN CASE ph WHEN 'sleep' THEN 'bedroom' WHEN 'study' THEN 'study' ELSE 'living' END END,
       NULL,
       occ::int,
       CASE WHEN occ THEN floor(e / 3600) * 10 + k END,
       CASE WHEN occ THEN 0.6 END,
       CASE WHEN occ THEN round((1.5 * sin(e / 300.0))::numeric, 2) END,
       CASE WHEN occ THEN round((2 + cos(e / 300.0))::numeric, 2) END,
       CASE WHEN occ THEN 1.0 END
FROM pg_temp.times(interval '30 seconds', interval '30 seconds', interval '2 days') AS t,
     generate_series(1, 2) AS k,
     LATERAL (SELECT extract(epoch FROM t) AS e, pg_temp.phase(t) AS ph) AS x,
     LATERAL (SELECT ph <> 'away' AND (k = 1 OR ph = 'evening') AS occ) AS y;

-- ── built-in publisher: binary states per node ─────────────────────────────
INSERT INTO ingest.ha_binary (time, device, entity, value, "on")
SELECT t, 'wifi_densepose_ruview-node' || k, ent, CASE WHEN v THEN 'ON' ELSE 'OFF' END, v::int
FROM pg_temp.times(interval '2 minutes', interval '1 minute') AS t,
     generate_series(1, 3) AS k,
     unnest(ARRAY['presence', 'someone_sleeping', 'room_active', 'possible_distress']) AS ent,
     LATERAL (SELECT pg_temp.phase(t) AS ph) AS x,
     LATERAL (SELECT CASE ent
                 WHEN 'presence' THEN (k = 1 AND ph = 'sleep') OR (k = 2 AND ph = 'study')
                                   OR (k = 3 AND ph IN ('morning', 'evening'))
                 WHEN 'someone_sleeping' THEN k = 1 AND ph = 'sleep'
                                           AND extract(minute FROM t) NOT BETWEEN 10 AND 14
                 WHEN 'room_active' THEN (k = 3 AND ph = 'evening') OR (k = 2 AND ph = 'study'
                                           AND sin(extract(epoch FROM t) / 53.0) > 0.6)
                 ELSE false END AS v) AS y;

-- ── built-in publisher: values per node, every minute (10 min before) ──────
INSERT INTO ingest.ha_sensor (time, tags, fields)
SELECT t, jsonb_build_object('device', 'wifi_densepose_ruview-node' || k, 'entity', ent),
       CASE ent
           WHEN 'heart_rate' THEN jsonb_build_object('bpm', round((64 + 6 * sin(e / 1900.0))::numeric, 1),
                                                     'confidence', 0.6, 'ts', e)
           WHEN 'breathing_rate' THEN jsonb_build_object('bpm', round((15 + 2 * sin(e / 1700.0))::numeric, 1),
                                                         'confidence', 0.6, 'ts', e)
           WHEN 'person_count' THEN jsonb_build_object('n_persons', CASE WHEN here THEN 1 ELSE 0 END, 'ts', e)
           WHEN 'motion_level' THEN jsonb_build_object('level_pct', CASE WHEN here THEN 30 ELSE 2 END, 'ts', e)
           WHEN 'presence_score' THEN jsonb_build_object('score_pct', CASE WHEN here THEN 85 ELSE 5 END, 'ts', e)
           WHEN 'rssi' THEN jsonb_build_object('dbm', CASE k WHEN 1 THEN -58 WHEN 2 THEN -63 ELSE -71 END, 'ts', e)
           ELSE jsonb_build_object('score', round((0.05 + 0.03 * sin(e / 777.0))::numeric, 3), 'ts', e) END
FROM pg_temp.times(interval '10 minutes', interval '1 minute', interval '14 days') AS t,
     generate_series(1, 3) AS k,
     unnest(ARRAY['heart_rate', 'breathing_rate', 'person_count', 'motion_level', 'presence_score',
                  'rssi', 'fall_risk_elevated']) AS ent,
     LATERAL (SELECT extract(epoch FROM t) AS e,
                     CASE pg_temp.phase(t) WHEN 'sleep' THEN 1 WHEN 'study' THEN 2
                                           WHEN 'away' THEN 0 ELSE 3 END = k AS here) AS x;

-- ── built-in publisher: events (bed exits each morning, one fall) ──────────
INSERT INTO ingest.ha_event (time, tags, fields)
SELECT d + interval '6 hours 55 minutes',
       '{"device": "wifi_densepose_ruview-node1", "entity": "bed_exit"}'::jsonb,
       '{"event_type": "bed_exit", "confidence": 0.7}'::jsonb
FROM generate_series(date_trunc('day', now() AT TIME ZONE :tz) - interval '34 days',
                     date_trunc('day', now() AT TIME ZONE :tz), interval '1 day') AS l(day),
     LATERAL (SELECT l.day AT TIME ZONE :tz AS d) AS x
WHERE d + interval '6 hours 55 minutes' < now()
UNION ALL
SELECT now() - interval '17 days', '{"device": "wifi_densepose_ruview-node3", "entity": "fall"}',
       '{"event_type": "fall", "confidence": 0.35}'
UNION ALL
SELECT now() - interval '5 hours', '{"device": "wifi_densepose_ruview-node2", "entity": "multi_room_transition"}',
       '{"event_type": "multi_room_transition"}';

-- ── ruview/bridge, every 10 s (5 min before) ───────────────────────────────
INSERT INTO ingest.bridge (time, tags, fields)
SELECT t, '{}',
       jsonb_build_object('ws_connected', 1, 'source', CASE WHEN off THEN 'esp32:offline' ELSE 'esp32' END,
                          'stale_source', off::int, 'msgs_in_hz', 10.0,
                          'new_ticks_hz', CASE WHEN off THEN 0.0 ELSE 9.9 END,
                          'duplicates_hz', CASE WHEN off THEN 10.0 ELSE 0.1 END,
                          'state_published_hz', CASE WHEN off THEN 0.0 ELSE 9.9 END,
                          'state_throttled_hz', 0.0, 'edge_vitals_in_hz', 1.0, 'state_max_hz', 10.0,
                          'last_new_tick_age_s', CASE WHEN off THEN 300.0 ELSE 0.1 END)
FROM pg_temp.times(interval '5 minutes', interval '10 seconds') AS t,
     LATERAL (SELECT pg_temp.csi_offline(t) AS off) AS x;

-- ── ruview/watchdog/metrics, every 30 s (5 min before) ─────────────────────
INSERT INTO ingest.watchdog (time, tags, fields)
SELECT t, '{}',
       jsonb_build_object('status_level', CASE WHEN problems > 0 THEN 2 ELSE 0 END,
                          'open_issues', problems, 'critical', 0, 'warning', problems, 'info', 0,
                          'check_server', 0, 'check_api', 0, 'check_bridge', 0,
                          'check_source', off::int, 'check_node_1', off::int,
                          'check_node_2', (off OR n2)::int, 'check_node_3', off::int,
                          'check_vitals_frozen', 0, 'check_vitals_range', 0,
                          'check_calibration', (NOT cal)::int, 'check_recorder', 0, 'ha_link', 1,
                          'autocal_collecting', 0)
       || CASE WHEN cal THEN jsonb_build_object('calibration_age_h',
                                                 round((extract(epoch FROM t - (now() - interval '3 days'))
                                                        / 3600 % 24)::numeric, 1))
               ELSE '{}' END
FROM pg_temp.times(interval '5 minutes', interval '30 seconds') AS t,
     LATERAL (SELECT pg_temp.csi_offline(t) AS off, pg_temp.node2_down(t) AS n2,
                     t >= now() - interval '3 days' AS cal) AS x,
     LATERAL (SELECT (off::int * 4) + n2::int + (NOT cal)::int AS problems) AS y;

-- ── ruview/recorder/status, every 30 s (5 min before) ──────────────────────
INSERT INTO ingest.recorder (time, tags, fields)
SELECT t, '{}',
       jsonb_build_object('recording', 1, 'datagrams_hz', CASE WHEN off THEN 0 ELSE 85.5 END,
                          'bytes_hz', CASE WHEN off THEN 0 ELSE 34200 END,
                          'kinds_hz_csi', CASE WHEN off THEN 0 ELSE 85.5 END,
                          'nodes_1_csi_hz', CASE WHEN off THEN 0 ELSE 28.5 END,
                          'nodes_2_csi_hz', CASE WHEN off OR pg_temp.node2_down(t) THEN 0 ELSE 28.5 END,
                          'nodes_3_csi_hz', CASE WHEN off THEN 0 ELSE 28.5 END,
                          'last_datagram_age_s', CASE WHEN off THEN 120 ELSE 0.05 END,
                          'ring_files', 72, 'ring_hours', least(72, extract(epoch FROM t - (now() - interval '35 days')) / 3600),
                          'ring_bytes', 8.9e9, 'ring_limit_hours', 72, 'ring_limit_gb', 20,
                          'disk_free_gb', round((1250 - extract(epoch FROM t - (now() - interval '35 days')) / 86400 * 1.5)::numeric, 1),
                          'disk_low', 0, 'dropped', 0, 'write_errors', 0, 'kernel_drops', 0,
                          'keep_pending', 0, 'keep_today_min', 2.5, 'keep_budget_min', 120)
FROM pg_temp.times(interval '5 minutes', interval '30 seconds') AS t,
     LATERAL (SELECT pg_temp.csi_offline(t) AS off) AS x;

-- ── label buttons, calibrations and kept clips ─────────────────────────────
INSERT INTO ingest.annotation (time, tags, fields) VALUES
    (now() - interval '20 hours', '{"kind": "tag", "label": "sleep", "action": "start", "person": "sq", "source": "ha"}',
     '{"value": 1}'),
    (now() - interval '12 hours', '{"kind": "tag", "label": "sleep", "action": "stop", "person": "sq", "source": "ha"}',
     '{"value": 1}'),
    (now() - interval '6 hours', '{"kind": "change", "label": "node_moved", "action": "mark", "person": "sq", "source": "ha"}',
     '{"value": 1, "text": "node 3 moved 20 cm left"}'),
    (now() - interval '3 days', '{"kind": "calibration", "label": "empty_room", "action": "done", "source": "watchdog"}',
     '{"value": 1, "text": "empty-room calibration with node 2"}');
INSERT INTO ingest.recorder_keep (time, tags, fields) VALUES
    (now() - interval '115 minutes', '{"status": "kept", "tag": "watchdog_vitals_frozen", "source": "watchdog"}',
     '{"id": "wd-vitals_frozen-1", "duration_s": 600, "file": "keep/wd-vitals_frozen-1.bin"}');

COMMIT;

-- The aggregates over the whole range (the refresh policies cover 3 days).
CALL refresh_continuous_aggregate('agg.state_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.state_1h', NULL, NULL);
CALL refresh_continuous_aggregate('agg.node_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.node_1h', NULL, NULL);
CALL refresh_continuous_aggregate('agg.ha_sensor_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.ha_sensor_1h', NULL, NULL);

-- Every row above is at most as new as the transaction's start, and the
-- refreshes take a minute or so: repeat the newest state row up to now, so
-- that the panels of the last 2 minutes have something to show.
INSERT INTO ingest.state
SELECT (jsonb_populate_record(NULL::ingest.state, to_jsonb(s) || jsonb_build_object('time', g))).*
FROM (SELECT * FROM ingest.state ORDER BY time DESC LIMIT 1) AS s,
     generate_series(s.time + interval '10 seconds', now(), interval '10 seconds') AS g;
