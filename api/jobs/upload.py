"""Upload endpoint — PDF streaming upload + image->PDF conversion."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
import uuid

import fitz  # PyMuPDF — 图片合成 PDF

from fastapi import APIRouter, UploadFile, File, HTTPException, Request

from config import config
from db.client import get_db
from api.jobs import (
    _CHUNK_SIZE,
    _IMAGE_CONVERT_TIMEOUT_S,
    _IMAGE_EXTENSIONS,
    _IMAGE_MAGIC_PREFIXES,
    _MAX_PDF_BYTES,
    _WEBP_MAGIC,
    router,
)

logger = logging.getLogger(__name__)

@router.post("")
async def create_job(
    file: UploadFile = File(...),
    force: bool = False,
    request: Request = None,
):
    """Upload a PDF and start OCR + analysis pipeline.

    Duplicate detection: the MD5 of the streamed content is stored in
    jobs.md5; re-uploading identical content returns 409 unless force=1
    (query param), which lets users re-analyze the same batch record
    intentionally (e.g. after SOP/rule changes).
    """
    # Runtime resolution — tests monkeypatch api.jobs.{Path, open,
    # launch_pipeline, _MAX_CONCURRENT_JOBS, _ACTIVE_STATUSES,
    # _MAX_IMAGE_PIXELS, db_lock, transition_status}.
    from api.jobs import (
        Path,
        _ACTIVE_STATUSES,
        _MAX_CONCURRENT_JOBS,
        _MAX_IMAGE_PIXELS,
        db_lock,
        launch_pipeline,
        transition_status,
        open as _open,
    )
    # 对抗审查（cr-13）：上传端点是 multipart/form-data（CORS safelisted，
    # 浏览器不触发 preflight），此前无守卫 — 恶意网页可跨站 POST 任意 PDF，
    # 真实启动 pipeline 消耗用户 OCR/LLM 配额。与 /api/settings/* 对齐。
    # request 为 None 时跳过守卫（单元测试直接调用 create_job 的场景）。
    from core.security import is_local_request
    if request is not None and not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    # 扩展名白名单：显式命名时校验（含图片）；空文件名（异常客户端/拖拽）
    # 兜底为 {job_id}.pdf —— 后续 magic bytes 校验（%PDF- 头）仍然拦截伪装内容。
    safe_name0 = Path(file.filename or "").name
    ext = Path(safe_name0 or "fallback.pdf").suffix.lower()
    if ext != ".pdf" and ext not in _IMAGE_EXTENSIONS:
        raise HTTPException(
            400,
            "仅支持 PDF 或图片（jpg/jpeg/png/webp/bmp/tif/tiff）",
        )
    is_image = ext != ".pdf"

    # 友好拦截：未配置 LLM 服务商时拒绝上传。
    # 批记录审查核心价值是 LLM 结构化分析，未配置时上传必然在 analysis
    # 阶段失败，浪费用户上传时间（PDF 可能很大）。拦截比"上传后失败"体验更好。
    # 注意：config 的键是 "providers"（与 config.py:200 / main.py:227 /
    # llm/client.py:30 保持一致），不是 "llm_providers"。早期实现误用
    # "llm_providers" 导致 production 永远拿不到 provider，所有上传被错误拒绝。
    # 检查"是否有非空 api_key"即可（UI 的 _is_real_api_key 严格筛掉 test
    # 占位 key，那是 UI 显示用途；上传守卫只需要"用户配过任意 key"）。
    providers = config.get("providers", {}) or {}
    has_any_key = any(
        bool(p.get("api_key")) if isinstance(p, dict)
        else bool(getattr(p, "api_key", None))
        for p in providers.values()
    )
    if not has_any_key:
        logger.warning("Upload rejected: no LLM provider configured")
        raise HTTPException(
            400,
            "尚未配置 LLM 服务商，无法进行结构化分析。请先前往「设置」完成配置后再上传。",
        )

    # Concurrency guard: count active jobs before accepting new work.
    # 性能优化：COUNT 检查在 db_lock 内（保证读一致性），但 PDF 写盘移到锁外，
    # 避免大文件上传期间阻塞所有 DB 操作（cancel/retry/transition_status）。
    # COUNT 与 INSERT 之间有间隙，但 MAX_CONCURRENT_JOBS 是软限制，
    # 偶尔多一个 job 不会导致系统崩溃（pipeline 内部有 per-job lock 保护）。
    db = await get_db()
    async with db_lock:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({','.join('?' * len(_ACTIVE_STATUSES))})",
            _ACTIVE_STATUSES,
        )
        active_count = (await cursor.fetchone())[0]
        if active_count >= _MAX_CONCURRENT_JOBS:
            logger.warning(
                f"Upload rejected: {active_count} active jobs >= limit {_MAX_CONCURRENT_JOBS}"
            )
            raise HTTPException(
                409,
                f"已有 {active_count} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
            )

    # PDF 写盘 + 校验在 db_lock 外执行，不阻塞其他 DB 操作
    job_id = str(uuid.uuid4())[:12]
    # Phase 5B: use config output_dir (frozen mode → %APPDATA%/PBC/output)
    job_dir = Path(config["app"].output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename — strip path separators from uploaded name to prevent
    # path traversal via crafted Content-Disposition filenames.
    safe_name = Path(file.filename).name
    if not safe_name or safe_name in (".", ".."):
        safe_name = f"{job_id}.pdf"
    logger.info(f"[{job_id}] Upload start: name={safe_name}")
    pdf_path = job_dir / safe_name

    # Stream to disk in chunks; enforce size limit without loading full file
    total_bytes = 0
    file_md5 = hashlib.md5()
    try:
        with _open(pdf_path, "wb") as f:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_PDF_BYTES:
                    f.close()
                    pdf_path.unlink(missing_ok=True)
                    # H5（对抗性审查）：原消息写死 "PDF too large"，图片超限
                    # 时误导用户；统一为"文件"表述
                    raise HTTPException(400, "文件过大（上限 200MB）")
                f.write(chunk)
                file_md5.update(chunk)
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        # Don't leak internal paths/exception details to client
        logger.error(f"Upload write failed: {e}", exc_info=True)
        raise HTTPException(500, "上传失败（磁盘写入错误）")
    content_md5 = file_md5.hexdigest()

    # Magic bytes check: PDF 以 %PDF- 开头；图片按格式白名单匹配。
    # 真实文件头校验独立于扩展名 — 伪装扩展名的图片不得绕过校验。
    if total_bytes < 5:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(400, "文件过小，不是有效文件")
    try:
        with _open(pdf_path, "rb") as f:
            header = f.read(12)
        if is_image:
            ok = False
            if header.startswith(_WEBP_MAGIC[0]) and len(header) >= 12:
                ok = header[8:12] == _WEBP_MAGIC[1]
            if not ok:
                ok = any(header.startswith(m) for m, _ in _IMAGE_MAGIC_PREFIXES)
            if not ok:
                pdf_path.unlink(missing_ok=True)
                logger.warning(f"[{job_id}] Upload rejected: bad image magic bytes {header[:8]!r}")
                raise HTTPException(400, "文件不是有效的图片（文件头不匹配）")
        elif not header.startswith(b"%PDF-"):
            pdf_path.unlink(missing_ok=True)
            logger.warning(f"[{job_id}] Upload rejected: bad magic bytes {header[:8]!r}")
            raise HTTPException(400, "文件不是有效的 PDF（缺少 %PDF- 文件头）")
    except HTTPException:
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        logger.error(f"Magic bytes check failed: {e}", exc_info=True)
        raise HTTPException(500, "上传校验失败")

    if total_bytes == 0:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(400, "文件为空")

    # 上传后立即读取 PDF 总页数 — OCR/分析期间 review 页面就能显示正确的
    # "X / Y" 页码（之前 total_pages 在 OCR 完成后才写入，导致显示 "1 / 0"）。
    # 用 PyMuPDF (fitz) 读取，开销 < 100ms 即使 200MB PDF。
    # 线程池执行避免阻塞事件循环（损坏/超大 PDF 的 xref 修复可能秒级）。
    class _PdfStructuralError(Exception):
        """PDF 结构性不可用（加密/0 页）— 上传时立即拒绝而非等 pipeline 失败。"""

    def _count_pdf_pages_sync(path: str) -> int:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            # 加密 PDF：fitz 可打开但 needs_pass=True（内容需密码解密）。
            # 云端 OCR 后端必然失败（请求会带不出解密内容），上传时拒绝
            # 比用户等 pipeline 跑 10+ 分钟才 error 体验好得多。
            if doc.needs_pass:
                raise _PdfStructuralError("encrypted")
            if doc.page_count == 0:
                raise _PdfStructuralError("empty")
            return doc.page_count

    # 图片 → 转换 PDF：Pillow 解码（EXIF 方向修正）+ PyMuPDF 合成单页。
    # 线程池执行 — 超大图解码可能秒级，不阻塞事件循环。
    # 注意：Image.open 惰性解码，exif_transpose 可能返回原对象 — 全部处理
    # 必须在 with 块内完成（块退出即 close），块外只保留纯值。
    def _image_to_pdf_sync(src: str, dst: str) -> int:
        from PIL import Image, ImageOps
        img_w = img_h = 0
        png_bytes = b""
        try:
            with Image.open(src) as img:
                # H4（对抗性审查）：像素上限在解码前用头部尺寸检查 — exif_transpose
                # 会触发全量解码（1 亿像素图峰值 ~300MB），先检查可拒绝超大图，
                # 避免 DoS 面；转置只交换宽高，像素总数不变，头部检查等价。
                img_w, img_h = img.size
                if img_w * img_h > _MAX_IMAGE_PIXELS:
                    raise ValueError(
                        f"图片过大（{img_w}x{img_h} 像素，上限 1 亿像素）"
                    )
                # H1/H3（对抗性审查）：多页 TIFF / 动画 WEBP 之前静默只保留首帧
                # （Pillow 默认仅解码第一帧）— 扫描件多页被无声砍掉，OCR/LLM/
                # 复核/报告全部残缺且无警告。GMP 场景宁缺勿滥：明确拒绝并提示。
                if getattr(img, "n_frames", 1) > 1:
                    raise ValueError(
                        "暂不支持多页 TIFF / 动画 WEBP，请拆分为单页文件"
                        "或转换为 PDF 后上传"
                    )
                img = ImageOps.exif_transpose(img)
                img_w, img_h = img.size
                # H2（对抗性审查）：透明 PNG 直接 convert("RGB") 会保留原始
                # RGB 值（全透明像素 (0,0,0) → 黑块），OCR 视作内容。先合成
                # 白底 — 扫描文件语义=白纸黑字。
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    rgba = img.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.split()[-1])
                    img = background
                elif img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                import io as _io
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                png_bytes = buf.getvalue()
            if not png_bytes:
                raise ValueError("图片解码为空")
            # 300 DPI 映射：像素 → PDF 点（pt = px * 72/300），扫描件
            # 常见分辨率，页面物理尺寸与纸面一致
            width_pt = img_w * 72 / 300
            height_pt = img_h * 72 / 300
            with fitz.open() as doc:
                page = doc.new_page(width=width_pt, height=height_pt)
                page.insert_image(
                    page.rect,
                    stream=png_bytes,
                    width=img_w,
                    height=img_h,
                )
                doc.save(dst, garbage=4, deflate=True)
            return 1
        except (ValueError, OSError, TypeError) as e:
            raise _PdfStructuralError(str(e)) from e

    if is_image:
        try:
            converted_pdf = job_dir / f"{job_id}.pdf"
            # P2-2: 图片转换无超时 — Pillow 解码 + PNG 编码 + PDF 压缩对
            # 接近上限（1 亿像素）的合法大图可能耗时数分钟，请求挂起、线程池
            # 被长期占用。wait_for 保证调用方到时返回明确错误（to_thread 取消
            # 后后台线程仍会跑完，但不阻塞事件循环与用户感知）。
            pdf_page_count = await asyncio.wait_for(
                asyncio.to_thread(
                    _image_to_pdf_sync, str(pdf_path), str(converted_pdf)
                ),
                timeout=_IMAGE_CONVERT_TIMEOUT_S,
            )
            # 转换成功后 jobs.pdf_path 指向转换 PDF；原图保留在 job 目录留档
            pdf_path = converted_pdf
            logger.info(f"[{job_id}] Image converted to PDF: {safe_name} -> {converted_pdf.name}")
        except asyncio.TimeoutError:
            pdf_path.unlink(missing_ok=True)
            shutil.rmtree(job_dir, ignore_errors=True)
            detail = f"图片转换超时（超过 {_IMAGE_CONVERT_TIMEOUT_S} 秒），请降低分辨率后重试。"
            logger.warning(f"[{job_id}] Upload rejected: {detail}")
            raise HTTPException(408, detail)
        except _PdfStructuralError as e:
            pdf_path.unlink(missing_ok=True)
            shutil.rmtree(job_dir, ignore_errors=True)
            detail = f"图片无法解析：{e}"
            logger.warning(f"[{job_id}] Upload rejected: {detail}")
            raise HTTPException(400, detail)
        except Exception as e:
            pdf_path.unlink(missing_ok=True)
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.error(f"[{job_id}] Image conversion failed: {e}", exc_info=True)
            raise HTTPException(500, "图片转换失败（服务器内部错误）")
    else:
        pdf_page_count = 0
        try:
            pdf_page_count = await asyncio.to_thread(_count_pdf_pages_sync, str(pdf_path))
            logger.info(f"[{job_id}] PDF page count: {pdf_page_count}")
        except _PdfStructuralError as e:
            pdf_path.unlink(missing_ok=True)
            shutil.rmtree(job_dir, ignore_errors=True)
            detail = "PDF 已加密，无法进行 OCR 分析，请先解密后上传。" if "encrypted" in str(e) else "PDF 不包含任何页面。"
            logger.warning(f"[{job_id}] Upload rejected: {detail}")
            raise HTTPException(400, detail)
        except Exception as e:
            # 其他读取失败不阻断上传 — 部分损坏 PDF 云端 OCR 后端可能仍能处理
            # （Paddle/MinerU 各有容错），pipeline 仍会在 OCR 完成后设置 total_pages
            logger.warning(f"[{job_id}] Failed to read PDF page count: {e}")
            pdf_page_count = 0

    # INSERT 在 db_lock 内（与去重检查 + 其他 DB 写入序列化，避免两个相同
    # 上传并发都通过检查）。去重：内容 md5 相同 → 409 提示已有任务，不创建
    # 重复 job（重复全流程 OCR/LLM 是纯浪费）。force=1 绕过（同一批记录在
    # 规则/SOP 变更后重新分析的合法场景）。
    async with db_lock:
        try:
            cursor = await db.execute(
                "SELECT id, filename, status, created_at FROM jobs "
                "WHERE md5 = ? AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (content_md5,),
            )
            dup = await cursor.fetchone()
            if dup and not force:
                logger.info(
                    f"[{job_id}] Upload rejected: duplicate content of job {dup['id']}"
                )
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(
                    409,
                    f"该文件已上传过（任务 {dup['id']}「{dup['filename']}」，"
                    f"状态 {dup['status']}）。点击历史记录即可查看；"
                    f"确需重新分析请先删除旧任务或重新上传（将创建新任务）。",
                )
            await db.execute(
                "INSERT INTO jobs (id, filename, status, pdf_path, total_pages, md5, created_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, datetime('now','localtime'))",
                (job_id, safe_name, str(pdf_path), pdf_page_count or None, content_md5),
            )
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, 'pipeline_start', ?, datetime(\'now\',\'localtime\'))",
                (job_id, f"Uploaded {safe_name} ({total_bytes} bytes, {pdf_page_count} pages, source={ 'image' if is_image else 'pdf' })"),
            )
            await db.commit()
        except HTTPException:
            # 去重 409 等已由业务分支 raise 的异常直接透传，不落入通用兜底
            raise
        except Exception as e:
            # INSERT 失败时清理孤儿 PDF 文件 + job_dir，避免磁盘累积
            logger.error(f"[{job_id}] DB INSERT failed, cleaning up job_dir: {e}", exc_info=True)
            try:
                await db.rollback()
            except Exception:
                pass
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(500, "数据库写入失败，请重试")

    # Launch async pipeline（注册到 _pipeline_tasks 以便优雅关闭）
    launch_pipeline(job_id, str(pdf_path))
    logger.info(f"[{job_id}] Upload complete: {total_bytes} bytes, pipeline launched")

    return {"job_id": job_id, "filename": safe_name, "status": "pending"}
