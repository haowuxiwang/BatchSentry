"""报告缓存的**输入覆盖**：缓存 key 必须覆盖报告渲染用到的**全部**输入。

对抗审查 R83 第二十三批定位：`api/report.py` 的 `cache_key` 旧形态
`(job_id, len(findings), last_id, status_hash, len(exemptions))` **不含**
`empty_pages` / `unanalyzed_pages` / `total_pages` / job 级元数据（`filename` 等）。
这些字段都参与 `report.md` 渲染（「总页数」行、「页面覆盖」行），但
**重试 / 重分析**改变它们时 `findings` 可以完全不变 ⇒ 缓存 key 不变 ⇒
`report.md` 返回**过期**内容（GMP 场景下静默携带过期的覆盖声明 / 页数，
与项目此前已修的 P1-A「报告静默携带过期内容」同源）。

本文件把「这些输入变化必须使报告刷新」钉成护栏（**逐输入判别力**：
三个用例分别只动 `unanalyzed_pages` / `total_pages` / `empty_pages`，
对应变异 `devlogs/_verify/r83c_mutation.py` 的 M2/M4/M3）。
"""
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client(test_db):
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


async def _new_job(test_db, job: str, total_pages: int) -> None:
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) VALUES (?,?,?,?,?)",
        (job, f"{job}.pdf", f"/tmp/{job}.pdf", "review", total_pages),
    )


@pytest.mark.asyncio
async def test_report_refreshes_when_unanalyzed_pages_change(client, test_db):
    """页面从「未完成分析」变为「已分析」（findings 不变）时报告必须刷新。

    复现路径（可达）：job 终态含 1 页未完成分析且 0 findings；用户 `retry`
    （status != 'review' ⇒ 不清 findings，也不重置 structured_json）⇒ Stage 2
    重新分析该页并成功 ⇒ `unanalyzed_pages` 1 → 0，findings 仍为 0。
    此时旧缓存 key 逐项相同 ⇒ 旧实现返回**旧报告**（仍声明「1 页未完成分析」）。
    """
    job = "cache-cov-job"
    await _new_job(test_db, job, 1)
    # 1 页存在但尚未完成分析（structured_json = NULL）
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, structured_json) VALUES (?,?,?)",
        (job, 1, None),
    )
    await test_db.commit()

    r1 = await client.get(f"/api/jobs/{job}/report.md")
    assert r1.status_code == 200
    assert "1 页未完成分析" in r1.text, "初始：报告应声明 1 页未完成分析"

    # 模拟 retry 后 Stage 2 成功：该页被分析（findings 不变，仍为 0）
    await test_db.execute(
        "UPDATE page_cache SET structured_json = ? WHERE job_id = ? AND page = 1",
        (json.dumps({"page": 1, "findings": []}), job),
    )
    await test_db.commit()

    r2 = await client.get(f"/api/jobs/{job}/report.md")
    assert r2.status_code == 200
    assert "未完成分析" not in r2.text, (
        "覆盖计数已变化 ⇒ 报告必须刷新；仍含旧文案说明缓存 key 漏了 unanalyzed_pages"
    )


@pytest.mark.asyncio
async def test_report_refreshes_when_total_pages_change(client, test_db):
    """job 级字段（总页数）变化时报告必须刷新（findings 完全不变）。"""
    job = "cache-totalpages-job"
    await _new_job(test_db, job, 2)
    await test_db.execute(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (job, 1, "参数越界", "critical", "rule", "温度超出", "pending"),
    )
    await test_db.commit()

    r1 = await client.get(f"/api/jobs/{job}/report.md")
    assert "**总页数**: 2" in r1.text

    # 只动 job 级字段，findings 逐字节不变
    await test_db.execute("UPDATE jobs SET total_pages = 5 WHERE id = ?", (job,))
    await test_db.commit()

    r2 = await client.get(f"/api/jobs/{job}/report.md")
    assert "**总页数**: 5" in r2.text, (
        "总页数已变化 ⇒ 报告必须刷新；仍含旧值说明缓存 key 漏了 total_pages / job 元数据"
    )


@pytest.mark.asyncio
async def test_report_refreshes_when_empty_pages_change(client, test_db):
    """`empty_pages`（OCR 内容为空）变化时报告必须刷新（findings 不变）。"""
    job = "cache-emptypages-job"
    await _new_job(test_db, job, 1)
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, structured_json) VALUES (?,?,?)",
        (job, 1, json.dumps({"_ocr_empty": True})),
    )
    await test_db.commit()

    r1 = await client.get(f"/api/jobs/{job}/report.md")
    assert "1 页 OCR 内容为空" in r1.text, "初始：报告应声明 1 页 OCR 内容为空"

    # 该页重新分析后有内容（不再 _ocr_empty）
    await test_db.execute(
        "UPDATE page_cache SET structured_json = ? WHERE job_id = ? AND page = 1",
        (json.dumps({"page": 1}), job),
    )
    await test_db.commit()

    r2 = await client.get(f"/api/jobs/{job}/report.md")
    assert "OCR 内容为空" not in r2.text, (
        "empty_pages 已变化 ⇒ 报告必须刷新；仍含旧文案说明缓存 key 漏了 empty_pages"
    )
