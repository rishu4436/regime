<#
Public SIM demo for judges. Does not read .env and does not start LIVE trading.

  .\backend\app\deploy\judge-demo.ps1

Starts three local processes, all bound to 127.0.0.1:
  - FastAPI on 8010, TRADING_MODE=SIM, its own logs\judge-demo folder
  - Vite on 5174, proxying /api and /ws to that SIM API
  - a Cloudflare quick tunnel to the Vite port

The live agent on ports 8000 and 5173 is left alone. The tunnel URL is printed
and written to logs\judge-demo\url.txt. A quick tunnel URL changes every restart
and stays up only while this PC and these processes stay up.

No private key, seed phrase, or API secret is read or printed.
#>
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$DemoDir = Join-Path $Root "logs\judge-demo"
New-Item -ItemType Directory -Force -Path $DemoDir | Out-Null
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Missing $Py. Run .\backend\app\deploy\run.ps1 once in SIM mode first." }
$Cloud = Join-Path $Root "tools\cloudflared.exe"
if (-not (Test-Path $Cloud)) { throw "Missing $Cloud" }

function Stop-DemoPort([int]$Port) {
  $lines = netstat -ano | Select-String ":$Port\s"
  foreach ($line in $lines) {
    if ($line -match "LISTENING\s+(\d+)") {
      Stop-Process -Id ([int]$Matches[1]) -Force -ErrorAction SilentlyContinue
    }
  }
}
# Only the judge ports. Never 8000 or 5173, which may be the LIVE agent.
Stop-DemoPort 8010
Stop-DemoPort 5174

$env:TRADING_MODE = "SIM"
$env:LIVE_TRADING_ENABLED = "false"
$env:BINANCE_LIVE_ENABLED = "false"
$env:RISK_ALLOW_LIVE = "false"
$env:LOOP_AUTOSTART = "true"
$env:LOG_DIR = $DemoDir
$env:STATE_DIR = $DemoDir
foreach ($secret in @("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_WALLET_ADDRESS", "XAI_API_KEY", "AGENT_STUDIO_PRIVATE_KEY")) {
  Remove-Item "Env:$secret" -ErrorAction SilentlyContinue
}

Start-Process -FilePath $Py -ArgumentList @("-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8010") -WorkingDirectory $Root -WindowStyle Hidden
$env:REGIME_API_PROXY = "http://127.0.0.1:8010"
$env:VITE_API_MODE = "http"
$vite = Join-Path $Root "frontend\node_modules\.bin\vite.cmd"
if (-not (Test-Path $vite)) { throw "Missing $vite. Run npm install in frontend first." }
Start-Process -FilePath $vite -ArgumentList @("--host", "127.0.0.1", "--port", "5174", "--strictPort") -WorkingDirectory (Join-Path $Root "frontend") -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(40)
do {
  Start-Sleep -Seconds 1
  try {
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:8010/health" -UseBasicParsing -TimeoutSec 2
    if ($health.Content -match '"trading_mode"\s*:\s*"SIM"') { break }
    throw "judge demo came up as something other than SIM"
  } catch {
    if ((Get-Date) -gt $deadline) { throw "SIM API on 8010 did not become ready" }
  }
} while ($true)

$tunnelLog = Join-Path $DemoDir "tunnel.log"
if (Test-Path $tunnelLog) { Remove-Item $tunnelLog -Force }
Start-Process -FilePath $Cloud -ArgumentList @(
  "tunnel", "--no-autoupdate", "--protocol", "quic",
  "--url", "http://127.0.0.1:5174",
  "--http-host-header", "localhost:5174",
  "--logfile", $tunnelLog
) -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(40)
$url = $null
do {
  Start-Sleep -Seconds 1
  if (Test-Path $tunnelLog) {
    $hit = Select-String -Path $tunnelLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -Last 1
    if ($hit) { $url = $hit.Matches[0].Value }
  }
  if ($url) { break }
  if ((Get-Date) -gt $deadline) { throw "cloudflared did not print a URL. See $tunnelLog" }
} while ($true)

Set-Content -Path (Join-Path $DemoDir "url.txt") -Value $url -Encoding ascii
Write-Output $url
