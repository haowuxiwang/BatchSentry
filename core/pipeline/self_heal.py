"""Empty-page self-heal: re-OCR truncated pages as small slices (module refactor)"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from config import config
from core.pipeline.ocr_support import _sanitize_ocr_text
from core.pipeline.state import _audit_log
from core.security import redact_urls

logger = logging.getLogger(__name__)

# 缺失内容标记：任何页面文本出现这些即视为"内容不完整"（含整表降级
# stub simple_table / 表格提取失败占位 / 空页占位）。自愈验收、空页判定
# 及其它完整性检查共用，防止"111 字符的 simple_table stub 被当作已恢复"。
_MISSING_MARKERS = (
    "simple_table",
    "[表格内容提取失败 — OCR 结构缺失]",
    "（此页无文本内容）",
    "[OCR 警告:",
)

# OCR 警告前缀（stage1.py 写入 raw_html 的完整性诊断标签）：strip 后
# 再判断空页长度，避免前缀膨胀导致"非空"误判。_has_missing_markers 已
# 能检测该前缀，但语义上长度检查应基于实际内容而非诊断标签。
_OCR_WARNING_RE = re.compile(
    r"^\[OCR 警告: .+?分析仅供参考\]\n\n", re.DOTALL
)


def _has_missing_markers(text: str) -> bool:
    raw = text or ""
    if any(m in raw for m in _MISSING_MARKERS):
        return True
    # MinerU may return a non-empty markdown page composed exclusively of its
    # page marker and header blocks.  A length threshold cannot catch this
    # failure mode; treat it as incomplete so self-heal does not certify it.
    content_lines = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not re.fullmatch(r"##\s*第\s*\d+\s*页", line.strip())
    ]
    return bool(content_lines) and all(line.startswith("#") for line in content_lines)


def _self_heal_diag(prior: dict | None, *, recovered: bool = False,
                    content_len: int = 0, round_num: int = 0) -> str | None:
    """自愈恢复页的诊断 JSON：保留原始完整性证据，标记自愈状态。

    门禁 1（页级诊断可追溯）：旧实现自愈 UPDATE 把 ocr_diagnostics 置 NULL，
    该页"曾因空页/缺失占位被判不完整"的证据就此丢失。恢复页应能回答
    "此页为何被重跑" — 以 prior_diagnostics 存原始诊断 + self_healed 标记。
    新增：recovery_round / content_length / recovered 标记，便于审计追踪
    自愈效果（哪一轮恢复、恢复后内容量）。
    """
    if not prior:
        return None
    diag = {
        "self_healed": True,
        "recovered": recovered,
        "source": prior.get("source", "unknown"),
        "prior_diagnostics": prior,
    }
    if round_num:
        diag["recovery_round"] = round_num
    if content_len:
        diag["content_length"] = content_len
    return json.dumps(diag, ensure_ascii=False)

async def _report_heal_progress(db, job_id: str, done: int, total: int, pages: list[int]) -> None:
    """空页自愈进度上报：读当前 ocr_progress 主进度，合并 self_heal 子键。

    自愈期间主 OCR 进度已 done==total，SSE 客户端看不到任何变化，
    长自愈（几十秒）会被误判为卡死 — 该键让前端显示"空页自愈 x/y"。
    """
    from core.pipeline import _update_self_heal_progress as _run_update
    main_done = main_total = 0
    try:
        cursor = await db.execute(
            "SELECT ocr_progress FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        if row and row["ocr_progress"]:
            data = json.loads(row["ocr_progress"])
            main_done = int(data.get("done", 0))
            main_total = int(data.get("total", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    await _run_update(job_id, main_done, main_total, done, total, pages)


async def _self_heal_empty_pages(
    db, job_id: str, pdf_path: str, pages: list[dict], backend: str,
    skip_pages: set[int] | None = None,
) -> list[int]:
    """Re-OCR pages whose tag-stripped text is < 100 chars. (refactor)

    返回实际恢复的页号列表（分片路径用于补跑分析；整份路径忽略返回值）。
    skip_pages: 跳过不检测的页号集合 — 分片模式下这些页的 LLM 分析已
    完成，其 raw_html 的 [OCR 警告:] 前缀会命中缺失标记，若不自愈前
    排除会把已分析页的 structured_json 清掉、触发无谓重跑。
    """
    # Runtime resolution — tests patch core.pipeline._is_cancelled.
    from core.pipeline import _is_cancelled as _run_is_cancelled
    recovered: list[int] = []  # 恢复页号（无自愈/异常路径保持空列表）
    if backend in ("mineru", "paddle"):
        # 空页判定增强（对抗审查 cr-17）：仅看 raw_html 长度会漏判
        # "标签多、文字少"的页（如 <table><tr><td></td></tr></table>
        # 无文字模板 >100 字符）。去 HTML 标签后按真实文本长度判定。
        # 小文件（<10 页）也启用自愈 — 单页切片重跑仅一次调用
        # （~5s），收益大于小文件直接静默空页的成本。
        cursor = await db.execute(
            "SELECT page, raw_html FROM page_cache WHERE job_id = ? "
            "ORDER BY page",
            (job_id,),
        )
        retry_targets = []
        _skip = skip_pages or set()
        for r in await cursor.fetchall():
            if r["page"] in _skip:
                continue
            html = r["raw_html"] or ""
            # 缺失标记（simple_table 整表降级 stub / 表格失败占位 / 空页
            # 占位）即使超 100 字符也视为未完成 — 防 111 字符 stub 逃过
            # 空页判定（对抗审查，页 48 整表丢失静默通过的真实案例）。
            if _has_missing_markers(html):
                retry_targets.append(r["page"])
                continue
            # strip OCR 警告前缀后检查长度，避免诊断标签膨胀导致误判
            content = _OCR_WARNING_RE.sub("", html)
            if len(content) < 100:
                retry_targets.append(r["page"])
                continue
            stripped = re.sub(r"<[^>]+>", "", content).strip()
            if len(stripped) < 100:
                retry_targets.append(r["page"])
        if retry_targets:
            logger.warning(
                f"[{job_id}] {len(retry_targets)} empty pages detected — "
                f"retrying as small slices: p{retry_targets}"
            )
            await _audit_log(
                db, job_id, "stage1_empty_pages",
                f"pages={retry_targets} — retrying with per-page slices",
            )
            # P0-1 修复配套：自愈 UPDATE page_cache 后同步回写内存
            # pages dict（Stage 2 从内存读取 — 若只更新 DB，首次运行
            # 时 LLM 仍收到自愈前的空文本，恢复白做）。
            pages_by_num = {i + 1: p for i, p in enumerate(pages)}
            # 门禁 1：自愈前快照各目标页的原始诊断，恢复写库时以
            # prior_diagnostics 保留（不置 NULL）。
            prior_diags: dict[int, dict] = {}
            try:
                ph = ",".join("?" * len(retry_targets))
                cursor2 = await db.execute(
                    f"SELECT page, ocr_diagnostics FROM page_cache "
                    f"WHERE job_id = ? AND page IN ({ph})",
                    [job_id, *retry_targets],
                )
                for r2 in await cursor2.fetchall():
                    if r2["ocr_diagnostics"]:
                        try:
                            prior_diags[int(r2["page"])] = json.loads(r2["ocr_diagnostics"])
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass
            try:
                if backend == "mineru":
                    from core.mineru_client import run_ocr_pages

                    recovered = []
                    still_empty = list(retry_targets)
                    # 两轮重试：首轮 3 页小批（快）；未恢复页第二轮单页批
                    # （服务端丢页有随机性，单页批成功率最高 — 实测
                    # 3 页批 4/6 恢复、单页批全部恢复过）。
                    for attempt, batch_size in enumerate((3, 1)):
                        if not still_empty:
                            break
                        # 对抗审查 P2-9：两轮重试各需 15-40s+，期间用户
                        # 取消 → 继续跑完并写库，浪费上游配额且取消
                        # 不生效。每轮前检查取消，中断并保留已恢复页。
                        if await _run_is_cancelled(job_id):
                            logger.info(
                                f"[{job_id}] Empty-page retry cancelled "
                                f"(round {attempt + 1}) — "
                                f"keeping {len(recovered)} recovered pages"
                            )
                            break
                        logger.info(
                            f"[{job_id}] Empty-page retry round {attempt + 1} "
                            f"(batch_size={batch_size}): p{still_empty}"
                        )
                        retried = await asyncio.to_thread(
                            run_ocr_pages,
                            pdf_path,
                            still_empty,
                            job_id=job_id,
                            batch_size=batch_size,
                        )
                        heal_total = len(retry_targets)
                        # 本轮仍未恢复的页（下一轮只重跑这些 — 旧实现
                        # still_empty 只增不减，第二轮会重跑第一轮已
                        # 恢复的页，浪费一倍上游配额）
                        next_pending: list[int] = []
                        for pno, md, discarded in retried:
                            # 对抗审查：恢复验收需排除"整表降级 stub"。
                            # 旧阈值只查长度 <100 → 111 字符的 simple_table
                            # stub 被误判"已恢复"，页 48 整表丢失静默通过。
                            # 合法页面文本不含这些占位/缺失标记。
                            if md and len(md.strip()) > 100 and not _has_missing_markers(md):
                                clean = _sanitize_ocr_text(md.strip())
                                # D3 修复（Round 3）：自愈恢复页也补回
                                # OCR 不完整警告前缀（主流程 L714 对
                                # 自愈路径不生效，恢复页曾被静默当作
                                # 完整页 — LLM 置信度虚高）。
                                if discarded:
                                    clean = (
                                        f"[OCR 警告: 本页有 {discarded} 个内容块"
                                        f"因置信度过低被 OCR 丢弃, 以下内容可能"
                                        f"不完整, 分析仅供参考]\n\n{clean}"
                                    )
                                await db.execute(
                                    "UPDATE page_cache SET raw_html = ?, ocr_diagnostics = ?, "
                                    "structured_json = NULL, analyzed_at = NULL "
                                    "WHERE job_id = ? AND page = ?",
                                    (clean, _self_heal_diag(prior_diags.get(pno),
                                     recovered=True, content_len=len(clean),
                                     round_num=attempt + 1), job_id, pno),
                                )
                                if pno in pages_by_num:
                                    pages_by_num[pno]["markdown"]["text"] = clean
                                recovered.append(pno)
                            else:
                                next_pending.append(pno)
                        still_empty = next_pending
                        # 每轮结束上报自愈进度（SSE 可见，防"卡死"误判）
                        await _report_heal_progress(
                            db, job_id,
                            heal_total - len(still_empty), heal_total,
                            still_empty,
                        )
                        await db.commit()
                        if not still_empty:
                            break
                else:
                    # Paddle：fitz 提取单页为独立 PDF 重提交一次。
                    # Paddle 无切片接口且服务端无大文件丢页缺陷 —
                    # 空页大概率是单次服务端波动/真空白页，一轮足够。
                    # 真空白页（扫描件末页）重跑后仍空 → 保留
                    # _ocr_empty 标记走人工复核路径。
                    import fitz
                    import core.ocr_client as ocr_client  # runtime-visible for PyInstaller

                    job_dir_p = Path(config["app"].output_dir) / job_id
                    recovered = []
                    still_empty = []
                    # 打开大 PDF + 写切片文件是磁盘 I/O（秒级），必须放线程池，
                    # 否则每个空页都在事件循环上同步停顿（同函数内 run_ocr
                    # 已包 to_thread，此处对齐）。
                    src_doc = await asyncio.to_thread(fitz.open, pdf_path)
                    try:
                        for idx, pno in enumerate(retry_targets, 1):
                            if await _run_is_cancelled(job_id):
                                logger.info(
                                    f"[{job_id}] Paddle empty-page retry "
                                    f"cancelled — keeping {len(recovered)} "
                                    f"recovered pages"
                                )
                                break
                            logger.info(
                                f"[{job_id}] Paddle self-heal: re-OCR page "
                                f"{pno} as standalone slice"
                            )
                            slice_path = job_dir_p / f"selfheal-p{pno}.pdf"

                            def _make_slice(src=src_doc, dst=slice_path, pg=pno):
                                out = fitz.open()
                                out.insert_pdf(
                                    src, from_page=pg - 1, to_page=pg - 1
                                )
                                out.save(str(dst))
                                out.close()

                            await asyncio.to_thread(_make_slice)
                            try:
                                # P0-7 修复：变量遮蔽 — 此前
                                # `pages = ...` 覆盖外层整份 OCR 结果
                                # 列表，Paddle 自愈触发后 Stage 2 的
                                # enumerate(pages) 只遍历到最后一个
                                # 自愈单页，其余页全部漏分析。
                                slice_pages = await asyncio.to_thread(
                                    ocr_client.run_ocr, str(slice_path)
                                )
                                md = (
                                    slice_pages[0]["markdown"]["text"]
                                    if slice_pages and len(slice_pages) > 0
                                    else ""
                                )
                            except Exception as slice_err:
                                logger.warning(
                                    f"[{job_id}] Paddle self-heal p{pno} "
                                    f"failed: "
                                    f"{redact_urls(str(slice_err))[:200]}"
                                )
                                md = ""
                            finally:
                                slice_path.unlink(missing_ok=True)
                            # 对抗审查：与 MinerU 分支同款验收 — 含整表降级
                            #  stub / 缺失占位标记的"恢复"视为未恢复，继续
                            #  下一轮（Paddle 单轮重跑后仍 stub 则保留空页
                            #  标记走人工复核，不静默放行）。
                            if md and len(md.strip()) > 100 and not _has_missing_markers(md):
                                clean = _sanitize_ocr_text(md.strip())
                                await db.execute(
                                    "UPDATE page_cache SET raw_html = ?, ocr_diagnostics = ?, "
                                    "structured_json = NULL, analyzed_at = NULL "
                                    "WHERE job_id = ? AND page = ?",
                                    (clean, _self_heal_diag(prior_diags.get(pno),
                                     recovered=True, content_len=len(clean),
                                     round_num=1), job_id, pno),
                                )
                                if pno in pages_by_num:
                                    pages_by_num[pno]["markdown"]["text"] = clean
                                recovered.append(pno)
                            else:
                                still_empty.append(pno)
                            await _report_heal_progress(
                                db, job_id,
                                idx, len(retry_targets), still_empty,
                            )
                            await db.commit()
                    finally:
                        src_doc.close()
                if recovered:
                    logger.info(
                        f"[{job_id}] Empty-page retry: recovered "
                        f"{len(recovered)}/{len(retry_targets)} pages: "
                        f"p{recovered}"
                    )
                    await _audit_log(
                        db, job_id, "stage1_empty_recovered",
                        f"recovered_pages={recovered}",
                    )
                if still_empty:
                    logger.warning(
                        f"[{job_id}] Empty-page retry: still empty "
                        f"after re-OCR — p{still_empty} truly "
                        f"unrecognizable by the OCR backend"
                    )
                # 自愈结束：清除 self_heal 子键（total<=0 时 state 层跳过写入）
                await _report_heal_progress(db, job_id, 0, 0, [])
            except Exception as retry_err:
                logger.error(
                    f"[{job_id}] Empty-page retry failed: {retry_err}"
                )
    return recovered
