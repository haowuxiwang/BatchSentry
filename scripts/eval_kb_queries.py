"""Knowledge-base retrieval golden-set evaluation (T5.7).

Runs ``docs/KB_GOLDEN_QUERIES.json`` through the *production* retrieval path
(``retriever.query_for`` -> ``_Index.search``, same defaults the pipeline uses
for ``attach_kb_refs``) and reports the hit rate.

A query hits when the retrieved top-k entry ids intersect its ``expect_any``
list. The expectations were fixed by hand from the article text (not
back-derived from retrieval output), so a regression in query expansion, the
multi-source filter or the BM25 index shows up as a rate drop.

Usage:
    python scripts/eval_kb_queries.py                     # default threshold .85
    python scripts/eval_kb_queries.py --fail-under 0.9
    python scripts/eval_kb_queries.py --verbose           # list every query
    python scripts/eval_kb_queries.py --topk 6            # widen the window
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_PATH = ROOT / "docs" / "KB_GOLDEN_QUERIES.json"


def load_golden(path: Path = GOLDEN_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(golden: dict, topk: int | None = None,
             source_ids: set[str] | None = None) -> dict:
    """Run every query; returns {total, hits, misses:[...], details:[...]}.

    Queries flagged ``known_gap`` in the golden set are still executed and
    counted, but reported separately so the gate can tell an accepted
    limitation apart from a regression.
    """
    from core.kb import retriever

    idx = retriever._get_index()
    details: list[dict] = []
    hits = 0
    gap_total = 0
    gap_hits = 0
    for q in golden.get("queries", []):
        f = {"type": q.get("type", ""), "description": q.get("description", "")}
        terms = retriever.query_for(f)
        kwargs = {"source_ids": source_ids} if source_ids is not None else {}
        if topk is not None:
            kwargs["topk"] = topk
        refs = idx.search(terms, **kwargs)
        got = [r["entry_id"] for r in refs]
        expect = list(q.get("expect_any", []))
        matched = [e for e in expect if e in got]
        hit = bool(matched)
        hits += int(hit)
        is_gap = bool(q.get("known_gap"))
        if is_gap:
            gap_total += 1
            gap_hits += int(hit)
        details.append({
            "id": q.get("id", ""), "type": q.get("type", ""),
            "hit": hit, "got": got, "expect_any": expect, "matched": matched,
            "known_gap": is_gap, "gap_reason": q.get("gap_reason", ""),
        })
    total = len(details)
    graded = total - gap_total
    graded_hits = hits - gap_hits
    return {
        "total": total,
        "hits": hits,
        "rate": (hits / total) if total else 0.0,
        # 排除已登记缺口后的口径（用于回归判定）
        "graded_total": graded,
        "graded_hits": graded_hits,
        "graded_rate": (graded_hits / graded) if graded else 0.0,
        "known_gaps": gap_total,
        "gap_still_missing": gap_total - gap_hits,
        "misses": [d for d in details if not d["hit"]],
        "details": details,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KB golden query evaluation")
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    ap.add_argument("--fail-under", type=float, default=0.85)
    ap.add_argument("--topk", type=int, default=None,
                    help="override retrieval window (default: pipeline top-k)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every query with its retrieved ids")
    args = ap.parse_args(argv)

    golden = load_golden(Path(args.golden))
    threshold = args.fail_under
    if golden.get("threshold") and args.fail_under == 0.85:
        threshold = float(golden["threshold"])

    if args.topk is None:
        # The golden set is defined against the production retrieval window
        # (retriever._TOPK), i.e. exactly what attach_kb_refs uses.
        from core.kb import retriever as _r
        args.topk = _r._TOPK

    res = evaluate(golden, topk=args.topk)

    for d in res["details"]:
        if args.verbose or not d["hit"]:
            mark = "HIT " if d["hit"] else ("GAP " if d["known_gap"] else "MISS")
            print(f"[kb-eval][{mark}] {d['id']} ({d['type']})")
            if not d["hit"]:
                print(f"           got       = {d['got']}")
                print(f"           expect_any= {d['expect_any']}")
                if d["gap_reason"]:
                    print(f"           known_gap : {d['gap_reason']}")

    print(f"[kb-eval] golden={Path(args.golden).name} topk={args.topk} "
          f"hit={res['hits']}/{res['total']} "
          f"rate={res['rate']:.1%} threshold={threshold:.0%} "
          f"known_gaps={res['known_gaps']}")
    if res["graded_total"]:
        print(f"[kb-eval] 排除已登记缺口: {res['graded_hits']}/{res['graded_total']} "
              f"= {res['graded_rate']:.1%}")
    ok = res["graded_total"] > 0 and res["graded_rate"] >= threshold
    print(f"[kb-eval] OVERALL: {'pass' if ok else 'fail'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
