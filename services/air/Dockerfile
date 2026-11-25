# Build from the repository root (docker-compose.yml does this), because the
# image also needs the shared services/common package:
#   docker build -f services/air/Dockerfile -t airbreda-air .
FROM python:3.12-slim

# redis-cli: lets you check the broker from inside the container
# (docker exec air-ingest redis-cli -h redis ping)
RUN apt-get update \
 && apt-get install -y --no-install-recommends redis-tools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY services/air/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
COPY services/air/ingest_air.py ./

# GET /health
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["python", "ingest_air.py"]
