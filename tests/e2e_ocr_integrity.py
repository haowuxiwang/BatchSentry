"""E2E（离线确定性）：OCR 输入链完整性 —— "该页不得静默标记成功"（M3 / T3.6）。

覆盖从上传前规范化到页级完整性判定的完整离线链路（Stage 0 + 门禁 1）：

    gen_ocr_samples（O1/O2/O3/O6 合成样本）
        → _prepare_ocr_pdf        （输入规范化，产出工作副本）
        → _pdf_page_diagnostics   （工作副本页级结构诊断）
        → assess_ocr_page         （完整性判定 + 显式原因）

硬断言（"不静默成功"）：
1. 可修复缺陷（O1 微型盒 / O3 超大盒）规范化后必须 integrity=ok，且
   有效 DPI 达可用区（不得留下"静默稀疏"页）；
2. 不可无损修复的缺陷（O2 极端长宽比 / O6 小字号+低 DPI）必须
   integrity=incomplete 且带明确原因（强制人工复核）；
3. 正常页与矢量微型页不得误报（无假告警、不重渲染）。

真实 OCR 服务不可离线复现，故本脚本以合成样本覆盖"输入→判定"确定性
部分；冻结包的真实样本端到端复跑由 `e2e_run.py --rounds robust` 承担。

用法：python tests/e2e_ocr_integrity.py   （退出码 0=全通过）
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.pipeline.ocr_support import (  # noqa: E402
    _pdf_page_diagnostics,
    _prepare_ocr_pdf,
    assess_ocr_page,
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_ocr_samples", _REPO / "scripts" / "gen_ocr_samples.py")

# 样本 → {页: (期望 integrity, 必须出现的关键词|None, 说明, 期望被规范化的页)}
_CASES = {
    "o1_small_box": (
        {1: ("ok", None, "微型盒放大到目标 DPI 后健康")}, [1],
    ),
    "o2_extreme_aspect": (
        {1: ("incomplete", "长宽比", "极端长宽比不可无损修复 → 人工复核")}, [1],
    ),
    "o3_mixed_size": (
        {1: ("ok", None, "正常幅面原样保留"),
         2: ("ok", None, "超大盒重渲染恢复真实尺寸"),
         3: ("ok", None, "正常幅面原样保留")}, [2],
    ),
    "o3_normal": (
        {1: ("ok", None, "正常 A4"), 2: ("ok", None, "正常 A4 横向")}, [],
    ),
    "o6_small_font_low_dpi": (
        {1: ("incomplete", "小字号", "小字号+低 DPI 联合标记")}, [],
    ),
    "guard_small_text_only": (
        {1: ("ok", None, "矢量微型页不重渲染、无假告警")}, [],
    ),
}

# 规范化后的目标密度区（300dpi 目标，留 250-350 容差）
_MIN_HEALTHY_DPI = 250.0


def _page_dict(pno: int) -> dict:
    """模拟 stage1 传入 assess_ocr_page 的页对象（含正文，避免空文本误判）。"""
    return {
        "markdown": {"text": f"## 第 {pno} 页\n批号 1127011N250101 温度 25.0"},
        "_ocr_diagnostics": {"source": "e2e"},
        "_source": "e2e",
    }


def run(outdir=None) -> list[tuple[bool, str, str]]:
    """执行全链路检查，返回 [(通过, 用例名, 详情)]。"""
    results: list[tuple[bool, str, str]] = []
    tmp = None
    if outdir is None:
        tmp = tempfile.TemporaryDirectory()
        outdir = Path(tmp.name) / "samples"
    try:
        samples = gen.build_all(outdir)
        for name, (expect_pages, expect_normalized) in _CASES.items():
            src = samples[name]
            work, normalized = _prepare_ocr_pdf(src, f"e2e-{name}")

            results.append((
                sorted(normalized) == sorted(expect_normalized),
                f"{name} 规范化页集",
                f"normalized={normalized} 期望={expect_normalized}",
            ))

            diags = _pdf_page_diagnostics(work)
            assessed: dict[int, dict] = {}
            for pno, (want_integrity, kw, note) in expect_pages.items():
                diag, reasons = assess_ocr_page(_page_dict(pno), diags.get(pno))
                assessed[pno] = diag
                got = diag.get("integrity")
                kw_ok = kw is None or any(kw in r for r in reasons)
                results.append((
                    got == want_integrity and kw_ok,
                    f"{name} p{pno} 完整性",
                    f"integrity={got}(期望 {want_integrity}) "
                    f"kw={kw!r} reasons={reasons} — {note}",
                ))

            # 不静默稀疏：被规范化的页有效 DPI 必须进入可用区
            for pno in expect_normalized:
                d = diags.get(pno) or {}
                eff = d.get("effective_dpi")
                results.append((
                    eff is not None and eff >= _MIN_HEALTHY_DPI
                    and not d.get("low_dpi"),
                    f"{name} p{pno} 密度",
                    f"effective_dpi={eff} low_dpi={d.get('low_dpi')} "
                    f"（规范化后不得静默稀疏）",
                ))

            # 不可修复缺陷页必须落在结构化风险键上（可机检，非仅文案）
            for pno, (want_integrity, _kw, _note) in expect_pages.items():
                if want_integrity != "incomplete":
                    continue
                d = assessed.get(pno) or {}
                risk_keys = [
                    k for k in ("low_dpi", "small_font", "extreme_aspect")
                    if d.get(k) is True
                ]
                results.append((
                    d.get("integrity") == "incomplete" and bool(risk_keys),
                    f"{name} p{pno} 结构化风险信号",
                    f"integrity={d.get('integrity')} risk_keys={risk_keys}",
                ))
    finally:
        if tmp is not None:
            tmp.cleanup()
    return results


def main() -> int:
    results = run()
    passed = sum(1 for ok, _, _ in results if ok)
    failed = [(n, d) for ok, n, d in results if not ok]
    print("\n=== OCR 输入链完整性 E2E（不得静默标记成功）===")
    for ok, name, detail in results:
        print(f"  {'OK ' if ok else 'XX '} {name}: {detail}")
    print("=" * 60)
    print(f"Total: {passed} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
