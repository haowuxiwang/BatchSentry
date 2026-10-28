# -*- coding: utf-8 -*-
"""F3-b 护栏：`set_page_exemption` 的「读-改-写」必须在**同一临界区**内。

缺陷（2026-10-19 复核 F3，2026-10-20 用本用例复现）
--------------------------------------------------
原实现：

    SELECT ocr_diagnostics ...            ← 锁外读
    diag = json.loads(...); diag["exemption"] = {...}
    async with db_lock:                   ← 锁内写
        UPDATE page_cache SET ocr_diagnostics = ?     ← 写回**锁外读到的那份**

中间只要有别的写者改过这一行（`self_heal._rotation_heal` 就是这样的写者，
且它**根本不用锁**），本次 UPDATE 就把对方刚写的内容**整份覆盖**掉。
典型后果：先设豁免、再走旋转自愈 ⇒ 豁免没了；反过来 ⇒ 自愈诊断没了。

为什么这个用例是**确定性**的
----------------------------
测试先占住一把锁，让 `set_page_exemption` 完成"读"后卡在锁上；此时**另一个写者**
（模拟 `_rotation_heal`，它本来就不拿锁）改这一行；然后放锁。
修好后：SELECT 在锁内 ⇒ 读到的是**改后**的行 ⇒ 两个键都在。
修好前：写回的是**改前**的副本 ⇒ `self_healed` 被抹掉。

⚠️ 同 F4：用**本用例私有**的锁 + `patch`，不 acquire 模块全局 `db_lock`
（全局锁会被绑死到本用例的 event loop，污染后续用例）。
"""
import asyncio
import json
from unittest.mock import patch

import pytest


async def _seed(test_db) -> None:
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ex-job", "ex.pdf", "/tmp/ex.pdf", "review", 1),
    )
    await test_db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, ocr_diagnostics) "
        "VALUES (?, ?, ?, ?)",
        ("ex-job", 1, "<p>x</p>", json.dumps({"source": "paddle"}, ensure_ascii=False)),
    )
    await test_db.commit()


@pytest.mark.asyncio
async def test_exemption_write_does_not_clobber_a_concurrent_heal_write(test_db):
    from api.review import set_page_exemption

    await _seed(test_db)

    lock = asyncio.Lock()
    with patch("api.review.db_lock", lock):
        await lock.acquire()
        try:
            task = asyncio.create_task(
                set_page_exemption("ex-job", 1, request=None,
                                   reason="原图手写批注已人工确认", revoke=0))
            await asyncio.sleep(0.05)
            assert not task.done(), (
                "请求已经跑完 ⇒ 窗口未复现（它没卡在锁上），本用例是空断言")

            # 模拟 self_heal._rotation_heal：**不拿锁**直接改这一行（它本来就不拿）
            await test_db.execute(
                "UPDATE page_cache SET ocr_diagnostics = ? WHERE job_id = ? AND page = ?",
                (json.dumps({"self_healed": True, "recovered": True,
                             "source": "paddle", "rotation_deg": 90}, ensure_ascii=False),
                 "ex-job", 1),
            )
            await test_db.commit()
        finally:
            lock.release()

        await task

    cursor = await test_db.execute(
        "SELECT ocr_diagnostics FROM page_cache WHERE job_id = ? AND page = ?",
        ("ex-job", 1),
    )
    diag = json.loads((await cursor.fetchone())["ocr_diagnostics"])

    assert diag.get("exemption"), (
        f"豁免没写进去（读-改-写被并发写打断？）：{diag}")
    assert diag.get("self_healed") is True, (
        "豁免写入把并发写者的 self_healed 抹掉了 ⇒ SELECT 在锁外 ⇒ "
        f"写回的是陈旧副本：{diag}")
