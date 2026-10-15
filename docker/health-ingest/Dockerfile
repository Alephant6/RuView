FROM python:3.12-slim
WORKDIR /app
COPY ingest.py import_export.py /app/
RUN chmod 0644 /app/ingest.py /app/import_export.py
ENV PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8095
ENTRYPOINT ["python", "/app/ingest.py"]
