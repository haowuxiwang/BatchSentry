"""三引擎 OCR 输出对比 + 差异页报告（M7 / T7.3）。

对标门禁 3（`core/pipeline/dual_compare.py`，在产品主链内自动跑的双后端
对比）：本脚本是**离线**工具，把可用的 2~3 个 OCR 后端（PaddleOCR-VL /
MinerU / docling）对**同一份 PDF** 各跑一次，逐页对比并输出差异页报告，
用于人工抽查、选型评估与回归基线。

差异判定复用 `core.pipeline.dual_compare.compare_page`（单一来源）——
不在这里另造一套阈值。

可用性：
- docling：可选依赖，`core.docling_client.is_available()`（未装 → 跳过并标注）；
- paddle / mineru：需 `remote_backend_configured()`（token / api_url 完整），
  否则跳过并标注，不会因缺凭据而失败退出。

用法：
  python scripts/compare_ocr_engines.py <pdf> [--engines paddle,mineru,docling]
                                       [--max-pages N] [--json out.json]

退出码：0 = 至少两个引擎跑成且报告已产出；1 = 可用引擎 <2（无法对比）。
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 后端 → (模块, 属性)。属性在调用期 getattr 解析，便于测试 monkeypatch。
_RUNNER_TARGETS: dict[str, tuple[str, str]] = {
    "paddle": ("core.ocr_client", "run_ocr"),
    "mineru": ("core.mineru_client", "run_ocr"),
    "docling": ("core.docling_client", "run_ocr"),
}


def _resolve_runner(name: str):
    mod_name, attr = _RUNNER_TARGETS[name]
    return getattr(importlib.import_module(mod_name), attr)


def engine_available(name: str) -> tuple[bool, str]:
    """引擎在本机是否可用 → (可用, 不可用原因)。"""
    if name not in _RUNNER_TARGETS:
        return False, f"未注册的后端 {name!r}"
    if name == "docling":
        from core import docling_client
        if docling_client.is_available():
            return True, ""
        return False, "docling 可选依赖未安装（pip install docling）"
    from core.pipeline.ocr_support import remote_backend_configured
    if remote_backend_configured(name):
        return True, ""
    return False, f"{name} 凭据不完整（缺 token / api_url）"


def _select_engines(requested: list[str] | None) -> list[str]:
    if requested:
        return [e.strip().lower() for e in requested if e.strip()]
    return list(_RUNNER_TARGETS)


def run_engine(name: str, pdf_path: str) -> dict:
    """跑单个引擎 → {engine, ok, pages, elapsed_s, error}。异常不外抛。"""
    t0 = time.monotonic()
    try:
        runner = _resolve_runner(name)
        pages = runner(pdf_path) or []
        return {
            "engine": name,
            "ok": True,
            "pages": pages,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": "",
        }
    except Exception as e:  # 单引擎失败不影响其它引擎
        return {
            "engine": name,
            "ok": False,
            "pages": [],
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": f"{type(e).__name__}: {e}",
        }


def _page_text(page: dict) -> str:
    return ((page or {}).get("markdown") or {}).get("text") or ""


def compare_engines(
    results: dict[str, dict], max_pages: int | None = None
) -> dict:
    """对成功引擎做两两逐页对比。返回结构化报告（纯函数，便于单测）。"""
    from core.pipeline.dual_compare import compare_page

    ok_engines = sorted(n for n, r in results.items() if r.get("ok"))
    report: dict = {
        "engines": {},
        "pairs": {},
        "diff_pages": [],
    }
    for n in ok_engines:
        pages = results[n]["pages"]
        report["engines"][n] = {
            "pages": len(pages),
            "chars": sum(len(_page_text(p)) for p in pages),
            "elapsed_s": results[n].get("elapsed_s"),
        }

    import itertools

    for a, b in itertools.combinations(ok_engines, 2):
        pa, pb = results[a]["pages"], results[b]["pages"]
        total = max(len(pa), len(pb))
        if max_pages is not None:
            total = min(total, max_pages)
        diffs = []
        for idx in range(total):
            ta = _page_text(pa[idx]) if idx < len(pa) else ""
            tb = _page_text(pb[idx]) if idx < len(pb) else ""
            is_diff, reason = compare_page(ta, tb)
            if is_diff:
                diffs.append({"page": idx + 1, "reason": reason})
                report["diff_pages"].append(
                    {"pair": f"{a}|{b}", "page": idx + 1, "reason": reason}
                )
        report["pairs"][f"{a}|{b}"] = {
            "pages": total,
            "diff_count": len(diffs),
            "diffs": diffs,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="三引擎 OCR 输出对比（M7/T7.3）")
    ap.add_argument("pdf", help="待对比的 PDF 路径")
    ap.add_argument("--engines", default="",
                    help="逗号分隔的后端名（默认全部注册后端）")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="只对比前 N 页（默认全部）")
    ap.add_argument("--json", default="", help="报告 JSON 落盘路径")
    args = ap.parse_args(argv)

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[compare] PDF 不存在: {pdf}")
        return 1

    requested = _select_engines(args.engines.split(",") if args.engines else None)
    print(f"[compare] PDF={pdf.name}  候选后端={requested}")

    results: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for name in requested:
        ok, reason = engine_available(name)
        if not ok:
            skipped[name] = reason
            print(f"  - {name}: 跳过（{reason}）")
            continue
        print(f"  - {name}: 运行中 ...")
        r = run_engine(name, str(pdf))
        results[name] = r
        if r["ok"]:
            print(f"    → {len(r['pages'])} 页 / {r['elapsed_s']}s")
        else:
            print(f"    → 失败：{r['error']}")

    ok_names = [n for n, r in results.items() if r["ok"]]
    if len(ok_names) < 2:
        print(f"[compare] 可用引擎不足（{len(ok_names)}）—— 无法两两对比")
        if args.json:
            Path(args.json).write_text(
                json.dumps({"skipped": skipped, "engines": {}, "pairs": {}},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 1

    report = compare_engines(results, max_pages=args.max_pages)
    report["skipped"] = skipped

    print("\n=== 引擎概览 ===")
    for n, info in report["engines"].items():
        print(f"  {n:8s} pages={info['pages']} chars={info['chars']} "
              f"elapsed={info['elapsed_s']}s")
    print("\n=== 两两差异 ===")
    for pair, info in report["pairs"].items():
        print(f"  {pair:16s} pages={info['pages']} diffs={info['diff_count']}")
        for d in info["diffs"][:10]:
            print(f"      p{d['page']}: {d['reason']}")
        if info["diff_count"] > 10:
            print(f"      ... 另 {info['diff_count'] - 10} 页")
    print(f"\n[compare] 差异页合计 {len(report['diff_pages'])}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[compare] 报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
