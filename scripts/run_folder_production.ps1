$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptRoot = 'D:\脚本'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'one_click_folder_production.py'
$stateFile = Join-Path $projectRoot 'data\folder_production_state.json'
$runtimeDir = Join-Path $projectRoot '.local-runtime'
$apiFile = Join-Path $runtimeDir 'folder-production-api.txt'
$sshFile = Join-Path $runtimeDir 'folder-production-ssh.txt'
$sshOut = Join-Path $runtimeDir 'folder-production-ssh.stdout.log'
$sshErr = Join-Path $runtimeDir 'folder-production-ssh.stderr.log'
$sshProcess = $null

function Test-ApiHealth([string]$BaseUrl) {
  try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/api/health" -Method Get -TimeoutSec 2
    return $response.status -eq 'ok'
  } catch {
    return $false
  }
}

try {
  if (-not (Test-Path -LiteralPath $python)) { throw "项目 Python 不存在：$python" }
  if (-not (Test-Path -LiteralPath $runner)) { throw "批量制作程序不存在：$runner" }
  if (-not (Test-Path -LiteralPath $scriptRoot)) { throw "脚本目录不存在：$scriptRoot" }

  $apiUrl = $env:VIDEO_FACTORY_API_URL
  if ([string]::IsNullOrWhiteSpace($apiUrl) -and (Test-Path -LiteralPath $sshFile)) {
    $lines = @(Get-Content -LiteralPath $sshFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($lines.Count -lt 2) { throw "SSH 配置格式无效：$sshFile 需要两行，分别是 root@HOST 与 SSH_PORT。" }
    $target = $lines[0]
    $sshPort = 0
    if ($target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$' -or -not [int]::TryParse($lines[1], [ref]$sshPort) -or $sshPort -lt 1 -or $sshPort -gt 65535) {
      throw "SSH 配置无效：第一行应为 root@HOST，第二行为 1–65535 的 SSH 端口。"
    }

    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $localPort = $listener.LocalEndpoint.Port
    $listener.Stop()
    $apiUrl = "http://127.0.0.1:$localPort"
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $sshArgs = @(
      '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
      '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
      '-o', 'StrictHostKeyChecking=accept-new', '-L', "127.0.0.1:${localPort}:127.0.0.1:18766",
      '-p', "$sshPort", $target
    )
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    Remove-Item -LiteralPath $sshOut, $sshErr -Force -ErrorAction SilentlyContinue
    $sshProcess = Start-Process -FilePath $ssh -ArgumentList $sshArgs -PassThru -WindowStyle Hidden `
      -RedirectStandardOutput $sshOut -RedirectStandardError $sshErr

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
      if ($sshProcess.HasExited) {
        $details = if (Test-Path -LiteralPath $sshErr) { (Get-Content -LiteralPath $sshErr -Tail 20) -join "`n" } else { 'SSH 子进程提前退出。' }
        throw "无法建立到实例的 SSH 隧道。请检查本机 SSH 密钥、主机与端口。`n$details"
      }
      if (Test-ApiHealth $apiUrl) { break }
      Start-Sleep -Milliseconds 500
    }
    if (-not (Test-ApiHealth $apiUrl)) { throw "SSH 隧道已启动，但实例自动化 API 在 30 秒内未就绪：$apiUrl/api/health" }
    Write-Host "已通过仅本机可访问的 SSH 隧道连接 AI_Video_automation（远端 127.0.0.1:18766）。"
  } elseif ([string]::IsNullOrWhiteSpace($apiUrl) -and (Test-Path -LiteralPath $apiFile)) {
    $apiUrl = (Get-Content -LiteralPath $apiFile -Raw).Trim()
  }

  if ([string]::IsNullOrWhiteSpace($apiUrl)) { $apiUrl = 'http://127.0.0.1:8765' }
  $apiUrl = $apiUrl.TrimEnd('/')
  $env:VIDEO_FACTORY_API_URL = $apiUrl

  Write-Host ''
  Write-Host '扫描 Word、绑定同目录共享产品图并执行只读预检。'
  Write-Host "Word 根目录：$scriptRoot"
  Write-Host "应用 API：$apiUrl"
  Write-Host '默认工作流：H3 Fast 4-step + 本机 Real-ESRGAN x4plus 4K 超分（精确规格需通过服务能力验收）。'
  Write-Host '云端服务与付费回退：关闭。'
  & $python $runner --root $scriptRoot --api $apiUrl --all --dry-run --limit 500 --state-file $stateFile
  if ($LASTEXITCODE -ne 0) { throw "扫描或预检失败（exit=$LASTEXITCODE）；没有提交生成任务。" }

  Write-Host ''
  $confirm = Read-Host '复核脚本、产品图哈希与服务能力后，输入 START ALL 开始批量生成'
  if ($confirm -cne 'START ALL') { Write-Host '已取消；没有提交生成任务。'; exit 0 }

  & $python $runner --root $scriptRoot --api $apiUrl --all --submit --confirm-submit --wait --continue-on-error --limit 500 --state-file $stateFile
  if ($LASTEXITCODE -ne 0) { throw "批量处理有失败或阻断项（exit=$LASTEXITCODE）；状态已写入台账。" }
  Write-Host '批量任务已处理完成；请在 AI_Video_automation 中查看成片和异常项。'
  exit 0
} catch {
  Write-Error $_
  exit 1
} finally {
  if ($sshProcess -and -not $sshProcess.HasExited) {
    Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
  }
}
