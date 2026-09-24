# ============================================================
# Pharma Batch Checker — Windows build script
# ============================================================
# Produces:
#   1. static/app.css       (Tailwind CLI build, ~14KB)
#   2. dist/pbc-server/     (PyInstaller bundle, ~100MB)
#      └─ build_manifest.json  (入包清单 — 产物新鲜度的判据载体，见 2.6 步)
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

# ── 运行台账（B9-6）─────────────────────────────────────────────────
# 目的：把「中途被杀」与「跑完但失败」**分开**。2026-09-21 实测：把构建放后台
# 跑、回合结束时被连带回收，症状与真崩溃**完全一致**（exit 1 + 零输出 +
# 日志戛然而止无 Traceback + workpath 为空），当时据此误判成"PyInstaller 失败"。
#
# 原理：进程被 TerminateProcess 时 **finally 不会执行**（Windows 强杀不给收尾
# 机会）⇒「有 start 台账、没有 finish 台账」就是**被终止的签名**。
# 判定与解读见 scripts/build_status.py（唯一实现，支持 --json / --assert-state）。
$runDir = Join-Path $projectRoot "build\_run"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
# 只保留最近 20 次运行（40 个文件），避免无限堆积
Get-ChildItem $runDir -Filter "*.json" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -Skip 40 |
    Remove-Item -Force -ErrorAction SilentlyContinue
$runId = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$PID"
$startFile = Join-Path $runDir "$runId.start.json"
$finishFile = Join-Path $runDir "$runId.finish.json"
$gitHeadForLedger = ""
try { $gitHeadForLedger = (& git rev-parse --short HEAD) 2>$null } catch { }
@{
    run_id     = $runId
    started_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    pid        = $PID
    git_head   = "$gitHeadForLedger"
} | ConvertTo-Json | Set-Content -Path $startFile -Encoding UTF8

# 落"结局"。**成功与失败都要落** —— 只落成功的话，"失败"就与"被杀"不可区分了。
function Write-Finish($code, $errText) {
    @{
        run_id      = $runId
        finished_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
        rc          = [int]$code
        error       = "$errText"
    } | ConvertTo-Json | Set-Content -Path $finishFile -Encoding UTF8
}

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
    # 真失败必须**落下退出码** —— 这一步正是与被杀的关键差别：
    # 被杀时本函数根本不会执行（进程已死），台账于是停在"有始无终"。
    Write-Finish 1 $msg
    exit 1
}

# ── 未捕获异常的兜底出口（B9-6）─────────────────────────────────────
# `Write-Fail` 只覆盖**显式失败**（我们能写出可操作提示的那些）。而**未捕获的
# 终止错误**同样是"真失败"，却会绕过它 ⇒ 台账停在"有始无终" ⇒ 被判成"被杀"。
#
# 2026-09-23 实测（正是本条护栏发现的）：`& python --version` 在 python 不在
# PATH 时抛 `CommandNotFoundException` —— 它是 **statement-terminating** 错误，
# 即使 `$ErrorActionPreference="Continue"` 也会中断该语句，并在脚本层
# （EAP=Stop）变成 terminating ⇒ 脚本**直接中断**，pre-flight 后面那句
# `Write-Fail "Python not found"` 根本执行不到。于是"工具缺失"伪装成"进程被杀"。
#
# trap 补上这条出口：任何未捕获的终止错误都留下退出码。内层 try/catch 是防
# "落台账本身又失败"导致 trap 递归。
trap {
    $emsg = "$($_.Exception.Message)"
    Write-Host "  [FAIL] 未捕获异常：$emsg" -ForegroundColor Red
    try { Write-Finish 1 "未捕获异常：$emsg" } catch { }
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
    } catch {
        # 命令**不存在**（CommandNotFoundException）等异常必须在这里截住：
        # 传出去会被脚本级 trap 收成一条晦涩的 CategoryInfo，而调用方的
        # `$LASTEXITCODE -ne 0` 分支才是给得出"怎么修"的地方。
        # ⚠️ 必须**显式**把退出码设成非 0 —— CommandNotFoundException 不会改
        # `$LASTEXITCODE`，它会保持上一条命令的值（可能是 0）⇒ 调用方会把
        # "工具缺失"读成"成功"（2026-09-23 实测）。
        $script:LASTEXITCODE = 1
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
    } catch {
        # 同 Invoke-Native：把"命令不存在"截在这里，并**显式**给出非 0 退出码
        # （CommandNotFoundException 不会动 $LASTEXITCODE）。
        $script:LASTEXITCODE = 1
        return ""
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

# ── 2.6 入包清单（B7-3/B7-4：产物新鲜度的判据载体）──────────────────
# 时机是**刻意的**：必须在 PyInstaller 之后（要绑定 exe 字节）、electron-builder
# 之前（清单随 extraResources 一起进 resources\pbc-server\）。
# 没有它，release_gate 的 artifact_freshness 无法给出结论 ⇒ 直接 FAIL。
# 判据与用法见 scripts/bundle_manifest.py 的模块 docstring。
Write-Step "Step 2.6/3: 生成产物入包清单 build_manifest.json"
if (-not (Test-Path "dist/pbc-server/pbc-server.exe")) {
    Write-Fail "dist/pbc-server/pbc-server.exe 不存在 —— 无法生成清单（先跑 PyInstaller）"
}
$bmLog = Join-Path $projectRoot "build\bundle_manifest.log"
New-Item -ItemType Directory -Force -Path (Split-Path $bmLog) | Out-Null
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & python scripts/bundle_manifest.py --write *> $bmLog
} finally {
    $ErrorActionPreference = $prevEAP
}
if ($LASTEXITCODE -ne 0) {
    Get-Content $bmLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Fail "写入 build_manifest.json 失败（日志: $bmLog）"
}
if (-not (Test-Path "dist/pbc-server/build_manifest.json")) {
    Write-Fail "dist/pbc-server/build_manifest.json 未生成"
}
# 写完立刻自校验：清单必须与刚构建出来的产物一致（把"门禁才发现"提前到构建现场）
& python scripts/bundle_manifest.py --check *> $bmLog
if ($LASTEXITCODE -ne 0) {
    Get-Content $bmLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Fail "产物与清单不一致（日志: $bmLog）—— 产物新鲜度自检未通过"
}
Write-OK "dist/pbc-server/build_manifest.json 已生成并自校验通过"

# ── 3. Electron-builder ─────────────────────────────────────────────
if (-not $SkipElectron) {
    Write-Step "Step 3/3: Building Electron installer with electron-builder"

    # ── 3.0 Electron 二进制获取通道（2026-09-23 实测新增，必须）─────────
    # electron-builder 打包**必须**拿到 `electron-v<ver>-win32-x64.zip`。
    # 本机实测三条事实：
    #   · 官方 GitHub releases **不可达** —— 直连 curl=000；走系统代理 =502 Bad Gateway
    #     （同刻 registry.npmjs.org 可达 ⇒ 只有 GitHub 被挡，不是断网）。
    #   · 可达的镜像是 npmmirror ⇒ 显式指向它，否则本步**必然失败**
    #     （实测：不加该变量跑冒烟即 `⨯ Response code 502 (Bad Gateway)`）。
    #   · 外部若已设 ELECTRON_MIRROR（自建镜像/企业代理）⇒ **不覆盖**。
    # ⚠️ 版本只能选镜像上**确实存在**的：43.7.5 在 npmmirror / 华为云 / 腾讯云 / 清华
    #    全部 404，43.7.4 全部 200 ⇒ `electron` 已按**精确版本**钉在 `43.7.4`
    #    （**不能**写 `^43.7.4` —— 那会解析回 43.7.5，又变成"拿不到的版本"）。
    #    教训见 docs/PROJECT_PITFALLS.md §四十五。
    if (-not $env:ELECTRON_MIRROR) {
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
        Write-Host "  [info] ELECTRON_MIRROR=$env:ELECTRON_MIRROR （GitHub 不可达，走镜像）"
    }

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
    # 占用（BatchSentry.exe 未退出；或外部进程正持有 app.asar 的句柄），
    # EnsureEmptyDir 会以 "The process cannot access the file" 失败，
    # 报错栈是 app-builder 的 Go 内部栈，极难定位。
    #
    # 自愈策略（M8 起实测：外部进程会长期持有 app.asar 句柄，重启应用亦不释放。
    # ⚠️ 归因已于 2026-09-17 更正：**不是安全软件**，是**宿主进程**把 .asar 当"包"
    # 打开后留下未带 FILE_SHARE_DELETE 的句柄 —— 详见 docs/PROJECT_PITFALLS.md §二十二）：
    #   检测到占用 → 自动切到**带时间戳**的备用输出目录（每次唯一）；
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
        # 但备用目录自己写过之后，其 app.asar 同样会被该进程持有 ——
        # 2026-09-16 实测：dist-electron / dist-electron-locked / dist-electron-v112 /
        # dist-electron-m8 四个目录的 app.asar **全部被锁**（PermissionError 32，
        # 重启不释放）。也就是说固定名备用目录 = **第二次自愈必然失败**。
        # 按时间戳生成即可保证唯一，不需要事先探测。
        $outDir = "dist-electron-out-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Write-Host "  [WARN] $stdUnpacked 被占用（resources\app.asar 无法重命名）。" -ForegroundColor Yellow
        Write-Host "         原因：BatchSentry.exe 未退出，或外部进程正持有上次产物句柄" -ForegroundColor Yellow
        Write-Host "         （2026-09-17 实测：本机为宿主 WorkBuddy —— 它把 .asar 当包打开；" -ForegroundColor Yellow
        Write-Host "          解锁须完全退出该进程，加杀软白名单无效。详见 docs/PROJECT_PITFALLS.md §二十二）。" -ForegroundColor Yellow
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
    # 3.2 产物出处留痕（round-27 卫生规则 R2 + D5「唯一产物能自证出处」）：
    # **无条件**写 PROVENANCE.txt —— 出处（HEAD/时间/版本）+ 收敛指引。
    # ⚠️ 曾只在备用目录写（`if ($finalUnpacked -ne $stdUnpacked)`），2026-09-24
    # 实测暴露契约断裂：`release_gate.py::count_complete_artifacts` 把
    # 「win-unpacked/PROVENANCE.txt 存在」当作完整产物的三件套之一 ⇒
    # 标准路径构建的产物**永远被判残壳**、`runtime_eol` 永远 SKIP —— 门禁在
    # 「产物最标准的状态」上反而最瞎。旧轮次未暴露只因当时完整产物恰好
    # 来自被锁时的备用目录（天然带 PROVENANCE）。
    # ⚠️ 语句必须平铺，不得包裸 `{...}`：PowerShell 语句位的 scriptblock 是
    # **表达式**（被输出、不执行）—— 首版修复即因此"看似成功实则从未写入"。
    $gitHead = ""
    try { $gitHead = (& git rev-parse --short HEAD) 2>$null } catch {}
    $appVer = ""
    try {
        $appVer = (Select-String -Path "main.py" -Pattern 'APP_VERSION = "(.+)"').Matches[0].Groups[1].Value
    } catch {}
    $prov = Join-Path $finalUnpacked "PROVENANCE.txt"
    $origin = if ($finalUnpacked -ne $stdUnpacked) {
        "fallback output: standard dist-electron was locked at build time"
    } else {
        "standard output: dist-electron was NOT locked at build time"
    }
    @(
        $origin
        "built_at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "git_head: $gitHead"
        "version:  $appVer"
        "verify:   `$env:PBC_E2E_EXE = '<this dir>\resources\pbc-server\pbc-server.exe'; python tests/e2e_frozen.py"
        "cleanup:  python scripts/clean_dist.py  (dry-run first; --apply sends to recycle bin)"
        "unblock:  fully EXIT the process holding resources\app.asar, then re-run"
        "          'python scripts/clean_dist.py --apply'. Measured on this host"
        "          (2026-09-17): the holder is the WorkBuddy host process - it opens"
        "          .asar as a package and keeps a handle without FILE_SHARE_DELETE."
        "          An antivirus allow-list does NOT help (the holder is not AV)."
        "          Details: docs/PROJECT_PITFALLS.md section 22."
    ) -join "`r`n" | Set-Content -Path $prov -Encoding UTF8
    Write-Host "  [INFO] PROVENANCE.txt written to $prov" -ForegroundColor DarkGray

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
# 先落"成功"结局再打印总结（总结里的 Write-Host 万一出错，不该让台账漏记结局）。
Write-Finish 0 ""

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Artifacts:"
if (Test-Path "static/app.css") { Write-Host "  - static/app.css" }
if (Test-Path "dist/pbc-server/pbc-server.exe") { Write-Host "  - dist/pbc-server/pbc-server.exe" }
if (Test-Path "dist/pbc-server/build_manifest.json") { Write-Host "  - dist/pbc-server/build_manifest.json (入包清单)" }
if (Test-Path "dist-electron\win-unpacked\BatchSentry.exe") {
    Write-Host "  - dist-electron\win-unpacked\ (folder, run BatchSentry.exe)"
}
Write-Host ""
Write-Host "Next: python scripts/release_gate.py   # 9 项门禁（含产物新鲜度）"
