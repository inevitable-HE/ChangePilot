[CmdletBinding()]
param(
    [string]$Python = ".venv\Scripts\python.exe",
    [int]$ApiPort = 8000,
    [int]$ConsolePort = 5173
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $repoRoot $Python
$consoleRoot = Join-Path $repoRoot "console"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python environment not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $consoleRoot "node_modules"))) {
    throw "Console dependencies are missing. Run 'npm install' in console first."
}

$env:PYTHONPATH = "$(Join-Path $repoRoot "src");$repoRoot"
$env:CHANGEPILOT_OPERATIONS_ROOT = Join-Path $repoRoot ".operations"

$api = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @(
        "-m", "uvicorn", "changepilot.operations.server:app",
        "--host", "127.0.0.1", "--port", "$ApiPort"
    ) `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -PassThru

$console = Start-Process `
    -FilePath "npm.cmd" `
    -ArgumentList @("run", "dev", "--", "--port", "$ConsolePort") `
    -WorkingDirectory $consoleRoot `
    -WindowStyle Hidden `
    -PassThru

Write-Host "ChangePilot API:     http://127.0.0.1:$ApiPort/docs"
Write-Host "Operations console: http://127.0.0.1:$ConsolePort"
Write-Host "Press Ctrl+C to stop both local processes."

try {
    while (-not $api.HasExited -and -not $console.HasExited) {
        Start-Sleep -Seconds 1
        $api.Refresh()
        $console.Refresh()
    }
    throw "A demo process stopped unexpectedly."
}
finally {
    foreach ($process in @($api, $console)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id
        }
    }
}
