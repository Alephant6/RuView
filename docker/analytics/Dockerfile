FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY ruview_analytics /app/ruview_analytics

# World-readable even when the host umask is 077 (see docker/bridge/Dockerfile).
RUN chmod -R a+rX /app

ENV PYTHONUNBUFFERED=1
USER nobody

ENTRYPOINT ["python", "-m", "ruview_analytics"]
