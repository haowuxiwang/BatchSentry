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
from core.security import redact_urls
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
# T2.2：改为运行时动态读取（Settings 页保存 MINERU_BASE_URL 后
# 立即生效，无需重启；此前是模块导入期常量）。
_DEFAULT_API_BASE = "https://mineru.net/api/v4"


def _api_base() -> str:
    """当前 MinerU API 入口（尾斜杠已剥离）。"""
    url = os.getenv("MINERU_BASE_URL", _DEFAULT_API_BASE).strip()
    return url.rstrip("/")


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
        f"{_api_base()}/file-urls/batch",
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
        raise RuntimeError(
            "申请上传链接失败 HTTP "
            f"{resp.status_code}: {redact_urls(resp.text[:300])}"
        )

    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(
            f"申请上传链接失败: {redact_urls(str(result.get('msg', result)))}"
        )

    data = result["data"]
    batch_id = data["batch_id"]
    file_urls = data["file_urls"]
    if not file_urls:
        raise RuntimeError(f"未返回上传 URL: {redact_urls(str(data))}")

    upload_url = file_urls[0]
    logger.info(f"[MinerU] 上传链接获取成功, batch_id={batch_id}")

    # Step 2: PUT 上传文件（注意：上传时不要设置 Content-Type）
    logger.info("[MinerU] 上传文件到 OSS...")
    try:
        with open(pdf_path, "rb") as f:
            upload_resp = requests.put(upload_url, data=f, timeout=300)
    except requests.exceptions.RequestException as e:
        # requests 异常消息可能回显完整签名 URL（MaxRetryError）— 脱敏后抛
        raise RuntimeError(f"文件上传网络错误: {redact_urls(str(e))}")
    if upload_resp.status_code != 200:
        raise RuntimeError(
            "文件上传失败 HTTP "
            f"{upload_resp.status_code}: {redact_urls(upload_resp.text[:300])}"
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
    url = f"{_api_base()}/extract-results/batch/{batch_id}"
    consecutive_errors = 0

    while (time.time() - start) < POLL_TIMEOUT:
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
        except requests.RequestException as e:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询请求异常, 重试: {redact_urls(str(e))}")
            if consecutive_errors >= 5:
                raise RuntimeError(
                    f"MinerU 轮询失败: 连续 5 次网络错误: {redact_urls(str(e))}"
                )
            time.sleep(POLL_INTERVAL)
            continue

        if resp.status_code != 200:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询 HTTP {resp.status_code}, 重试...")
            if consecutive_errors >= 5:
                raise RuntimeError(f"MinerU 轮询失败: 连续 5 次 HTTP {resp.status_code} 状态码异常")
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
                raise RuntimeError("MinerU 轮询失败: 连续 5 次非 JSON 响应")
            time.sleep(POLL_INTERVAL)
            continue
        if j.get("code") != 0:
            consecutive_errors += 1
            logger.warning(f"[MinerU] 轮询返回错误码: {redact_urls(str(j.get('msg')))}")
            if consecutive_errors >= 5:
                raise RuntimeError(
                    f"MinerU 轮询失败: 连续 5 次错误码: {redact_urls(str(j.get('msg')))}"
                )
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
            raise RuntimeError(f"[MinerU] 解析失败 task={task_id}: {redact_urls(str(err))}")

        time.sleep(POLL_INTERVAL)

    raise RuntimeError(f"[MinerU] 轮询超时 ({POLL_TIMEOUT}s) batch={batch_id}")


def download_result(task_result: dict, pdf_path: str = "") -> list[dict]:
    """下载结果 zip 并按页拆分。

    MinerU zip 包含:
      - full.md          全部页合并的 Markdown
      - *_content_list.json  内容块列表，每个块含 page_idx 字段
      - *_layout.json     版面信息
      - *_model.json      模型推理结果

    我们用 content_list.json 的 page_idx 字段按页分组，生成每页的
    Markdown 文本，返回与 PaddleOCR 兼容的格式。

    pdf_path 用于降级拆分时的页数校验（P0-2）：full.md 无分页符且
    PDF 实际多页 → 抛错触发 failover，而非并 1 页丢内容。
    """
    zip_url = task_result.get("full_zip_url")
    if not zip_url:
        raise RuntimeError(
            f"[MinerU] 结果中无 full_zip_url: {redact_urls(str(task_result))}"
        )

    # P0-2：降级拆分的期望页数（fitz 读物理页数；失败则不校验）
    expected_pages: int | None = None
    if pdf_path:
        try:
            import fitz

            with fitz.open(pdf_path) as _doc:
                expected_pages = _doc.page_count
        except Exception:
            expected_pages = None

    # 对抗审查（cr-16）+ P1-7：zip_url 是签名 CDN 地址，query 可能带签名
    # token — 日志只记 pathname；异常消息同样脱敏（requests 网络异常会
    # 回显完整 URL，冒泡进 jobs.error_message → 报告/通知反刍泄露）。
    from urllib.parse import urlsplit
    logger.info(f"[MinerU] 下载结果 zip: {urlsplit(zip_url).path[:80]}...")
    try:
        resp = requests.get(zip_url, timeout=300, verify=True)
    except requests.RequestException as e:
        raise RuntimeError(f"[MinerU] 下载 zip 网络错误: {redact_urls(str(e))}") from e
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
            pages, n_tables, n_paragraphs = _split_pages_by_content_list(
                zf, content_list_name
            )
            # 完整性对照（OCR 鲁棒性，P1-4 重构）：content_list 结构化解析若
            # 丢失大部分内容（格式漂移/未知块类型/服务端降级），静默输出残缺
            # 页会让下游 LLM 拿到残缺信息。用 full.md（MinerU 官方合并文本）
            # 按"结构维度"兜底对照 — 字符数比值对照不可靠：表格保留为 HTML
            # 时标签膨胀改变总量（50% 阈值既误报又漏检表格丢失），改为三个
            # 高信号结构信号：页数差 / 表格整体丢失 / 正文表格全丢。
            full_md_name = next((n for n in names if n.endswith("full.md")), None)
            if full_md_name:
                full_md = zf.read(full_md_name).decode("utf-8", errors="replace")
                violated, reason = _structural_completeness_violated(
                    full_md, len(pages), n_tables, n_paragraphs
                )
                if violated:
                    logger.warning(
                        f"[MinerU] content_list 结构不完整（{reason}），"
                        f"回退 full.md 拆分 — 防止静默丢失页面内容"
                    )
                    pages = _split_pages_by_separator(full_md, expected_pages)
        elif full_md_name:
            # 降级：content_list 不存在时，按分页符拆分 full.md
            logger.warning(
                "[MinerU] 未找到 content_list.json, 降级按分页符拆分 full.md"
            )
            full_md = zf.read(full_md_name).decode("utf-8", errors="replace")
            pages = _split_pages_by_separator(full_md, expected_pages)
        else:
            raise RuntimeError(
                f"[MinerU] zip 中未找到 content_list.json 或 full.md: {names}"
            )

    if not pages:
        raise RuntimeError("[MinerU] 按页拆分后未得到任何页面")
    logger.info(f"[MinerU] 按页拆分完成: {len(pages)} 页")
    return pages


def _structural_completeness_violated(
    full_md: str, n_pages: int, n_tables: int, n_paragraphs: int
) -> tuple[bool, str]:
    """结构维度完整性对照（P1-4）— 判定 content_list 是否丢失大部分内容。

    背景：旧实现用"提取字符数 < full.md 50%"做对照，但 content_list 的
    表格保留为 HTML（LLM 友好），标签膨胀使字符总量失真 — 阈值既不触发
    （表格多时 HTML 总量反超），也漏检表格整体丢失。改为三个高信号
    结构指标（任一命中即残缺）：

    1. 页数差：content_list 分组页数显著少于 full.md 分页数（≥2 页，
       ±1 容差分页符解析抖动）
    2. 表格整体丢失：content_list 0 个表格 而 full.md 含 ≥2 行管道表
       （批记录的核心信息在表格；全丢 = 灾难。管道表最少 2 行 — 表头+
       数据，单行管道基本只可能是表内一行，回退 full.md 代价低、召回优先）
    3. 正文+表格全丢：表格与段落都没有（只剩页眉/页脚/页码等噪声块），
       而 full.md 有实质内容
    """
    full_stripped = full_md.strip()
    if not full_stripped:
        return False, ""
    md_pages = len(_split_pages_by_separator(full_md))
    pipe_rows = sum(1 for ln in full_md.splitlines() if ln.strip().startswith("|"))
    if md_pages >= 1 and n_pages < md_pages - 1:
        return True, f"页数缺失（content_list {n_pages} 页 < full.md {md_pages} 页）"
    if n_tables == 0 and pipe_rows >= 2:
        return True, (
            f"表格全部丢失（content_list 0 个表格，"
            f"full.md 含 {pipe_rows} 行管道表）"
        )
    if n_paragraphs == 0 and pipe_rows == 0:
        return True, "正文与表格均未提取（content_list 仅剩噪声块）"
    return False, ""


def _split_pages_by_content_list(
    zf: zipfile.ZipFile, name: str
) -> tuple[list[dict], int, int]:
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

    Returns:
        (pages, n_tables, n_paragraphs) — 页面列表 + 结构统计
        （表格/段落块数量，供完整性对照用）
    """
    raw = zf.read(name).decode("utf-8", errors="replace")
    try:
        blocks = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"[MinerU] content_list.json 解析失败: {e}")

    # v2 格式：按页分组的二维数组，索引即页号（0-based）
    if isinstance(blocks, list) and blocks and isinstance(blocks[0], list):
        pages = []
        n_tables = n_paragraphs = 0
        for i, group in enumerate(blocks):
            block_dicts = [b for b in group if isinstance(b, dict)]
            for b in block_dicts:
                btype = b.get("type", "text")
                if btype == "table":
                    n_tables += 1
                elif btype in ("paragraph", "text", "title"):
                    n_paragraphs += 1
            text, discarded_count = _compose_page_markdown(i + 1, block_dicts)
            page_dict = {
                "markdown": {"text": text},
                "page_count": i + 1,
                "_source": "mineru",
            }
            if discarded_count > 0:
                # 暴露页级 OCR 完整性信息，pipeline 用于 UI 警告 + LLM 降级提示
                page_dict["_discarded_count"] = discarded_count
            pages.append(page_dict)
        return pages, n_tables, n_paragraphs

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
    n_tables = n_paragraphs = 0
    for i in range(max_page + 1):
        blocks_for_page = page_blocks.get(i, [])
        for b in blocks_for_page:
            btype = b.get("type", "text")
            if btype == "table":
                n_tables += 1
            elif btype in ("paragraph", "text", "title"):
                n_paragraphs += 1
        text, discarded_count = _compose_page_markdown(i + 1, blocks_for_page)
        page_dict = {
            "markdown": {"text": text},
            "page_count": i + 1,
            "_source": "mineru",
        }
        if discarded_count > 0:
            page_dict["_discarded_count"] = discarded_count
        pages.append(page_dict)
    return pages, n_tables, n_paragraphs


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
        return f"## 第 {page_num} 页\n\n（此页无文本内容）", 0

    parts: list[str] = [f"## 第 {page_num} 页"]
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
        if btype == "table":
            # 表格块：先结束当前段落，表格前后加空行
            flush_paragraph()
            parts.append("")
            if content:
                parts.append(content)
            else:
                # P1-2: 表格结构+文本全部提取失败 — 显式占位并计数，
                # 触发 pipeline 的"OCR 不完整"警告注入（此前整表静默丢失）
                discarded_count += 1
                parts.append("[表格内容提取失败 — OCR 结构缺失]")
            parts.append("")
            continue
        if not content:
            continue

        if btype == "text":
            # 文本块：累积到当前段落
            current_paragraph.append(content)
            # 句末标点 → 结束当前段落
            if content and content[-1] in _SENTENCE_END:
                flush_paragraph()
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
        if html:
            return html
        # P1-2: HTML 提取失败不再静默丢整表 — fall through 到下方通用
        # 文本提取（递归捞块内全部字符串），至少保留表格内容；连文本
        # 都没有时由 _compose_page_markdown 计数注入缺失占位 + OCR 警告。

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


def _split_pages_by_separator(
    full_md: str, expected_pages: int | None = None
) -> list[dict]:
    """降级方案：按分页符拆分 full.md。

    MinerU 的 full.md 在页面间通常有 '\\f' (form feed) 分页符。
    若无分页符，则将整个文档作为单页返回。

    P0-2 修复：无分页符且 expected_pages > 1 时抛错而非返回单页 —
    多页文档并成 1 页后，Stage 2 的 12000 字符截断会静默丢弃约 3/4
    内容且页边界全毁（页码与 PDF 原文对不上）。抛错让上层 failover
    到备用 OCR 后端（Paddle），保内容完整性。
    """
    if "\f" in full_md:
        parts = full_md.split("\f")
    elif "\n---\n" in full_md:
        parts = full_md.split("\n---\n")
    else:
        # 无分页符：无法按页拆分
        if expected_pages and expected_pages > 1:
            raise RuntimeError(
                f"[MinerU] full.md 无分页符且 PDF 为 {expected_pages} 页 — "
                f"无法按页拆分（返回单页会导致截断丢内容），请检查 "
                f"MinerU 输出或切换 OCR 后端"
            )
        return [{"markdown": {"text": full_md}, "page_count": 1, "_source": "mineru"}]

    pages = []
    for i, part in enumerate(parts):
        text = part.strip()
        if not text:
            # B2 修复（对抗性审查）：空部分不再跳过 — 空白扫描页/分隔页是
            # GMP 记录常见页，跳过会让后续所有页的 page_count 前移、与 PDF
            # 物理页错位 → 跨页分析页边界错误 + 复核导航对不上 PDF 原文。
            # 保留空页占位（与 _compose_page_markdown 空块页同款文案），
            # 上游空页自愈（切片重试）或 _ocr_empty 人工复核路径兜底。
            text = f"## 第 {i + 1} 页\n\n（此页无文本内容）"
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
        return download_result(task_result, pdf_path=pdf_path)
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
        [(page_num, markdown_text, discarded_count), ...]，保持 page_nums
        顺序；重跑后仍为空（服务端也识别不了）时返回原空文本。
        discarded_count 为该页因置信度过低被丢弃的块数（0 = 无），
        供 pipeline 恢复写库时补回 OCR 不完整警告（与主流程一致，
        D3 修复：此前自愈路径丢失该信息，恢复页被静默当作完整页）。
    """
    import fitz  # PyMuPDF — 切片 & 合并

    results: list[tuple[int, str, int]] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="pbc_retry_"))

    def _ocr_single(pno: int) -> tuple[str, int]:
        # 单页独立切片 OCR — P1-2/P2-1 的兜底路径
        pdoc = fitz.open()
        try:
            if 1 <= pno <= src_doc.page_count:
                pdoc.insert_pdf(src_doc, from_page=pno - 1, to_page=pno - 1)
            if pdoc.page_count == 0:
                return "", 0
            tmp = tmp_dir / f"single_{pno}.pdf"
            pdoc.save(tmp)
        finally:
            pdoc.close()
        try:
            spages = run_ocr(str(tmp), job_id=job_id)
            if spages:
                return (
                    spages[0].get("markdown", {}).get("text", ""),
                    spages[0].get("_discarded_count", 0),
                )
            return "", 0
        except Exception:
            return "", 0  # 单页也失败 → 该页保持空，交由外层空页逻辑兜底

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
                            results.append((pno, "", 0))
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
                        results.append((pno, *_ocr_single(pno)))
                    continue
                if len(pages) == len(batch):
                    for i, pno in enumerate(batch):
                        text = pages[i].get("markdown", {}).get("text", "") if i < len(pages) else ""
                        discarded = (
                            pages[i].get("_discarded_count", 0)
                            if i < len(pages)
                            else 0
                        )
                        results.append((pno, text or "", discarded))
                else:
                    # P1-2：服务端返回页数 != 请求页数 → 按数组下标写会把
                    # 第 N 页内容张冠李戴到第 M 页。退回逐页独立重跑保证页号
                    # 与内容严格一一对应（该函数本就为服务端丢页而生）。
                    logger.warning(
                        f"[{job_id}] OCR batch {batch} returned {len(pages)} pages "
                        f"(expected {len(batch)}) — re-OCR each page standalone"
                    )
                    for pno in batch:
                        results.append((pno, *_ocr_single(pno)))
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
                pages = download_result(task, pdf_path=path)
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
