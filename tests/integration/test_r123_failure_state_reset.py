"""#147 + #148：重试必须清空上一轮的失败状态；报告必须自带失败信息。

## 两个缺陷是一条链，不能只修一头

- **#147**：`retry` 只清 `error_message` / `finished_at`，而 `failed_pages` 与
  `stage1/2/3_ms` **只由 Stage 3 的终态 UPDATE 覆盖** ⇒ 用户点重试之后、
  Stage 3 跑完之前（OCR + 逐页分析，可能几十分钟），复核页 / 任务列表 /
  报告读到的都是**上一轮**的失败页与耗时。
- **#148**：报告（GMP 导出件）压根不携带失败信息 —— `report.json` 的 job 块
  只有 status/页数/耗时，MD 头部只有"页面覆盖"计数，而**失败页两个计数器都
  不覆盖**（它们的 `structured_json` 是带 `_parse_error` 的占位对象，既不是空
  也不是 NULL）⇒ 一份"0 条 finding + 全部页失败"的报告会渲染成
  "✅ 未发现问题，无需人工复核"。

**只修 #148 会更糟**：报告开始展示 `failed_pages`，而它可能还是上一轮的 ——
把"陈旧数据"升级成"看起来可信的错误陈述"。所以两条一起做，并分别钉住。

## 不得读成已通过的部分

- 本文件**不**验证 Stage 2 真的把失败页写进 `jobs.failed_pages`（那是
  `stage3._run_stage3_cross_analysis` 的终态 UPDATE，已有用例覆盖）；
- `reset_run_state()` 里的 `SELECT` 与 `_RESET_STATE_COLUMNS` 一致性靠
  **行为**断言（四列必须真的变 NULL），不靠文本比对；
- 两处清空实现（`retry_job` 与 `reset_run_state`）的列集合一致，靠的是
  `TestRetryClearsFailureState` 与 `TestResetRunStateHelper` 各断言一遍，
  **没有**机检 SQL 文本。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

STALE_COLUMNS = ("failed_pages", "stage1_ms", "stage2_ms", "stage3_ms")


@pytest.fixture(autouse=True)
def _clear_report_cache():
    """报告缓存是模块级 dict —— 用例之间必须互不污染。"""
    from api import report as report_mod

    report_mod._report_cache.clear()
    yield
    report_mod._report_cache.clear()


@pytest_asyncio.fixture
async def client(test_db):
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def _seed_job(
    test_db,
    tmp_path,
    job_id: str,
    *,
    status: str = "error",
    failed_pages: str | None = "[2, 5]",
    stage: tuple = (100, 200, 300),
    error_message: str | None = "OCR 失败：磁盘空间不足",
    total_pages: int = 5,
) -> None:
    """造一个带「上一轮失败痕迹」的 job（PDF 必须真实存在，retry 会校验）。"""
    import fitz

    doc = fitz.open()
    doc.new_page()
    pdf_path = tmp_path / f"{job_id}.pdf"
    doc.save(str(pdf_path))
    doc.close()

    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages,"
        " failed_pages, stage1_ms, stage2_ms, stage3_ms, error_message)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, f"{job_id}.pdf", str(pdf_path), status, total_pages,
         failed_pages, stage[0], stage[1], stage[2], error_message),
    )
    await test_db.commit()


async def _job_row(test_db, job_id: str) -> dict:
    cur = await test_db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    assert row is not None
    return dict(row)


# ─────────────────────────────────────────────────────────────────────────────
# #147 · 重试清空上一轮失败状态
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryClearsFailureState:
    @pytest.mark.asyncio
    async def test_retry_nulls_failed_pages_and_stage_timings(
        self, client, test_db, tmp_path
    ):
        """四列全清 —— 少清一列就够让界面继续展示上一轮的值。"""
        await _seed_job(test_db, tmp_path, "r123-retry")
        with patch("api.jobs.launch_pipeline"):
            r = await client.post("/api/jobs/r123-retry/retry")
        assert r.status_code == 200, r.text

        row = await _job_row(test_db, "r123-retry")
        for col in STALE_COLUMNS:
            assert row[col] is None, f"{col} 未被清空：{row[col]!r}"
        # error_message / finished_at 是原有行为，别在改动里丢掉
        assert row["error_message"] is None
        assert row["finished_at"] is None

    @pytest.mark.asyncio
    async def test_retry_keeps_physical_page_count(self, client, test_db, tmp_path):
        """`total_pages` 是物理页数，与"这一轮跑成什么样"无关 —— 不得一起清。"""
        await _seed_job(test_db, tmp_path, "r123-total", total_pages=7)
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/r123-total/retry")

        row = await _job_row(test_db, "r123-total")
        assert row["total_pages"] == 7

    @pytest.mark.asyncio
    async def test_retry_audits_the_cleared_values(self, client, test_db, tmp_path):
        """旧值必须留痕 —— GMP：清掉的东西要能回答"清之前是什么"。"""
        await _seed_job(test_db, tmp_path, "r123-audit")
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/r123-audit/retry")

        cur = await test_db.execute(
            "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'retry_reset'",
            ("r123-audit",),
        )
        rows = await cur.fetchall()
        assert len(rows) == 1, "retry_reset 应当且只当写一条"
        detail = rows[0]["detail"]
        # 逐列都要出现：只记一句"已清空"等于没记
        assert "failed_pages=[2, 5]" in detail, detail
        assert "stage1_ms=100" in detail, detail
        assert "stage2_ms=200" in detail, detail
        assert "stage3_ms=300" in detail, detail

    @pytest.mark.asyncio
    async def test_clean_retry_writes_no_reset_audit(self, client, test_db, tmp_path):
        """反向对照：本来就没有失败痕迹时不得插空行（审计噪声会淹没真事件）。"""
        await _seed_job(
            test_db, tmp_path, "r123-clean",
            failed_pages=None, stage=(None, None, None), error_message=None,
        )
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/r123-clean/retry")

        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM audit_log "
            "WHERE job_id = ? AND action = 'retry_reset'",
            ("r123-clean",),
        )
        assert (await cur.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_ui_no_longer_shows_previous_failed_pages(
        self, client, test_db, tmp_path
    ):
        """端到端判据：重试后状态接口不得再报上一轮的失败页。

        这才是缺陷本身（界面/报告读到旧值），前四条只是它的实现细节。
        """
        await _seed_job(test_db, tmp_path, "r123-status")
        with patch("api.jobs.launch_pipeline"):
            await client.post("/api/jobs/r123-status/retry")

        r = await client.get("/api/jobs/r123-status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["failed_pages"] is None, body["failed_pages"]
        assert body["stage1_ms"] is None
        assert body["stage2_ms"] is None


# ─────────────────────────────────────────────────────────────────────────────
# #147 · pipeline 启动兜底（直接 launch_pipeline 的路径）
# ─────────────────────────────────────────────────────────────────────────────


class TestResetRunStateHelper:
    """`reset_run_state()` 是 `_run_pipeline_impl` 用的兜底实现。

    单独测它，是因为 `_run_pipeline_impl` 在测试里一律被 mock 掉（它要真跑
    OCR + LLM）—— 只断言"retry 端点清了"会漏掉这条路径（"测了一半"）。
    """

    @pytest.mark.asyncio
    async def test_helper_nulls_all_columns_and_returns_old_values(
        self, test_db, tmp_path
    ):
        from core.pipeline.engine import reset_run_state

        await _seed_job(test_db, tmp_path, "r123-helper")
        stale = await reset_run_state(test_db, "r123-helper")
        await test_db.commit()

        assert stale == {
            "failed_pages": "[2, 5]",
            "stage1_ms": 100,
            "stage2_ms": 200,
            "stage3_ms": 300,
        }, stale
        row = await _job_row(test_db, "r123-helper")
        for col in STALE_COLUMNS:
            assert row[col] is None, f"{col} 未被清空：{row[col]!r}"
        assert row["error_message"] is None

    @pytest.mark.asyncio
    async def test_helper_on_clean_job_returns_empty(self, test_db, tmp_path):
        from core.pipeline.engine import reset_run_state

        await _seed_job(
            test_db, tmp_path, "r123-helper-clean",
            failed_pages=None, stage=(None, None, None), error_message=None,
        )
        assert await reset_run_state(test_db, "r123-helper-clean") == {}

    @pytest.mark.asyncio
    async def test_helper_does_not_commit(self, test_db, tmp_path):
        """事务边界归调用方（retry 要与归档/删除同一次提交）——
        helper 自己 commit 会让"清空"提前落盘，中途失败就留下半套状态。"""
        from core.pipeline.engine import reset_run_state

        await _seed_job(test_db, tmp_path, "r123-nocommit")
        await reset_run_state(test_db, "r123-nocommit")
        await test_db.rollback()

        row = await _job_row(test_db, "r123-nocommit")
        assert row["failed_pages"] == "[2, 5]", "helper 自己 commit 了"

    def test_detail_text_names_every_column(self):
        """审计文案要能逐列重建 —— 列集合变了这里必须一起改。"""
        from core.pipeline.engine import format_reset_detail

        detail = format_reset_detail({
            "failed_pages": "[3]",
            "stage1_ms": 1,
            "stage2_ms": 2,
            "stage3_ms": 3,
        })
        for col in STALE_COLUMNS:
            assert col in detail, detail

    @pytest.mark.asyncio
    async def test_pipeline_impl_actually_calls_the_helper(self, test_db, tmp_path):
        """管道型用例：`_run_pipeline_impl` 必须**真的**调 `reset_run_state`。

        R119/R121 的教训：只测 helper 证明不了接线 —— 有人把调用点换回
        旧的 `UPDATE ... error_message = NULL` 时，上面那些 helper 用例
        全绿，而兜底路径已经悄悄失效。

        做法：把 helper 换成"调用即抛 `CancelledError`"的哨兵。`_run_pipeline_impl`
        对 `CancelledError` 是**重新抛出**（不像 `Exception` 那样收敛成 error），
        所以断言只需看"有没有被调用"，不必真跑 OCR/LLM。
        """
        from unittest.mock import AsyncMock

        from core.pipeline import engine as engine_mod

        await _seed_job(test_db, tmp_path, "r123-wiring", status="pending")
        sentinel = AsyncMock(side_effect=asyncio.CancelledError())
        with patch.object(engine_mod, "reset_run_state", sentinel):
            with pytest.raises(asyncio.CancelledError):
                await engine_mod._run_pipeline_impl("r123-wiring", "/tmp/x.pdf", [])
        sentinel.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# #148 · 报告携带失败信息
# ─────────────────────────────────────────────────────────────────────────────


async def _seed_report_job(test_db, job_id: str, *, failed_pages="[2]",
                           error_message="第 2 页分析失败：模型返回非 JSON",
                           status="partial_review"):
    """3 页：1 页成功 / 1 页 `_parse_error`（失败页）/ 1 页无 structured_json。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages,"
        " failed_pages, error_message, stage1_ms, stage2_ms, stage3_ms)"
        " VALUES (?, ?, ?, ?, 3, ?, ?, 10, 20, 30)",
        (job_id, f"{job_id}.pdf", f"/tmp/{job_id}.pdf", status,
         failed_pages, error_message),
    )
    await test_db.executemany(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json)"
        " VALUES (?, ?, ?, ?)",
        [
            (job_id, 1, "<p>ok</p>", json.dumps({"steps": []})),
            (job_id, 2, "<p>bad</p>", json.dumps({"_parse_error": True})),
            (job_id, 3, "<p>pending</p>", None),
        ],
    )
    await test_db.commit()


class TestReportJsonCarriesFailureInfo:
    @pytest.mark.asyncio
    async def test_job_block_has_failure_fields(self, client, test_db):
        await _seed_report_job(test_db, "r123-json")
        r = await client.get("/api/jobs/r123-json/report.json")
        assert r.status_code == 200, r.text
        job = r.json()["job"]

        assert job["failed_pages"] == [2]
        assert job["error_message"] == "第 2 页分析失败：模型返回非 JSON"
        # 3 页里只有第 1 页产出可用结果（第 2 页是 _parse_error，第 3 页没跑）
        assert job["pages_analyzed"] == 1
        assert job["total_pages"] == 3

    @pytest.mark.asyncio
    async def test_failed_pages_null_is_not_empty_list(self, client, test_db):
        """NULL = "没有失败页记录"，`[]` = "确认零失败页" —— GMP 下含义不同。

        `_parse_failed_pages` 明确不伪造 `[]`，报告层必须原样透出。
        """
        await _seed_report_job(
            test_db, "r123-json-null", failed_pages=None, error_message=None,
            status="review",
        )
        r = await client.get("/api/jobs/r123-json-null/report.json")
        assert r.json()["job"]["failed_pages"] is None

    @pytest.mark.asyncio
    async def test_pages_analyzed_excludes_parse_error_pages(self, client, test_db):
        """口径来自 `stage2._get_analyzed_pages`，不是 `COUNT(structured_json)`。

        后者会把失败页算成"已分析"（#131 的同款错误），报告与状态接口就会
        各说一套。
        """
        await _seed_report_job(test_db, "r123-json-count")
        json_job = (await client.get("/api/jobs/r123-json-count/report.json")).json()["job"]
        status_job = (await client.get("/api/jobs/r123-json-count")).json()
        assert json_job["pages_analyzed"] == status_job["pages_analyzed"] == 1


class TestReportMarkdownCarriesFailureInfo:
    @pytest.mark.asyncio
    async def test_header_has_status_and_failed_pages(self, client, test_db):
        await _seed_report_job(test_db, "r123-md")
        text = (await client.get("/api/jobs/r123-md/report.md")).text

        assert "**任务状态**" in text
        assert "partial_review" in text          # 原始值，便于机器核对
        assert "**分析失败页**" in text
        assert "第 2 页" in text
        assert "1 页分析失败" in text            # 覆盖行并入失败页

    @pytest.mark.asyncio
    async def test_header_has_error_message(self, client, test_db):
        await _seed_report_job(test_db, "r123-md-err")
        text = (await client.get("/api/jobs/r123-md-err/report.md")).text
        assert "**失败原因**" in text
        assert "模型返回非 JSON" in text

    @pytest.mark.asyncio
    async def test_zero_findings_with_failed_pages_is_not_a_clean_bill(
        self, client, test_db
    ):
        """核心判据：0 条 finding **且**有失败页 ⇒ 绝不能说"无需人工复核"。

        这是 #148 的实质：失败页原先两个计数器都不覆盖，报告会宣称合规通过。

        ⚠️ 这里刻意造**纯失败**的 job（3 页全是 `_parse_error`，
        `empty_pages` 与 `unanalyzed_pages` 都是 0）—— 否则"覆盖不完整"警告
        会由另两个计数器触发，测不出失败页这一路（变异 M11 就是这么漏掉的）。
        """
        await test_db.execute(
            "INSERT INTO jobs (id, filename, pdf_path, status, total_pages,"
            " failed_pages, error_message, stage1_ms, stage2_ms, stage3_ms)"
            " VALUES (?, ?, ?, 'error', 3, ?, ?, 10, 20, 30)",
            ("r123-md-zero", "r123-md-zero.pdf", "/tmp/r123-md-zero.pdf",
             "[1, 2, 3]", "全部分析页失败"),
        )
        await test_db.executemany(
            "INSERT INTO page_cache (job_id, page, raw_html, structured_json)"
            " VALUES (?, ?, ?, ?)",
            [("r123-md-zero", p, f"<p>p{p}</p>", json.dumps({"_parse_error": True}))
             for p in (1, 2, 3)],
        )
        await test_db.commit()

        text = (await client.get("/api/jobs/r123-md-zero/report.md")).text

        assert "无需人工复核" not in text
        assert "未发现问题" in text          # 事实：确实 0 条
        assert "3 页分析失败" in text
        assert "请人工核对 PDF 原件" in text

    @pytest.mark.asyncio
    async def test_findings_summary_states_what_is_not_covered(
        self, client, test_db
    ):
        """有 findings 时也要说明"本报告不覆盖第 N 页" —— 有结论 ≠ 覆盖完整。"""
        await _seed_report_job(test_db, "r123-md-findings")
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source,"
            " description, status) VALUES (?, 1, '参数越界', 'warning', 'rule',"
            " '温度超出', 'pending')",
            ("r123-md-findings",),
        )
        await test_db.commit()

        text = (await client.get("/api/jobs/r123-md-findings/report.md")).text
        assert "本报告不覆盖第 2 页" in text

    @pytest.mark.asyncio
    async def test_cache_key_covers_failed_pages(self, client, test_db):
        """改 `failed_pages` 而 findings 不变时，报告**必须**重新生成。

        缓存 key 只覆盖 job 元数据的一部分时，就会静默返回旧文本
        （P1-A / R84 同源缺陷）；#148 新增了两个渲染输入，必须一起进 key。
        """
        await _seed_report_job(
            test_db, "r123-md-cache", failed_pages="[2]", error_message="分析失败",
        )
        first = (await client.get("/api/jobs/r123-md-cache/report.md")).text
        assert "**分析失败页**: 第 2 页" in first

        await test_db.execute(
            "UPDATE jobs SET failed_pages = ? WHERE id = ?",
            ("[1, 3]", "r123-md-cache"),
        )
        await test_db.commit()

        second = (await client.get("/api/jobs/r123-md-cache/report.md")).text
        # 断言整行，不断言裸页码：`error_message` 里也可能出现"第 N 页"，
        # 裸页码会把"缓存没失效"和"原因文案提到过这一页"混为一谈。
        assert "**分析失败页**: 第 1、3 页" in second, "缓存返回了旧文本"
        assert "**分析失败页**: 第 2 页" not in second

    @pytest.mark.asyncio
    async def test_cache_key_covers_error_message(self, client, test_db):
        await _seed_report_job(test_db, "r123-md-cache-err", error_message="旧原因")
        assert "旧原因" in (await client.get("/api/jobs/r123-md-cache-err/report.md")).text

        await test_db.execute(
            "UPDATE jobs SET error_message = ? WHERE id = ?",
            ("新原因", "r123-md-cache-err"),
        )
        await test_db.commit()

        text = (await client.get("/api/jobs/r123-md-cache-err/report.md")).text
        assert "新原因" in text
        assert "旧原因" not in text
