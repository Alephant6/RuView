"""RuView nightly offsite backup (WP1.1).

Dumps the TimescaleDB database (pg_dump, plus the role definitions) and the
Apple Health measurements from InfluxDB, and uploads them with restic, which
encrypts them, to a remote repository (Backblaze B2 through its S3 API).
"""

__version__ = "0.1.0"
