-- BatchSentry — SQLite schema v2

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    total_pages INTEGER,
    md5 TEXT,                       -- MD5 content hash of uploaded PDF (duplicate-upload detection)
    failed_pages TEXT,              -- JSON array of page numbers that failed LLM analysis
    stage1_ms INTEGER,              -- OCR stage duration
    stage2_ms INTEGER,              -- per-page LLM analysis duration
    stage3_ms INTEGER,              -- cross-page analysis duration
    error_message TEXT,
    pdf_path TEXT,
    ocr_progress TEXT,              -- OCR 轮询进度 JSON {"done":N,"total":M}（Stage 1 实时）
    ocr_backend_used TEXT           -- 实际执行 OCR 的后端（双 OCR 主备切换后的审计记录）
);

CREATE TABLE IF NOT EXISTS page_cache (
    job_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    raw_html TEXT,
    structured_json TEXT,
    analyzed_at TIMESTAMP,
    PRIMARY KEY (job_id, page),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    ocr_text TEXT,
    operator TEXT,
    reviewer_note TEXT,
    status TEXT DEFAULT 'pending',
    corrected_text TEXT,
    source TEXT DEFAULT 'rule',  -- Phase 3: rule | llm_page | llm_fallback | llm_cross
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    finding_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_call_audit (
    -- GMP 审计追踪：记录每次 LLM 调用的 provider/model/prompt_version/token 用量
    -- 用于追溯"哪个模型的哪个版本给出了这条 finding"
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    page INTEGER,                       -- NULL = cross-page 调用
    stage TEXT NOT NULL,                -- "page_analysis" | "cross_page_llm"
    provider TEXT NOT NULL,
    protocol TEXT NOT NULL,             -- openai | anthropic
    model TEXT NOT NULL,
    prompt_version TEXT,                -- v3, etc.
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    latency_ms INTEGER,
    success INTEGER NOT NULL DEFAULT 1, -- 0 = exception occurred
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);
CREATE INDEX IF NOT EXISTS idx_findings_job_page ON findings(job_id, page);
CREATE INDEX IF NOT EXISTS idx_findings_job_status ON findings(job_id, status);
CREATE INDEX IF NOT EXISTS idx_page_cache_job ON page_cache(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_job ON audit_log(job_id);
CREATE INDEX IF NOT EXISTS idx_llm_audit_job ON llm_call_audit(job_id);
-- Performance: list_jobs 查询 WHERE status != 'archived' ORDER BY created_at DESC
-- 这两个索引让分页查询走索引而非全表扫描
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- Valid status transitions (documented, enforced in code):
-- pending → ocr_running → ocr_done → analyzing → review | partial_review
-- any active state → cancelling → cancelled
-- any state → error
-- error → pending (retry)
