"""Gate 3 — dual-backend OCR output comparison (OCR_GOLDEN_CORPUS.md).

门禁 3：双后端输出存在关键字段差异时，任务进入人工复核而非自动通过。

主后端整份 OCR 成功后（含自愈），若 `OCR_DUAL_COMPARE` 开启且备选后端
凭据完整，用备选后端对同一规范化工作副本再跑一次，逐页对比：

- 页数不一致（任一侧多/少页）
- 单侧空页（一侧有正文另一侧无）
- 文本相似度 < 0.85（去标签去空白后 difflib 比对，截断 5000 字符防大页卡顿）
- 表格数量不一致

差异不裁决谁对谁错 —— 两个后端各有所长（MinerU 结构化强、Paddle 手写
强），差异本身就是"需要人工对照原图"的证据。差异以 source=rule /
type=completeness findings 呈现（复用现有去重、复核 UI、报告链路），
并强制任务终态为 partial_review。
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re

from config import config
from core.pipeline.state import _audit_log
from core.security import redact_urls

logger = logging.getLogger(__name__)

# 相似度阈值：低于此值视为显著差异。0.85 下真实双后端识别差异
# （表格 HTML 风格不同、空白差异）不会误报 —— 归一化已去掉标签和
# 全部空白，剩余是纯内容差异。
_DUAL_SIM_THRESHOLD = 0.85
# SequenceMatcher 输入截断：超长页（12K+ 字符）O(n²) 比对耗时不可控，
# 前 5000 字符足以判定"内容是否一致"。
_COMPARE_MAX_CHARS = 5000


def _normalize_for_compare(html: str) -> str:
    """对比归一化：剥 OCR 警告前缀 + 去标签 + 去全部空白。"""
    text = re.sub(r"^\[OCR 警告:[^\]]*\]\s*", "", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", "", text)


def _table_count(html: str) -> int:
    # 锚定正则防 "</table>" 子串双计（与 page_analyzer 同坑）
    return len(re.findall(r"<table[\s>]", html or ""))


_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)


def _extract_cells(html: str) -> list[str]:
    """提取表格单元格文本（去标签去空白，≥4 字符的有效单元格）。"""
    cells = []
    for raw in _TD_RE.findall(html or ""):
        t = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", raw))
        if len(t) >= 4:
            cells.append(t)
    return cells


def _cell_loss_ratio(from_html: str, against_norm: str) -> tuple[int, int]:
    """from_html 的表格单元格在 against 归一化文本中的缺失率。

    按内容对比而非标记数 —— 两后端标记风格不同是常态（Paddle 纯文本
    vs MinerU HTML），只要单元格文本在对侧正文中能找到就不算丢失。
    """
    cells = _extract_cells(from_html)
    if not cells:
        return 0, 0
    missing = sum(1 for c in cells if c not in against_norm)
    return missing, len(cells)


def _coverage(inner: str, outer: str) -> float:
    """inner 的内容被 outer 覆盖的比例（匹配块长度和 / inner 长度）。

    用覆盖率而非相似度比率：ratio 会惩罚"一侧多识别出内容"（插入），
    而门禁 3 关心的是"某侧是否丢失了对方已有的内容"，额外内容不是
    丢失。两侧各算一次覆盖率即可双向检测缺失。
    """
    if not inner:
        return 1.0
    sm = difflib.SequenceMatcher(
        None, inner[:_COMPARE_MAX_CHARS], outer[:_COMPARE_MAX_CHARS]
    )
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / min(len(inner), _COMPARE_MAX_CHARS)


def compare_page(primary_html: str, secondary_html: str) -> tuple[bool, str]:
    """单页对比。返回 (是否差异, 差异原因)。

    三层检查：
    1. 单侧空页（内容有无错位）
    2. 双向文本覆盖率 < 阈值（某侧丢失了对方已有的正文内容；
       一侧多识别出的内容不算差异 —— 手写体被单侧捕捉属正常方差）
    3. 表格单元格丢失率 > 30%（小表丢失在大页里拉不动覆盖率，
       按单元格内容在对侧正文中的存在性判断，兼容标记风格差异）
    """
    p_norm = _normalize_for_compare(primary_html)
    s_norm = _normalize_for_compare(secondary_html)
    if not p_norm and not s_norm:
        return False, ""
    if not p_norm or not s_norm:
        return True, "单侧空页（一侧有正文另一侧无可识别文本）"
    cov_p_in_s = _coverage(p_norm, s_norm)
    if cov_p_in_s < _DUAL_SIM_THRESHOLD:
        return True, (
            f"主侧内容在副侧缺失（覆盖率 {cov_p_in_s:.2f}，"
            f"阈值 {_DUAL_SIM_THRESHOLD}）"
        )
    cov_s_in_p = _coverage(s_norm, p_norm)
    if cov_s_in_p < _DUAL_SIM_THRESHOLD:
        return True, (
            f"副侧内容在主侧缺失（覆盖率 {cov_s_in_p:.2f}，"
            f"阈值 {_DUAL_SIM_THRESHOLD}）"
        )
    # 小表丢失检查（双向：任一侧相对另一侧丢单元格都算差异）
    miss_p, total_p = _cell_loss_ratio(primary_html or "", s_norm)
    if total_p and miss_p / total_p > 0.3:
        return True, f"主侧表格单元格在副侧缺失（{miss_p}/{total_p}）"
    miss_s, total_s = _cell_loss_ratio(secondary_html or "", p_norm)
    if total_s and miss_s / total_s > 0.3:
        return True, f"副侧表格单元格在主侧缺失（{miss_s}/{total_s}）"
    return False, ""


def _secondary_available(secondary: str) -> bool:
    """备选后端凭据完整性判断（与 _get_ocr_chain 同语义）。"""
    if secondary == "paddle":
        paddle_cfg = config["paddle_ocr"]
        return bool(paddle_cfg.api_url and paddle_cfg.token)
    if secondary == "mineru":
        return bool(config["mineru"].token)
    return False


async def run_dual_compare(
    db, job_id: str, ocr_pdf_path: str, primary_backend: str, pages: list[dict]
) -> list[dict]:
    """门禁 3 主入口。返回差异列表 [{page, reason}, ...]，无差异/跳过返回 []。

    任一环节失败（备选未配置 / OCR 异常 / 取消）都只记审计并返回 []，
    绝不阻断主流程 —— 对比是增强证据，不是硬依赖。
    """
    from core.pipeline import _is_cancelled as _run_is_cancelled

    if not getattr(config["app"], "ocr_dual_compare", False):
        return []
    if primary_backend not in ("paddle", "mineru"):
        return []
    secondary = "mineru" if primary_backend == "paddle" else "paddle"
    if not _secondary_available(secondary):
        await _audit_log(
            db, job_id, "dual_compare_skipped",
            f"secondary={secondary} 凭据不完整，无法对比",
        )
        return []
    if await _run_is_cancelled(job_id):
        return []

    await _audit_log(
        db, job_id, "dual_compare_start",
        f"primary={primary_backend} secondary={secondary}",
    )
    logger.info(
        f"[{job_id}] Dual compare: re-running OCR via secondary "
        f"backend={secondary} (gate 3)"
    )
    try:
        # 函数体内延迟解析 — 测试 monkeypatch core.{mineru,ocr}_client.run_ocr
        if secondary == "mineru":
            from core.mineru_client import run_ocr as _secondary_run
        else:
            from core.ocr_client import run_ocr as _secondary_run
        sec_pages = await asyncio.to_thread(_secondary_run, ocr_pdf_path)
    except Exception as e:
        msg = redact_urls(str(e))[:300]
        await _audit_log(db, job_id, "dual_compare_error", msg)
        logger.warning(f"[{job_id}] Dual compare secondary run failed: {msg}")
        return []

    diffs: list[dict] = []
    total = max(len(pages), len(sec_pages))
    for idx in range(total):
        pno = idx + 1
        p_html = ""
        if idx < len(pages):
            p_html = (pages[idx].get("markdown") or {}).get("text") or ""
        s_html = ""
        if idx < len(sec_pages):
            s_html = (sec_pages[idx].get("markdown") or {}).get("text") or ""
        is_diff, reason = compare_page(p_html, s_html)
        if is_diff:
            diffs.append({"page": pno, "reason": reason})

    await _audit_log(
        db, job_id, "dual_compare_done",
        f"pages={total} diffs={len(diffs)}"
        + (f" detail={[(d['page'], d['reason']) for d in diffs[:10]]}" if diffs else ""),
    )
    if diffs:
        logger.warning(
            f"[{job_id}] Dual compare: {len(diffs)}/{total} page(s) differ "
            f"— forcing manual review (gate 3)"
        )
    else:
        logger.info(f"[{job_id}] Dual compare: {total}/{total} pages consistent")
    return diffs
