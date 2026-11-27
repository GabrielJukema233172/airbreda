---
title: AirBreda — Runbook
---

# AirBreda — Runbook

How to run, deploy, retrain, check, back up and shut down AirBreda. It replaces the four
per-day setup files (Days 1–4). *Why* things are built this way is in the
[Architecture Design Document](index.md) and its ADRs.

Laptop commands are **PowerShell**, run from the repository root. Blocks marked `bash` run
**on the VM** after `ssh`.

```powershell
$Key = "$env:USERPROFILE\.ssh\airbreda-day3.pem"; $Vm = "ec2-user@16.192.119.40"
```

## 1. What is deployed

| Resource | Name / ID | Notes |
|---|---|---|
| Region | **eu-north-1** (Stockholm) | The only region the account's organization policy allows for EC2/RDS (ADR-0004) |
| EC2 instance | `airbreda-vm` · `i-0dd1b70fd3b8badb5` | t3.small (2 GB), Amazon Linux 2023, 20 GB gp3, termination protection on, CPU credits *standard*, IMDSv2 with hop limit 2 |
| Elastic IP | `eipalloc-0d59a0f26157f73cf` → **16.192.119.40** | Fixed address; **bills while the VM is stopped**, so release it when done (§11) |
| Security group | `airbreda-vm-sg` · `sg-00f41cf700c384efa` | TCP 22 from the owner's IPs only; TCP 8000 from the owner's IPs **and 0.0.0.0/0 until the Day 5 evaluation** |
| Containers | `db`, `air-ingest`, `traffic-ingest`, `dashboard` | `docker run --restart unless-stopped` on network `airbreda` ([`infra/vm/deploy.sh`](https://github.com/GabrielJukema233172/airbreda/blob/main/infra/vm/deploy.sh)) |
| S3 bucket | `airbreda-raw-730631` | Public access blocked. `ndw/YYYY-MM-DD/HH-<site>.csv` raw traffic, `backups/postgres/YYYY-MM-DD.sql.gz` nightly dumps |
| IAM role | `airbreda-ec2-role` (instance profile `airbreda-ec2-profile`) | Put/Get on `ndw/*` + `backups/*`, List on those prefixes, no Delete ([`infra/aws/`](https://github.com/GabrielJukema233172/airbreda/tree/main/infra/aws)) |
| DB logins | `airbreda` (owner, ingesters), `dashboard_ro` (SELECT only) | Passwords only in `~/airbreda/.env` on the VM (mode 600) |
| Budget | `airbreda-monthly` | $11 (≈ €10)/month, email at 80% actual / 100% forecast |
| SSH key | `airbreda-day3` → `%USERPROFILE%\.ssh\airbreda-day3.pem` | Never commit it (`*.pem` is git-ignored) |

Endpoints: **http://16.192.119.40:8000/** (dashboard), `/site/{hrl|hrr|vwd|vwa}`, `/health`.
On the VM only: `127.0.0.1:8001/health` (air), `127.0.0.1:8002/health` (traffic).

## 2. Checks: is everything working?

```powershell
curl.exe http://16.192.119.40:8000/health        # "status": "ok", both sources + database
curl.exe http://16.192.119.40:8000/site/hrl      # real NO2, intensity, prediction, "errors": []
ssh -i $Key $Vm
```

```bash
docker ps                                        # 4 containers, all "healthy"
docker logs --tail 20 traffic-ingest             # one JSON object per line
docker logs traffic-ingest 2>&1 | grep DATA_QUALITY_ERROR     # NDW -1 values (speed at night is normal)
docker exec db psql -U airbreda -d airbreda -c "SELECT component, COUNT(*), COUNT(value), SUM(is_flagged::int) AS flagged, MAX(timestamp) FROM sensor_readings GROUP BY component;"
aws s3 ls s3://airbreda-raw-730631/ndw/ --recursive | tail -4
docker stats --no-stream
```

The ingesters run one cycle at start and then one every hour. `docker restart air-ingest` forces an extra cycle.

## 3. Local development (laptop)

`.env` in the repo root (git-ignored; template: `.env.example`):

```
POSTGRES_PASSWORD=<letters and digits>
S3_BUCKET=airbreda-raw-730631
```

```powershell
docker compose up -d --build       # db, redis, air-ingest, traffic-ingest, dashboard
start http://localhost:8000
curl.exe http://localhost:8001/health; curl.exe http://localhost:8002/health
docker compose stop                # data is kept in the volumes
```

Locally, NO₂ comes from the laptop's own Postgres and traffic from the real bucket (your AWS
CLI credentials are mounted read-only). Note that a local `traffic-ingest` also writes to the
real bucket. The local stack still contains the Day 2 Redis queue (`docker exec -it redis redis-cli LLEN readings`);
the VM doesn't (ADR-0004).

**Tests** (50):

```powershell
docker run --rm -v "${PWD}:/app" -w /app python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && pytest"
```

## 4. Deploy a code change to the VM

```powershell
tar czf $env:TEMP\airbreda-vm.tgz --exclude=__pycache__ --exclude=ml/*.csv --exclude=*.png services sql infra/vm ml .gitattributes
scp -i $Key $env:TEMP\airbreda-vm.tgz "${Vm}:~/"
ssh -i $Key $Vm "tar xzf ~/airbreda-vm.tgz -C ~/airbreda && rm ~/airbreda-vm.tgz && cd ~/airbreda && infra/vm/deploy.sh"
```

`deploy.sh`:
- creates the `dashboard_ro` login on first run;
- rebuilds the three images;
- keeps `db` and its volume;
- recreates `air-ingest`, `traffic-ingest` and `dashboard`.

The dashboard only gets `DASHBOARD_DATABASE_URL`, `S3_BUCKET` and the region, never the owner password.

## 5. Retrain the model

```powershell
.\infra\retrain.ps1               # build data on the VM -> train -> 50 tests -> deploy -> smoke test
.\infra\retrain.ps1 -SkipDeploy   # stop after the tests
```

Then commit `services/dashboard/model.pkl`, `ml/training_data.csv`, `ml/metrics.json` and
`ml/no2_vs_traffic.png` **together**: the deployed model must stay traceable to its data (ADR-0006).
The steps by hand:
1. `infra/vm/build-training-data.sh` on the VM;
2. `scp` the CSV back;
3. `python ml/train_model.py` in `python:3.12-slim` with `ml/requirements.txt`;
4. `pytest`;
5. §4.

## 6. Backups and restore

Nightly at 02:15 UTC, from `ec2-user`'s crontab:

```bash
crontab -l    # 15 2 * * * /home/ec2-user/airbreda/infra/vm/backup.sh >> /home/ec2-user/airbreda/backup.log 2>&1
tail -3 ~/airbreda/backup.log
```

Restore test (done on Day 3: 125 of 125 rows):

```bash
KEY=backups/postgres/$(date -u +%F).sql.gz
docker exec db psql -U airbreda -d airbreda -c "CREATE DATABASE restore_test"
aws s3 cp s3://airbreda-raw-730631/$KEY - | gunzip | docker exec -i db psql -q -U airbreda -d restore_test
docker exec db psql -U airbreda -d restore_test -c "SELECT COUNT(*) FROM sensor_readings"
docker exec db psql -U airbreda -d airbreda -c "DROP DATABASE restore_test"
```

A real restore into `airbreda` is the same pipe with `-d airbreda` (the dump contains `DROP ... IF EXISTS`).

## 7. Network access

**SSH times out after moving to another network?** Your public IP changed. Add the new one:

```powershell
$Ip = (Invoke-RestMethod https://checkip.amazonaws.com).Trim() + "/32"
aws ec2 authorize-security-group-ingress --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 22 --cidr $Ip
aws ec2 describe-security-groups --region eu-north-1 --group-ids sg-00f41cf700c384efa --query "SecurityGroups[0].IpPermissions[].[FromPort,join(',',IpRanges[].CidrIp)]" --output text
```

**Port 8000 after the evaluation**: close it to the public again (your own IPs keep access):

```powershell
aws ec2 revoke-security-group-ingress --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 8000 --cidr 0.0.0.0/0
```

## 8. Costs

```powershell
aws ce get-cost-and-usage --time-period Start=2026-11-01,End=2026-12-01 --granularity MONTHLY --metrics UnblendedCost
aws freetier get-account-plan-state --region us-east-1      # remaining Free plan credit
```

Expect ≈ $0.70/day (≈ $21/month) while the VM runs: t3.small + 20 GB gp3 + public IPv4 + S3 cents.
It is paid from the Free plan credit. Cost Explorer lags by up to a day. The budget's *forecast* alert
fires on purpose, as a reminder to stop the VM after the course.

## 9. Rebuild from scratch

Every resource above was created with the AWS CLI. In order:

```powershell
$R = "eu-north-1"
# Key pair: create it from Git Bash with `> file`. PowerShell's pipeline joins the key's lines and breaks it.
#   aws ec2 create-key-pair --region eu-north-1 --key-name airbreda-day3 --key-type ed25519 --query KeyMaterial --output text > ~/.ssh/airbreda-day3.pem
icacls $Key /inheritance:r /grant:r "$($env:USERNAME):R"                       # = chmod 400

$MyIp = (Invoke-RestMethod https://checkip.amazonaws.com).Trim()
$Vpc  = aws ec2 describe-vpcs --region $R --filters Name=is-default,Values=true --query "Vpcs[0].VpcId" --output text
$Sg   = aws ec2 create-security-group --region $R --group-name airbreda-vm-sg --description "AirBreda VM" --vpc-id $Vpc --query GroupId --output text
aws ec2 authorize-security-group-ingress --region $R --group-id $Sg --protocol tcp --port 22   --cidr "$MyIp/32"
aws ec2 authorize-security-group-ingress --region $R --group-id $Sg --protocol tcp --port 8000 --cidr "$MyIp/32"

$Bucket = "airbreda-raw-" + (Get-Random -Minimum 100000 -Maximum 999999)
aws s3 mb "s3://$Bucket" --region $R                 # public access is blocked by default
# put the new bucket name into infra/aws/airbreda-s3-policy.json and infra/vm/env.example first
aws iam create-role --role-name airbreda-ec2-role --assume-role-policy-document file://infra/aws/ec2-trust-policy.json
aws iam put-role-policy --role-name airbreda-ec2-role --policy-name airbreda-s3-raw-and-backups --policy-document file://infra/aws/airbreda-s3-policy.json
aws iam create-instance-profile --instance-profile-name airbreda-ec2-profile
aws iam add-role-to-instance-profile --instance-profile-name airbreda-ec2-profile --role-name airbreda-ec2-role

$Ami = aws ec2 describe-images --region $R --owners amazon --filters "Name=name,Values=al2023-ami-2023*-kernel-*-x86_64" --query "sort_by(Images,&CreationDate)[-1].ImageId" --output text
aws ec2 run-instances --region $R --image-id $Ami --instance-type t3.small --key-name airbreda-day3 `
  --security-group-ids $Sg --iam-instance-profile Name=airbreda-ec2-profile `
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3}" `
  --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled" `
  --credit-specification CpuCredits=standard --disable-api-termination `
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=airbreda-vm}]"
# then: aws ec2 allocate-address + associate-address for a fixed IP
```

On the VM:

```bash
sudo dnf update -y && sudo dnf install -y docker cronie       # AL2023 has no cron by default
sudo systemctl enable --now docker crond && sudo usermod -aG docker ec2-user   # log out and in again
mkdir -p ~/airbreda           # copy the code in (section 4), then:
cd ~/airbreda && sed "s/change-me-letters-and-digits-only/$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')/g" infra/vm/env.example > .env && chmod 600 .env
infra/vm/deploy.sh
echo "15 2 * * * /home/ec2-user/airbreda/infra/vm/backup.sh >> /home/ec2-user/airbreda/backup.log 2>&1" | crontab -
```

## 10. Known pitfalls (all hit during the build)

| Symptom | Cause / fix |
|---|---|
| `explicit deny in a service control policy` | Wrong region: only eu-north-1 is allowed |
| SSH `Connection timed out` | Your IP changed (§7), or the laptop's network dropped. The VM keeps running regardless |
| `Load key ...: error in libcrypto` | Key file written by a PowerShell pipeline; recreate it with `> file` from Git Bash |
| `Invalid device name C:/Program Files/Git/dev/xvda` | Git Bash path conversion; prefix with `MSYS_NO_PATHCONV=1` |
| A script exits silently | `set -o pipefail` + `tr </dev/urandom \| head` (SIGPIPE); use `od -N16 /dev/urandom` |
| `crontab -l` is empty after installing | Under `set -e`, `grep -v` on an empty crontab exits 1; install with `echo ... \| crontab -` |
| boto3 finds no credentials in a container | IMDSv2 hop limit must be 2 |
| `curl` behaves oddly in PowerShell | Use `curl.exe` (plain `curl` is `Invoke-WebRequest`) |
| Dashboard shows no prediction for ~1 min after a cycle | Fixed: it uses the newest *complete* hour of traffic files (ADR-0006) |
| A pinned package goes missing or warns | Pins: `numpy==2.1.3` with pandas 2.2.3; `anyio==4.6.2.post1` (4.6.2 was yanked) |

## 11. After the course

```powershell
aws ec2 revoke-security-group-ingress --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 8000 --cidr 0.0.0.0/0
aws ec2 stop-instances --region eu-north-1 --instance-ids i-0dd1b70fd3b8badb5      # data kept; ~$1.67/month disk
aws ec2 disassociate-address --region eu-north-1 --association-id eipassoc-04aace55eda25b2b8
aws ec2 release-address --region eu-north-1 --allocation-id eipalloc-0d59a0f26157f73cf   # stops the IP billing
```

To delete everything:
1. Snapshot or download what you want to keep.
2. Turn off termination protection: `aws ec2 modify-instance-attribute --no-disable-api-termination ...`.
3. Terminate the instance.
4. Empty and delete the bucket.
5. Delete the role, the instance profile, the security group, the key pair and the budget.
