@echo off
REM build.bat — 入口脚本，转发到 build.ps1
REM 项目约束：build.bat 必须调用 build.ps1（不在 .bat 内重复构建逻辑）
powershell -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
