FROM python:3.12-slim
WORKDIR /app
COPY ingest.py import_export.py /app/
RUN chmod 0644 /app/ingest.py /app/import_export.py
ENV PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8095/health',timeout=3)"
ENTRYPOINT ["python", "/app/ingest.py"]
