"""PyInstaller entry point — launches uvicorn server.

This script is what PyInstaller bundles as the main executable. It starts
the FastAPI app via uvicorn on 127.0.0.1:58765 (Electron's expected port).

Run directly in dev:  python server.py
Bundled:              pbc-server.exe (spawned by electron/main.js)
"""
import multiprocessing
import sys

if __name__ == "__main__":
    # Windows multiprocessing support for PyInstaller
    multiprocessing.freeze_support()

    import os
    import sys
    import time
    import traceback
    import uvicorn

    # Use port 58765 by default (Electron's expected port), but allow
    # override via env var PORT for dev/testing.
    port = int(os.getenv("PORT", "58765"))
    # CORS 一致性：config.py 的 app.port 也从 PORT env 派生（默认 8000），
    # 若不显式同步，server.py 监听 58765 而 CORS allowlist 是 8000 →
    # 浏览器访问设置页的 POST/PUT 会被 CORS 拦截。必须在 import config
    # 之前 setdefault，否则 config 模块已按 8000 初始化（Electron
    # main.js 已传 PORT=58765，天然一致；此兜底覆盖「直接运行
    # python server.py」场景）。
    os.environ.setdefault("PORT", str(port))

    from config import config

    # Host 从 config 读取（APP_HOST env var），避免硬编码 127.0.0.1 与
    # config.py 脱节。默认值 127.0.0.1 由 config 提供。
    host = config["app"].host

    print(f"[PBC] Starting server on {host}:{port}", flush=True)
    print(f"[PBC] Frozen: {getattr(sys, 'frozen', False)}", flush=True)

    # 健壮性: 绑定失败兜底 — 端口被占/权限不足时 uvicorn 抛 OSError
    # 并给出难读的堆栈。双击工具场景下用户需要明确的中文提示, 而不是
    # 一个"看不见退出原因"的崩溃窗口。捕获后打印可操作信息再退出
    # (退出码 1, Electron 端 waitForServer 会超时报错并弹窗)。
    try:
        uvicorn.run(
            "main:app",
            host=host,
            port=port,
            log_level="info",
            reload=False,
            workers=1,
        )
    except OSError as e:
        message = str(e)
        print(
            f"\n[PBC][FATAL] 无法在 {host}:{port} 启动服务: {message}\n"
            f"可能原因:\n"
            f"  1. 端口 {port} 已被占用 (另一个 BatchSentry 或程序正在运行)\n"
            f"  2. 防火墙/权限限制\n"
            f"解决方法: 关闭其他 BatchSentry 实例后重新启动。\n",
            flush=True,
        )
        time.sleep(2)  # 短暂停留, 让退出前的输出可被捕获
        sys.exit(1)
    except Exception:
        print(
            f"\n[PBC][FATAL] 服务启动失败:\n"
            f"{traceback.format_exc()}\n"
            f"请将以上信息连同日志目录下的日志反馈给开发者。\n",
            flush=True,
        )
        time.sleep(2)
        sys.exit(1)
