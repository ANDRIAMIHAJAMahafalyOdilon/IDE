@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai-studio\demarrer.ps1" %*
endlocal
