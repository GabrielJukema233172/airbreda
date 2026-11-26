# AirBreda - retrain the NO2 model on everything collected so far, and redeploy.
# Run from anywhere on the laptop (Windows PowerShell 5.1 or 7):
#
#   .\infra\retrain.ps1               # build data on VM -> train -> test -> deploy
#   .\infra\retrain.ps1 -SkipDeploy   # stop after the tests (inspect ml\metrics.json first)
#
# Steps (the Day 4 flow, see infra/day4-deploy.md and ADR-0006):
#   1. ship code to the VM, build ml/training_data.csv there (the database is on the VM)
#   2. copy the CSV back, train in the pinned environment (same scikit-learn as the dashboard)
#   3. run the full test suite - a model that breaks tests/test_model.py is never deployed
#   4. ship again (now with the new model.pkl) and run infra/vm/deploy.sh
#   5. smoke-test GET /site/hrl on the public address
# Afterwards: commit services/dashboard/model.pkl, ml/training_data.csv, ml/metrics.json
# and ml/no2_vs_traffic.png together, so the deployed model stays traceable to its data.
# (ASCII only: Windows PowerShell 5.1 misreads UTF-8 scripts without a BOM.)

param([switch]$SkipDeploy)

# "Continue", not "Stop": in PowerShell 5.1, "Stop" turns any stderr line from a
# native tool (pip notices, docker progress) into a fatal error. Failures are
# detected from exit codes instead (Invoke-Native).
$ErrorActionPreference = "Continue"
$Key  = Join-Path $env:USERPROFILE ".ssh\airbreda-day3.pem"
$Ip   = "16.192.119.40"           # Elastic IP of airbreda-vm
$Vm   = "ec2-user@$Ip"
$Tgz  = Join-Path $env:TEMP "airbreda-vm.tgz"
$Ssh  = @("-i", $Key, "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15")

Push-Location (Join-Path $PSScriptRoot "..")
try {
    function Invoke-Native([string]$What, [scriptblock]$Cmd, [int]$Tries = 1) {
        for ($i = 1; $i -le $Tries; $i++) {
            & $Cmd
            if ($LASTEXITCODE -eq 0) { return }
            # 255 = ssh/scp could not connect (the laptop's network drops now and then): retry
            if ($LASTEXITCODE -ne 255 -or $i -eq $Tries) { throw "$What failed (exit $LASTEXITCODE)" }
            Write-Host "  $What : connection failed, retrying ($i/$Tries)..."
            Start-Sleep -Seconds 10
        }
    }
    function Send-Code {
        Invoke-Native "packing code" { tar czf $Tgz --exclude=__pycache__ --exclude=ml/*.csv --exclude=*.png services sql infra/vm ml .gitattributes }
        Invoke-Native "copying code to the VM" { scp @Ssh $Tgz "${Vm}:~/airbreda-vm.tgz" } 5
        Invoke-Native "unpacking on the VM" { ssh @Ssh $Vm "tar xzf ~/airbreda-vm.tgz -C ~/airbreda && rm ~/airbreda-vm.tgz && chmod +x ~/airbreda/infra/vm/*.sh" } 5
    }

    Write-Host "== 1. Building the training data on the VM"
    Send-Code
    Invoke-Native "build-training-data.sh" { ssh @Ssh $Vm "cd ~/airbreda && infra/vm/build-training-data.sh 2>&1 | grep -v -e notice -e '^$'" } 3
    Invoke-Native "copying training_data.csv back" { scp @Ssh "${Vm}:~/airbreda/ml/training_data.csv" "ml\training_data.csv" } 5

    Write-Host "== 2. Training (pinned environment)"
    Invoke-Native "train_model.py" { docker run --rm -v "${PWD}:/work" -w /work python:3.12-slim sh -c "pip install -q --root-user-action=ignore -r ml/requirements.txt 2>/dev/null && python ml/train_model.py > /dev/null" }
    $m = Get-Content ml\metrics.json -Raw | ConvertFrom-Json
    Write-Host ("   rows {0} | LOO MAE {1:N2} (mean baseline {2:N2}) | traffic coef {3:N5} per veh/h | in-sample R2 {4:N2}" -f `
        $m.rows, $m.leave_one_out.mae_linear, $m.leave_one_out.mae_baseline_mean, `
        $m.coefficients.total_intensity_veh_per_hr, $m.in_sample.r2)
    if ($m.leave_one_out.mae_linear -ge $m.leave_one_out.mae_baseline_mean) {
        Write-Warning "The model does not beat the 'predict the mean' baseline. Check before deploying."
    }

    Write-Host "== 3. Tests"
    Invoke-Native "pytest" { docker run --rm -v "${PWD}:/app" -w /app python:3.12-slim sh -c "pip install -q --root-user-action=ignore -r requirements-dev.txt 2>/dev/null && pytest -q 2>&1 | tail -2" }

    if ($SkipDeploy) { Write-Host "== Skipping deploy (-SkipDeploy). New model is in services\dashboard\model.pkl"; return }

    Write-Host "== 4. Deploying"
    Send-Code
    Invoke-Native "deploy.sh" { ssh @Ssh $Vm "cd ~/airbreda && infra/vm/deploy.sh 2>&1 | tail -6" } 3

    Write-Host "== 5. Smoke test"
    for ($i = 1; $i -le 20; $i++) {
        try { $r = Invoke-RestMethod "http://${Ip}:8000/site/hrl" -TimeoutSec 5; break } catch { Start-Sleep -Seconds 3 }
    }
    if (-not $r) { throw "dashboard did not answer on http://${Ip}:8000" }
    Write-Host ("   /site/hrl: measured {0} | predicted {1} | risk {2} | total {3} veh/h | errors: {4}" -f `
        $r.no2_ug_m3, $r.no2_ug_m3_predicted, $r.no2_exceedance_risk, $r.total_intensity_veh_per_hr, ($r.errors -join "; "))
    Write-Host "Done. Commit services/dashboard/model.pkl + ml/ together."
}
finally {
    Pop-Location
}
