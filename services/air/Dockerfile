FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ingest_air.py .

# No data file is baked in: every run calls the live Luchtmeetnet API.
# Pass DATABASE_URL at run time to also write to Postgres.
CMD ["python", "ingest_air.py"]
