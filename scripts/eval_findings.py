"""Finding 离线评测（M2/T2.3）—— precision / recall / F1 + 噪声 KPI。

定位
----
把"降噪靠手感"变成"可证明"。对 `scripts/synthetic_corpus.py` 的合成语料
（每例埋有已知缺陷）跑**规则层**（纯函数、零 LLM 零 OCR、逐位可复现），
与 `docs/FINDING_GROUND_TRUTH.json` 的标签比对，输出：

- 逐例 TP/FP/FN 与 missing（漏报清单）、unexpected（误报清单）
- 汇总 precision / recall / F1
- 噪声 KPI：`noise_types` 的产出条数占比（当前 = completeness 洪水）

`known_gap=true` 的用例**不计入** P/R（如 self_review 尚无规则覆盖），
但仍打印，作为 M4 的可见 TODO —— 规则落地后把 known_gap 翻成 false 即成验收。

用法：
  python scripts/eval_findings.py                # 文本报告 + 落盘 JSON
  python scripts/eval_findings.py --json         # 额外打印 JSON
  python scripts/eval_findings.py --min-f1 0.9   # 低于阈值退出码 1（可作 CI 门禁）

设计：纯离线（不碰 DB/网络/LLM）。规则层的 LLM 兜底/语义两处调用被替换为
空实现，故不产生任何外部依赖。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GT_PATH = REPO_ROOT / "docs" / "FINDING_GROUND_TRUTH.json"

sys.path.insert(0, str(REPO_ROOT))


def _load_corpus_module():
    """importlib 按路径加载 scripts/synthetic_corpus.py（scripts/ 非包）。"""
    path = Path(__file__).parent / "synthetic_corpus.py"
    spec = importlib.util.spec_from_file_location("synthetic_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["synthetic_corpus"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ── 加载与校验 ──────────────────────────────────────────────────────────────


def load_ground_truth(path: Path = GT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_cases(gt: dict, corpus: dict[str, list[dict]]) -> None:
    """金标与语料的 case_id 必须一一对应（缺一即 RuntimeError）。"""
    gt_ids = [c["case_id"] for c in gt["cases"]]
    missing = [c for c in gt_ids if c not in corpus]
    orphan = [c for c in corpus if c not in gt_ids]
    if missing or orphan:
        raise RuntimeError(
            f"金标/语料 case_id 不一致：金标缺语料={missing}，语料缺金标={orphan}"
        )


# ── 规则层执行（离线：屏蔽 LLM 两条支路）────────────────────────────────────


async def run_rules(pages: list[dict]) -> list[dict]:
    from unittest.mock import AsyncMock, patch

    from core.rules import analyze_cross_page

    with patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[])), \
            patch("core.rules._llm_based_check", new=AsyncMock(return_value=[])):
        return await analyze_cross_page(pages, job_id="eval")


# ── 打分（纯函数）──────────────────────────────────────────────────────────


def score_case(case: dict, findings: list[dict], noise_types: set[str]) -> dict:
    """单例打分：期望/允许/噪声三分，返回 TP/FP/FN 明细。"""
    expected = {(e["page"], e["type"]) for e in case.get("expect", [])}
    allowed = set(case.get("allowed_extra_types", []))

    produced_pairs = [(f.get("page"), f.get("type")) for f in findings]
    produced_set = set(produced_pairs)

    tp = sorted(expected & produced_set)
    fn = sorted(expected - produced_set)
    fp = sorted({
        p for p in produced_set
        if p not in expected and p[1] not in allowed
    })
    noise = sum(1 for _, t in produced_pairs if t in noise_types)
    return {
        "case_id": case["case_id"],
        "known_gap": bool(case.get("known_gap")),
        "n_findings": len(findings),
        "tp": [list(x) for x in tp],
        "fp": [list(x) for x in fp],
        "fn": [list(x) for x in fn],
        "noise": noise,
        "precision": _safe_div(len(tp), len(tp) + len(fp)),
        "recall": _safe_div(len(tp), len(tp) + len(fn)),
    }


def _safe_div(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def aggregate(scored: list[dict]) -> dict:
    """汇总（**排除** known_gap 用例）：micro 口径 P/R/F1。"""
    active = [s for s in scored if not s["known_gap"]]
    tp = sum(len(s["tp"]) for s in active)
    fp = sum(len(s["fp"]) for s in active)
    fn = sum(len(s["fn"]) for s in active)
    p = _safe_div(tp, tp + fp)
    r = _safe_div(tp, tp + fn)
    f1 = round(2 * p * r / (p + r), 4) if (p and r) else None
    total = sum(s["n_findings"] for s in active)
    noise = sum(s["noise"] for s in active)
    return {
        "cases_scored": len(active),
        "cases_known_gap": len(scored) - len(active),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": p, "recall": r, "f1": f1,
        "total_findings": total,
        "noise_findings": noise,
        "noise_ratio": _safe_div(noise, total),
    }


# ── 编排 ────────────────────────────────────────────────────────────────────


def evaluate() -> dict:
    gt = load_ground_truth()
    corpus = _load_corpus_module().build_cases()
    validate_cases(gt, corpus)
    noise_types = set(gt.get("noise_types", []))

    scored = []
    for case in gt["cases"]:
        findings = asyncio.run(run_rules(corpus[case["case_id"]]))
        scored.append(score_case(case, findings, noise_types))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ground_truth": str(GT_PATH.relative_to(REPO_ROOT)),
        "schema_version": gt.get("schema_version"),
        "summary": aggregate(scored),
        "cases": scored,
    }


def _print_report(report: dict) -> None:
    s = report["summary"]
    print("\n════════ Finding 离线评测（规则层）════════")
    print(f"{'case':<38}{'n':>4} {'TP':>3} {'FP':>3} {'FN':>3}  P/R")
    for c in report["cases"]:
        tag = "  [known_gap]" if c["known_gap"] else ""
        p = "—" if c["precision"] is None else f"{c['precision']:.2f}"
        r = "—" if c["recall"] is None else f"{c['recall']:.2f}"
        print(f"{c['case_id']:<38}{c['n_findings']:>4} "
              f"{len(c['tp']):>3} {len(c['fp']):>3} {len(c['fn']):>3}  {p}/{r}{tag}")
        for kind in ("fn", "fp"):
            for page, ftype in c[kind]:
                label = "漏报" if kind == "fn" else "误报"
                print(f"      {label}: p{page} {ftype}")
    print(f"\n汇总（{s['cases_scored']} 例，排除 {s['cases_known_gap']} 例 known_gap）：")
    print(f"  precision={s['precision']}  recall={s['recall']}  F1={s['f1']}")
    print(f"  TP={s['tp']} FP={s['fp']} FN={s['fn']}")
    print(f"  噪声 KPI：{s['noise_findings']}/{s['total_findings']} "
          f"条为噪声类型（占比 {s['noise_ratio']}）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Finding 离线评测（M2）")
    ap.add_argument("--json", action="store_true", help="额外打印 JSON")
    ap.add_argument("--out", default=None, help="报告落盘路径（默认 devlogs/）")
    ap.add_argument("--min-f1", type=float, default=None,
                    help="F1 低于该值 → 退出码 1（可作 CI 门禁）")
    args = ap.parse_args(argv)

    report = evaluate()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else REPO_ROOT / "devlogs" / f"eval_findings_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_report(report)
    print(f"报告: {out}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    f1 = report["summary"]["f1"]
    if args.min_f1 is not None:
        if f1 is None or f1 < args.min_f1:
            print(f"F1 门禁未达标：{f1} < {args.min_f1}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
