$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $PSScriptRoot 'library-service.json'
if (-not (Test-Path -LiteralPath $stateFile)) { exit 0 }
$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$process = Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue
if ($process) {
    if ($process.StartTime.ToUniversalTime().ToString('o') -ne $state.StartTimeUtc) { throw 'Library process identity changed; refusing to stop unrelated process.' }
    & taskkill.exe /PID $state.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $state.ProcessId -ErrorAction SilentlyContinue)) { throw 'Could not stop library.' }
}
Remove-Item -LiteralPath $stateFile
Write-Output 'Data library stopped.'
