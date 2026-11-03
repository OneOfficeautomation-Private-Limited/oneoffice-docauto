<#
  Interactive configuration for Oneoffice Document Automation.
  Writes config\local.env (read by start.ps1). Re-run any time; press Enter to keep current values.
#>
$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $App "config\local.env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $App "config\local.env.example") $envFile }

# load current values (keeps comments/order)
$lines = Get-Content $envFile
$cur = @{}
foreach ($l in $lines) { if ($l -match '^\s*([A-Z0-9_]+)=(.*)$') { $cur[$matches[1]] = $matches[2] } }

function Ask($key, $prompt, $secret = $false) {
    $old = $cur[$key]
    $shown = if ($secret -and $old) { "********" } else { $old }
    if ($secret) {
        $s = Read-Host "$prompt [$shown]" -AsSecureString
        $v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    } else { $v = Read-Host "$prompt [$shown]" }
    if ($v -ne "") { $cur[$key] = $v }
}
function Choose($key, $prompt, $options) {
    Write-Host "`n$prompt"
    for ($i = 0; $i -lt $options.Count; $i++) { Write-Host ("  {0}) {1}" -f ($i + 1), $options[$i]) }
    $v = Read-Host "Choice [current: $($cur[$key])]"
    if ($v -match '^\d+$' -and [int]$v -ge 1 -and [int]$v -le $options.Count) { $cur[$key] = ($options[[int]$v - 1] -split ' ')[0] }
}
function YesNo($key, $prompt) {
    $v = Read-Host "$prompt (y/n) [current: $($cur[$key])]"
    if ($v -match '^[yY]') { $cur[$key] = "true" } elseif ($v -match '^[nN]') { $cur[$key] = "false" }
}

Write-Host "=== Oneoffice Document Automation - configuration ===" -ForegroundColor Cyan
Choose "DOCAUTO_PROFILE" "Deployment profile:" @(
    "onprem_airgapped  (nothing leaves this network)",
    "onprem_hybrid     (local engines + approved cloud engines)",
    "cloud_saas        (all engines)")

Write-Host "`nOption packs:"
YesNo "DOCAUTO_PACK_OPENSOURCE" "  Option 1 - Open source (Tesseract/PaddleOCR)"
YesNo "DOCAUTO_PACK_CLOUD"      "  Option 2 - Licensed / cloud (Azure, Google, AWS, ABBYY)"
YesNo "DOCAUTO_PACK_AI"         "  Option 3 - AI extraction (LLM)"

if ($cur["DOCAUTO_PACK_CLOUD"] -eq "true") {
    $v = Read-Host "`nConfigure Azure AI Document Intelligence now? (y/n)"
    if ($v -match '^[yY]') {
        Ask "AZURE_DI_ENDPOINT" "  Endpoint (https://<resource>.cognitiveservices.azure.com)"
        Ask "AZURE_DI_KEY" "  Key" $true
        Ask "AZURE_DI_REGION" "  Region (e.g. uaenorth, westeurope)"
    }
    $v = Read-Host "Configure AWS Textract now? (needs: pip install boto3) (y/n)"
    if ($v -match '^[yY]') {
        Ask "AWS_REGION" "  Region (e.g. me-central-1, eu-west-1)"
        Ask "AWS_ACCESS_KEY_ID" "  Access key id"
        Ask "AWS_SECRET_ACCESS_KEY" "  Secret access key" $true
    }
    $v = Read-Host "Configure Google Document AI now? (needs: pip install google-cloud-documentai) (y/n)"
    if ($v -match '^[yY]') {
        Ask "GCP_PROJECT" "  GCP project id"
        Ask "DOCAI_LOCATION" "  Location (us / eu)"
        Ask "DOCAI_OCR_PROCESSOR" "  Document OCR processor id"
        Ask "DOCAI_FORM_PROCESSOR" "  Form Parser processor id (optional)"
        Ask "GOOGLE_APPLICATION_CREDENTIALS" "  Path to service-account JSON"
    }
}
if ($cur["DOCAUTO_PACK_AI"] -eq "true") {
    $v = Read-Host "`nConfigure a cloud LLM (OpenAI-compatible API) now? (y/n)"
    if ($v -match '^[yY]') {
        Ask "LLM_BASE_URL" "  Base URL (ending in /v1)"
        Ask "LLM_MODEL" "  Model id (vision-capable recommended)"
        Ask "LLM_API_KEY" "  API key" $true
        Ask "LLM_REGION" "  Processing region (for residency policy)"
    }
    $v = Read-Host "Configure a self-hosted LLM (vLLM / Ollama) now? (y/n)"
    if ($v -match '^[yY]') {
        Ask "LOCAL_LLM_BASE_URL" "  Base URL (e.g. http://localhost:11434/v1 for Ollama)"
        Ask "LOCAL_LLM_MODEL" "  Model id"
    }
}

Write-Host "`nDefaults (engine ids: tesseract, paddleocr, azure-di, google-docai, aws-textract | builtin-rules, llm-local, llm-cloud)"
Ask "DOCAUTO_DEFAULT_OCR" "  Default document OCR engine"
Ask "DOCAUTO_DEFAULT_EXTRACTION" "  Default extraction engine"
Ask "DOCAUTO_ESCALATE_TO" "  Escalate low-confidence pages to (blank = never)"
Ask "DOCAUTO_PORT" "  Service port"
Ask "DOCAUTO_API_TOKEN" "  API token for the RPA tool (blank = no auth, local testing only)" $true

# write back preserving comments
$port = $cur["DOCAUTO_PORT"]
$out = foreach ($l in $lines) {
    if ($l -match '^\s*([A-Z0-9_]+)=') { "$($matches[1])=$($cur[$matches[1]])"; $cur.Remove($matches[1]) | Out-Null } else { $l }
}
foreach ($k in $cur.Keys) { $out += "$k=$($cur[$k])" }
$out | Set-Content $envFile -Encoding UTF8
Write-Host "`nSaved $envFile" -ForegroundColor Green
Write-Host "Restart the service (windows\start.bat) and check http://localhost:$port/api/docs/v1/providers"
