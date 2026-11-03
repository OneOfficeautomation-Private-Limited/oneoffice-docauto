<# Creates "OneOffice DocFlow" shortcuts on the Desktop and in the Start menu. #>
$App = Split-Path -Parent $PSScriptRoot
$icon = Join-Path $PSScriptRoot "docflow.ico"
$ws = New-Object -ComObject WScript.Shell
$menu = Join-Path ([Environment]::GetFolderPath("Programs")) "OneOffice DocFlow"
New-Item -ItemType Directory -Force -Path $menu | Out-Null
function Link($path, $target, $desc) {
    $l = $ws.CreateShortcut($path)
    $l.TargetPath = $target
    $l.WorkingDirectory = $App
    $l.IconLocation = $icon
    $l.Description = $desc
    $l.Save()
}
$desktop = [Environment]::GetFolderPath("Desktop")
Link (Join-Path $desktop "OneOffice DocFlow.lnk") (Join-Path $PSScriptRoot "start.bat") "Start OneOffice DocFlow"
Link (Join-Path $menu "OneOffice DocFlow.lnk") (Join-Path $PSScriptRoot "start.bat") "Start OneOffice DocFlow"
Link (Join-Path $menu "Update OneOffice DocFlow.lnk") (Join-Path $PSScriptRoot "update.bat") "Get the latest DocFlow from GitHub"
Link (Join-Path $menu "Configure OneOffice DocFlow.lnk") (Join-Path $PSScriptRoot "configure.bat") "Engine settings wizard"
Write-Host "    OK  shortcuts: Desktop + Start menu > OneOffice DocFlow" -ForegroundColor Green
