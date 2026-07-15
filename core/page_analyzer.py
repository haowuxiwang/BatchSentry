"""Stage 1 — per-page LLM analysis.

Given raw HTML table from PaddleOCR-VL, extract structured data:
- page info (title, batch number, production date, ...)
- steps (operations with times, parameters, operators)
- anomalies (time reversals, year contradictions, etc.)
"""
import logging
import re

from llm.client import get_llm_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个 GMP 批生产记录数据提取专家。
给定一页批生产记录的 HTML 表格（OCR 识别产物），请提取结构化数据。

重点提取：
1. 页面标题（岗位/工序名称）
2. 文件编号、版本号、产品批号、生产日期
3. 工序步骤：编号、操作描述、开始/结束时间、参数（名称/规格限/实测值/单位/是否合格）、操作人、复核人
4. 时间异常检测：多年份矛盾、时间非单调递增、未来年份、顺序错误

时间规则：
- KK:MM 格式从页面顶部"生产日期"推断完整日期
- "X日X时X分"需推断缺省年份
- 同页出现多个不同年份 → 标 year_contradiction
- 同工序时间戳非单调递增 → 标 time_reversal
- 年份早于2000或晚于当前年份 → 标 suspicious_year

严格输出 JSON，不要 Markdown 代码包裹。"""

USER_PROMPT_TEMPLATE = """提取以下 HTML 表格中的结构化数据：

```html
{html}
```

输出 JSON 格式：
{{"page_info":{{"title":"","file_code":"","version":"","batch_no":"","production_date":"","batch_no":""}},"steps":[{{"step_no":"","operation":"","start_time":"","end_time":"","parameters":[{{"name":"","spec_range":"","value":"","unit":""}}],"operator":"","reviewer":"","handwritten":[],"anomalies":[]}}],"time_anomalies":[],"ocr_noise":[],"overall_confidence":"high|medium|low"}}"""


async def analyze_page(html: str, page_num: int) -> dict:
    """Analyze a single page's HTML table and return structured data."""
    client = get_llm_client()

    # Pre-process: strip excessive HTML attributes to reduce token count
    cleaned = _clean_html(html)

    prompt = USER_PROMPT_TEMPLATE.format(html=cleaned)
    result = await client.chat_json(
        SYSTEM_PROMPT,
        prompt,
        max_tokens=1500,
        temperature=0.1,
        timeout=90.0,
    )

    # Handle parse failure
    if result.get("_parse_error"):
        logger.warning(f"Page {page_num}: JSON parse failure, returning raw")
        return {
            "page_number": page_num,
            "_parse_error": True,
            "_raw": result.get("_raw", "")[:500],
            "overall_confidence": "low",
        }

    result["page_number"] = page_num
    return result


def _clean_html(html: str) -> str:
    """Reduce HTML token noise: strip style attributes, class names, etc."""
    # Remove style='...'
    cleaned = re.sub(r"\s*style='[^']*'", "", html)
    # Remove width='...'
    cleaned = re.sub(r"\s*width='[^']*'", "", cleaned)
    # Simplify long URLs in img src (keep only filename)
    cleaned = re.sub(r'(src=")[^"]*/([^/"]+)("\s)', r'\1\2\3', cleaned)
    # Collapse excessive whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:6000]  # Truncate to prevent token overflow
