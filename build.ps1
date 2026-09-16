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

# PS 5.1 坑: $ErrorActionPreference="Stop" 时 native stderr（Browserslist 过时
# 警告、PyInstaller INFO 日志等）会抛 NativeCommandError 中断脚本，且
# `2>&1 | Out-Null` 重定向仍受 preference 影响挡不住。标准解法：调用期间
# 临时降级 EAP，成败只看 $LASTEXITCODE。
function Invoke-Native {
    param([scriptblock]$Command)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

# 同 Invoke-Native，但把输出读回为字符串（--version 类探测需要文本）。
# 缺 PyInstaller 时 `python -m PyInstaller --version` 把
# "No module named PyInstaller" 写到 stderr —— EAP=Stop 下会抛
# NativeCommandError 直接中断构建（曾导致 pre-flight 后静默退出）。
function Invoke-NativeText {
    param([scriptblock]$Command)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $Command 2>&1 | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

# ── Pre-flight: verify required tools are on PATH ────────────────────
Write-Step "Pre-flight checks"

$pythonVersion = Invoke-NativeText { & python --version }
Write-Host "  Python: $pythonVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "Python not found on PATH. Install Python 3.11+ and retry." }

# 解释器正确性校验：PATH 上的 python 必须是装了本项目运行时依赖的那个。
# 若 PATH 首位是"干净"解释器（如未装依赖的 managed Python），PyInstaller
# 会构建出残缺包或在中途才以晦涩错误失败。此处前置失败并给出可操作提示。
$depProbe = Invoke-NativeText { & python -c "import fastapi, fitz, uvicorn, aiosqlite, httpx; print('deps-ok')" }
if ($LASTEXITCODE -ne 0 -or $depProbe -notmatch "deps-ok") {
    Write-Fail "PATH 上的 python 缺少项目依赖(fastapi/fitz/uvicorn/aiosqlite/httpx)。请把装了依赖的解释器目录放到 PATH 最前（如 Python311）后重试。当前: $pythonVersion"
}
Write-Host "  Python deps: OK"

$nodeVersion = Invoke-NativeText { & node --version }
Write-Host "  Node:   $nodeVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "Node.js not found on PATH. Install Node.js 20+ and retry." }

$npmVersion = Invoke-NativeText { & npm --version }
Write-Host "  npm:    $npmVersion"
if ($LASTEXITCODE -ne 0) { Write-Fail "npm not found on PATH." }

# Verify pyinstaller availability (will be auto-installed below if missing)
$pyi = Get-Command pyinstaller -ErrorAction SilentlyContinue
$pyiModule = Invoke-NativeText { & python -m PyInstaller --version }
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
        Invoke-Native { & npm install }
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install failed (exit code $LASTEXITCODE)"
        }
    }

    # Resolve npx via PATH; fall back to npm-cli.js inside node_modules
    # (works on any machine without hardcoded paths).
    $npx = Get-Command npx -ErrorAction SilentlyContinue
    if ($npx) {
        Invoke-Native { & npx tailwindcss build -i static/input.css -o static/app.css --minify }
    } elseif (Test-Path "node_modules\npm\bin\npx-cli.js") {
        Invoke-Native { & node node_modules\npm\bin\npx-cli.js tailwindcss build -i static/input.css -o static/app.css --minify }
    } else {
        Write-Fail "npx not found on PATH and node_modules\\npm\\bin\\npx-cli.js missing. Run 'npm install' first."
    }
    if ($LASTEXITCODE -ne 0) { Write-Fail "tailwindcss build failed (exit code $LASTEXITCODE)" }

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
        Invoke-Native { & python -m pip install pyinstaller }
    }

    # PyInstaller 的 INFO 日志走 stderr — 用 EAP 降级 + 落盘日志（失败时可诊断，
    # 之前 Invoke-Native 把输出 Out-Null 掉，FAIL 时无从查因）。
    $pyiLog = Join-Path $projectRoot "build\pyinstaller.log"
    New-Item -ItemType Directory -Force -Path (Split-Path $pyiLog) | Out-Null
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -m PyInstaller pbc-server.spec --noconfirm --clean *> $pyiLog
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  --- PyInstaller 日志末尾 25 行 ---" -ForegroundColor Yellow
        Get-Content $pyiLog -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Fail "PyInstaller build failed (完整日志: $pyiLog)"
    }

    if (Test-Path "dist/pbc-server/pbc-server.exe") {
        # Get-Item 对目录返回 Length=0 — 统计 exe 实际大小（含 _internal 需递归）
        $total = (Get-ChildItem "dist/pbc-server" -Recurse -File |
            Measure-Object -Property Length -Sum).Sum / 1MB
        Write-OK ("dist/pbc-server/ built ({0:N1} MB)" -f $total)
    } else {
        Write-Fail "dist/pbc-server/pbc-server.exe not found"
    }

    # ── 2.5 冒烟测试（robustness-F1）───────────────────────────
    # 启动刚构建的 exe，轮询 /health，通过后再继续打包。exe 启动即崩
    # 时在此暴露（此前会产出无法运行的 Electron 包）。
    # 即便 58765 已有响应（开发实例/旧 exe 占用），也要验证"新 exe 本身
    # 可启动"——直接 spawn 并轮询，成功后停掉，不依赖端口空闲。
    Write-Step "Smoke test: 启动 pbc-server.exe 验证 /health"
    $proc = $null
    $smokeOk = $false
    $skipSmoke = $false
    try {
        # 端口占用分类：pbc-server.exe（旧产物）→ 停掉后换新验证；
        # 其他进程（dev uvicorn 等）→ 不动，警告并跳过（避免误杀开发环境）。
        $holder = (netstat -ano | Select-String ":58765\s" | Select-String "LISTENING" |
            Select-Object -First 1)
        if ($holder) {
            $holderPid = [int](($holder.ToString().Trim() -split "\s+")[-1])
            $holderName = (Get-Process -Id $holderPid -ErrorAction SilentlyContinue).ProcessName
            if ($holderName -eq "pbc-server") {
                Stop-Process -Id $holderPid -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
            } else {
                Write-Warning "  58765 被非 pbc-server 进程占用 ($holderName, PID $holderPid) — 跳过 smoke，请手动验证新 exe"
                $skipSmoke = $true
            }
        }
        if (-not $skipSmoke) {
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
            # EAP=Stop 下 taskkill 找不到 PID（Stop-Process 已杀）会把 stderr
            # 变终止错误，中断整个构建 — 必须 try/catch 包住
            try {
                & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
            } catch { }
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
        Invoke-Native { & npm install }
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "npm install failed (exit code $LASTEXITCODE)"
        }
    }

    # ── 3.0 占用检测 + 输出目录自愈 ────────────────────────────────
    # electron-builder 先清空 win-unpacked 再重装 Electron。若上一次产物仍被
    # 占用（BatchSentry.exe 未退出；或安全软件正在扫描 190MB 的 app.asar/
    # exe），EnsureEmptyDir 会以 "The process cannot access the file" 失败，
    # 报错栈是 app-builder 的 Go 内部栈，极难定位。
    #
    # 自愈策略（M8 实测：火绒类实时防护会长期持有 app.asar 句柄，重启亦不释放）：
    #   检测到占用 → 自动切到备用输出目录 dist-electron-locked；
    #   构建成功后 → best-effort 归位到标准 dist-electron\win-unpacked；
    #   归位仍失败 → 保留备用目录并打印可执行的归位命令（不使构建整体失败）。
    $stdUnpacked = "dist-electron\win-unpacked"
    $outDir = "dist-electron"
    $locked = $false
    if (Test-Path $stdUnpacked) {
        $lockProbe = Join-Path $stdUnpacked "resources\app.asar"
        if (Test-Path $lockProbe) {
            try {
                Rename-Item -Path $lockProbe -NewName "app.asar.lockprobe" -ErrorAction Stop
                Rename-Item -Path (Join-Path $stdUnpacked "resources\app.asar.lockprobe") -NewName "app.asar"
            } catch {
                $locked = $true
            }
        }
    }
    if ($locked) {
        # 备用目录名必须**每次都是新的**。历史实现用固定名 `dist-electron-locked`，
        # 但备用目录自己写过之后，其 app.asar 同样会被安全软件持有 ——
        # 2026-09-16 实测：dist-electron / dist-electron-locked / dist-electron-v112 /
        # dist-electron-m8 四个目录的 app.asar **全部被锁**（PermissionError 32，
        # 重启不释放）。也就是说固定名备用目录 = **第二次自愈必然失败**。
        # 按时间戳生成即可保证唯一，不需要事先探测。
        $outDir = "dist-electron-out-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Write-Host "  [WARN] $stdUnpacked 被占用（resources\app.asar 无法重命名）。" -ForegroundColor Yellow
        Write-Host "         原因：BatchSentry.exe 未退出，或安全软件正持有上次产物句柄。" -ForegroundColor Yellow
        Write-Host "         自愈：本次改用全新输出目录 $outDir（固定名备用目录是一次性的）。" -ForegroundColor Yellow
    }

    # electron-builder 的进度/警告输出走 stderr — 落盘日志，失败可诊断。
    $ebLog = Join-Path $projectRoot "build\electron-builder.log"
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($locked) {
            & npx electron-builder --win --x64 "-c.directories.output=$outDir" *> $ebLog
        } else {
            & npx electron-builder --win --x64 *> $ebLog
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  --- electron-builder 日志末尾 25 行 ---" -ForegroundColor Yellow
        Get-Content $ebLog -Tail 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        Write-Fail "electron-builder failed (完整日志: $ebLog)"
    }

    # ── 3.1 best-effort 归位到标准路径 ────────────────────────────
    # 归位前**必须再探一次**占用：直接 Remove-Item 一个仍被占用的目录，会在删到
    # 被持有的 app.asar 时失败 —— 而失败前它已经删掉了目录里的一部分文件，把
    # 一个完好的旧产物变成残缺目录（比不归位更糟，且会让"最新产物"判定指向
    # 一个残缺目录）。故只有确认标准目录已可写才动手。
    $finalUnpacked = $stdUnpacked
    if ($locked) {
        $finalUnpacked = Join-Path $outDir "win-unpacked"
        $canMoveBack = $false
        $probeFile = Join-Path $stdUnpacked "resources\app.asar"
        if (-not (Test-Path $probeFile)) {
            $canMoveBack = $true
        } else {
            try {
                Rename-Item -Path $probeFile -NewName "app.asar.lockprobe" -ErrorAction Stop
                Rename-Item -Path (Join-Path $stdUnpacked "resources\app.asar.lockprobe") -NewName "app.asar" -ErrorAction Stop
                $canMoveBack = $true
            } catch {
                $canMoveBack = $false
            }
        }
        if ($canMoveBack) {
            $moved = $false
            try {
                Remove-Item -Recurse -Force $stdUnpacked -ErrorAction Stop
                Move-Item -Path $finalUnpacked -Destination $stdUnpacked -ErrorAction Stop
                $moved = $true
            } catch {
                $moved = $false
            }
            if ($moved) {
                Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue
                $finalUnpacked = $stdUnpacked
                Write-OK "占用已释放，产物已归位 $stdUnpacked"
            } else {
                Write-Host "  [WARN] 归位失败，产物保留在 $finalUnpacked" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  [WARN] 未尝试归位（$stdUnpacked 仍被占用）。产物保留在 $finalUnpacked" -ForegroundColor Yellow
            Write-Host "         释放占用后手动归位：" -ForegroundColor Yellow
            Write-Host "           Remove-Item -Recurse -Force $stdUnpacked" -ForegroundColor Yellow
            Write-Host "           Move-Item $finalUnpacked $stdUnpacked" -ForegroundColor Yellow
        }
    }

    # dir target produces win-unpacked/ folder (not a single exe)
    # 3.2 变体目录留痕（round-27 卫生规则 R2）：凡最终产物不在标准路径
    # dist-electron/win-unpacked（安全软件锁 app.asar 时自愈到带时间戳的
    # 备用目录且归位失败），就地写 PROVENANCE.txt —— 出处（HEAD/时间/版本）
    # + 收敛指引。没有出身的变体目录会被下一个会话当成"身份不明垃圾"，
    # 判定成本（哈希比对/健康探测/时间线推理）远高于写这个文件的成本。
    if ($finalUnpacked -ne $stdUnpacked) {
        $gitHead = ""
        try { $gitHead = (& git rev-parse --short HEAD) 2>$null } catch {}
        $appVer = ""
        try {
            $appVer = (Select-String -Path "main.py" -Pattern 'APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
        } catch {}
        $prov = Join-Path $finalUnpacked "PROVENANCE.txt"
        @(
            "fallback output: standard dist-electron was locked at build time"
            "built_at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
            "git_head: $gitHead"
            "version:  $appVer"
            "verify:   `$env:PBC_E2E_EXE = '<this dir>\resources\pbc-server\pbc-server.exe'; python tests/e2e_frozen.py"
            "cleanup:  python scripts/clean_dist.py  (dry-run first; --apply sends to recycle bin)"
        ) -join "`r`n" | Set-Content -Path $prov -Encoding UTF8
        Write-Host "  [INFO] PROVENANCE.txt written to $prov" -ForegroundColor DarkGray
    }

    $exePath = Join-Path $finalUnpacked "BatchSentry.exe"
    if (Test-Path $exePath) {
        $size = (Get-Item $exePath).Length / 1MB
        Write-OK ("BatchSentry.exe built ({0:N1} MB)" -f $size)
        $totalSize = (Get-ChildItem $finalUnpacked -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
        Write-Host ""
        # 注意：必须用括号包住 -f 格式化表达式，否则 PowerShell 会把 -f 当成
        # Write-Host 的参数（导致 "Cannot bind parameter 'ForegroundColor'" 错误）
        Write-Host ("  Output: $finalUnpacked\ (total {0:N1} MB)" -f $totalSize) -ForegroundColor Yellow
        Write-Host "  Run:    $exePath" -ForegroundColor Yellow
    } else {
        Write-Fail "$finalUnpacked\BatchSentry.exe not found"
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
