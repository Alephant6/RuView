FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY csi_recorder /app/csi_recorder

# World-readable even when the host umask is 077 (see docker/bridge/Dockerfile).
# /data is the recording volume; a bind mount must be writable by uid 65534.
RUN chmod -R a+rX /app && mkdir -p /data && chown nobody:nogroup /data

ENV PYTHONUNBUFFERED=1
USER nobody

ENTRYPOINT ["python", "-m", "csi_recorder"]
