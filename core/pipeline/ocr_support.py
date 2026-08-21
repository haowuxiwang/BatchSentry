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
    if diag.get("header_footer_only"):
        reasons.append("仅识别到页眉、页脚或辅助块，未识别正文")
    if not text.strip() or diag["text_chars"] == 0:
        reasons.append("页面无可用文本")
    if pdf_diag:
        # 合并 PDF 结构诊断（媒体盒/旋转/长宽比/像素/有效 DPI）。
        # 仅合入有值的字段，不覆盖 block 级事实。
        for k, v in (
            ("media_box_pt", pdf_diag.get("media_box_pt")),
            ("rotation", pdf_diag.get("rotation")),
            ("aspect_ratio", pdf_diag.get("aspect_ratio")),
            ("image_pixels", pdf_diag.get("image_pixels")),
            ("effective_dpi", pdf_diag.get("effective_dpi")),
        ):
            if v is not None:
                diag[k] = v
        if pdf_diag.get("low_dpi"):
            diag["low_dpi"] = True
            reasons.append(pdf_diag.get("low_dpi_reason") or "有效 DPI 过低")
        if diag.get("media_box_pt"):
            box_long = max(diag["media_box_pt"])
            if box_long > _PDF_ABNORMAL_BOX_PT:
                reasons.append(
                    f"PDF 页面盒异常（长边 {box_long:.0f}pt > "
                    f"{_PDF_ABNORMAL_BOX_PT}pt），扫描 DPI 标注可能错误"
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


# ── OCR 输入规范化（Stage 0，2026-08-20）──────────────────────────────
# 扫描件被错误导出为"超大页面盒"是真实缺陷（51 页实测全为 3000x4000pt
# ≈41.7x55.6in，嵌入 3000x4000px JPEG，有效 DPI 仅 72）。MinerU 按 PDF
# 坐标假设 1pt=1/72in 渲染，对畸形页面盒输出像素爆炸（×3 upscale 后
# 直踩 JPEG 65500px 硬限 / VLM 2048px 输入上界），整表降级为字面量
# simple_table stub（同页 Paddle 输出 991 字符完整表格，MinerU 仅 111
# 字符 stub）。修复策略（行业最佳实践，调研来源见 CLAUDE.md）：
#   - 检测页面盒异常（长边 >1600pt 且远超标准尺寸）→ 重新渲染为
#     300 DPI 等效页面盒（页面图像素 / 300 * 72 pt），长边 cap 4096px，
#     以灰度渲染（VLM 优先，PDF 提交给服务端后按 300dpi 还原出
#     恰好原图像素，不再触发服务端二次放大）。
#   - 仅生成规范化的"工作副本"，原始 PDF 原件保留（GMP 追溯 +
#     review 页预览仍用原件）。
_PDF_ABNORMAL_BOX_PT = 1600  # 超过视为扫描 DPI 标注错误（A3=1191pt 封顶
                              # 于正常印刷幅面；1600pt=22.2in 已是异常）
_NORMALIZE_TARGET_DPI = 300  # 目标有效 DPI — 服务端安全区（150-300）
_NORMALIZE_MAX_SIDE_PX = 4096  # 发送端长边上限（MinerU ×3 后 12288px
                               # 仍低于 JPEG 65500px 硬限）
_LOW_DPI_THRESHOLD = 150  # 有效 DPI 低于此值视为低质量扫描（samples
                          # 登记表 low-dpi 样本验收：不得静默标记成功）


def _pdf_page_diagnostics(pdf_path: str) -> dict[int, dict]:
    """扫描 PDF 每页的结构诊断（OCR 前，fitz 读取，不渲染页面）。

    门禁 1（页级诊断可追溯，docs/OCR_GOLDEN_CORPUS.md）：每页记录
    页面盒、旋转、长宽比、页内图像物理像素与有效 DPI
    （= 像素长边 / 图像显示尺寸英寸）。低 DPI 直接影响 OCR 识别质量
    （服务端放大/降采样失真），作为完整性证据并入 assess_ocr_page。

    返回 {page_index_1based: {media_box_pt, rotation, aspect_ratio,
    image_pixels, effective_dpi, low_dpi, low_dpi_reason}}。
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
                best_px_w = best_px_h = 0
                best_disp_w = best_disp_h = 0.0
                for im in page.get_image_info(xrefs=True):
                    wpx = int(im.get("width") or 0)
                    hpx = int(im.get("height") or 0)
                    if wpx <= 0 or hpx <= 0 or wpx * hpx <= best_px_w * best_px_h:
                        continue
                    bb = im.get("bbox") or (0, 0, 0, 0)
                    dw = float(bb[2] - bb[0])
                    dh = float(bb[3] - bb[1])
                    if dw <= 0 or dh <= 0:
                        continue
                    best_px_w, best_px_h = wpx, hpx
                    best_disp_w, best_disp_h = dw, dh
                diag: dict = {
                    "media_box_pt": [round(w_pt, 1), round(h_pt, 1)],
                    "rotation": int(page.rotation or 0),
                    "aspect_ratio": round(w_pt / h_pt, 3) if h_pt else None,
                    "image_pixels": [best_px_w, best_px_h],
                    "effective_dpi": None,
                    "low_dpi": False,
                }
                if best_px_w > 0 and best_px_h > 0 and max(best_disp_w, best_disp_h) > 0:
                    eff = max(best_px_w, best_px_h) / (max(best_disp_w, best_disp_h) / 72.0)
                    diag["effective_dpi"] = round(eff, 1)
                    if eff < _LOW_DPI_THRESHOLD:
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

    页面盒长边 > _PDF_ABNORMAL_BOX_PT 的页会被重新渲染进工作副本，
    否则原文件直接返回。规范化永不修改原始 PDF。
    """
    import fitz  # PyMuPDF — 页面盒检测 + 重渲染

    try:
        with fitz.open(pdf_path) as doc:
            abnormal: list[tuple[int, fitz.Rect]] = []
            for i, page in enumerate(doc, 1):
                rect = page.rect
                if max(rect.width, rect.height) > _PDF_ABNORMAL_BOX_PT:
                    abnormal.append((i, rect))
            if not abnormal:
                return pdf_path, []
            logger.info(
                f"[{job_id}] Input normalize: {len(abnormal)} page(s) have "
                f"abnormal media box (> {_PDF_ABNORMAL_BOX_PT}pt): "
                f"{[(p, f'{r.width:.0f}x{r.height:.0f}') for p, r in abnormal]}"
                f" — re-rendering 300dpi working copy (original untouched)"
            )
            out_path = str(Path(pdf_path).with_name(f"{job_id}_normalized.pdf"))
            norm = fitz.open()
            try:
                for i, page in enumerate(doc, 1):
                    rect = page.rect
                    if max(rect.width, rect.height) <= _PDF_ABNORMAL_BOX_PT:
                        # 正常页原样拷贝（避免无关页被重采样损失保真度）
                        norm.insert_pdf(doc, from_page=i - 1, to_page=i - 1)
                        continue
                    # 畸形页：渲染为 ≤4096px 位图，按 300dpi 摆放
                    zoom = min(1.0, _NORMALIZE_MAX_SIDE_PX / max(rect.width, rect.height))
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(zoom, zoom),
                        colorspace=fitz.csGRAY, alpha=False,
                    )
                    w_pt = pix.width * 72.0 / _NORMALIZE_TARGET_DPI
                    h_pt = pix.height * 72.0 / _NORMALIZE_TARGET_DPI
                    npage = norm.new_page(width=w_pt, height=h_pt)
                    npage.insert_image(npage.rect, pixmap=pix)
            except Exception:
                norm.close()
                raise
            norm.save(out_path, garbage=4, deflate=True)
            norm.close()
            logger.info(
                f"[{job_id}] Input normalize: wrote {out_path} "
                f"({len(abnormal)} re-rendered page(s))"
            )
    except Exception as e:
        logger.error(
            f"[{job_id}] Input normalize failed — submitting original pdf: "
            f"{redact_urls(str(e))[:300]}"
        )
        return pdf_path, []
    return out_path, [p for p, _ in abnormal]


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
        _ocr_ctx_token = ocr_job_id_var.set(job_id)
        try:
            try:
                pages = await asyncio.to_thread(run_fn, pdf_path, progress_cb)
            finally:
                ocr_job_id_var.reset(_ocr_ctx_token)
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {redact_urls(str(e))[:300]}")
            logger.error(f"[{job_id}] OCR attempt failed (backend={name}): {failures[-1]}")
            continue
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
