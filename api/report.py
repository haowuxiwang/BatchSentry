"""Report API — generate and download Markdown + JSON reports.

缓存策略：报告内容缓存在内存中（FIFO，maxsize=32），key 为 (job_id, findings_count,
last_finding_id)。findings 数量或最后一条 finding id 变化时自动失效。
job 删除时缓存项自然淘汰。

并发安全：_report_cache 用 asyncio.Lock 保护，防止 SSE 请求 + 报告请求并发读写导致
字典迭代器失效或 key 覆盖。
"""
import asyncio
import html
import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from db.client import get_db
from core.zh_map import zh_finding_status, zh_severity
from core.kb.retriever import dedup_refs_for_report

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["report"])


# 缓存：key=(job_id, findings_count, last_finding_id) → markdown 文本
_report_cache: dict[tuple, str] = {}
_report_cache_lock = asyncio.Lock()
_REPORT_CACHE_MAX = 32


async def _audit_report_export(job_id: str, fmt: str, size: int) -> None:
    """报告导出写 audit_log（对抗审查：导出是质量体系事件，此前无留痕）。

    失败不阻断导出（审计是附属动作）。
    """
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime(\'now\',\'localtime\'))",
            (job_id, "report_export", f"format={fmt} size={size}"),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write report_export audit log: {e}")


async def _load_exemptions(db, job_id: str) -> list[dict]:
    """加载本 job 的 OCR 完整性人工豁免清单（门禁 2 可追溯透出）。

    从 page_cache.ocr_diagnostics.exemption 提取已确认豁免的页，返回按页码
    排序的 [{page, reason, created_at, reasons}]。空列表 = 无豁免记录。
    """
    exemptions: list[dict] = []
    cursor = await db.execute(
        "SELECT page, ocr_diagnostics FROM page_cache "
        "WHERE job_id = ? AND ocr_diagnostics IS NOT NULL",
        (job_id,),
    )
    for r in await cursor.fetchall():
        try:
            diag = json.loads(r["ocr_diagnostics"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(diag, dict) and diag.get("exemption"):
            exemptions.append({
                "page": int(r["page"]),
                "reason": diag["exemption"].get("reason", ""),
                "created_at": diag["exemption"].get("created_at", ""),
                "reasons": diag.get("reasons") or [],
            })
    exemptions.sort(key=lambda x: x["page"])
    return exemptions


async def _generate_report_md_cached(job_id: str) -> str:
    """生成 Markdown 报告（带缓存，并发安全）。

    缓存 key 为 (job_id, findings_count, last_finding_id)。
    复核操作改变 findings 时，下次请求会因 key 变化而重新生成。
    """
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT * FROM findings WHERE job_id = ? ORDER BY page, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 END, id",
        (job_id,),
    )
    findings = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        "SELECT total_pages FROM jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    # 总页数以 jobs.total_pages 为准（OCR 时写入的物理页数，P1-4 保证
    # 缺页时也保持物理页数）；page_cache 行数可能因 OCR 失败而偏少，
    # 旧实现用它导致报告页数比真实 PDF 少（GMP 报告信息失真）。
    total_pages = row["total_pages"] if row and row["total_pages"] else 0
    if not total_pages:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM page_cache WHERE job_id = ?", (job_id,)
        )
        total_pages = (await cursor.fetchone())[0]

    # 缓存 key：findings 数量 + 最后一条 finding 的 id
    last_id = findings[-1]["id"] if findings else 0
    # Include status hash in cache key so review operations (confirm/reject/correct)
    # invalidate the cache — without this, reports show stale finding statuses.
    # 对抗审查 P1-A：必须把 corrected_text/reviewer_note/reviewed_at 也纳入 —
    # 复核接口允许不改 status 单独更新这两个字段（同状态二次修正），原 key
    # 只看 (id, status) → 修正内容变化后报告仍返回旧缓存文本（GMP 场景下
    # 报告静默携带过期内容，用户以为导出的是最新版本）。
    status_hash = hash(tuple(sorted(
        (f["id"], f["status"], f.get("corrected_text") or "", f.get("reviewer_note") or "")
        for f in findings
    )))

    # 生成报告（在锁外执行，避免长时间持锁）
    exemptions = await _load_exemptions(db, job_id)
    # round-23 C：复核反馈统计（纯函数聚合已载入的 findings 行 —
    # 缓存 key 的 status_hash 已含 (id,status) 对，裁决变化自动失效）
    from api.review import _review_stats_from_rows
    review_stats = _review_stats_from_rows(findings)
    # 对抗审查 P1：零 findings ≠ 全部合规。OCR 全空页/分析缺失时 job 照样
    # 终态 review，旧报告输出"✅ 无需人工复核"= 静默合规通过假象。
    # 报告头部与汇总必须声明 OCR 覆盖情况，供复核者判定可信度。
    empty_pages = 0
    unanalyzed_pages = 0
    pc = await db.execute(
        "SELECT structured_json FROM page_cache WHERE job_id = ?", (job_id,)
    )
    for r in await pc.fetchall():
        sj_raw = r["structured_json"]
        if not sj_raw:
            unanalyzed_pages += 1
            continue
        try:
            sj = json.loads(sj_raw)
        except json.JSONDecodeError:
            unanalyzed_pages += 1
            continue
        if isinstance(sj, dict) and sj.get("_ocr_empty"):
            empty_pages += 1
    # 缓存 key：findings 数量 + 最后一条 finding 的 id + status_hash +
    # 豁免清单规模（记录/撤销豁免不改变 findings，但改变报告内容）。
    cache_key = (job_id, len(findings), last_id, status_hash, len(exemptions))

    # 并发安全：用锁保护字典读写
    async with _report_cache_lock:
        if cache_key in _report_cache:
            logger.info(f"[{job_id}] Report.md cache hit (findings={len(findings)})")
            return _report_cache[cache_key]

    # 生成报告（在锁外执行，避免长时间持锁）
    md = _generate_markdown(job, findings, total_pages, exemptions,
                            empty_pages=empty_pages,
                            unanalyzed_pages=unanalyzed_pages,
                            review_stats=review_stats)

    # 写入缓存，清理超出的项
    async with _report_cache_lock:
        _report_cache[cache_key] = md
        if len(_report_cache) > _REPORT_CACHE_MAX:
            # 简单 FIFO 淘汰：删除最早插入的 key
            oldest = next(iter(_report_cache))
            del _report_cache[oldest]

    logger.info(
        f"[{job_id}] Report.md generated and cached: {len(findings)} findings, {len(md)} chars"
    )
    return md


@router.get("/jobs/{job_id}/report.md", response_class=PlainTextResponse)
async def download_report_md(job_id: str, request: Request = None):
    """Generate and return Markdown report (cached)."""
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    md = await _generate_report_md_cached(job_id)
    await _audit_report_export(job_id, "md", len(md))
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/jobs/{job_id}/report.json")
async def download_report_json(job_id: str, request: Request = None):
    """Return structured JSON report with job metadata + findings.

    Phase 3 fix: include job field (filename/status/total_pages/stages) so
    downstream consumers (e.g. E2E test, external integrations) can identify
    the job without a separate API call.
    """
    # P2-1: GET 读端点守卫统一
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    db = await get_db()
    cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    job = await cursor.fetchone()
    if not job:
        raise HTTPException(404, "Job not found")

    cursor = await db.execute(
        "SELECT * FROM findings WHERE job_id = ? ORDER BY page, "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 END, "
        "CASE COALESCE(source, 'rule') WHEN 'rule' THEN 0 "
        "WHEN 'llm_fallback' THEN 1 WHEN 'llm_page' THEN 2 "
        "WHEN 'llm_cross' THEN 3 ELSE 4 END, id",
        (job_id,),
    )
    findings = [dict(r) for r in await cursor.fetchall()]
    exemptions = await _load_exemptions(db, job_id)
    # round-23 C：复核反馈统计随 JSON 报告导出（下游做规则阈值调优分析）
    from api.review import _review_stats_from_rows
    review_stats = _review_stats_from_rows(findings)
    logger.info(f"[{job_id}] Report.json generated: {len(findings)} findings, {len(exemptions)} exemptions")
    await _audit_report_export(job_id, "json", len(findings))
    return {
        # v9: 对外 JSON 报告加版本字段 —— 后续列演进（如 kb_refs）时
        # 下游消费者可据此分支，避免 SELECT * 直出的静默漂移。
        "schema_version": 1,
        "job": {
            "id": job["id"],
            "filename": job["filename"],
            "status": job["status"],
            "total_pages": job["total_pages"],
            "stage1_ms": job["stage1_ms"],
            "stage2_ms": job["stage2_ms"],
            "stage3_ms": job["stage3_ms"],
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
        },
        "findings": findings,
        "count": len(findings),
        "ocr_exemptions": exemptions,
        "review_stats": review_stats,
    }


def _append_exemption_section(lines: list[str], exemptions: list[dict], esc) -> None:
    """门禁 2：在报告中追加 OCR 完整性豁免清单章节。"""
    lines.append("---")
    lines.append("")
    lines.append("## OCR 完整性豁免（人工已核对原图）")
    lines.append("")
    for ex in exemptions:
        lines.append(
            f"- **第{ex['page']}页** | {esc(ex['created_at'])} | "
            f"原因: {esc(ex['reason'])}"
        )
        if ex.get("reasons"):
            lines.append(f"  - 原始完整性原因: {esc('；'.join(ex['reasons']))}")
    lines.append("")


def _append_review_stats_section(lines: list[str], stats: dict, esc) -> None:
    """round-23 C：复核反馈统计章节（确认/驳回率 + 高频驳回类型 +
    按来源驳回率）— 复核数据回流报告，反哺规则阈值调优。"""
    from core.zh_map import zh_finding_type as _zh_type

    lines.append("---")
    lines.append("")
    lines.append("## 复核反馈统计")
    lines.append("")
    lines.append(
        f"- **已裁决**: {stats['adjudicated']}/{stats['total']} 条"
        f"（确认 {stats['by_status']['confirmed']} · 驳回 "
        f"{stats['by_status']['rejected']} · 修正 "
        f"{stats['by_status']['corrected']} · 待复核 {stats['pending']}）"
    )
    if stats["adjudicated"]:
        lines.append(
            f"- **确认率**: {stats['confirm_rate'] * 100:.1f}% · "
            f"**驳回率**: {stats['reject_rate'] * 100:.1f}%"
        )
    else:
        lines.append("- 尚无已裁决条目（确认率/驳回率待复核后统计）。")
    if stats["top_rejected_types"]:
        lines.append("")
        lines.append("### 高频驳回类型")
        lines.append("")
        for t in stats["top_rejected_types"]:
            lines.append(
                f"- {esc(_zh_type(t['type']))}"
                f"（`{esc(t['type'])}`）: {t['count']} 条"
                f"（占驳回 {t['share'] * 100:.1f}%）"
            )
    if stats["by_source"]:
        lines.append("")
        lines.append("### 按来源驳回率（规则阈值调优信号）")
        lines.append("")
        for s in stats["by_source"]:
            flag = " ⚠️ 驳回率偏高，建议核查该层阈值/提示词" if (
                s["reject_rate"] > 0.5 and s["total"] >= 4
            ) else ""
            lines.append(
                f"- `{esc(s['source'])}`: {s['total']} 条中驳回 "
                f"{s['rejected']} 条（{s['reject_rate'] * 100:.1f}%，"
                f"待复核 {s['pending']}）{flag}"
            )
    lines.append("")


def _generate_markdown(job: dict, findings: list[dict], total_pages: int,
                       exemptions: list[dict] | None = None,
                       empty_pages: int = 0,
                       unanalyzed_pages: int = 0,
                       review_stats: dict | None = None) -> str:
    """Build Markdown report from findings."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    SeverityIcon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    StatusIcon = {
        "pending": "⏳",
        "confirmed": "✅",
        "rejected": "❌",
        "corrected": "✏️",
    }

    # 对抗审查(cr-7): 报告里的 LLM/OCR 内容是模型生成的可疑文本，可能含
    # HTML/脚本。Markdown 本身浏览器不渲染，但用户常用 Typora/Obsidian 等
    # 自动渲染 HTML 的编辑器打开 → 形成 XSS 执行面。统一 HTML-escape。
    # 对抗审查 P1-B：html.escape 只转义 <>& — LLM 文本里的 `![x](url)` /
    # `[x](...)` 在 Typora/Obsidian 中成为真实图片/链接（远端图片会触发
    # 请求 = 隐私泄露/追踪；路径外链可指向 file://）。对 LLM/OCR 无信任
    # 文本额外转义 Markdown 元字符，使其成为纯文本。
    def esc(text) -> str:
        s = html.escape(str(text), quote=False)
        # 转义 Markdown 图像/链接/强调元字符 — 仅限无信任来源字段
        for ch in ("!", "[", "]", "(", ")"):
            s = s.replace(ch, "&#{};".format(ord(ch)))
        return s

    lines = [
        "# GMP 批生产记录合规检查报告",
        "",
        f"- **文件名**: {esc(job['filename'])}",
        f"- **总页数**: {total_pages}",
        f"- **生成时间**: {now}",
        f"- **Job ID**: {job['id']}",
        f"- **总 Findings**: {len(findings)}",
    ]
    # 对抗审查 P1：OCR 覆盖不完整时报告头部必须显式声明 —
    # 零 findings 可能只是数据缺失，不是合规通过。
    if empty_pages or unanalyzed_pages:
        lines.append(
            f"- **页面覆盖**: {empty_pages} 页 OCR 内容为空，"
            f"{unanalyzed_pages} 页未完成分析"
        )
    lines += [
        "",
        "---",
        "",
        "## Findings 列表",
        "",
    ]

    # 空报告文案：无 findings 时明确告知"未发现问题"，而不是只输出
    # 空分组（复核者导出空报告会困惑是否数据丢失）
    if not findings:
        lines.append("## Findings 列表")
        lines.append("")
        lines.append("未发现问题。")
        lines.append("")
        if exemptions:
            _append_exemption_section(lines, exemptions, esc)
        lines.append("---")
        lines.append("")
        lines.append("## 汇总")
        lines.append("")
        if empty_pages or unanalyzed_pages:
            # 覆盖不完整：零问题可能只是数据缺失，不得宣称合规通过
            lines.append(
                f"⚠️ 未发现问题，但页面覆盖不完整"
                f"（{empty_pages} 页内容为空、{unanalyzed_pages} 页未完成分析）。"
                f"零问题记录可能源于数据缺失，请人工核对 PDF 原件后再判定。"
            )
        else:
            lines.append("✅ 未发现问题，无需人工复核。")
        lines.append("")
        return "\n".join(lines)

    # Group by severity
    for sev in ["critical", "warning", "info"]:
        sev_findings = [f for f in findings if f["severity"] == sev]
        if not sev_findings:
            continue
        icon = SeverityIcon.get(sev, "")
        lines.append(f"### {icon} {zh_severity(sev)} ({len(sev_findings)})")
        lines.append("")
        for f in sev_findings:
            st_icon = StatusIcon.get(f["status"], "")
            lines.append(f"- **第{f['page']}页** | `{esc(f['type'])}` {st_icon} {zh_finding_status(f['status'])}")
            lines.append(f"  - {esc(f['description'])}")
            if f.get("ocr_text"):
                # P2: 先截原始文本再转义 — 反过来的话实体（如 &#33;）会被
                # 拦腰截断，渲染成字面 "&#33"，且实际展示字符数不足
                lines.append(f"  - OCR原文: `{esc(f['ocr_text'][:100])}`")
            # GMP 依据引用（v7）：报告携带法规依据（gmp_basis.py 知识库映射）
            if f.get("gmp_basis"):
                lines.append(f"  - 法规依据: {esc(f['gmp_basis'])}")
            if f.get("corrected_text"):
                lines.append(f"  - 修正为: `{esc(f['corrected_text'][:100])}`")
            if f.get("reviewer_note"):
                lines.append(f"  - 审查员备注: {esc(f['reviewer_note'])}")
            lines.append("")

    # 门禁 2（OCR 金标发布门禁）：豁免页清单 — 人工已核对原图的 OCR
    # 不完整页面，在报告中显式列出，GMP 审计可追溯。
    if exemptions:
        _append_exemption_section(lines, exemptions, esc)

    # v8 知识库：本次 findings 引用的法规条文附录（去重，全文）
    kb_cited = dedup_refs_for_report(findings)
    if kb_cited:
        from core.kb.retriever import group_refs_by_source as _group_kb
        from core.kb.store import get_entry as _kb_entry

        lines.append("## 依据条文附录")
        lines.append("")
        lines.append(
            "> 以下为本次问题清单引用的法规条文，**按来源分组**；每条引用可追溯到"
            "具体法规、文号与知识库版本。标注「要点摘编」的条目为外文法规的中文"
            "摘编，非条文原文。"
        )
        lines.append("")
        for meta, _refs in _group_kb(kb_cited):
            title = meta.get("title") or meta.get("source_id") or "未标注来源"
            bits = []
            if meta.get("document_no"):
                bits.append(str(meta["document_no"]))
            if meta.get("version"):
                bits.append(f"版本 {meta['version']}")
            suffix = f"（{'，'.join(bits)}）" if bits else ""
            lines.append(f"### {title}{suffix}")
            lines.append("")
            for r in _refs:
                e = _kb_entry(r["entry_id"])
                body = e["text"] if e else r.get("excerpt", "")
                kind = (r.get("text_kind")
                        or (e or {}).get("text_kind") or "original")
                mark = "" if kind == "original" else "（要点摘编）"
                lines.append(
                    f"#### {r['label']}{mark}（{r.get('chapter', '')}）"
                )
                lines.append("")
                lines.append(esc(body))
                lines.append("")
    else:
        from core.kb.store import source_meta as _kb_meta

        if _kb_meta()["source_id"]:
            pass  # 知识库已装载但本报告无引用 —— 不输出空章节
        # 知识库未装载时同样静默：附录是增强项，非必需章节

    # round-23 C：复核反馈统计章节（有 findings 才有意义 — 零 findings
    # 无反馈数据，不输出空节）
    if review_stats and review_stats.get("total"):
        _append_review_stats_section(lines, review_stats, esc)

    # Summary
    pending = len([f for f in findings if f["status"] == "pending"])
    lines.append("---")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    if pending:
        lines.append(f"⚠️ **{pending} 条 Finding 需人工复核**，请在复核界面确认/拒绝/修正后重新导出。")
    else:
        lines.append("✅ 所有 Findings 已处理完毕。")
    lines.append("")

    return "\n".join(lines)
