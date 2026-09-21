/**
 * BatchSentry — Electron Main Process
 *
 * Architecture:
 *   Electron main (this file)
 *     └─ spawn → pbc-server.exe (PyInstaller bundle, contains Python + FastAPI)
 *                └─ uvicorn listening on 127.0.0.1:58765
 *     └─ BrowserWindow → loads http://127.0.0.1:58765/
 *
 * Lifecycle:
 *   1. app.whenReady() → show splash window (instant feedback)
 *   2. pre-flight port check (58765 must be free or owned by us)
 *   3. spawn pbc-server.exe + poll /health until ready (up to 30s)
 *   4. splash → main window, load the app URL
 *   5. on quit: POST /api/shutdown (后端自行优雅退出) → 等端口释放 → 超时才 taskkill /T /F
 */

const { app, BrowserWindow, shell, dialog, Menu } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");

// Phase 8: remove the default Electron menu bar (File/Edit/View/Window/Help).
// BatchSentry is a single-window app — the menu bar adds visual clutter with
// no useful functionality. Must be set before app.whenReady() for it to take
// effect on the first window.
Menu.setApplicationMenu(null);

// 2026-08: splash 卡顿修复 — 在 VM/远程桌面/无 GPU 驱动环境下 Chromium 检测
// 不到可用 GPU，全部回退软件渲染（gpu_compositing=disabled_software）。
// 软件合成器默认把帧率节流在 ~30fps，CSS 线性旋转动画观感明显卡顿。
// 该 switch 解除合成器帧率上限（实测 30→~40fps，软件光栅化本身有上限，
// 剩余观感问题由 createSplashWindow 的 steps 离散动画兜底）。
if (process.platform === "win32") {
  app.commandLine.appendSwitch("disable-frame-rate-limit");
}

const SERVER_PORT = 58765;
const SERVER_HOST = "127.0.0.1";

// 对抗审查（分发实证）：冷启动失败不可诊断 —— uvicorn 的绑定时间线只打在
// stdout，打包后被 console 吞掉；健康检查把 ECONNREFUSED/超时一律静默计数，
// 超时报 "not ready after 60 checks" 却无法区分原因。两处修复：
// 1) 后端 stdout/stderr 追加写入 %APPDATA%/PBC/logs/backend-boot.log
// 2) 等待逻辑改截止时间制（180s），子进程存活就继续等；错误带进拒绝消息
const READY_TIMEOUT_MS = 180_000;

let reusedBackend = false; // 复用孤儿后端时 waitForServer 不要求子进程存活

let bootLogStream = null;

function bootLog(line) {
  try {
    if (!bootLogStream) {
      const dir = path.join(app.getPath("appData"), "PBC", "logs");
      fs.mkdirSync(dir, { recursive: true });
      bootLogStream = fs.createWriteStream(path.join(dir, "backend-boot.log"), {
        flags: "a",
      });
    }
    bootLogStream.write(
      `${new Date().toISOString()} ${line}\n`,
    );
  } catch {
    // 日志尽力而为，绝不阻断启动流程
  }
}
// 优雅关闭的时间预算（v1.2.0 重写）。
// 旧实现 = "发 /api/shutdown → 固定 sleep 2.5s → SIGTERM →（疑似）taskkill 兜底"，
// 两个已实证的缺陷：
//   ① Windows 上 Node 的 SIGTERM 等价于 TerminateProcess，**不执行任何 Python 代码**
//      ⇒ uvicorn 的 lifespan 收尾从不运行、`close_db()` 不执行、SQLite -wal/-shm 残留
//      （实测 devlogs/_verify/probe_shutdown_semantics.py）；
//   ② 兜底条件写的是 `!pythonProcess.killed`，而 Node 的 `killed` 在**信号发出时**
//      就置真（语义是"已发送信号"，不是"已退出"）⇒ 条件恒假 ⇒ `taskkill /T /F`
//      是**死代码**；复用孤儿路径（pythonProcess 为 null）更是整体跳过终止。
// 现在：请求优雅退出 → 按**可观测的端口释放**等待 → 超时才升级强杀。
const SHUTDOWN_EXIT_WAIT_MS = 6000; // 等后端自行（优雅）退出的上限
const SHUTDOWN_KILL_WAIT_MS = 3000; // 强杀后再等端口释放的上限

// robustness-G1: 看门狗 — 后端运行中自崩/僵死时自动重启。
// 探测间隔 15s，连续失败 3 次（约 45s 无响应）判定崩溃。
// 端口误占（其他程序抢端口）时 waitForServer 会失败并保持原样退出，
// 不做无限重启（避免与其他程序端口打架）。
const WATCHDOG_INTERVAL_MS = 15000;
const WATCHDOG_MAX_FAILURES = 3;
const WATCHDOG_RETRY_DELAY_MS = 2000;

let pythonProcess = null;
let reusedPid = null; // 对抗审查 P2-K：复用的孤儿 pbc-server.exe PID（退出时需清理）
let spawnError = null; // 对抗审查 P2-L：spawn error 事件记录，waitForServer 立即失败
let mainWindow = null;
let splashWindow = null;
let isShuttingDown = false;
let watchdogTimer = null;
let watchdogFailures = 0;
let watchdogRestarting = false;

// robustness-F2: 单实例锁 — 双击工具最常见的误用是重复启动。第二次启动
// 时聚焦已有实例窗口而非弹"端口冲突"框；强杀 Electron 残留的孤儿
// pbc-server.exe 仍由 isPortFree 预检兜底提示（main.js:332）。
const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  // 已有实例在运行：退出本进程，主实例会收到 second-instance 事件
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

/**
 * Check if a TCP port is free (nothing listening on it).
 * Used to detect port conflicts before spawning the server.
 *
 * Phase 8: prevents confusing "Backend startup timed out" errors when
 * another BatchSentry instance (or another app) is already on 58765.
 */
function isPortFree(port) {
  return new Promise((resolve) => {
    const tester = net
      .createServer()
      .once("error", () => resolve(false))
      .once("listening", () => {
        tester.once("close", () => resolve(true)).close();
      })
      .listen(port, SERVER_HOST);
  });
}

/**
 * Resolve the Python server executable path.
 *
 * In production (packaged): resources/pbc-server/pbc-server.exe
 * In development: fall back to `python server.py` for local testing.
 */
function resolveServerCommand() {
  const isPackaged = app.isPackaged;
  const exeName =
    process.platform === "win32" ? "pbc-server.exe" : "pbc-server";

  if (isPackaged) {
    const serverPath = path.join(process.resourcesPath, "pbc-server", exeName);
    return { cmd: serverPath, args: [], isDev: false };
  }

  // Dev mode: run `python server.py` from the project root
  const projectRoot = path.resolve(__dirname, "..");
  return { cmd: "python", args: ["server.py"], isDev: true, cwd: projectRoot };
}

/**
 * Start the Python server as a child process.
 */
function startPythonServer() {
  spawnError = null; // 每次重启清空上次 spawn 失败记录
  const { cmd, args, isDev, cwd } = resolveServerCommand();
  const env = {
    ...process.env,
    PORT: String(SERVER_PORT),
    PYTHONUNBUFFERED: "1",
  };

  console.log(`[BatchSentry] Spawning server: ${cmd} ${args.join(" ")}`);
  // 打包模式不在 console 回显绝对路径（同 #5）；bootLog 落盘保留
  if (!app.isPackaged) {
    bootLog(`[spawn] ${cmd} ${args.join(" ")} port=${SERVER_PORT}`);
  } else {
    bootLog(`[spawn] pbc-server.exe port=${SERVER_PORT}`);
  }

  pythonProcess = spawn(cmd, args, {
    cwd: cwd || undefined,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  pythonProcess.stdout.on("data", (data) => {
    const msg = data.toString().trim();
    if (msg) {
      // 对抗审查（分发实证 #5）：打包模式 console 不再回显含绝对路径的
      // 后端输出（用户 DevTools 可见，观感差且暴露本机目录）；完整内容
      // 始终落盘 %APPDATA%/PBC/logs/backend-boot.log 供排障。
      if (!app.isPackaged) console.log(`[pbc-server] ${msg}`);
      bootLog(`[stdout] ${msg}`);
    }
  });

  pythonProcess.stderr.on("data", (data) => {
    const msg = data.toString().trim();
    if (msg) {
      if (!app.isPackaged) console.error(`[pbc-server] ${msg}`);
      bootLog(`[stderr] ${msg}`);
    }
  });

  pythonProcess.on("error", (err) => {
    // 对抗审查 P2-L：spawn 失败（exe 缺失/无权限/被杀软拦截）此前只打
    // 日志，waitForServer 继续空轮询满 60 次（30s）后才报误导性的
    // "启动超时"。此处把错误记录到全局，waitForServer 立即失败。
    console.error("[BatchSentry] Failed to start server:", err);
    bootLog(`[spawn-error] ${err.message}`);
    spawnError = err;
  });

  pythonProcess.on("exit", (code, signal) => {
    console.log(`[BatchSentry] Server exited (code=${code} signal=${signal})`);
    bootLog(`[exit] code=${code} signal=${signal}`);
    pythonProcess = null;
  });
}

/**
 * Poll the server health endpoint until ready (or timeout).
 */
function waitForServer() {
  return new Promise((resolve, reject) => {
    let checks = 0;
    let lastError = "no error captured"; // 对抗审查：失败时带出真实原因
    // 对抗审查（分发实证）：固定 60 次×500ms 会误杀"慢但健康"的冷启动
    // （杀软深度扫描实测可拖 >145s 才到 uvicorn）。改为截止时间制：
    // 子进程存活就继续等（上限 180s）；进程退出/spawn 失败立即失败。
    const deadline = Date.now() + READY_TIMEOUT_MS;

    const check = () => {
      if (spawnError) {
        reject(spawnError);
        return;
      }
      if (!pythonProcess && !reusedBackend) {
        reject(new Error(
          `Server process exited before becoming ready (last error: ${lastError})`,
        ));
        return;
      }
      if (Date.now() > deadline) {
        reject(new Error(
          `Server not ready within 180s (last error: ${lastError}) `
          + `— see %APPDATA%/PBC/logs/backend-boot.log`,
        ));
        return;
      }
      const req = http.get(
        `http://${SERVER_HOST}:${SERVER_PORT}/health`,
        (res) => {
          if (res.statusCode === 200) {
            console.log("[BatchSentry] Server ready");
            bootLog(`[ready] after ${checks} failed checks`);
            resolve();
          } else {
            lastError = `HTTP ${res.statusCode}`;
            retry();
          }
          res.resume();
        },
      );

      req.on("error", (err) => {
        lastError = err.code || err.message; // ECONNREFUSED / ETIMEDOUT / ...
        retry();
      });
      req.setTimeout(2000, () => {
        req.destroy();
        lastError = "request timeout (2s)";
        retry();
      });
    };

    const retry = () => {
      checks += 1;
      // 每 20 次（约 10s）把进度与当前错误刷到 splash + 日志
      if (checks > 0 && checks % 20 === 0) {
        setSplashStatus(`仍在等待后端就绪…（${Math.round(checks / 2)}s）`);
        console.warn(
          `[BatchSentry] health check #${checks}: still waiting (${lastError})`,
        );
        bootLog(`[wait] check=${checks} lastError=${lastError}`);
      }
      setTimeout(check, 500);
    };

    check();
  });
}

/**
 * robustness-G1: 后端看门狗 — 主窗口打开后定期探测 /health。
 * 连续 WATCHDOG_MAX_FAILURES 次失败 → 认为后端崩溃/僵死：
 *   1. 强杀残留进程树（若句柄仍存活）
 *   2. 重新 spawn + waitForServer（复用启动期逻辑）
 *   3. 成功后重置计数，并 reload 主窗口（数据在 SQLite，重载无损失）
 * 关闭流程（isShuttingDown）期间停止探测，避免与优雅关闭竞争。
 */
function startWatchdog() {
  if (watchdogTimer) return;
  watchdogTimer = setInterval(async () => {
    if (isShuttingDown || watchdogRestarting) return;
    const healthy = await probeHealth(SERVER_PORT);
    if (healthy) {
      watchdogFailures = 0;
      return;
    }
    watchdogFailures += 1;
    console.warn(
      `[BatchSentry] Watchdog: health probe failed (${watchdogFailures}/${WATCHDOG_MAX_FAILURES})`
    );
    if (watchdogFailures < WATCHDOG_MAX_FAILURES) return;

    // 判定崩溃 → 重启
    watchdogRestarting = true;
    console.error("[BatchSentry] Watchdog: backend unhealthy, restarting...");
    try {
      // 存活判据必须是 exitCode/signalCode（见 childAlive）；这里旧写法用了
      // `!pythonProcess.killed`，与 gracefulShutdown 里那处同源错误 —— 已收敛到
      // `childAlive` + `killProcessTree`，taskkill 逻辑全仓只剩一处实现。
      if (childAlive(pythonProcess)) {
        killProcessTree(pythonProcess.pid);
      }
      pythonProcess = null;
      await new Promise((r) => setTimeout(r, WATCHDOG_RETRY_DELAY_MS));
      startPythonServer();
      try {
        await waitForServer();
        console.log("[BatchSentry] Watchdog: backend restarted successfully");
        watchdogFailures = 0;
        if (mainWindow) {
          mainWindow.webContents.reload();
        }
      } catch (err) {
        // 端口仍被占/重启失败 — 停止看门狗，让用户通过窗口错误提示得知
        console.error("[BatchSentry] Watchdog: restart failed:", err.message);
        clearInterval(watchdogTimer);
        watchdogTimer = null;
        dialog.showErrorBox(
          "BatchSentry — 后端恢复失败",
          `后端服务已停止且无法自动重启：\n\n${err.message}\n\n请关闭 BatchSentry 后重新打开。`,
        );
      }
    } finally {
      watchdogRestarting = false;
    }
  }, WATCHDOG_INTERVAL_MS);
}

/**
 * Single-shot health probe — returns true if a BatchSentry backend is
 * already listening and healthy on the port. Used for orphan recovery:
 * when the port is occupied but /health answers, we reuse the existing
 * server instead of failing with a conflict dialog.
 */
function probeHealth(port) {
  return new Promise((resolve) => {
    const req = http.get(
      `http://${SERVER_HOST}:${port}/health`,
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * Find the PID of the process listening on a TCP port (Windows: netstat).
 * Used to track orphaned pbc-server.exe processes for cleanup on exit
 * (对抗审查 P2-K). Returns null when nothing is listening.
 */
function findPortPid(port) {
  return new Promise((resolve) => {
    try {
      const out = execSync("netstat -ano", { encoding: "utf8", timeout: 5000 });
      const re = new RegExp(`\\b${port}\\b\\s+.*LISTENING\\s+(\\d+)\\s*$`, "m");
      const m = out.match(re);
      resolve(m ? parseInt(m[1], 10) : null);
    } catch {
      resolve(null);
    }
  });
}

/**
 * Create the splash window shown while the Python server boots.
 *
 * Uses a data: URL so it works in both dev and packaged mode without
 * needing a separate splash.html file. Minimal HTML, no external deps.
 * Phase 8: progress callback updates the status text in real time.
 */
function createSplashWindow() {
  splashWindow = new BrowserWindow({
    width: 480,
    height: 320,
    frame: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    center: true,
    show: true,
    backgroundColor: "#ffffff",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Inline splash HTML — minimalist BatchSentry branding + spinner + status
  splashWindow.loadURL(
    "data:text/html;charset=utf-8," +
      encodeURIComponent(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
      "Microsoft YaHei", "微软雅黑", system-ui, sans-serif;
    background: #ffffff;
    color: #0a0a0a;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 24px;
    user-select: none;
    -webkit-app-region: no-drag;
  }
  h1 {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.02em;
  }
  .spinner {
    /* 2026-08/2026-08-20: 旋转 spinner 在软件渲染（无 GPU 的 VM/远程桌
       面，帧率 16-40fps）下，无论 steps(8) 离散步进还是线性旋转，都会
       出现明显的"顿挫/跳帧"观感（每帧跳过 >1 步）。改为无位移的呼吸
       光点：仅有 opacity 变化，低帧下视觉连续，不再有位置跳变。 */
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .spinner i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #0a0a0a;
    opacity: 0.25;
    animation: breath 1.0s ease-in-out infinite;
  }
  .spinner i:nth-child(2) { animation-delay: 0.2s; }
  .spinner i:nth-child(3) { animation-delay: 0.4s; }
  @keyframes breath {
    0%, 100% { opacity: 0.25; transform: scale(1); }
    50% { opacity: 1; transform: scale(1.25); }
  }
  #status {
    font-size: 13px;
    color: #71717a;
    font-weight: 400;
    text-align: center;
    min-height: 18px;
  }
</style>
</head>
<body>
  <h1 id="brand"></h1>
  <div class="spinner"><i></i><i></i><i></i></div>
  <p id="status"></p>
  <script>
    // 对抗审查（分发实证 #1）：首帧曾直接渲染中文文本，软件渲染环境下
    // DirectWrite 的 CJK 回退字体（雅黑）尚未就绪 → 先画出方框（tofu），
    // 数秒后字体可用才恢复正常 —— 观感即"先方框、后动画/文字"。
    // 修复：spinner 圆点是纯形状不依赖字形，立即呈现；文本等 fonts.ready
    // （300ms 兜底）再填充，首帧永远是干净的 白底+呼吸点。
    (function () {
      var fill = function () {
        document.getElementById("brand").textContent = "BatchSentry";
        document.getElementById("status").textContent = "正在初始化…";
      };
      if (document.fonts && document.fonts.ready) {
        var done = false;
        var go = function () { if (!done) { done = true; fill(); } };
        document.fonts.ready.then(go);
        setTimeout(go, 300);
      } else {
        fill();
      }
    })();
  </script>
</body>
</html>`),
  );

  splashWindow.on("closed", () => {
    splashWindow = null;
  });
}

/**
 * Update splash status text (Phase 8).
 * Safe to call before splashWindow exists or after it's destroyed.
 */
function setSplashStatus(text) {
  if (!splashWindow || splashWindow.isDestroyed()) return;
  try {
    splashWindow.webContents.executeJavaScript(
      `document.getElementById('status').textContent = ${JSON.stringify(text)};`,
      true,
    );
  } catch {
    // splash may be mid-load; ignore
  }
}

/**
 * Create the main application window.
 */
function createWindow() {
  // Icon path — only set if file exists (avoid crash when icon.ico missing)
  const iconPath = path.join(__dirname, "icon.ico");
  const windowOptions = {
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    backgroundColor: "#ffffff",
    title: "BatchSentry — GMP 批生产记录合规检查",
    show: false, // 隐藏直到内容加载完成，避免白屏
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  };
  try {
    const fs = require("fs");
    if (fs.existsSync(iconPath)) {
      windowOptions.icon = iconPath;
    }
  } catch {
    // ignore — electron-builder will use default icon in packaged mode
  }

  mainWindow = new BrowserWindow(windowOptions);

  // Load the app
  mainWindow.loadURL(`http://${SERVER_HOST}:${SERVER_PORT}/`);

  // 内容加载完成后才显示主窗口 + 销毁 splash
  mainWindow.webContents.once("did-finish-load", () => {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.close();
      splashWindow = null;
    }
    mainWindow.show();
  });

  // Open external links (PDFs, links with target=_blank) in the default browser.
  // 对抗审查 P2-N：原实现对非 http(s) 协议一律 allow —— 页面里任何
  // window.open("file:///...") / target=_blank 会开新 BrowserWindow 加载
  // 本地文件（file:// 窗口可读任意本地路径）。收紧：仅 http(s) 交给系统
  // 浏览器，其余（file:/javascript:/data: 等）一律 deny。
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  // Block same-window navigation away from the local server. setWindowOpenHandler
  // only covers window.open — without this guard, window.location = "https://..."
  // (or any injected <a> click) would navigate the trusted app window to an
  // attacker-controlled page (credential phishing against GMP users).
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const allowedPrefixes = [
      `http://${SERVER_HOST}:${SERVER_PORT}/`,
      `http://localhost:${SERVER_PORT}/`,
    ];
    if (!allowedPrefixes.some((prefix) => url.startsWith(prefix))) {
      event.preventDefault();
    }
  });

  // DevTools in dev mode only
  if (!app.isPackaged) {
    mainWindow.webContents.openDevTools({ mode: "detach" });
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ── App lifecycle ────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  // 1. 立即显示 splash（用户点击图标后 <100ms 内有反馈）
  createSplashWindow();
  setSplashStatus("正在检查端口…");

  try {
    // Phase 8: pre-flight port conflict detection.
    // If 58765 is already taken, either (a) an orphaned pbc-server.exe from a
    // force-killed Electron is still alive, (b) another BatchSentry instance
    // is running (normally blocked by the single-instance lock), or (c) an
    // unrelated app grabbed the port.
    // Robustness-E3: probe /health before giving up — if it responds, reuse
    // the running server instead of failing (orphan recovery). Only when the
    // port answers AND is not our backend do we show the error dialog.
    const portOk = await isPortFree(SERVER_PORT);
    let reused = false;
    if (!portOk) {
      reused = await probeHealth(SERVER_PORT);
      if (reused) {
        reusedBackend = true; // waitForServer 不要求子进程存活
        setSplashStatus("检测到正在运行的 BatchSentry 服务，直接连接…");
        console.warn(`SERVER: Port ${SERVER_PORT} occupied by a healthy backend, reusing it (orphan recovery)`);
        // 对抗审查 P2-K：记录孤儿后端 PID，退出时必须一并清理 —
        // 否则每次"强杀 Electron 后重启"都残留一个 pbc-server.exe，
        // 累积多个后端进程常驻内存。
        reusedPid = await findPortPid(SERVER_PORT);
        console.warn(`SERVER: orphan backend pid=${reusedPid} will be shut down on exit`);
      } else {
        const choice = dialog.showMessageBoxSync({
          type: "error",
          title: "BatchSentry — 端口冲突",
          message: `端口 ${SERVER_PORT} 已被占用`,
          detail:
            `另一个 BatchSentry 实例可能正在运行，或端口被其他应用占用。\n\n` +
            `请先关闭其他 BatchSentry 进程（任务管理器查找 pbc-server.exe / BatchSentry），\n` +
            `或修改 electron/main.js 中的 SERVER_PORT 后重新打包。\n\n` +
            `点击“确定”退出 BatchSentry。`,
          buttons: ["确定"],
          noLink: true,
        });
        app.quit();
        return;
      }
    }

    // 2. 启动后端（reused 时跳过，直接复用已有实例）+ 轮询健康检查
    setSplashStatus("正在启动后端服务…");
    if (portOk) {
      startPythonServer();
    }
    console.log("[BatchSentry] Waiting for server to be ready...");
    setSplashStatus("正在加载数据库与配置…");
    await waitForServer();
    setSplashStatus("正在打开主窗口…");
    // 3. 创建主窗口（splash 会在主窗口加载完成后销毁）
    createWindow();
    // robustness-G1: 主窗口就绪后启动后端看门狗
    startWatchdog();
  } catch (err) {
    console.error("[BatchSentry] Startup failed:", err.message);
    dialog.showErrorBox(
      "BatchSentry — 启动失败",
      `无法启动后端服务：\n\n${err.message}\n\n请检查日志或重新安装。`,
    );
    app.quit();
  }
});

app.on("window-all-closed", () => {
  // On macOS, keep menu bar active; on other platforms quit
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

// ── Graceful shutdown ────────────────────────────────────────────────────

/**
 * 优雅关闭流程（v1.2.0 重写，与实测对齐）：
 *   1. POST /api/shutdown —— 后端取消在飞 pipeline task（写 error + audit_log），
 *      并在响应写回后**请求自己的 uvicorn 优雅关停**（响应体里的 exit_requested）。
 *   2. 按**端口释放**等待后端自行退出（上限 SHUTDOWN_EXIT_WAIT_MS）。
 *      "优雅"的判据是后端跑完了 lifespan 收尾（close_db），不是"我们发过什么信号"。
 *   3. 超时未退 ⇒ `taskkill /pid <pid> /T /F`（本实例子进程与**复用孤儿**一视同仁），
 *      再等端口释放；仍不放就明确报错，不假装成功。
 *
 * 旧实现声称的三条保证**都不成立**（2026-09-21 实测，见下），故连同注释一起重写：
 *   - "数据库连接正常关闭"：旧 Step 3 的 SIGTERM 在 Windows 上就是 TerminateProcess，
 *     不执行任何 Python 代码 ⇒ `close_db()` 从不运行、-wal/-shm 原样残留。
 *   - Step 4 的强杀兜底是**死代码**：条件 `!pythonProcess.killed` 恒假（Node 的
 *     `killed` 表示"信号已发出"，与"已退出"无关）；复用孤儿路径
 *     （pythonProcess 为 null）更是整体跳过终止。
 *   - "audit_log 中有完整的关闭记录"：该表 `job_id` 是 NOT NULL，纯关闭事件本身
 *     无处落库 —— 只有被取消的**在飞任务**才会留下记录。
 */
async function gracefulShutdown() {
  if (isShuttingDown) return;
  isShuttingDown = true;

  // 目标进程 = 本实例 spawn 的 pythonProcess，或复用的孤儿后端（reusedPid）。
  const targetPid = pythonProcess ? pythonProcess.pid : reusedPid;
  if (!targetPid) {
    pythonProcess = null;
    reusedPid = null;
    return;
  }

  console.log(`[BatchSentry] Requesting backend graceful shutdown (pid=${targetPid})...`);
  const res = await requestBackendShutdown();
  if (!res.exitRequested) {
    // 后端没确认退出通道（旧产物，或后端被换成了不认识 exit_requested 的版本）。
    // 必须**说出来**：否则"没有通道"会被读成"已经优雅退出了"。
    console.warn(
      "[BatchSentry] 后端未确认优雅退出通道 (exit_requested=false," +
        ` status=${res.statusCode}) — 将依赖强杀兜底，close_db() 可能未执行。`,
    );
  }

  if (await waitBackendGone(SHUTDOWN_EXIT_WAIT_MS, pythonProcess)) {
    console.log("[BatchSentry] Backend exited gracefully (port released).");
  } else {
    console.warn(
      `[BatchSentry] Backend still alive after ${SHUTDOWN_EXIT_WAIT_MS}ms; ` +
        `force-killing process tree pid=${targetPid}...`,
    );
    killProcessTree(targetPid);
    if (await waitBackendGone(SHUTDOWN_KILL_WAIT_MS, pythonProcess)) {
      console.log("[BatchSentry] Backend killed; port released.");
    } else {
      console.error(
        "[BatchSentry] Backend STILL alive after force-kill — port may remain occupied.",
      );
    }
  }
  pythonProcess = null;
  reusedPid = null;
}

/**
 * `POST /api/shutdown`，并把"后端是否确认有优雅退出通道"带回来。
 *
 * 旧实现用 `res.resume()` 把响应体丢掉了，于是**没法知道**后端到底照做没有 ——
 * 只能假定它优雅了。现在读响应体的 `exit_requested` 作为判据。
 */
function requestBackendShutdown() {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (v) => {
      if (!settled) {
        settled = true;
        resolve(v);
      }
    };
    const req = http.request(
      {
        hostname: SERVER_HOST,
        port: SERVER_PORT,
        path: "/api/shutdown",
        method: "POST",
        timeout: 5000,
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => {
          body += c;
        });
        res.on("end", () => {
          let exitRequested = false;
          try {
            exitRequested = JSON.parse(body).exit_requested === true;
          } catch {
            // 响应不是 JSON（旧版本/异常）⇒ 保持 false，由调用方告警
          }
          finish({
            ok: res.statusCode === 200,
            statusCode: res.statusCode,
            exitRequested,
          });
        });
      },
    );
    req.on("error", (err) => {
      console.warn("[BatchSentry] /api/shutdown failed:", err.message);
      finish({ ok: false, statusCode: null, exitRequested: false });
    });
    req.on("timeout", () => {
      req.destroy(); // 触发 error → finish
    });
    req.end();
  });
}

/**
 * 子进程是否**仍在运行**。
 *
 * ⚠️ 不要用 `!child.killed`：Node 的 `child.kill()` 在信号**发出后立刻**把 `killed`
 * 置真（官方语义是"已成功发出信号"）。旧代码拿它当"还活着"的判据 ⇒ 条件恒假 ⇒
 * `taskkill` 兜底成了死代码。权威判据是 `exitCode` / `signalCode`。
 */
function childAlive(child) {
  return !!child && child.exitCode === null && child.signalCode === null;
}

/**
 * 后端是否**确实**退出了 —— 判据取"端口已释放"，因为那才是用户可观测的事实。
 * 只判"我们发过信号"没有判别力；复用孤儿路径没有 child 对象，只剩端口可判。
 */
async function waitBackendGone(timeoutMs, child) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const gone = (await isPortFree(SERVER_PORT)) && !childAlive(child);
    if (gone) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, 250));
  }
}

/** 强杀进程树（本实例子进程与复用孤儿共用）。只在"优雅路径确实超时"后调用。 */
function killProcessTree(pid) {
  if (!pid) return;
  try {
    if (process.platform === "win32") {
      execSync(`taskkill /pid ${pid} /T /F`, { stdio: "ignore" });
    } else {
      process.kill(pid, "SIGKILL");
    }
  } catch {
    // 进程可能已退出 —— 由调用方按端口释放复核，不在 catch 里下结论
  }
}

app.on("before-quit", async (e) => {
  // 阻止立即退出，等优雅关闭完成后再 quit
  if (!isShuttingDown) {
    e.preventDefault();
    if (watchdogTimer) {
      clearInterval(watchdogTimer);
      watchdogTimer = null;
    }
    await gracefulShutdown();
    app.quit();
  }
});

// 兜底：进程级别信号（Ctrl+C / 任务管理器结束）
process.on("SIGINT", async () => {
  await gracefulShutdown();
  app.quit();
});
process.on("SIGTERM", async () => {
  await gracefulShutdown();
  app.quit();
});
