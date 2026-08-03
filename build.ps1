# ============================================================
# Pharma Batch Checker — Windows build script
# ============================================================
# Produces:
#   1. static/app.css       (Tailwind CLI build)
#   2. dist/pbc-server/     (PyInstaller bundle, ~50MB)
#   3. dist-electron/BatchSentry-Setup-1.0.0.exe (NSIS installer, ~250MB)
#
# Prerequisites:
#   - Python 3.11+ with pyinstaller installed (pip install pyinstaller)
#   - Node.js 20+ (with npm/npx)
#   - Run from project root: .\build.ps1
#
# Usage:
#   .\build.ps1              # full build
#   .\build.ps1 -Clean       # clean then full build
#   .\build.ps1 -SkipCSS     # skip Tailwind (if app.css is current)
#   .\build.ps1 -SkipPyInstaller  # skip Python bundle
#   .\build.ps1 -SkipElectron     # skip NSIS installer
# ============================================================

param(
    [switch]$SkipCSS,
    [switch]$SkipPyInstaller,
    [switch]$SkipElectron,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
if (-not $projectRoot) { $projectRoot = (Get-Location).Path }
Set-Location $projectRoot

function Write-Step($msg) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Fail($msg) {
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    exit 1
}

# ── Pre-flight: verify required tools are on PATH ────────────────────
Write-Step "Pre-flight checks"

$pythonVersion = & python --version 2>&1
Write-Host "  Python: $pythonVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "Python not found on PATH. Install Python 3.11+ and retry." }

$nodeVersion = & node --version 2>&1
Write-Host "  Node:   $nodeVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "Node.js not found on PATH. Install Node.js 20+ and retry." }

$npmVersion = & npm --version 2>&1
Write-Host "  npm:    $npmVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "npm not found on PATH." }

# Verify pyinstaller availability (will be auto-installed below if missing)
$pyi = Get-Command pyinstaller -ErrorAction SilentlyContinue
$pyiModule = & python -m PyInstaller --version 2>&1
if (-not $pyi -and $LASTEXITCODE -ne 0) {
    Write-Host "  PyInstaller: NOT installed (will install)"
} else {
    Write-Host "  PyInstaller: $pyiModule"
}

Write-OK "Pre-flight passed"

# ── 0. Clean ────────────────────────────────────────────────────────
if ($Clean) {
    Write-Step "Cleaning previous build artifacts"
    Remove-Item -Recurse -Force dist, dist-electron, build -ErrorAction SilentlyContinue
    Write-OK "Cleaned dist/, dist-electron/, build/"
}

# ── 1. Tailwind CSS build ────────────────────────────────────────────
if (-not $SkipCSS) {
    Write-Step "Step 1/3: Building Tailwind CSS"

    # Ensure npm packages are installed
    if (-not (Test-Path "node_modules")) {
        Write-Host "  Installing npm dependencies..."
        & npm install 2>&1 | Out-Null
    }

    $npx = Get-Command npx -ErrorAction SilentlyContinue
    if (-not $npx) {
        # Fallback: use full path to npx
        $npxPath = "D:\nodejs\node-v22.16.0-win-x64\npx.cmd"
        if (Test-Path $npxPath) {
            & $npxPath tailwindcss build -i static/input.css -o static/app.css --minify
        } else {
            Write-Fail "npx not found. Install Node.js and npm."
        }
    } else {
        & npx tailwindcss build -i static/input.css -o static/app.css --minify
    }

    if (Test-Path "static/app.css") {
        $size = (Get-Item "static/app.css").Length / 1KB
        Write-OK ("static/app.css built ({0:N1} KB)" -f $size)
    } else {
        Write-Fail "static/app.css not built"
    }
} else {
    Write-Step "Step 1/3: Skipping CSS build"
}

# ── 2. PyInstaller ──────────────────────────────────────────────────
if (-not $SkipPyInstaller) {
    Write-Step "Step 2/3: Building Python server with PyInstaller"

    # Ensure pyinstaller is installed
    $pyi = Get-Command pyinstaller -ErrorAction SilentlyContinue
    if (-not $pyi) {
        Write-Host "  Installing pyinstaller..."
        & python -m pip install pyinstaller 2>&1 | Out-Null
    }

    & python -m PyInstaller pbc-server.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "PyInstaller build failed"
    }

    if (Test-Path "dist/pbc-server/pbc-server.exe") {
        $size = (Get-Item "dist/pbc-server").Length / 1MB
        Write-OK ("dist/pbc-server/ built ({0:N1} MB)" -f $size)
    } else {
        Write-Fail "dist/pbc-server/pbc-server.exe not found"
    }
} else {
    Write-Step "Step 2/3: Skipping PyInstaller build"
}

# ── 3. Electron-builder ─────────────────────────────────────────────
if (-not $SkipElectron) {
    Write-Step "Step 3/3: Building Electron installer with electron-builder"

    # Ensure electron + electron-builder are installed
    if (-not (Test-Path "node_modules/electron") -or -not (Test-Path "node_modules/electron-builder")) {
        Write-Host "  Installing electron + electron-builder..."
        & npm install 2>&1 | Out-Null
    }

    & npx electron-builder --win --x64
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "electron-builder failed"
    }

    # Find the portable build (BatchSentry-Portable-*.exe)
    $installer = Get-ChildItem "dist-electron" -Filter "BatchSentry-Portable-*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installer) {
        $size = $installer.Length / 1MB
        Write-OK ("Portable build: {0} ({1:N1} MB)" -f $installer.Name, $size)
        Write-Host ""
        Write-Host "  Output: $($installer.FullName)" -ForegroundColor Yellow
    } else {
        Write-Fail "Portable build not found in dist-electron/"
    }
} else {
    Write-Step "Step 3/3: Skipping Electron build"
}

# ── Summary ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Artifacts:"
if (Test-Path "static/app.css") { Write-Host "  - static/app.css" }
if (Test-Path "dist/pbc-server/pbc-server.exe") { Write-Host "  - dist/pbc-server/pbc-server.exe" }
if (Test-Path "dist-electron") {
    Get-ChildItem "dist-electron" -Filter "*.exe" | ForEach-Object {
        Write-Host ("  - dist-electron/{0}" -f $_.Name)
    }
}
