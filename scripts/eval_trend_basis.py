"""趋势筛查数据基础评估（M6/T6.5）—— 结论见 docs/TREND_SCREENING_EVAL.md。

对标宝软 EIOS 的"参数未超限但偏离历史区间"（OOT, Out Of Trend）需要三个
前提，本脚本用**真实库实测**回答它们能否成立，而不是拍脑袋：

  Q1 同一产品/工序是否存在**多个批次**的可比样本（历史区间的前提）？
  Q2 参数/测量列名能否作**序列主键**（自由文本的归一化程度）？
  Q3 `spec_range` 的**可机械解析率**（能否机械判定"偏离"）？

用法：
  python scripts/eval_trend_basis.py [--db data/pharma.db] [--json out.json]

退出码恒为 0（这是评估工具不是门禁）；库不存在时打印说明并退出 1。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _load(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    jobs = con.execute(
        "SELECT id, filename, status, total_pages, created_at FROM jobs "
        "ORDER BY created_at"
    ).fetchall()

    per_job_products: dict[str, Counter] = {}
    per_job_batches: dict[str, set] = {}
    param_names: Counter = Counter()
    col_names: Counter = Counter()
    param_total = spec_total = spec_numeric = 0

    for j in jobs:
        rows = con.execute(
            "SELECT page, structured_json FROM page_cache "
            "WHERE job_id = ? AND structured_json IS NOT NULL", (j["id"],)
        ).fetchall()
        if not rows:
            continue
        prod: Counter = Counter()
        batches: set = set()
        for r in rows:
            try:
                d = json.loads(r["structured_json"])
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            pi = d.get("page_info") or {}
            ident = f"{str(pi.get('title') or '').strip()}|" \
                    f"{str(pi.get('file_code') or '').strip()}"
            if ident.strip("|"):
                prod[ident] += 1
            bn = str(pi.get("batch_no") or "").strip()
            if bn:
                batches.add(bn)
            for st in d.get("steps") or []:
                if not isinstance(st, dict):
                    continue
                for p in st.get("parameters") or []:
                    nm = str((p or {}).get("name") or "").strip()
                    if not nm:
                        continue
                    param_names[nm] += 1
                    param_total += 1
                    sr = str((p or {}).get("spec_range") or "").strip()
                    if sr:
                        spec_total += 1
                        if any(ch.isdigit() for ch in sr):
                            spec_numeric += 1
                for m in st.get("measurements") or []:
                    for col in ((m or {}).get("values") or {}):
                        c = str(col).strip()
                        if c:
                            col_names[c] += 1
        if prod:
            per_job_products[j["id"]] = prod
        per_job_batches[j["id"]] = batches

    con.close()

    prod_jobs: dict[str, set] = defaultdict(set)
    for jid, prod in per_job_products.items():
        for k in prod:
            prod_jobs[k].add(jid)
    multi_ident = {k: sorted(v) for k, v in prod_jobs.items() if len(v) > 1}

    singletons = [k for k, v in param_names.items() if v == 1]
    return {
        "jobs_total": len(jobs),
        "status_counts": dict(Counter(j["status"] for j in jobs)),
        "jobs_by_filename": dict(Counter(j["filename"] for j in jobs)),
        "jobs_with_product_identity": len(per_job_products),
        "identities_total": len(prod_jobs),
        "identities_in_multiple_jobs": len(multi_ident),
        "multi_identity_sample": list(multi_ident.items())[:5],
        "batch_no_per_job": {k: len(v) for k, v in per_job_batches.items()},
        "param_occurrences": param_total,
        "param_distinct": len(param_names),
        "param_top": param_names.most_common(12),
        "param_singleton_ratio": len(singletons) / max(1, len(param_names)),
        "col_distinct": len(col_names),
        "col_top": col_names.most_common(12),
        "spec_declared": spec_total,
        "spec_numeric": spec_numeric,
        "spec_numeric_ratio": spec_numeric / max(1, spec_total),
        "spec_declared_ratio": spec_total / max(1, param_total),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="趋势筛查数据基础评估")
    ap.add_argument("--db", default="data/pharma.db")
    ap.add_argument("--json", default=None, help="把实测结果写为 JSON")
    ap.add_argument("--sample", type=int, default=5, help="打印的样例条数")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[trend-basis] 数据库不存在: {db_path}（需先跑过真实 job）")
        return 1

    m = _load(db_path)
    print(f"=== jobs: {m['jobs_total']} ===")
    print("status:", m["status_counts"])
    print("按文件名:", m["jobs_by_filename"])

    print("\n=== Q1 多批次可比性 ===")
    print(f"含产品/文件编号标识的 job: {m['jobs_with_product_identity']}")
    print(f"不同标识总数: {m['identities_total']}")
    print(f"出现在 ≥2 个 job 的标识: {m['identities_in_multiple_jobs']}")
    for k, v in m["multi_identity_sample"][:3]:
        print(f"    {k[:70]}… → {len(v)} jobs")
    print(f"各 job 识别到的批号数: {m['batch_no_per_job']}")
    print("  ⚠ 同一 PDF 被多次重跑也会让标识出现于多个 job —— 判读时须核对"
          "文件名/文件编号是否为**同一批次**，而非同一文件重跑。")

    print("\n=== Q2 参数名 / 测量列名可作主键吗 ===")
    print(f"参数出现 {m['param_occurrences']} 次，不同参数名 {m['param_distinct']}")
    print(f"不同测量列名 {m['col_distinct']}")
    print("Top 参数名:", m["param_top"])
    print("Top 测量列:", m["col_top"])
    print(f"只出现 1 次的参数名占比: {m['param_singleton_ratio']:.0%}")

    print("\n=== Q3 spec 可解析率 ===")
    print(f"声明了 spec_range 的参数: {m['spec_declared']} "
          f"({m['spec_declared_ratio']:.0%} of 参数)")
    print(f"其中含数字（可机械解析）: {m['spec_numeric']} "
          f"({m['spec_numeric_ratio']:.0%})")

    if args.json:
        Path(args.json).write_text(
            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[trend-basis] 结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
