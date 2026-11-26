# Day 3 — EC2 VM, S3 and the stack in the cloud

All laptop commands are **PowerShell**, run from the `airbreda` folder. Commands in
`bash` blocks run **on the VM** (after `ssh`). Why each choice was made: [ADR-0004](../docs/adr/0004-compute-strategy.md).

## What exists (created 25 Nov 2026)

| Resource | Name / ID | Notes |
|---|---|---|
| Region | **eu-north-1** (Stockholm) | The only region the account's organization policy (SCP) allows for EC2/RDS |
| EC2 instance | `airbreda-vm` · `i-0dd1b70fd3b8badb5` · **16.192.119.40** | t3.small, Amazon Linux 2023, 20 GB gp3, termination protection on, CPU credits *standard* |
| Elastic IP | `airbreda-vm-eip` · `eipalloc-0d59a0f26157f73cf` · **16.192.119.40** | Attached 26 Nov (before that: 13.53.194.127, which changed on every stop/start). Same $0.005/h as any public IPv4 while attached, **but it keeps billing if the VM is stopped or the IP is detached**: release it when the project ends |
| Key pair | `airbreda-day3` → `C:\Users\gabri\.ssh\airbreda-day3.pem` | ed25519; readable only by you. Never commit it (`*.pem` is git-ignored) |
| Security group | `airbreda-vm-sg` · `sg-00f41cf700c384efa` | Inbound **TCP 22 from the owner's two IPs only** (86.80.42.249/32, 145.101.245.100/32). **TCP 8000** (dashboard): the same two IPs, plus **0.0.0.0/0 from 26 Nov until the Day 5 evaluation** (closing it: [day4-deploy.md §6](day4-deploy.md)) |
| S3 bucket | `airbreda-raw-730631` | Public access blocked; `ndw/…` raw CSVs, `backups/postgres/…` nightly dumps |
| IAM role | `airbreda-ec2-role` (+ instance profile `airbreda-ec2-profile`) | Inline policy [`aws/airbreda-s3-policy.json`](aws/airbreda-s3-policy.json): Put/Get on `ndw/*` and `backups/*` only |
| Budget | `airbreda-monthly` | $11 (≈ €10)/month, email at 80% actual and 100% forecast |

> Since 26 Nov the VM has an **Elastic IP**, so its address stays 16.192.119.40 across
> stop/start. (Without one, AWS hands out a new public IP on every start.)

## Daily use

```powershell
# Connect (Windows OpenSSH)
ssh -i $env:USERPROFILE\.ssh\airbreda-day3.pem ec2-user@16.192.119.40
```

```bash
docker ps                                   # db, air-ingest, traffic-ingest -> "healthy"
docker logs --tail 20 air-ingest            # structured JSON logs
docker logs --tail 20 traffic-ingest
curl -s localhost:8001/health; echo         # Luchtmeetnet
curl -s localhost:8002/health; echo         # NDW
docker exec db psql -U airbreda -d airbreda -c "SELECT component, COUNT(*), MAX(timestamp) FROM sensor_readings GROUP BY component;"
aws s3 ls s3://airbreda-raw-730631/ndw/ --recursive | wc -l
```

The ingesters run continuously and do one cycle at start plus one every hour, so
`docker restart air-ingest` forces an extra cycle.

**Your IP changed and SSH times out?** Replace the rule (PowerShell, on the laptop):

```powershell
$Old = "86.80.42.249/32"; $New = (Invoke-RestMethod https://checkip.amazonaws.com).Trim() + "/32"
aws ec2 revoke-security-group-ingress    --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 22 --cidr $Old
aws ec2 authorize-security-group-ingress --region eu-north-1 --group-id sg-00f41cf700c384efa --protocol tcp --port 22 --cidr $New
```

## Deploy a code change

```powershell
# On the laptop: package what the VM needs (no docker-compose.yml, no Redis)
tar czf $env:TEMP\airbreda-vm.tgz --exclude=__pycache__ services sql infra/vm .gitattributes
scp -i $env:USERPROFILE\.ssh\airbreda-day3.pem $env:TEMP\airbreda-vm.tgz ec2-user@16.192.119.40:~/
ssh -i $env:USERPROFILE\.ssh\airbreda-day3.pem ec2-user@16.192.119.40 "tar xzf ~/airbreda-vm.tgz -C ~/airbreda && ~/airbreda/infra/vm/deploy.sh"
```

[`vm/deploy.sh`](vm/deploy.sh) rebuilds both images, keeps the `db` container and its
volume, and recreates the two ingesters.

---

## How it was built (the steps, in order)

### Part 0 — find the allowed region

```powershell
aws ec2 describe-instances --region eu-central-1   # explicit deny in a service control policy
aws ec2 describe-instances --region eu-north-1     # works
```

Every region except eu-north-1 denies EC2 (checked with a loop over
`aws ec2 describe-regions`). `aws freetier get-account-plan-state --region us-east-1`
shows the Free plan and the remaining credit.

### Part 1 — the VM

```powershell
$R = "eu-north-1"

# Key pair. Do this from Git Bash or with `> file` redirection: PowerShell's
# pipeline joins the key's lines into one and breaks the file.
#   aws ec2 create-key-pair --region eu-north-1 --key-name airbreda-day3 --key-type ed25519 --query KeyMaterial --output text > ~/.ssh/airbreda-day3.pem
icacls $env:USERPROFILE\.ssh\airbreda-day3.pem /inheritance:r /grant:r "$($env:USERNAME):R"   # = chmod 400

# Security group: SSH from my IP only
$MyIp = (Invoke-RestMethod https://checkip.amazonaws.com).Trim()
$Vpc  = aws ec2 describe-vpcs --region $R --filters Name=is-default,Values=true --query "Vpcs[0].VpcId" --output text
$Sg   = aws ec2 create-security-group --region $R --group-name airbreda-vm-sg --description "AirBreda VM: SSH from owner IP only" --vpc-id $Vpc --query GroupId --output text
aws ec2 authorize-security-group-ingress --region $R --group-id $Sg --protocol tcp --port 22 --cidr "$MyIp/32"

# Latest Amazon Linux 2023 AMI
$Ami = aws ec2 describe-images --region $R --owners amazon --filters "Name=name,Values=al2023-ami-2023*-kernel-*-x86_64" --query "sort_by(Images,&CreationDate)[-1].ImageId" --output text

# Launch (after the IAM role in Part 2 exists)
aws ec2 run-instances --region $R --image-id $Ami --instance-type t3.small `
  --key-name airbreda-day3 --security-group-ids $Sg `
  --iam-instance-profile Name=airbreda-ec2-profile `
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3}" `
  --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled" `
  --credit-specification CpuCredits=standard --disable-api-termination `
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=airbreda-vm},{Key=Project,Value=airbreda}]"
```

- **t3.small, not t3.micro:** one NDW cycle peaks at ~700 MB RAM (measured), which doesn't fit next to Postgres and Docker on 1 GB.
- **`HttpPutResponseHopLimit=2`:** containers sit one network hop behind the VM. With the default of 1 they can't fetch the IAM role's credentials.
- **`--disable-api-termination`:** terminating deletes the root disk, and the database with it (ADR-0003).
- **`CpuCredits=standard`:** a t3 can't run up surplus-credit charges.

On the VM:

```bash
sudo dnf update -y
sudo dnf install -y docker cronie          # AL2023 has no cron by default
sudo systemctl enable --now docker crond
sudo usermod -aG docker ec2-user           # log out and back in
docker run --rm hello-world
```

### Part 2 — bucket and IAM role

```powershell
$Bucket = "airbreda-raw-" + (Get-Random -Minimum 100000 -Maximum 999999)
aws s3 mb "s3://$Bucket" --region eu-north-1
aws s3api get-public-access-block --bucket $Bucket     # all four: True

aws iam create-role --role-name airbreda-ec2-role --assume-role-policy-document file://infra/aws/ec2-trust-policy.json
aws iam put-role-policy --role-name airbreda-ec2-role --policy-name airbreda-s3-raw-and-backups --policy-document file://infra/aws/airbreda-s3-policy.json
aws iam create-instance-profile --instance-profile-name airbreda-ec2-profile
aws iam add-role-to-instance-profile --instance-profile-name airbreda-ec2-profile --role-name airbreda-ec2-role
```

Least privilege instead of `AmazonS3FullAccess`. Check it on the VM:

```bash
ls ~/.aws                                   # does not exist: no keys on the VM
aws sts get-caller-identity                 # ...assumed-role/airbreda-ec2-role/i-0dd1...
aws s3 cp /tmp/t.txt s3://airbreda-raw-730631/ndw/test.txt     # allowed
aws s3 cp /tmp/t.txt s3://airbreda-raw-730631/other/test.txt   # AccessDenied
```

### Part 3 — the stack (no docker compose, no Redis)

```bash
mkdir -p ~/airbreda          # then copy the files (see "Deploy a code change")
cd ~/airbreda
PW=$(tr -dc A-Za-z0-9 </dev/urandom | head -c 32)
sed "s/change-me-letters-and-digits-only/$PW/g" infra/vm/env.example > .env && chmod 600 .env
infra/vm/deploy.sh
```

The password exists only in `~/airbreda/.env` (mode 600) and reaches containers through
`--env-file`: not on the command line, in shell history or in the repo.

The laptop's Day 1–2 rows were merged in once:

```powershell
docker compose exec -T db pg_dump -U airbreda -d airbreda --data-only --inserts --on-conflict-do-nothing --table sensor_readings > laptop.sql
```
```bash
docker exec -i db psql -U airbreda -d airbreda < laptop.sql
```

### Part 4 — running continuously

- **Ingesters:** long-running containers with `--restart unless-stopped`, each cycling hourly, with Docker enabled at boot. Tested with `aws ec2 reboot-instances`: everything was back within a minute.
- **Backups (cron):** nightly at 02:15 UTC.

```bash
crontab -l
# 15 2 * * * /home/ec2-user/airbreda/infra/vm/backup.sh >> /home/ec2-user/airbreda/backup.log 2>&1
```

Restore test (done on Day 3: 125 of 125 rows):

```bash
KEY=backups/postgres/$(date -u +%F).sql.gz
docker exec db psql -U airbreda -d airbreda -c "CREATE DATABASE restore_test"
aws s3 cp s3://airbreda-raw-730631/$KEY - | gunzip | docker exec -i db psql -q -U airbreda -d restore_test
docker exec db psql -U airbreda -d restore_test -c "SELECT COUNT(*) FROM sensor_readings"
docker exec db psql -U airbreda -d airbreda -c "DROP DATABASE restore_test"
```

## Cost check

```powershell
aws ce get-cost-and-usage --time-period Start=2026-11-01,End=2026-12-01 --granularity MONTHLY --metrics UnblendedCost
aws freetier get-account-plan-state --region us-east-1      # remaining credit
```

Expected ≈ $0.70/day (≈ $21/month) while the VM runs, all paid from the Free plan credit
(ADR-0004). Cost Explorer lags by up to 24 hours.

## Stop / clean up

```powershell
aws ec2 stop-instances  --region eu-north-1 --instance-ids i-0dd1b70fd3b8badb5   # data kept; Elastic IP stays
aws ec2 start-instances --region eu-north-1 --instance-ids i-0dd1b70fd3b8badb5
```

After the course, release the Elastic IP. It bills even while the VM is stopped, and
releasing it gives the address back to AWS for good:

```powershell
aws ec2 disassociate-address --region eu-north-1 --association-id eipassoc-04aace55eda25b2b8
aws ec2 release-address      --region eu-north-1 --allocation-id  eipalloc-0d59a0f26157f73cf
```
