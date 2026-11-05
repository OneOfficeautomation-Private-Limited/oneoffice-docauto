<# Starts the Document Automation service with settings from config\local.env

   Database: PostgreSQL. Without DOCAUTO_DATABASE_URL an embedded PostgreSQL 16 runs from <data dir>\postgres
   (started with the service, stopped with it). A data folder from DocFlow 3 (SQLite docauto.db) is copied into
   PostgreSQL once, before the first start (python -m docauto.migrate_sqlite; the SQLite file is kept).
#>
$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot
Set-Location $App
$envFile = Join-Path $App "config\local.env"
if (Test-Path $envFile) {
    foreach ($l in Get-Content $envFile) {
        if ($l -match '^\s*([A-Z0-9_]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim(), "Process") }
    }
}
if ($env:TESSERACT_CMD) { $env:Path = (Split-Path $env:TESSERACT_CMD) + ";" + $env:Path }
$env:DOCAUTO_CONFIG = Join-Path $App "config\document-service.yaml"
$port = if ($env:DOCAUTO_PORT) { $env:DOCAUTO_PORT } else { "8085" }
$vpy = Join-Path $App ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw "Not installed yet. Run windows\install.ps1 first." }
$dataDir = if ($env:DOCAUTO_DATA_DIR) { $env:DOCAUTO_DATA_DIR } else { "data" }
& $vpy -m docauto.migrate_sqlite $dataDir --if-needed
if ($LASTEXITCODE -ne 0) { throw "Copying the SQLite data to PostgreSQL failed - see above. Nothing was changed." }
Write-Host "OneOffice DocFlow on http://localhost:$port  (profile: $env:DOCAUTO_PROFILE)" -ForegroundColor Green
if ($env:DOCAUTO_DATABASE_URL) { Write-Host "  Database  PostgreSQL from DOCAUTO_DATABASE_URL" }
else { Write-Host "  Database  embedded PostgreSQL in $dataDir\postgres" }
Write-Host "  Screens   OOA Command Center: AI > Document Automation"
Write-Host "  API docs  http://localhost:$port/docs"
Write-Host "  Engines   http://localhost:$port/api/docs/v1/providers"
Write-Host "Press Ctrl+C to stop."
& $vpy -m uvicorn docauto.api.app:app --host 127.0.0.1 --port $port
