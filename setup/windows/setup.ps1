# Jenkins MCP - company setup: Windows + VS Code GitHub Copilot.
# Step-by-step guide: setup\windows\README.md
#
# Usage (PowerShell, from the project folder):
#   powershell -ExecutionPolicy Bypass -File setup\windows\setup.ps1
#   powershell -ExecutionPolicy Bypass -File setup\windows\setup.ps1 -Python "C:\Program Files\Python311\python.exe" -Proxy "http://proxy.example.com:80"
#   powershell -ExecutionPolicy Bypass -File setup\windows\setup.ps1 -Dev     # developers: + test tools, runs the tests
#
# Creates .venv, installs the server, copies the config templates (existing files are kept) and writes
# .vscode\mcp.json with one password prompt per Jenkins instance: VS Code asks for each API token once and
# keeps it in its secure storage. Run the script again after adding an instance to the yaml.
# -TokensInEnvFile: no prompts, tokens stay in config\.env.
# Measure the server start time: Measure-Command { .venv\Scripts\python.exe -c "import jenkins_mcp.main" }
# -Dev installs in editable mode (code changes apply without reinstalling) with pytest & co.
# and runs the offline tests; guide: tests\README.md, section "Windows + VS Code".

param(
    [string]$Python = "python",
    [string]$Proxy = "",
    [switch]$Dev,
    [switch]$TokensInEnvFile
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"   # UTF-8 console output (Jenkins logs contain non-ASCII characters)
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
}

$Steps = if ($Dev) { 5 } else { 4 }
Write-Host "==> [1/$Steps] Virtual environment: $Root\.venv"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python not found or venv failed. Pass -Python <path to python.exe> (3.10 or newer)." }
}
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "==> [2/$Steps] Installing jenkins-mcp$(if ($Dev) { ' (editable, with test tools)' })"
& $VenvPython -m pip install --upgrade pip
if ($Dev) { & $VenvPython -m pip install -e ".[dev]" } else { & $VenvPython -m pip install . }
if ($LASTEXITCODE -ne 0) { throw "pip install failed (behind a proxy? pass -Proxy http://host:port)." }

Write-Host "==> [3/$Steps] Config files"
foreach ($pair in @(@("config\jenkins_instances.example.yaml", "config\jenkins_instances.yaml"),
                    @("config\.env.example", "config\.env"))) {
    if (Test-Path $pair[1]) {
        Write-Host "    keep existing $($pair[1])"
    } else {
        Copy-Item $pair[0] $pair[1]
        Write-Host "    created $($pair[1]) - edit it"
    }
}

Write-Host "==> [4/$Steps] VS Code: .vscode\mcp.json"
$GenArgs = @("-m", "jenkins_mcp.vscode_config", "--home", $Root, "--platform", "windows")
if ($TokensInEnvFile) { $GenArgs += "--no-inputs" }
& $VenvPython @GenArgs
if ($LASTEXITCODE -ne 0) { throw "Could not write .vscode\mcp.json (see above)." }

if ($Dev) {
    Write-Host "==> [5/5] Offline tests"
    & (Join-Path $Root ".venv\Scripts\pytest.exe") -q
    if ($LASTEXITCODE -ne 0) { throw "Offline tests failed (see above)." }
    Write-Host ""
    Write-Host "Developer setup done (guide: tests\README.md, section 'Windows + VS Code'):"
    Write-Host "  - VS Code Testing panel (beaker icon) lists all tests; .venv is picked up automatically"
    Write-Host "  - Live test against a Jenkins in Docker: powershell -ExecutionPolicy Bypass -File tests\live\run_live.ps1"
    exit 0
}

Write-Host ""
Write-Host "Done. Next steps (details: setup\windows\README.md):"
Write-Host "  1. config\jenkins_instances.yaml : your Jenkins URL(s) - then run this script again"
if ($TokensInEnvFile) {
    Write-Host "  2. config\.env                   : JENKINS_<NAME>_USER and JENKINS_<NAME>_TOKEN (API token)"
} else {
    Write-Host "  2. config\.env                   : JENKINS_<NAME>_USER only - VS Code asks for the tokens"
}
Write-Host "  3. VS Code: open this folder, Ctrl+Shift+P -> 'MCP: List Servers' -> jenkins -> Start"
Write-Host "  4. Copilot Chat in Agent mode: 'list_instances with check_connection'"
