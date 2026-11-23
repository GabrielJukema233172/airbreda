# Day 1 — AWS setup and run steps

All commands are **PowerShell**, run from the `airbreda` folder. Your CLI's default
region should be `eu-central-1` (check with `aws configure get region`).

## 0. Check the tools (Lab 1 Follow)

```powershell
aws sts get-caller-identity   # prints your account and the gabriel-admin user
docker run hello-world        # prints "Hello from Docker!"
```

## 1. Lab 1 — air-quality container

```powershell
cd services\air
docker build -t airbreda-air .
docker run --rm airbreda-air
cd ..\..
```

You should see the latest NO₂ value for NL10240 and `Database: skipped`.

Run the tests (from the `airbreda` folder):

```powershell
pip install pytest pandas requests boto3
pytest
```

## 2. Lab 2 — Postgres on RDS

**Password:** pick 16+ characters using only letters and digits (RDS refuses `/`, `@`, `"`
and spaces, and other symbols would need escaping in the connection URL).

```powershell
# Your laptop's public IP - the database will only accept connections from it
$MyIp = (Invoke-RestMethod https://checkip.amazonaws.com).Trim()

# Firewall (security group) in your default VPC: Postgres port 5432, only from your IP
$SgId = aws ec2 create-security-group --group-name airbreda-db-sg `
  --description "AirBreda Postgres from my laptop" --query GroupId --output text
aws ec2 authorize-security-group-ingress --group-id $SgId --protocol tcp --port 5432 --cidr "$MyIp/32"

# The database (free-tier size). Takes ~5-10 minutes.
$DbPass = Read-Host "Choose a DB password (letters and digits only)"
aws rds create-db-instance `
  --db-instance-identifier airbreda-db `
  --engine postgres `
  --db-instance-class db.t4g.micro `
  --allocated-storage 20 `
  --storage-type gp2 `
  --db-name airbreda `
  --master-username airbreda `
  --master-user-password $DbPass `
  --vpc-security-group-ids $SgId `
  --publicly-accessible `
  --no-multi-az `
  --backup-retention-period 1 `
  --query "DBInstance.DBInstanceStatus" --output text

aws rds wait db-instance-available --db-instance-identifier airbreda-db
$DbHost = aws rds describe-db-instances --db-instance-identifier airbreda-db `
  --query "DBInstances[0].Endpoint.Address" --output text
$DbUrl = "postgresql://airbreda:$DbPass@${DbHost}:5432/airbreda?sslmode=require"
```

Run the air container against it — **twice**, to show it is idempotent:

```powershell
docker run --rm -e DATABASE_URL=$DbUrl airbreda-air   # "50 new rows inserted"
docker run --rm -e DATABASE_URL=$DbUrl airbreda-air   # "0 new rows inserted, 50 already present"
```

The script creates `sensor_readings` itself (`CREATE TABLE IF NOT EXISTS`, same SQL as
`sql/schema.sql`).

> `$DbPass`, `$DbHost` and `$DbUrl` only live in this PowerShell window. Never put them
> in a file that gets committed.

## 3. Lab 2 Extend — S3 bucket + traffic container

```powershell
$Bucket = "airbreda-raw-" + (Get-Random -Maximum 999999)   # bucket names are global
aws s3 mb "s3://$Bucket" --region eu-central-1
$Bucket   # write this name down

cd services\traffic
docker build -t airbreda-traffic .
docker run --rm -e S3_BUCKET=$Bucket -e AWS_REGION=eu-central-1 `
  -v "$env:USERPROFILE\.aws:/root/.aws:ro" airbreda-traffic
cd ..\..

aws s3 ls "s3://$Bucket/ndw/" --recursive
```

The `-v` flag lends the container your CLI credentials read-only, for local testing only.
On the EC2 VM (Day 3) the container gets permissions from an IAM role instead, with no
keys at all. The run takes 1–3 minutes: the NDW site-config file is ~160 MB of XML.

New S3 buckets block all public access by default — keep it that way.

## 4. Save credits when you're done for the day

RDS bills every hour it runs (credits on the Free plan).

```powershell
aws rds stop-db-instance --db-instance-identifier airbreda-db    # pause
aws rds start-db-instance --db-instance-identifier airbreda-db   # resume (~5 min)
```

A stopped instance restarts by itself after 7 days. If your home IP changes, add the
new one with the `authorize-security-group-ingress` command above.
