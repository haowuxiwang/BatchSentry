"""进程内「进展」时钟 —— 给看门狗提供比 `jobs.last_activity_at` 更细的心跳。

## 为什么不能直接写 `jobs.last_activity_at`（TODO 0-10 / R122）

Stage 2 的多页**并发**跑在同一事件循环、**共用同一条 aiosqlite 连接**上。
在那条连接上随时 `commit()` 会把别的页**写到一半**的事务一并提交 ——
`stage2._mark_page_failed` 的注释已把这条列成已知事故形态（"页面被标失败的
同时留下半套 findings，retry 后与重分析结果重复"）。心跳是**纯可观测性**，
不值得为它冒 GMP 数据一致性的风险，所以走进程内。

## 语义：只能让 job 显得**更新**，不能更旧

`find_stalled_jobs` 取 `min(DB 静默, 进程内静默)`。由此得到两条性质：

- **重启后退化**：表是空的 ⇒ 自动只看 DB 时间戳，行为与 R122 之前一致；
- **不会掩盖真挂死**：记录只会随时间变旧，超过阈值后 `min()` 就取 DB 那一侧
  —— 不存在"残留记录让 job 永远不被判停滞"。

## 粒度

`llm/client._chat_result` 每次**发起 HTTP 调用前**打点（`chat_json` 的
`on_call_start` 参数）。"下一次调用已经开始"本身就是进展；于是合法静默的
上界从"整页 6 次调用叠加"（8640s）退回"单次调用"（1440s，
见 `page_analyzer.page_analysis_silence_bound_s`）。

表的大小以"本进程见过的 job 数"为上界（几十条），不需要淘汰。
"""

from __future__ import annotations

import time
from datetime import datetime

#: job_id -> 最近一次打点的 epoch 秒（`time.time()`）。
_LAST: dict[str, float] = {}


async def note(job_id: str) -> None:
    """打一次点（`chat_json` 的 `on_call_start` 回调）。

    `async` 只是为了能直接作为 `on_call_start` 传入（那里被 `await`）；
    本函数**不做 I/O**，也不写数据库 —— 原因见模块 docstring。

    空 `job_id` 静默忽略：离线脚本 / 单测常不传 job。
    """
    if job_id:
        _LAST[job_id] = time.time()


def silence_seconds(job_id: str, *, now: datetime | None = None) -> float | None:
    """该 job 距最近一次打点的秒数；从未打点过返回 `None`。

    `now` 与 `watchdog.find_stalled_jobs(now=...)` 同源，便于测试固定时钟。
    """
    ts = _LAST.get(job_id)
    if ts is None:
        return None
    current = now.timestamp() if now is not None else time.time()
    return max(0.0, current - ts)


def reset() -> None:
    """清空全部记录（**只给测试用**，避免跨用例串味）。"""
    _LAST.clear()
