-- BatchSentry — SQLite schema v2

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
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
    ocr_diagnostics TEXT,          -- 页级 OCR 完整性证据（JSON）
    regions_json TEXT,             -- v11: 区域级证据锚（P0-3）{"space":[w,h],"regions":[{label,bbox,text}]}
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
    user_rule_id TEXT,           -- Phase 11: 命中的用户规则 id（source='user_rule' 时，GMP 溯源）
    gmp_basis TEXT,
    kb_refs TEXT,                -- v8: 知识库引用 JSON [{entry_id,label,excerpt,score}]              -- v7: 法规依据引用（GMP 2010/ALCOA+ 等知识库映射，gmp_basis.py）
    confidence REAL,             -- v10: 写入期置信度 [0.30,0.95]（core.finding_quality），旧行 NULL 时读取期兜底
    raw_type TEXT,               -- v10: 归一前的原始 type（LLM 输出留痕；GMP 可追溯，仅在归一发生时写入）
    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    reviewed_at TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    finding_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
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
    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);
CREATE INDEX IF NOT EXISTS idx_findings_job_page ON findings(job_id, page);
CREATE INDEX IF NOT EXISTS idx_findings_job_status ON findings(job_id, status);
-- v5: 去重索引 — retry 防重复插入 + INSERT OR IGNORE 原子去重
CREATE UNIQUE INDEX IF NOT EXISTS idx_findings_dedup ON findings(job_id, source, page, type, description);
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

CREATE TABLE IF NOT EXISTS kb_entries (
    entry_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    chapter TEXT,
    article_label TEXT NOT NULL,
    no INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kb_entries_source ON kb_entries(source_id);

-- v11: 抑制留痕（P0-2）— 被降噪规则抑制的候选条目的**不可变台账**。
-- 抑制 ≠ 删除：EU GMP Annex 11 §16 / 中国附录《计算机化系统》第 15/16 条要求
-- 关键数据的修改经批准并记录理由。因此 reason 由 CHECK 强制非空 ——
-- 数据库层直接堵死"无理由的抑制"这种不可抽检的记录。
-- reverted_* 记录"一键回退为正式 finding"的动作（原记录不改写，保持台账完整）。
CREATE TABLE IF NOT EXISTS finding_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    description TEXT NOT NULL,
    ocr_text TEXT,
    source TEXT NOT NULL DEFAULT 'llm_page',
    -- SQLite 的单参 trim() 默认只去 ASCII 空格，\t / \n / \r 会被留下 →
    -- 一个"看起来有理由"的制表符就能绕过留痕要求。显式给出空白字符集。
    reason TEXT NOT NULL
        CHECK (length(trim(reason, char(32) || char(9) || char(10) || char(13))) > 0),
    evidence TEXT,                 -- JSON：命中的 (名称, 规格, 实测, 判定) 明细
    reverted_finding_id INTEGER,   -- 已回退为正式 finding 时记录其 id
    reverted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_suppress_job ON finding_suppressions(job_id);
CREATE INDEX IF NOT EXISTS idx_suppress_job_page ON finding_suppressions(job_id, page);
-- 重分析/重试幂等：同一页同一指纹只留一条台账
CREATE UNIQUE INDEX IF NOT EXISTS idx_suppress_dedup
    ON finding_suppressions(job_id, page, type, description);

