param([int]$Port = 8080)
$ErrorActionPreference = 'Stop'
$deployRoot = $PSScriptRoot
$appRoot = Join-Path $deployRoot 'app'
$mediaPython = Join-Path $appRoot '.venv\Scripts\python.exe'
$stateFile = Join-Path $deployRoot 'service.json'
$baseUrl = "http://127.0.0.1:$Port"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $deployRoot 'Start-Library.ps1') -Port ($Port + 1)
if ($LASTEXITCODE -ne 0) { throw 'Could not start data library.' }

if (Test-Path -LiteralPath $stateFile) {
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $running = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
    if ($running -and $running.StartTime.ToUniversalTime().ToString('o') -eq $state.StartTimeUtc) {
        Write-Output "MediaCrawler is already running: $($state.Url)"
        exit 0
    }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is occupied. Use Start.ps1 -Port <free-port>."
}
foreach ($folder in @('logs', 'tmp', 'cache\matplotlib', 'cache\uv')) {
    New-Item -ItemType Directory -Path (Join-Path $deployRoot $folder) -Force | Out-Null
}
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $deployRoot 'browsers'
$env:TEMP = Join-Path $deployRoot 'tmp'
$env:TMP = $env:TEMP
$env:MPLCONFIGDIR = Join-Path $deployRoot 'cache\matplotlib'
$env:UV_CACHE_DIR = Join-Path $deployRoot 'cache\uv'
$env:UV_OFFLINE = 'true'
$env:UV_FROZEN = 'true'
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$env:PATH = (Join-Path $appRoot '.venv\Scripts') + ';C:\Python312\Scripts;' + $env:PATH
$serverProcess = Start-Process -FilePath $mediaPython -ArgumentList @('-m', 'uvicorn', 'api.main:app', '--host', '127.0.0.1', '--port', "$Port") -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $deployRoot 'logs\server.out.log') -RedirectStandardError (Join-Path $deployRoot 'logs\server.err.log') -PassThru
@{
    ProcessId = $serverProcess.Id
    StartTimeUtc = $serverProcess.StartTime.ToUniversalTime().ToString('o')
    Url = $baseUrl
    PythonPath = $mediaPython
} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) { throw 'Server exited. Check logs\server.err.log.' }
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
        if ($health.status -eq 'ok') { Write-Output "MediaCrawler is running: $baseUrl"; exit 0 }
    } catch { }
}
throw 'Server is still starting. Check logs\server.err.log and service.json.'
