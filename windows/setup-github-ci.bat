@echo off
rem Copies the GitHub Actions workflows (tests + Windows install check + releases) into .github\workflows.
rem Run once before publishing the repository with GitHub Desktop.
if not exist "%~dp0..\.github\workflows" mkdir "%~dp0..\.github\workflows"
copy /Y "%~dp0..\ci\workflows\*.yml" "%~dp0..\.github\workflows\" >nul && echo GitHub workflows ready in .github\workflows
pause
