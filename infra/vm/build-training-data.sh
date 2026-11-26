#!/usr/bin/env bash
# AirBreda - Day 4: build ml/training_data.csv ON THE VM, where the database lives.
#
#   cd ~/airbreda && infra/vm/build-training-data.sh
#   # then, on the laptop:  scp ...:~/airbreda/ml/training_data.csv ml/
#
# A one-off container on the airbreda network: it reaches Postgres as db:5432
# and S3 through the VM's IAM role (ListBucket on ndw/*, GetObject on ndw/*).
set -euo pipefail
cd "$(dirname "$0")/../.."
docker run --rm --network airbreda --env-file .env \
  -v "$PWD:/work" -w /work python:3.12-slim \
  sh -c "pip install -q --root-user-action=ignore -r ml/requirements.txt && python ml/build_training_data.py"
