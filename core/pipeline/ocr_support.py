"""OCR support layer: backend selection, chain, text sanitizing, page count (module refactor)"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from config import config
from core.pipeline.state import _audit_log
from core.security import redact_urls

logger = logging.getLogger(__name__)


_OCR_MISSING_MARKERS = (
    "simple_table",
    "[表格内容提取失败 — OCR 结构缺失]",
    "（此页无文本内容）",
)


def assess_ocr_page(page: dict, pdf_diag: dict | None = None) -> tuple[dict, list[str]]:
    """Return persisted diagnostics and explicit incompleteness reasons.

    This is intentionally evidence-based: it does not reject a short but
    valid form merely for having few characters. MinerU's block taxonomy is
    used when available; all backends share the marker/empty-text checks.

    pdf_diag: PDF 结构诊断（_pdf_page_diagnostics 的单页内容，整份路径
    注入）。合并进持久化 diag；低 DPI / 超常页面盒 / 旋转页作为完整性
    理由（samples table low-dpi 样本验收：不得静默标记成功）。
    """
    text = str((page.get("markdown") or {}).get("text") or "")
    diag = dict(page.get("_ocr_diagnostics") or {})
    diag.setdefault("source", page.get("_source") or "unknown")
    diag["text_chars"] = len(re.sub(r"<[^>]+>", "", text).strip())
    reasons: list[str] = []
    if any(marker in text for marker in _OCR_MISSING_MARKERS):
        reasons.append("检测到 OCR 缺失内容占位")
    if diag.get("discarded_blocks", 0):
        reasons.append(f"{diag['discarded_blocks']} 个内容块被 OCR 丢弃")
    # MinerU 纯数字页脚过滤通道可观测化：布局模型把真实数据行（手写
    # 日期/百分比实测值）误分类为 page_footer 时内容被静默丢弃。正常页
    # 页脚数 1-2 个；≥3 个视为误分类高概率，触发完整性警告供人工核对。
    if diag.get("footer_dropped", 0) >= 3:
        reasons.append(
            f"{diag['footer_dropped']} 个纯数字页脚块被过滤"
            f"（可能含被误分类的数据，请对照原图）"
        )
    if diag.get("header_footer_only"):
        reasons.append("仅识别到页眉、页脚或辅助块，未识别正文")
    if not text.strip() or diag["text_chars"] == 0:
        reasons.append("页面无可用文本")
    if pdf_diag:
        # 合并 PDF 结构诊断（媒体盒/旋转/长宽比/像素/覆盖率/字号/有效 DPI）。
        # 仅合入有值的字段，不覆盖 block 级事实。
        for k, v in (
            ("media_box_pt", pdf_diag.get("media_box_pt")),
            ("rotation", pdf_diag.get("rotation")),
            ("aspect_ratio", pdf_diag.get("aspect_ratio")),
            ("image_pixels", pdf_diag.get("image_pixels")),
            ("image_coverage", pdf_diag.get("image_coverage")),
            ("effective_dpi", pdf_diag.get("effective_dpi")),
            ("min_font_pt", pdf_diag.get("min_font_pt")),
        ):
            if v is not None:
                diag[k] = v
        low_dpi = bool(pdf_diag.get("low_dpi"))
        small_font = bool(pdf_diag.get("small_font"))
        extreme = _is_extreme_aspect(diag.get("aspect_ratio"))
        if low_dpi:
            diag["low_dpi"] = True
        if small_font:
            diag["small_font"] = True
        # 超大页面盒（扫描 DPI 标注错误）与极端长宽比同属"几何异常"，
        # 后者已单独标记 → 不重复告警（避免对已规范化的条状页误报
        # "扫描 DPI 标注可能错误"）。
        if not extreme and diag.get("media_box_pt"):
            box_long = max(diag["media_box_pt"])
            if box_long > _PDF_ABNORMAL_BOX_PT:
                reasons.append(
                    f"PDF 页面盒异常（长边 {box_long:.0f}pt > "
                    f"{_PDF_ABNORMAL_BOX_PT}pt），扫描 DPI 标注可能错误"
                )
        # O2：极端长宽比 → 超长/超宽正文可能被压缩或切碎，无法无损修复，
        # 强制人工复核（不得静默标记成功）。
        if extreme:
            diag["extreme_aspect"] = True
            reasons.append(
                f"页面长宽比 {diag['aspect_ratio']:.1f}:1 极端，"
                f"超长/超宽正文可能被压缩，请对照原图核对"
            )
        # O6：小字号（<6pt）叠加低 DPI（<150）→ 识别风险叠加，
        # 必须进入低置信度/人工复核。
        mf = diag.get("min_font_pt")
        if low_dpi and small_font:
            reasons.append(
                f"小字号（{mf:.1f}pt < {_SMALL_FONT_PT:.0f}pt）叠加低 DPI"
                f"（{pdf_diag.get('effective_dpi')} < {_LOW_DPI_THRESHOLD}），"
                f"识别风险高，需人工复核"
            )
        elif low_dpi:
            reasons.append(pdf_diag.get("low_dpi_reason") or "有效 DPI 过低")
        elif small_font:
            reasons.append(
                f"正文最小字号 {mf:.1f}pt 偏小，识别可能缺字，请抽查"
            )
    diag["integrity"] = "incomplete" if reasons else "ok"
    diag["reasons"] = reasons
    return diag, reasons
def _get_ocr_backend():
    """根据配置返回 OCR 后端的 run_ocr 函数。

    OCR_BACKEND=paddle (默认): 使用 PaddleOCR-VL
    OCR_BACKEND=mineru:        使用 MinerU 精准解析
    """
    backend = config["app"].ocr_backend.lower()
    if backend == "mineru":
        from core.mineru_client import run_ocr as mineru_run
        logger.info("[Pipeline] OCR 后端: MinerU")
        return mineru_run
    # 默认 PaddleOCR
    from core.ocr_client import run_ocr as paddle_run
    logger.info("[Pipeline] OCR 后端: PaddleOCR-VL")
    return paddle_run


def _get_ocr_chain() -> list[tuple[callable, str]]:
    # Runtime resolution — tests patch core.pipeline._get_ocr_backend.
    from core.pipeline import _get_ocr_backend as _run_get_ocr_backend

    """返回 OCR 主备链：[(run_ocr, name), ...]，首个为主后端。

    双 OCR 兜底：主后端（OCR_BACKEND 配置）之外的另一个后端若已配置
    token/api_url，则作为 failover 备选。仅当两个后端都可用时链长为 2。
    """
    backend = config["app"].ocr_backend.lower()
    primary = _run_get_ocr_backend()
    chain = [(primary, backend if backend in ("paddle", "mineru") else "paddle")]
    # 备选：未激活的后端配置完整时才加入 failover 链
    if backend == "mineru":
        paddle_cfg = config["paddle_ocr"]
        if paddle_cfg.api_url and paddle_cfg.token:
            from core.ocr_client import run_ocr as paddle_run
            chain.append((paddle_run, "paddle"))
            logger.info("[Pipeline] OCR failover 备选: PaddleOCR-VL")
    else:
        mineru_cfg = config["mineru"]
        if mineru_cfg.token:
            from core.mineru_client import run_ocr as mineru_run
            chain.append((mineru_run, "mineru"))
            logger.info("[Pipeline] OCR failover 备选: MinerU")
    return chain


def _sanitize_ocr_text(text: str) -> str:
    """清洗 OCR 原始文本（存库前），消除 MinerU/Paddle 产物噪音。

    噪音来源：
    - MinerU 表格 HTML 用字面 "\\n"（反斜杠+n）分隔单元格文本，直出时
      用户看到满屏 "\\n" 而非真实换行；
    - 每个 <td> 都带 style='text-align: center; word-wrap: break-word;'
      行内样式（对 LLM 和 OCR 文本面板都是纯噪音）；
    - img src 是长路径（imgs/img_in_image_box_xxx.jpg），截断为文件名；
    - 伪 LaTeX 残留（$\\text{...}$、{{...}}，公式检测误报），与
      cross_page_analyzer._parse_spec 的剥离规则对齐（F2）；
    - 空单元格（<td> </td>/<td>&nbsp;</td>）与标签间空白（token 浪费）；
    - PDF 控制字符；PaddleOCR-VL 路径无块级页脚过滤（页码整行，
      MinerU 已在后端过滤，此处幂等）。

    清洗后 raw_html 同时服务于 LLM 输入（page_analyzer 仍会二次剥离）
    与 review 页面 OCR 文本面板（htmlToText 展示）。
    """
    if not text:
        return text
    # F2: 伪 LaTeX / OCR 残留符号（$...$、\text/\frac 命令、花括号）——
    # 必须先于下方 \\n/\\t 字面转义，否则 \text 的 \t 会被转成制表符，
    # 子串失配导致命令剥离失效（与 cross_page_analyzer._parse_spec 对齐）。
    s = re.sub(r"\$+", "", text)
    # {2,}：排除 \\n / \\t 单字母字面转义（MinerU 合法分隔符，下方 replace 处理）
    s = re.sub(r"\\[a-zA-Z]{2,}", "", s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\n", "\n").replace("\\t", "\t")
    s = re.sub(r"""\s*style=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""\s*width=['"][^'"]*['"]""", "", s)
    s = re.sub(r"""(src=["'])[^"']*/([^/"']+)(["'>])""", r"\1\2\3", s)
    # F2: 空单元格规整（&nbsp;/空格 → 空），减少 LLM prompt token 浪费
    s = re.sub(r"<td>(?:&nbsp;|\s)*</td>", "<td></td>", s, flags=re.IGNORECASE)
    # F2: HTML 标签间空白压缩（不触碰单元格文本内容）
    s = re.sub(r">\s+<", "><", s)
    # F2: 剥离 PDF 控制字符（保留 \n \t；替换为空格防单词粘连）
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    s = re.sub(r" {2,}", " ", s)
    # F2: 页码整行过滤（"第 N 页" / "N/M" — 正文表格外的明确页码模式）
    lines = []
    for ln in s.split("\n"):
        t = ln.strip()
        if re.fullmatch(r"第\s*\d+\s*页", t) or re.fullmatch(r"\d+\s*/\s*\d+", t):
            continue
        lines.append(ln)
    s = "\n".join(lines)
    # 折叠 3+ 个连续空行为 2 个（保留段落分隔）
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _pdf_page_count(pdf_path: str) -> int | None:
    """读取 PDF 物理页数（PyMuPDF），失败时返回 None（不阻断 OCR 流程）。

    robustness-A1：用于对比 OCR 结果页数，检测"解析成功但静默缺页"。
    """
    try:
        import fitz  # PyMuPDF — 仅需页数，不渲染

        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception as e:
        logger.warning(f"PDF page count probe failed ({pdf_path}): {e}")
        return None


# ── OCR 输入规范化（Stage 0，2026-08-20；M3 尺寸鲁棒性 2026-09-10）─────
# 扫描件被错误导出为"超大页面盒"是真实缺陷（51 页实测全为 3000x4000pt
# ≈41.7x55.6in，嵌入 3000x4000px JPEG，有效 DPI 仅 72）。MinerU 按 PDF
# 坐标假设 1pt=1/72in 渲染，对畸形页面盒输出像素爆炸（×3 upscale 后
# 直踩 JPEG 65500px 硬限 / VLM 2048px 输入上界），整表降级为字面量
# simple_table stub（同页 Paddle 输出 991 字符完整表格，MinerU 仅 111
# 字符 stub）。修复策略（行业最佳实践，调研来源见 CLAUDE.md）：
#   - 规范化按"目标像素密度"统一（O3），不再只判长边 >1600pt：
#     · O3 超大盒（>_PDF_ABNORMAL_BOX_PT）→ 重渲染为 300 DPI 等效页面盒
#       （页面图像素 / 300 * 72 pt），长边 cap 4096px，灰度渲染（提交
#       服务端后按 300dpi 还原出恰好原图像素，不再二次放大）；
#     · O1 微型盒（<_PDF_SMALL_BOX_PT，栅格页）→ 放大到目标 DPI，
#       避免整页欠采样；矢量/文本微型页保留保真不重渲染；
#     · O2 极端长宽比（≥_PDF_EXTREME_ASPECT）→ 放宽长边上限并抬升
#       短边下限，避免超长/超宽页正文被压碎。
#   - 仅生成规范化的"工作副本"，原始 PDF 原件保留（GMP 追溯 +
#     review 页预览仍用原件）。
_PDF_ABNORMAL_BOX_PT = 1600  # 上界（O3）：超过视为扫描 DPI 标注错误
                             # （A3=1191pt 封顶于正常印刷幅面；1600pt=
                             # 22.2in 已是异常）
_PDF_SMALL_BOX_PT = 300      # 下界（O1）：长边低于此视为微型盒（≈1024px
                             # @300dpi≈246pt，取整到 300 留余量）→ 放大到
                             # 目标 DPI，避免整页欠采样（栅格微型页才会
                             # 重渲染，矢量/文本页保留保真）
_PDF_EXTREME_ASPECT = 4.0    # 长宽比 ≥ 此值视为极端（O2）
_NORMALIZE_TARGET_DPI = 300  # 目标有效 DPI — 服务端安全区（150-300）
_NORMALIZE_MAX_SIDE_PX = 4096  # 发送端长边上限（MinerU ×3 后 12288px
                               # 仍低于 JPEG 65500px 硬限）
_NORMALIZE_EXTREME_MAX_SIDE_PX = 8192  # 极端长宽比页的长边上限（O2）：
                                       # 放宽以保住短边细节
_NORMALIZE_MIN_SHORT_SIDE_PX = 1024    # 短边像素下限（O2）：避免超长/超宽
                                       # 页正文被压碎
_LOW_DPI_THRESHOLD = 150  # 有效 DPI 低于此值视为低质量扫描（samples
                          # 登记表 low-dpi 样本验收：不得静默标记成功）
_SMALL_FONT_PT = 6.0      # 正文最小字号低于此值 + 低 DPI → 联合标记（O6）


def _is_extreme_aspect(aspect_ratio) -> bool:
    """长宽比是否极端（O2）：max(a, 1/a) ≥ _PDF_EXTREME_ASPECT。"""
    try:
        a = float(aspect_ratio)
    except (TypeError, ValueError):
        return False
    if a <= 0:
        return False
    return max(a, 1.0 / a) >= _PDF_EXTREME_ASPECT


def _box_geometry_reason(w_pt: float, h_pt: float) -> str | None:
    """页面盒几何分类（O1/O2/O3）：返回异常原因，正常页返回 None。

    判定顺序：极端长宽比（O2）→ 超大盒（O3）→ 微型盒（O1）。落在
    [_PDF_SMALL_BOX_PT, _PDF_ABNORMAL_BOX_PT] 的正常幅面返回 None，
    原样提交以保证矢量/文本保真。
    """
    long_pt = max(w_pt, h_pt)
    short_pt = min(w_pt, h_pt)
    if long_pt <= 0 or short_pt <= 0:
        return None
    if long_pt / short_pt >= _PDF_EXTREME_ASPECT:
        return "extreme_aspect"
    if long_pt > _PDF_ABNORMAL_BOX_PT:
        return "large_box"
    if long_pt < _PDF_SMALL_BOX_PT:
        return "small_box"
    return None


def _normalize_zoom(w_pt: float, h_pt: float, reason: str) -> float:
    """按异常类别选择渲染缩放系数 zoom（O1/O2/O3）。

    - small_box（O1）：放大到目标 DPI（300/72≈4.17），页盒尺寸保持不变
      而像素密度升至可用水平 → 微型页不再整页稀疏；
    - large_box（O3）：不放大（不伪造像素），仅受 _NORMALIZE_MAX_SIDE_PX
      约束 → 输出的 300dpi 页盒即恢复真实物理尺寸；
    - extreme_aspect（O2）：放宽长边上限并在必要时抬升短边，避免超长/
      超宽页正文被压碎。
    """
    long_pt = max(w_pt, h_pt)
    short_pt = min(w_pt, h_pt)
    if long_pt <= 0:
        return 1.0
    if reason == "small_box":
        return max(1.0, min(_NORMALIZE_TARGET_DPI / 72.0, _NORMALIZE_MAX_SIDE_PX / long_pt))
    if reason == "extreme_aspect":
        cap = _NORMALIZE_EXTREME_MAX_SIDE_PX
        z = min(_NORMALIZE_TARGET_DPI / 72.0, cap / long_pt)
        if short_pt > 0:
            z = max(z, min(_NORMALIZE_MIN_SHORT_SIDE_PX / short_pt, cap / long_pt))
        return max(1.0, z)
    # large_box：不放大，仅受长边上限约束（不伪造像素）
    return min(1.0, _NORMALIZE_MAX_SIDE_PX / long_pt)


def _normalization_decision(
    w_pt: float, h_pt: float, has_raster: bool
) -> tuple[bool, str | None, float]:
    """单页 OCR 输入规范化决策 → (是否重渲染, 原因, 渲染 zoom)。

    O3：触发条件改为"按目标像素密度统一"——页面盒落在
    [_PDF_SMALL_BOX_PT, _PDF_ABNORMAL_BOX_PT] 之外（欠采样或标注错误）
    或长宽比极端（O2）才重渲染，不再只是"长边 >1600pt"。
    微型盒仅在含嵌入栅格时重渲染（has_raster）——矢量/文本微型页保留
    保真，交由后端自身按文本层光栅化。
    """
    reason = _box_geometry_reason(w_pt, h_pt)
    if reason is None:
        return False, None, 1.0
    if reason == "small_box" and not has_raster:
        return False, None, 1.0
    return True, reason, _normalize_zoom(w_pt, h_pt, reason)


def _page_image_stats(page) -> dict:
    """页内最大嵌入图像的像素 / 覆盖率 / 有效 DPI（不渲染页面）。

    取面积最大的嵌入图像作为该页"主栅格"（扫描批记录每页通常只有一张
    整页图；混排页以最大图为准）。覆盖率用于判定栅格是否主导整页。
    无图像页返回 {[0,0], 0.0, None}，text PDF 不被误判为低质量。
    """
    rect = page.rect
    page_area = max(rect.width * rect.height, 1e-6)
    bw = bh = 0
    bdw = bdh = 0.0
    for im in page.get_image_info(xrefs=True):
        wpx = int(im.get("width") or 0)
        hpx = int(im.get("height") or 0)
        if wpx <= 0 or hpx <= 0 or wpx * hpx <= bw * bh:
            continue
        bb = im.get("bbox") or (0, 0, 0, 0)
        dw = float(bb[2] - bb[0])
        dh = float(bb[3] - bb[1])
        if dw <= 0 or dh <= 0:
            continue
        bw, bh, bdw, bdh = wpx, hpx, dw, dh
    if bw <= 0:
        return {"image_pixels": [0, 0], "image_coverage": 0.0, "effective_dpi": None}
    eff = None
    if max(bdw, bdh) > 0:
        eff = round(max(bw, bh) / (max(bdw, bdh) / 72.0), 1)
    return {
        "image_pixels": [bw, bh],
        "image_coverage": round((bdw * bdh) / page_area, 3),
        "effective_dpi": eff,
    }


def _page_min_font_pt(page) -> float | None:
    """文本层最小字号（pt）；无文本层（纯扫描件）返回 None。

    纯扫描页的质量由有效 DPI 反映，字号不可得；含文本层的 PDF（含
    矢量小字标注）取所有非空白 span 的最小 size，作为 O6 小字号证据。
    """
    try:
        data = page.get_text("dict")
    except Exception:
        return None
    best: float | None = None
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if not (span.get("text") or "").strip():
                    continue
                size = span.get("size")
                if not size or size <= 0:
                    continue
                if best is None or size < best:
                    best = float(size)
    return round(best, 2) if best is not None else None


def _pdf_page_diagnostics(pdf_path: str) -> dict[int, dict]:
    """扫描 PDF 每页的结构诊断（OCR 前，fitz 读取，不渲染页面）。

    门禁 1（页级诊断可追溯，docs/OCR_GOLDEN_CORPUS.md）：每页记录
    页面盒、旋转、长宽比、页内主栅格像素/覆盖率与有效 DPI
    （= 像素长边 / 图像显示尺寸英寸），以及文本层最小字号。低 DPI /
    小字号直接影响 OCR 识别质量（服务端放大/降采样失真），作为完整性
    证据并入 assess_ocr_page。

    返回 {page_index_1based: {media_box_pt, rotation, aspect_ratio,
    image_pixels, image_coverage, effective_dpi, low_dpi[, low_dpi_reason],
    min_font_pt, small_font}}。
    无图像页 effective_dpi=None（text PDF 不误判为低质量）；
    PDF 打开/解析失败返回 {}（不阻断流程）。
    """
    import fitz  # PyMuPDF — 页面盒 + 图像元数据，不渲染

    try:
        with fitz.open(pdf_path) as doc:
            out: dict[int, dict] = {}
            for i, page in enumerate(doc, 1):
                rect = page.rect
                w_pt, h_pt = rect.width, rect.height
                stats = _page_image_stats(page)
                min_font = _page_min_font_pt(page)
                diag: dict = {
                    "media_box_pt": [round(w_pt, 1), round(h_pt, 1)],
                    "rotation": int(page.rotation or 0),
                    "aspect_ratio": round(w_pt / h_pt, 3) if h_pt else None,
                    "image_pixels": stats["image_pixels"],
                    "image_coverage": stats["image_coverage"],
                    "effective_dpi": stats["effective_dpi"],
                    "low_dpi": False,
                    "min_font_pt": min_font,
                    "small_font": bool(
                        min_font is not None and min_font < _SMALL_FONT_PT
                    ),
                }
                eff = stats["effective_dpi"]
                if eff is not None and eff < _LOW_DPI_THRESHOLD:
                    diag["low_dpi"] = True
                    diag["low_dpi_reason"] = (
                        f"有效 DPI {eff:.0f} 低于 {_LOW_DPI_THRESHOLD}，"
                        f"识别质量可能不足"
                    )
                out[i] = diag
            return out
    except Exception as e:
        logger.warning(
            f"PDF 页级诊断扫描失败（不影响主流程）: {redact_urls(str(e))[:300]}"
        )
        return {}


def _prepare_ocr_pdf(pdf_path: str, job_id: str) -> tuple[str, list[int]]:
    """OCR 提交前输入规范化。返回 (实际用于 OCR 的路径, 被规范化的页码)。

    逐页按 _normalization_decision 判定（O1 微型盒 / O2 极端长宽比 /
    O3 超大盒），命中页重新渲染进工作副本并按目标 DPI 摆放；其余页原样
    拷贝（避免无关页被重采样损失保真度）。规范化永不修改原始 PDF。
    """
    import fitz  # PyMuPDF — 页面盒检测 + 重渲染

    try:
        with fitz.open(pdf_path) as doc:
            plans: dict[int, tuple[str, float]] = {}
            for i, page in enumerate(doc, 1):
                rect = page.rect
                stats = _page_image_stats(page)
                px = stats["image_pixels"]
                needs, reason, zoom = _normalization_decision(
                    rect.width, rect.height, has_raster=bool(px[0] and px[1]),
                )
                if needs:
                    plans[i] = (reason or "unknown", zoom)
            if not plans:
                return pdf_path, []
            logger.info(
                f"[{job_id}] Input normalize: {len(plans)} page(s) need "
                f"normalization: "
                f"{[(p, r, round(z, 3)) for p, (r, z) in plans.items()]}"
                f" — re-rendering 300dpi working copy (original untouched)"
            )
            out_path = str(Path(pdf_path).with_name(f"{job_id}_normalized.pdf"))
            norm = fitz.open()
            try:
                for i, page in enumerate(doc, 1):
                    plan = plans.get(i)
                    if plan is None:
                        # 正常页原样拷贝（避免无关页被重采样损失保真度）
                        norm.insert_pdf(doc, from_page=i - 1, to_page=i - 1)
                        continue
                    # 异常页：按计划 zoom 渲染位图，按 300dpi 摆放
                    _reason, zoom = plan
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(zoom, zoom),
                        colorspace=fitz.csGRAY, alpha=False,
                    )
                    w_pt = pix.width * 72.0 / _NORMALIZE_TARGET_DPI
                    h_pt = pix.height * 72.0 / _NORMALIZE_TARGET_DPI
                    npage = norm.new_page(width=w_pt, height=h_pt)
                    # JPEG q85 嵌入（2026-08-24 e2e 实证：灰度扫描页 PNG 无损
                    # 嵌入膨胀 5-8×，51 页 3000x4000 → 224.8MB 工作副本，超过
                    # 系统 200MB 上限且拖垮上游提交；JPEG q85 对灰度扫描件
                    # OCR 识别无损，体积约 1/4。tobytes 失败（异常构建）回退
                    # pixmap 直嵌，行为退化为旧版可用路径。
                    try:
                        img_bytes = pix.tobytes("jpeg", jpg_quality=85)
                        npage.insert_image(npage.rect, stream=img_bytes)
                    except Exception:
                        npage.insert_image(npage.rect, pixmap=pix)
            except Exception:
                norm.close()
                raise
            norm.save(out_path, garbage=4, deflate=True)
            norm.close()
            logger.info(
                f"[{job_id}] Input normalize: wrote {out_path} "
                f"({len(plans)} re-rendered page(s))"
            )
    except Exception as e:
        logger.error(
            f"[{job_id}] Input normalize failed — submitting original pdf: "
            f"{redact_urls(str(e))[:300]}"
        )
        return pdf_path, []
    return out_path, sorted(plans.keys())


async def _run_ocr_with_failover(db, job_id: str, pdf_path: str, progress_cb) -> tuple[list, str, list[str]]:
    # Runtime resolution — tests patch core.pipeline.{_get_ocr_chain,_pdf_page_count}.
    from core.pipeline import (
        _get_ocr_chain as _run_get_ocr_chain,
        _pdf_page_count as _run_pdf_page_count,
    )

    """整份 OCR 主备链执行（双 OCR 兜底）。

    返回 (pages, used_backend, failures)：
    - pages: 成功的 OCR 结果，全部失败时 []
    - used_backend: 实际成功执行的后端名（"paddle"/"mineru"）
    - failures: 失败记录列表（每个元素描述一个后端的失败原因）

    失败判定：异常 / 0 页 / 严重页数缺失（缺 >10% 且 >2 页）。
    任一失败 → 切下一个后端整单重试；全部失败时 failures 非空。
    仅整份路径使用；分片路径（MinerU + OCR_SLICES>1）保持原逻辑。
    """
    from logging_config import ocr_job_id_var

    # 对抗审查 P2：取消语义 — OCR 阻塞在 to_thread 线程里无法被 await
    # 中断，注入同步探针让轮询循环在用户取消后数秒内主动中止，而不是
    # 主/备两个后端各跑满轮询超时（51 页最长数十分钟）。
    import inspect

    from core.ocr_client import OCRCancelled
    from core.pipeline.state import is_job_stopping_sync

    def _cancel_probe() -> bool:
        return is_job_stopping_sync(job_id)

    def _run_with_cancel(run_fn):
        """返回零参可调用对象（to_thread 的目标）；测试替身（无 cancel_check
        形参）保持旧签名调用。"""
        try:
            accepts = "cancel_check" in inspect.signature(run_fn).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            return lambda: run_fn(pdf_path, progress_cb, cancel_check=_cancel_probe)
        return lambda: run_fn(pdf_path, progress_cb)

    chain = _run_get_ocr_chain()
    failures: list[str] = []
    for attempt, (run_fn, name) in enumerate(chain):
        if attempt > 0:
            logger.warning(
                f"[{job_id}] OCR failover: {chain[0][1]} failed → trying {name}"
            )
            await _audit_log(
                db, job_id, "ocr_failover",
                f"from={chain[0][1]} to={name} reason={failures[-1] if failures else 'unknown'}",
            )
            # 实时可见性：切换发生时立即写 ocr_backend_used — 此前该字段
            # 在 Stage 1 全部完成后才写入，备选后端运行期间（分钟级）SSE
            # 快照里为 NULL，用户无法看到"已切换备用 OCR"。
            try:
                await db.execute(
                    "UPDATE jobs SET ocr_backend_used = ? WHERE id = ?",
                    (name, job_id),
                )
                await db.commit()
            except Exception:
                pass  # 可见性尽力而为，不阻断 failover
        _ocr_ctx_token = ocr_job_id_var.set(job_id)
        # 注意：OCRCancelled 是 RuntimeError 子类，必须先于通用
        # except Exception 放行，否则会被当作后端故障触发 failover
        try:
            pages = await asyncio.to_thread(_run_with_cancel(run_fn))
        except OCRCancelled:
            # 用户取消不是后端故障：立即终止整条 failover 链，
            # 由 stage1 的 _is_cancelled 完成正式状态迁移
            raise
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {redact_urls(str(e))[:300]}")
            logger.error(f"[{job_id}] OCR attempt failed (backend={name}): {failures[-1]}")
            continue
        finally:
            ocr_job_id_var.reset(_ocr_ctx_token)
        if not pages:
            failures.append(f"{name}: 0 pages returned")
            logger.error(f"[{job_id}] OCR attempt returned 0 pages (backend={name})")
            continue
        pdf_total = await asyncio.to_thread(_run_pdf_page_count, pdf_path)
        if pdf_total is not None and len(pages) != pdf_total:
            missing = pdf_total - len(pages)
            # 对抗审查 cr-17：阈值从 max(5, 20%) 收紧到 max(2, 10%) —
            # MinerU 服务端丢页缺陷对中小文件同样发生（丢 2-4 页
            # 时旧阈值不触发 failover，静默输出残缺页）。
            if missing > max(2, int(pdf_total * 0.1)):
                failures.append(f"{name}: page mismatch ({len(pages)}/{pdf_total})")
                logger.error(
                    f"[{job_id}] OCR page count mismatch (backend={name}): {failures[-1]}"
                )
                continue
        return pages, name, failures
    return [], "", failures
