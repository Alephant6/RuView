FROM python:3.12-slim
WORKDIR /app
COPY ingest.py /app/ingest.py
RUN chmod 0644 /app/ingest.py
ENV PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8095
ENTRYPOINT ["python", "/app/ingest.py"]
