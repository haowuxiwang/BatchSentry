"""#3 人工修正回流（few-shot exemplar）测试。

覆盖：
- _load_review_exemplars：只取 LLM 生成型 + 已裁决 + 非空描述，按时间倒序
- _exemplars_section：空池返回空串；有样例时含裁决中文与截断
- _llm_based_check：有样例时 prompt 含 <HUMAN_REVIEW_FEEDBACK> 且
  prompt_version 带 fb 标记；无样例时不带
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from config import config


@pytest_asyncio.fixture
async def pipeline_db(tmp_path):
    import db.client as db_mod
    from config import config as _cfg

    db_path = tmp_path / "test.db"
    orig = (_cfg["app"].database_path, _cfg["app"].output_dir, db_mod._db)
    _cfg["app"].database_path = str(db_path)
    _cfg["app"].output_dir = str(tmp_path / "output")
    db_mod._db = None
    try:
        db = await db_mod.get_db()
        yield db
    finally:
        import core.pipeline as p_mod

        p_mod.db_lock._loop = None
        p_mod.db_lock._waiters = None
        p_mod.db_lock._locked = False
        if db_mod._db:
            await db_mod._db.close()
        db_mod._db = orig[2]
        _cfg["app"].database_path = orig[0]
        _cfg["app"].output_dir = orig[1]


async def _insert_finding(db, source, status, description,
                          ftype="completeness", severity="warning",
                          reviewed_at=None):
    # findings.job_id 有外键约束 — 确保宿主 job 存在
    await db.execute(
        "INSERT OR IGNORE INTO jobs (id, filename, status, pdf_path) "
        "VALUES ('job-x', 't.pdf', 'review', '/tmp/t.pdf')"
    )
    await db.execute(
        "INSERT INTO findings (job_id, page, type, severity, description, "
        "source, status, created_at, reviewed_at) VALUES (?, 1, ?, ?, ?, ?, ?, "
        "datetime('now','localtime'), ?)",
        ("job-x", ftype, severity, description, source, status, reviewed_at),
    )
    await db.commit()


class TestLoadReviewExemplars:
    @pytest.mark.asyncio
    async def test_filters_source_status_and_empty(self, pipeline_db):
        from core.rules.llm_checks import _load_review_exemplars

        await _insert_finding(pipeline_db, "llm_cross", "confirmed", "确认的问题",
                              reviewed_at="2026-08-20 10:00:00")
        await _insert_finding(pipeline_db, "llm_cross", "rejected", "误报的问题",
                              reviewed_at="2026-08-21 10:00:00")
        await _insert_finding(pipeline_db, "llm_cross", "pending", "未裁决不算")
        await _insert_finding(pipeline_db, "rule", "confirmed", "规则层不算")
        await _insert_finding(pipeline_db, "llm_page", "confirmed", "页级不算")
        await _insert_finding(pipeline_db, "llm_fallback", "corrected", "")

        exemplars = await _load_review_exemplars()
        descs = [e["description"] for e in exemplars]
        assert descs == ["误报的问题", "确认的问题"]  # 倒序

    @pytest.mark.asyncio
    async def test_limit_respected(self, pipeline_db):
        from core.rules.llm_checks import _load_review_exemplars

        for i in range(20):
            await _insert_finding(
                pipeline_db, "llm_cross", "confirmed", f"问题 {i}",
                reviewed_at=f"2026-08-{(i % 28) + 1:02d} 10:00:00",
            )
        exemplars = await _load_review_exemplars(limit=5)
        assert len(exemplars) == 5


class TestExemplarsSection:
    def test_empty_pool_returns_empty(self):
        from core.rules.llm_checks import _exemplars_section

        assert _exemplars_section([]) == ""

    def test_section_contains_verdicts_and_truncates(self):
        from core.rules.llm_checks import _exemplars_section

        long_desc = "很长的描述" * 30
        section = _exemplars_section([
            {"type": "batch_consistency", "severity": "critical",
             "description": long_desc, "verdict": "rejected"},
            {"type": "completeness", "severity": "info",
             "description": "缺 QA 签名", "verdict": "confirmed"},
        ])
        assert "<HUMAN_REVIEW_FEEDBACK>" in section
        assert "误报" in section
        assert "问题属实" in section
        # 描述截断到上限（80 字符）
        assert long_desc not in section


class TestLlmBasedCheckFeedback:
    @pytest.mark.asyncio
    async def test_prompt_includes_feedback_and_version_tag(self, pipeline_db):
        from core.rules import llm_checks

        await _insert_finding(
            pipeline_db, "llm_cross", "rejected", "历史误报样例"
        )

        captured = {}

        class _FakeClient:
            async def chat_json(self, system_prompt, user_content, **kw):
                captured["prompt"] = user_content
                captured["version"] = kw["audit_ctx"]["prompt_version"]
                return []

        with patch.object(
            llm_checks, "get_llm_client", return_value=_FakeClient()
        ):
            findings = await llm_checks._llm_based_check(
                "摘要内容", job_id="job-fb"
            )

        assert findings == []
        assert "<HUMAN_REVIEW_FEEDBACK>" in captured["prompt"]
        assert "历史误报样例" in captured["prompt"]
        assert "+fb1" in captured["version"]

    @pytest.mark.asyncio
    async def test_no_feedback_no_tag(self, pipeline_db):
        from core.rules import llm_checks

        captured = {}

        class _FakeClient:
            async def chat_json(self, system_prompt, user_content, **kw):
                captured["prompt"] = user_content
                captured["version"] = kw["audit_ctx"]["prompt_version"]
                return []

        with patch.object(
            llm_checks, "get_llm_client", return_value=_FakeClient()
        ):
            await llm_checks._llm_based_check("摘要内容", job_id="job-nofb")

        assert "<HUMAN_REVIEW_FEEDBACK>" not in captured["prompt"]
        assert "+fb" not in captured["version"]
