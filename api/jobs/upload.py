"""Upload endpoint — PDF streaming upload + image->PDF conversion."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid

import fitz  # PyMuPDF — 图片合成 PDF

from fastapi import UploadFile, File, HTTPException, Request

from config import config, check_upload_page_limits, UPLOAD_LIMITS
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
    # _MAX_IMAGE_PIXELS, db_lock}.
    from api.jobs import (
        Path,
        _ACTIVE_STATUSES,
        _MAX_CONCURRENT_JOBS,
        _MAX_IMAGE_PIXELS,
        _count_active_jobs,
        db_lock,
        launch_pipeline,
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

    # ── 并发额度：**两道检查**（B2-7 修 TOCTOU）──────────────────────────
    # ① 这里是**尽力而为的前置快检**：目的只是别让用户白传一个 200MB 的文件，
    #    **不是**授权依据 —— COUNT 与最终 INSERT 之间隔着写盘 + 解析（大文件
    #    可达分钟级），期间其他上传会陆续通过这里。
    # ② 真正的授权在下方 **INSERT 的同一把 db_lock 内**（检查与写入之间不释放
    #    锁、无 await 让出点，对其他上传而言是原子的）。那才是"绝不超额"的保证。
    # 性能：锁内只有一次 COUNT，PDF 写盘仍在锁外，不阻塞其他 DB 操作。
    db = await get_db()
    async with db_lock:
        early_active = await _count_active_jobs(db, _ACTIVE_STATUSES)
    if early_active >= _MAX_CONCURRENT_JOBS:
        logger.warning(
            f"Upload rejected (early check): {early_active} active >= limit {_MAX_CONCURRENT_JOBS}"
        )
        raise HTTPException(
            409,
            f"已有 {early_active} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
        )

    # PDF 写盘 + 校验在 db_lock 外执行，不阻塞其他 DB 操作
    job_id = str(uuid.uuid4())[:12]
    # Phase 5B: use config output_dir (frozen mode → %APPDATA%/PBC/output)
    job_dir = Path(config["app"].output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename — strip path separators from uploaded name to prevent
    # path traversal via crafted Content-Disposition filenames.
    # filename=None 时 Path(None) 抛 TypeError → 500（与 :63 的 or "" 兜底对齐）
    safe_name = Path(file.filename or "").name
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
                    # 时误导用户；统一为"文件"表述。
                    # 数值由 _MAX_PDF_BYTES 派生 —— 写死字面量会与常量漂移
                    # （前端预检/页面文案同源，见 config.UPLOAD_LIMITS）。
                    raise HTTPException(
                        400,
                        f"文件过大（上限 {_MAX_PDF_BYTES // 1024 // 1024}MB）",
                    )
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
        except Image.DecompressionBombError as e:
            # ⚠️ B11-6 定位到的缺陷：Pillow 自己的 bomb 阈值（默认 2×MAX_IMAGE_PIXELS
            # ≈ 1.79e8 像素）比产品上限（_MAX_IMAGE_PIXELS = 1e8）**更宽**，所以在
            # (1e8, 1.79e8] 区间由**产品的头部检查**拦下，而超过 1.79e8 时
            # **Pillow 在 Image.open 阶段就先抛** DecompressionBombError。
            # 它是 `Exception` 的直接子类 —— 既不是 ValueError 也不是 OSError
            # ⇒ 早先的 `except (ValueError, OSError, TypeError)` **接不住**，
            # 一路落到通用 `except Exception` ⇒ 客户端输入问题被报成 **500**
            # 并记 `logger.error(exc_info=True)` 全栈（实测 78 字节即可触发）。
            # 显式接住 ⇒ 归为 400 友好拒绝。
            raise _PdfStructuralError(f"图片过大（{e}）") from e
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

    # ── 页数限额（单一真值/策略见 config.check_upload_page_limits）──────
    # 与体积上限**正交**：体积检查挡不住"低密度但极长"的 PDF —— 数字排版件
    # 可能 50KB/页，200MB 能装下数千页，而体积检查毫无察觉。而数千页按实测
    # 9.0 s/页 OCR 会先撞上 core/ocr_client.POLL_TIMEOUT_MAX=3600s（100 页即
    # 封顶）→ 用户等满 1 小时后收到"轮询超时"。上传时以明确提示拒绝，远好于
    # 让用户在服务端超时后才得知。
    #
    # 判定为纯函数：上限的数值、文案与边界语义同源，可脱离 HTTP 直接单测。
    # ⚠️ 页数读取失败（0）时该函数一律放行。
    reject_detail, page_warning = check_upload_page_limits(
        pdf_page_count, UPLOAD_LIMITS
    )
    if reject_detail:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.warning(f"[{job_id}] Upload rejected: {reject_detail}")
        raise HTTPException(400, reject_detail)
    if page_warning:
        logger.warning(f"[{job_id}] Large document: {page_warning}")

    # INSERT 在 db_lock 内（与去重检查 + 其他 DB 写入序列化，避免两个相同
    # 上传并发都通过检查）。去重：内容 md5 相同 → 409 提示已有任务，不创建
    # 重复 job（重复全流程 OCR/LLM 是纯浪费）。force=1 绕过（同一批记录在
    # 规则/SOP 变更后重新分析的合法场景）。
    async with db_lock:
        # B2-7：**授权式配额检查** —— 与下面的 INSERT 在同一把 db_lock 内，
        # 中间既不释放锁、也无 await 让出点 ⇒ "查过没过"与"写下这一行"对其他
        # 上传而言是原子的。此前只在函数开头查一次，则并发上传会**全部**通过
        # 那道检查再各自 INSERT ⇒ 活跃数可远超 MAX_CONCURRENT_JOBS（上限形同
        # 虚设；pipeline 的 per-job lock 并不限制**这个**上限）。
        # 注：db_lock 是**进程内**的 asyncio.Lock（单进程 uvicorn + aiosqlite
        # 单连接），原子性以"单进程"为前提 —— 与本模块其余配额语义一致。
        late_active = await _count_active_jobs(db, _ACTIVE_STATUSES)
        if late_active >= _MAX_CONCURRENT_JOBS:
            logger.warning(
                f"[{job_id}] Upload rejected (atomic quota check): "
                f"{late_active} active >= limit {_MAX_CONCURRENT_JOBS}"
            )
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                409,
                f"已有 {late_active} 个任务在处理中，上限为 {_MAX_CONCURRENT_JOBS}。请等待完成或取消后再试。",
            )
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
                "INSERT INTO jobs (id, filename, status, pdf_path, total_pages, md5, "
                "created_at, last_activity_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, datetime('now','localtime'), "
                "datetime('now','localtime'))",
                (job_id, safe_name, str(pdf_path), pdf_page_count or None, content_md5),
            )
            audit_detail = (
                f"Uploaded {safe_name} ({total_bytes} bytes, {pdf_page_count} pages, "
                f"source={ 'image' if is_image else 'pdf' })"
            )
            # 大文件软告警留痕（GMP 追溯：影响复核员对耗时的预期，须可回溯）
            if page_warning:
                audit_detail += f" [告警] {page_warning}"
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, 'pipeline_start', ?, datetime(\'now\',\'localtime\'))",
                (job_id, audit_detail),
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
    #
    # #142：launch 必须包在 try 里。`asyncio.create_task` 在"无运行中事件循环"
    # 时会抛 RuntimeError；此前该异常会从端点逃逸，而 DB 里的 job 已经写成
    # `pending` —— 于是留下一个**无终态黑洞**：`pending` 且注册表里没有 task，
    # 看门狗不监视它（`is_watched` 只认 pening+有活 task），启动恢复又要求
    # `created_at < process_started_at`（同进程内的孤儿要等下次重启），
    # 期间 SSE 的 `while True` 只认终态 ⇒ 用户界面无限转圈、无任何提示。
    # 失败即刻转 error（带原因 + 审计），把黑洞变成可重试的明确失败。
    try:
        launch_pipeline(job_id, str(pdf_path))
    except Exception as e:
        logger.error(f"[{job_id}] launch_pipeline failed: {e!r}", exc_info=True)
        from api.jobs import mark_launch_failed
        await mark_launch_failed(job_id, e)
        raise HTTPException(500, "任务已创建但流水线启动失败，请重试")
    logger.info(f"[{job_id}] Upload complete: {total_bytes} bytes, pipeline launched")

    # page_warning：仅超软阈值时非 None —— 前端据此提示"较大文件，预估耗时"
    return {
        "job_id": job_id,
        "filename": safe_name,
        "status": "pending",
        "page_warning": page_warning,
    }
