"""CLAUDE.md 的 OCR failover 阈值声明必须等于源码（防 sediment）。

背景（2026-10-10 实测）
---------------------
`CLAUDE.md` 写 failover 触发条件为「loses **>20%/5 pages**」，而源码早已收紧为
`missing > max(2, int(pdf_total * 0.1))`（`core/pipeline/ocr_support.py`，其注释明确
记录 **cr-17** 把 `max(5, 20%)` 改成 `max(2, 10%)`）。

这与 `docs/PROJECT_PITFALLS.md` 的"文档复述代码事实却无人绑定"同源，且**危害面更大**：
CLAUDE.md 是**agent 指令文件** —— 过时阈值会误导后续 agent 对 failover 行为的判断。

判据从源码**重算**（不写死字面量），并显式禁止已被否证的旧阈值回流。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _code_threshold() -> tuple[str, str]:
    """从 ocr_support.py 解析 (最小页数, 百分比串)，如 ('2', '10%')。"""
    src = (_ROOT / "core" / "pipeline" / "ocr_support.py").read_text(encoding="utf-8")
    m = re.search(
        r"missing\s*>\s*max\((\d+),\s*int\(pdf_total\s*\*\s*([0-9.]+)\)\)", src
    )
    assert m, (
        "未从 core/pipeline/ocr_support.py 解析到 failover 阈值"
        "（`missing > max(N, int(pdf_total * F))`）—— 解析方式可能已过时"
    )
    min_pages, frac = m.group(1), m.group(2)
    pct = f"{int(round(float(frac) * 100))}%"
    return min_pages, pct


def test_claude_md_ocr_failover_threshold_matches_code():
    min_pages, pct = _code_threshold()
    doc = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert f"max({min_pages}, {pct})" in doc, (
        f"CLAUDE.md 未把 OCR failover 阈值写成 `max({min_pages}, {pct})`"
        f"（源码：missing > max({min_pages}, int(pdf_total * {pct[:-1]}%))）。"
        f"改阈值时必须同步改 agent 指令文件。"
    )


def test_claude_md_does_not_keep_the_refuted_threshold():
    """旧阈值 `max(5, 20%)` 已被 cr-17 否证 ⇒ 不得回流。"""
    doc = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "20%/5" not in doc, (
        "CLAUDE.md 仍含已被源码否证的旧 failover 阈值 `>20%/5 pages`"
        "（cr-17 已收紧为 `max(2, 10%)`）"
    )
