"""Print docker/telegraf/telegraf.conf for the ingest test: the InfluxDB
output becomes a file output (no InfluxDB in the test) and Telegraf flushes
every second. Everything else, the TimescaleDB outputs included, is unchanged.

    python telegraf_test_conf.py ../telegraf/telegraf.conf > /tmp/telegraf.conf
"""
import pathlib
import sys

conf = pathlib.Path(sys.argv[1]).read_text()
start = conf.index("[[outputs.influxdb_v2]]")
end = conf.index("\n# ─", start)
conf = conf[:start] + '[[outputs.file]]\n  files = ["/tmp/metrics.out"]\n  data_format = "influx"\n' + conf[end:]
if 'flush_interval = "10s"' not in conf:
    sys.exit("telegraf.conf: flush_interval not found")
sys.stdout.write(conf.replace('flush_interval = "10s"', 'flush_interval = "1s"'))
