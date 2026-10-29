# One-shot live test on Windows: start Jenkins in Docker, seed jobs, check every MCP tool.
# PowerShell version of run_live.sh. Needs Docker Desktop (running) and: setup\windows\setup.ps1 -Dev
#
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File tests\live\run_live.ps1

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"   # tool output contains non-ASCII characters
Set-Location $PSScriptRoot
$Py = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path + "\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Run 'setup\windows\setup.ps1 -Dev' first (.venv missing)." }

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose failed - is Docker Desktop running?" }

& $Py setup_jobs.py http://localhost:8080
if ($LASTEXITCODE -ne 0) { throw "Seeding the test Jenkins failed (see above)." }

& $Py live_test.py http://localhost:8080
exit $LASTEXITCODE
