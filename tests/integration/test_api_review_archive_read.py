"""`GET /api/jobs/{id}/archived_findings` —— 归档台账读端点（TODO 0-53）。

## 为什么单独一个文件

`findings_archive` 自 v14（0-44）起就一直在**写**（`api/jobs/actions.py` 的
review→retry 归档、`core/pipeline/stage3.py` 的 LLM 层重算），但到 Round 88
为止**没有任何读接口** —— `grep -rn findings_archive api/ core/ db/` 只出
INSERT 与 schema。GMP 追溯要求"记录可追溯"，只写不读等于台账不可查。

## 这些用例钉住什么（每条都对应一个会静默出错的点）

1. **`total` 与 `count` 是两个数**：`count` 是本页条数，`total` 是全量条数。
   混成一个 ⇒ 前端分页/「共 N 条」永远是 200（或永远是第一页的数字）。
2. **顺序是 `archive_id DESC`**：台账的价值在"最近一次重新分析归档了什么"，
   正序会让人先看到三年前的行。`findings_archive` **没有** `id` 列
   （主键叫 `archive_id`）—— 写成 `ORDER BY id` 会直接 SQL 报错。
3. **limit 上限 500**：`?limit=999999` 不许把整张台账拉进内存。
4. **不存在的 job 返回空而不是 404**：与 `/audit`、`/llm_audit` 口径一致
   （读台账不是资源定位）。
5. **非本地 Host → 403**：与同文件其他 GET 端点统一（P2-1）。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture
async def archive_client(test_db):
    """3 条归档行（不同页/状态）+ 1 条别的 job 的行（必须不被串进来）。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("arch-job", "test.pdf", "/tmp/test.pdf", "review", 6),
    )
    rows = [
        ("arch-job", 1, "param_out_of_spec", "critical", "第 1 页温度超标",
         "confirmed", "已与车间确认"),
        ("arch-job", 6, "time_reversal", "warning", "第 6 页时间倒序",
         "rejected", "OCR 误读"),
        ("arch-job", 2, "completeness", "info", "第 2 页缺复核签名",
         "pending", None),
        # 干扰行：同一批测试里另一个 job 的归档，不得出现在 arch-job 的响应里
        ("other-job", 1, "completeness", "info", "别的任务的行",
         "confirmed", "不应出现"),
    ]
    for job_id, page, ftype, sev, desc, status, note in rows:
        await test_db.execute(
            "INSERT INTO findings_archive (finding_id, job_id, page, type, severity,"
            " description, status, reviewer_note, archive_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (page * 100, job_id, page, ftype, sev, desc, status, note,
             "review_retry_full_reanalysis"),
        )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestArchivedFindings:
    @pytest.mark.asyncio
    async def test_returns_entries_and_total(self, archive_client):
        r = await archive_client.get("/api/jobs/arch-job/archived_findings")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 3, f"本 job 应 3 条，实得 {data['count']}"
        assert data["total"] == 3, f"total 应 3，实得 {data['total']}"
        descs = [e["description"] for e in data["entries"]]
        assert "别的任务的行" not in descs, "串到了别的 job 的归档行"
        # 复核结论必须原样带出（这就是归档存在的理由）
        notes = {e["description"]: e["reviewer_note"] for e in data["entries"]}
        assert notes["第 1 页温度超标"] == "已与车间确认"
        assert notes["第 2 页缺复核签名"] is None

    @pytest.mark.asyncio
    async def test_newest_archived_first(self, archive_client):
        """archive_id DESC：最后插入的那条排第一。"""
        r = await archive_client.get("/api/jobs/arch-job/archived_findings")
        entries = r.json()["entries"]
        ids = [e["archive_id"] for e in entries]
        assert ids == sorted(ids, reverse=True), f"顺序不是 archive_id DESC：{ids}"
        assert entries[0]["description"] == "第 2 页缺复核签名"

    @pytest.mark.asyncio
    async def test_limit_keeps_total_whole(self, archive_client):
        r = await archive_client.get("/api/jobs/arch-job/archived_findings?limit=1")
        data = r.json()
        assert data["count"] == 1
        assert data["total"] == 3, (
            "limit 只该切页，不该改 total —— 否则前端「共 N 条」永远是 1"
        )

    @pytest.mark.asyncio
    async def test_offset_pages_through(self, archive_client):
        first = (await archive_client.get(
            "/api/jobs/arch-job/archived_findings?limit=2&offset=0")).json()
        second = (await archive_client.get(
            "/api/jobs/arch-job/archived_findings?limit=2&offset=2")).json()
        seen = [e["archive_id"] for e in first["entries"] + second["entries"]]
        assert len(seen) == 3 and len(set(seen)) == 3, f"分页有重有漏：{seen}"

    @pytest.mark.asyncio
    async def test_limit_is_clamped(self, archive_client):
        r = await archive_client.get("/api/jobs/arch-job/archived_findings?limit=999999")
        assert r.status_code == 200, "上限要 clamp，不是 400"
        r0 = await archive_client.get("/api/jobs/arch-job/archived_findings?limit=0")
        assert r0.status_code == 200
        assert r0.json()["count"] == 1, "limit=0 应被抬到 1（不能返回 0 条假装没数据）"

    @pytest.mark.asyncio
    async def test_unknown_job_is_empty_not_404(self, archive_client):
        r = await archive_client.get("/api/jobs/no-such-job/archived_findings")
        assert r.status_code == 200
        data = r.json()
        assert data == {"entries": [], "count": 0, "total": 0}

    @pytest.mark.asyncio
    async def test_non_local_host_is_forbidden(self, test_db):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://evil.com:8000"
        ) as c:
            r = await c.get("/api/jobs/arch-job/archived_findings")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_route_is_not_shadowed_by_findings_id(self, archive_client):
        """路径名不能落进 `/findings/{finding_id}` 的 int 解析（会变 422）。

        这是**真实存在过的陷阱形态**：同文件先注册
        `GET /jobs/{job_id}/findings/{finding_id}`（`finding_id: int`），
        所以端点必须叫 `/archived_findings` 而不是 `/findings_archive`。
        本用例断言"它没有退化成 422"，防止有人后来改路径名。
        """
        r = await archive_client.get("/api/jobs/arch-job/archived_findings")
        assert r.status_code == 200, (
            f"端点被 /findings/{{finding_id}} 抢走了（{r.status_code}）—— "
            f"路径名不能以 findings/ 开头"
        )
