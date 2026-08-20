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
# Phase 8 adversarial review: added python-multipart, openai, anthropic,
# jinja2, httpx, email.mime — these are frequently missed by PyInstaller's
# static analysis but required at runtime (file upload, LLM SDK, templating).
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
    "pydantic._internal._core_utils",
    "pydantic._internal._validators",
    "pydantic._internal._fields",
    "pydantic._internal._config",
    "pydantic._internal._generate_schema",
    "pydantic._internal._generics",
    "pydantic._internal._signature",
    "pydantic._internal._typing_extra",
    # Phase 8: python-multipart — required by FastAPI for multipart/form-data
    # file uploads (POST /api/jobs). PyInstaller misses this because it's
    # imported dynamically by Starlette on first file upload request.
    "multipart",
    "multipart.multipart",
    "multipart.exceptions",
    # Phase 8: Jinja2 templating engine (templates/*.html)
    "jinja2",
    "jinja2.ext",
    "jinja2.parsers",
    "jinja2.runtime",
    "jinja2.utils",
    "jinja2.compiler",
    # Our app modules (2026-08: pipeline/jobs/settings/cross_page_analyzer
    # split into packages — hiddenimports must list the leaf modules,
    # PyInstaller does not recurse into packages from a bare package name)
    "main",
    "server",
    "config",
    "api.jobs",
    "api.jobs.upload",
    "api.jobs.listings",
    "api.jobs.page_image",
    "api.jobs.status",
    "api.jobs.actions",
    "api.review",
    "api.report",
    "api.settings",
    "api.settings.read",
    "api.settings.write",
    "api.settings.rules",
    "api.settings.provider",
    "api.settings.probe",
    "db.client",
    "core.pipeline",
    "core.pipeline.locks",
    "core.pipeline.state",
    "core.pipeline.ocr_support",
    "core.pipeline.self_heal",
    "core.pipeline.stage1",
    "core.pipeline.stage2",
    "core.pipeline.stage3",
    "core.pipeline.engine",
    "core.rules",
    "core.rules.base",
    "core.rules.parsing",
    "core.rules.rule_time",
    "core.rules.rule_spec",
    "core.rules.rule_doc",
    "core.rules.llm_checks",
    "core.page_analyzer",
    "core.hw_signal",  # Round 7 OCR handwriting-signal extraction
    "core.cross_page_analyzer",  # shim — kept for apps importing the old name
    "core.ocr_client",
    "core.mineru_client",  # pipeline.py 动态导入（_get_ocr_backend）
    "core.notify",  # Phase 12 Feishu job notifications
    "core.security",
    "core.health",
    "core.zh_map",
    "llm.client",
    # Phase 7: LLM adapter layer (dynamic import via get_adapter)
    "llm.adapters",
    "llm.adapters.base",
    "llm.adapters.openai_adapter",
    "llm.adapters.anthropic_adapter",
    "models.schemas",
    "logging_config",
    # Phase 8: LLM SDKs — imported by adapters but PyInstaller misses them
    # because adapter selection is dynamic (config-driven).
    "openai",
    "openai.types",
    "openai.types.chat",
    "openai.resources",
    "openai.resources.chat",
    "openai.resources.chat.completions",
    "anthropic",
    "anthropic.types",
    "anthropic.resources",
    "anthropic.resources.messages",
    # Phase 8: httpx — used by openai/anthropic SDKs for HTTP
    "httpx",
    "httpx._transports",
    "httpx._transports.default",
    "httpcore",
    "h11",
    "h2",
    "hpack",
    "hyperframe",
    # SQLite (used by aiosqlite)
    "sqlite3",
    "aiosqlite",
    # dotenv (config.py loads .env)
    "dotenv",
    # markupsafe (Jinja2 dep, sometimes missed)
    "markupsafe",
    # json (used everywhere)
    "json",
    # logging config file support
    "logging.config",
    "logging.handlers",
    # Phase 8: email.mime — used by report.py for DOCX generation if needed
    "email.mime",
    "email.mime.text",
    "email.mime.multipart",
    "email.mime.base",
    # Phase 8: requests + urllib3 (ocr_client.py, mineru_client.py)
    "requests",
    "urllib3",
    "urllib3.util",
    "urllib3.util.retry",
    "urllib3.connection",
    "urllib3.connectionpool",
    # Phase 8: certifi — SSL certificates for HTTPS requests to LLM/OCR APIs
    "certifi",
    "ssl",
    # Phase 8: anyio — used by Starlette/httpx for async I/O
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "sniffio",
    # Phase 8: PyMuPDF (optional, used by report.py if installed)
    "fitz",
]

# Filters to exclude unnecessary files
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "pytest",
    "IPython",
    "jupyter",
    "pylint",
    "mypy",
    "black",
    "flake8",
]

a = Analysis(
    ["server.py"],
    pathex=[str(_PROJECT_ROOT)],
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
