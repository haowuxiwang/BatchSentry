from __future__ import annotations

import logging

from typing import Optional

from llm.client import get_llm_client

logger = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# LLM fallback for params rules could not judge
# ---------------------------------------------------------------------------


_FALLBACK_SYSTEM_PROMPT = """你是 GMP 批生产记录参数合规判定助手。
给定规则层无法判定的参数列表（spec 描述非数字范围，例如"应澄清"、"符合要求"），
请基于 GMP 常识判断 actual 是否符合 spec。

返回 JSON 数组，每个元素：
{"index":1,"in_spec":true|false|null,"reason":"..."}

- in_spec=true  表示合规
- in_spec=false 表示不合规
- in_spec=null  表示无法判定（需人工）

严格输出 JSON 数组，不要添加其他文本。"""


def _flag_llm_queue_for_review(llm_queue: list[dict], *, reason: str) -> list[dict]:
    """Fail-closed: when LLM fallback is unavailable, flag all queued params
    for human review rather than silently passing them (GMP safety principle)."""
    findings = []
    for q in llm_queue:
        findings.append({
            "page": q["page"],
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{q['page']}页 参数 {q['name']}={q['actual']}{q.get('unit','')} "
                f"规格 {q['spec']} 无法自动判定（{reason}），需人工确认"
            ),
            "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
            "operator": "",
            "source": "rule",
        })
    return findings


# LLM fallback 单次调用的最大参数数量。
# 超过此数量时直接 flag 为 human review，避免 prompt 过大导致 LLM 调用超时/重试
# （实测 449 个参数会导致 SiliconFlow API 重试并卡住 Stage 3 数分钟）。
# 50 个参数的单次 LLM 调用 prompt 约 3-4KB，在 4000 max_tokens 内可完成。
_LLM_FALLBACK_BATCH_MAX = 50


async def _llm_fallback_check(llm_queue: list[dict], *, job_id: str = "") -> list[dict]:
    if not llm_queue:
        return []

    # 防止 LLM 调用过载：队列过大时直接 flag 为 human review。
    # 449 个参数的 prompt 会超过 LLM 的处理能力，导致重试和超时。
    # GMP 安全原则：无法自动判定时 flag 为人工复核，而非让 pipeline 卡死。
    if len(llm_queue) > _LLM_FALLBACK_BATCH_MAX:
        logger.warning(
            f"[{job_id}] LLM fallback queue too large ({len(llm_queue)} > "
            f"{_LLM_FALLBACK_BATCH_MAX}), flagging all as human review"
        )
        return _flag_llm_queue_for_review(
            llm_queue,
            reason=f"参数数量过多（{len(llm_queue)}），超出自动判定上限，需人工确认"
        )

    items_text = "\n".join(
        f"{i+1}. 第{q['page']}页 工序{q.get('step_no','?')} "
        f"{q.get('time','') + ' ' if q.get('time') else ''}"
        f"{q['name']} | spec={q['spec']} | actual={q['actual']} | unit={q.get('unit','')}"
        for i, q in enumerate(llm_queue)
    )
    prompt = f"参数列表：\n{items_text}\n\n请逐条判定并返回 JSON 数组。"
    client = get_llm_client()
    try:
        result = await client.chat_json(
            _FALLBACK_SYSTEM_PROMPT, prompt, max_tokens=4000, temperature=0.1, timeout=120.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm_fallback",
                       "prompt_version": "fallback_v1"},
        )
    except Exception as e:
        logger.warning(f"[{job_id}] LLM fallback failed: {e} — flagging {len(llm_queue)} params for human review")
        return _flag_llm_queue_for_review(llm_queue, reason=f"LLM 调用失败: {type(e).__name__}")
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning(f"[{job_id}] LLM fallback: JSON parse failure — flagging {len(llm_queue)} params for human review")
        return _flag_llm_queue_for_review(llm_queue, reason="LLM 返回 JSON 解析失败")
    items = result if isinstance(result, list) else (
        result.get("items", []) if isinstance(result, dict) else []
    )
    findings = []
    for item in items:
        # P1-2: type-polluted fallback output must not crash Stage 3
        if not isinstance(item, dict):
            logger.warning(f"[{job_id}] LLM fallback: skipping non-dict item: {item}")
            continue
        try:
            idx = int(item.get("index", 0)) - 1
        except (TypeError, ValueError):
            idx = -1
        if idx < 0 or idx >= len(llm_queue):
            continue
        q = llm_queue[idx]
        in_spec = item.get("in_spec")
        if in_spec is False:
            findings.append({
                "page": q["page"],
                "type": "param_out_of_spec",
                "severity": "warning",
                "description": (
                    f"第{q['page']}页 参数 {q['name']}={q['actual']}{q.get('unit','')} "
                    f"不在规格 {q['spec']} 内（LLM 判定）"
                ),
                "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
                "operator": "",
                "source": "llm_fallback",
            })
        elif in_spec is None:
            findings.append({
                "page": q["page"],
                "type": "completeness",
                "severity": "warning",
                "description": (
                    f"第{q['page']}页 参数 {q['name']} 规格描述模糊（spec={q['spec']}），"
                    f"需人工确认"
                ),
                "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
                "operator": "",
                "source": "llm_fallback",
            })
    return findings


# ---------------------------------------------------------------------------
# LLM semantic check (catches what rules missed)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """你是一个 GMP 批生产记录合规分析专家。
给定一份批生产记录所有页的结构化数据摘要（已由规则层跑过规则，
请只关注规则层无法判定的语义异常，例如签名一致性、批次逻辑、跨页参数漂移等。

输出 JSON 数组，每个 finding 包含：
{"page":页码,"type":"signature_mismatch|param_out_of_spec|completeness|user_rule","severity":"critical|warning|info","description":"问题描述","ocr_text":"原文摘录","operator":"涉及人员","rule_id":"命中规则的ID"}

type 为 "user_rule" 时表示命中用户自定义合规规则（按用户规则逐条核对），
且 rule_id 必须填写命中的那条规则的 ID（取自下方规则清单中的 [规则ID: xxx]，逐字照抄；未命中用户规则时该字段填 null）。"""


def _user_rules_section(rules: list[dict] | None = None) -> tuple[str, str]:
    """构造用户自定义合规规则提示段 + 内容 hash。

    返回 (section_text, rules_hash)：无启用规则时返回 ("", "none")。
    hash 用于 llm_call_audit 的 prompt_version 留痕（GMP 可追溯）。
    传入预加载的启用规则列表可避免每 job 重复读 config.json（性能）。
    """
    if rules is None:
        from config import load_user_rules
        rules = [r for r in load_user_rules() if r.get("active")]
    if not rules:
        return "", "none"
    # 对抗审查 P2-8：手改 config.json（文档支持）可造出缺 id/text 的规则，
    # r['id'] 下标访问 → KeyError → 整个 Stage 3 崩溃。防御式取值并跳过
    # 无文本的条目（无法执行的规则不应进 prompt）。
    rules = [r for r in rules if isinstance(r, dict) and str(r.get("text", "")).strip()]
    if not rules:
        return "", "none"
    lines = [f"- [规则ID: {r.get('id', '?')}] {r.get('text')}" for r in rules]
    section = (
        "<USER_RULES>\n"
        "用户自定义合规规则（必须逐条核对记录是否满足，"
        "违反时输出 type=user_rule 的 finding，按影响定级，rule_id 照抄对应规则 ID）：\n"
        + "\n".join(lines)
        + "\n</USER_RULES>"
    )
    import hashlib
    rules_hash = hashlib.md5(section.encode("utf-8")).hexdigest()[:8]
    return section, rules_hash


async def _llm_based_check(summary: str, *, job_id: str = "",
                           user_rules: list[dict] | None = None) -> list[dict]:
    if not summary.strip():
        return []
    # 每次 job 只读一次 config.json（analyze_cross_page 预加载后传入）
    if user_rules is None:
        from config import load_user_rules
        user_rules = [r for r in load_user_rules() if r.get("active")]
    user_section, rules_hash = _user_rules_section(user_rules)
    prompt = f"{user_section}\n\n{summary}" if user_section else summary
    prompt_version = f"semantic_v2+rules{rules_hash}" if rules_hash != "none" else "semantic_v2"
    client = get_llm_client()
    try:
        result = await client.chat_json(
            SYSTEM_PROMPT, prompt, max_tokens=4000, temperature=0.1, timeout=180.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm",
                       "prompt_version": prompt_version},
        )
    except Exception as e:
        logger.warning(f"[{job_id}] LLM semantic check failed: {e} — flagging for human review")
        return [{
            "page": 0,
            "type": "completeness",
            "severity": "info",
            "description": (
                f"跨页 LLM 语义检查不可用（LLM 调用失败: {type(e).__name__}），"
                f"规则层未覆盖的跨页异常（如签名一致性、批次逻辑、参数漂移）需人工复核"
            ),
            "ocr_text": "",
            "operator": "",
            "source": "rule",
        }]
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning(f"[{job_id}] Cross-page LLM analysis: JSON parse failure — flagging for human review")
        return [{
            "page": 0,
            "type": "completeness",
            "severity": "info",
            "description": (
                "跨页 LLM 语义检查返回 JSON 解析失败，规则层未覆盖的跨页异常"
                "（如签名一致性、批次逻辑、参数漂移）需人工复核"
            ),
            "ocr_text": "",
            "operator": "",
            "source": "rule",
        }]
    if isinstance(result, list):
        findings = result
    elif isinstance(result, dict):
        findings = result.get("findings", [])
    else:
        findings = []
        logger.warning(f"[{job_id}] Cross-page LLM analysis: unexpected result type {type(result).__name__}, no findings")
    valid = []
    required = {"page", "type", "severity", "description"}
    # 启用规则 id 集合：user_rule finding 的 rule_id 只接受集合内的 id
    enabled_rule_ids = {r["id"] for r in user_rules}
    for f in findings:
        if not isinstance(f, dict) or not required.issubset(f.keys()):
            logger.warning(f"[{job_id}] Skipping invalid finding (missing fields): {f}")
            continue
        f.setdefault("ocr_text", "")
        f.setdefault("operator", "")
        # user_rule 类型的 finding 标记独立 source，便于复核页区分来源
        f["source"] = "user_rule" if f.get("type") == "user_rule" else "llm_cross"
        # rule_id 防伪：超出启用规则集合（含缺失/幻觉 id）一律置 None
        rid = f.get("rule_id")
        if f["source"] == "user_rule" and rid in enabled_rule_ids:
            f["rule_id"] = rid
        else:
            f["rule_id"] = None
        valid.append(f)
    return valid


def _build_summary(pages: list[dict]) -> str:
    """Build compact text summary of all pages for LLM consumption."""
    lines = []
    for page in pages:
        pi = page["page_info"]
        lines.append(f"--- 第{page['page']}页 ---")
        if pi.get("title"):
            lines.append(f"  标题: {pi['title']}")
        if pi.get("production_date"):
            lines.append(f"  生产日期: {pi['production_date']}")
        if pi.get("batch_no"):
            lines.append(f"  批号: {pi['batch_no']}")
        eyg = page["event_year_groups"]
        if eyg:
            lines.append(f"  事件年份分组: {eyg}")
        for step in page["steps"]:
            line = f"  步骤{step.get('step_no','?')}: {step.get('operation','')[:60]}"
            if step.get("start_time"):
                line += f" | 开始:{step['start_time']}"
            if step.get("end_time"):
                line += f" | 结束:{step['end_time']}"
            if step.get("operator"):
                line += f" | 操作人:{step['operator']}"
            if step.get("reviewer"):
                line += f" | 复核人:{step['reviewer']}"
            for p in step.get("parameters", []) or []:
                if p.get("value"):
                    line += f" | {p.get('name','')}:{p['value']}{p.get('unit','')}"
            lines.append(line)
            for m in step.get("measurements", []) or []:
                t = m.get("time", "")
                cells = m.get("values") or {}
                first_keys = list(cells.keys())[:4]
                cells_str = ", ".join(
                    f"{k}={cells[k].get('actual','')}" for k in first_keys
                )
                lines.append(f"    @ {t} | {cells_str}{' ...' if len(cells) > 4 else ''}")
            for sig in step.get("signatures", []) or []:
                lines.append(
                    f"    签名: {sig.get('role','')}|{sig.get('name','')}|{sig.get('sign_time','')}"
                )
        if page["findings"]:
            lines.append(f"  已抓 findings ({len(page['findings'])} 条):")
            for f in page["findings"][:5]:
                lines.append(f"    - [{f.get('type','')}] {f.get('description','')[:80]}")
    return "\n".join(lines)
