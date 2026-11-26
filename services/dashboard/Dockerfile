# The lab's Dockerfile.dashboard. Build from the repository root (it needs services/common):
#   docker build -f services/dashboard/Dockerfile -t airbreda-dashboard .
# python:3.12-slim (not the lab's 3.11) so all three images share one base and
# the model is served by the same Python it was trained with (ml/requirements.txt).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY services/dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/common ./common
# model.pkl is baked in: this image always serves exactly the model it was built with.
COPY services/dashboard/dashboard.py services/dashboard/predict.py services/dashboard/model.pkl ./

EXPOSE 8000
# Liveness of the web server only (GET / is static). /health is about the
# ingesters; an NDW outage must not get the dashboard marked unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=4)"

CMD ["uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "8000"]
