from __future__ import annotations

import logging
import time

from llm.client import DETERMINISTIC_TEMPERATURE, get_llm_client

logger = logging.getLogger(__name__)

# 跨页 summary 字符上限（context budget 护栏，llm_checks._build_summary）：
# 61 页实测 summary ≈ 46K chars ≈ 23K prompt tokens，已占 DeepSeek-V3.2
# 128K 窗口约 22%。页码线性外推 100+ 页会逼近"内容过载"质量退化区
# （best practice: 窗口占用 ≤ 30-40%）。上限 100K chars ≈ 50K tokens（39%）
# 封顶于此；超限时第一遍字段压缩（operation 60→24 字符、测量列 4→2、
# findings 5→2 条），仍超则逐行保留完整行 + 显式截断标记（LLM 知道输入
# 不完整，不会把缺失当正常——宁可 drop 不可 starve）。
_SUMMARY_MAX_CHARS = 100_000
_OP_LEN_FULL = 60
_OP_LEN_COMPRESSED = 24
_MEASURE_COLS_FULL = 4
_MEASURE_COLS_COMPRESSED = 2
_FINDINGS_FULL = 5
_FINDINGS_COMPRESSED = 2



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


def _where_tag(q: dict) -> str:
    """把「步骤 / 时间点」编码进 description —— **0-42 的修复点**。

    为什么必须编进去：写库去重键是 UNIQUE
    ``idx_findings_dedup(job_id, source, page, type, description)``。``kind='cell'``
    的同一参数在一页里常有**多个时间点、同一取值**（真实批记录常态：10:38 与 11:34
    各测一次 3.6bar）⇒ 旧模板产出的 description **逐字相同** ⇒ ``INSERT OR IGNORE``
    **静默丢弃**后一条（实测全库 7 个 job 丢 64–77 条 / 13–17%，见 0-42）。

    ⚠️ 两个字段都没有时返回**空串** ⇒ 描述与历史实现**逐字节一致**
    （不制造"（步骤None）"，也不动既有快照断言）。

    ⚠️ 为什么不复用 ``finding_aggregate._where_phrase``：那里是**句中**插入
    （``第{p}页 {subject} 在 X 时实测 …``），而本文件的句子是
    ``第{p}页 参数 {name}={actual} …``，没有句中位置可插；硬套会把**所有**既有
    描述一起改写（包括没有时间点的）。两者共同的不变式只有一条 ——
    "时间点不同 ⇒ 描述不同"，由 ``test_cross_page_llm`` 的 0-42 用例分别钉住。
    """
    parts = []
    step = q.get("step_no")
    if step not in (None, ""):
        parts.append(f"步骤{step}")
    t = str(q.get("time") or "").strip()
    if t:
        parts.append(t)
    return f"（{' '.join(parts)}）" if parts else ""


def _flag_llm_queue_for_review(llm_queue: list[dict], *, reason: str,
                               aggregate: bool = False) -> list[dict]:
    """Fail-closed: when LLM fallback is unavailable, flag queued params
    for human review rather than silently passing them (GMP safety principle).

    对抗审查（aggregate=True，队列超限分支）：把所有参数合并为**单条**
    汇总 finding — 此前逐参数各生成一条（407 参数 = 407 条"参数数量
    过多"警告），复核页被噪音淹没，真实问题被掩盖。GMP 复核针对
    "某一页某批参数待人工判定"这一事项，汇总一条即可；明细由
    ocr_text 携带参数数与页码。小队列（LLM 调用失败/解析失败分支）
    保持逐条标记，参数少时可读且可逐条核对。
    """
    if aggregate and len(llm_queue) > 1:
        pages = sorted({q["page"] for q in llm_queue})
        page_s = "、".join(f"第{p}页" for p in pages[:10])
        if len(pages) > 10:
            page_s += f" 等{len(pages)}页"
        n = len(llm_queue)
        names = "、".join((q["name"] or "?")[:20] for q in llm_queue[:6])
        if len(llm_queue) > 6:
            names += f" 等{n}项"
        return [{
            "page": pages[0],
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"{page_s} 共 {n} 个参数（如 {names}）{reason}，"
                f"需人工逐项核对"
            ),
            "ocr_text": f"params={n} pages={len(pages)} reason={reason}",
            "operator": "",
            "source": "rule",
        }]
    findings = []
    for q in llm_queue:
        findings.append({
            "page": q["page"],
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{q['page']}页{_where_tag(q)} 参数 {q['name']}={q['actual']}{q.get('unit','')} "
                f"规格 {q['spec']} 无法自动判定（{reason}），需人工确认"
            ),
            "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
            "operator": "",
            "source": "rule",
        })
    return findings


# LLM fallback 单次调用的最大参数数量。
# 超过此数量时**分批**调用（见 `_llm_fallback_check`），不再整份降级。
# 实测 449 个参数的单次调用会让 SiliconFlow API 重试并卡住 Stage 3 数分钟；
# 50 个参数的 prompt 约 3-4KB，在 4000 max_tokens 内可完成。
_LLM_FALLBACK_BATCH_MAX = 50

# 0-41（P2）：单 job 自动判定的**批数**上限 —— 乘上批大小就是"参数数上限"。
# 取值依据是**真实最大样本**（job 670672f3-d0f，51 页 / 400 个待判定参数）⇒ 8 批
# 正好覆盖。更大的记录应当**看得见地**降级，而不是悄悄把 1000 个参数跑成 20 次
# 调用（成本与时长都不可控）。
_LLM_FALLBACK_MAX_BATCHES = 8

# 0-41：整个 fallback 阶段的墙钟预算（秒）。
# 分批会把"最坏耗时"直接乘上批数（8 × 单批最坏）⇒ 必须有全局闸门。0-38 的教训：
# 单页 21 分钟是真实发生过的，不能让 Stage 3 无限期挂着。
# 超预算的批次**显式**降级（每批一条汇总 finding，reason 写明已用时长）。
_LLM_FALLBACK_BUDGET_S = 600.0


async def _judge_one_batch(batch: list[dict], *, job_id: str = "",
                           batch_no: int = 1, batch_total: int = 1) -> list[dict]:
    """判定**一批**（≤ `_LLM_FALLBACK_BATCH_MAX`）参数。

    失败只降级**本批** —— 不连坐其余批次。这是 0-41 的关键：覆盖缺口只能按批
    出现，不能整 job 归零。失败路径的**逐参数**口径与改造前完全一致（只有
    "整份降级"那一条分支被替换），所以既有 fail-closed 断言不需要改。
    """
    items_text = "\n".join(
        f"{i+1}. 第{q['page']}页 工序{q.get('step_no','?')} "
        f"{q.get('time','') + ' ' if q.get('time') else ''}"
        f"{q['name']} | spec={q['spec']} | actual={q['actual']} | unit={q.get('unit','')}"
        for i, q in enumerate(batch)
    )
    prompt = f"参数列表：\n{items_text}\n\n请逐条判定并返回 JSON 数组。"
    client = get_llm_client()
    tag = f"batch {batch_no}/{batch_total}"
    try:
        result = await client.chat_json(
            _FALLBACK_SYSTEM_PROMPT, prompt, max_tokens=4000, temperature=DETERMINISTIC_TEMPERATURE, timeout=120.0,
            audit_ctx={"job_id": job_id, "page": None, "stage": "cross_page_llm_fallback",
                       "prompt_version": "fallback_v1"},
        )
    except Exception as e:
        logger.warning(f"[{job_id}] LLM fallback {tag} failed: {e} — flagging {len(batch)} params for human review")
        return _flag_llm_queue_for_review(batch, reason=f"LLM 调用失败: {type(e).__name__}")
    if isinstance(result, dict) and result.get("_parse_error"):
        logger.warning(f"[{job_id}] LLM fallback {tag}: JSON parse failure — flagging {len(batch)} params for human review")
        return _flag_llm_queue_for_review(batch, reason="LLM 返回 JSON 解析失败")
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
        if idx < 0 or idx >= len(batch):
            continue
        q = batch[idx]
        in_spec = item.get("in_spec")
        if in_spec is False:
            findings.append({
                "page": q["page"],
                "type": "param_out_of_spec",
                "severity": "warning",
                "description": (
                    f"第{q['page']}页{_where_tag(q)} 参数 {q['name']}={q['actual']}{q.get('unit','')} "
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
                    f"第{q['page']}页{_where_tag(q)} 参数 {q['name']} 规格描述模糊（spec={q['spec']}），"
                    f"需人工确认"
                ),
                "ocr_text": f"{q['name']}: spec={q['spec']} actual={q['actual']}",
                "operator": "",
                "source": "llm_fallback",
            })
    return findings


async def _llm_fallback_check(llm_queue: list[dict], *, job_id: str = "") -> list[dict]:
    """把待判定参数**分批**送 LLM —— 0-41 的修复点。

    改造前的行为（即缺陷本身）：`len(queue) > 50` 就**整份**降级为人工复核。
    真实样本（job 670672f3-d0f，51 页 / 400 个待判定参数）因此**一条
    `param_out_of_spec` 都没有** —— 复核页只看到"参数数量过多"，而用户标注页
    （page 6）的实测值 `6.88` 在全 job findings 中命中 **0 次**。那不是"安全
    降级"，是**覆盖缺口**：产品恰好在最需要自动判定的大记录上退化成一张白纸。

    现在：按 `_LLM_FALLBACK_BATCH_MAX` 切块、逐块判定；每块独立失败、独立降级。
    两道闸门防止"批数放大最坏耗时"（取值理由见上面两个常量）：
      · 超出 `_LLM_FALLBACK_MAX_BATCHES` 的批次 ⇒ 显式降级（写明总数与上限）；
      · 超出 `_LLM_FALLBACK_BUDGET_S` 的批次 ⇒ 显式降级（写明已用时长）。
    降级一律 `aggregate=True`（每批一条汇总 finding），不会像更早的实现那样给
    407 个参数各发一条 warning 把复核页淹没。

    不变式（护栏 `tests/unit/test_llm_fallback_batching.py` 用计数钉住）：
    每个入参**恰好**落在「被判定」或「被某条降级 finding 覆盖」之一，**不重不漏**。
    """
    if not llm_queue:
        return []

    chunks = [llm_queue[i:i + _LLM_FALLBACK_BATCH_MAX]
              for i in range(0, len(llm_queue), _LLM_FALLBACK_BATCH_MAX)]
    if len(chunks) > 1:
        logger.info(
            f"[{job_id}] LLM fallback: {len(llm_queue)} params -> "
            f"{len(chunks)} batches of <= {_LLM_FALLBACK_BATCH_MAX}"
        )

    findings: list[dict] = []
    t0 = time.monotonic()
    for no, batch in enumerate(chunks, start=1):
        if no > _LLM_FALLBACK_MAX_BATCHES:
            logger.warning(
                f"[{job_id}] LLM fallback: batch {no}/{len(chunks)} beyond "
                f"_LLM_FALLBACK_MAX_BATCHES={_LLM_FALLBACK_MAX_BATCHES} — degrading"
            )
            findings.extend(_flag_llm_queue_for_review(
                batch,
                reason=(f"参数数量过多（共 {len(llm_queue)}，超出自动判定上限 "
                        f"{_LLM_FALLBACK_MAX_BATCHES}×{_LLM_FALLBACK_BATCH_MAX}）"),
                aggregate=True,
            ))
            continue
        elapsed = time.monotonic() - t0
        if elapsed > _LLM_FALLBACK_BUDGET_S:
            logger.warning(
                f"[{job_id}] LLM fallback: {elapsed:.0f}s > budget "
                f"{_LLM_FALLBACK_BUDGET_S:.0f}s at batch {no}/{len(chunks)} — degrading"
            )
            findings.extend(_flag_llm_queue_for_review(
                batch,
                reason=(f"自动判定超时（已用 {elapsed:.0f}s，预算 "
                        f"{_LLM_FALLBACK_BUDGET_S:.0f}s）"),
                aggregate=True,
            ))
            continue
        findings.extend(await _judge_one_batch(
            batch, job_id=job_id, batch_no=no, batch_total=len(chunks)))
    return findings


# ---------------------------------------------------------------------------
# LLM semantic check (catches what rules missed)
# ---------------------------------------------------------------------------


#: 跨页语义检查允许输出的 type 枚举。
#: ⚠️ 旧版只有 `signature_mismatch|param_out_of_spec|completeness|user_rule`
#: 四种 —— 缺 `batch_inconsistency` 等跨页缺陷的**真实类型**，模型只能就近
#: 塞进 `param_out_of_spec`。实测（试用批记录 job 4cffb358-3b3）："第1页批号
#: 与第2页批号不一致" 被标成「参数超标」，复核页标题与内容不符（R95/A2）。
#: 类型是给人看的**分类标题**，枚举缺项 ⇒ 标题必错。
_CROSS_TYPES: tuple[str, ...] = (
    "batch_inconsistency", "signature_mismatch", "signature_time_anomaly",
    "param_out_of_spec", "completeness", "self_review", "step_gap",
    "time_reversal", "suspicious_date", "doc_version", "mass_balance",
    "deviation_link", "alteration", "equipment_state", "env_monitor",
)


def _system_prompt(has_user_rules: bool) -> str:
    """构造跨页语义检查的系统提示。

    ``user_rule`` 只在**确实配置了启用规则**时才进枚举（R95/A1、A3）：
    实测 config `user_rules=[]` 时模型仍产出 7 条 type=user_rule 的 critical，
    每条都声称命中一条不存在的规则 —— 没有规则文本，这个类型**没有指称对象**。
    """
    types = "|".join(_CROSS_TYPES + (("user_rule",) if has_user_rules else ()))
    lines = [
        "你是一个 GMP 批生产记录合规分析专家。",
        "给定一份批生产记录所有页的结构化数据摘要（已由规则层跑过规则，",
        "请只关注规则层无法判定的语义异常，例如签名一致性、批次逻辑、跨页参数漂移等。",
        "",
        "输出 JSON 数组，每个 finding 包含：",
        '{"page":页码,"type":"' + types + '","severity":"critical|warning|info",'
        '"description":"问题描述","ocr_text":"原文摘录","operator":"涉及人员","rule_id":null}',
        "",
        "type 必须按**缺陷本身的性质**选，不得为了省事复用 param_out_of_spec ——",
        "它只用于**实测值超出规格范围**；批号、版本、签名、工序编号的问题各有其类型。",
    ]
    if has_user_rules:
        lines.append(
            'type 为 "user_rule" 时表示命中用户自定义合规规则（按用户规则逐条核对），'
            "且 rule_id 必须填写命中的那条规则的 ID（取自下方规则清单中的 "
            "[规则ID: xxx]，逐字照抄；未命中用户规则时该字段填 null）。"
        )
    return chr(10).join(lines)


#: 无用户规则时的默认系统提示（保持模块级常量可用；有规则时用
#: :func:`_system_prompt` 动态构造）。
SYSTEM_PROMPT = _system_prompt(False)


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


# 人工复核反馈样例池上限 — 控制 prompt 预算（每条约 100-150 字符）
_EXEMPLAR_LIMIT = 12
_EXEMPLAR_DESC_MAX = 80

_VERDICT_ZH = {"confirmed": "问题属实", "rejected": "误报", "corrected": "描述已更正"}


async def _load_review_exemplars(limit: int = _EXEMPLAR_LIMIT) -> list[dict]:
    """加载近期人工裁决的 LLM 生成型 findings 作为 few-shot 校准样例。

    数据源：findings 表中 source ∈ {llm_cross, llm_fallback, user_rule}
    且 status ∈ {confirmed, rejected, corrected} 的行 — 每条都是复核员
    对模型输出的真实裁决。全局池（跨 job）：单 job 裁决量少，跨任务
    积累才能形成有效校准信号；retry 场景当前 job 上一轮的裁决同样
    入池（正是最相关的反馈）。
    """
    from db.client import get_db

    db = await get_db()
    cursor = await db.execute(
        "SELECT type, severity, description, status FROM findings "
        "WHERE source IN ('llm_cross', 'llm_fallback', 'user_rule') "
        "AND status IN ('confirmed', 'rejected', 'corrected') "
        "AND TRIM(COALESCE(description, '')) != '' "
        "ORDER BY COALESCE(reviewed_at, created_at) DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "type": r["type"],
            "severity": r["severity"],
            "description": r["description"],
            "verdict": r["status"],
        }
        for r in rows
    ]


def _exemplars_section(exemplars: list[dict]) -> str:
    """构造 <HUMAN_REVIEW_FEEDBACK> 提示段（空池返回 ""）。"""
    if not exemplars:
        return ""
    lines = []
    for e in exemplars[:_EXEMPLAR_LIMIT]:
        desc = str(e.get("description", ""))[:_EXEMPLAR_DESC_MAX]
        verdict = _VERDICT_ZH.get(e.get("verdict", ""), e.get("verdict", ""))
        lines.append(
            f"- [{e.get('severity', '?')}][{e.get('type', '?')}] {desc} → {verdict}"
        )
    return (
        "<HUMAN_REVIEW_FEEDBACK>\n"
        "以下是复核员对近期同类分析输出的人工裁决样例"
        "（问题属实=该类发现真实存在；误报=不构成问题，请避免再报类似；"
        "描述已更正=发现方向正确但表述与实际不符）。"
        "请据此校准你的输出倾向，不要照抄样例内容：\n"
        + "\n".join(lines)
        + "\n</HUMAN_REVIEW_FEEDBACK>"
    )


async def _llm_based_check(summary: str, *, job_id: str = "",
                           user_rules: list[dict] | None = None) -> list[dict]:
    if not summary.strip():
        return []
    # 每次 job 只读一次 config.json（analyze_cross_page 预加载后传入）
    if user_rules is None:
        from config import load_user_rules
        user_rules = [r for r in load_user_rules() if r.get("active")]
    user_section, rules_hash = _user_rules_section(user_rules)
    # 人工复核反馈回流（few-shot 校准）：近期裁决样例注入 prompt，
    # 让模型从误报/确认中学习。加载失败不阻断（降级为无样例）。
    try:
        exemplars = await _load_review_exemplars()
    except Exception as e:
        logger.warning(f"[{job_id}] review exemplars load failed: {e}")
        exemplars = []
    fb_section = _exemplars_section(exemplars)
    sections = [s for s in (user_section, fb_section) if s]
    prompt = "\n\n".join(sections + [summary]) if sections else summary
    fb_tag = f"+fb{len(exemplars)}" if exemplars else ""
    prompt_version = (
        f"semantic_v2+rules{rules_hash}{fb_tag}"
        if rules_hash != "none"
        else f"semantic_v2{fb_tag}"
    )
    client = get_llm_client()
    try:
        result = await client.chat_json(
            _system_prompt(bool(user_rules)), prompt, max_tokens=4000,
            temperature=DETERMINISTIC_TEMPERATURE, timeout=180.0,
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
        rid = f.get("rule_id")
        # R95/A1、A3、A7：声称命中用户规则，但这条规则**不存在**（未配置、
        # 或 id 是幻觉）⇒ 主张没有指称对象，依据（user_rule 的依据就是规则
        # 文本本身）为空。旧实现只是把 rule_id 置 None 就照常落库，还给
        # `source='user_rule'`（复核分级里算"确定性判据"）—— 实测在
        # user_rules=[] 的配置下产出了 7 条这样的 critical。
        # 处置：降级为普通跨页语义发现，类型按文案回填，交给 llm_finding_guard
        # 正常复核（fail-open：不静默丢条目，但不再冒充用户规则命中）。
        if f.get("type") == "user_rule" and rid not in enabled_rule_ids:
            from core.finding_quality import is_spec_verdict, type_from_description
            _desc = f.get("description")
            # 回填顺序：强信号表 → "判定词+区间"（真超差）→ 兜底 completeness。
            # 缺了中间一级，「30°C 超出用户规则 15-25°C」会被洗成 completeness。
            f["type"] = (
                type_from_description(_desc)
                or ("param_out_of_spec" if is_spec_verdict(_desc) else "completeness")
            )
            logger.warning(
                f"[{job_id}] unbacked user_rule finding downgraded to "
                f"llm_cross/{f['type']} (rule_id={rid!r} 不在启用规则集合 "
                f"{sorted(enabled_rule_ids)} 内)"
            )
            f["source"] = "llm_cross"
            f["rule_id"] = None
            valid.append(f)
            continue
        # user_rule 类型的 finding 标记独立 source，便于复核页区分来源
        f["source"] = "user_rule" if f.get("type") == "user_rule" else "llm_cross"
        f["rule_id"] = rid if f["source"] == "user_rule" else None
        valid.append(f)
    return valid


def _summary_max_chars(context_window: int) -> int:
    """Derive the cross-page summary character cap from the model's context
    window.

    Budget rule (Round 7): the cross-page prompt must stay ≤ 35% of the
    window so page-1/2 reruns, instructions and completions fit with margin
    (context rot + attention degradation beyond ~30-40%; window×1.6 converts
    tokens → CJK-heavy characters ≈ 1.6 chars/token). Floor 8K chars keeps
    tiny windows usable instead of starving the prompt entirely.
    """
    if not isinstance(context_window, int) or context_window <= 0:
        context_window = 128_000
    return max(8_000, int(context_window * 0.35 * 1.6))


def _build_summary(pages: list[dict], context_window: int | None = None) -> str:
    """Build compact text summary of all pages for LLM consumption.

    Context-budget guarded: cap = _summary_max_chars(context_window) when a
    window is provided (config.app.llm_context_window), else the fixed
    _SUMMARY_MAX_CHARS default (128K-window equivalent). Two-stage
    degradation (field compression, then whole-line preservation with an
    explicit truncation marker so the LLM knows the input is incomplete).
    """
    cap = _summary_max_chars(context_window) if context_window else _SUMMARY_MAX_CHARS
    text = "\n".join(_summary_lines(pages, op_len=_OP_LEN_FULL,
                                    meas_cols=_MEASURE_COLS_FULL,
                                    findings_n=_FINDINGS_FULL))
    if len(text) <= cap:
        return text
    logger.warning(
        f"Cross-page summary {len(text)} chars > {cap} cap — "
        f"compressing fields (op {_OP_LEN_FULL}→{_OP_LEN_COMPRESSED}, "
        f"cells {_MEASURE_COLS_FULL}→{_MEASURE_COLS_COMPRESSED}, "
        f"findings {_FINDINGS_FULL}→{_FINDINGS_COMPRESSED})"
    )
    lines = _summary_lines(pages, op_len=_OP_LEN_COMPRESSED,
                           meas_cols=_MEASURE_COLS_COMPRESSED,
                           findings_n=_FINDINGS_COMPRESSED)
    text = "\n".join(lines)
    if len(text) <= cap:
        return text
    # 仍超限：逐行保留完整行（不切开半行），显式截断标记防 LLM 幻觉。
    kept, chars = [], 0
    marker_len = 0
    for i, ln in enumerate(lines):
        delta = len(ln) + 1
        if chars + delta + marker_len > cap:
            break
        kept.append(ln)
        chars += delta
    marker = (f"## 内容过长已截断：仅完整展示 {len(kept)}/{len(lines)} 行，"
              f"之后页面内容未注入，跨页分析可能缺失，需人工复核")
    kept.append(marker)
    logger.warning(
        f"Compressed summary still {len(text)} chars — line-truncated to "
        f"{len(kept)}/{len(lines)} lines with explicit marker"
    )
    return "\n".join(kept)


def _summary_lines(pages: list[dict], *, op_len: int, meas_cols: int,
                   findings_n: int) -> list[str]:
    """Build the summary lines with the given field-width limits."""
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
            line = f"  步骤{step.get('step_no','?')}: {step.get('operation','')[:op_len]}"
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
                first_keys = list(cells.keys())[:meas_cols]
                cells_str = ", ".join(
                    f"{k}={cells[k].get('actual','')}" for k in first_keys
                )
                lines.append(f"    @ {t} | {cells_str}{' ...' if len(cells) > meas_cols else ''}")
            for sig in step.get("signatures", []) or []:
                lines.append(
                    f"    签名: {sig.get('role','')}|{sig.get('name','')}|{sig.get('sign_time','')}"
                )
        if page["findings"]:
            lines.append(f"  已抓 findings ({len(page['findings'])} 条):")
            for f in page["findings"][:findings_n]:
                lines.append(f"    - [{f.get('type','')}] {f.get('description','')[:80]}")
    return lines
