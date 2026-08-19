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
    db, job_id: str, pdf_path: str, pages: list[dict], backend: str
) -> None:
    """Re-OCR pages whose tag-stripped text is < 100 chars. (refactor)"""
    # Runtime resolution — tests patch core.pipeline._is_cancelled.
    from core.pipeline import _is_cancelled as _run_is_cancelled
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
        for r in await cursor.fetchall():
            html = r["raw_html"] or ""
            if len(html) < 100:
                retry_targets.append(r["page"])
                continue
            stripped = re.sub(r"<[^>]+>", "", html).strip()
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
                        heal_done = heal_total - len(still_empty)
                        for pno, md, discarded in retried:
                            if md and len(md.strip()) > 100:
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
                                    "UPDATE page_cache SET raw_html = ?, "
                                    "structured_json = NULL, analyzed_at = NULL "
                                    "WHERE job_id = ? AND page = ?",
                                    (clean, job_id, pno),
                                )
                                if pno in pages_by_num:
                                    pages_by_num[pno]["markdown"]["text"] = clean
                                recovered.append(pno)
                            else:
                                still_empty.append(pno)
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
                    src_doc = fitz.open(pdf_path)
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
                            out = fitz.open()
                            out.insert_pdf(
                                src_doc, from_page=pno - 1, to_page=pno - 1
                            )
                            out.save(str(slice_path))
                            out.close()
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
                            if md and len(md.strip()) > 100:
                                clean = _sanitize_ocr_text(md.strip())
                                await db.execute(
                                    "UPDATE page_cache SET raw_html = ?, "
                                    "structured_json = NULL, analyzed_at = NULL "
                                    "WHERE job_id = ? AND page = ?",
                                    (clean, job_id, pno),
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
