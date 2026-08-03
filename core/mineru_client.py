"""MinerU 精准解析 API 客户端。

接入 MinerU 作为可选 OCR/解析后端，替代 PaddleOCR-VL。
MinerU 输出 Markdown + 结构化 JSON，对表格/公式的识别精度更高，
且输出 Markdown 格式对下游 LLM 分析更友好（无需 HTML→文本转换）。

流程：
  1. 申请上传链接 (POST /api/v4/file-urls/batch)
  2. PUT 上传本地 PDF 文件
  3. 轮询批次结果 (GET /api/v4/extract-results/batch/{batch_id})
  4. 下载结果 zip，按页拆分 content_list.json
  5. 返回与 paddle ocr_client.run_ocr 兼容的 list[dict]

返回格式（兼容 pipeline.py Stage 1）：
  [{"markdown": {"text": "<每页 markdown>"}, "page_count": N}, ...]
"""
import io
import json
import logging
import time
import zipfile
from pathlib import Path

import requests

from config import config

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # 秒
POLL_TIMEOUT = 1800  # 30 分钟（批记录 PDF 较大，MinerU 解析耗时较长）

_API_BASE = "https://mineru.net/api/v4"


def _headers() -> dict:
    """构建认证请求头。"""
    token = config["mineru"].token
    if not token:
        raise RuntimeError(
            "MinerU token 未配置，请在 .env 中设置 MINERU_TOKEN "
            "（在 https://mineru.net/apiManage 页面创建）"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def submit_pdf(pdf_path: str) -> tuple[str, str]:
    """申请上传链接并上传本地 PDF。

    返回 (batch_id, task_id_hint)。MinerU 批量上传后系统自动提交解析任务，
    无需额外调用提交接口。
    """
    cfg = config["mineru"]
    # Token 检查提前，配置缺失时立即报清晰错误
    if not cfg.token:
        raise RuntimeError(
            "MinerU token 未配置，请在 .env 中设置 MINERU_TOKEN "
            "（在 https://mineru.net/apiManage 页面创建）"
        )

    pdf_name = Path(pdf_path).name
    file_size = Path(pdf_path).stat().st_size

    if file_size > 200 * 1024 * 1024:
        raise RuntimeError(
            f"文件 {file_size/1024/1024:.1f}MB 超过 MinerU 200MB 限制"
        )

    logger.info(
        f"[MinerU] 申请上传链接: {pdf_name} ({file_size/1024/1024:.1f}MB), "
        f"model_version={cfg.model_version}"
    )

    # Step 1: 申请上传链接
    resp = requests.post(
        f"{_API_BASE}/file-urls/batch",
        headers=_headers(),
        json={
            "files": [{"name": pdf_name, "is_ocr": True}],
            "model_version": cfg.model_version,
            "language": cfg.language,
            "enable_formula": cfg.enable_formula,
            "enable_table": cfg.enable_table,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"申请上传链接失败 HTTP {resp.status_code}: {resp.text[:300]}")

    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"申请上传链接失败: {result.get('msg', result)}")

    data = result["data"]
    batch_id = data["batch_id"]
    file_urls = data["file_urls"]
    if not file_urls:
        raise RuntimeError(f"未返回上传 URL: {data}")

    upload_url = file_urls[0]
    logger.info(f"[MinerU] 上传链接获取成功, batch_id={batch_id}")

    # Step 2: PUT 上传文件（注意：上传时不要设置 Content-Type）
    logger.info(f"[MinerU] 上传文件到 OSS...")
    with open(pdf_path, "rb") as f:
        upload_resp = requests.put(upload_url, data=f, timeout=300)
    if upload_resp.status_code != 200:
        raise RuntimeError(
            f"文件上传失败 HTTP {upload_resp.status_code}: {upload_resp.text[:300]}"
        )
    logger.info(f"[MinerU] 文件上传完成, 等待系统自动提交解析任务...")

    return batch_id, pdf_name


def poll_job(batch_id: str) -> dict:
    """轮询批次结果直到全部完成。

    MinerU 批量上传后用 batch_id 查询整体进度。
    返回 extract_result 列表中的第一个（我们只上传了一个文件）。
    """
    start = time.time()
    url = f"{_API_BASE}/extract-results/batch/{batch_id}"

    while (time.time() - start) < POLL_TIMEOUT:
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
        except requests.RequestException as e:
            logger.warning(f"[MinerU] 轮询请求异常, 重试: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            logger.warning(f"[MinerU] 轮询 HTTP {resp.status_code}, 重试...")
            time.sleep(POLL_INTERVAL)
            continue

        j = resp.json()
        if j.get("code") != 0:
            logger.warning(f"[MinerU] 轮询返回错误码: {j.get('msg')}")
            time.sleep(POLL_INTERVAL)
            continue

        data = j.get("data", {})
        extract_result = data.get("extract_result", [])
        if not extract_result:
            logger.info(f"[MinerU] 批次处理中, 等待任务创建...")
            time.sleep(POLL_INTERVAL)
            continue

        # 我们只上传一个文件，取第一个结果
        task = extract_result[0]
        state = str(task.get("state") or "").lower()
        task_id = task.get("task_id", "?")

        # 提取进度（running 时有效）
        progress = task.get("extract_progress") or {}
        extracted = progress.get("extracted_pages", "?")
        total = progress.get("total_pages", "?")
        elapsed = int(time.time() - start)
        logger.info(
            f"[MinerU] task={task_id} state={state} "
            f"pages={extracted}/{total} elapsed={elapsed}s"
        )

        if state == "done":
            logger.info(f"[MinerU] 解析完成 task={task_id}")
            return task
        if state == "failed":
            err = task.get("err_msg", "未知错误")
            raise RuntimeError(f"[MinerU] 解析失败 task={task_id}: {err}")

        time.sleep(POLL_INTERVAL)

    raise RuntimeError(f"[MinerU] 轮询超时 ({POLL_TIMEOUT}s) batch={batch_id}")


def download_result(task_result: dict) -> list[dict]:
    """下载结果 zip 并按页拆分。

    MinerU zip 包含:
      - full.md          全部页合并的 Markdown
      - *_content_list.json  内容块列表，每个块含 page_idx 字段
      - *_layout.json     版面信息
      - *_model.json      模型推理结果

    我们用 content_list.json 的 page_idx 字段按页分组，生成每页的
    Markdown 文本，返回与 PaddleOCR 兼容的格式。
    """
    zip_url = task_result.get("full_zip_url")
    if not zip_url:
        raise RuntimeError(f"[MinerU] 结果中无 full_zip_url: {task_result}")

    logger.info(f"[MinerU] 下载结果 zip: {zip_url[:80]}...")
    resp = requests.get(zip_url, timeout=300, verify=True)
    if resp.status_code != 200:
        raise RuntimeError(f"[MinerU] 下载 zip 失败 HTTP {resp.status_code}")

    # 解压 zip
    pages: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        logger.info(f"[MinerU] zip 内容: {names}")

        # 优先找 content_list.json（含 page_idx，可按页拆分）
        content_list_name = next(
            (n for n in names if n.endswith("content_list.json")), None
        )
        full_md_name = next((n for n in names if n.endswith("full.md")), None)

        if content_list_name:
            pages = _split_pages_by_content_list(zf, content_list_name)
        elif full_md_name:
            # 降级：content_list 不存在时，按分页符拆分 full.md
            logger.warning(
                "[MinerU] 未找到 content_list.json, 降级按分页符拆分 full.md"
            )
            full_md = zf.read(full_md_name).decode("utf-8", errors="replace")
            pages = _split_pages_by_separator(full_md)
        else:
            raise RuntimeError(
                f"[MinerU] zip 中未找到 content_list.json 或 full.md: {names}"
            )

    if not pages:
        raise RuntimeError("[MinerU] 按页拆分后未得到任何页面")
    logger.info(f"[MinerU] 按页拆分完成: {len(pages)} 页")
    return pages


def _split_pages_by_content_list(zf: zipfile.ZipFile, name: str) -> list[dict]:
    """用 content_list.json 的 page_idx 字段按页分组。

    content_list.json 是一个数组，每个元素形如:
      {"type": "text"|"table"|"image"|..., "text"|"markdown"|..., "page_idx": N}

    将同一 page_idx 的块拼接为该页的 Markdown 文本。
    """
    raw = zf.read(name).decode("utf-8", errors="replace")
    try:
        blocks = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"[MinerU] content_list.json 解析失败: {e}")

    if not isinstance(blocks, list):
        raise RuntimeError(f"[MinerU] content_list.json 不是数组: {type(blocks)}")

    # 按 page_idx 分组（page_idx 从 0 开始）
    page_map: dict[int, list[str]] = {}
    max_page = 0
    for block in blocks:
        page_idx = block.get("page_idx", 0)
        if not isinstance(page_idx, int):
            page_idx = 0
        content = _block_to_markdown(block)
        if content:
            page_map.setdefault(page_idx, []).append(content)
            if page_idx > max_page:
                max_page = page_idx

    # 生成连续页（1-based，与 pipeline 一致）
    pages = []
    for i in range(max_page + 1):
        parts = page_map.get(i, [])
        text = "\n\n".join(parts) if parts else ""
        pages.append({
            "markdown": {"text": text},
            "page_count": i + 1,
            "_source": "mineru",
        })
    return pages


def _block_to_markdown(block: dict) -> str:
    """将 content_list 的单个块转为 Markdown 文本。

    MinerU 块类型: text / table / image / equation / heading 等。
    - text: 直接取 text 字段
    - table: 取 markdown/html 字段（优先 markdown）
    - image/equation: 跳过（批记录检查不需要图片/公式渲染）
    """
    btype = block.get("type", "text")
    if btype == "text":
        return block.get("text", "").strip()
    if btype == "table":
        # MinerU 表格输出 markdown 格式，对 LLM 分析非常友好
        return (block.get("markdown") or block.get("text") or "").strip()
    if btype in ("image", "equation"):
        # 图片/公式块：保留占位提示，便于 LLM 知道此处有图
        caption = block.get("text") or block.get("caption") or ""
        return f"[{btype}: {caption}]" if caption else ""
    # 其他类型：尝试取 text 字段
    return (block.get("text") or "").strip()


def _split_pages_by_separator(full_md: str) -> list[dict]:
    """降级方案：按分页符拆分 full.md。

    MinerU 的 full.md 在页面间通常有 '\\f' (form feed) 分页符。
    若无分页符，则将整个文档作为单页返回。
    """
    if "\f" in full_md:
        parts = full_md.split("\f")
    elif "\n---\n" in full_md:
        parts = full_md.split("\n---\n")
    else:
        # 无分页符：无法按页拆分，作为单页返回
        return [{"markdown": {"text": full_md}, "page_count": 1, "_source": "mineru"}]

    pages = []
    for i, part in enumerate(parts):
        text = part.strip()
        if text:
            pages.append({
                "markdown": {"text": text},
                "page_count": i + 1,
                "_source": "mineru",
            })
    return pages if pages else [{"markdown": {"text": full_md}, "page_count": 1, "_source": "mineru"}]


def run_ocr(pdf_path: str) -> list[dict]:
    """端到端 MinerU 解析: 上传 → 轮询 → 下载 → 按页拆分。

    返回格式与 core.ocr_client.run_ocr 兼容，pipeline.py 可透明替换。
    """
    batch_id, _ = submit_pdf(pdf_path)
    task_result = poll_job(batch_id)
    return download_result(task_result)
