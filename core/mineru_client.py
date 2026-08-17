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
import os
import re
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from config import config
from logging_config import ocr_job_id_var, JobIdFilter

logger = logging.getLogger(__name__)

# robustness-F1: job_id 透传 — 与 core/ocr_client.py 相同机制：共享
# logging_config.ocr_job_id_var（ContextVar）+ JobIdFilter 给本模块所有日志
# 加 [job_id] 前缀。pipeline 在 to_thread 前 set，排障时按 job 反查完整
# MinerU 流程（上传/轮询/下载/页拆分）；分片 worker 线程不复刻 context，
# 由 run_ocr_sliced 在 _ocr_one 内显式 set。
logger.addFilter(JobIdFilter())

POLL_INTERVAL = 5  # 秒
POLL_TIMEOUT = 1800  # 30 分钟（批记录 PDF 较大，MinerU 解析耗时较长）

# MinerU API 入口 — 默认官方地址；私有化部署/代理场景可用
# MINERU_BASE_URL 覆盖（对抗审查 cr-17：此前硬编码无法配置）。
_API_BASE = os.getenv("MINERU_BASE_URL", "https://mineru.net/api/v4").rstrip("/")


def _headers() -> dict:
    """构建认证请求头。"""
    token = config["mineru"].token
    if not token:
        raise RuntimeError(
            "MinerU token 未配置，请在设置页面配置 MINERU_TOKEN "
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
            "MinerU token 未配置，请在设置页面配置 MINERU_TOKEN "
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
    logger.info("[MinerU] 上传文件到 OSS...")
    with open(pdf_path, "rb") as f:
        upload_resp = requests.put(upload_url, data=f, timeout=300)
    if upload_resp.status_code != 200:
        raise RuntimeError(
            f"文件上传失败 HTTP {upload_resp.status_code}: {upload_resp.text[:300]}"
        )
    logger.info("[MinerU] 文件上传完成, 等待系统自动提交解析任务...")

    return batch_id, pdf_name


def poll_job(batch_id: str, progress_callback=None) -> dict:
    """轮询批次结果直到全部完成。

    MinerU 批量上传后用 batch_id 查询整体进度。
    返回 extract_result 列表中的第一个（我们只上传了一个文件）。

    progress_callback(done, total): 每次轮询到 extract_progress 时回调，
    供 pipeline 实时更新 job 进度（Stage 1 流式反馈）。
    """
    start = time.time()
    url = f"{_API_BASE}/extract-results/batch/{batch_id}"
    consecutive_errors = 0

    while (time.time() - start) < POLL_TIMEOUT:
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
        except requests.RequestException as e:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询请求异常, 重试: {e}")
            if consecutive_errors >= 5:
                raise RuntimeError(f"MinerU poll failed: 5 consecutive network errors: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询 HTTP {resp.status_code}, 重试...")
            if consecutive_errors >= 5:
                raise RuntimeError(f"MinerU poll failed: 5 consecutive HTTP {resp.status_code}")
            time.sleep(POLL_INTERVAL)
            continue

        # P-W6 修复：响应可能非 JSON（HTML 错误页、空响应、网关拦截页等），
        # resp.json() 会抛 ValueError/json.JSONDecodeError，未被 except
        # requests.RequestException 捕获 → 轮询中断。这里显式捕获并重试。
        try:
            j = resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            consecutive_errors += 1
            logger.warning(
                f"[MinerU] 轮询返回非 JSON 响应 (status={resp.status_code}): "
                f"{resp.text[:200]} (parse_err: {e})"
            )
            if consecutive_errors >= 5:
                raise RuntimeError("MinerU poll failed: 5 consecutive non-JSON responses")
            time.sleep(POLL_INTERVAL)
            continue
        if j.get("code") != 0:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询返回错误码: {j.get('msg')}")
            if consecutive_errors >= 5:
                raise RuntimeError(f"MinerU poll failed: 5 consecutive error codes: {j.get('msg')}")
            time.sleep(POLL_INTERVAL)
            continue

        consecutive_errors = 0

        data = j.get("data", {})
        extract_result = data.get("extract_result", [])
        if not extract_result:
            logger.info("[MinerU] 批次处理中, 等待任务创建...")
            time.sleep(POLL_INTERVAL)
            continue

        # 我们只上传一个文件，取第一个结果
        task = extract_result[0]
        state = str(task.get("state") or "").lower()
        task_id = task.get("task_id", "?")

        # 提取进度（running 时有效）+ 实时回调（Stage 1 流式反馈）
        progress = task.get("extract_progress") or {}
        extracted = progress.get("extracted_pages", "?")
        total = progress.get("total_pages", "?")
        if progress_callback and isinstance(extracted, int) and isinstance(total, int):
            progress_callback(extracted, total)
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

    # 对抗审查（cr-16）：zip_url 是签名 CDN 地址，query 可能带签名 token —
    # 日志只记 pathname（pipeline.log 可查排障）。
    from urllib.parse import urlsplit
    logger.info(f"[MinerU] 下载结果 zip: {urlsplit(zip_url).path[:80]}...")
    resp = requests.get(zip_url, timeout=300, verify=True)
    if resp.status_code != 200:
        raise RuntimeError(f"[MinerU] 下载 zip 失败 HTTP {resp.status_code}")

    # 解压 zip
    pages: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        logger.info(f"[MinerU] zip 内容: {names}")

        # 优先 content_list_v2.json（新版按页分组结构，信息完整），
        # 降级 content_list.json（旧版扁平结构）。注意 endswith 匹配
        # "content_list.json" 不会命中 "_v2.json" 文件名，必须显式分开找。
        content_list_name = (
            next((n for n in names if n.endswith("content_list_v2.json")), None)
            or next((n for n in names if n.endswith("content_list.json")), None)
        )
        full_md_name = next((n for n in names if n.endswith("full.md")), None)

        if content_list_name:
            pages = _split_pages_by_content_list(zf, content_list_name)
            # 完整性对照（OCR 鲁棒性）：content_list 结构化解析若丢失大部分
            # 内容（格式漂移/未知块类型/服务端降级），静默输出残缺页会让
            # 下游 LLM 拿到残缺信息。用 full.md（MinerU 官方合并文本）做
            # 兜底对照：逐页文本总量 < full.md 总量 50% 时回退 full.md 拆分。
            full_md_name = next((n for n in names if n.endswith("full.md")), None)
            if full_md_name:
                full_md = zf.read(full_md_name).decode("utf-8", errors="replace")
                extracted_total = sum(len(p["markdown"]["text"]) for p in pages)
                if len(full_md.strip()) > 0 and extracted_total < len(full_md) * 0.5:
                    logger.warning(
                        f"[MinerU] content_list 提取不完整 "
                        f"({extracted_total} < {len(full_md) * 0.5:.0f} chars of full.md), "
                        f"回退 full.md 拆分 — 防止静默丢失页面内容"
                    )
                    pages = _split_pages_by_separator(full_md)
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
    """用 content_list 按页分组，保留整页结构（表格 HTML + 正文）。

    优先 content_list_v2.json，其结构为按页分组的二维数组
    [[page1_blocks], [page2_blocks], ...]，块类型为
    page_header / page_footer / page_number / paragraph / table / title
    / image / equation_interline / page_aside_text 等，文本位于
    content.<type>_content[].content 嵌套结构中。

    降级：content_list.json（v1）为扁平数组，块带 page_idx 字段，
    表格内容在 table_body 字段（HTML），文本在 text 字段。

    整页解析策略（减少噪音，对 LLM 友好）：
    1. 表格块保留 HTML（page_analyzer 的 prompt 按 HTML 表格设计），
       不再丢失 56 个表格的内容
    2. 页脚/页码块（footer / page_footer / page_number）过滤 —
       批记录页脚是 "15.60%"、页码 "2/24" 之类的重复噪音
    3. 页眉/标题块保留为 markdown 标题（含起草/审核日期信息）
    4. 图片/公式块保留占位提示，让 LLM 知道此处有图
    5. 每页开头添加 "## 第 N 页" 标题，便于 LLM 理解页面边界
    """
    raw = zf.read(name).decode("utf-8", errors="replace")
    try:
        blocks = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"[MinerU] content_list.json 解析失败: {e}")

    # v2 格式：按页分组的二维数组，索引即页号（0-based）
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], list):
        pages = []
        for i, group in enumerate(blocks):
            text, discarded_count = _compose_page_markdown(
                i + 1, [b for b in group if isinstance(b, dict)]
            )
            page_dict = {
                "markdown": {"text": text},
                "page_count": i + 1,
                "_source": "mineru",
            }
            if discarded_count > 0:
                # 暴露页级 OCR 完整性信息，pipeline 用于 UI 警告 + LLM 降级提示
                page_dict["_discarded_count"] = discarded_count
            pages.append(page_dict)
        return pages

    # v1 格式：扁平数组 + page_idx 字段
    if not isinstance(blocks, list):
        raise RuntimeError(f"[MinerU] content_list.json 不是数组: {type(blocks)}")

    # 按 page_idx 分组（page_idx 从 0 开始），保留块顺序
    page_blocks: dict[int, list[dict]] = {}
    max_page = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        page_idx = block.get("page_idx", 0)
        if not isinstance(page_idx, int):
            page_idx = 0
        page_blocks.setdefault(page_idx, []).append(block)
        if page_idx > max_page:
            max_page = page_idx

    # 生成连续页（1-based，与 pipeline 一致）
    pages = []
    for i in range(max_page + 1):
        blocks_for_page = page_blocks.get(i, [])
        text, discarded_count = _compose_page_markdown(i + 1, blocks_for_page)
        page_dict = {
            "markdown": {"text": text},
            "page_count": i + 1,
            "_source": "mineru",
        }
        if discarded_count > 0:
            page_dict["_discarded_count"] = discarded_count
        pages.append(page_dict)
    return pages


# 句末标点 — 用于判断 text 块是否构成完整段落
_SENTENCE_END = set("。！？.!?;；")


def _compose_page_markdown(page_num: int, blocks: list[dict]) -> tuple[str, int]:
    """将同一页的所有块组合为整页 Markdown 文本。

    段落合并规则：
    - 连续的 text 块视为同一段落，用空格连接
    - 遇到句末标点（。！？.!?）时，结束当前段落并换行
    - 表格/图片/标题块前后自动加空行，与正文分隔
    - 每页开头添加 "## 第 N 页" 标题

    Returns:
        (markdown_text, discarded_count)：discarded_count 为本页因低置信度
        被 MinerU 丢弃的块数，供上游生成"OCR 不完整"警告。
    """
    if not blocks:
        return f"<!-- 第 {page_num} 页 -->\n\n（此页无文本内容）", 0

    parts: list[str] = [f"<!-- 第 {page_num} 页 -->"]
    current_paragraph: list[str] = []
    discarded_count = 0

    def flush_paragraph():
        """把当前累积的 text 块合并为一个段落并输出。"""
        if current_paragraph:
            # 用空格连接同一段落内的 text 块（MinerU 可能把一个段落拆成多块）
            para = " ".join(current_paragraph).strip()
            if para:
                parts.append(para)
            current_paragraph.clear()

    for block in blocks:
        btype = block.get("type", "text")
        if btype in ("discarded", "discard") or block.get("discard") is True:
            # 低置信度丢弃块：不进入正文，但计数供 OCR 不完整警告
            discarded_count += 1
            continue
        content = _block_to_markdown(block)
        if not content:
            continue

        if btype == "text":
            # 文本块：累积到当前段落
            current_paragraph.append(content)
            # 句末标点 → 结束当前段落
            if content and content[-1] in _SENTENCE_END:
                flush_paragraph()
        elif btype == "table":
            # 表格块：先结束当前段落，表格前后加空行
            flush_paragraph()
            parts.append("")
            parts.append(content)
            parts.append("")
        elif btype in ("image", "equation"):
            # 图片/公式：先结束当前段落，占位提示独占一行
            flush_paragraph()
            parts.append(content)
        else:
            # heading 等其他类型：先结束当前段落，独占一行
            flush_paragraph()
            parts.append(content)

    # 输出最后一个段落
    flush_paragraph()

    return "\n".join(parts).strip(), discarded_count


def _block_to_markdown(block: dict) -> str:
    """将 content_list 的单个块转为 Markdown 文本。

    同时支持 v1（扁平 + page_idx）与 v2（按页分组 + content 嵌套）格式：
    - table: 取 table_body / content.html（HTML 表格，page_analyzer 的
      prompt 按 HTML 设计，保留原始结构对 LLM 最友好）
    - text/paragraph: 取 text 或 content.paragraph_content[].content
    - header/page_header: 保留为 markdown 三级标题（含起草/审核日期信息）
    - title: 保留为 markdown 二级标题
    - footer/page_footer/page_number: 过滤 — 批记录页脚/页码是重复噪音
      （"15.60%"、"2/24"），LLM 无需看到
    - image/equation: 保留占位提示，让 LLM 知道此处有图
    """
    btype = block.get("type", "text")

    # 页脚/页码块：选择性保留（对抗审查 cr-17）— 批记录页脚常含
    # 文件编号（如"文件编号：SOP-001-R3"）、版本号、公司名等跨页规则
    # 数据基础。纯数字/页码/百分比（"2/24"、"15.60%"）是重复噪音丢弃；
    # 含文字（汉字/字母）的页脚保留为普通文本行，供跨页一致性分析。
    if btype in ("footer", "page_footer", "page_number"):
        txt = _content_text(block) or (block.get("text") or "").strip()
        if not txt:
            return ""
        compact = txt.replace(" ", "").replace("\u00a0", "")
        if re.fullmatch(r"[\d./%\u00b0\-()]{1,20}", compact):
            return ""  # 纯数字/符号噪音
        if re.fullmatch(r"第\s*\d+\s*页", txt) or re.fullmatch(r"\d+\s*/\s*\d+", txt):
            return ""  # "第 2 页" / "2/24" 页码模式
        return txt  # 含文字的页脚保留

    # 低置信度丢弃块：MinerU 因置信度过低丢弃的内容（type="discarded" 或
    # discard 标记），不放入正文——否则 LLM 会把残缺片段当正常文本分析。
    # 由 _compose_page_markdown 计数，用于页面 OCR 不完整警告。
    if btype in ("discarded", "discard") or block.get("discard") is True:
        return ""

    if btype == "table":
        html = _table_html(block)
        return html if html else ""

    if btype == "paragraph":
        txt = _content_text(block) or (block.get("text") or "").strip()
        return txt

    if btype in ("header", "page_header"):
        txt = _content_text(block) or (block.get("text") or "").strip()
        return f"### {txt}" if txt else ""

    if btype == "title":
        txt = _content_text(block) or (block.get("text") or "").strip()
        return f"## {txt}" if txt else ""

    if btype in ("image", "equation", "equation_interline"):
        # 图片/公式块：保留占位提示，便于 LLM 知道此处有图。
        # caption 来源：v2 嵌套 content > 顶层 caption 字段 > 顶层 text 字段
        caption = (
            _content_text(block)
            or (block.get("caption") or "").strip()
            or (block.get("text") or "").strip()
        )
        return f"[{btype}: {caption}]" if caption else f"[{btype}]"

    if btype in ("page_aside_text", "aside_text"):
        return _content_text(block) or (block.get("text") or "").strip()

    # 其他类型：尽力提取（不静默丢内容 — 服务端可能引入新块类型）
    extracted = _content_text(block) or (block.get("text") or "").strip()
    if not extracted:
        # 兜底：递归捞取块内全部字符串内容字段（格式漂移时保底）。
        # 跳过元数据键（type/bbox/score 等机器字段），否则最小块
        # {"type": "text"} 会被捞出 "text" 当作正文。
        _META_KEYS = ("type", "bbox", "poly", "score", "conf", "idx", "id", "page_", "line_")
        parts = []

        def _collect_str(obj):
            if isinstance(obj, str):
                s = obj.strip()
                if s and s not in _SENTENCE_END and len(s) > 1:
                    parts.append(s)
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if k.startswith(_META_KEYS):
                        continue
                    _collect_str(v)
            elif isinstance(obj, list):
                for item in obj:
                    _collect_str(item)

        _collect_str(block)
        extracted = " ".join(parts).strip()
    return extracted


def _table_html(block: dict) -> str:
    """提取表格 HTML：v2 在 content.html，v1 在 table_body 字段。

    多路兜底（格式漂移防护）：content.html → content.table_html →
    content.table_body → content.table_markdown → 顶层 table_body /
    markdown / html 字段。
    """
    content = block.get("content")
    if isinstance(content, dict):
        for key in ("html", "table_html", "table_body", "table_markdown"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key, val in content.items():
            if isinstance(val, str) and val.strip() and ("<tr" in val or "<td" in val):
                return val.strip()
    body = block.get("table_body")
    if isinstance(body, str) and body.strip():
        return body.strip()
    md = block.get("markdown")
    if isinstance(md, str) and md.strip():
        return md.strip()
    html = block.get("html")
    if isinstance(html, str) and html.strip():
        return html.strip()
    return ""


def _content_text(block: dict) -> str:
    """递归提取 v2 嵌套结构 content.<type>_content[].content 中的文本。

    例：{"content": {"paragraph_content": [{"type": "text", "content": "..."}]}}
    叶子节点的 content 是 str，需直接返回；中间节点是 dict/list 递归
    （superscript / italic 等行内块在真实 v2 结果中会再嵌套一层）。
    """
    content = block.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # content 本身是列表（superscript 等行内块：{"content": [{...}]}）
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = _content_text(item)
                if t:
                    parts.append(t)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts).strip()
    if not isinstance(content, dict):
        return ""
    parts: list[str] = []
    for key, val in content.items():
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    t = _content_text(item)
                    if t:
                        parts.append(t)
                elif isinstance(item, str):
                    parts.append(item)
        elif isinstance(val, str):
            parts.append(val)
    return " ".join(parts).strip()


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


def run_ocr(pdf_path: str, progress_callback=None, job_id: str = "") -> list[dict]:
    """端到端 MinerU 解析: 上传 → 轮询 → 下载 → 按页拆分。

    返回格式与 core.ocr_client.run_ocr 兼容，pipeline.py 可透明替换。
    progress_callback 透传给 poll_job（Stage 1 实时进度）。
    job_id: 应用层 job id — 仅用于日志前缀（本模块日志自动带 [job_id]）。
    """
    if job_id:
        _token = ocr_job_id_var.set(job_id)
    try:
        batch_id, _ = submit_pdf(pdf_path)
        task_result = poll_job(batch_id, progress_callback=progress_callback)
        return download_result(task_result)
    finally:
        if job_id:
            ocr_job_id_var.reset(_token)


def split_pdf(pdf_path: str, slice_pages: int) -> tuple[int, list[tuple[int, str]]]:
    """按 slice_pages 页/片拆分 PDF 为临时单文件切片。

    Args:
        pdf_path: 源 PDF 路径
        slice_pages: 每片页数（>=1）

    Returns:
        (total_pages, [(start_page_1based, tmp_path), ...])
        切片文件位于临时目录，调用方用完后应调用 cleanup_slices() 清理。
    """
    import fitz  # PyMuPDF — 拆页

    doc = fitz.open(pdf_path)
    try:
        total = doc.page_count
        tmp_dir = Path(tempfile.mkdtemp(prefix="pbc_slice_"))
        slices: list[tuple[int, str]] = []
        for start in range(0, total, slice_pages):
            end = min(start + slice_pages, total)
            out = tmp_dir / f"pages_{start + 1}_{end}.pdf"
            pdoc = fitz.open()
            pdoc.insert_pdf(doc, from_page=start, to_page=end - 1)
            pdoc.save(out)
            pdoc.close()
            slices.append((start + 1, str(out)))
    finally:
        doc.close()
    return total, slices


def cleanup_slices(slices: list[tuple[int, str]]) -> None:
    """删除 split_pdf 生成的临时切片文件及其目录。"""
    dirs = {str(Path(p).parent) for _, p in slices}
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def run_ocr_pages(
    pdf_path: str,
    page_nums: list[int],
    batch_size: int = 3,
    job_id: str = "",
) -> list[tuple[int, str]]:
    """对指定页号做小批量切片重跑 OCR（OCR 完整性抗挫折）。

    背景：MinerU 服务端处理超大 PDF（百 MB 级、数十页）时存在丢页缺陷 —
    同一页在大文件里输出空内容、切成小 PDF 后完整识别（51 页实测丢 6 页，
    6 页/单页切片全部完整）。整册重跑成本高且不可控，小切片逐页重跑
    是确定性修复路径。

    Args:
        pdf_path: 源 PDF 路径
        page_nums: 需要重跑的 1-based 页号列表
        batch_size: 每批页数（每批一个独立 MinerU 任务，3 页 ~15-40s）
        job_id: 日志前缀（透传 run_ocr）

    Returns:
        [(page_num, markdown_text), ...]，保持 page_nums 顺序；
        重跑后仍为空（服务端也识别不了）时返回原空文本。
    """
    import fitz  # PyMuPDF — 切片 & 合并

    results: list[tuple[int, str]] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="pbc_retry_"))

    def _ocr_single(pno: int) -> str:
        # 单页独立切片 OCR — P1-2/P2-1 的兜底路径
        pdoc = fitz.open()
        try:
            if 1 <= pno <= src_doc.page_count:
                pdoc.insert_pdf(src_doc, from_page=pno - 1, to_page=pno - 1)
            if pdoc.page_count == 0:
                return ""
            tmp = tmp_dir / f"single_{pno}.pdf"
            pdoc.save(tmp)
        finally:
            pdoc.close()
        try:
            spages = run_ocr(str(tmp), job_id=job_id)
            return spages[0].get("markdown", {}).get("text", "") if spages else ""
        except Exception:
            return ""  # 单页也失败 → 该页保持空，交由外层空页逻辑兜底

    try:
        src_doc = fitz.open(pdf_path)
        try:
            for start in range(0, len(page_nums), batch_size):
                batch = page_nums[start : start + batch_size]
                pdoc = fitz.open()
                try:
                    for pno in batch:
                        if 1 <= pno <= src_doc.page_count:
                            pdoc.insert_pdf(src_doc, from_page=pno - 1, to_page=pno - 1)
                    if pdoc.page_count == 0:
                        for pno in batch:
                            results.append((pno, ""))
                        continue
                    tmp = tmp_dir / f"slice_{batch[0]}_{batch[-1]}.pdf"
                    pdoc.save(tmp)
                finally:
                    pdoc.close()
                try:
                    pages = run_ocr(str(tmp), job_id=job_id)
                except Exception as e:
                    # P2-1：单批网络/服务抖动不再中断整条重试链 —
                    # 该批退回单页重试，避免此前的"已恢复页不落库、后续批不执行"
                    logger.warning(f"[{job_id}] OCR batch {batch} failed: {e} — falling back to single pages")
                    for pno in batch:
                        results.append((pno, _ocr_single(pno)))
                    continue
                if len(pages) == len(batch):
                    for i, pno in enumerate(batch):
                        text = pages[i].get("markdown", {}).get("text", "") if i < len(pages) else ""
                        results.append((pno, text or ""))
                else:
                    # P1-2：服务端返回页数 != 请求页数 → 按数组下标写会把
                    # 第 N 页内容张冠李戴到第 M 页。退回逐页独立重跑保证页号
                    # 与内容严格一一对应（该函数本就为服务端丢页而生）。
                    logger.warning(
                        f"[{job_id}] OCR batch {batch} returned {len(pages)} pages "
                        f"(expected {len(batch)}) — re-OCR each page standalone"
                    )
                    for pno in batch:
                        results.append((pno, _ocr_single(pno)))
        finally:
            src_doc.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


def run_ocr_sliced(
    pdf_path: str,
    slice_pages: int,
    on_batch=None,
    progress_callback=None,
    job_id: str = "",
) -> list[tuple[int, list[dict]]]:
    """分片并行 OCR — 流式输出（问题 2 核心）。

    把 PDF 切成多片，每片独立提交 MinerU batch 并并行轮询；**一片完成
    立即回调 on_batch(start_page, pages)**（不等其他片），让 pipeline 可以
    "第一片 OCR 完成即开始该片页面的 LLM 分析"，用户无需等全部 51 页
    OCR 完成才看到任何结果。

    Args:
        pdf_path: 源 PDF
        slice_pages: 每片页数
        on_batch: 每片完成时回调 on_batch(start_page_1based, pages)。
            pages 内每页 page_count 已重映射为全局页号（非片内页号）。
        progress_callback: 全局进度回调 (done_global, total_global)，
            由每片的 poll_job 进度合并而来。
        job_id: 应用层 job id — 仅用于日志前缀（线程池内 ContextVar
            自动继承，每片日志统一带 [job_id]）。

    Returns:
        [(start_page, pages), ...]（按提交顺序）。pages 元素结构同
        run_ocr 的返回值（{"markdown": {...}, "page_count": 全局页号}）。

    Raises:
        单片提交/轮询/下载失败时向上抛出（与整份 run_ocr 语义一致）。
    """
    if job_id:
        _token = ocr_job_id_var.set(job_id)
    try:
        total, slices = split_pdf(pdf_path, slice_pages)
    finally:
        if job_id:
            ocr_job_id_var.reset(_token)
    n = len(slices)
    results: list = [None] * n
    try:
        with ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
            def _ocr_one(index: int, start_page: int, path: str):
                # worker 线程不继承 ContextVar（ThreadPoolExecutor 不复刻
                # 调用方 context），显式 set 保证该片日志带 [job_id] 前缀
                if job_id:
                    ocr_job_id_var.set(job_id)
                # 片内进度 → 全局进度（start_page 是全局 1-based 页号）
                def _cb(done: int, total_in_slice: int):
                    if progress_callback:
                        progress_callback(start_page - 1 + done, total)

                batch_id, _ = submit_pdf(path)
                task = poll_job(batch_id, progress_callback=_cb)
                pages = download_result(task)
                # page_count 重映射为全局页号（download_result 返回片内 1-based）
                for p in pages:
                    p["page_count"] = start_page + p["page_count"] - 1
                return start_page, pages

            futures = {
                pool.submit(_ocr_one, i, s, p): i for i, (s, p) in enumerate(slices)
            }
            # as_completed：一片完成立即回调（不等待全部片）
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    start_page, pages = fut.result()
                except Exception as e:
                    # P1-1 修复：单片失败不再让整份分片 OCR 上抛（整单 error）。
                    # 记录并继续等待其余片 — 缺失页由 pipeline 端缺页补记
                    # 并入 failed_pages，job 降级 partial_review 而非 error。
                    logger.warning(
                        f"[{job_id}] Slice {idx + 1} OCR failed: {e} — "
                        f"continuing with remaining slices"
                    )
                    continue
                results[idx] = (start_page, pages)
                if on_batch:
                    # 对抗审查 P0-1：原实现只传 (start_page, pages) 两参，
                    # pipeline 端签名是 (start_page, pages, total) → 第一片
                    # 完成即 TypeError → 分片路径 100% 失败（测试 mock 掩盖）。
                    # total 由 split_pdf 返回值闭包捕获，与单页切片语义一致。
                    on_batch(start_page, pages, total)
    finally:
        cleanup_slices(slices)
    return results
