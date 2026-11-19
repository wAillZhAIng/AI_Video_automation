#requires -Version 5.1
param([switch]$Open)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$source = Join-Path $PSScriptRoot 'Program.cs'
$manifest = Join-Path $PSScriptRoot 'manager.manifest'
$output = Join-Path $root ('Windows' + [char]0x7BA1 + [char]0x7406 + '.exe')
$compiled = Get-Item -LiteralPath $output -ErrorAction SilentlyContinue
if (-not $compiled -or $compiled.LastWriteTimeUtc -lt (Get-Item -LiteralPath $source).LastWriteTimeUtc -or
    $compiled.LastWriteTimeUtc -lt (Get-Item -LiteralPath $manifest).LastWriteTimeUtc) {
    $paths = 'Microsoft.NET\Framework64\v4.0.30319\csc.exe', 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    $compiler = $paths | ForEach-Object { Join-Path $env:WINDIR $_ } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $compiler) { throw 'The Windows .NET Framework 4 C# compiler is required.' }
    & $compiler /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/win32manifest:$manifest" "/out:$output" $source
    if ($LASTEXITCODE -ne 0) { throw "Could not build the manager (exit $LASTEXITCODE)." }
}
if ($Open) { Start-Process -FilePath $output -WorkingDirectory $root -WindowStyle Normal }
else { Get-Item -LiteralPath $output }
