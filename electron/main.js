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
 *   2. spawn pbc-server.exe + poll /health until ready (up to 30s)
 *   3. splash → main window, load the app URL
 *   4. on quit: POST /api/shutdown → wait 2s → SIGTERM → taskkill /T /F fallback
 */

const { app, BrowserWindow, shell } = require("electron");
const { spawn, execSync } = require("child_process");
const path = require("path");
const http = require("http");

const SERVER_PORT = 58765;
const SERVER_HOST = "127.0.0.1";
const MAX_READY_CHECKS = 60; // 60 × 500ms = 30s timeout
const SHUTDOWN_GRACE_MS = 2500; // wait for /api/shutdown to complete

let pythonProcess = null;
let mainWindow = null;
let splashWindow = null;
let isShuttingDown = false;

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
    console.error("[BatchSentry] Failed to start server:", err);
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
 * Create the splash window shown while the Python server boots.
 *
 * Uses a data: URL so it works in both dev and packaged mode without
 * needing a separate splash.html file. Minimal HTML, no external deps.
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

  // Inline splash HTML — minimalist BatchSentry branding + spinner
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
  p {
    font-size: 13px;
    color: #71717a;
    font-weight: 400;
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
</style>
</head>
<body>
  <h1>BatchSentry</h1>
  <div class="spinner"></div>
  <p>正在启动后端服务…</p>
</body>
</html>`),
  );

  splashWindow.on("closed", () => {
    splashWindow = null;
  });
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

  // Open external links (PDFs, links with target=_blank) in the default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
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

  try {
    // 2. 启动后端 + 轮询健康检查
    startPythonServer();
    console.log("[BatchSentry] Waiting for server to be ready...");
    await waitForServer();
    // 3. 创建主窗口（splash 会在主窗口加载完成后销毁）
    createWindow();
  } catch (err) {
    console.error("[BatchSentry] Startup failed:", err.message);
    const { dialog } = require("electron");
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
  if (pythonProcess) {
    console.log("[BatchSentry] Requesting backend graceful shutdown...");
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
}

app.on("before-quit", async (e) => {
  // 阻止立即退出，等优雅关闭完成后再 quit
  if (!isShuttingDown) {
    e.preventDefault();
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
