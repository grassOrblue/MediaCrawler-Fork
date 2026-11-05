param([int]$Port = 8081)
$ErrorActionPreference = 'Stop'
$deployRoot = $PSScriptRoot
$stateFile = Join-Path $deployRoot 'library-service.json'
$baseUrl = "http://127.0.0.1:$Port"
if (Test-Path -LiteralPath $stateFile) {
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $running = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
    if ($running -and $running.StartTime.ToUniversalTime().ToString('o') -eq $state.StartTimeUtc) {
        Write-Output "Data library is already running: $($state.Url)"
        exit 0
    }
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Library port $Port is occupied." }
New-Item -ItemType Directory -Path (Join-Path $deployRoot 'logs') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $deployRoot 'tmp') -Force | Out-Null
$env:TEMP = Join-Path $deployRoot 'tmp'
$env:TMP = $env:TEMP
$env:SQLITE_TMPDIR = $env:TEMP
$env:PYTHONUTF8 = '1'
$env:PYTHONUNBUFFERED = '1'
$appRoot = Join-Path $deployRoot 'app'
$serverProcess = Start-Process -FilePath (Join-Path $appRoot '.venv\Scripts\python.exe') -ArgumentList @('-m','uvicorn','api.library_app:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $appRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $deployRoot 'logs\library.out.log') -RedirectStandardError (Join-Path $deployRoot 'logs\library.err.log') -PassThru
@{ProcessId=$serverProcess.Id;StartTimeUtc=$serverProcess.StartTime.ToUniversalTime().ToString('o');Url=$baseUrl} | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
for ($attempt=0;$attempt -lt 40;$attempt++) {
    Start-Sleep -Milliseconds 500
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) { throw 'Library exited. Check logs\library.err.log.' }
    try { if ((Invoke-RestMethod "$baseUrl/api/health" -TimeoutSec 2).status -eq 'ok') { Write-Output "Data library is running: $baseUrl"; exit 0 } } catch { }
}
throw 'Library is still starting. Check logs\library.err.log.'
