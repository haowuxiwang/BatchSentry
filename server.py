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

    import uvicorn

    # Use port 58765 by default (Electron's expected port), but allow
    # override via env var PORT for dev/testing.
    port = int(__import__("os").getenv("PORT", "58765"))
    host = "127.0.0.1"

    print(f"[PBC] Starting server on {host}:{port}", flush=True)
    print(f"[PBC] Frozen: {getattr(sys, 'frozen', False)}", flush=True)

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
        workers=1,
    )
