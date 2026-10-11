<#
Regime - one-command local run on Windows (PowerShell 5+/7): backend (FastAPI :8000) + dashboard (Vite :5173).
  .\backend\app\deploy\run.ps1                       # SIM mode, both services; the dashboard talks to the real backend
  .\backend\app\deploy\run.ps1 -Mock                 # dashboard on built-in mock data instead (no backend calls)
  .\backend\app\deploy\run.ps1 -BackendOnly          # API only (http://localhost:8000/docs)
  .\backend\app\deploy\run.ps1 -Supervised           # 24/7: restart the backend whenever it exits (backoff 2..60 s)
  .\backend\app\deploy\run.ps1 -Supervised -BackendOnly
Mode/secrets come from the environment or a git-ignored .env at the repo root (TRADING_MODE=SIM by default).
LIVE needs LIVE_TRADING_ENABLED=true, BINANCE_LIVE_ENABLED=true, RISK_ALLOW_LIVE=true and BINANCE_API_KEY /
BINANCE_API_SECRET; see backend\app\GO_LIVE.md. If `baw` is not on PATH, BAW_CMD is set to
node <npm root -g>\@binance\agentic-wallet\dist\index.js automatically.
The dashboard listens on 127.0.0.1 only ($env:UI_HOST = "0.0.0.0" to share it on your LAN; wallet sign-in stays
localhost-only).
#>
param([switch]$BackendOnly, [switch]$Supervised, [switch]$Mock, [int]$Port = 0)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
Set-Location $Root

# ---- .env (KEY=VALUE lines; # comments; existing environment variables win)
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
  foreach ($line in Get-Content $envFile) {
    $l = $line.Trim()
    if (-not $l -or $l.StartsWith("#") -or -not $l.Contains("=")) { continue }
    $k, $v = $l.Split("=", 2)
    $k = $k.Trim(); $v = $v.Trim().Trim('"').Trim("'")
    if (-not [Environment]::GetEnvironmentVariable($k)) { [Environment]::SetEnvironmentVariable($k, $v) }
  }
}
if (-not $env:TRADING_MODE) { $env:TRADING_MODE = "SIM" }
if (-not $env:LOG_DIR) { $env:LOG_DIR = Join-Path $Root "logs" }
if (-not $env:STATE_DIR) { $env:STATE_DIR = $env:LOG_DIR }
if ($Port -eq 0) { $Port = if ($env:PORT) { [int]$env:PORT } else { 8000 } }
New-Item -ItemType Directory -Force -Path $env:LOG_DIR | Out-Null

# ---- Python venv
$venv = if ($env:VENV) { $env:VENV } else { Join-Path $Root ".venv" }
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
  Write-Host "==> creating venv $venv"
  python -m venv $venv
}
# eth_account is in the check so an older .venv picks up new deps after a git pull.
# Agent Studio "Register" also needs the optional SDK: & $py -m pip install -e ".[studio]"
& $py -c "import fastapi, uvicorn, eth_account" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "==> pip install -e .[dev]"
  & $py -m pip install -q -e ".[dev]"
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# ---- baw (Binance Agentic Wallet CLI): prefer PATH, else node + the npm global package entry point
if (-not $env:BAW_CMD -and -not (Get-Command baw -ErrorAction SilentlyContinue)) {
  $npmRoot = $null
  try { $npmRoot = (& npm root -g 2>$null | Select-Object -First 1) } catch { }
  $candidates = @()
  if ($npmRoot) { $candidates += Join-Path $npmRoot "@binance\agentic-wallet\dist\index.js" }
  if ($env:APPDATA) { $candidates += Join-Path $env:APPDATA "npm\node_modules\@binance\agentic-wallet\dist\index.js" }
  $entry = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($entry) {
    $node = (Get-Command node -ErrorAction SilentlyContinue).Source
    if (-not $node) { $node = "node" }
    $env:BAW_CMD = (ConvertTo-Json @($node, $entry) -Compress)
    Write-Host "==> baw not on PATH; BAW_CMD=$($env:BAW_CMD)"
  } elseif ($env:TRADING_MODE -eq "LIVE") {
    Write-Warning "LIVE: baw not found. npm i -g @binance/agentic-wallet@1.10.0, then sign in (QR) - see GO_LIVE.md"
  }
}

# ---- frontend
$ui = $null
if (-not $BackendOnly) {
  $fe = Join-Path $Root "frontend"
  # real backend unless -Mock (env beats frontend\.env*, so a copied .env.example can't switch it to mock)
  if ($Mock) { $env:VITE_API_MODE = "mock" } elseif (-not $env:VITE_API_MODE) { $env:VITE_API_MODE = "http" }
  $uiHost = if ($env:UI_HOST) { $env:UI_HOST } else { "127.0.0.1" }
  # npm install every time: picks up new dependencies (e.g. qrcode-generator) after a git pull; fast when up to date
  Push-Location $fe; npm install --no-audit --no-fund; $npmCode = $LASTEXITCODE; Pop-Location
  if ($npmCode -ne 0) { throw "npm install failed in $fe" }
  $ui = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", $uiHost) -WorkingDirectory $fe -PassThru -NoNewWindow
  Write-Host "==> Regime dashboard: http://localhost:5173  (VITE_API_MODE=$($env:VITE_API_MODE))"
}

$uvArgs = @("-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "$Port")
Write-Host "==> Regime backend: TRADING_MODE=$($env:TRADING_MODE)  data (saved strategies, agent state, logs): $($env:LOG_DIR)  api: http://localhost:$Port/docs  supervised: $Supervised"
$supLog = Join-Path $env:LOG_DIR "supervisor.log"
function Log($m) { $line = "$(Get-Date -Format o) supervisor: $m"; Write-Host $line; Add-Content -Path $supLog -Value $line }

try {
  if (-not $Supervised) {
    & $py @uvArgs
    exit $LASTEXITCODE
  }
  if (-not $env:LOOP_AUTOSTART) { $env:LOOP_AUTOSTART = "true" }
  & $py -c "from backend.app.settings import Settings; Settings.from_env().validate()"
  if ($LASTEXITCODE -ne 0) { Log "startup refused (config error above) - not starting"; exit 3 }
  $backoff = 2
  while ($true) {
    $started = Get-Date
    Log "starting backend"
    & $py @uvArgs
    $code = $LASTEXITCODE
    $up = [int]((Get-Date) - $started).TotalSeconds
    Log "backend exited (code $code, up ${up}s)"
    if ($up -gt 300) { $backoff = 2 }
    Log "restarting in ${backoff}s"
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min(60, $backoff * 2)
  }
} finally {
  if ($ui -and -not $ui.HasExited) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
}
