"""OCR support layer: backend selection, chain, text sanitizing, page count (module refactor)"""
from __future__ import annotations

import asyncio
import logging
import re

from config import config
from core.pipeline.state import _audit_log
from core.security import redact_urls

logger = logging.getLogger(__name__)
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
