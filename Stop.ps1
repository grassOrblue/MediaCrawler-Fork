$ErrorActionPreference = 'Stop'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Stop-Library.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Could not stop data library.' }
$stateFile = Join-Path $PSScriptRoot 'service.json'
if (-not (Test-Path -LiteralPath $stateFile)) { Write-Output 'MediaCrawler is not running.'; exit 0 }
$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$serverProcess = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
if ($serverProcess) {
    if ($serverProcess.StartTime.ToUniversalTime().ToString('o') -ne $state.StartTimeUtc) { throw 'Process identity changed; refusing to stop an unrelated process.' }
    try { Invoke-RestMethod -Method Post -Uri "$($state.Url)/api/crawler/stop" -TimeoutSec 20 | Out-Null } catch { }
    # Stop the recorded server and its crawler subprocesses, never other servers.
    & taskkill.exe /PID $state.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue)) { throw 'Could not stop MediaCrawler.' }
}
Remove-Item -LiteralPath $stateFile
Write-Output 'MediaCrawler stopped.'
