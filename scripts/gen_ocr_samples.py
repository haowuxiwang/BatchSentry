"""OCR 尺寸/形态鲁棒性合成样本生成器（M3 / T3.5）。

`docs/OCR_GOLDEN_CORPUS.md` 登记的是受限的真实批记录；受 GMP 文件权限
限制原件不入库，因此每类缺陷另配一个**可重复、确定性**的最小合成样本，
供单测与离线预检回归（O1/O2/O3/O6 四类）。

设计原则：
- 纯 PyMuPDF 构造，无随机性 → 同一输入哈希稳定，可作最小回归基线；
- 只构造"页面盒 + 嵌入栅格 + 文本字号"三项结构特征，不含业务原文；
- 生成物写到调用方指定目录（默认 `devlogs/ocr_samples/`），不入 Git 主树。

用法：
    python scripts/gen_ocr_samples.py                 # 写入 devlogs/ocr_samples/
    python scripts/gen_ocr_samples.py --out <dir>     # 指定输出目录
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz


def _add_image(page: fitz.Page, px_w: int, px_h: int) -> None:
    """在整页铺一张 px_w×px_h 的灰度栅格（模拟 1px:1pt 的错误嵌入）。"""
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, int(px_w), int(px_h)), False)
    pix.clear_with(235)
    page.insert_image(page.rect, pixmap=pix)


def make_pdf(path, pages: list[dict]) -> Path:
    """按页规格构造 PDF。

    pages 每项：{w, h, image=(px_w, px_h)|None, text=[(x, y, str, size)]|None}
    坐标以左上为原点（fitz 约定）。
    """
    path = Path(path)
    doc = fitz.open()
    try:
        for spec in pages:
            page = doc.new_page(width=spec["w"], height=spec["h"])
            if spec.get("image"):
                _add_image(page, *spec["image"])
            for x, y, txt, size in spec.get("text") or []:
                page.insert_text(fitz.Point(x, y), txt, fontsize=size)
        doc.save(str(path))
    finally:
        doc.close()
    return path


# ── O1 微型页面盒（嵌入栅格欠采样）────────────────────────────────────
def small_box_pdf(path) -> Path:
    """200×120pt 微型页 + 同尺寸栅格 + 8pt 标签 → 放大到目标 DPI，页盒不变。"""
    return make_pdf(path, [{
        "w": 200, "h": 120, "image": (200, 120),
        "text": [(12, 60, "label 1127011N250101", 8)],
    }])


# ── O2 极端长宽比（超长条页）──────────────────────────────────────────
def extreme_aspect_pdf(path) -> Path:
    """250×2000pt（8:1）条状页 + 同尺寸栅格 → 放宽长边上限并抬升短边。"""
    return make_pdf(path, [{
        "w": 250, "h": 2000, "image": (250, 2000),
        "text": [(12, 120, "strip 1127011N250101", 8)],
    }])


# ── O3 混合尺寸（正常幅面 + 超大页面盒）───────────────────────────────
# 正常页嵌入 300dpi 栅格（A4 → 2479×3509px），与真实高质量扫描件一致：
# 诊断 effective_dpi≈300、low_dpi=False，方可作为"健康页不误报"的对照。
_A4_300DPI = (2479, 3509)
_A4L_300DPI = (3509, 2479)


def mixed_size_pdf(path) -> Path:
    """A4@300dpi（原样保留）+ 3000×4000pt（重渲染恢复真实尺寸）+ A4 横向。"""
    return make_pdf(path, [
        {"w": 595, "h": 842, "image": _A4_300DPI},
        {"w": 3000, "h": 4000, "image": (1000, 1333)},
        {"w": 842, "h": 595, "image": _A4L_300DPI},
    ])


def normal_pdf(path) -> Path:
    """全正常幅面 @300dpi → 不应产生任何规范化工作副本，且不得误报告警。"""
    return make_pdf(path, [
        {"w": 595, "h": 842, "image": _A4_300DPI},
        {"w": 842, "h": 595, "image": _A4L_300DPI},
    ])


# ── O6 小字号 + 低 DPI（联合标记）─────────────────────────────────────
def small_font_low_dpi_pdf(path) -> Path:
    """A4 页 + 72dpi 栅格（低 DPI）+ 4pt 文本层（小字号）→ 联合标记。

    几何正常（长边在带内）→ 不重渲染；质量证据全部保留给
    assess_ocr_page 强制标记 incomplete（不得静默成功）。
    """
    return make_pdf(path, [{
        "w": 595, "h": 842,
        "image": (595, 842),
        "text": [(72, 120, "batch 1127011N250101", 4)],
    }])


# ── 反例：矢量/文本微型页不重渲染（保留矢量保真）──────────────────────
def small_text_only_pdf(path) -> Path:
    """200×120pt 纯文本微型页（无栅格）→ 不应被重渲染。"""
    return make_pdf(path, [{
        "w": 200, "h": 120,
        "text": [(10, 40, "label 1127011N250101", 10)],
    }])


_BUILDERS = {
    "o1_small_box": small_box_pdf,
    "o2_extreme_aspect": extreme_aspect_pdf,
    "o3_mixed_size": mixed_size_pdf,
    "o3_normal": normal_pdf,
    "o6_small_font_low_dpi": small_font_low_dpi_pdf,
    "guard_small_text_only": small_text_only_pdf,
}


def build_all(outdir) -> dict[str, str]:
    """生成全部样本，返回 {样本名: 绝对路径}。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    return {name: str(fn(out / f"{name}.pdf")) for name, fn in _BUILDERS.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 OCR 尺寸鲁棒性合成样本")
    ap.add_argument(
        "--out", default=str(Path(__file__).resolve().parents[1] / "devlogs" / "ocr_samples"),
        help="输出目录（默认 devlogs/ocr_samples/）",
    )
    args = ap.parse_args()
    files = build_all(args.out)
    manifest = {k: Path(v).name for k, v in files.items()}
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n已写入 {len(files)} 个样本 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
