# Keep the tag in step with the timescaledb service in docker-compose.yml.
FROM timescale/timescaledb:2.30.2-pg17

# db-migrate. migrate.sh and the migrations live in the image, not in a bind
# mount: on the home NAS the checkout's files are readable only by their
# owner and root, and the job runs as postgres (found at the 2026-10-16
# deploy). The image tag also pins the schema version it applies. Built by
# .github/workflows/fork-images.yml as ghcr.io/<owner>/ruview-db-migrate.
COPY migrate.sh /migrate/migrate.sh
COPY migrations /migrate/migrations

# World-readable even when the host umask is 077 (see docker/bridge/Dockerfile).
RUN chmod -R a+rX /migrate

USER postgres

ENTRYPOINT ["bash", "/migrate/migrate.sh"]
