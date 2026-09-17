"""PaddleOCR-VL async OCR client.

Logic derived from OCR_BAIDU/core/api_client.py (submitted/polled/extracted there).
Kept minimal: submit, poll, download result JSONL.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from config import config
from core.security import redact_urls
from logging_config import ocr_job_id_var, JobIdFilter

logger = logging.getLogger(__name__)

# robustness-F1: job_id 透传 — 内部函数（submit/poll/download）不逐一改
# 签名，通过 logging_config.ocr_job_id_var（ContextVar）+ JobIdFilter 给本
# 模块所有日志加 [job_id] 前缀。pipeline 在 to_thread 调用前 set（asyncio
# 自动拷贝 context 到线程），排障时从 pipeline.log 按 job 反查全流程。
logger.addFilter(JobIdFilter())

POLL_INTERVAL = 5  # seconds
POLL_TIMEOUT = 600  # 10 minutes（基础值，大文档按页数扩展）


class OCRCancelled(RuntimeError):
    """Raised inside blocking OCR worker threads when the job was cancelled.

    asyncio.to_thread 无法中断已运行的线程，只能由轮询循环主动检查外部
    取消标记后抛出本异常，把中止信号带回事件循环 —— 避免用户点取消后
    主/备两个后端仍各跑满整个轮询超时（51 页任务最长可达数十分钟，
    白白消耗上游配额）。failover 链必须放行本异常而非切换备选。
    """
# 每页额外轮询预算（秒）：e2e 实证 51 页/43.8MB 任务 600s 内未返回
# （2026-08-24 任务 85287516750168064 轮询超时）；大任务在服务端排队+
# 逐页抽取时间随页数线性增长，固定 600s 对大文档过紧。
POLL_TIMEOUT_PER_PAGE = 30
POLL_TIMEOUT_MAX = 3600  # 单任务轮询上限 1 小时


def poll_timeout_for_pages(pages: int) -> int:
    """按**页数**计算轮询超时（纯函数形式）。

    与 `poll_timeout_for` 同一公式 —— 抽出来是为了让「上游单次调用封顶」
    可以被**推导它的地方**读取（如 self_heal 的旋转补救预算、看门狗的
    阈值不变式），而不必各自手写 630 这个数字。本项目的教训：同一个
    数字被写死两处，迟早漂移（见 docs/PROJECT_PITFALLS.md）。
    """
    return min(POLL_TIMEOUT + POLL_TIMEOUT_PER_PAGE * max(0, int(pages)),
               POLL_TIMEOUT_MAX)


def poll_timeout_for(pdf_path: str) -> int:
    """按页数计算轮询超时：base + per_page × 页数（封顶 POLL_TIMEOUT_MAX）。"""
    try:
        import fitz  # 局部导入：仅计数时加载

        with fitz.open(pdf_path) as doc:
            pages = doc.page_count
    except Exception:
        pages = 0
    return poll_timeout_for_pages(pages)


# ─── 上游错误分类：跨后端「容量/拥塞类」的单一真值 ────────────────────
# 判据：**容量/拥塞类**（上游队列满、限流、5xx、服务端明示可重试）与
# **永久类**（参数错、格式错）必须分开处置 —— 前者等一会儿可恢复，
# 后者等多久都一样。
#
# 为什么必须有这张表（2026-09-16 实测，见 docs/PROJECT_PITFALLS.md §十一）：
# 该词汇表此前**散落两处**（mineru_client.run_ocr 内联的 transient_markers
# 元组 + 各处裸字符串），而「容量类」**无人识别** —— self_heal 的旋转探测
# 把 Paddle `HTTP 400 code:10010 任务提交队列已满` 当成永久失败，每个角度
# 只花 ~15s 就换下一个，**61s 内耗尽全部角度尝试**；而实测拥塞窗口是
# **分钟级**（同日 ≥18 分钟）。结果旋转通道在**最需要它的场景**（上游
# 降级造成空页，正是它被设计的场景）放弃得最快 → 内容永久丢失。
#
# **刻意不把「轮询超时」算作拥塞**：它已被自身封顶约束
# （`poll_timeout_for` 对单页 = 630s），若再按拥塞退避重试，等于把一次
# 630s 等待乘 4 —— 看门狗的 OCR 单页缺口上界会随之爆掉（阈值不变式，
# docs/RUNTIME_WATCHDOG.md §5）。超时是「上游慢」，不是「上游拒绝服务」，
# 重试的边际收益远低于成本。
_CONGESTION_MARKERS: tuple[str, ...] = (
    "10010",                    # Paddle：任务提交队列已满
    "队列已满",
    "queue is full",
    "too many requests",        # 429 的文本形态（状态码另有正则兜底）
    "please try again later",   # MinerU：终态但明示可重试
    "parsing failed",           # MinerU：同上（与之成对出现）
)
# 429 / 5xx 的**状态码**形态：`submit_pdf` 的消息形如
# "提交失败 HTTP 503: ..."。带词边界，避免 "HTTP 5001" 这类自造码误命中
# （`5\d\d` 后紧跟数字则不构成词边界）。
_CONGESTION_STATUS_RE = re.compile(r"\bHTTP\s*(?:429|5\d\d)\b", re.IGNORECASE)


def is_congestion_error(err: object) -> bool:
    """该错误是否属「上游容量/拥塞类」—— 等一会儿可恢复？

    入参可以是异常对象，或已 redact 的字符串（调用方在日志里已脱敏的
    场景；标记词与状态码都不含 URL，脱敏不影响判定）。

    **只看容量/拥塞**，不看超时（理由见上方常量区的说明）。
    """
    text = str(err)
    if _CONGESTION_STATUS_RE.search(text):
        return True
    lowered = text.lower()
    return any(m.lower() in lowered for m in _CONGESTION_MARKERS)


def submit_pdf(pdf_path: str, retries: int = 3) -> str:
    """Submit a PDF to PaddleOCR-VL async API, return job_id."""
    cfg = config["paddle_ocr"]
    headers = {"Authorization": f"bearer {cfg.token}"}
    optional_payload = json.dumps({
        # 批记录来源包含扫描件、拍摄件和横向表格；关闭这两项会把可纠正
        # 的旋转/几何畸变直接传给 VLM，表现为漏正文或错列。默认优先
        # 准确性，服务端若不支持会按其兼容策略忽略可选字段。
        "useDocOrientationClassify": True,
        "useDocUnwarping": True,
        "useChartRecognition": False,
    })
    data = {"model": cfg.model, "optionalPayload": optional_payload}

    # 流式读取：避免一次性将大文件（131MB+）全部读入内存
    # 使用文件对象让 requests 自动分块上传
    pdf_file = open(pdf_path, "rb")
    file_size_mb = pdf_file.seek(0, 2) / 1024 / 1024
    pdf_file.seek(0)
    files = {"file": (Path(pdf_path).name, pdf_file, "application/pdf")}
    # 动态超时：大文件需要更长的上传时间
    # 基准 120s + 每 10MB 额外 30s（约 3MB/s 上传速度假设）
    upload_timeout = max(120, int(120 + file_size_mb * 3))
    logger.info(
        f"Submitting to PaddleOCR-VL: file={Path(pdf_path).name} "
        f"size={file_size_mb:.1f}MB model={cfg.model} url={cfg.api_url} "
        f"timeout={upload_timeout}s"
    )

    last_error = None
    try:
        for attempt in range(1, retries + 1):
            try:
                # 重试时重置文件指针到开头（上次失败可能已部分读取）
                pdf_file.seek(0)
                resp = requests.post(
                    cfg.api_url,
                    files=files,
                    data=data,
                    headers=headers,
                    timeout=upload_timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"提交失败 HTTP {resp.status_code}: {redact_urls(resp.text[:300])}")
                result = resp.json()
                job_id = result.get("data", {}).get("jobId") or result.get("jobId")
                if not job_id:
                    raise RuntimeError(f"响应中缺少 jobId: {redact_urls(str(result))}")
                logger.info(f"OCR 任务已提交: jobId={job_id}")
                return job_id
            except Exception as e:
                last_error = e
                logger.warning(
                    f"提交尝试 {attempt}/{retries} 失败: {type(e).__name__}: {redact_urls(str(e))}"
                )
                if attempt < retries:
                    backoff = 2 * attempt
                    logger.info(f"提交重试: 退避 {backoff}s 后尝试第 {attempt + 1} 次")
                    time.sleep(backoff)
        raise RuntimeError(f"提交失败: 重试 {retries} 次后放弃: {redact_urls(str(last_error))}")
    finally:
        pdf_file.close()


def poll_job(
    job_id: str,
    progress_callback=None,
    timeout_s: int | None = None,
    cancel_check=None,
) -> dict:
    """Poll until job done. Returns the final poll response dict.

    容错：网络异常重试，最�?POLL_MAX_RETRIES 次后放弃�?
    progress_callback(done, total): 每次轮询�?extractProgress 时回调，
    �?pipeline 实时更新 job 进度（Stage 1 流式反馈）�?
    timeout_s: 轮询上限（缺�?POLL_TIMEOUT；大文档�?run_ocr 按页数扩展）�?
    cancel_check: 同步取消探针（阻塞线程内无法 await，由调用方提供
    线程安全的只读检查）；返回 True 时抛 OCRCancelled 中止轮询。
    """
    timeout_s = timeout_s or POLL_TIMEOUT
    cfg = config["paddle_ocr"]
    headers = {"Authorization": f"bearer {cfg.token}"}
    url = f"{cfg.api_url}/{job_id}"
    start = time.time()
    consecutive_errors = 0

    while (time.time() - start) < timeout_s:
        if cancel_check is not None and cancel_check():
            raise OCRCancelled(f"job {job_id} cancelled during OCR polling")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"轮询 HTTP {resp.status_code}, 重试...")
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise RuntimeError(f"轮询失败: 连续 {consecutive_errors} 次 HTTP 状态异常")
                time.sleep(POLL_INTERVAL)
                continue
            try:
                j = resp.json()
            except ValueError as e:
                # 对抗审查 P1-3：HTTP 200 但响应体是网关 HTML 错误页/非 JSON
                # 时 json() 抛 JSONDecodeError — 未被下方 except RequestException
                # 捕获 → 轮询中断整单失败（mineru 端 P-W6 已修，此处同构未修）。
                # 计为重试错误，与网络错误同一退避路径。
                consecutive_errors += 1
                logger.warning(f"轮询非 JSON 响应 ({consecutive_errors}/5): {e}")
                if consecutive_errors >= 5:
                    raise RuntimeError(
                        f"轮询失败: 连续 {consecutive_errors} 次非 JSON 响应: {e}"
                    )
                time.sleep(POLL_INTERVAL * 2)
                continue
            # 修复 P0 回归：重置必须在 JSON 解析成功之后——此前重置点在
            # json() 之前，HTTP 200 但响应体非 JSON 时每次循环都被重置为 0，
            # (1/5) 永远不递增，轮询空转满 POLL_TIMEOUT(600s) 才失败。
            consecutive_errors = 0
            state = str(j.get("data", {}).get("state") or j.get("state") or "").lower()
            progress = j.get("data", {}).get("extractProgress", {})
            extracted = progress.get("extractedPages", "?")
            total = progress.get("totalPages", "?")
            if progress_callback and isinstance(extracted, int) and isinstance(total, int):
                progress_callback(extracted, total)
            logger.info(f"轮询 state={state} pages={extracted}/{total}")
            if state in ("done", "success"):
                elapsed = int(time.time() - start)
                logger.info(f"轮询完成: job_id={job_id} elapsed={elapsed}s pages={extracted}/{total}")
                return j
            if state in ("failed", "error"):
                raise RuntimeError(f"OCR 任务失败: {redact_urls(str(j))}")
            time.sleep(POLL_INTERVAL)
        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            logger.warning(f"轮询网络错误 ({consecutive_errors}/5): {redact_urls(str(e))}")
            if consecutive_errors >= 5:
                raise RuntimeError(
                    f"轮询失败: 连续 {consecutive_errors} 次网络错误: {redact_urls(str(e))}"
                )
            time.sleep(POLL_INTERVAL * 2)  # 网络错误时退避更久
    elapsed = int(time.time() - start)
    raise RuntimeError(
        f"轮询超时: {elapsed}s 内未收到 OCR 结果（上限 {timeout_s}s，任务 {job_id}）"
    )


def _extract_block_text(block) -> str:
    """深度提取 parsing_res_list 块中的可见文本（dict/list 递归）。

    只认 text/content 类语义键与子块递归，跳过 box/points/words 坐标类键 —
    坐标数字对 LLM 无意义，混入会放大噪音。
    """
    if isinstance(block, str):
        return block.strip()
    if isinstance(block, list):
        parts = [_extract_block_text(b) for b in block]
        return "\n".join(p for p in parts if p)
    if isinstance(block, dict):
        for key in ("text", "content", "text_content"):
            v = block.get(key)
            if v is None:
                continue
            t = _extract_block_text(v)
            if t:
                return t
        parts = []
        for v in block.values():
            if isinstance(v, (dict, list, str)):
                t = _extract_block_text(v)
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _ensure_page_text(pages: list[dict]) -> None:
    """P0 级兜底（对抗审查 cr-19）：服务端 markdown.text 可能为空/过短，
    而 prunedResult.parsing_res_list（块级文本）此前从不被读取 — 表格外
    文本/图片文字会在 OCR 服务端 HTML 组装失败时静默丢失，且无任何
    校验可自证。此处以块级文本组装纯文本兜底并改写 markdown.text，
    pipeline 无感知。
    """
    for p in pages:
        md = p.get("markdown")
        if not isinstance(md, dict):
            continue
        text = (md.get("text") or "").strip()
        if len(text) >= 20:
            continue
        pl = (p.get("prunedResult") or {}).get("parsing_res_list") or []
        fallback = "\n".join(t for t in (_extract_block_text(b) for b in pl) if t)
        if fallback.strip():
            logger.warning(
                f"Page markdown.text empty/short ({len(text)} chars) — "
                f"built fallback from parsing_res_list ({len(fallback)} chars)"
            )
            # P1-3: 显式降级标记 — 块级文本兜底丢失表格结构，LLM 与规则层
            # 必须知道输入已降级（否则把纯文本当完整表格分析，结论失真）。
            md["text"] = (
                "[OCR 警告: 服务端表格组装失败，以下为块级文本兜底，"
                "表格结构已丢失，各行内容可能串行]\n\n" + fallback
            )


def _persist_paddle_original(raw: str, pdf_path: str, suffix: str) -> None:
    """门禁 1c：Paddle 原始响应落盘到 job_dir（pdf_path 同级）。

    单 JSON 写 paddle_original.json，JSONL 写 paddle_original.jsonl，
    原始字节即服务端产物 —— GMP 追溯时可用其复现解析结果。
    失败仅告警不阻断主流程。
    """
    if not pdf_path or not raw:
        return
    try:
        out = Path(pdf_path).parent / f"paddle_original.{suffix}"
        tmp = out.with_suffix(f".{suffix}.tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, out)
        logger.info(
            f"原始产物已落盘: {out.name} ({len(raw) / 1024:.0f}KB)"
        )
    except Exception as e:
        logger.warning(
            f"原始产物落盘失败（不影响主流程）: {redact_urls(str(e))[:200]}"
        )


def download_result(poll_response: dict, pdf_path: str = "") -> list[dict]:
    """Download OCR result JSON from the URL in poll response.

    Returns a list of page dicts, each containing:
      - markdown.text (HTML table string; falls back to parsing_res_list text
        when the server-side HTML assembly is empty/short)
      - prunedResult.parsing_res_list (block-level structure)

    pdf_path 与 MinerU 版签名对齐（当前未用于解析，保留扩展位）。
    """
    result_url_obj = poll_response.get("data", {}).get("resultUrl") or poll_response.get("resultUrl")
    json_url = None
    if isinstance(result_url_obj, dict):
        json_url = result_url_obj.get("jsonUrl") or result_url_obj.get("url")
    elif isinstance(result_url_obj, str):
        json_url = result_url_obj

    if not json_url:
        raise RuntimeError(f"No result URL in poll response: {redact_urls(str(poll_response))}")

    # 对抗审查（cr-16）+ P1-7：resultUrl 是服务端签名 CDN 地址，query 可能带
    # 签名 token — 日志只记 pathname；网络异常消息脱敏（requests 异常回显
    # 完整 URL，冒泡进 jobs.error_message → 报告/通知反刍泄露）。
    from urllib.parse import urlsplit
    logger.info(f"Downloading OCR result from {urlsplit(json_url).path}...")
    try:
        resp = requests.get(json_url, timeout=180, verify=True)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Download network error: {redact_urls(str(e))}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Download failed HTTP {resp.status_code}")

    raw = resp.text
    raw_size_kb = len(raw) / 1024
    pages: list[dict] = []

    # Try single JSON first
    try:
        obj = json.loads(raw)
        lpr = obj.get("result", {}).get("layoutParsingResults", [])
        if not lpr:
            # JSON 解析成功但 layoutParsingResults 为空 — 上游 OCR 服务返回了
            # 异常结构。记录完整响应便于诊断，抛异常让 pipeline 转 error 状态，
            # 而非静默返回空列表导致 review 页面空白。
            result_obj = obj.get("result", {})
            logger.error(
                f"OCR result JSON parsed but layoutParsingResults is empty. "
                f"top_keys={list(obj.keys())} result_keys={list(result_obj.keys()) if isinstance(result_obj, dict) else type(result_obj).__name__} "
                f"raw_first_500={redact_urls(raw[:500])!r}"
            )
            raise RuntimeError(
                "OCR 返回空结果: layoutParsingResults 为空。"
                f"top_keys={list(obj.keys())}, "
                f"result_keys={list(result_obj.keys()) if isinstance(result_obj, dict) else type(result_obj).__name__}"
            )
        pages.extend(lpr)
        data_info = obj.get("result", {}).get("dataInfo", {})
        if data_info:
            for i, p in enumerate(pages):
                if not p.get("page_count"):
                    p["page_count"] = i + 1
        logger.info(
            f"OCR download complete (single JSON): {len(pages)} pages, {raw_size_kb:.1f}KB"
        )
        _ensure_page_text(pages)
        for p in pages:
            p["source"] = "paddle"
        _persist_paddle_original(raw, pdf_path, "json")
        return pages
    except json.JSONDecodeError:
        pass

    # JSONL (one JSON object per line, each with 4 pages)
    line_count = 0
    bad_lines = 0
    # 对抗审查 P2-3：Windows 侧服务常发 UTF-8 BOM 的 JSONL，首行
    # \ufeff 前缀使 json.loads 失败 → 前 4 页静默丢弃（小文件直接 0 页）
    raw = raw.lstrip("\ufeff")
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        line_count += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            # P0-3 修复：坏行 = 该行 4 页内容丢失。此前静默 continue →
            # 后续行内容整体前移，页码张冠李戴（复核对不上 PDF 原图）且
            # failed_pages 按尾部缺失计算标错页。现插入 4 个空占位 dict
            # 保持页码对齐 — 占位页文本为空，走 pipeline 空页自愈
            # （<100 字符判定）重新 OCR 恢复。
            bad_lines += 1
            logger.warning(
                f"OCR JSONL 第 {line_count} 行解析失败（预期包含第 "
                f"{(line_count - 1) * 4 + 1}-{line_count * 4} 页）: {e.msg} — "
                f"已插入空占位页保持页码对齐，空页自愈将重试: "
                f"line_first_200={line[:200]!r}"
            )
            pages.extend([{"markdown": {"text": ""}} for _ in range(4)])
            continue
        result = obj.get("result", obj)
        lpr = result.get("layoutParsingResults", [])
        if isinstance(lpr, list):
            pages.extend(lpr)
        data_info = result.get("dataInfo", {})
        if data_info:
            for i, p in enumerate(pages):
                if not p.get("page_count"):
                    p["page_count"] = i + 1

    if not pages:
        # JSONL 解析后仍无页 — 记录诊断信息并抛异常，避免 pipeline 继续
        # 走到 Stage 2/3 最终生成空 findings 导致 review 页面空白。
        logger.error(
            f"OCR JSONL parsing yielded 0 pages. lines={line_count} "
            f"raw_first_500={raw[:500]!r}"
        )
        raise RuntimeError(
            f"OCR 返回空结果: JSONL 解析后 0 页（共 {line_count} 行）。"
            f"raw_first_200={raw[:200]!r}"
        )
    logger.info(
        f"OCR download complete (JSONL): {len(pages)} pages, {raw_size_kb:.1f}KB"
        + (f" ({bad_lines} bad lines placeholdered)" if bad_lines else "")
    )
    _ensure_page_text(pages)
    for p in pages:
        p["source"] = "paddle"
    _persist_paddle_original(raw, pdf_path, "jsonl")
    return pages


def run_ocr(
    pdf_path: str,
    progress_callback=None,
    job_id: str = "",
    cancel_check=None,
) -> list[dict]:
    """End-to-end OCR: submit �?poll �?download. Returns list of page results.

    progress_callback 透传�?poll_job（Stage 1 实时进度）�?
    job_id: 应用�?job id �?仅用于日志前缀（本模块所有日志自动带
    [job_id]），便于�?pipeline.log 反查某个 job �?OCR 全流程�?
    cancel_check: 同步取消探针，透传 poll_job（见 OCRCancelled）。
    """
    if job_id:
        _token = ocr_job_id_var.set(job_id)
    try:
        paddle_job_id = submit_pdf(pdf_path)
        # 大文档按页数扩展轮询预算（e2e 实证 51 �?600s 超时�?
        timeout_s = poll_timeout_for(pdf_path)
        poll_response = poll_job(
            paddle_job_id,
            progress_callback=progress_callback,
            timeout_s=timeout_s,
            cancel_check=cancel_check,
        )
        # pdf_path 透传（与 MinerU 签名对齐；Paddle 解析暂不用它，留给
        # 后续页数对齐校验扩展）
        pages = download_result(poll_response, pdf_path=pdf_path)
        if not pages:
            # download_result 现在应在空结果时抛异常，此处为防御性兜底
            logger.warning(
                f"OCR returned 0 pages for job_id={paddle_job_id} (defensive check)"
            )
    finally:
        if job_id:
            ocr_job_id_var.reset(_token)
    return pages
