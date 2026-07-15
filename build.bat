@echo off
REM Build Pharma Batch Checker into a single-directory exe
REM Requires: pip install pyinstaller
cd /d "%~dp0"
pyinstaller pharma.spec --clean --noconfirm
echo.
echo Build complete. Output: dist\pharma-batch-checker\
echo Run: dist\pharma-batch-checker\pharma-batch-checker.exe
pause
