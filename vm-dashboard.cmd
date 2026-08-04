@echo off
rem Compatibility wrapper. Official entrypoint: scripts\local\vm-dashboard.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\local\vm-dashboard.ps1" %*
