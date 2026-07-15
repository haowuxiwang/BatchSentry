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


def submit_pdf(pdf_path: str) -> str:
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

    files = {"file": (Path(pdf_path).name, file_content, "application/pdf")}
    logger.info(f"Submitting {pdf_path} ({len(file_content)/1024/1024:.1f} MB) to PaddleOCR-VL...")

    resp = requests.post(
        cfg.api_url,
        files=files,
        data=data,
        headers=headers,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Submit failed HTTP {resp.status_code}: {resp.text[:300]}")
    result = resp.json()
    job_id = result.get("data", {}).get("jobId") or result.get("jobId")
    if not job_id:
        raise RuntimeError(f"No jobId in response: {result}")
    logger.info(f"Job submitted: {job_id}")
    return job_id


def poll_job(job_id: str) -> dict:
    """Poll until job done. Returns the final poll response dict."""
    cfg = config["paddle_ocr"]
    headers = {"Authorization": f"bearer {cfg.token}"}
    url = f"{cfg.api_url}/{job_id}"
    start = time.time()

    while (time.time() - start) < POLL_TIMEOUT:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"Poll HTTP {resp.status_code}, retrying...")
            time.sleep(POLL_INTERVAL)
            continue
        j = resp.json()
        state = str(j.get("data", {}).get("state") or j.get("state") or "").lower()
        progress = j.get("data", {}).get("extractProgress", {})
        extracted = progress.get("extractedPages", "?")
        total = progress.get("totalPages", "?")
        logger.info(f"Poll state={state} pages={extracted}/{total}")
        if state in ("done", "success"):
            return j
        if state in ("failed", "error"):
            raise RuntimeError(f"Job failed: {j}")
        time.sleep(POLL_INTERVAL)
    raise RuntimeError(f"Polling timed out after {POLL_TIMEOUT}s for job {job_id}")


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

    logger.info(f"Downloading result from {json_url}...")
    resp = requests.get(json_url, timeout=180, verify=cfg.api_url.startswith("https"))
    if resp.status_code != 200:
        raise RuntimeError(f"Download failed HTTP {resp.status_code}")

    raw = resp.text
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
    return pages


def run_ocr(pdf_path: str) -> list[dict]:
    """End-to-end OCR: submit → poll → download. Returns list of page results."""
    job_id = submit_pdf(pdf_path)
    poll_response = poll_job(job_id)
    return download_result(poll_response)
