from __future__ import annotations

import logging
import re
from datetime import datetime

from core.rules.parsing import (
    _CN_DATE_RE,
    _DATE_RE,
    _MD_TIME_RE,
    _extract_year,
    _interval_after,
    _interval_before,
    _parse_time,
    _parse_time_interval,
)
from core.rules.year_vote import YearVote, build_year_vote

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 判据证据分级（R1a / R1b 共用）
#
# 背景（B1-9，2026-09-20 视觉复核 p48）：规则层对手写体时刻直接给了 critical，
# 而项目自身约定是「手写体字段应标 confidence=low，**规则不直接判定**」。
# 更一般地：**不确定来源不给最高严重度** —— 与 B1-6 的 L4 同源思想。
# 时序类判据的两个"证据不足"来源：
#   ① 所依据的时刻为手写填写；
#   ② 时刻字面量缺日期成分 ⇒ 比较所用的日期系由该页 production_date 推定，
#      **不是记录里写的事实**（实测 p46 的 fallback 甚至是当天日期 2026-09-18）。
# ---------------------------------------------------------------------------

#: 从时刻字面量里取 HH:MM（兼容 `03:12` 与 `03时12分`，允许分隔符旁有空白）。
#: ⚠️ 必须在**原字面量**上匹配，不能先去掉空白 —— `2025-09-25 03:36` 去空白后
#: 变成 `2025-09-2503:36`，日期的末位数字与时刻粘连，`(?<!\d)` 会挡住匹配
#: （实测：曾因此让手写封顶整条失效）。
_HM_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[:时]\s*(\d{2})")


def _literal_has_date(lit) -> bool:
    """时刻字面量是否自带日期成分。

    `_parse_time` 对"只有时刻"的输入（`11:04`）以及"无年份"的输入
    （`07-17 14:30`）都会用 `fallback_date` 补全。补出来的日期**不是记录里
    写的事实** ⇒ 跨日/跨页判序不成立。只认可从字面量里读到的年-月-日
    （`_DATE_RE` / `_CN_DATE_RE`）或月-日（`_MD_TIME_RE`）。
    """
    if not lit or not isinstance(lit, str):
        return False
    return bool(
        _DATE_RE.search(lit)
        or _CN_DATE_RE.search(lit)
        or _MD_TIME_RE.search(lit)
    )


def _time_is_handwritten(hw_tokens, lit) -> bool:
    """该时刻字面量是否来自手写填写（依据 step 的 ``handwritten`` 标注）。

    ``handwritten`` 是 **LLM 标注（模型猜测）**，因此只用于**下调**严重度，
    绝不用于升级 —— 方向保守，与 `core.hw_signal` 的既定原则一致。
    匹配方式：整个字面量命中，或"时:分"部分命中（手写值常写作
    `25日03时12分`，而结构化字段已归一为 `2025-09-25 03:12`）。
    """
    if not lit or not isinstance(lit, str):
        return False
    if not isinstance(hw_tokens, (list, tuple)) or not hw_tokens:
        return False
    m = _HM_RE.search(lit)
    hm_cn = f"{int(m.group(1)):02d}时{m.group(2)}分" if m else None
    hm_ascii = f"{int(m.group(1)):02d}:{m.group(2)}" if m else None
    norm = lit.replace(" ", "")
    for tok in hw_tokens:
        t = str(tok).replace(" ", "")
        if not t:
            continue
        if norm in t:
            return True
        if hm_cn and (hm_cn in t or (hm_ascii and hm_ascii in t)):
            return True
    return False


def _evidence_notes(hw_prev, lit_prev, hw_curr, lit_curr) -> list[str]:
    """收集"证据不足"说明；非空 ⇒ 该时序判定不得给 critical。"""
    notes: list[str] = []
    if _time_is_handwritten(hw_prev, lit_prev) or _time_is_handwritten(hw_curr, lit_curr):
        notes.append("所依据的时刻为手写填写，按项目约定手写体不直接判定")
    if not _literal_has_date(lit_prev) or not _literal_has_date(lit_curr):
        notes.append("时刻字面量缺日期成分，比较所用日期系由该页生产日期推定")
    return notes


def _year_resolved(vote: YearVote, *years: int | None) -> str | None:
    """若任一输入年份被投票归一，返回归一理由（否则 None）。

    R3 降噪的公共闸门：`year_delta > 2` 的"异常"往往源于**同一次单字符年份
    误读**（如 2015/2025 混淆），而非记录本身矛盾。此时按投票归一并**不发射**
    该 finding —— 理由回传供调用方记录（不静默丢弃）。
    """
    for y in years:
        _, reason = vote.resolve(y)
        if reason:
            return reason
    return None



# ---------------------------------------------------------------------------
# R1-a: per-step time_reversal
# ---------------------------------------------------------------------------


def _check_time_reversal_in_page(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            # Interval comparison: date-only values span the whole day, so a
            # start "2024-01-01" (date-only) vs end "2024-01-01 14:30" is NOT
            # a reversal — the start may fall anywhere that day.
            iv_start = _parse_time_interval(step.get("start_time"), fb_date)
            iv_end = _parse_time_interval(step.get("end_time"), fb_date)
            if iv_start and iv_end and _interval_after(iv_start, iv_end):
                # B1-9：同一工序"结束早于开始"在逻辑上确属不可能，故基准为
                # critical；但若所依据的时刻是手写填写、或日期系由该页生产
                # 日期推定，则该结论依赖的输入不可靠 ⇒ 封顶为 warning。
                notes = _evidence_notes(
                    step.get("handwritten"), step.get("end_time"),
                    step.get("handwritten"), step.get("start_time"),
                )
                findings.append({
                    "page": pno,
                    "type": "time_reversal",
                    "severity": "warning" if notes else "critical",
                    "description": (
                        f"第{pno}页 工序{step.get('step_no','?')} "
                        f"开始时间({step.get('start_time')}) 晚于结束时间({step.get('end_time')})"
                    ) + (f"（{'；'.join(notes)}，故不作为确定性结论）" if notes else ""),
                    "ocr_text": f"{step.get('start_time')} → {step.get('end_time')}",
                    "operator": step.get("operator") or "",
                    "source": "rule",
                })
    return findings


# ---------------------------------------------------------------------------
# R1-b: cross-step time_reversal (across pages)
# ---------------------------------------------------------------------------


def _check_time_reversal_cross_page(pages: list[dict]) -> list[dict]:
    """R1-b: cross-step time_reversal across pages.

    Robustness improvements (avoid false positives):
    1. Sort steps by step_no within each page — pages may list steps out of
       numeric order, but the rule should compare in step order, not row order.
    2. Skip comparison when curr.step_no <= prev.step_no AND pages differ —
       this indicates an appendix/supplement re-listing an earlier step, not a
       real sequence violation.
    3. When start and end differ by >2 years on what should be the same batch,
       flag as extraction_error (warning) instead of time_reversal (critical):
       the LLM/OCR likely leaked the production_date year (e.g. 2015 vs 2025).
    4. [B1-9] Separate "true inversion" from "mere overlap" and cap severity on
       weak evidence:
       - true inversion (curr.start < prev.start) — step numbering contradicts
         the recorded instants ⇒ critical (unless evidence is weak, see below);
       - mere overlap (prev.start <= curr.start < prev.end) — different
         equipment/materials may legitimately be cleaned in parallel ⇒ warning,
         worded as overlap rather than reversal;
       - weak evidence (the time literal is handwritten, or carries no date so
         the comparison date was back-filled from the page production_date) ⇒
         never critical (same idea as B1-6's severity ceiling: an uncertain
         source does not get the highest severity).
    """
    findings = []
    ordered = []
    vote = build_year_vote(pages)
    year_resolutions: list[str] = []
    unparseable_times: list[tuple[int, str, str, str]] = []  # (page, step_no, operation, raw_time)
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        # Sort steps within page by step_no (numeric when possible) so the
        # cross-page sequence follows step order, not table row order.
        page_steps = list(page["steps"])
        try:
            page_steps.sort(key=lambda s: _step_sort_key(s.get("step_no")))
        except Exception:
            pass  # keep original order if sorting fails
        for step in page_steps:
            t_start = _parse_time(step.get("start_time"), fb_date)
            t_end = _parse_time(step.get("end_time"), fb_date)
            # Detect unparseable time strings (OCR errors) — flag for human review
            raw_start = step.get("start_time", "")
            raw_end = step.get("end_time", "")
            if (raw_start or raw_end) and not (t_start and t_end):
                unparseable_times.append((pno, str(step.get("step_no", "?")),
                                          (step.get("operation") or "")[:40],
                                          f"start={raw_start} end={raw_end}"))
            if t_start and t_end:
                ordered.append({
                    "page": page["page"],
                    "step_no": step.get("step_no"),
                    "step_key": _step_sort_key(step.get("step_no")),
                    "start_time": step.get("start_time"),
                    "end_time": step.get("end_time"),
                    "t_start": t_start,
                    "t_end": t_end,
                    "iv_start": _parse_time_interval(step.get("start_time"), fb_date),
                    "iv_end": _parse_time_interval(step.get("end_time"), fb_date),
                    "operator": step.get("operator") or "",
                    "hw": step.get("handwritten"),
                })
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        # Skip when this looks like an appendix re-listing an earlier step:
        # different page, step number decreases or stays the same.
        if curr["page"] != prev["page"] and curr["step_key"] <= prev["step_key"]:
            continue
        # Skip duplicate step entries on the same page (same step_no split
        # across two rows — e.g. p11 工序6 appears twice). Comparing a step
        # against itself produces meaningless "start < end" findings.
        if curr["page"] == prev["page"] and curr["step_key"] == prev["step_key"]:
            continue
        if curr["iv_start"] and prev["iv_end"] and _interval_before(curr["iv_start"], prev["iv_end"]):
            # Year-mismatch detection: if the two timestamps differ by >2 years,
            # treat as extraction_error (warning) rather than time_reversal (critical).
            year_delta = abs(curr["t_start"].year - prev["t_end"].year)
        else:
            continue
        if year_delta > 2:
            # R3：先做年份投票归一 —— 若该差异由"同一次单字符年份误读"造成
            # （如 2015/2025），它就不是提取错误提示而是纯噪声：归一 + 不发射。
            reason = _year_resolved(vote, curr["t_start"].year, prev["t_end"].year)
            if reason:
                year_resolutions.append(reason)
                continue
            findings.append({
                "page": curr["page"],
                "type": "time_reversal",
                "severity": "warning",
                "description": (
                    f"第{curr['page']}页 工序{curr['step_no']} 开始({curr['start_time']}) "
                    f"早于第{prev['page']}页 工序{prev['step_no']} 结束({prev['end_time']})，"
                    f"年份相差 {year_delta} 年，可能为 OCR/LLM 提取错误（原值 2015/2025 混淆等），请人工核对"
                ),
                "ocr_text": f"{curr['start_time']} < {prev['end_time']} (Δ{year_delta}y)",
                "operator": curr["operator"],
                "source": "rule",
            })
            continue

        # ── B1-9：判据分级 ───────────────────────────────────────────
        # 进入本分支只说明 curr.start < prev.end —— 即两个工序的**时间区间
        # 重叠**。重叠本身不是"倒序"：编号是否被违反，要看**开始时刻**。
        # 实测 p48（视觉复核）：D2101/D2102 箱体清洗 03:12→03:36 与
        # 烘盘/盘罩/勺子清洗 03:34→04:12 是**不同设备的两道并行清洗**，
        # 重叠属正常，old 实现却按 `time_reversal` 给了 critical。
        is_inversion = curr["t_start"] < prev["t_start"]
        notes = _evidence_notes(
            prev["hw"], prev["end_time"], curr["hw"], curr["start_time"],
        )
        if is_inversion:
            desc = (
                f"第{curr['page']}页 工序{curr['step_no']} 开始({curr['start_time']}) "
                f"早于第{prev['page']}页 工序{prev['step_no']} 开始({prev['start_time']})，"
                f"工序编号顺序与开始时刻矛盾（时间倒序）"
            )
            ocr_ref = f"{curr['start_time']} < {prev['start_time']} (开始时刻倒序)"
        else:
            desc = (
                f"第{curr['page']}页 工序{curr['step_no']} 开始({curr['start_time']}) "
                f"早于第{prev['page']}页 工序{prev['step_no']} 结束({prev['end_time']})，"
                f"两工序时间重叠（若为不同设备/物料的并行操作则属正常，请核对）"
            )
            ocr_ref = f"{curr['start_time']} < {prev['end_time']} (区间重叠)"
        if notes:
            desc += f"（{'；'.join(notes)}，故不作为确定性结论）"
        findings.append({
            "page": curr["page"],
            "type": "time_reversal",
            "severity": "critical" if (is_inversion and not notes) else "warning",
            "description": desc,
            "ocr_text": ocr_ref,
            "operator": curr["operator"],
            "source": "rule",
        })
    # Flag unparseable time strings for human review (OCR quality issue)
    for pno, step_no, op, raw in unparseable_times:
        findings.append({
            "page": pno,
            "type": "completeness",
            "severity": "info",
            "description": (
                f"第{pno}页 工序{step_no} {op} "
                f"时间格式无法解析（{raw}），请人工核对时间顺序"
            ),
            "ocr_text": raw,
            "operator": "",
            "source": "rule",
        })
    if year_resolutions:
        logger.info(
            f"R1-b 年份投票归一：抑制 {len(year_resolutions)} 条误读派生的 time_reversal "
            f"（{year_resolutions[0]}）"
        )
    return findings


def _step_sort_key(step_no) -> float:
    """Convert step_no to a numeric sort key.

    Handles values like "1", "2", "附表1", "3.1" by extracting the leading
    number; non-numeric steps sort after all numeric ones.
    """
    if step_no is None:
        return 9999.0
    m = re.search(r"(\d+(?:\.\d+)?)", str(step_no))
    if m:
        return float(m.group(1))
    return 9999.0


# ---------------------------------------------------------------------------
# R2: year_contradiction within same event_type
# ---------------------------------------------------------------------------


def _check_year_contradiction(pages: list[dict]) -> list[dict]:
    """R2：同一事件类型内出现多个年份。

    R3 降噪（交叉约束）：先用**全文档年份投票**把"单字符误读的少数读法"归一
    （如 `[2015, 2025]` → `[2025]`），只有在归一后**仍剩 ≥2 个年份**时才发射
    —— 即"禁止单选一个候选就生成 contradiction"（评审文档 §5 R3）。
    """
    findings = []
    vote = build_year_vote(pages)
    normalized: list[str] = []
    for page in pages:
        eyg = page["event_year_groups"]
        if not eyg:
            continue
        pno = page["page"]
        for event_type in ("draft", "production", "review", "approval", "issue", "other"):
            years = eyg.get(event_type) or []
            # de-dup but preserve multi-year signal
            uniq = sorted({int(y) for y in years if y is not None})
            if len(uniq) <= 1:
                continue
            # R3：投票归一 —— 逐个年份过闸，被归一的记录理由、不进 `kept`。
            kept: list[int] = []
            for y in uniq:
                _resolved, reason = vote.resolve(y)
                if reason:
                    normalized.append(f"第{pno}页 {event_type}: {reason}")
                else:
                    kept.append(y)
            uniq = sorted(set(kept))
            if len(uniq) <= 1:
                continue
            findings.append({
                "page": pno,
                "type": "year_contradiction",
                "severity": "warning",
                "description": (
                    f"第{pno}页 {event_type} 事件内年份不一致: {uniq}，需人工确认"
                ),
                "ocr_text": str(uniq),
                "operator": "",
                "source": "rule",
            })
    if normalized:
        logger.info(
            f"R2 年份投票归一：{len(normalized)} 处单字符误读归入多数读法"
            f"（{normalized[0]}）"
        )
    return findings


# ---------------------------------------------------------------------------
# R4: suspicious_date
# ---------------------------------------------------------------------------


def record_production_year(pages: list[dict]) -> int | None:
    """记录里**解析出的生产年份**（多页多数票）；一页都没解析出来时返回 ``None``。

    这是"分析基准年"的首选来源（B1-7）。为什么必须固化基准：此前 R4 直接用
    ``datetime.now().year`` 做上界 ⇒ **同一份 PDF 在不同年份重分析会得到不同结论**
    （finding 不可复现，审计上不可接受）。判据方向本身是对的（记录里出现未来日期
    确实可疑），问题只在"基准随运行时刻漂移"。
    """
    votes: dict[int, int] = {}
    for page in pages:
        pi = page.get("page_info") or {}
        y = _extract_year(pi.get("production_date"))
        if y is not None:
            votes[y] = votes.get(y, 0) + 1
    if not votes:
        return None
    # 票数优先；票数相同取**较小年份** —— 让结果与 dict 顺序无关、可复现
    return max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def _baseline_year(pages: list[dict], override: int | None = None) -> tuple[int, bool]:
    """返回 ``(分析基准年, 是否由"当前年"回退而来)``。

    优先级：调用方显式给出 > 记录里的生产年份 > 当前年（**并在 finding 里注明**）。
    返回第二个分量是为了让"回退"这件事在文案上**可见** —— 悄悄回退会让
    "不可复现"重新从后门回来。
    """
    if override is not None:
        return int(override), False
    ry = record_production_year(pages)
    if ry is not None:
        return ry, False
    return datetime.now().year, True


def _suspicion_reason(year: int, ceiling: int, now_ceiling: int) -> str | None:
    """该年份"可疑"的**依据**；不可疑返回 ``None``。

    两条判据**语义不同**，刻意分开表达、并在文案里各自点名基准：

    ① 相对**分析基准年**（记录内解析出的生产年份 ⇒ **可复现**）：
       记录内部就不该出现超出"生产年+1"的日期。
    ② 相对**当前年份**（绝对不可信）：**整份记录落在未来**。
       ⚠️ 这一条**天然依赖运行时刻** —— 一份 2036 年的记录放到 2036 年再看，
       它就不再"未来"了。所以文案里**明说是按当前年份判的**，不假装它可复现。
       （若丢掉这条，B1-7 的"固化基准"就会**静默削弱检测**：
       记录自我一致地落在未来时，只靠 ① 永远判不出来。）
    """
    if year < 2000:
        return "早于2000"
    if year > ceiling:
        return f"晚于{ceiling}"
    if year > now_ceiling:
        return f"晚于当前年份+1（{now_ceiling}），按运行时刻判断"
    return None


def _check_suspicious_dates(pages: list[dict],
                            analysis_year: int | None = None) -> list[dict]:
    findings = []
    baseline, year_inferred = _baseline_year(pages, analysis_year)
    ceiling = baseline + 1
    now_ceiling = datetime.now().year + 1
    for page in pages:
        pno = page["page"]
        seen: set[str] = set()
        for ds in _collect_all_date_strings(page):
            if ds in seen:
                continue
            seen.add(ds)
            year = _extract_year(ds)
            if year is None:
                continue
            reason = _suspicion_reason(year, ceiling, now_ceiling)
            if reason is None:
                continue
            # 回退到"当前年"时必须注明 —— 否则用户看不出这条结论**依赖运行时刻**
            basis_note = (
                "；基准年取当前年份（记录中未解析出生产年份）" if year_inferred else ""
            )
            findings.append({
                "page": pno,
                "type": "suspicious_date",
                "severity": "warning",
                "description": (
                    f"第{pno}页 日期 {ds} 年份 {year} 异常（{reason}），"
                    f"需人工确认{basis_note}"
                ),
                "ocr_text": ds,
                "operator": "",
                "source": "rule",
            })
    return findings


def _collect_all_date_strings(page: dict) -> list[str]:
    out = []
    pi = page["page_info"]
    if pi.get("production_date"):
        out.append(str(pi["production_date"]))
    for step in page["steps"]:
        for k in ("start_time", "end_time"):
            if step.get(k):
                out.append(str(step[k]))
        for sig in step.get("signatures", []) or []:
            if sig.get("sign_time"):
                out.append(str(sig["sign_time"]))
    return out


# ---------------------------------------------------------------------------
# R5: signature_time_anomaly (sign_time < op_time only)
# ---------------------------------------------------------------------------


def _check_signature_time_anomaly(pages: list[dict]) -> list[dict]:
    findings = []
    vote = build_year_vote(pages)
    year_resolutions: list[str] = []
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            iv_op = _parse_time_interval(
                step.get("start_time") or step.get("end_time"), fb_date
            )
            for sig in step.get("signatures", []) or []:
                st = sig.get("sign_time")
                if not st:
                    continue
                # Interval comparison: a date-only signature time spans the
                # whole day, so it is NOT "earlier than" an operation time on
                # the same day (previous point-comparison falsely flagged
                # sign "2024-01-01" vs op "2024-01-01 14:30" as anomalous).
                iv_sig = _parse_time_interval(st, fb_date)
                if iv_sig is None or iv_op is None:
                    continue
                if _interval_before(iv_sig, iv_op):
                    # Year-mismatch hint (mirrors R1-b): sign_time and op_time
                    # on the same page should be within ~2 years of each other.
                    # A larger gap almost always means OCR misread the year
                    # (e.g. 2015/2025), so the "earlier than" conclusion is
                    # too strong — flag it as an extraction hint instead.
                    year_delta = abs(iv_sig[0].year - iv_op[0].year)
                    if year_delta > 2:
                        # R3：同一次年份误读不得派生第二条 finding（归一 + 不发射）。
                        reason = _year_resolved(vote, iv_sig[0].year, iv_op[0].year)
                        if reason:
                            year_resolutions.append(reason)
                            continue
                    desc = (
                        f"第{pno}页 {sig.get('role','')} {sig.get('name','')} "
                        f"签名时间 {st} 早于操作时间 "
                        f"{step.get('start_time') or step.get('end_time')}"
                    )
                    if year_delta > 2:
                        desc += (
                            f"，年份相差 {year_delta} 年，可能为 OCR 提取错误"
                            "（原值 2015/2025 混淆等），请人工核对"
                        )
                    findings.append({
                        "page": pno,
                        "type": "signature_time_anomaly",
                        "severity": "warning",
                        "description": desc,
                        "ocr_text": f"{sig.get('name','')} {st}",
                        "operator": sig.get("name") or "",
                        "source": "rule",
                    })
    if year_resolutions:
        logger.info(
            f"R5 年份投票归一：抑制 {len(year_resolutions)} 条误读派生的 "
            f"signature_time_anomaly（{year_resolutions[0]}）"
        )
    return findings


# ---------------------------------------------------------------------------
# R9: signature ORDER between roles on the same step — reviewer/QA must sign
# AFTER the operator (GMP: 复核在操作之后). Interval comparison avoids
# date-only false positives (a date-only reviewer "2024-01-01" spans the
# whole day and is NOT earlier than operator "2024-01-01 14:30"). Year gaps
# >2 years get the OCR-confusion hint (mirrors R5).
# ---------------------------------------------------------------------------

_ROLE_RANK = {
    "operator": 0, "操作人": 0, "操作员": 0,
    "reviewer": 1, "复核人": 1, "复核员": 1, "workshop_reviewer": 1,
    "qa": 2, "qa_reviewer": 2, "质量保证": 2, "qa审核": 2, "批准人": 2, "放行人": 2,
    "issuer": 1, "记录发放": 1,
}


def _check_signature_order(pages: list[dict]) -> list[dict]:
    findings = []
    vote = build_year_vote(pages)
    year_resolutions: list[str] = []
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            sigs = step.get("signatures", []) or []
            ranked = []
            for s in sigs:
                role = (s.get("role") or "").lower()
                rk = _ROLE_RANK.get(role)
                st = s.get("sign_time")
                if rk is None or not st:
                    continue
                iv = _parse_time_interval(st, fb_date)
                if iv is None:
                    continue
                ranked.append((rk, s, iv))
            ranked.sort(key=lambda x: x[0])
            for i in range(1, len(ranked)):
                prev = ranked[i - 1]
                curr = ranked[i]
                # 对抗审查 P2：同角色相邻对不比较 —— 排序后同 rank 的两名
                # 复核人/操作人之间的先后在 GMP 上通常无约束，逐对比较会
                # 结构性误报；只在跨级（严格升序）时检查时间递增。
                if curr[0] <= prev[0]:
                    continue
                if _interval_before(curr[2], prev[2]):
                    year_delta = abs(curr[2][0].year - prev[2][0].year)
                    if year_delta > 2:
                        # R3：同一次年份误读不得派生第二条 finding（归一 + 不发射）。
                        reason = _year_resolved(vote, curr[2][0].year, prev[2][0].year)
                        if reason:
                            year_resolutions.append(reason)
                            continue
                    desc = (
                        f"第{pno}页 {curr[1].get('role','')} {curr[1].get('name','')} "
                        f"签名时间({curr[1].get('sign_time')}) 早于 "
                        f"{prev[1].get('role','')} "
                        f"{prev[1].get('name','')}"
                        f"({prev[1].get('sign_time')}),"
                        f"复核/审批顺序异常"
                    )
                    if year_delta > 2:
                        desc += (
                            f"，年份相差 {year_delta} 年，可能为 OCR 提取错误"
                            "（原值 2015/2025 混淆等），请人工核对"
                        )
                    findings.append({
                        "page": pno,
                        "type": "signature_time_anomaly",
                        "severity": "warning",
                        "description": desc,
                        "ocr_text": (
                            f"{prev[1].get('role','')}={prev[1].get('sign_time')}"
                            f" > {curr[1].get('role','')}={curr[1].get('sign_time')}"
                        ),
                        "operator": curr[1].get("name") or "",
                        "source": "rule",
                    })
    if year_resolutions:
        logger.info(
            f"R9a 年份投票归一：抑制 {len(year_resolutions)} 条误读派生的 "
            f"signature_time_anomaly（{year_resolutions[0]}）"
        )
    return findings


# ---------------------------------------------------------------------------
# R10: step-number gaps — 批记录工序号应连续（1..N），缺口提示缺页/漏页
# 或 OCR 漏识别。对齐"编号类程序化校验"需求（17 条内置规则方向）。
#
# 防误报设计：
# - 子工序号 "3.1" 归并为整数 3（与 _step_sort_key 的数值语义一致）
# - 数值工序号去重后 < 3 个不检查（封面/附录页样本太少无统计意义）
# - 同一工序号跨页出现（续表）是正常装订，去重后不触发
# - 缺口数 > 已识别数的一半时降级 info（可能混入附表/设备编号体系）
# ---------------------------------------------------------------------------

_STEP_NO_RE = re.compile(r"(\d+)")


def _check_step_number_gaps(pages: list[dict]) -> list[dict]:
    """Report gaps in the numeric step_no sequence across all pages."""
    step_pages: dict[int, int] = {}  # step_no(int) -> first page seen
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            raw = step.get("step_no")
            if raw is None:
                continue
            m = _STEP_NO_RE.search(str(raw))
            if not m:
                continue  # "附表A" 等非数字编号不参与
            n = int(m.group(1))
            if n < 1:
                continue
            step_pages.setdefault(n, pno)

    nums = sorted(step_pages)
    if len(nums) < 3:
        return []
    max_n = nums[-1]
    missing = [n for n in range(1, max_n + 1) if n not in step_pages]
    if not missing:
        return []

    # 连续缺口段合并描述：3,4,5 → "3-5"
    segments: list[str] = []
    start = prev = missing[0]
    for n in missing[1:]:
        if n == prev + 1:
            prev = n
        else:
            segments.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    segments.append(str(start) if start == prev else f"{start}-{prev}")

    # 缺口占比过高 → 编号体系混杂嫌疑（附表/设备号），降级 info
    severity = "warning" if len(missing) * 2 <= len(nums) else "info"
    desc = (
        f"工序编号不连续：已识别 {len(nums)} 个工序号（最大 {max_n}），"
        f"缺少 {len(missing)} 个（{','.join(segments[:6])}"
        f"{'…' if len(segments) > 6 else ''}），"
    )
    if severity == "warning":
        desc += "请核对是否存在缺页/漏页或 OCR 漏识别"
    else:
        desc += "缺口较多，可能为附表/其他编号体系混入，请人工确认"
    first_gap_page = step_pages.get(missing[0] + 1, step_pages.get(missing[0] - 1, 1))
    return [{
        "page": first_gap_page,
        "type": "step_gap",
        "severity": severity,
        "description": desc,
        "ocr_text": f"step_nos={nums} missing={missing}",
        "operator": "",
        "source": "rule",
    }]
