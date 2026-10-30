"""按人工标注集给一轮 findings 打分（R133）。

为什么单独成脚本而不是塞进单测：标注集**含真实批号与页码**，只能放 gitignored 的
`devlogs/_verify/`；而判分逻辑本身必须可评审、可单测 ⇒ 逻辑入库、数据不入库。

标注集（gold）格式见 `devlogs/_verify/gold_*.json`，要点：

- 每条 label 必须写明 `evidence`（证据来源）。`criterion: true` 才计入召回率 ——
  它表示"这条缺陷有**独立于当前系统输出**的证据"（页图核对 / OCR 原文结构 /
  用户裁定）。从旧一轮 system output 抄来的条目只能记 `criterion: false`，
  否则就是拿系统验证系统（本项目已有的 `FINDING_GROUND_TRUTH.json` 同款纪律）。
- `expect.min` / `expect.max` 是**条数区间**：GMP 场景里"漏报"和"重复报"
  都会伤害复核效率，两者都要能判红。
- `severity_max` 判"过度定性"（如把需核对写成 critical）。
- 顶层 `forbid_text` 是全文档禁用措辞（指控性文案），任一命中即失败。

用法：
    python scripts/eval_findings.py --findings devlogs/_verify/e2e_sy_r3.json \
        --gold devlogs/_verify/gold_sy.json
退出码：0 全通过；1 有判据失败；2 输入不可用（fail-closed，不把"读不到"当通过）。
"""
from __future__ import annotations

import argparse
import json
import sys

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def load_findings(path: str) -> list[dict]:
    """容忍三种外壳：e2e 结果 / {"findings": [...]} / 裸列表。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        inner = data.get("findings")
        if isinstance(inner, dict):
            inner = inner.get("findings")
        if isinstance(inner, list):
            return inner
        raise ValueError(f"{path}: 找不到 findings 列表")
    if isinstance(data, list):
        return data
    raise ValueError(f"{path}: 无法识别的结构")


def _text_of(f: dict) -> str:
    return f"{f.get('description') or ''} {f.get('ocr_text') or ''}".lower()


def _matches(f: dict, m: dict) -> bool:
    """label 的 match 条件是否命中这条 finding（不匹配 page 时为文档级）。"""
    page = m.get("page")
    if page is not None and f.get("page") != page:
        return False
    types = m.get("type_any") or []
    if types and str(f.get("type") or "") not in types:
        return False
    text = _text_of(f)
    for needle in m.get("text_all") or []:
        if str(needle).lower() not in text:
            return False
    any_of = m.get("text_any") or []
    if any_of and not any(str(n).lower() in text for n in any_of):
        return False
    for needle in m.get("text_none") or []:
        if str(needle).lower() in text:
            return False
    return True


def evaluate(findings: list[dict], gold: dict) -> dict:
    """纯函数：返回逐 label 判定 + 全局禁用措辞命中。"""
    results = []
    for label in gold.get("labels") or []:
        hit = [f for f in findings if _matches(f, label.get("match") or {})]
        exp = label.get("expect") or {}
        lo, hi = exp.get("min", 0), exp.get("max")
        problems: list[str] = []
        if len(hit) < lo:
            problems.append(f"漏报：命中 {len(hit)} < 下限 {lo}")
        if hi is not None and len(hit) > hi:
            problems.append(f"重复：命中 {len(hit)} > 上限 {hi}")
        sev_max = exp.get("severity_max")
        if sev_max and hit:
            worst = max(
                (SEVERITY_ORDER.get(str(f.get("severity") or "info"), 0) for f in hit)
            )
            if worst > SEVERITY_ORDER[sev_max]:
                problems.append(f"过度定性：最高严重度 {worst} > 允许 {SEVERITY_ORDER[sev_max]}")
        results.append({
            "id": label.get("id"),
            "criterion": bool(label.get("criterion", False)),
            "evidence": label.get("evidence"),
            "description": label.get("description"),
            "hits": len(hit),
            "expect": {"min": lo, "max": hi, "severity_max": sev_max},
            "pass": not problems,
            "problems": problems,
            "hit_samples": [
                {
                    "page": f.get("page"), "source": f.get("source"),
                    "type": f.get("type"), "severity": f.get("severity"),
                    "description": (f.get("description") or "")[:120],
                }
                for f in hit[:5]
            ],
        })

    forbid = gold.get("forbid_text") or []
    forbidden_hits = [
        {
            "page": f.get("page"), "source": f.get("source"), "type": f.get("type"),
            "word": w, "description": (f.get("description") or "")[:120],
        }
        for f in findings
        for w in forbid
        if str(w).lower() in _text_of(f)
    ]

    criterion = [r for r in results if r["criterion"]]
    passed = [r for r in criterion if r["pass"]]
    return {
        "record": gold.get("record"),
        "total_findings": len(findings),
        "labels_total": len(results),
        "criterion_labels": len(criterion),
        "criterion_passed": len(passed),
        "recall_proxy": (
            round(len(passed) / len(criterion), 4) if criterion else None
        ),
        "forbidden_hits": forbidden_hits,
        "labels": results,
        "ok": len(passed) == len(criterion) and not forbidden_hits,
    }


def _print_report(rep: dict) -> None:
    print(f"记录: {rep['record']}   findings: {rep['total_findings']}")
    print(f"判据性标签: {rep['criterion_passed']}/{rep['criterion_labels']} 通过"
          f"（recall_proxy={rep['recall_proxy']}）")
    for r in rep["labels"]:
        flag = "PASS" if r["pass"] else "FAIL"
        tag = "判据" if r["criterion"] else "回归钉"
        print(f"  [{flag}] {r['id']} ({tag}, {r['evidence']}) hits={r['hits']} "
              f"{r['description']}")
        for p in r["problems"]:
            print(f"         - {p}")
        if not r["pass"]:
            for s in r["hit_samples"]:
                print(f"         · p{s['page']}/{s['source']}/{s['type']}/"
                      f"{s['severity']}: {s['description']}")
    if rep["forbidden_hits"]:
        print(f"禁用措辞命中 {len(rep['forbidden_hits'])} 处：")
        for h in rep["forbidden_hits"][:10]:
            print(f"  · p{h['page']}/{h['source']}/「{h['word']}」: {h['description']}")
    print("OVERALL:", "pass" if rep["ok"] else "fail")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按标注集给 findings 打分")
    ap.add_argument("--findings", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    try:
        findings = load_findings(args.findings)
        with open(args.gold, encoding="utf-8") as fh:
            gold = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[FAIL] 输入不可用：{exc}", file=sys.stderr)
        return 2
    if not findings:
        print("[FAIL] findings 为空 —— 空输入不得判通过", file=sys.stderr)
        return 2
    if not gold.get("labels"):
        print("[FAIL] 标注集没有 label —— 空判据不得判通过", file=sys.stderr)
        return 2

    rep = evaluate(findings, gold)
    _print_report(rep)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print(f"报告: {args.json_out}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
