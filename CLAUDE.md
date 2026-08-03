# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**BatchSentry** — GMP 批生产记录半自动合规检查系统（前身 Pharma Batch Checker / PBC）。

用户上传 PDF 批生产记录 → OCR 识别 → LLM 结构化提取 → 规则+LLM 跨页合规分析 → 人工复核界面 → 导出报告。

**Current phase**: Phase 7 (multi-provider LLM architecture), v0.1.0, local single-user deployment via PyInstaller exe + Electron wrapper.

---

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, coverage, httpx

# Run dev server (from project root)
uvicorn main:app --reload --host 127.0.0.1 --port 8000

# Or via server.py (matches bundled entry point)
python server.py            # listens on 127.0.0.1:58765

# Run tests
pytest
pytest --cov=. --cov-report=term --cov-report=html

# Build local Tailwind CSS (15.8KB, no CDN)
npx tailwindcss -i ./static/input.css -o ./static/app.css --minify

# Build Windows installer (must run in real PowerShell, NOT IDE Sandbox)
.\build.ps1                # full build: css + pyinstaller + electron
.\build.ps1 -SkipCss       # skip tailwind rebuild
.\build.ps1 -Clean         # clean rebuild from scratch

# API docs (Swagger): http://127.0.0.1:8000/docs
```

Test coverage target: ≥90%. Current: ~91% (see `tests/` with unit + integration suites).

---

## Environment Setup

Copy `.env.example` → `.env` and fill in:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Active provider name (must be one of the registered providers below) |
| `DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` | Built-in LLM providers (OpenAI-compatible) |
| `LLM_PROVIDERS` | Comma-separated list of additional providers to register (e.g. `glm,kimi,qwen,mimo,anthropic`) |
| `<NAME>_PROTOCOL` | `openai` (default) or `anthropic` — wire format for custom providers |
| `<NAME>_API_KEY` / `<NAME>_BASE_URL` / `<NAME>_MODEL` | Per-provider config (prefix = UPPER(name)) |
| `PADDLE_OCR_TOKEN` / `PADDLE_OCR_API_URL` | PaddleOCR-VL async API |
| `MINERU_TOKEN` | MinerU OCR backend (optional, set `OCR_BACKEND=mineru`) |
| `DATABASE_PATH` | SQLite file (default: `data/pharma.db`) |
| `OUTPUT_DIR` | PDF storage + job artifacts (default: `output/`) |
| `MAX_CONCURRENT_JOBS` | Max simultaneous active pipelines (default 3) |

**Adding a new LLM provider** (no code changes needed):
1. Add the provider name to `LLM_PROVIDERS` (e.g. `LLM_PROVIDERS=glm,kimi`)
2. Set its 4 env vars: `GLM_PROTOCOL=openai`, `GLM_API_KEY=...`, `GLM_BASE_URL=...`, `GLM_MODEL=...`
3. Or use the in-app Settings page → "添加提供商" dropdown (writes to `.env` + live reload)
4. Set `LLM_PROVIDER=glm` to activate it

Both `openai` (DeepSeek/SiliconFlow/GLM/Kimi/Qwen/MiMo) and `anthropic` (Claude) protocols are supported via the adapter layer in `llm/adapters/`.

**Frozen mode (PyInstaller bundle)**: `.env` is read from `%APPDATA%/PBC/.env` (Windows), `~/Library/Application Support/PBC/.env` (macOS), `~/.local/share/PBC/.env` (Linux). Database and output files redirect to `%APPDATA%/PBC/` as well. Use the in-app Settings page to edit credentials at runtime — saves are applied live without restart.

`config.py` reads `.env` via `python-dotenv` at import time. Settings page calls `update_config()` to mutate the in-memory config for live reload.

---

## Architecture

### Frontend (Jinja2 + Tailwind + vanilla JS)

Templates, styles, and scripts are **strictly separated** — no inline CSS/JS except a single `window.__PBC__` bridge per page.

- `templates/upload.html` + `static/upload.js` + `static/upload.css` — upload + job history list
- `templates/review.html` + `static/review.js` + `static/review.css` — 3-column review (page nav | PDF | findings)
- `templates/settings.html` + `static/settings.js` + `static/settings.css` — LLM/OCR credential editor
- `static/app.css` — locally built Tailwind output (15.8KB, do not edit directly)
- `static/design-tokens.css` — shadcn HSL variables

Design system: minimalist, white background, black primary, flat lists (no cards), `border-b` hairline separators, pill-shaped nav buttons. No dark mode.

Frontend logs use `[PBC]` prefix with color coding (blue=info, orange=warn, red=error).

### Backend (FastAPI + aiosqlite)

Entry point: `main.py` (dev) or `server.py` (bundled, port 58765).

**Three-stage pipeline** (`core/pipeline.py`) runs as FastAPI `BackgroundTask`:

1. **Stage 1 — OCR** (`core/ocr_client.py` or `core/mineru_client.py`): submit PDF → poll → download JSON. 10-minute poll timeout, 5s interval. Blocking `requests` wrapped via `asyncio.to_thread`.
2. **Stage 2 — Per-page LLM** (`core/page_analyzer.py`): each page's HTML table → LLM extraction prompt → structured JSON with `steps[].measurements[]` time series. Uses string concatenation (NOT `.format()`) to avoid brace collision with HTML. 180s timeout, 3 retries with exponential backoff.
3. **Stage 3 — Cross-page analysis** (`core/cross_page_analyzer.py`): rule-based time reversal + LLM-based semantic anomalies. Both write to the same `findings` table with `source` field (`rule` / `llm_page` / `llm_cross` / `llm_fallback`).

**State machine** (`pipeline.VALID_TRANSITIONS`): `pending → ocr_running → ocr_done → analyzing → review | partial_review | error | cancelled`. Terminal states can `archived`. Invalid transitions raise `InvalidTransitionError`.

### Data Flow

```
PDF upload → output/{job_id}/filename.pdf
                ↓
         page_cache (raw_html per page)        ← Stage 1
                ↓
         page_cache (structured_json per page) ← Stage 2
                ↓
         findings (severity, source, status)  ← Stage 3
                ↓
         audit_log (every state change + user action)
```

### Database Schema (`db/schema.sql`)

- **`jobs`**: id, filename, status, pdf_path, total_pages, failed_pages, stage1_ms/stage2_ms/stage3_ms, error_message, created_at, finished_at
- **`page_cache`**: (job_id, page) → raw_html + structured_json + analyzed_at
- **`findings`**: id, job_id, page, type, severity, source, description, ocr_text, operator, status (`pending → confirmed | rejected | corrected`), reviewer_note, corrected_text, reviewed_at
- **`audit_log`**: id, job_id, finding_id, action, detail, created_at

SQLite via `aiosqlite` with WAL mode. Singleton connection in `db/client.py`.

### API Layer (`api/`)

| Router | Prefix | Purpose |
|---|---|---|
| `jobs.py` | `/api/jobs` | Upload (8MB chunked, 200MB max), status, cancel, retry, archive, unarchive, delete, page data, findings |
| `review.py` | `/api/jobs/{id}/findings` | List/get/update findings (confirm/reject/correct) + audit log + page measurements |
| `report.py` | `/api/jobs/{id}/report.{md,json}` | Export Markdown + JSON reports |
| `settings.py` | `/api/settings` | Read (masked) / update `.env` with live reload |

Server-rendered HTML pages:
- `GET /` → upload + job list
- `GET /jobs/{id}/review?page=N` → review UI (3-column)
- `GET /settings` → credential editor
- `GET /health` → health check

### Logging (`logging_config.py`)

Structured logging with `request_id` ContextVar. Middleware logs `[req_id] METHOD path → STATUS (duration_ms)` for every request (skips `/static/` and `/health`).

Handlers:
- Console (stdout)
- `logs/pharma.log` — all levels
- `logs/pipeline.log` — pipeline stage events only
- `logs/error.log` — ERROR+ only

API routes emit business logs (upload/cancel/retry/archive/delete/finding update/report generation) with `[job_id]` prefix.

### Security Posture

- **CORS**: only `127.0.0.1:8000`, `localhost:8000`, `127.0.0.1:58765`, `localhost:58765`. `file://` removed to prevent XSS via Electron renderer.
- **CORS headers**: restricted to `Content-Type, X-Request-ID` (not `*`).
- **Upload**: 8MB chunked streaming, `Path(file.filename).name` sanitization, 200MB hard limit, empty-file rejection, `%PDF-` magic bytes check.
- **SQL**: all queries parameterized (`?` placeholders).
- **Secrets**: `.env` never committed; Settings API masks keys (`sk-abcd...wxyz`).
- **PDF preview**: `content_disposition_type="inline"` for iframe rendering.
- **XSS**: `render_page_links` filter escapes HTML before inserting links; `review.js` `renderFindings` escapes all LLM-sourced text via `esc()` helper; `upload.js` `setStatus` uses `textContent` not `innerHTML`.
- **Path traversal**: `delete_job` validates `job_dir` is inside `output_root` before `rmtree`.
- **Concurrency**: `MAX_CONCURRENT_JOBS` env var (default 3) caps active pipelines to prevent memory exhaustion.
- **Downstream probes**: `GET /api/health/downstream` checks OCR + LLM reachability; Settings page has "测试连接" button.

### Secret Rotation Procedure

If a key was committed to git history (e.g. the original `PADDLE_OCR_TOKEN` leak in PLAN.md):

1. **Rotate at provider** — log into PaddleOCR / DeepSeek / SiliconFlow console, revoke the old key, issue a new one. Just deleting from the repo is NOT enough — git history is immutable.
2. **Update local `.env`** (dev) or `%APPDATA%/PBC/.env` (frozen) via Settings page.
3. **Verify** with the "测试连接" button on Settings page.
4. **Audit**: `git log --all -p | grep <old-key-prefix>` to confirm no other leaks exist.

### Downstream Service Health

Before submitting real work, use `GET /api/health/downstream` to verify:
- OCR service URL is reachable + token is valid (PaddleOCR) or configured (MinerU)
- LLM service accepts auth + responds to a 1-token ping

The probe does NOT submit real OCR/LLM work — it just verifies auth + connectivity in <8 seconds.

### Packaging

- **Backend**: PyInstaller via `pbc-server.spec` → `dist/pbc-server/pbc-server.exe`. Hidden imports include `core.mineru_client`, `api.settings`. Resource paths resolve via `sys._MEIPASS` in frozen mode.
- **Frontend installer**: electron-builder via `build.ps1` → `dist-electron/PBC-Setup-1.0.0.exe`. Electron main (`electron/main.js`) spawns `pbc-server.exe`, health-checks, creates `BrowserWindow`, cleans up child processes on exit. Icon `icon.ico` loaded conditionally.
- **Run build in real PowerShell** (not IDE Sandbox) — AppData write restrictions in sandbox break packaging.

### Key Design Decisions

- **LLM provider architecture (Phase 7)**: providers are NO LONGER hardcoded. A dynamic registry in `config.py` (`_load_all_providers`) loads built-in providers (deepseek, siliconflow) + any declared via `LLM_PROVIDERS` env var. Each provider specifies a `protocol` (`openai` or `anthropic`) that selects the right adapter from `llm/adapters/`. The `LLMClient` (`llm/client.py`) owns retry/backoff + JSON parsing + audit logging; the adapter owns wire-format translation. Adding a provider requires only an env var entry — zero code changes.
- **Protocol adapters** (`llm/adapters/`): `OpenAIAdapter` wraps `openai.AsyncOpenAI` (handles DeepSeek, SiliconFlow, GLM, Kimi, Qwen, MiMo, OpenAI). `AnthropicAdapter` wraps `anthropic.AsyncAnthropic` (handles Claude) — loaded lazily so the `anthropic` package is optional. Both return a uniform `ChatResult` (content + token usage + model).
- **Settings API auth**: POST `/api/settings` is guarded by `is_local_request()` (core/security.py) — only requests with `Host: localhost:*` / `127.0.0.1:*` and an allow-listed `Origin` are accepted, blocking CSRF from arbitrary web origins.
- **SSRF protection**: `validate_external_url()` blocks base_url / OCR API URLs pointing to link-local (169.254/16), loopback (127/8), private (10/8, 192.168/16, 172.16/12), or unspecified (0.0.0.0) addresses.
- **.env atomic write**: Settings POST uses a PID + UUID-suffixed tmp file + `os.replace` for atomic rename, preventing concurrent-write corruption.
- **GMP audit trail**: every LLM call (per-page + cross-page + fallback) is recorded in `llm_call_audit` table with provider, protocol, model, prompt_version, token usage, latency, success/error — for traceability.
- **JSON parsing resilience** (`llm/client.py:_parse_json`): handles markdown fences, leading text, both `{...}` and `[...]`, truncated JSON recovery.
- **HTML cleaning** (`page_analyzer.py`): strips `style=`/`width=`, simplifies img src, truncates to 6000 chars. Prevents token overflow.
- **Rule + LLM hybrid**: rule-based checks (deterministic, no token cost) + LLM-based semantic anomalies. Both feed `findings` table with `source` field.
- **Resume**: pipeline skips pages that already have `structured_json` in `page_cache`.
- **Fault tolerance**: single page LLM failure sets `_parse_error` flag, cross-page analysis skips it, job continues to `partial_review`.

---

## Conventions

- **Language**: Code comments and commit messages in English. UI strings and LLM prompts in Chinese.
- **Docstrings**: every module has a module-level docstring explaining its role.
- **Error handling**: stage exceptions set job status to `error` with truncated message. No partial success — failed pages are marked but pipeline continues.
- **Finding severity**: `critical | warning | info`. Finding status: `pending | confirmed | rejected | corrected`.
- **OCR client**: blocking `requests` calls. Pipeline wraps with `asyncio.to_thread`. Don't call its functions directly from async context without threading.
- **Frontend logging**: `[PBC]` prefix with color coding. Critical DOM elements probed on `DOMContentLoaded` for E2E test visibility.

---

## Subdirectories

- `api/` — FastAPI routers (jobs, review, report, settings)
- `core/` — pipeline, OCR/LLM clients, analyzers
- `db/` — schema + aiosqlite client
- `llm/` — LLM client with retry + JSON recovery + GMP audit (`client.py`), protocol adapters (`adapters/`: `openai_adapter.py`, `anthropic_adapter.py`, `base.py`)
- `models/` — Pydantic schemas
- `templates/` — Jinja2 HTML
- `static/` — CSS, JS, design tokens (separated, no inline)
- `tests/` — unit + integration suites (pytest)
- `electron/` — Electron main process
- `samples/` — sample PDFs (gitignored binaries)
- `spike/` — experimental ad-hoc test inputs and reports (not part of app)
