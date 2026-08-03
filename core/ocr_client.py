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

logger = logging.getLogger(__name__)

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

    with open(pdf_path, "rb") as f:
        file_content = f.read()

    file_size_mb = len(file_content) / 1024 / 1024
    files = {"file": (Path(pdf_path).name, file_content, "application/pdf")}
    # 动态超时：大文件需要更长的上传时间
    # 基准 120s + 每 10MB 额外 30s（约 3MB/s 上传速度假设）
    upload_timeout = max(120, int(120 + file_size_mb * 3))
    logger.info(
        f"Submitting to PaddleOCR-VL: file={Path(pdf_path).name} "
        f"size={file_size_mb:.1f}MB model={cfg.model} url={cfg.api_url} "
        f"timeout={upload_timeout}s"
    )

    last_error = None
    for attempt in range(1, retries + 1):
        try:
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


def poll_job(job_id: str) -> dict:
    """Poll until job done. Returns the final poll response dict.

    容错：网络异常重试，最多 POLL_MAX_RETRIES 次后放弃。
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
            consecutive_errors = 0  # 成功后重置错误计数
            j = resp.json()
            state = str(j.get("data", {}).get("state") or j.get("state") or "").lower()
            progress = j.get("data", {}).get("extractProgress", {})
            extracted = progress.get("extractedPages", "?")
            total = progress.get("totalPages", "?")
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
    cfg = config["paddle_ocr"]
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
        if lpr:
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
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
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
    logger.info(
        f"OCR download complete (JSONL): {len(pages)} pages, {raw_size_kb:.1f}KB"
    )
    return pages


def run_ocr(pdf_path: str) -> list[dict]:
    """End-to-end OCR: submit → poll → download. Returns list of page results."""
    job_id = submit_pdf(pdf_path)
    poll_response = poll_job(job_id)
    return download_result(poll_response)
