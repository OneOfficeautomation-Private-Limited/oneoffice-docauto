<# Smoke test against the running service: registers the sample schema, processes the sample invoice, prints fields. #>
$App = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $App "config\local.env"
$port = "8085"; $token = ""
if (Test-Path $envFile) { foreach ($l in Get-Content $envFile) {
    if ($l -match '^DOCAUTO_PORT=(.+)$') { $port = $matches[1].Trim() }
    if ($l -match '^DOCAUTO_API_TOKEN=(.+)$') { $token = $matches[1].Trim() } } }
$base = "http://localhost:$port/api/docs/v1"
$h = @{}; if ($token) { $h["Authorization"] = "Bearer $token" }
Write-Host "Engines:" -ForegroundColor Cyan
(Invoke-RestMethod "$base/providers" -Headers $h).providers | Format-Table id, optionPack, available, allowedByPolicy, reason -AutoSize
Invoke-RestMethod "$base/schemas" -Method Post -Headers $h -ContentType "application/json" -InFile (Join-Path $App "samples\invoice_schema.json") | Out-Null
$opts = Join-Path $env:TEMP "docauto-options.json"
'{"schemaId":"supplier-invoice","languages":["en","ar"],"classifyLabels":["invoice","contract"]}' | Set-Content $opts -Encoding ASCII
$curlArgs = @("-s", "-F", "file=@$App\samples\invoice_scan.png", "-F", "options=<$opts")
if ($token) { $curlArgs += @("-H", "Authorization: Bearer $token") }
$out = & curl.exe @curlArgs "$base/documents?wait=true"
$doc = ($out | ConvertFrom-Json).documentId
Write-Host "Extracted fields (document $doc):" -ForegroundColor Cyan
Invoke-RestMethod "$base/documents/$doc/fields" -Headers $h | ConvertTo-Json -Depth 5
Write-Host "Open http://localhost:$port/validation to review low-confidence fields."
