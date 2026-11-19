#requires -Version 5.1
[CmdletBinding()]
param([switch]$Development, [switch]$OpenBrowser, [switch]$EmitBrowserUrl)
. (Join-Path $PSScriptRoot 'docker-common.ps1')
$composeFile = if ($Development) { 'compose.dev.yaml' } else { 'compose.yaml' }
Push-Location $script:DockerProjectRoot
try {
    $engineOs = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait for its engine to be ready, then retry.' }
    if ($engineOs -ne 'linux') { throw 'Switch Docker Desktop to Linux containers. Windows containers cannot run these application images.' }
    Invoke-DockerChecked -DockerArguments @('compose', '-f', $composeFile, 'up', '-d', '--wait', '--wait-timeout', '180')
    Invoke-DockerChecked -DockerArguments @('compose', '-f', $composeFile, 'exec', '-T', 'frontend', 'wget', '-q', '-O', '/dev/null', 'http://127.0.0.1:8080/api/health')
    $address = & docker compose -f $composeFile port frontend 8080
    if ($LASTEXITCODE -ne 0 -or -not $address) { throw 'The frontend did not publish a host port.' }
    $url = 'http://' + ($address -replace '^0\.0\.0\.0:', '127.0.0.1:' -replace '^\[::\]:', '[::1]:')
    Write-Output "Open $url"
    if ($EmitBrowserUrl) { Write-Output "VF_OPEN_BROWSER $url" }
    if ($OpenBrowser) {
        try { Start-Process $url -ErrorAction Stop } catch { Write-Warning "Could not open the browser. Visit $url" }
    }
} finally { Pop-Location }
