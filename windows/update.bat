@echo off
title OneOffice DocFlow - update
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-docflow.ps1" & pause & exit /b
