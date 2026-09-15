"""抑制台账 API 集成测试（P0-2 抑制留痕）。

法规前提：抑制 ≠ 删除。被降噪规则抑制的条目必须**可查、可回退、可抽检**
（EU GMP Annex 11 §16 / 中国附录《计算机化系统》第 15/16 条；PIC/S PI 041-1
认可"经验证的异常报告"替代逐页复核，前提正是留痕可追溯）。

覆盖：
- GET  /api/jobs/{id}/suppressions            台账查询（含 page 过滤 / 计数口径）
- POST /api/jobs/{id}/suppressions/{sid}/revert  一键回退为正式 finding
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_ROW = (
    "INSERT INTO finding_suppressions "
    "(job_id, page, type, severity, description, ocr_text, source, reason, evidence) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_EVIDENCE = (
    '{"rule": "core.rules.spec_guard._triple_state", "states": ["in"], '
    '"matched": [{"name": "T2101a_压力", "spec": "<0.3 MPa", '
    '"actual": "0.16", "state": "in"}]}'
)


@pytest_asyncio.fixture
async def sup_client(test_db):
    """带抑制台账数据的客户端。"""
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("sup-job", "test.pdf", "/tmp/test.pdf", "review", 3),
    )
    await test_db.executemany(
        _ROW,
        [
            ("sup-job", 9, "param_out_of_spec", "critical",
             "T2101a 压力 0.16 MPa 超出规格范围 <0.3 MPa", "T2101a 压力 0.16 MPa",
             "llm_page", "规则层复核为合规", _EVIDENCE),
            ("sup-job", 9, "param_out_of_spec", "info",
             "温度 42.1°C 超出规格范围 40±3°C", "温度 42.1",
             "llm_page", "规则层复核为合规（命中 40±3°C）", _EVIDENCE),
            ("sup-job", 14, "param_out_of_spec", "warning",
             "压力 7.49 超差", "压力 7.49",
             "llm_page", "规则层已以 info 独立呈现同一三元组", _EVIDENCE),
        ],
    )
    await test_db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        yield client


class TestListSuppressions:
    @pytest.mark.asyncio
    async def test_lists_all_with_reason_and_evidence(self, sup_client):
        r = await sup_client.get("/api/jobs/sup-job/suppressions")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 3
        assert data["total"] == 3 and data["active"] == 3 and data["reverted"] == 0
        # 留痕的核心：每条都能回答"为什么被抑制"
        for e in data["entries"]:
            assert e["reason"].strip(), "抑制记录必须带非空理由"
            assert isinstance(e["evidence"], dict) and e["evidence"], (
                "必须带结构化证据（命中的三元组）"
            )
            assert e["reverted"] is False
            assert e["type_zh"], "呈现面需要中文类型名"

    @pytest.mark.asyncio
    async def test_filter_by_page(self, sup_client):
        r = await sup_client.get("/api/jobs/sup-job/suppressions?page=9")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        assert all(e["page"] == 9 for e in data["entries"])
        # 计数是全 job 口径，不受 page 过滤影响
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_unknown_job_404(self, sup_client):
        r = await sup_client.get("/api/jobs/nope/suppressions")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_stats_expose_suppression_counts(self, sup_client):
        r = await sup_client.get("/api/jobs/sup-job/review-stats")
        assert r.status_code == 200
        data = r.json()
        assert data["suppressed"] == 3
        assert data["suppressed_reverted"] == 0


class TestRevertSuppression:
    @pytest.mark.asyncio
    async def test_revert_creates_finding_and_marks_ledger(self, sup_client, test_db):
        cur = await test_db.execute(
            "SELECT id FROM finding_suppressions WHERE page = 9 ORDER BY id LIMIT 1"
        )
        sid = (await cur.fetchone())["id"]

        r = await sup_client.post(f"/api/jobs/sup-job/suppressions/{sid}/revert")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["page"] == 9
        fid = body["finding_id"]
        assert fid

        # 1) 正式 finding 已可裁决（pending + 带法规依据）
        cur = await test_db.execute("SELECT * FROM findings WHERE id = ?", (fid,))
        f = dict(await cur.fetchone())
        assert f["status"] == "pending"
        assert f["page"] == 9
        assert f["type"] == "param_out_of_spec"
        assert f["gmp_basis"], "回退的条目必须与原始条目同等富集（带法规依据）"

        # 2) 台账不可变：行不删除，只追加回退痕迹（审计可追溯）
        cur = await test_db.execute(
            "SELECT * FROM finding_suppressions WHERE id = ?", (sid,)
        )
        row = dict(await cur.fetchone())
        assert row["reverted_at"] is not None
        assert row["reverted_finding_id"] == fid
        assert row["reason"].strip(), "理由不得因回退被清空"

        # 3) audit_log 必须留痕
        cur = await test_db.execute(
            "SELECT * FROM audit_log WHERE job_id = ? AND action = 'suppression_reverted'",
            ("sup-job",),
        )
        logs = [dict(x) for x in await cur.fetchall()]
        assert len(logs) == 1
        assert f"suppression_id={sid}" in logs[0]["detail"]

        # 4) 计数口径随之变化
        r = await sup_client.get("/api/jobs/sup-job/suppressions")
        assert r.json()["active"] == 2 and r.json()["reverted"] == 1

    @pytest.mark.asyncio
    async def test_revert_is_not_repeatable(self, sup_client, test_db):
        cur = await test_db.execute(
            "SELECT id FROM finding_suppressions ORDER BY id LIMIT 1"
        )
        sid = (await cur.fetchone())["id"]
        assert (await sup_client.post(
            f"/api/jobs/sup-job/suppressions/{sid}/revert")).status_code == 200
        r = await sup_client.post(f"/api/jobs/sup-job/suppressions/{sid}/revert")
        assert r.status_code == 400, "重复回退必须显式拒绝，不得静默造重复 finding"

    @pytest.mark.asyncio
    async def test_revert_unknown_id_404(self, sup_client):
        r = await sup_client.post("/api/jobs/sup-job/suppressions/99999/revert")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_revert_wrong_job_404(self, sup_client, test_db):
        """跨 job 的 id 不得被本 job 回退（越权防护）。"""
        cur = await test_db.execute("SELECT id FROM finding_suppressions LIMIT 1")
        sid = (await cur.fetchone())["id"]
        r = await sup_client.post("/api/jobs/nope/suppressions/{}/revert".format(sid))
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_revert_is_idempotent_against_existing_finding(
        self, sup_client, test_db
    ):
        """若正式 finding 已存在（同 job/source/page/type/description，UNIQUE 去重），
        回退应挂到既存行上，不制造重复条目。"""
        cur = await test_db.execute(
            "SELECT * FROM finding_suppressions ORDER BY id LIMIT 1"
        )
        sup = dict(await cur.fetchone())
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, ocr_text, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            ("sup-job", sup["page"], sup["type"], sup["severity"],
             sup["source"], sup["description"], sup["ocr_text"]),
        )
        # 关键：再插一条**无关** finding，使 sqlite 的 lastrowid 指向它 ——
        # INSERT OR IGNORE 被去重忽略时 lastrowid 不更新，会返回上一次插入的
        # rowid。若实现靠 lastrowid 判断，这里就会把 reverted_finding_id 记成
        # 这条无关行的 id（审计追踪被污染）。本用例即锁定该陷阱。
        await test_db.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            ("sup-job", 1, "completeness", "info", "rule", "无关条目（lastrowid 陷阱）"),
        )
        await test_db.commit()
        cur = await test_db.execute(
            "SELECT id FROM findings WHERE job_id = ? AND page = ? AND type = ? "
            "AND description = ?",
            ("sup-job", sup["page"], sup["type"], sup["description"]),
        )
        existing_id = (await cur.fetchone())["id"]
        cur = await test_db.execute(
            "SELECT MAX(id) AS m FROM findings WHERE job_id = ?", ("sup-job",)
        )
        assert (await cur.fetchone())["m"] != existing_id, (
            "用例前提：lastrowid 指向的行必须与既存行不同，否则测不出陷阱"
        )

        r = await sup_client.post(
            f"/api/jobs/sup-job/suppressions/{sup['id']}/revert"
        )
        assert r.status_code == 200
        assert r.json()["finding_id"] == existing_id

        cur = await test_db.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE job_id = ? AND page = ? "
            "AND description = ?",
            ("sup-job", sup["page"], sup["description"]),
        )
        assert (await cur.fetchone())["n"] == 1, "不得产生重复 finding"

        # 台账里记的必须是**那个**既存行，不是 lastrowid 指向的无关行
        cur = await test_db.execute(
            "SELECT reverted_finding_id FROM finding_suppressions WHERE id = ?",
            (sup["id"],),
        )
        assert (await cur.fetchone())["reverted_finding_id"] == existing_id

    @pytest.mark.asyncio
    async def test_revert_rejects_overlong_note(self, sup_client, test_db):
        cur = await test_db.execute("SELECT id FROM finding_suppressions LIMIT 1")
        sid = (await cur.fetchone())["id"]
        r = await sup_client.post(
            f"/api/jobs/sup-job/suppressions/{sid}/revert",
            data={"reviewer_note": "x" * 2001},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_revert_tolerates_corrupt_page_cache_json(self, sup_client, test_db):
        """page_cache.structured_json 为坏 JSON 时不得阻断回退 —— 安全退化。

        坏数据只应用来判定 confidence（页面标志位），不是回退的前置条件；
        若在这里抛异常，一次 OCR 写入异常就会让整条台账**永久无法回退**。"""
        cur = await test_db.execute(
            "SELECT * FROM finding_suppressions WHERE page = 9 ORDER BY id LIMIT 1"
        )
        sup = dict(await cur.fetchone())
        await test_db.execute(
            "INSERT OR REPLACE INTO page_cache (job_id, page, structured_json) "
            "VALUES (?, ?, ?)",
            ("sup-job", sup["page"], "{不是合法 JSON"),
        )
        await test_db.commit()

        r = await sup_client.post(f"/api/jobs/sup-job/suppressions/{sup['id']}/revert")
        assert r.status_code == 200, r.text
        assert r.json()["finding_id"]

    @pytest.mark.asyncio
    async def test_revert_survives_kb_unavailable(self, sup_client, test_db,
                                                  monkeypatch):
        """知识库不可用时回退照常完成（法规依据是**可选**富集，非硬前置）。"""
        import core.kb.retriever as retriever

        def _boom(*_a, **_k):
            raise RuntimeError("kb down")

        monkeypatch.setattr(retriever, "attach_kb_refs", _boom)
        cur = await test_db.execute(
            "SELECT id FROM finding_suppressions ORDER BY id LIMIT 1"
        )
        sid = (await cur.fetchone())["id"]

        r = await sup_client.post(f"/api/jobs/sup-job/suppressions/{sid}/revert")
        assert r.status_code == 200, r.text
        fid = r.json()["finding_id"]
        assert fid
        # 依据缺失不影响条目本身可裁决
        cur = await test_db.execute("SELECT gmp_basis FROM findings WHERE id = ?", (fid,))
        assert (await cur.fetchone())["gmp_basis"], "GMP 依据来自本地映射，不依赖知识库"
