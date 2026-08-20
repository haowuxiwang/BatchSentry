from __future__ import annotations

import logging

from core.rules.parsing import (
    _interval_after,
    _normalize_batch_no,
    _parse_time,
    _parse_time_interval,
)

logger = logging.getLogger(__name__)


# A step with recorded start/end time but no operator signature is a GMP
# compliance gap (signature missing). Cover/toc pages legitimately lack
# signatures, so we only flag steps that have time data.
# ---------------------------------------------------------------------------


def _check_completeness(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, issue) — LLM may emit dup steps
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            has_time = bool(step.get("start_time") or step.get("end_time"))
            has_operation = bool(step.get("operation"))
            if not has_time:
                # Skip cover/toc/heading steps without operation description
                if not has_operation:
                    continue
                # Steps with operation but no time — flag as GMP gap
                # (missing date means execution time cannot be traced)
                step_no = step.get("step_no", "?")
                key = (pno, str(step_no), "no_time")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"有操作描述但缺少执行时间记录"
                        ),
                        "ocr_text": f"step_no={step_no} time=空",
                        "operator": "",
                        "source": "rule",
                    })
                continue
            step_no = step.get("step_no", "?")
            sigs = step.get("signatures", []) or []
            has_operator = bool(step.get("operator")) or any(
                (s.get("role") or "").lower() in ("operator", "操作人", "操作员")
                for s in sigs
            )
            has_reviewer = bool(step.get("reviewer")) or any(
                (s.get("role") or "").lower() in ("reviewer", "复核人", "复核员", "workshop_reviewer")
                for s in sigs
            )
            has_qa = any(
                (s.get("role") or "").lower() in ("qa", "qa_reviewer", "质量保证", "qa审核", "批准人", "放行人")
                for s in sigs
            )
            if not has_operator:
                key = (pno, str(step_no), "operator")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"已记录操作时间但缺少操作人签名"
                        ),
                        "ocr_text": f"step_no={step_no} operator=空",
                        "operator": "",
                        "source": "rule",
                    })
            if not has_reviewer:
                key = (pno, str(step_no), "reviewer")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"缺少复核人签名"
                        ),
                        "ocr_text": f"step_no={step_no} reviewer=空",
                        "operator": "",
                        "source": "rule",
                    })
            if not has_qa:
                # QA signature required for GMP compliance on executed steps
                key = (pno, str(step_no), "qa")
                if key not in seen:
                    seen.add(key)
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} "
                            f"{(step.get('operation') or '')[:40]} "
                            f"缺少 QA 签名"
                        ),
                        "ocr_text": f"step_no={step_no} qa=空",
                        "operator": "",
                        "source": "rule",
                    })
    return findings

# image. Info-level only: never blocks, but tells the reviewer where to look.
# ---------------------------------------------------------------------------


def _check_handwritten_notes(pages: list[dict]) -> list[dict]:
    findings = []
    seen: set[tuple] = set()  # dedup by (page, step_no, first note)
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            notes = [str(n).strip() for n in (step.get("handwritten") or []) if str(n).strip()]
            if not notes:
                continue
            step_no = step.get("step_no", "?")
            key = (pno, str(step_no), notes[0])
            if key in seen:
                continue
            seen.add(key)
            preview = notes[0][:30]
            extra = len(notes) - 1
            findings.append({
                "page": pno,
                "type": "handwritten",
                "severity": "info",
                "description": (
                    f"第{pno}页 工序{step_no} 含 {len(notes)} 条手写内容"
                    f"（如「{preview}」{f' 等 {extra} 条' if extra else ''}）"
                    f"，手写体 OCR 易误读，请对照 PDF 原页人工核对"
                ),
                "ocr_text": "；".join(notes)[:200],
                "operator": "",
                "source": "rule",
            })
    return findings

# R7: batch number consistency — all pages should share the same batch_no.
# Mixed batch numbers across pages indicate binding errors or cross-batch
# contamination (serious GMP deviation).
# ---------------------------------------------------------------------------


def _check_batch_consistency(pages: list[dict]) -> list[dict]:
    """Check that all pages with a batch_no use the same value.
    Pages without batch_no (cover, toc, appendix) are skipped.

    OCR variants of the SAME batch number are grouped before comparison:
    separators ("·"/spaces/"°"), full-width glyphs, digit confusions
    ("25010i" → "250101") and sheet-suffix numbers ("1127011N250101-04")
    all collapse onto one core. Prefix-truncated extractions (LLM cut the
    batch no short) merge into the longer group. Only groups that survive
    normalization are reported as a real inconsistency."""
    findings = []
    # normalized core (before "-suffix") -> {raw batch_no: [page numbers]}
    cores: dict[str, dict[str, list[int]]] = {}
    for page in pages:
        pno = page["page"]
        # batch_no 由 _normalize_pages 嵌套在 page_info 中（生产 schema）
        bno = ((page.get("page_info") or {}).get("batch_no") or "").strip()
        if not bno:
            continue
        norm = _normalize_batch_no(bno)
        if not norm:
            continue
        # Sheet suffixes ("-02"…"-06" 工序页号) belong to the same batch core
        raw_map = cores.setdefault(norm.split("-")[0], {})
        raw_map.setdefault(bno, []).append(pno)

    # Merge prefix-truncated cores into the longer group they extend
    merged: dict[str, dict[str, list[int]]] = {}
    for core, raw_map in sorted(cores.items(), key=lambda kv: -len(kv[0])):
        host = next(
            (g for c, g in merged.items() if core.startswith(c) or c.startswith(core)),
            None,
        )
        if host is None:
            merged[core] = raw_map
        else:
            for raw, pages_ in raw_map.items():
                host.setdefault(raw, [])
                host[raw].extend(pages_)

    if len(merged) <= 1:
        return findings  # all same (or none) — consistent

    # Report: main group + surviving suspicious groups, noting merged variants
    items = sorted(merged.items(), key=lambda kv: -sum(len(v) for v in kv[1].values()))
    main_core, main_map = items[0]
    summary_parts = []
    for core, raw_map in items:
        pns = sorted(p for pages_ in raw_map.values() for p in pages_)
        summary_parts.append(f"{core}(第{','.join(str(p) for p in pns)}页)")
    n_variants = sum(len(m) for m in merged.values()) - len(merged)
    all_raw = [raw for m in merged.values() for raw in m]
    findings.append({
        "page": min(min(v) for v in main_map.values()),
        "type": "batch_inconsistency",
        "severity": "critical",
        "description": (
            f"跨页批号不一致：检测到 {len(merged)} 组不同批号 — "
            f"{'；'.join(summary_parts[:3])}"
            f"{'…' if len(summary_parts) > 3 else ''}"
            f"（已归并 {n_variants} 个空格/分隔符/工序后缀等 OCR 变体），"
            f"请核对是否装订错误或混批"
        ),
        "ocr_text": f"batch_nos={all_raw}",
        "operator": "",
        "source": "rule",
    })
    logger.warning(
        f"R7 batch inconsistency: {len(merged)} distinct batch groups "
        f"({len(all_raw)} raw variants, {n_variants} merged)"
    )
    return findings


# ---------------------------------------------------------------------------
# R8: check consistency — a QA/review checkbox answered "否" (failed) must
# be traceable to a deviation record; answered "是" on an item that is
# clearly N/A-safe is fine. The check data comes from steps[].checks[]
# (printed "√是/□否" option templates are NOT real checkboxes — the LLM
# prompt filters those; here we only judge what survived extraction).
# ---------------------------------------------------------------------------


def _check_check_consistency(pages: list[dict]) -> list[dict]:
    findings = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            for c in step.get("checks", []) or []:
                selected = (c.get("selected") or "").strip()
                item = (c.get("item") or "").strip()
                marker = (c.get("marker") or "").strip()
                if not selected or not item:
                    continue
                if selected == "否":
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 检查项「{item[:40]}」勾选为“否”"
                            f"（{marker or '勾选标记'}），需确认是否已启动偏差"
                            f"处理并在记录中留痕"
                        ),
                        "ocr_text": f"item={item[:40]} selected=否",
                        "operator": "",
                        "source": "rule",
                    })
                elif selected == "无法识别":
                    findings.append({
                        "page": pno,
                        "type": "completeness",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 检查项「{item[:40]}」勾选状态无法识别"
                            f"（{marker or '手绘勾选'}），请对照 PDF 原页人工核对"
                        ),
                        "ocr_text": f"item={item[:40]} selected=无法识别",
                        "operator": "",
                        "source": "rule",
                    })
    return findings

# OCR may misread handwritten values (e.g. "0.974" → "0.914"). Low confidence
# values that pass spec check could mask a real out-of-spec result. We flag
# them for human verification rather than silently trusting the OCR output.
# ---------------------------------------------------------------------------


def _check_low_confidence_params(pages: list[dict]) -> list[dict]:
    """Flag parameter values with low OCR confidence for human review.

    A low-confidence value that passes spec check might actually be
    out-of-spec if the OCR misread a handwritten digit. We surface these
    cases so reviewers can verify against the original document.
    """
    findings = []
    seen: set[tuple] = set()
    for page in pages:
        pno = page["page"]
        # overall_confidence is emitted by the per-page LLM (v3 prompt). In
        # _normalize_pages it stays nested under page["data"]; unit tests
        # pass it at the top level. Check both locations so the rule fires
        # in production and in tests.
        overall_conf = (
            page.get("overall_confidence")
            or (page.get("data") or {}).get("overall_confidence")
            or ""
        ).lower()
        # If overall page confidence is low, flag the whole page
        if overall_conf == "low":
            key = (pno, "page_low_confidence")
            if key not in seen:
                seen.add(key)
                findings.append({
                    "page": pno,
                    "type": "completeness",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 整体识别置信度较低，"
                        f"建议人工核对 OCR 结果（可能存在手写体或印章干扰）"
                    ),
                    "ocr_text": f"overall_confidence={overall_conf}",
                    "operator": "",
                    "source": "rule",
                })
    return findings


def _check_measurement_time_sequence(pages: list[dict]) -> list[dict]:
    """R-M1: 同一工序（step_no）的跨页测量时间序列应单调不减。

    批记录的参数矩阵常跨页（大表在 51 页实测文件中跨页出现），R1-a/b 只
    比较 step.start_time/end_time，逐行测量时间（measurements[].time）从未
    被校验——缺行/行序错乱/时间倒序会静默通过。此规则把同 step_no 的所有
    页测量行按 (页序, 行序) 拼接后检查严格倒序（time_prev > time_curr）。
    相等的重复测量（同一分钟多次取样）合理，不报告；解析失败点打断连续
    性（避免把跨无效时间的行误判为倒序）。
    """
    findings: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for page in pages:
        pno = page["page"]
        fb_date = page["page_info"].get("production_date")
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            for m in step.get("measurements", []) or []:
                if not isinstance(m, dict):
                    continue
                groups.setdefault(key, []).append({
                    "page": pno,
                    "time_raw": m.get("time"),
                    "t": _parse_time(m.get("time"), fb_date),
                    "iv": _parse_time_interval(m.get("time"), fb_date),
                })
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        prev: dict | None = None
        for row in rows:
            if row["t"] is None:
                prev = None  # 解析失败点打断连续性，避免误报
                continue
            # Interval comparison: date-only times span the whole day, so a
            # date-only row that touches the previous datetime row on the
            # same day is not a reversal (e.g. "2024-01-01" vs "2024-01-01 14:30").
            if prev is not None and prev["iv"] and row["iv"] and _interval_after(prev["iv"], row["iv"]):
                findings.append({
                    "page": row["page"],
                    "type": "time_reversal",
                    "severity": "warning",
                    "description": (
                        f"工序{key} 测量时间跨页倒序：第{prev['page']}页 "
                        f"{prev['time_raw']} 晚于 第{row['page']}页 {row['time_raw']}"
                        f" — 可能表格缺行、行序错乱或 OCR 提取错误，请人工核对参数矩阵"
                    ),
                    "ocr_text": f"{prev['time_raw']} > {row['time_raw']}",
                    "operator": "",
                    "source": "rule",
                })
            prev = row
    return findings


def _check_measurement_column_consistency(pages: list[dict]) -> list[dict]:
    """R-M2: 同一工序（step_no）跨页参数矩阵列集合应一致。

    大表跨页时每页表头重印；若某页测量行 values 键缺失（整列丢失），
    R3 的逐格判定无法发现"整列缺失"（它只看有值的格）。此规则只报
    "缺列"（多出来的列可能是新增参数，不误报），且要求该 step 跨 ≥2 页
    且有 ≥2 行数据，避免小样本噪音。
    """
    findings: list[dict] = []
    groups: dict[str, dict] = {}
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            key = str(step.get("step_no", "?"))
            g = groups.setdefault(key, {"pages": set(), "rows": 0, "page_cols": {}})
            rows = step.get("measurements", []) or []
            g["rows"] += len(rows)
            cols: set[str] = set()
            for m in rows:
                if isinstance(m, dict) and isinstance(m.get("values"), dict):
                    cols.update(m["values"].keys())
            if cols:
                g["pages"].add(pno)
                g["page_cols"][pno] = cols
    for key, g in groups.items():
        if len(g["pages"]) < 2 or g["rows"] < 2:
            continue
        all_cols: set[str] = set()
        for cols in g["page_cols"].values():
            all_cols.update(cols)
        for pno in sorted(g["page_cols"]):
            missing = all_cols - g["page_cols"][pno]
            if not missing:
                continue
            missing_sorted = sorted(missing)
            findings.append({
                "page": pno,
                "type": "completeness",
                "severity": "info",
                "description": (
                    f"工序{key} 参数矩阵跨页列不一致：第{pno}页缺列 "
                    f"{'、'.join(missing_sorted)}（其余页均有）— "
                    f"可能表格截断或提取丢失，请人工核对"
                ),
                "ocr_text": "missing: " + "、".join(missing_sorted),
                "operator": "",
                "source": "rule",
            })
    return findings
