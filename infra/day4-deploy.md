# Day 4 — train the model, test the dashboard, deploy it

PowerShell on the laptop (from the `airbreda` folder) unless a block says `bash` (= on the VM).
VM details and SSH: [day3-aws-setup.md](day3-aws-setup.md). Why: [ADR-0005](../docs/adr/0005-compute-and-deployment-strategy.md),
[ADR-0006](../docs/adr/0006-ml-serving-architecture.md).

```powershell
$Key = "$env:USERPROFILE\.ssh\airbreda-day3.pem"; $Vm = "ec2-user@13.53.194.127"
```

## 1. Build the training data (on the VM, where the database is)

```powershell
tar czf $env:TEMP\airbreda-vm.tgz --exclude=__pycache__ services sql infra/vm ml .gitattributes
scp -i $Key $env:TEMP\airbreda-vm.tgz "${Vm}:~/"
ssh -i $Key $Vm "tar xzf ~/airbreda-vm.tgz -C ~/airbreda && ~/airbreda/infra/vm/build-training-data.sh"
scp -i $Key "${Vm}:~/airbreda/ml/training_data.csv" ml\
```

It prints how many rows were joined (17 on 26 Nov).

## 2. Train (on the laptop, in the same pinned environment the dashboard serves with)

```powershell
docker run --rm -v "${PWD}:/work" -w /work python:3.12-slim sh -c "pip install -q -r ml/requirements.txt && python ml/train_model.py"
```

Writes `services/dashboard/model.pkl`, `ml/metrics.json` and `ml/no2_vs_traffic.png`, and
prints R², MAE (in-sample and leave-one-out vs a mean baseline), the coefficients and a row-by-row table.

## 3. Tests

```powershell
docker run --rm -v "${PWD}:/app" -w /app python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && pytest"
```

Expect `47 passed`.

## 4. Test locally with Compose (before the VM)

`.env` must have `S3_BUCKET=airbreda-raw-730631`; your AWS CLI credentials are mounted read-only.

```powershell
docker compose up -d --build
curl.exe http://localhost:8000/site/hrl
curl.exe http://localhost:8000/health
start http://localhost:8000
docker compose stop          # when done
```

Locally, NO₂ comes from the laptop's own Postgres (filled by the local air ingester) and
traffic from the real bucket. The VM's database has no public endpoint (ADR-0001).

## 5. Deploy to the VM

```powershell
tar czf $env:TEMP\airbreda-vm.tgz --exclude=__pycache__ --exclude=ml/*.csv --exclude=*.png services sql infra/vm ml .gitattributes
scp -i $Key $env:TEMP\airbreda-vm.tgz "${Vm}:~/"
ssh -i $Key $Vm "tar xzf ~/airbreda-vm.tgz -C ~/airbreda && cd ~/airbreda && infra/vm/deploy.sh"
```

`deploy.sh`:
- creates the read-only `dashboard_ro` database login the first time (password generated into `~/airbreda/.env`);
- rebuilds all three images;
- recreates `air-ingest`, `traffic-ingest` and `dashboard` (the `db` container and its data are kept).

The dashboard gets only `DASHBOARD_DATABASE_URL`, `S3_BUCKET` and the region, never the owner password.

Open **http://13.53.194.127:8000** from an allowed IP.

## 6. Who may reach port 8000

```powershell
aws ec2 describe-security-groups --region eu-north-1 --group-ids sg-00f41cf700c384efa --query "SecurityGroups[0].IpPermissions[].[FromPort,IpRanges[].CidrIp]" --output text
# allow one more IP (e.g. the grader's on Day 5):
aws ec2 authorize-security-group-ingress --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 8000 --cidr <ip>/32
```

## 7. Checks

```bash
docker ps                                          # db, air-ingest, traffic-ingest, dashboard -> healthy
docker logs --tail 20 dashboard                    # JSON events + uvicorn access log
curl -s localhost:8000/health; echo                # both sources + database
docker stats --no-stream
```

After a reboot (`aws ec2 reboot-instances --region eu-north-1 --instance-ids i-0dd1b70fd3b8badb5`)
all four containers come back by themselves (tested 26 Nov).
