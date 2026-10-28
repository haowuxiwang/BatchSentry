"""R103/P0-2：任务被中断时，`jobs.error_message` 必须诚实且可操作。

背景（2026-10-26 实测事故，`e50e9a7e…`）
--------------------------------------
51 页 / 138 MB 的任务，OCR 在 10:50:32 就全部完成（`ocr_progress` = 51/51），
Stage 2 从 10:51:14 跑到 11:07:07 分析了 33 页，随后后端被构建脚本的冒烟
测试强杀（见 `build.ps1` 2.5 步）。启动恢复把它写成：

    应用重启恢复：原状态 analyzing 非终态，标记为 error 供重试

三个问题，用户的原话是"报错显示不友好"：

1. **归因错** —— 用户没有重启，是构建脚本杀的；
2. **不说完成度** —— 用户只看到"任务处理失败"，不知道自己已经跑完 33/51 页；
3. **不说能续跑** —— 而续跑**是支持的**：`retry` 复用 `raw_html`
   （Stage 1 走 `stage1_skipped`），Stage 2 用 `_get_analyzed_pages` 跳过已分析页。

本文件锁定"文案必须含完成度与续跑承诺"，并钉住两个反向控制：
- 完成度**不得**取 `ocr_progress`（`analyzing` 阶段它早已是 100%，会把
  33/51 说成 51/51 —— 正是本次最误导人的那个数）；
- 读取进度失败**不得**让恢复流程本身失败。
"""
from __future__ import annotations

import pytest

import core.pipeline.state as state_mod
from core.pipeline.state import _interruption_message, recover_stuck_jobs


async def _async(obj):
    return obj


async def _insert_job(test_db, job_id: str, status: str, total_pages=None,
                      ocr_progress=None):
    await test_db.execute(
        "INSERT INTO jobs (id, filename, status, pdf_path, total_pages, ocr_progress)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, f"{job_id}.pdf", status, f"/tmp/{job_id}.pdf",
         total_pages, ocr_progress),
    )
    await test_db.commit()


async def _insert_page(test_db, job_id: str, page: int, analyzed: bool):
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, analyzed_at)"
        " VALUES (?, ?, ?, ?)",
        (job_id, page, "<p>x</p>",
         "2026-10-26 11:00:00" if analyzed else None),
    )
    await test_db.commit()


class TestInterruptionMessage:
    """文案三要素：发生了什么 / 完成到哪 / 能不能续跑。"""

    @pytest.mark.asyncio
    async def test_reports_analyzed_page_count(self, test_db):
        await _insert_job(test_db, "job-1", "analyzing", total_pages=51)
        for p in range(1, 34):          # 33 页已分析
            await _insert_page(test_db, "job-1", p, analyzed=True)

        msg = await _interruption_message(test_db, "job-1", "analyzing")

        assert "33/51" in msg, f"必须报完成度，实际：{msg}"
        assert "中断" in msg
        assert "重试" in msg and "继续" in msg, f"必须说明可续跑，实际：{msg}"

    @pytest.mark.asyncio
    async def test_does_not_use_ocr_progress_as_completion(self, test_db):
        """反向控制：`ocr_progress` 是 51/51，但分析只做了 33 页。

        若实现改用 `ocr_progress`，文案会变成 51/51 —— 那是在骗人。
        """
        await _insert_job(
            test_db, "job-2", "analyzing", total_pages=51,
            ocr_progress='{"done": 51, "total": 51}',
        )
        for p in range(1, 34):
            await _insert_page(test_db, "job-2", p, analyzed=True)

        msg = await _interruption_message(test_db, "job-2", "analyzing")

        assert "33/51" in msg
        assert "51/51" not in msg, f"不得把 OCR 完成度当分析完成度：{msg}"

    @pytest.mark.asyncio
    async def test_zero_pages_says_resume_from_analysis(self, test_db):
        """一页都没分析完时也要给出可操作指引（OCR 已完成的场景）。"""
        await _insert_job(test_db, "job-3", "analyzing", total_pages=51)

        msg = await _interruption_message(test_db, "job-3", "analyzing")

        assert "0/51" in msg
        assert "OCR" in msg, f"应说明 OCR 已保留，实际：{msg}"

    @pytest.mark.asyncio
    async def test_unknown_total_falls_back_without_numbers(self, test_db):
        """`total_pages` 为空时不得编造 `0/0`。"""
        await _insert_job(test_db, "job-4", "ocr_running", total_pages=None)

        msg = await _interruption_message(test_db, "job-4", "ocr_running")

        assert "0/0" not in msg
        assert "ocr_running" in msg, "没有数字时必须交代原状态"
        assert "重试" in msg

    @pytest.mark.asyncio
    async def test_read_failure_does_not_raise(self, test_db):
        """进度读取失败（表缺失）必须回落文案，绝不能把恢复升级成启动崩溃。"""
        await _insert_job(test_db, "job-5", "analyzing", total_pages=51)
        await test_db.execute("DROP TABLE page_cache")
        await test_db.commit()

        msg = await _interruption_message(test_db, "job-5", "analyzing")

        assert "中断" in msg and "重试" in msg


class TestRecoverWritesHonestMessage:
    """端到端：`recover_stuck_jobs` 落库的文案必须是新文案。"""

    @pytest.mark.asyncio
    async def test_recovery_message_contains_progress(self, test_db, monkeypatch):
        await _insert_job(test_db, "job-6", "analyzing", total_pages=51)
        for p in range(1, 4):           # 3 页已分析
            await _insert_page(test_db, "job-6", p, analyzed=True)
        monkeypatch.setattr(state_mod, "get_db", lambda: _async(test_db))
        monkeypatch.setattr("core.notify.notify_job", _async_none)

        assert await recover_stuck_jobs() == 1

        cur = await test_db.execute(
            "SELECT status, error_message FROM jobs WHERE id = 'job-6'"
        )
        row = await cur.fetchone()
        assert row["status"] == "error"
        assert "3/51" in row["error_message"]
        assert "应用重启恢复" not in row["error_message"], (
            "旧文案把构建脚本强杀说成'应用重启'，归因错"
        )


async def _async_none(*a, **k):
    return None
