# -*- coding: utf-8 -*-
"""F3 端到端验收：**设豁免 → 该页走一次自愈 → 报告仍列该页豁免**。

纯函数护栏（`tests/unit/test_self_heal_preserves_exemption.py`）只证明
`_self_heal_diag` 不再丢键；本文件走**真实读者** `api/report.py::_load_exemptions`
（报告与 `report.json` 的唯一取数入口），证明用户可见的结果也对了。

为什么值得单独一条：门禁 2 要求"每页 integrity=ok **或** 带人工豁免及原因"。
豁免被自愈吃掉后，报告里该页既没 integrity 也没豁免 ⇒ **门禁可被静默绕过**，
而库里其实还留着豁免（在 `prior_diagnostics` 深处）—— 只有真读一遍报告才看得出来。
"""
import json

import pytest


async def _seed(test_db) -> None:
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("e2e-ex", "e2e.pdf", "/tmp/e2e.pdf", "review", 2),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, ocr_diagnostics) "
        "VALUES (?, ?, ?, ?)",
        ("e2e-ex", 1, "<p>批记录 p1</p>", json.dumps({"source": "paddle"}, ensure_ascii=False)),
    )
    await test_db.commit()


@pytest.mark.asyncio
async def test_exemption_still_listed_by_report_after_a_self_heal(test_db):
    from api.report import _load_exemptions
    from api.review import set_page_exemption
    from core.pipeline.self_heal import _self_heal_diag

    await _seed(test_db)

    reason = "原图为手写批注页，已对照纸质批记录人工确认"
    await set_page_exemption("e2e-ex", 1, request=None, reason=reason, revoke=0)

    # 报告侧：设完就有
    listed = await _load_exemptions(test_db, "e2e-ex")
    assert [e["page"] for e in listed] == [1]
    assert listed[0]["reason"] == reason

    # ── 模拟 self_heal._rotation_heal 对这一页做的事：读旧诊断 → 重建 → 写回 ──
    cursor = await test_db.execute(
        "SELECT ocr_diagnostics FROM page_cache WHERE job_id = ? AND page = ?",
        ("e2e-ex", 1),
    )
    prior = json.loads((await cursor.fetchone())["ocr_diagnostics"])
    healed = _self_heal_diag(prior, recovered=True, content_len=1234, rotation_deg=90)
    assert healed is not None
    await test_db.execute(
        "UPDATE page_cache SET raw_html = ?, ocr_diagnostics = ? "
        "WHERE job_id = ? AND page = ?",
        ("<p>旋转后重识别的批记录 p1</p>", healed, "e2e-ex", 1),
    )
    await test_db.commit()

    # ── 报告侧：自愈之后**仍然**在（这就是用户能看见的那份）──
    listed = await _load_exemptions(test_db, "e2e-ex")
    assert [e["page"] for e in listed] == [1], (
        "自愈之后报告里该页豁免消失 ⇒ 门禁 2 被绕过（库里其实还在 prior_diagnostics 深处）")
    assert listed[0]["reason"] == reason

    # 自愈自身的证据也不能少（防止"为了保住豁免把自愈诊断写坏"）
    cursor = await test_db.execute(
        "SELECT ocr_diagnostics FROM page_cache WHERE job_id = ? AND page = ?",
        ("e2e-ex", 1),
    )
    diag = json.loads((await cursor.fetchone())["ocr_diagnostics"])
    assert diag["self_healed"] is True
    assert diag["rotation_deg"] == 90
    assert diag["prior_diagnostics"]["source"] == "paddle"
