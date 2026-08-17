"""PaddleOCR-VL async OCR client.

Logic derived from OCR_BAIDU/core/api_client.py (submitted/polled/extracted there).
Kept minimal: submit, poll, download result JSONL.
"""
import json
import logging
import time
from pathlib import Path

import requests

from config import config
from logging_config import ocr_job_id_var, JobIdFilter

logger = logging.getLogger(__name__)

# robustness-F1: job_id 透传 — 内部函数（submit/poll/download）不逐一改
# 签名，通过 logging_config.ocr_job_id_var（ContextVar）+ JobIdFilter 给本
# 模块所有日志加 [job_id] 前缀。pipeline 在 to_thread 调用前 set（asyncio
# 自动拷贝 context 到线程），排障时从 pipeline.log 按 job 反查全流程。
logger.addFilter(JobIdFilter())

POLL_INTERVAL = 5  # seconds
POLL_TIMEOUT = 600  # 10 minutes


def submit_pdf(pdf_path: str, retries: int = 3) -> str:
    """Submit a PDF to PaddleOCR-VL async API, return job_id."""
    cfg = config["paddle_ocr"]
    headers = {"Authorization": f"bearer {cfg.token}"}
    optional_payload = json.dumps({
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
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
                    raise RuntimeError(f"Submit failed HTTP {resp.status_code}: {resp.text[:300]}")
                result = resp.json()
                job_id = result.get("data", {}).get("jobId") or result.get("jobId")
                if not job_id:
                    raise RuntimeError(f"No jobId in response: {result}")
                logger.info(f"OCR job submitted: jobId={job_id}")
                return job_id
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Submit attempt {attempt}/{retries} failed: {type(e).__name__}: {e}"
                )
                if attempt < retries:
                    backoff = 2 * attempt
                    logger.info(f"Submit retry: backing off {backoff}s before attempt {attempt + 1}")
                    time.sleep(backoff)
        raise RuntimeError(f"Submit failed after {retries} attempts: {last_error}")
    finally:
        pdf_file.close()


def poll_job(job_id: str, progress_callback=None) -> dict:
    """Poll until job done. Returns the final poll response dict.

    容错：网络异常重试，最多 POLL_MAX_RETRIES 次后放弃。
    progress_callback(done, total): 每次轮询到 extractProgress 时回调，
    供 pipeline 实时更新 job 进度（Stage 1 流式反馈）。
    """
    cfg = config["paddle_ocr"]
    headers = {"Authorization": f"bearer {cfg.token}"}
    url = f"{cfg.api_url}/{job_id}"
    start = time.time()
    consecutive_errors = 0

    while (time.time() - start) < POLL_TIMEOUT:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"Poll HTTP {resp.status_code}, retrying...")
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise RuntimeError(f"Poll failed after {consecutive_errors} consecutive HTTP errors")
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
                logger.warning(f"Poll non-JSON response ({consecutive_errors}/5): {e}")
                if consecutive_errors >= 5:
                    raise RuntimeError(
                        f"Poll failed after {consecutive_errors} consecutive non-JSON responses: {e}"
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
            logger.info(f"Poll state={state} pages={extracted}/{total}")
            if state in ("done", "success"):
                elapsed = int(time.time() - start)
                logger.info(f"Poll done: job_id={job_id} elapsed={elapsed}s pages={extracted}/{total}")
                return j
            if state in ("failed", "error"):
                raise RuntimeError(f"Job failed: {j}")
            time.sleep(POLL_INTERVAL)
        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            logger.warning(f"Poll network error ({consecutive_errors}/5): {e}")
            if consecutive_errors >= 5:
                raise RuntimeError(f"Poll failed after {consecutive_errors} consecutive network errors: {e}")
            time.sleep(POLL_INTERVAL * 2)  # 网络错误时退避更久
    elapsed = int(time.time() - start)
    raise RuntimeError(
        f"Polling timed out after {elapsed}s (limit={POLL_TIMEOUT}s) for job {job_id}"
    )


def download_result(poll_response: dict) -> list[dict]:
    """Download OCR result JSON from the URL in poll response.

    Returns a list of page dicts, each containing:
      - markdown.text (HTML table string)
      - prunedResult.parsing_res_list (block-level structure)
    """
    result_url_obj = poll_response.get("data", {}).get("resultUrl") or poll_response.get("resultUrl")
    json_url = None
    if isinstance(result_url_obj, dict):
        json_url = result_url_obj.get("jsonUrl") or result_url_obj.get("url")
    elif isinstance(result_url_obj, str):
        json_url = result_url_obj

    if not json_url:
        raise RuntimeError(f"No result URL in poll response: {poll_response}")

    logger.info(f"Downloading OCR result from {json_url}...")
    resp = requests.get(json_url, timeout=180, verify=True)
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
                f"raw_first_500={raw[:500]!r}"
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
        return pages
    except json.JSONDecodeError:
        pass

    # JSONL (one JSON object per line, each with 4 pages)
    line_count = 0
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
        except json.JSONDecodeError:
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
    )
    return pages


def run_ocr(pdf_path: str, progress_callback=None, job_id: str = "") -> list[dict]:
    """End-to-end OCR: submit → poll → download. Returns list of page results.

    progress_callback 透传给 poll_job（Stage 1 实时进度）。
    job_id: 应用层 job id — 仅用于日志前缀（本模块所有日志自动带
    [job_id]），便于从 pipeline.log 反查某个 job 的 OCR 全流程。
    """
    if job_id:
        _token = ocr_job_id_var.set(job_id)
    try:
        paddle_job_id = submit_pdf(pdf_path)
        poll_response = poll_job(paddle_job_id, progress_callback=progress_callback)
        pages = download_result(poll_response)
        if not pages:
            # download_result 现在应在空结果时抛异常，此处为防御性兜底
            logger.warning(
                f"OCR returned 0 pages for job_id={paddle_job_id} (defensive check)"
            )
    finally:
        if job_id:
            ocr_job_id_var.reset(_token)
    return pages
