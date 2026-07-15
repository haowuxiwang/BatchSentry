-- Pharma Batch Checker — SQLite schema

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    total_pages INTEGER,
    ocr_cost_ms INTEGER,
    error_message TEXT,
    pdf_path TEXT
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_page_cache_job ON page_cache(job_id);
