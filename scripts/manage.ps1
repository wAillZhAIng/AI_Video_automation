#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('Start', 'Stop', 'Clean')][string]$Action,
    [switch]$Yes,
    [switch]$NoBrowser,
    [switch]$EmitBrowserUrl
)
. (Join-Path $PSScriptRoot 'docker-common.ps1')
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$env:COMPOSE_ANSI = 'never'
$env:BUILDKIT_PROGRESS = 'plain'

Push-Location $script:DockerProjectRoot
try {
    if ($Action -eq 'Start') {
        & (Join-Path $PSScriptRoot 'start.ps1') -OpenBrowser:(-not ($NoBrowser -or $EmitBrowserUrl)) -EmitBrowserUrl:$EmitBrowserUrl
        return
    }
    $engine = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') { throw 'Start Docker Desktop and switch to Linux containers.' }
    $raw = & docker compose -f compose.yaml config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the current Compose project.' }
    $project = ($raw -join [Environment]::NewLine | ConvertFrom-Json).name
    if ($project -notmatch '^[a-z0-9][a-z0-9_-]*$') { throw 'Could not identify the current Compose project.' }
    if ($Action -eq 'Stop') {
        Invoke-DockerChecked -DockerArguments @('compose', '-f', 'compose.yaml', 'stop', '--timeout', '5')
        $remaining = & docker compose -f compose.yaml ps --quiet
        if ($LASTEXITCODE -ne 0 -or $remaining) { throw "Some containers in $project may still be running." }
        Write-Output "Stopped $project. Its data volumes are retained."
        return
    }
    if (-not $Yes) {
        $answer = Read-Host "Permanently clear $project business files and records (keep model settings). Type the project name to confirm"
        if ($answer -cne $project) { return }
    }
    Invoke-DockerChecked -DockerArguments @('compose', '-f', 'compose.yaml', 'stop', '--timeout', '5')
    $remaining = & docker compose -f compose.yaml ps --quiet
    if ($LASTEXITCODE -ne 0 -or $remaining) { throw "Cleanup cancelled because containers in $project may still be running." }
    $resetScript = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'reset_business_data.py')
    $resetScript | & docker compose -f compose.yaml run --rm --no-deps --pull never -T --entrypoint python backend -
    if ($LASTEXITCODE -ne 0) { throw "Business-data cleanup failed for $project. Check the previous output." }
    Write-Output "Business-data cleanup complete for $project. Model configuration and named volumes retained."
} finally { Pop-Location }
