FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ndw_parser.py ingest_traffic.py ./

# Live NDW feeds are downloaded on every run; nothing is baked into the image.
# Pass S3_BUCKET (and AWS credentials) at run time to upload the CSVs.
CMD ["python", "ingest_traffic.py"]
