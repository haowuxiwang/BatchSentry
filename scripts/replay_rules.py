"""规则层离线重放（M4 验证）—— 在真实 job 的 page_cache 上量测规则净效果。

为什么需要它
------------
M4 新增 R11–R17 会改变 finding 数量；计划要求"必须证明净增价值而非净增
噪音"。真实 51 页文档无法重新 OCR/LLM（成本高且不可复现），但**规则层是
纯函数**：把 job 的 `page_cache.structured_json` 离线喂给 `analyze_cross_page`
（LLM 两条支路替换为空实现），即可逐位复现规则层产出，并对比"启用/关闭
M4 新规则"的差异 —— 这是可控、可复现的净效果量测（模型化重放，非端到端）。

用法：
  python scripts/replay_rules.py                 # 自动取页数最多的 job
  python scripts/replay_rules.py --job <job_id>
  python scripts/replay_rules.py --json          # 额外打印 JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "pharma.db"

# M4 新增规则 id（用于"关闭 M4"对照）
M4_RULE_IDS = {"R11", "R12", "R13", "R14", "R15", "R16", "R17"}


def load_job(job_id: str | None) -> tuple[str, list[dict], int]:
    """从 page_cache 读某 job 的结构化页；job_id=None → 取页数最多者。"""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    if not job_id:
        row = con.execute(
            "select job_id, count(*) n from page_cache group by job_id "
            "order by n desc limit 1"
        ).fetchone()
        if not row:
            raise SystemExit("page_cache 为空")
        job_id = row["job_id"]
    rows = con.execute(
        "select page, structured_json from page_cache where job_id=? order by page",
        (job_id,),
    ).fetchall()
    pages: list[dict] = []
    for r in rows:
        try:
            data = json.loads(r["structured_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            pages.append({"page": r["page"], "data": data})
    con.close()
    return job_id, pages, len(rows)


async def run_rule_layer(pages: list[dict], m4_on: bool) -> list[dict]:
    """离线跑规则层；m4_on=False 时临时排除 R11–R17。"""
    from core.rules import analyze_cross_page

    with patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[])), \
            patch("core.rules._llm_based_check", new=AsyncMock(return_value=[])):
        if m4_on:
            return await analyze_cross_page(pages, job_id="replay")
        # 排除 M4 规则：临时包装 enabled_rule_specs
        import core.rules as cr
        from core.rules.registry import enabled_rule_specs as _orig
        with patch.object(
            cr, "enabled_rule_specs",
            new=lambda: [s for s in _orig() if s.id not in M4_RULE_IDS],
        ):
            return await analyze_cross_page(pages, job_id="replay")


def _dist(findings: list[dict]) -> dict:
    by_type = Counter(f.get("type") for f in findings)
    by_sev = Counter(f.get("severity") for f in findings)
    return {
        "total": len(findings),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_severity": dict(by_sev),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规则层离线重放（M4）")
    ap.add_argument("--job", default=None, help="job_id（默认取页数最多者）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    job_id, pages, n_rows = load_job(args.job)
    print(f"job={job_id}  page_cache 行={n_rows}  可用页={len(pages)}")

    base = _dist(asyncio.run(run_rule_layer(pages, m4_on=False)))
    full = _dist(asyncio.run(run_rule_layer(pages, m4_on=True)))

    print("\n──── 关闭 M4（R11–R17）────")
    print(f"  total={base['total']}  severity={base['by_severity']}")
    print("\n──── 启用 M4（R11–R17）────")
    print(f"  total={full['total']}  severity={full['by_severity']}")

    print("\n──── M4 净增（按类型）────")
    keys = sorted(set(base["by_type"]) | set(full["by_type"]))
    for k in keys:
        b, f = base["by_type"].get(k, 0), full["by_type"].get(k, 0)
        delta = f - b
        if delta or k in {"mass_balance", "self_review", "equipment_state",
                          "env_monitor", "doc_version", "deviation_link",
                          "alteration"}:
            print(f"  {k:<24} {b:>4} → {f:>4}  ({delta:+d})")
    print(f"\n  净增总数: {full['total'] - base['total']:+d}")
    print(f"  critical: {base['by_severity'].get('critical', 0)} → "
          f"{full['by_severity'].get('critical', 0)}")

    if args.json:
        print(json.dumps({"job_id": job_id, "pages": len(pages),
                          "before_m4": base, "after_m4": full},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
