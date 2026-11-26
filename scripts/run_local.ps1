<#  Run the tool locally on Windows (PowerShell 5.1+ / 7+).
    Usage:  .\scripts\run_local.ps1 [-- extra args for src.main]
    e.g.    .\scripts\run_local.ps1 --output-dir reports --no-email
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if (Test-Path .env) {
    Get-Content .env | Where-Object { $_ -match '^\s*[^#\s][^=]*=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
    }
}
if (-not $env:DIU_EMAIL_ENABLED) { $env:DIU_EMAIL_ENABLED = "false" }
& .\.venv\Scripts\python.exe -m src.main --config config/settings.yaml @args
exit $LASTEXITCODE
