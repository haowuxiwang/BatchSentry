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
 *   5. on quit: POST /api/shutdown → wait 2s → SIGTERM → taskkill /T /F fallback
 */

const { app, BrowserWindow, shell, dialog, Menu } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const http = require("http");
const net = require("net");

// Phase 8: remove the default Electron menu bar (File/Edit/View/Window/Help).
// BatchSentry is a single-window app — the menu bar adds visual clutter with
// no useful functionality. Must be set before app.whenReady() for it to take
// effect on the first window.
Menu.setApplicationMenu(null);

const SERVER_PORT = 58765;
const SERVER_HOST = "127.0.0.1";
const MAX_READY_CHECKS = 60; // 60 × 500ms = 30s timeout
const SHUTDOWN_GRACE_MS = 2500; // wait for /api/shutdown to complete

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

  pythonProcess = spawn(cmd, args, {
    cwd: cwd || undefined,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  pythonProcess.stdout.on("data", (data) => {
    const msg = data.toString().trim();
    if (msg) console.log(`[pbc-server] ${msg}`);
  });

  pythonProcess.stderr.on("data", (data) => {
    const msg = data.toString().trim();
    if (msg) console.error(`[pbc-server] ${msg}`);
  });

  pythonProcess.on("error", (err) => {
    // 对抗审查 P2-L：spawn 失败（exe 缺失/无权限/被杀软拦截）此前只打
    // 日志，waitForServer 继续空轮询满 60 次（30s）后才报误导性的
    // "启动超时"。此处把错误记录到全局，waitForServer 立即失败。
    console.error("[BatchSentry] Failed to start server:", err);
    spawnError = err;
  });

  pythonProcess.on("exit", (code, signal) => {
    console.log(`[BatchSentry] Server exited (code=${code} signal=${signal})`);
    pythonProcess = null;
  });
}

/**
 * Poll the server health endpoint until ready (or timeout).
 */
function waitForServer() {
  return new Promise((resolve, reject) => {
    let checks = 0;

    const check = () => {
      if (spawnError) {
        reject(spawnError);
        return;
      }
      const req = http.get(
        `http://${SERVER_HOST}:${SERVER_PORT}/health`,
        (res) => {
          if (res.statusCode === 200) {
            console.log("[BatchSentry] Server ready");
            resolve();
          } else {
            retry();
          }
          res.resume();
        },
      );

      req.on("error", () => retry());
      req.setTimeout(2000, () => {
        req.destroy();
        retry();
      });
    };

    const retry = () => {
      checks += 1;
      if (checks >= MAX_READY_CHECKS) {
        reject(new Error(`Server not ready after ${MAX_READY_CHECKS} checks`));
      } else {
        setTimeout(check, 500);
      }
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
      if (pythonProcess && !pythonProcess.killed) {
        try {
          if (process.platform === "win32") {
            execSync(`taskkill /pid ${pythonProcess.pid} /T /F`, { stdio: "ignore" });
          } else {
            pythonProcess.kill("SIGKILL");
          }
        } catch {
          // 进程可能已自行退出
        }
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
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
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
    width: 28px;
    height: 28px;
    border: 2px solid #e4e4e7;
    border-top-color: #0a0a0a;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
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
  <h1>BatchSentry</h1>
  <div class="spinner"></div>
  <p id="status">正在初始化…</p>
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
    title: "BatchSentry",
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
 * 优雅关闭流程：
 *   1. POST /api/shutdown — 让后端取消所有运行中的 pipeline task，写 error 状态
 *   2. 等待 2.5s 让后端完成清理
 *   3. SIGTERM 软终止（让 uvicorn lifespan 执行 close_db）
 *   4. 如果 3s 后进程仍存活，taskkill /T /F 强杀（兜底）
 *
 * 这样可以保证：
 * - 正在运行的 job 被标记为 error（而不是永远卡在 ocr_running）
 * - 数据库连接正常关闭（避免 SQLite WAL 残留）
 * - audit_log 中有完整的关闭记录
 */
async function gracefulShutdown() {
  if (isShuttingDown) return;
  isShuttingDown = true;

  // Step 1: 请求后端优雅关闭
  // 对抗审查 P2-K：目标进程 = 本实例 spawn 的 pythonProcess 或复用的
  // 孤儿后端（reusedPid）。原实现以 `if (pythonProcess)` 为入口，
  // reused 路径（pythonProcess 保持 null）完全跳过清理 → 孤儿永不关闭，
  // 每次"强杀后重启"累积一个常驻 pbc-server.exe。
  const targetPid = pythonProcess ? pythonProcess.pid : reusedPid;
  if (targetPid) {
    console.log(`[BatchSentry] Requesting backend graceful shutdown (pid=${targetPid})...`);
    try {
      await new Promise((resolve) => {
        const req = http.request(
          {
            hostname: SERVER_HOST,
            port: SERVER_PORT,
            path: "/api/shutdown",
            method: "POST",
            timeout: 3000,
          },
          (res) => {
            res.resume();
            res.on("end", resolve);
          },
        );
        req.on("error", () => resolve());
        req.on("timeout", () => {
          req.destroy();
          resolve();
        });
        req.end();
      });
    } catch (err) {
      console.warn("[BatchSentry] /api/shutdown failed:", err.message);
    }

    // Step 2: 等待后端清理（取消 task + 写 audit_log）
    await new Promise((r) => setTimeout(r, SHUTDOWN_GRACE_MS));

    // Step 3: SIGTERM 软终止（让 uvicorn 执行 lifespan 的 yield 后部分）
    if (pythonProcess && !pythonProcess.killed) {
      console.log("[BatchSentry] Sending SIGTERM to server...");
      try {
        // Windows 上 SIGTERM 等同于 TerminateProcess，但先尝试让进程自行退出
        pythonProcess.kill("SIGTERM");
      } catch {
        // ignore
      }

      // Step 4: 等待 2s，如果仍存活则 taskkill /T /F 强杀
      await new Promise((r) => setTimeout(r, 2000));
      if (pythonProcess && !pythonProcess.killed) {
        console.log("[BatchSentry] Force-killing server process tree...");
        try {
          if (process.platform === "win32") {
            execSync(`taskkill /pid ${pythonProcess.pid} /T /F`, {
              stdio: "ignore",
            });
          } else {
            pythonProcess.kill("SIGKILL");
          }
        } catch {
          // 进程可能已退出
        }
      }
    }
    pythonProcess = null;
  }
  reusedPid = null;
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
