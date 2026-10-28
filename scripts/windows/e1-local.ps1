# EviReel E1 on native Windows (no Docker/WSL2): project-local PostgreSQL + S3 store, tests, demo server.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\e1-local.ps1 -Action <up|down|status|test|serve|all>
# Binaries come from .local-infra (see docs/engineering/E1_DELIVERY.md, "Windows native").
# Generated dev credentials live in .local-infra\dev-credentials.env (git-ignored) and are never printed.
# Logs: e1-local-logs\ (git-ignored).
param([ValidateSet('up', 'down', 'status', 'test', 'serve', 'all')][string]$Action = 'all')
$ErrorActionPreference = 'Continue'  # PS 5.1 would abort on native stderr redirection under 'Stop'
$Repo  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Infra = Join-Path $Repo '.local-infra'
$Logs  = Join-Path $Repo 'e1-local-logs'
New-Item -ItemType Directory -Force $Logs, "$Infra\data" | Out-Null
$PgPort = 54329; $S3Port = 9100; $WebPort = 8000
$Py = Join-Path $Repo '.venv\Scripts\python.exe'

function Find-Exe($name) { Get-ChildItem $Infra -Recurse -Filter $name -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName }
$PgBin  = Join-Path $Infra 'pgsql\bin'
$Minio  = Find-Exe 'minio.exe'
$Ffmpeg = Find-Exe 'ffmpeg.exe'
if ($Ffmpeg) { $env:PATH = (Split-Path $Ffmpeg) + ';' + $env:PATH }

function Credentials {
  $file = Join-Path $Infra 'dev-credentials.env'
  if (-not (Test-Path $file)) {
    $r = { -join (1..24 | ForEach-Object { '0123456789abcdef'[(Get-Random -Maximum 16)] }) }
    @("PG_USER=evireel", "PG_PASSWORD=dev$(& $r)", "S3_ACCESS_KEY=dev$(& $r)", "S3_SECRET_KEY=dev$(& $r)", "E2E_PASSWORD=e2e$(& $r)") |
      Set-Content $file -Encoding ascii
  }
  $c = @{}; Get-Content $file | ForEach-Object { $k, $v = $_ -split '=', 2; $c[$k] = $v }; $c
}
$C = Credentials
$DbAdmin = "postgresql://$($C.PG_USER):$($C.PG_PASSWORD)@127.0.0.1:$PgPort/postgres"
$DbApp   = "postgresql://$($C.PG_USER):$($C.PG_PASSWORD)@127.0.0.1:$PgPort/evireel"
$S3Url   = "http://127.0.0.1:$S3Port"

function Up {
  if (-not (Test-Path "$PgBin\pg_ctl.exe")) { throw "PostgreSQL binaries missing in $PgBin (run outputs\e1-local\setup.ps1)" }
  if (-not $Minio) { throw "minio.exe not found under $Infra (run setup-step2.ps1)" }
  $pgData = "$Infra\data\pg"
  if (-not (Test-Path "$pgData\PG_VERSION")) {
    $pw = "$Infra\pwfile.tmp"; Set-Content $pw $C.PG_PASSWORD -NoNewline -Encoding ascii
    & "$PgBin\initdb.exe" -D $pgData -U $C.PG_USER --pwfile=$pw -A scram-sha-256 -E UTF8 --locale=C | Out-File "$Logs\initdb.log"
    Remove-Item $pw
  }
  & "$PgBin\pg_ctl.exe" status -D $pgData *> $null
  if ($LASTEXITCODE -ne 0) {
    # Run via cmd with nul handles: postgres inherits pg_ctl's stdout, and a PowerShell pipe would never close.
    cmd /c "`"$PgBin\pg_ctl.exe`" start -D `"$pgData`" -l `"$Logs\postgres.log`" -o `"-p $PgPort -h 127.0.0.1`" -w >nul 2>&1"
  }
  $env:PGPASSWORD = $C.PG_PASSWORD
  $exists = & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U $C.PG_USER -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='evireel'"
  if (-not $exists) { & "$PgBin\createdb.exe" -h 127.0.0.1 -p $PgPort -U $C.PG_USER evireel }
  & "$PgBin\pg_isready.exe" -h 127.0.0.1 -p $PgPort

  $pidFile = "$Infra\data\minio.pid"
  $running = (Test-Path $pidFile) -and (Get-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue)
  if (-not $running) {
    $env:MINIO_ROOT_USER = $C.S3_ACCESS_KEY; $env:MINIO_ROOT_PASSWORD = $C.S3_SECRET_KEY
    $env:MINIO_API_CORS_ALLOW_ORIGIN = "http://127.0.0.1:$WebPort,http://localhost:$WebPort,http://127.0.0.1:8876,http://127.0.0.1:5173"
    $p = Start-Process -FilePath $Minio -ArgumentList @('server', "$Infra\data\minio", '--address', "127.0.0.1:$S3Port", '--console-address', "127.0.0.1:$($S3Port+1)") `
      -RedirectStandardOutput "$Logs\minio.log" -RedirectStandardError "$Logs\minio.err.log" -WindowStyle Hidden -PassThru
    $p.Id | Set-Content $pidFile
  }
  for ($i = 0; $i -lt 30; $i++) {
    try { if ((Invoke-WebRequest "$S3Url/minio/health/ready" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { 'minio ready'; break } } catch { Start-Sleep 1 }
  }
  & $Minio --version
  & "$PgBin\postgres.exe" --version
}

function Down {
  if (Test-Path "$Infra\data\pg\PG_VERSION") { & "$PgBin\pg_ctl.exe" stop -D "$Infra\data\pg" -m fast *> $null }
  $pidFile = "$Infra\data\minio.pid"
  if (Test-Path $pidFile) { Stop-Process -Id (Get-Content $pidFile) -ErrorAction SilentlyContinue; Remove-Item $pidFile }
  'stopped'
}

function TestEnv {
  $env:EVIREEL_TEST_DATABASE_URL = $DbAdmin
  $env:EVIREEL_TEST_S3_ENDPOINT = $S3Url
  $env:EVIREEL_TEST_S3_ACCESS_KEY = $C.S3_ACCESS_KEY
  $env:EVIREEL_TEST_S3_SECRET_KEY = $C.S3_SECRET_KEY
  $env:EVIREEL_E2E_PASSWORD = $C.E2E_PASSWORD
  foreach ($k in 'SILICONFLOW_API_KEY', 'OPENROUTER_API_KEY', 'DASHSCOPE_API_KEY') { Set-Item "env:$k" '' }
  $env:ENABLE_RETRIEVAL = 'false'; $env:QUERY_INCLUDE_LATEST_SEGMENT_VIDEO = 'false'
}

# npm.cmd, not npm.ps1: the .ps1 shim drops splatted arguments after `--`, so pnpm would only print its help.
function Pnpm { & npm.cmd exec --yes pnpm@11.25.0 -- @args }

function Wait-Http($url, $seconds) {
  for ($i = 0; $i -lt $seconds; $i++) { try { Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { Start-Sleep 1 } }
  return $false
}

function Test {
  TestEnv
  Push-Location $Repo
  $summary = @()
  & $Py -m pytest tests -q -rs --junitxml "$Logs\pytest-all.xml" *> "$Logs\pytest-all.log"
  $summary += "pytest (all, incl. E1 real PG+S3) exit=$LASTEXITCODE"
  Push-Location frontend
  Pnpm run typecheck *> "$Logs\frontend-typecheck.log"; $summary += "typecheck exit=$LASTEXITCODE"
  Pnpm test *> "$Logs\frontend-unit.log"; $summary += "vitest exit=$LASTEXITCODE"
  Pnpm run build *> "$Logs\frontend-build.log"; $summary += "build exit=$LASTEXITCODE"
  Pop-Location
  $edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
  if (-not (Get-ChildItem "$env:LOCALAPPDATA\ms-playwright" -Filter 'chromium-*' -ErrorAction SilentlyContinue) -and (Test-Path $edge)) {
    $env:PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH = $edge; $summary += 'browser=Microsoft Edge (Playwright Chromium not installed)'
  }
  # Regression: existing P5 browser workflows (single-user mode, explicit mock providers).
  $p5 = Start-Process $Py -ArgumentList 'tests\p5_browser_server.py', '--port', '8875' -RedirectStandardOutput "$Logs\p5-server.log" -RedirectStandardError "$Logs\p5-server.err.log" -WindowStyle Hidden -PassThru
  if (Wait-Http 'http://127.0.0.1:8875/__test__/health' 90) {
    Push-Location frontend; $env:EVIREEL_E1_URL = ''
    Pnpm exec playwright test --grep-invert 'interrupted upload|pause and continue' *> "$Logs\e2e-p5.log"; $summary += "e2e existing exit=$LASTEXITCODE"
    Pop-Location
  } else { $summary += 'e2e existing: server did not start' }
  Stop-Process -Id $p5.Id -ErrorAction SilentlyContinue
  # E1 browser acceptance on real PostgreSQL + S3.
  $e1 = Start-Process $Py -ArgumentList 'tests\e1_browser_server.py', '--port', '8876' -RedirectStandardOutput "$Logs\e1-server.log" -RedirectStandardError "$Logs\e1-server.err.log" -WindowStyle Hidden -PassThru
  if (Wait-Http 'http://127.0.0.1:8876/__test__/health' 120) {
    Push-Location frontend; $env:EVIREEL_E1_URL = 'http://127.0.0.1:8876'
    Pnpm exec playwright test e2e/upload-resume.spec.ts --reporter=list *> "$Logs\e2e-e1.log"; $summary += "e2e E1 exit=$LASTEXITCODE"
    Pop-Location
  } else { $summary += 'e2e E1: server did not start' }
  Stop-Process -Id $e1.Id -ErrorAction SilentlyContinue
  Pop-Location
  $summary | Tee-Object "$Logs\summary.txt"
}

function Serve {
  # Manual demo: platform mode on the dev database. Real data in data\ is not migrated by this script.
  TestEnv
  $env:DATABASE_URL = $DbApp; $env:S3_ENDPOINT = $S3Url; $env:S3_PUBLIC_ENDPOINT = $S3Url
  $env:S3_ACCESS_KEY = $C.S3_ACCESS_KEY; $env:S3_SECRET_KEY = $C.S3_SECRET_KEY; $env:S3_BUCKET = 'evireel-dev'
  $env:DATA_ROOT = Join-Path $Infra 'demo-data'
  Push-Location $Repo
  & $Py -m vam.platform.migrate schema
  $env:EVIREEL_NEW_PASSWORD = $C.E2E_PASSWORD
  & $Py -m vam.platform.migrate create-user --username demo 2>$null
  Remove-Item env:EVIREEL_NEW_PASSWORD
  "Demo: http://127.0.0.1:$WebPort/app/  user=demo  password: E2E_PASSWORD in .local-infra\dev-credentials.env"
  & $Py run.py
  Pop-Location
}

switch ($Action) {
  'up' { Up } 'down' { Down } 'test' { Test } 'serve' { Up; Serve }
  'status' { & "$PgBin\pg_ctl.exe" status -D "$Infra\data\pg"; Get-Content "$Infra\data\minio.pid" -ErrorAction SilentlyContinue }
  'all' { Up; Test }
}
