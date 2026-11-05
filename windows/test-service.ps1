<# Smoke test against the running service: registers the sample schema, processes the sample invoice, prints fields. #>
$App = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $App "config\local.env"
$port = "8085"; $token = ""
if (Test-Path $envFile) { foreach ($l in Get-Content $envFile) {
    if ($l -match '^DOCAUTO_PORT=(.+)$') { $port = $matches[1].Trim() }
    if ($l -match '^DOCAUTO_API_TOKEN=(.+)$') { $token = $matches[1].Trim() } } }
$base = "http://localhost:$port/api/docs/v1"
$h = @{}; if ($token) { $h["Authorization"] = "Bearer $token" }
$health = Invoke-RestMethod "http://localhost:$port/health" -Headers $h
Write-Host ("Service: {0}, database: {1} (schema {2})" -f $health.status, $health.database.kind, $health.database.schema) -ForegroundColor Cyan
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
$failed = @(Invoke-RestMethod "$base/df/outbox?status=failed&limit=1000" -Headers $h)
$pending = @(Invoke-RestMethod "$base/df/outbox?status=pending&limit=1000" -Headers $h)
Write-Host ("Outbox: {0} delivery/event(s) waiting, {1} failed for good" -f $pending.Count, $failed.Count) -ForegroundColor Cyan
$sets = @(Invoke-RestMethod "$base/df/testsets" -Headers $h)
Write-Host ("Test sets: {0}" -f $sets.Count) -ForegroundColor Cyan
foreach ($s in $sets) {
    $r = $s.lastRun
    $line = "  {0} ({1}, {2} documents)" -f $s.name, $s.docType, $s.itemCount
    if ($r) { foreach ($c in $r.results) { if (-not $c.skipped) { $line += ("  {0}: field accuracy {1:P0}, straight-through {2:P0}" -f $c.label, $c.fieldAccuracy, $c.straightThrough) } } }
    Write-Host $line
}
Write-Host "Review low-confidence fields in the OOA Command Center (Task inbox, or AI > Document Automation)."
