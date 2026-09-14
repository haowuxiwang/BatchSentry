"""docling 第三 OCR 后端（可选依赖，MIT 许可；T7.2 / M7）。

定位：docling（IBM → Linux Foundation AI & Data，**MIT**）是**本地**多格式
解析流水线（DocLayNet 版面 + TableFormer 表格），与 MinerU / PaddleOCR-VL
的"上传 → 轮询 → 下载"服务式后端不同 —— 直接在进程内 `DocumentConverter`
转换 PDF，无需 token / 网络。

接入原则（**缺失即降级**，M7 验收）：
- docling 未安装 → `is_available()` 返回 False；`_get_ocr_chain()` 不纳入，
  主链完全不受影响；`OCR_BACKEND=docling` 时 `_get_ocr_backend()` 抛
  `OcrBackendUnavailable` → 链级优雅回退到默认 PaddleOCR。
- 已安装 → 提供与 `core.mineru_client.run_ocr` / `core.ocr_client.run_ocr`
  **同签名**的 `run_ocr`，输出 page dict 形状对齐
  （`{"markdown": {"text": str}, "page_count": int, "_source": "docling"}`），
  可被 `_run_ocr_with_failover` / 三引擎对比脚本透明使用。

许可：MIT（2026-09-11 勘误，见 docs/ROADMAP_v1.1.md）。仅作本地对照/验证
用途，不改变产品分发形态。

轻依赖纪律：重依赖（torch / docling 模型）**仅在真正调用 run_ocr 时**才
导入 —— 仅探测可用性（is_available）不会拉起 torch，避免拖慢进程启动。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TIER_SOURCE = "docling"


def is_available() -> bool:
    """docling 是否可导入（可选依赖）。

    只做顶层包探测 —— 不 import torch / 不加载模型。任何导入错误
    （未安装 / ABI 不匹配 / 缺 so）一律视为不可用，绝不抛出。
    """
    try:
        import docling  # noqa: F401
    except Exception:
        return False
    return True


def _load_converter():
    """构造 docling DocumentConverter（延迟导入重依赖）。"""
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def _num_pages(doc) -> int:
    """从 DoclingDocument 取页数（多路径兜底，取不到返回 0）。"""
    for attr in ("num_pages",):
        fn = getattr(doc, attr, None)
        if callable(fn):
            try:
                v = fn()
                if isinstance(v, int) and v > 0:
                    return v
            except Exception:
                pass
    pages = getattr(doc, "pages", None)
    if isinstance(pages, dict) and pages:
        try:
            return max(int(k) for k in pages.keys())
        except (TypeError, ValueError):
            pass
    if isinstance(pages, (list, tuple)):
        return len(pages)
    return 0


def _page_markdown(doc, page_no: int) -> str:
    """导出单页 markdown（page_no 1-based，docling 语义）。失败返回 ""。"""
    try:
        return doc.export_to_markdown(page_no=page_no) or ""
    except Exception:
        # 单页导出失败（少见 API 变动）→ 回退整份导出后按页切分
        return ""


def _whole_markdown(doc) -> str:
    try:
        return doc.export_to_markdown() or ""
    except Exception:
        return ""


def _split_by_page_sep(full_md: str, total: int | None = None) -> list[str]:
    """整份 markdown 按分页符切分（docling 用 `<!-- page break -->` 类标记）。

    total 给定时要求切分结果段数恰好等于 total（否则视为不可信 → 返回 []）；
    total 为 None 时只要求存在分页符（段数 >1）。无分页符返回 []。
    """
    import re

    parts = re.split(r"<!--\s*page[_ ]?break\s*-->", full_md, flags=re.IGNORECASE)
    if len(parts) <= 1:
        return []
    if total is not None and len(parts) != total:
        return []
    return [p.strip() for p in parts]


def run_ocr(
    pdf_path: str,
    progress_callback=None,
    job_id: str = "",
    cancel_check=None,
) -> list[dict]:
    """docling 本地解析入口（与 mineru / ocr_client.run_ocr 同签名）。

    返回 `[{markdown: {text}, page_count, _source: "docling"}, ...]`（1-based，
    连续页）。docling 未安装时抛 `OcrBackendUnavailable`（调用方降级）。

    progress_callback(done, total)：逐页导出时回调（对齐服务式后端的
    轮询进度语义）。cancel_check()：同步取消探针，为 True 时中止并抛
    OCRCancelled —— 与 MinerU 路径同款语义，避免取消后仍跑满本地推理。
    """
    if not is_available():
        # 延迟导入避免与 pipeline 形成导入环。
        from core.pipeline.ocr_support import OcrBackendUnavailable

        raise OcrBackendUnavailable(
            "docling 未安装（可选依赖）—— `pip install docling` 后可用"
        )

    from core.ocr_client import OCRCancelled
    from logging_config import ocr_job_id_var

    _token = ocr_job_id_var.set(job_id) if job_id else None
    try:
        converter = _load_converter()
        logger.info(f"[docling] 本地解析开始: {pdf_path}")
        result = converter.convert(str(pdf_path))
        doc = getattr(result, "document", None) or result

        total = _num_pages(doc)
        pages: list[dict] = []

        if total > 0:
            # 逐页导出：可控进度 + 页间可取消
            for page_no in range(1, total + 1):
                if cancel_check is not None and cancel_check():
                    raise OCRCancelled(f"job {job_id} cancelled during docling export")
                text = _page_markdown(doc, page_no)
                pages.append(
                    {
                        "markdown": {"text": text},
                        "page_count": page_no,
                        "_source": TIER_SOURCE,
                    }
                )
                if progress_callback is not None:
                    try:
                        progress_callback(page_no, total)
                    except Exception as e:
                        logger.warning(f"[docling] progress_callback 失败: {e}")
        else:
            # 取不到页数：整份导出后按分页符切分；再失败则单页兜底
            full_md = _whole_markdown(doc)
            parts = _split_by_page_sep(full_md)
            if not parts:
                parts = [full_md]
            for i, text in enumerate(parts, start=1):
                pages.append(
                    {
                        "markdown": {"text": text},
                        "page_count": i,
                        "_source": TIER_SOURCE,
                    }
                )
            if progress_callback is not None:
                try:
                    progress_callback(len(parts), len(parts))
                except Exception:
                    pass

        logger.info(f"[docling] 本地解析完成: {len(pages)} 页")
        return pages
    finally:
        if _token is not None:
            ocr_job_id_var.reset(_token)
