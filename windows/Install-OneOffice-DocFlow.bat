@echo off
title Install OneOffice DocFlow
rem Installs OneOffice DocFlow from GitHub on this PC. Share this file with anyone who should install DocFlow.
rem Fill in the repository below (owner/name) or leave it blank to be asked.
set "DOCFLOW_REPO="
set "DOCFLOW_REF=main"
if "%DOCFLOW_REPO%"=="" set /p DOCFLOW_REPO=GitHub repository (owner/name): 
echo.
echo Private repository: paste a GitHub token with Contents = Read-only (leave blank for a public repository).
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $s=Read-Host 'GitHub token' -AsSecureString; $t=[Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)).Trim(); $h=@{Accept='application/vnd.github.raw';'User-Agent'='oneoffice-docflow'}; if($t){$h.Authorization='Bearer '+$t; $env:DOCFLOW_GITHUB_TOKEN=$t}; $api=if($env:DOCFLOW_GITHUB_API){$env:DOCFLOW_GITHUB_API}else{'https://api.github.com'}; Invoke-RestMethod ($api+'/repos/'+$env:DOCFLOW_REPO+'/contents/windows/get-docflow.ps1?ref='+$env:DOCFLOW_REF) -Headers $h | Invoke-Expression"
pause
