# -*- coding: utf-8 -*-
"""F4 护栏：审计里的状态迁移必须来自**同一临界区**内的读。

缺陷（2026-10-19 复核 F4，2026-10-20 用本用例复现）
--------------------------------------------------
`api/review.py::update_finding` 原实现：

    SELECT id, status ...        ← 锁外读 old_status
    ...
    async with db_lock:          ← 锁内写
        UPDATE findings SET status=?
        INSERT audit_log (detail="status: {old_status} → {status}")

两个并发请求对同一条 finding 提交 `confirmed`：

    都读到 old_status='pending' → 都写 'confirmed' → 审计出现**两条**
    `pending → confirmed`

终态没错，但**审计轨迹错**（GMP 场景下"谁先改的"不可判定；21 CFR 11.10(e) 要求
审计不得给出错误的历史）。修法：把 SELECT 移进 `db_lock`，与 UPDATE+INSERT 同一临界区。

⚠️ **不要**用 `UPDATE ... RETURNING status`：实测（sqlite 3.45.1）`RETURNING`
返回的是**更新后**的值，拿不到 old_status。

为什么这个用例是**确定性**的（不靠 sleep 碰运气）
------------------------------------------------
测试**先占住一把锁**，再放两个请求进来：两个请求都会完成"读"然后**卡在锁上**。
等到两个任务都确认没结束时才放锁 ⇒ 两个读**必然**都发生在任何写之前。
这是"窗口被强制打开"，不是"窗口大概会打开" —— 用例自带反空断言。

⚠️ 用**本用例私有**的锁 + `patch("api.review.db_lock", ...)`，**不要**去 acquire
模块全局那把锁：`asyncio.Lock` 一旦有等待者就绑定到当时的 event loop，而
pytest-asyncio 每个用例一个新 loop ⇒ 拿全局锁做闸门会把锁"绑死"在旧 loop 上，
后续用例再进 `async with db_lock` 会抛 `is bound to a different event loop`
（2026-10-20 实测踩到：单跑绿、组合跑红）。
"""
import asyncio
from unittest.mock import patch

import pytest


async def _seed(test_db) -> None:
    await test_db.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        ("race-job", "race.pdf", "/tmp/race.pdf", "review", 1),
    )
    await test_db.execute(
        "INSERT INTO findings (job_id, page, type, severity, source, description, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("race-job", 1, "参数越界", "critical", "rule", "温度超出上限", "pending"),
    )
    await test_db.commit()


async def _audit_details(test_db) -> list[str]:
    cursor = await test_db.execute(
        "SELECT detail FROM audit_log WHERE job_id = ? AND action = 'finding_update' "
        "ORDER BY id",
        ("race-job",),
    )
    rows = await cursor.fetchall()
    return [r["detail"] for r in rows]


@pytest.mark.asyncio
async def test_concurrent_confirm_records_only_one_pending_to_confirmed(test_db):
    """两个并发 `confirmed` ⇒ 审计里**恰好一条** `pending → confirmed`。"""
    from api.review import update_finding

    await _seed(test_db)

    lock = asyncio.Lock()
    with patch("api.review.db_lock", lock):
        # ── 强制打开窗口：先占住锁，让两个请求都停在"已读、未写"的位置 ──
        await lock.acquire()
        try:
            # ⚠️ 必须显式传 None：不传的话拿到的是 FastAPI 的 `Form(...)` 对象本身
            # （默认值不是 None），`len(val)` 会抛 TypeError ⇒ 请求根本没进窗口。
            t1 = asyncio.create_task(update_finding(
                "race-job", 1, request=None, status="confirmed",
                reviewer_note=None, corrected_text=None))
            await asyncio.sleep(0.05)
            t2 = asyncio.create_task(update_finding(
                "race-job", 1, request=None, status="confirmed",
                reviewer_note=None, corrected_text=None))
            await asyncio.sleep(0.05)

            # 反空断言：两个任务都必须**还在等锁**（= 都已越过读、都没开始写）。
            # 若这里为假，说明窗口没打开，后面的断言就没有判别力。
            assert not t1.done(), "第一个请求已经跑完 ⇒ 窗口未复现，本用例是空断言"
            assert not t2.done(), "第二个请求已经跑完 ⇒ 窗口未复现，本用例是空断言"
        finally:
            lock.release()

        await asyncio.gather(t1, t2)

    details = await _audit_details(test_db)
    assert len(details) == 2, f"两个请求应各留一条审计，实际 {len(details)}：{details}"

    n_pending = sum("pending → confirmed" in d for d in details)
    n_confirmed = sum("confirmed → confirmed" in d for d in details)
    assert n_pending == 1, (
        f"`pending → confirmed` 出现 {n_pending} 次（应为 1）⇒ "
        f"old_status 是锁外读的，两个请求用了同一个旧值：{details}")
    assert n_confirmed == 1, (
        f"`confirmed → confirmed` 出现 {n_confirmed} 次（应为 1）⇒ "
        f"第二个请求应在锁内读到 confirmed：{details}")

    # 终态仍须正确（防止"为了审计正确把状态改坏"）
    cursor = await test_db.execute(
        "SELECT status FROM findings WHERE job_id = ? AND page = 1", ("race-job",))
    assert (await cursor.fetchone())["status"] == "confirmed"
