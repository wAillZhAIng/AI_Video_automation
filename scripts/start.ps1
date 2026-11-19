#requires -Version 5.1
[CmdletBinding()]
param([switch]$Development, [switch]$OpenBrowser, [switch]$EmitBrowserUrl, [string]$NativeConfig)
. (Join-Path $PSScriptRoot 'docker-common.ps1')
$composeFile = if ($Development) { 'compose.dev.yaml' } else { 'compose.yaml' }
Push-Location $script:DockerProjectRoot
try {
    $engineOs = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait for its engine to be ready, then retry.' }
    if ($engineOs -ne 'linux') { throw 'Switch Docker Desktop to Linux containers. Windows containers cannot run these application images.' }
    if (-not $NativeConfig) {
        $variables = & docker compose -f $composeFile config --environment
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read the Compose configuration.' }
        $configured = $variables | Where-Object { $_ -like 'VF_NATIVE_RUNTIME_CONFIG=*' } | Select-Object -First 1
        if ($configured) { $NativeConfig = $configured.Substring('VF_NATIVE_RUNTIME_CONFIG='.Length) }
    }
    $defaultNativeConfig = Join-Path $script:DockerProjectRoot '.local-runtime\inference\native-runtime.json'
    if (-not $NativeConfig -and (Test-Path -LiteralPath $defaultNativeConfig -PathType Leaf)) {
        $NativeConfig = $defaultNativeConfig
    }
    if ($NativeConfig) {
        $configPath = (Resolve-Path -LiteralPath $NativeConfig).ProviderPath
        $localModels = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
        $modelPython = $localModels.comfy_python
        if (-not [IO.Path]::IsPathRooted($modelPython) -or -not (Test-Path -LiteralPath $modelPython -PathType Leaf)) {
            throw 'The configured ComfyUI Python executable does not exist. Check comfy_python in the native runtime configuration.'
        }
        & $modelPython -X utf8 -B (Join-Path $PSScriptRoot 'local_inference\start_stack.py') --config $configPath
        if ($LASTEXITCODE -ne 0) { throw 'The ComfyUI / Context-IR host startup did not pass. See the previous status and native logs.' }
    } else {
        Write-Output 'No native runtime configured. Setup instructions: docs/local-inference.md'
    }
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
