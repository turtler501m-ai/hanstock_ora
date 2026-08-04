@echo off
rem Compatibility wrapper. Official entrypoint: tools\verify-local.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\verify-local.ps1" %*
