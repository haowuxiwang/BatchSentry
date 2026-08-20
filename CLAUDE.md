# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**BatchSentry** — GMP 批生产记录半自动合规检查系统（前身 Pharma Batch Checker / PBC）。

用户上传 PDF 批生产记录 → OCR 识别 → LLM 结构化提取 → 规则+LLM 跨页合规分析 → 人工复核界面 → 导出报告。

**Current phase**: Phase 12 (Feishu job notifications) + 2 rounds of adversarial review (22 fixes incl. sliced-OCR callback arity P0, poll non-JSON retry, report cache correctness; round 2: cr-13 upload CSRF guard, cr-17 config precedence JSON-wins, MinerU footer selective retention, failover threshold max(2,10%), empty-page tag-strip detection + small-file self-heal, page-mark HTML comments, MINERU_BASE_URL), v0.1.0, local single-user deployment via PyInstaller exe + Electron wrapper.

**Module refactor (2026-08, post round-4)**: large modules split into packages with zero behavior change (verified: 1015 tests, route table identical 37/37, perf regression-free): `api/jobs.py` (1236 lines) → `api/jobs/{upload,listings,page_image,status,actions}.py`, `api/settings.py` (1128 lines) → `api/settings/{read,write,rules,provider,probe}.py`, `core/pipeline.py` (1692 lines) → `core/pipeline/{locks,state,ocr_support,self_heal,stage1,stage2,stage3,engine}.py`, `core/cross_page_analyzer.py` (1700 lines) → `core/rules/{base,parsing,rule_time,rule_spec,rule_doc,llm_checks}.py` (old module kept as a noqa'd re-export shim). Pattern: `__init__` owns the router/constants and re-exports names tests monkeypatch (`api.jobs.{launch_pipeline,Path,open,db_lock,transition_status,...}`, `api.settings._config_path`); consumers resolve those at **call time** via `from api.jobs import X` inside the function body so monkeypatching keeps working. `api/settings` router has NO prefix (decorators carry full paths). `core/cross_page_analyzer.py` is a shim (noqa F401); new code imports from `core.rules`. PyInstaller hiddenimports in `pbc-server.spec` list every leaf module (packages don't recurse) + `core.notify` (function-body dynamic import). **PIL removed from PyInstaller excludes** (Round 5: image upload→PDF conversion needs Pillow at runtime, previously broken in frozen exe).

**Round 5 (中文化收尾 + 流式补全, 2026-08)**: page markers made **visible** to LLM (`<!-- 第 N 页 -->` → `## 第 N 页` in mineru_client, page_analyzer already used that format — empty pages keep `（此页无文本内容）` placeholders); OCR truncation surfaced as `_ocr_truncated` flag (`_clean_html` returns `(cleaned, truncated)` tuple) → review banner "此页 OCR 内容过长已截断"; Paddle plain-text fallback (no `<table>/<tr>/<div>`) gets an adaptive prompt (`提取以下 纯文本内容 中的结构化数据` + `text` fence + explicit "表格结构已丢失" system warning) instead of being falsely labeled HTML; SSE snapshots gained `phase` (`ocr`/`analyze`/`cross`/`done`, derived: analyzing + pages_analyzed≥total_pages ⇒ cross-page analysis) and `self_heal_progress` (`{done,total,pages}` merged into jobs.ocr_progress as a `self_heal` sub-key by `_update_self_heal_progress`, cleared at end — upload/review pages show "空页自愈 x/y" instead of looking stuck); `ocr_backend_display` (zh_map `OCR_BACKEND_ZH`, incl. `cached`) in status/review/uploads; status-machine + HTTP error details localized (transition details like "开始 OCR 识别", "任务不存在", "问题记录不存在", Paddle/OcrClient poll messages, SSE "任务不存在" error frame); feishu_mode default unified to `webhook` (app_bot leftovers cleaned); user_rules total length cap 8000 chars with live counter; settings test-provider probes unsaved form values via `dataclasses.replace`; **frozen exe smoke-tested with image upload** (PORT=58766 smoke job: PNG → total_pages=1 → ocr_running, PIL path verified).

**Round 3 (P1-4~P1-8 + P2-1~P2-6)**: image upload (jpg/png/webp/bmp/tif/tiff → backend converts to PDF), MinerU structural completeness check, table-first truncation, review→pending full re-analysis state machine, signed-URL redaction, stuck-job recovery notifications + provider-test audit, unified GET/DELETE endpoint guards, recover_stuck_jobs process-start cutoff, CORS port constants.

**Round 3 audit round 3 (A1/B1/B2/C1/C2/C3/D3/A3)**: empty-page self-heal extended to Paddle (fitz single-page re-submit), `[OCR 警告]` prefix moved OUT of the fenced OCR data zone into the system-warning zone (`_OCR_WARNING_RE` in page_analyzer + `_ocr_warning` result key → review banner), schema-validation-failure fix-hint retry (1 retry with error echo, `_schema_warn` marker if still invalid), `run_ocr_pages` returns `(page, text, discarded_count)` so self-healed pages re-attach the OCR warning prefix, severity counts moved after dedup (log matches real DB writes), archive-nonexistent test corrected to 404 (unified guard behavior).

**Round 3 localization wrap-up (竞价收尾 / adversarial #2)**: `core/zh_map.py` single source for Chinese enums (severity/finding-status/job-status/finding-type) consumed by report.md / Feishu notify / InvalidTransitionError messages; `api/jobs.py` upload error messages + image edge cases (multi-page TIFF / animated WEBP → 400 explicit reject, transparent PNG composited on white background, pixel check on HEADER size before decode to avoid full-decode DoS); MinerU `full.md` separator split keeps empty pages as `（此页无文本内容）` placeholders (page numbers no longer shift); table truncation regex widened to `<table[\s>]` (attribute tables) + open table gets synthetic `</table>` in plain-truncation fallback (closed-table count uses anchored regex — `str.count("<table")` double-counts `</table>` substrings); OCR token masked write-back protection in settings (paddle_ocr_token/mineru_token, was feishu + api_key only); `recover_stuck_jobs` UPDATE now guarded by `status IN (...)` (stale snapshot can't clobber a job a concurrent path already advanced); SSE `_get_job_progress` projects columns instead of `SELECT *`; LLM timeout log path masked with `_mask_secrets`; protocol dropdown labels Chinese.

**Round 4 (UX + robustness)**: cancel checkpoint injection (`AnalysisCancelled` + `cancel_check` three checkpoints in `page_analyzer.analyze_page`, cancelled pages not counted as failed — single HTTP call remains uninterruptible), prompt page-number injection + `findings[].page` backend enforcement (`_validate_page_result` + force-overwrite after sanitize), LLM hallucination grounding check (`_grounding_check`/`_value_grounded`: ≥4-digit substring match, shorter numbers boundary-checked; `_grounding_warn` → review banner), UX P1 set (no `target="_blank"` in Electron, corrected/note info in AJAX finding cards, `safeAutoReload` skips reload while typing or in dialog, PDF zoom 0.5-2.0x buttons), finding locate v1 (click card → OCR panel mark + scroll), P2 quick wins (empty report.md explicit "no findings" text + total_pages from `jobs.total_pages` instead of page_cache COUNT which undercounts failed-OCR pages, image→PDF conversion 120s timeout via `asyncio.wait_for` → 408, review.js `has_more` notice when >50 findings truncated).

**Round 6 (规则第三轮 + 页 9 截断修复, 2026-08)**: 挂载两条新规则 —— R9a `signature_order`（同一 operator 跨步骤签名时间必须递增，`core/rules/rule_time.py._check_signature_order` + `_ROLE_RANK`；type 合入 `signature_time_anomaly`）和 R8b `check_consistency`（同一复核项前后勾选状态切换必须落在规定的 pendingPage/allowable_transitions 内，`core/rules/rule_doc.py._check_check_consistency`；type 合入 `completeness`）；**value_source 三层方案**（印刷体信任/手写体存疑）：① v3 prompt 的「列级可信度（value_source 必填）」项（标注每列 printed/handwritten），② base.py `_infer_value_source`（列名关键词启发式：实际/实测/记录/填写/手写/结果/偏差 → handwritten；规格/标准/范围/指导/要点/要求/检查项目/项目 → printed；LLM 50% 概率不输出，backfill 兜底，内存级不写回 DB）+ `_backfill_value_source`，③ rule_spec.py `_severity_for_out_of_spec(bounds, actual, spec, value_source)`：printed → warning 不降噪；handwritten/unknown → ≤10% `_EDGE_MARGIN` 边缘偏离降 info。`analyze_cross_page` 输出 value_source 覆盖率统计日志（parameters n/m、cells n/m）；**页 9 大矩阵页截断/超时修复**：页 9（9 时间点 × 8 列大表 + 12MB OCR HTML）LLM 输出 426-485s 且被 max_tokens=6000 截断在字符串中间 → JSON 不可恢复 → fix-hint 重试每次 240s 超时 → 12 分钟整页失败。修复：$`_PAGE_MAX_TOKENS=8000`/$`_PAGE_TIMEOUT=480.0`/$`_PAGE_RETRIES=2`（page_analyzer 两处 chat_json）；llm/client.py 新增 `_repair_truncated_json`（字符串中间截断 → 回退到开引号 + 补 null；尾部完整数字保留；legacy 补括号兜底）+ `_parse_json` block 提取加条件（text 以 `{`/`[` 开头时跳过 block 提取，防误抓内嵌 `[]` 返回空 list）+ 恢复时注入 `_truncated_recovered: True`（analyze_page 打 `_truncated_warn` 日志）。修复后同页 258s 成功、payload 12191 bytes；**server.py CORS 一致性修复**：`python server.py`（无 PORT env）监听 58765 但 config.app.port 默认 8000 → CORS allowlist 只放行 8000 → 浏览器设置页 POST/PUT 被 CORS 拦截。修复：`os.environ.setdefault("PORT", str(port))` 必须在 `from config import config` 之前（否则 config 模块已按 8000 初始化；Electron main.js 传 PORT=58765 天然一致）。

**Round 7 (value_source OCR 结构化信号优先 + 上下文按模型适配, 2026-08)**: `value_source` 从「LLM 猜」升级为「OCR 结构化信号优先」——调研结论：MinerU content_list_v2 无行级 score（score 在 middle.json 需额外参数）、PaddleOCR-VL 官方确认 VLM 不给 confidence、**MinerU `###` 低置信度占位符（已清洗为 `[手写内容未识别]`）是唯一可用单元格级机器信号**。新模块 `core/hw_signal.py`：`_extract_low_conf_tokens(text)` 把标记转为确定性 token —— ①列信号：管道表/HTML 表内标记单元格 → 表头名（colspan/rowspan 破坏列对齐则禁用列映射，退化为标签信号）；②标签信号：`审核意见:[标记]` 与 `签名[标记]`（无冒号，真实记录页 39/40/41/47 形态；CJK 标点截断防吞描述句）两种形态都提取标签。规则层 `_backfill_value_source` 优先级变为 **0. OCR 信号（`_ocr_low_conf_cols`，含 `_matches_low_conf` 双向包含匹配）> 1. 单元格标记 > 2. LLM 标注 > 3. 关键词兜底**（机器事实 > 模型猜测）。analyze_page 在清洗后文本上提取并注入 `_ocr_low_conf_cols` 内部键（与 `_ocr_warning` 同机制）；rules 覆盖率日志新增「OCR handwriting signal: n/m pages」；**上下文预算按模型适配**：`config.app.llm_context_window`（`LLM_CONTEXT_WINDOW` env / config.json 顶层键，默认 128000）→ `llm_checks._summary_max_chars(window)` = max(8000, window×0.35×1.6 字符)（≤35% 窗口预算，防 200 页 job/32K 小窗模型溢出；无窗口参数时回退 `_SUMMARY_MAX_CHARS=100_000` 保持旧行为）。验证：1118 tests/90.44%；定向 e2e 5 标记页（真实 OCR→真实 LLM）信号强制 64/64 值 handwritten（页 36 封面仅得 5 批准类 token、无参数可强制，符合预期）；图片 e2e ×2 + 51 页全量 e2e：value_source 356/356+450/450 覆盖率 100%、summary 24.2K tokens（<71K 预算不截断）、0 失败页；打包 3 轮成功（含 build.ps1 58765 占用即 FAIL 的坑——打包前须停 dev server）+ frozen e2e（APPDATA 隔离、图片上传全链路 review、12.4KB payload 无截断）。pbc-server.spec hiddenimports 新增 `core.hw_signal`。

**Test status**: 1118 passed, 90.44% coverage (target ≥90%). Note: test_config/test_health share the process-global config singleton — both now restore original values (order-independent).

**Web 版（飞书入口）决策（2026-08-18）**: 调研已完成（WEB.md，D1~D5 已拍板），但 **Web 版暂缓实施** — 当前优先桌面端迭代，不主动开展 Web 化（auth_mode/守卫改造/移动端适配等）。后续若启动，按 WEB.md §6 实施路线推进，决策结论无需重开讨论。

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

Test coverage target: ≥90%. Current: 94.09% (1081 tests, see `tests/` with unit + integration suites).

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

**Frozen mode (PyInstaller bundle)**: config is read from `%APPDATA%/PBC/config.json` (Windows), `~/Library/Application Support/PBC/config.json` (macOS), `~/.local/share/PBC/config.json` (Linux). Database and output files redirect to `%APPDATA%/PBC/` as well. Use the in-app Settings page to edit credentials at runtime — saves are applied live without restart. **Gotcha**: `config.json` must be UTF-8 **without BOM** — PowerShell 5.1 `Set-Content -Encoding UTF8` writes a BOM that makes the frozen server exit with code 1 at startup (config parse fails before logging initializes). Use the Settings page or write with `encoding="utf-8"` from Python.

**Runtime config source is `config.json` (Phase 9)**, not `.env`. On first run, a legacy `.env` is auto-migrated into `config.json` (loaded once; thereafter `config.json` wins). `config.py` exposes `update_config()` to mutate the in-memory config for live reload when the Settings page saves.

---

## Architecture

### Frontend (Jinja2 + Tailwind + vanilla JS)

Templates, styles, and scripts are **strictly separated** — no inline CSS/JS except a single `window.__PBC__` bridge per page.

- `templates/upload.html` + `static/upload.js` + `static/upload.css` — upload + job history list
- `templates/review.html` + `static/review.js` + `static/review.css` — 3-column review (page nav | PDF | findings)
- `templates/settings.html` + `static/settings.js` + `static/settings.css` — LLM/OCR credential editor
- `static/confirm-dialog.js` — shared Notion-style confirm/prompt dialogs + toast (`window.PBC.confirmDialog / promptDialog / showToast`), included by all three pages
- `static/app.css` — locally built Tailwind output (15.8KB, do not edit directly)
- `static/design-tokens.css` — shadcn HSL variables

Design system: minimalist, white background, black primary, flat lists (no cards), `border-b` hairline separators, pill-shaped nav buttons. No dark mode. Round 4 P2/P3 additions: page headers keep in-content breadcrumb + page title (BatchSentry / page name) with action button on the right — no global app bar, review page keeps its native 48px top bar; settings provider rows flattened to `border-b` list items (no nested cards) with left indicator for active; `tabular-nums` on all numeric data (times/pages/measurements); skip-link (`.skip-link` in input.css, all pages); page-shaped skeleton screens for upload history list + PDF loading (no spinner). Critical banners use static red glow — no infinite pulse (E2E probe in review.js checks the static rule).

Frontend logs use `[PBC]` prefix with color coding (blue=info, orange=warn, red=error).

### Backend (FastAPI + aiosqlite)

Entry point: `main.py` (dev) or `server.py` (bundled, port 58765).

**Three-stage pipeline** (`core/pipeline.py`) runs as FastAPI `BackgroundTask`:

1. **Stage 1 — OCR** (`core/ocr_client.py` or `core/mineru_client.py`): submit PDF → poll → download JSON. 10-minute poll timeout, 5s interval. Blocking `requests` wrapped via `asyncio.to_thread`.
   **Dual-OCR failover**: `_get_ocr_chain()` builds a primary+secondary chain (primary = `OCR_BACKEND`, secondary = the other backend if its token/api_url is configured). `_run_ocr_with_failover()` retries the whole job on the secondary when the primary raises, returns 0 pages, or loses >20%/5 pages vs the PDF physical page count (`_pdf_page_count`). The actual backend is stored in `jobs.ocr_backend_used` and surfaced in `/api/jobs/{id}` + SSE snapshots (GMP traceability). Sliced mode (`OCR_SLICES>1`, MinerU only) keeps its own path without failover.
   **MinerU structural completeness (Round 3 P1-4b)**: `_split_pages_by_content_list` returns `(pages, n_tables, n_paragraphs)`; MinerU pages whose `n_tables == 0` while the PDF physical page count ≥2 are treated as incomplete → whole-job failover to the secondary OCR backend.
   **Empty-page self-healing** (Phase 11): MinerU drops pages on >100MB PDFs (server-side defect; a page OCR'd standalone returns 1111-1702 chars). After page_cache write, pages with `<100` chars (tag-stripped text length) are re-OCR'd as small slices via `run_ocr_pages()` (two rounds: batch_size 3 then 1; mineru + any file size). Recovered pages UPDATE page_cache; audit_log records `stage1_empty_pages` / `stage1_empty_recovered`. **Round 3 audit A1**: extended to Paddle (fitz single-page slice re-submitted once, no slice API); **D3**: `run_ocr_pages` returns `(page, text, discarded_count)` and self-healed pages re-attach the `[OCR 警告]` prefix when the slice still dropped low-confidence blocks (previously silently treated as complete). Truly empty pages stay as-is and the review UI shows an `_ocr_empty` banner (manual review path).
2. **Stage 2 — Per-page LLM** (`core/page_analyzer.py`): each page's HTML table → LLM extraction prompt → structured JSON with `steps[].measurements[]` time series. Uses string concatenation (NOT `.format()`) to avoid brace collision with HTML. 240s timeout (fix-hint JSON-recovery retries inherit it), 3 retries with exponential backoff. **Round 3 audit B1**: the `[OCR 警告:...]` prefix injected by the pipeline is stripped out of the `<PBC_UNTRUSTED_OCR>` fenced data zone (`_OCR_WARNING_RE`) and re-injected as a `[系统警告]` in the system zone — plus an `_ocr_warning` result key surfaced in the review banner (C3) — so LLM treats it as an instruction-level signal instead of ignorable OCR data. **C1**: schema-validation failures trigger 1 fix-hint retry echoing the errors (`_schema_warn` marker persists the page as analysed-but-flagged if still invalid).
3. **Stage 3 — Cross-page analysis** (`core/cross_page_analyzer.py`): rule-based time reversal + LLM-based semantic anomalies + user-defined compliance rules injected into the LLM prompt (`source=user_rule` findings; `findings.user_rule_id` carries the matched rule id; `prompt_version` carries a rules content hash for GMP traceability). All write to the same `findings` table with `source` field (`rule` / `llm_page` / `llm_cross` / `llm_fallback` / `user_rule`).

**Job completion notifications (Phase 12, `core/notify.py`)**: on terminal state (review / partial_review / error / cancelled), `notify_job()` pushes a Feishu summary. Two channels: webhook group bot or app_bot DM (event subscription). 90-min dedup cache; notification failure never blocks the pipeline. Config in `config.json` under the `feishu` keys, editable from the Settings page ("飞书通知" section, includes a "测试连接" button hitting `POST /api/settings/test_feishu`).

**Live progress (SSE, Phase 10)**: `GET /api/jobs/{id}/stream` pushes a progress snapshot every 3s (default `message` event, `done` event + close on terminal state, `error` event when the job is missing). Review page subscribes and hot-refreshes the current page's findings as `pages_analyzed` grows (page-level streaming — no need to wait for the whole job). Upload page tracks active job rows the same way: inline `OCR 12/51` counts, per-page analysis counts, and auto re-enabling of archive/delete buttons at terminal state. Frontend logs page-level events via `[PBC]` logger.

**State machine** (`pipeline.VALID_TRANSITIONS`): `pending → ocr_running → ocr_done → analyzing → review | partial_review | error | cancelled`; cancel is a two-step `... → cancelling → cancelled` (pipeline checks `_is_cancelled` between stages/rounds, keeping partial results). Terminal states can `archived`; `error`/`cancelled` → `pending` for retry. **Round 3 P1-6**: `review → pending` allowed (full re-analysis; `partial_review → pending` also allowed — only missing pages). Invalid transitions raise `InvalidTransitionError`.

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

- **`jobs`**: id, filename, status, pdf_path, total_pages, md5 (duplicate-upload detection), failed_pages, stage1_ms/stage2_ms/stage3_ms, ocr_progress (JSON), ocr_backend_used (dual-OCR audit), error_message, created_at, finished_at
- **`page_cache`**: (job_id, page) → raw_html + structured_json + analyzed_at
- **`findings`**: id, job_id, page, type, severity, source, description, ocr_text, operator, status (`pending → confirmed | rejected | corrected`), reviewer_note, corrected_text, reviewed_at (+ `user_rule_id` when `source='user_rule'`)
- **`audit_log`**: id, job_id, finding_id, action, detail, created_at

SQLite via `aiosqlite` with WAL mode. Singleton connection in `db/client.py`.

### API Layer (`api/`)

| Router | Prefix | Purpose |
|---|---|---|
| `jobs/` (package) | `/api/jobs` | Upload (8MB chunked, 200MB max; PDF or image), status, cancel, retry, archive, unarchive, delete, page data, findings |
| `review.py` | `/api/jobs/{id}/findings` | List/get/update findings (confirm/reject/correct) + audit log + page measurements |
| `report.py` | `/api/jobs/{id}/report.{md,json}` | Export Markdown + JSON reports |
| `settings/` (package, no prefix) | `/api/settings` | Read (masked) / update `config.json` with live reload |

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

- **CORS**: allowlist generated from `config["app"].port` (Round 3 B8) — 127.0.0.1 + localhost on the actual serving port (dev 8000 / Electron 58765 via `PORT` env). Port constant lives in `config.py` (`port=_env_int("PORT", _env_int("APP_PORT", 8000))`); changing `electron/main.js` `SERVER_PORT` no longer breaks CORS. `file://` removed to prevent XSS via Electron renderer.
- **CORS headers**: restricted to `Content-Type, X-Request-ID` (not `*`).
- **Endpoint guards (unified)**: all state-changing endpoints (upload/cancel/retry/archive/unarchive/delete/settings/shutdown/health-probe) AND all GET read endpoints (jobs list/status/page image/SSE/findings/audit/reports, Round 3 P2-1) run `is_local_request()` — non-local `Host` → 403. GET read endpoints were previously unguarded (side-channel probing via `<img>/<script>` from hostile pages).
- **Upload**: 8MB chunked streaming, `Path(file.filename).name` sanitization, 200MB hard limit, empty-file rejection, magic bytes check (`%PDF-` for PDF; per-format prefixes for jpg/png/webp/bmp/tif/tiff), MD5 content-hash duplicate rejection (409, `force=1` bypass). Empty filename falls back to `{job_id}.pdf` (still magic-checked).
- **SQL**: all queries parameterized (`?` placeholders).
- **Secrets**: `.env` never committed; Settings API masks keys (`sk-abcd...wxyz`).
- **PDF preview**: pages are rendered by PyMuPDF to JPEG (quality 82) via `GET /api/jobs/{id}/page/{n}` and shown as `<img>` (zoom capped at 2000px, cached 6 docs / 30min TTL, render in thread pool). `content_disposition_type="inline"` for the raw PDF endpoint.
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
- **Frontend portable**: electron-builder via `build.ps1` → `dist-electron/BatchSentry 1.0.0.exe` (single-file portable, no installer). Electron main (`electron/main.js`) spawns `pbc-server.exe`, health-checks, creates `BrowserWindow`, cleans up child processes on exit. Icon `icon.ico` loaded conditionally.
- **Run build in real PowerShell** (not IDE Sandbox) — AppData write restrictions in sandbox break packaging.

### Key Design Decisions

- **LLM provider architecture (Phase 7)**: providers are NO LONGER hardcoded. A dynamic registry in `config.py` (`_load_all_providers`) loads built-in providers (deepseek, siliconflow) + any declared via `LLM_PROVIDERS` env var. Each provider specifies a `protocol` (`openai` or `anthropic`) that selects the right adapter from `llm/adapters/`. The `LLMClient` (`llm/client.py`) owns retry/backoff + JSON parsing + audit logging; the adapter owns wire-format translation. Adding a provider requires only an env var entry — zero code changes.
- **Protocol adapters** (`llm/adapters/`): `OpenAIAdapter` wraps `openai.AsyncOpenAI` (handles DeepSeek, SiliconFlow, GLM, Kimi, Qwen, MiMo, OpenAI). `AnthropicAdapter` wraps `anthropic.AsyncAnthropic` (handles Claude) — loaded lazily so the `anthropic` package is optional. Both return a uniform `ChatResult` (content + token usage + model).
- **Settings API auth**: POST `/api/settings` is guarded by `is_local_request()` (core/security.py) — only requests with `Host: localhost:*` / `127.0.0.1:*` and an allow-listed `Origin` are accepted, blocking CSRF from arbitrary web origins.
- **SSRF protection**: `validate_external_url()` blocks base_url / OCR API URLs pointing to link-local (169.254/16), loopback (127/8), private (10/8, 192.168/16, 172.16/12), or unspecified (0.0.0.0) addresses.
- **.env atomic write**: Settings POST uses a PID + UUID-suffixed tmp file + `os.replace` for atomic rename, preventing concurrent-write corruption.
- **GMP audit trail**: every LLM call (per-page + cross-page + fallback) is recorded in `llm_call_audit` table with provider, protocol, model, prompt_version, token usage, latency, success/error — for traceability.
- **JSON parsing resilience** (`llm/client.py:_parse_json`): handles markdown fences, leading text, both `{...}` and `[...]`, truncated JSON recovery. Parse failures trigger a fix-hint retry (`chat_json`, up to 2 extra single-shot calls, no API-level backoff) — found by the 51-page real-file regression (page 19 returned ```json-fenced output that survived the API call but failed parsing).
- **Upload dedup**: MD5 computed during chunked streaming (schema v3, `jobs.md5`); identical content → 409 with existing job hint. Dedup check + INSERT are inside `db_lock` (no TOCTOU race).
- **Image upload (Round 3 P1-4)**: jpg/png/webp/bmp/tif/tiff accepted; backend converts to PDF (Pillow `exif_transpose` for camera orientation + PyMuPDF at 300 DPI) so pipeline/OCR/LLM/review/report stay untouched. Original image archived in job_dir; `pdf_path` points at the converted `<job_id>.pdf`; MD5 is computed on the ORIGINAL image bytes (dedup works); audit_log records `source=image`. Magic bytes checked per format independently of extension.
- **HTML cleaning** (`page_analyzer.py`): strips `style=`/`width=`, simplifies img src, truncates to 12000 chars **table-first** (Round 3 P1-5: keep table content over body text; single oversized table falls back to plain truncation with explicit marker; multiple tables keep the fitting prefix). LLM knows info is incomplete. Prevents token overflow.
- **Rule + LLM hybrid**: rule-based checks (deterministic, no token cost) + LLM-based semantic anomalies. Both feed `findings` table with `source` field.
- **Resume**: pipeline skips pages that already have `structured_json` in `page_cache`.
- **Full re-analysis (Round 3 P1-6)**: `retry` on a `review`-state job = full re-analysis (clears findings + NULLs `structured_json`, keeps `raw_html` OCR cache, audit `analysis_reset`); `partial_review` still retries only missing pages.
- **Fault tolerance**: single page LLM failure sets `_parse_error` flag, cross-page analysis skips it, job continues to `partial_review`.
- **Crash recovery guard (Round 3 P2-x)**: `recover_stuck_jobs(process_started_at)` only marks jobs with `created_at` EARLIER than the process start as error — new uploads racing the async recovery task are never mis-marked.

---

## Conventions

- **Language**: Code comments and commit messages in English. UI strings and LLM prompts in Chinese.
- **Docstrings**: every module has a module-level docstring explaining its role.
- **Error handling**: stage exceptions set job status to `error` with truncated message. No partial success — failed pages are marked but pipeline continues.
- **Finding severity**: `critical | warning | info`. Finding status: `pending | confirmed | rejected | corrected`.
- **OCR client**: blocking `requests` calls. Pipeline wraps with `asyncio.to_thread`. Don't call its functions directly from async context without threading.
- **Dialogs**: never use native `alert()/confirm()/prompt()` — use `PBC.confirmDialog / promptDialog` (async Promise). Confirm dialogs for destructive actions focus the cancel button by default (Enter never misfires delete); Esc/overlay cancel; Enter follows focused button; Tab is trapped inside the dialog. All dialogs carry `role=dialog` + `aria-modal` + `aria-labelledby` (APG pattern).
- **Frontend logging**: `[PBC]` prefix with color coding. Critical DOM elements probed on `DOMContentLoaded` for E2E test visibility.

---

## Subdirectories

- `api/` — FastAPI routers (jobs/ package, review, report, settings/ package)
- `core/` — pipeline/ package, OCR/LLM clients, page analyzer, rules/ package
- `db/` — schema + aiosqlite client
- `llm/` — LLM client with retry + JSON recovery + GMP audit (`client.py`), protocol adapters (`adapters/`: `openai_adapter.py`, `anthropic_adapter.py`, `base.py`)
- `models/` — Pydantic schemas
- `templates/` — Jinja2 HTML
- `static/` — CSS, JS, design tokens (separated, no inline)
- `tests/` — unit + integration suites (pytest)
- `electron/` — Electron main process
- `samples/` — sample PDFs (gitignored binaries)
- `spike/` — experimental ad-hoc test inputs and reports (not part of app)
