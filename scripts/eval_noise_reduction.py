"""降噪净效果离线量测（R1 / M2）—— 在真实轮次的 findings 上量"少了多少、少了什么"。

为什么需要它
------------
降噪最容易变成"凭手感变少"。本工具把"少了几条"变成**可复现、可审计**的数字：

- 输入 = 真实轮次的 `findings` + `page_cache`（与 `stage3.py` 完全相同的取数口径：
  `flagged_pages` 由 `core.finding_quality.page_is_flagged` 从 `structured_json` 判出）。
- 处理 = `reduce_self_referential_noise`（R1）+ `reduce_completeness_noise`（M2）——
  与生产同一条链路、同一批默认阈值。
- 输出 = 逐类前后对比 + 噪声占比 KPI + **未丢失证据的核对**（页清单并集必须相等）。

**这不是端到端**：它重放的是"已落库的 finding 集合"上的确定性地变换，用于量测降噪
本身；端到端产出仍以 frozen e2e 为准。

用法：
  python scripts/eval_noise_reduction.py                       # 自动取 findings 最多的 job
  python scripts/eval_noise_reduction.py --job 0c5cfb00-897
  python scripts/eval_noise_reduction.py --db <path>/data.db   # 非默认库
  python scripts/eval_noise_reduction.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DB_PATH = REPO_ROOT / "data" / "pharma.db"


def load(job_id: str | None, db_path: str | None):
    con = sqlite3.connect(str(db_path or DB_PATH))
    con.row_factory = sqlite3.Row
    if not job_id:
        row = con.execute(
            "SELECT job_id FROM findings GROUP BY job_id ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise SystemExit("库内没有 findings")
        job_id = row["job_id"]

    findings = [
        {
            "page": r["page"],
            "type": r["type"],
            "severity": r["severity"],
            "description": r["description"],
            "ocr_text": r["ocr_text"],
        }
        for r in con.execute(
            "SELECT page, type, severity, description, ocr_text FROM findings "
            "WHERE job_id=? ORDER BY id",
            (job_id,),
        )
    ]

    # 与 stage3.py 同口径：flagged_pages / total_pages 从 page_cache 复算。
    from core.finding_quality import page_is_flagged

    flagged: set[int] = set()
    total_pages = 0
    for r in con.execute(
        "SELECT page, structured_json FROM page_cache WHERE job_id=? ORDER BY page",
        (job_id,),
    ):
        total_pages += 1
        if not r["structured_json"]:
            continue
        try:
            data = json.loads(r["structured_json"])
        except json.JSONDecodeError:
            continue
        if page_is_flagged(data):
            flagged.add(r["page"])
    return job_id, findings, flagged, total_pages


def summarize(findings: list[dict]) -> dict:
    return {
        "total": len(findings),
        "by_type": dict(Counter(f["type"] for f in findings).most_common()),
        "by_severity": dict(Counter(f["severity"] for f in findings).most_common()),
        "noise_share": round(
            sum(1 for f in findings if f["type"] in ("completeness", "handwritten"))
            / max(1, len(findings)),
            4,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="降噪净效果离线量测（R1 + M2）")
    ap.add_argument("--job", default=None, help="job_id（默认取 findings 最多者）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 data/pharma.db）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    job_id, findings, flagged, total_pages = load(args.job, args.db)
    before = summarize(findings)

    from core.finding_noise import (
        reduce_completeness_noise,
        reduce_self_referential_noise,
    )

    stage1, r1_report = reduce_self_referential_noise(findings)
    after, m2_report = reduce_completeness_noise(
        stage1, flagged_pages=flagged, total_pages=total_pages
    )
    mid = summarize(stage1)
    final = summarize(after)

    print(f"job={job_id}  页数={total_pages}  带完整性告警页={len(flagged)}")
    print(f"\n──── 原始 ────\n  total={before['total']}  {before['by_severity']}"
          f"  噪声占比={before['noise_share']:.1%}")
    print(f"\n──── R1 自指元噪声聚合后 ────\n  total={mid['total']}"
          f"  (-{before['total'] - mid['total']}, "
          f"聚合 {r1_report['aggregated_total']} 条 → {len(r1_report['aggregated'])} 条摘要)"
          f"  噪声占比={mid['noise_share']:.1%}")
    print(f"\n──── 再叠加 M2 completeness 降噪后 ────\n  total={final['total']}"
          f"  (-{mid['total'] - final['total']}, "
          f"聚合 {m2_report['aggregated_total']} 条, "
          f"降级 {m2_report['downgraded_extraction_uncertain']} 条)"
          f"  噪声占比={final['noise_share']:.1%}")
    print(f"\n  净效果：{before['total']} → {final['total']}"
          f"  ({(final['total'] - before['total']) / max(1, before['total']):+.1%})")

    print(f"\n{'type':<26}{'原始':>7}{'R1后':>7}{'最终':>7}{'净变化':>8}")
    for t in sorted(set(before["by_type"]) | set(final["by_type"])):
        x, y, z = (before["by_type"].get(t, 0), mid["by_type"].get(t, 0),
                   final["by_type"].get(t, 0))
        print(f"{t:<26}{x:>7}{y:>7}{z:>7}{z - x:>+8}")

    print(f"\n{'severity':<26}{'原始':>7}{'R1后':>7}{'最终':>7}{'净变化':>8}")
    for s in ("critical", "warning", "info"):
        x, y, z = (before["by_severity"].get(s, 0), mid["by_severity"].get(s, 0),
                   final["by_severity"].get(s, 0))
        print(f"{s:<26}{x:>7}{y:>7}{z:>7}{z - x:>+8}")

    # 证据不丢失核对：原始出现过的页，降噪后仍必须"有据可查" ——
    # 要么该页仍有逐条 finding，要么它落在某个聚合摘要的 page_list 里。
    before_pages = {f["page"] for f in findings if f["page"] is not None}
    after_pages = {f["page"] for f in after if f["page"] is not None}
    summarized_pages: set[int] = set()
    for f in after:
        if f.get("aggregated"):
            summarized_pages |= set(f.get("page_list") or [])
    lost = sorted(before_pages - after_pages - summarized_pages)
    print(f"\n  页级证据覆盖：原始 {len(before_pages)} 页 / 降噪后 {len(after_pages)} 页 "
          f"/ 摘要页清单补充 {len(summarized_pages - after_pages)} 页 → "
          f"{'无丢失' if not lost else '丢失 ' + str(lost)}")

    # 硬约束自检：高价值类型与 critical 不得被降噪吃掉
    guarded = ("param_out_of_spec", "time_reversal", "batch_inconsistency",
               "signature_time_anomaly", "self_review")
    bad = [
        t for t in guarded
        if final["by_type"].get(t, 0) < before["by_type"].get(t, 0)
    ]
    crit_ok = final["by_severity"].get("critical", 0) >= before["by_severity"].get("critical", 0)
    print(f"  高价值类型未被削弱：{'OK' if not bad else 'FAIL ' + str(bad)}")
    print(f"  critical 数未下降：{'OK' if crit_ok else 'FAIL'}")

    if args.json:
        print("\n" + json.dumps(
            {
                "job_id": job_id,
                "pages": total_pages,
                "flagged_pages": sorted(flagged),
                "before": before,
                "after_r1": mid,
                "final": final,
                "r1_report": r1_report,
                "m2_report": m2_report,
                "guardrails": {"weakened_types": bad, "critical_ok": crit_ok},
            },
            ensure_ascii=False, indent=2,
        ))
    return 0 if (not bad and crit_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
