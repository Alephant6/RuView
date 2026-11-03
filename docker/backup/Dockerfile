# Nightly offsite backup (WP1.1): the TimescaleDB database and the Apple
# Health data, encrypted and uploaded by restic. See README.md.

FROM restic/restic:0.19.1 AS restic

# paho-mqtt (status message) is pure Python: the wheel installed here runs on
# the Python of the final image.
FROM python:3.12-alpine AS deps
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --target /deps -r /tmp/requirements.txt

# The timescaledb image brings pg_dump and pg_restore of the server's own
# version, Python and bash. Keep the tag in step with the timescaledb
# service in docker-compose.yml: pg_dump refuses a newer server.
FROM timescale/timescaledb:2.30.2-pg17

COPY --from=restic /usr/bin/restic /usr/local/bin/restic
COPY --from=deps /deps /app/deps
COPY ruview_backup /app/ruview_backup
COPY restore-db.sh /app/restore-db.sh

# World-readable even when the host umask is 077 (see docker/bridge/Dockerfile).
RUN chmod -R a+rX /app && chmod a+x /app/restore-db.sh \
    && mkdir -p /backup && chown nobody:nobody /backup

# restic run by hand (README.md) shares the service's cache.
ENV PYTHONPATH=/app:/app/deps \
    PYTHONUNBUFFERED=1 \
    HOME=/backup \
    RESTIC_CACHE_DIR=/backup/cache
WORKDIR /app
USER nobody

ENTRYPOINT ["python3", "-m", "ruview_backup"]
