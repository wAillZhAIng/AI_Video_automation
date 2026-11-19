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
        $answer = Read-Host "Permanently reset application records and volumes. Type $project to confirm"
        if ($answer -cne $project) { return }
    }
    Invoke-DockerChecked -DockerArguments @('compose', '-f', 'compose.yaml', 'down', '--timeout', '5', '--volumes')
    $remainingVolumes = & docker volume ls --quiet --filter "label=com.docker.compose.project=$project"
    if ($LASTEXITCODE -ne 0) { throw "Could not check remaining volumes for $project." }
    $remainingContainers = & docker compose -f compose.yaml ps --all --quiet
    if ($LASTEXITCODE -ne 0 -or $remainingContainers -or $remainingVolumes) {
        throw "Reset incomplete for $project. Check for a container using its volumes and retry."
    }
    Write-Output "Reset complete for $project. Start again to create empty volumes."
} finally { Pop-Location }
