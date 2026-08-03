# -*- mode: python ; coding: utf-8 -*-
"""
# PyInstaller spec for BatchSentry backend (pbc-server.exe).

Produces pbc-server.exe — a standalone executable containing:
- Python 3.12 runtime
- uvicorn + FastAPI
- All app code (main.py, api/, core/, db/, models/, llm/, config.py, etc.)
- Templates and static assets (templates/, static/)
- sqlite3 module (for aiosqlite)

Build:
  pyinstaller pbc-server.spec

Output:
  dist/pbc-server/pbc-server.exe  (one-dir bundle, ~150-200MB)
"""

import sys
import os
from pathlib import Path

block_cipher = None

# Phase 5B: use absolute paths for datas — PyInstaller resolves relative
# paths against CWD which may differ when invoked via `python -m PyInstaller`.
# SPECPATH is the directory containing this .spec file (project root).
_PROJECT_ROOT = Path(SPECPATH).resolve()

# Collect everything from the project that isn't auto-detected by PyInstaller.
# datas format: (source, destination_relative_to_bundle_root)
datas = [
    (str(_PROJECT_ROOT / "templates"), "templates"),
    (str(_PROJECT_ROOT / "static"), "static"),
    (str(_PROJECT_ROOT / "db" / "schema.sql"), "db"),
]

# Hidden imports — modules PyInstaller can't detect via static analysis
# (dynamic imports, entry points, etc.)
hiddenimports = [
    # uvicorn internals — not auto-detected
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # FastAPI / Starlette / pydantic
    "fastapi",
    "fastapi.middleware",
    "fastapi.middleware.cors",
    "starlette",
    "starlette.routing",
    "starlette.templating",
    "starlette.staticfiles",
    "pydantic",
    # Our app modules
    "main",
    "server",
    "config",
    "api.jobs",
    "api.review",
    "api.report",
    "api.settings",
    "db.client",
    "core.pipeline",
    "core.page_analyzer",
    "core.cross_page_analyzer",
    "core.ocr_client",
    "core.mineru_client",  # pipeline.py 动态导入（_get_ocr_backend）
    "llm.client",
    # Phase 7: LLM adapter layer (dynamic import via get_adapter)
    "llm.adapters",
    "llm.adapters.base",
    "llm.adapters.openai_adapter",
    "llm.adapters.anthropic_adapter",
    "models.schemas",
    "logging_config",
    # SQLite (used by aiosqlite)
    "sqlite3",
    # dotenv (config.py loads .env)
    "dotenv",
    # markupsafe (Jinja2 dep, sometimes missed)
    "markupsafe",
    # json (used everywhere)
    "json",
    # logging config file support
    "logging.config",
    "logging.handlers",
]

# Filters to exclude unnecessary files
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PIL",
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
    "pylint",
    "mypy",
    "black",
    "flake8",
]

a = Analysis(
    ["server.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pbc-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # Keep console for log output (Electron captures stdout)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pbc-server",
)
