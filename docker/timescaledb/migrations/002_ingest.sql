-- WP1.1: L1 tables that Telegraf fills (docker/telegraf/telegraf.conf).
--
-- Telegraf may only insert, so every table and column it writes is defined
-- here. A table name is the Telegraf measurement name; a column is a field or
-- tag name. Column types follow Telegraf's: JSON numbers are double
-- precision, JSON booleans arrive as integers 1/0 (bigint), strings are text.
-- Tag columns carry the comment 'tag', as Telegraf's own tables do.
--
-- The high-rate topics get typed columns. The low-rate ones keep their tags
-- and fields as JSONB, so a new field in a payload is stored without a
-- migration.
--
-- Retention (section 4.2 of the architecture plan): the high-rate tables keep
-- 14 days and the L2 aggregates in 003 keep the long-term record; health
-- telemetry keeps 90 days; events, annotations and kept-clip results are
-- kept forever. Chunks older than a day are compressed.

-- ── ruview/state (mqtt-bridge), one row per new server tick, <= 10 Hz ──────
CREATE TABLE IF NOT EXISTS ingest.state (
    time                      timestamptz NOT NULL,
    source                    text,
    motion_level              text,
    presence                  bigint,
    confidence                double precision,
    motion_energy             double precision,
    breathing_rate_bpm        double precision,
    heart_rate_bpm            double precision,
    signal_quality            double precision,
    hr_conf                   double precision,
    br_conf                   double precision,
    vitals_present            bigint,
    n_persons                 double precision,
    calibrated                bigint,
    cal_person_count          double precision,
    room_class                text,
    room_conf                 double precision,
    stale_source              bigint,
    active_nodes              double precision,
    fresh_nodes               double precision,
    fall_detected             bigint,
    abstention_reason         text,
    numeric_vitals_authorized bigint,
    calibration_fresh         bigint,
    tick                      double precision,
    ts                        double precision
);
COMMENT ON TABLE ingest.state IS 'ruview/state: room-level sensing state per server tick (mqtt-bridge)';
COMMENT ON COLUMN ingest.state.source IS 'tag';
COMMENT ON COLUMN ingest.state.motion_level IS 'tag';

-- ── ruview/nodes/<id>, <= 1 Hz per node ────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest.node (
    time             timestamptz NOT NULL,
    node_id          text,
    rssi_dbm         double precision,
    csi_fps          double precision,
    stale            bigint,
    last_seen_ms     double precision,
    motion_level     text,
    person_score     double precision,
    subcarrier_count double precision,
    position_0       double precision,
    position_1       double precision,
    position_2       double precision
);
COMMENT ON TABLE ingest.node IS 'ruview/nodes/<id>: per-node health (mqtt-bridge)';
COMMENT ON COLUMN ingest.node.node_id IS 'tag';

-- ── ruview/person/<key>, on change plus a 30 s heartbeat ───────────────────
CREATE TABLE IF NOT EXISTS ingest.person (
    time       timestamptz NOT NULL,
    person_key text,
    zone       text,
    label      text,
    occupied   bigint,
    track_id   double precision,
    confidence double precision,
    x          double precision,
    y          double precision,
    z          double precision
);
COMMENT ON TABLE ingest.person IS 'ruview/person/<slot or label>: person slots (mqtt-bridge)';
COMMENT ON COLUMN ingest.person.person_key IS 'tag';
COMMENT ON COLUMN ingest.person.zone IS 'tag';
COMMENT ON COLUMN ingest.person.label IS 'tag';

-- ── sensing-server built-in publisher: binary states (ON/OFF) ──────────────
-- someone_sleeping, possible_distress, room_active, presence, ... per node;
-- on change plus a 60 s heartbeat.
CREATE TABLE IF NOT EXISTS ingest.ha_binary (
    time   timestamptz NOT NULL,
    device text,
    entity text,
    value  text,
    "on"   bigint
);
COMMENT ON TABLE ingest.ha_binary IS 'homeassistant/binary_sensor/wifi_densepose_*/<entity>/state: semantic states (sensing-server, ADR-115)';
COMMENT ON COLUMN ingest.ha_binary.device IS 'tag';
COMMENT ON COLUMN ingest.ha_binary.entity IS 'tag';

-- ── JSONB tables: (time, tags, fields) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest.ha_sensor (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.ha_sensor IS 'homeassistant/sensor/wifi_densepose_*/<entity>/state: per-node values (sensing-server, ADR-115); tags device, entity';
CREATE TABLE IF NOT EXISTS ingest.ha_event (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.ha_event IS 'homeassistant/event/wifi_densepose_*/<entity>/state: fall, bed_exit, ... (sensing-server)';
CREATE TABLE IF NOT EXISTS ingest.bridge (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.bridge IS 'ruview/bridge: mqtt-bridge stream health, every 10 s';
CREATE TABLE IF NOT EXISTS ingest.watchdog (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.watchdog IS 'ruview/watchdog/metrics: one row per watchdog round (check_<id> = 1 problem, 0 ok)';
CREATE TABLE IF NOT EXISTS ingest.recorder (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.recorder IS 'ruview/recorder/status: csi-recorder health, every 30 s';
CREATE TABLE IF NOT EXISTS ingest.recorder_keep (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.recorder_keep IS 'ruview/recorder/keep/result: raw CSI keep requests and kept clips; tags status, tag, source';
CREATE TABLE IF NOT EXISTS ingest.annotation (time timestamptz NOT NULL, tags jsonb, fields jsonb);
COMMENT ON TABLE ingest.annotation IS 'ruview/annotation: HA label buttons at press time; tags kind, label, action, person, source; fields value, text';

-- ── hypertables, compression, retention ────────────────────────────────────
-- (table, chunk interval, compress segment-by or NULL, compress after or
-- NULL, drop after or NULL = keep forever). Segmenting by the series key
-- (node, entity, the whole tags object) keeps similar values together.
-- Late rows can still be inserted into compressed chunks.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES
        ('ingest.state',         '1 day'::interval,  NULL::text,   '1 day'::interval, '14 days'::interval),
        ('ingest.node',          '1 day',            'node_id',    '1 day',           '14 days'),
        ('ingest.person',        '1 day',            'person_key', '1 day',           '14 days'),
        ('ingest.ha_sensor',     '1 day',            'tags',       '1 day',           '14 days'),
        ('ingest.ha_binary',     '7 days',           'device, entity', '7 days',      '90 days'),
        ('ingest.bridge',        '7 days',           NULL,         '7 days',          '90 days'),
        ('ingest.watchdog',      '7 days',           NULL,         '7 days',          '90 days'),
        ('ingest.recorder',      '7 days',           NULL,         '7 days',          '90 days'),
        ('ingest.ha_event',      '30 days',          NULL,         NULL,              NULL),
        ('ingest.recorder_keep', '30 days',          NULL,         NULL,              NULL),
        ('ingest.annotation',    '30 days',          NULL,         NULL,              NULL)
    ) AS v(tbl, chunk, segmentby, compress_after, drop_after) LOOP
        PERFORM create_hypertable(t.tbl::regclass, by_range('time', t.chunk), if_not_exists => true);
        IF t.compress_after IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %s SET (timescaledb.compress, timescaledb.compress_orderby = %L%s)',
                           t.tbl, 'time DESC',
                           CASE WHEN t.segmentby IS NULL THEN ''
                                ELSE format(', timescaledb.compress_segmentby = %L', t.segmentby) END);
            PERFORM add_compression_policy(t.tbl::regclass, t.compress_after, if_not_exists => true);
        END IF;
        IF t.drop_after IS NOT NULL THEN
            PERFORM add_retention_policy(t.tbl::regclass, t.drop_after, if_not_exists => true);
        END IF;
    END LOOP;
END
$$;

-- Default privileges cover tables created after 001; repeat for clarity.
GRANT INSERT ON ALL TABLES IN SCHEMA ingest TO telegraf;
GRANT SELECT ON ALL TABLES IN SCHEMA ingest TO analytics, grafana;
