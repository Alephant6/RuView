FROM eclipse-mosquitto:2.0.22

# mosquitto.conf and start.sh live in the image, not in a bind mount: on the
# home NAS the checkout's files are readable only by their owner and root,
# and the broker runs as uid 1883 (found at the 2026-10-16 deploy). Built by
# .github/workflows/fork-images.yml as ghcr.io/<owner>/ruview-mosquitto.
COPY mosquitto.conf start.sh /mosquitto/config/

# World-readable even when the host umask is 077 (see docker/bridge/Dockerfile).
RUN chmod 0644 /mosquitto/config/mosquitto.conf /mosquitto/config/start.sh

USER 1883:1883

CMD ["sh", "/mosquitto/config/start.sh"]
