@echo off
REM Build BatchSentry — Windows batch wrapper for build.ps1
REM Requires: PowerShell (to execute build.ps1)
REM Usage: build.bat            (full build)
REM        build.bat -SkipElectron  (skip NSIS installer)
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
pause
