# ============================================================
# Pharma Batch Checker — Windows build script
# ============================================================
# Produces:
#   1. static/app.css       (Tailwind CLI build, ~14KB)
#   2. dist/pbc-server/     (PyInstaller bundle, ~100MB)
#   3. dist-electron/win-unpacked/  (Electron 文件夹便携版, ~640MB)
#      └─ BatchSentry.exe   (双击运行，无需安装)
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
#   .\build.ps1 -SkipElectron     # skip Electron packaging
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
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install failed (exit code $LASTEXITCODE)"
        }
    }

    # Resolve npx via PATH; fall back to npm-cli.js inside node_modules
    # (works on any machine without hardcoded paths).
    $npx = Get-Command npx -ErrorAction SilentlyContinue
    if ($npx) {
        & npx tailwindcss build -i static/input.css -o static/app.css --minify
    } elseif (Test-Path "node_modules\npm\bin\npx-cli.js") {
        & node node_modules\npm\bin\npx-cli.js tailwindcss build -i static/input.css -o static/app.css --minify
    } else {
        Write-Fail "npx not found on PATH and node_modules\\npm\\bin\\npx-cli.js missing. Run 'npm install' first."
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

    # ── 2.5 冒烟测试（robustness-F1）───────────────────────────
    # 启动刚构建的 exe，轮询 /health，通过后再继续打包。exe 启动即崩
    # 时在此暴露（此前会产出无法运行的 Electron 包）。
    Write-Step "Smoke test: 启动 pbc-server.exe 验证 /health"
    $proc = $null
    $smokeOk = $false
    try {
        # 端口可能已被开发中运行的实例占用：若 /health 已响应则视为通过
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:58765/health" -TimeoutSec 2 -UseBasicParsing
            if ($resp.StatusCode -eq 200) {
                Write-OK "  /health already responds (existing instance) — skip spawn"
                $smokeOk = $true
            }
        } catch { }

        if (-not $smokeOk) {
            $proc = Start-Process -FilePath "dist/pbc-server/pbc-server.exe" -PassThru -WindowStyle Hidden
            for ($i = 0; $i -lt 30; $i++) {
                Start-Sleep -Milliseconds 1000
                if ($proc.HasExited) { break }
                try {
                    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:58765/health" -TimeoutSec 2 -UseBasicParsing
                    if ($resp.StatusCode -eq 200) { $smokeOk = $true; break }
                } catch { }
            }
            if ($smokeOk) {
                Write-OK "  /health OK (startup ~$($i + 1)s)"
            }
        }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
        }
    }
    if (-not $smokeOk) {
        Write-Fail "Smoke test failed: pbc-server.exe 未能在 30s 内响应 /health"
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
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install failed (exit code $LASTEXITCODE)"
        }
    }

    & npx electron-builder --win --x64
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "electron-builder failed"
    }

    # dir target produces win-unpacked/ folder (not a single exe)
    $exePath = "dist-electron\win-unpacked\BatchSentry.exe"
    if (Test-Path $exePath) {
        $size = (Get-Item $exePath).Length / 1MB
        Write-OK ("win-unpacked\BatchSentry.exe built ({0:N1} MB)" -f $size)
        $totalSize = (Get-ChildItem "dist-electron\win-unpacked" -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
        Write-Host ""
        # 注意：必须用括号包住 -f 格式化表达式，否则 PowerShell 会把 -f 当成
        # Write-Host 的参数（导致 "Cannot bind parameter 'ForegroundColor'" 错误）
        Write-Host ("  Output: dist-electron\win-unpacked\ (total {0:N1} MB)" -f $totalSize) -ForegroundColor Yellow
        Write-Host "  Run:    dist-electron\win-unpacked\BatchSentry.exe" -ForegroundColor Yellow
    } else {
        Write-Fail "win-unpacked\BatchSentry.exe not found"
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
if (Test-Path "dist-electron\win-unpacked\BatchSentry.exe") {
    Write-Host "  - dist-electron\win-unpacked\ (folder, run BatchSentry.exe)"
}
