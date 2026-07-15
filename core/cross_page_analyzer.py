"""Stage 2 — cross-page analysis.

Takes all per-page structured results and generates findings:
- Time line reconstruction (all steps sorted by start_time)
- Year contradiction detection across pages
- Signature chain consistency (operator/reviewer names)
- Parameter range checks (value vs spec)
"""
import logging
import re
from collections import defaultdict

from llm.client import get_llm_client

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个 GMP 批生产记录合规分析专家。
给定一份批生产记录所有页的结构化数据（每页已提取工序时间、参数、人员），
请分析并找出所有合规性异常。

检查维度：
1. **时间线顺序**：工序 N 开始时间应 ≥ 工序 N-1 结束时间
2. **年份一致性**：同批记录的生产年份应一致，出现多年份是 OCR 误识
3. **签名链**：同一操作人在不同页的姓名应一致
4. **参数范围**：实测值应在规格限内
5. **完整性**：空白单元格无 N/A 标注

输出 JSON 数组，每个 finding 包含：
{{"page":页码,"type":"time_reversal|year_contradiction|signature_mismatch|param_out_of_spec|completeness","severity":"critical|warning|info","description":"问题描述","ocr_text":"原文摘录","operator":"涉及人员"}}"""


async def analyze_cross_page(page_structures: list[dict]) -> list[dict]:
    """Analyze all pages and return findings list."""
    if not page_structures:
        return []

    # Build compact summary for LLM
    summary = _build_summary(page_structures)

    # Rule-based findings (no LLM needed)
    rule_findings = _rule_based_check(page_structures, summary)

    # LLM-based findings (semantic analysis)
    llm_findings = await _llm_based_check(summary)

    all_findings = rule_findings + llm_findings
    logger.info(f"Cross-page analysis: {len(rule_findings)} rule + {len(llm_findings)} LLM findings")
    return all_findings


def _build_summary(page_structures: list[dict]) -> str:
    """Build compact text summary of all pages for LLM consumption."""
    lines = []
    for ps in page_structures:
        data = ps["data"]
        if data.get("_parse_error"):
            continue
        pi = data.get("page_info", {})
        lines.append(f"--- 第{ps['page']}页 ---")
        if pi.get("title"):
            lines.append(f"  标题: {pi['title']}")
        if pi.get("production_date"):
            lines.append(f"  生产日期: {pi['production_date']}")
        if pi.get("batch_no"):
            lines.append(f"  批号: {pi['batch_no']}")
        for step in data.get("steps", []):
            line = f"  步骤{step.get('step_no','?')}: {step.get('operation','')[:60]}"
            if step.get("start_time"):
                line += f" | 开始:{step['start_time']}"
            if step.get("end_time"):
                line += f" | 结束:{step['end_time']}"
            if step.get("operator"):
                line += f" | 操作人:{step['operator']}"
            if step.get("reviewer"):
                line += f" | 复核人:{step['reviewer']}"
            for p in step.get("parameters", []):
                if p.get("value"):
                    line += f" | {p['name']}:{p['value']}{p.get('unit','')}"
            lines.append(line)
        for ta in data.get("time_anomalies", []):
            lines.append(f"  ⚠️ {ta}")
    return "\n".join(lines)


def _rule_based_check(page_structures: list[dict], summary: str) -> list[dict]:
    """Rule-based findings that don't need LLM."""
    findings = []

    # Year consistency check
    year_pattern = re.compile(r"\b(20\d{2})\b")
    page_years: dict[int, set[str]] = {}
    for ps in page_structures:
        data = ps["data"]
        if data.get("_parse_error"):
            continue
        text = str(data)
        years = set(year_pattern.findall(text))
        if years:
            page_years[ps["page"]] = years

    # Gather all production-related years
    all_years = set()
    for years in page_years.values():
        all_years.update(years)

    if len(all_years) > 1:
        # Find which pages have contradictory years
        for page, years in page_years.items():
            non_2025 = years - {"2025"}
            if non_2025 and "2025" not in years:
                findings.append({
                    "page": page,
                    "type": "year_contradiction",
                    "severity": "warning",
                    "description": f"第{page}页年份({','.join(sorted(years))})与主流2025不一致，可能是年份判断需人工确认",
                    "ocr_text": "",
                    "operator": "",
                })

    return findings


async def _llm_based_check(summary: str) -> list[dict]:
    """Use LLM to find semantic anomalies."""
    client = get_llm_client()
    prompt = f"以下是批生产记录跨页摘要:\n\n{summary}\n\n请输出 findings JSON 数组。"
    result = await client.chat_json(
        SYSTEM_PROMPT, prompt, max_tokens=2000, temperature=0.1, timeout=120.0
    )
    if result.get("_parse_error"):
        logger.warning("Cross-page LLM analysis: JSON parse failure")
        return []
    findings = result if isinstance(result, list) else result.get("findings", [])
    return findings
