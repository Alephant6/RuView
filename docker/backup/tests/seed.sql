-- Sample rows for the backup test (.github/workflows/backup.yml), from
-- :from_s to :to_s seconds before now: the room state every 30 s, three
-- nodes and their heart-rate sensor every minute or two, label presses and an
-- event. Then it compresses the chunks older than a day and refreshes the
-- aggregates, as the policies would later, so that the database does not
-- change while the test compares it with its restored copy.
--   psql -v from_s=432000 -v to_s=7200 -f seed.sql

INSERT INTO ingest.state (time, source, motion_level, presence, confidence, motion_energy,
                          breathing_rate_bpm, heart_rate_bpm, signal_quality, hr_conf, br_conf,
                          vitals_present, n_persons, stale_source, numeric_vitals_authorized,
                          calibration_fresh, abstention_reason, room_class, tick, ts)
SELECT now() - make_interval(secs => s),
       'esp32',
       (ARRAY['absent', 'present_still', 'present_moving', 'active'])[1 + s / 3600 % 4],
       (s / 3600 % 4 > 0)::int,
       0.5 + s % 50 / 100.0,
       s % 997 / 10.0,
       12 + s % 9 / 3.0,
       58 + s % 31 + s % 7 / 10.0,
       s % 89 / 89.0,
       0.7,
       0.6,
       1,
       1 + s / 7200 % 2,
       (s % 600 = 0)::int,
       1,
       1,
       CASE WHEN s / 7200 % 2 = 1 THEN 'multiple_persons' END,
       'bedroom',
       s,
       extract(epoch FROM now()) - s
FROM generate_series(:to_s, :from_s, 30) AS s;

INSERT INTO ingest.node (time, node_id, rssi_dbm, csi_fps, stale, last_seen_ms, motion_level,
                         person_score, subcarrier_count)
SELECT now() - make_interval(secs => s), n::text, -38 - 4 * n - s % 13, 18 + s % 9 / 4.0, 0,
       s % 1000, 'present_still', s % 100 / 100.0, 64
FROM generate_series(:to_s, :from_s, 60) AS s, generate_series(1, 3) AS n;

INSERT INTO ingest.ha_sensor (time, tags, fields)
SELECT now() - make_interval(secs => s),
       jsonb_build_object('device', 'wifi_densepose_' || n, 'entity', 'heart_rate'),
       jsonb_build_object('bpm', 60 + s % 13 + n / 10.0, 'confidence', 0.8)
FROM generate_series(:to_s, :from_s, 120) AS s, generate_series(1, 3) AS n;

INSERT INTO ingest.annotation (time, tags, fields)
SELECT now() - make_interval(secs => :to_s + 600 * g),
       jsonb_build_object('kind', 'label', 'label', 'sleep', 'action', 'start', 'person', 'sq',
                          'source', 'ha'),
       jsonb_build_object('value', g, 'text', format('note %s: "quoted", a comma, 中文', g))
FROM generate_series(1, 3) AS g;

INSERT INTO ingest.ha_event (time, tags, fields)
SELECT now() - make_interval(secs => :to_s + 300),
       '{"device": "wifi_densepose_1", "entity": "bed_exit"}', '{"event_type": "bed_exit"}';

SELECT count(compress_chunk(c, if_not_compressed => true)) AS compressed_chunks
FROM unnest(ARRAY['ingest.state', 'ingest.node', 'ingest.ha_sensor']::regclass[]) AS h,
     LATERAL show_chunks(h, older_than => INTERVAL '1 day') AS c;

CALL refresh_continuous_aggregate('agg.state_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.state_1h', NULL, NULL);
CALL refresh_continuous_aggregate('agg.node_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.node_1h', NULL, NULL);
CALL refresh_continuous_aggregate('agg.ha_sensor_1m', NULL, NULL);
CALL refresh_continuous_aggregate('agg.ha_sensor_1h', NULL, NULL);
