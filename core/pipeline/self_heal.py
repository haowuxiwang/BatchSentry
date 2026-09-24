"""Empty-page self-heal: re-OCR truncated pages as small slices (module refactor)"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from config import config
from core.ocr_client import (
    OCRCancelled,
    is_congestion_error,
    poll_timeout_for_pages,
)
from core.pipeline.ocr_support import _sanitize_ocr_text
from core.pipeline.state import _audit_log, touch_activity
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


# 恢复验收（自愈两分支 + 旋转恢复共用）：非空、>100 字符、无缺失标记。
# 抽取为函数消重 — 三处手写同一条件曾在对抗审查中被各自漂移。
def _accept_heal_text(md: str | None) -> bool:
    return bool(md) and len(md.strip()) > 100 and not _has_missing_markers(md)


# 旋转恢复候选角度（round-23 A）：页面内容横置（扫描时纸横放，非
# /Rotate 元数据）时 OCR 返回稀疏/空文本，切片重试无法恢复 — 逐角度
# 重渲染探测。90/270 在前（横放扫描最常见），180 最后。
_ROTATION_CANDIDATES = (90, 270, 180)

# ─── 旋转补救的时长预算（2026-09-16，缺陷 #120）─────────────────────────
# 背景：上游拥塞窗口是**分钟级**（同日实测 ≥18 分钟），而旧实现对上游错误
# **不做分类** —— 每个角度失败即换下一个，全部角度在 ~61s 内耗尽。拥塞期间
# 旋转通道因此**必然**空手而归，还把基础设施状况写成「已探测全部角度未果」
# 的内容结论（GMP 复核里这两件事不是一回事）。
# 修法：拥塞类错误**退避重试同一角度**（不消耗有限的角度预算），等待总量由
# 下面的页面/文档预算封顶。
#
# ⚠️ 这些常量是**看门狗阈值不变式的联动项**（docs/RUNTIME_WATCHDOG.md §5）：
# 等待期间必须持续写心跳（`state.touch_activity`），否则看门狗会把「正在按
# 预算等待」判成停滞。心跳写好之后，OCR 期的**静默上界**不再是「整页补救
# 总时长」，而是「单次探测尝试」与「单次退避」中的较大者 —— 见
# `rotation_silence_bound_s()`（被 test_watchdog.py 机检锁定）。
# 这也是不把「轮询超时」当拥塞的原因：超时重试会把单次尝试从 630s 拉到
# 1260s，直接抬高静默上界。
_ROTATION_PROBE_ATTEMPTS = 2
# 拥塞退避阶梯（秒）：递增到末项后保持；消费受页面/文档截止点约束。
# - 30s 起：下游抖动一两分钟即恢复的常见情形，一次就够。
# - 封顶 300s：**单次退避不得超过看门狗允许的静默**（见 rotation_silence_bound_s）。
_ROTATION_CONGESTION_BACKOFF_S: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0, 300.0)
# 一次**完整探测尝试**的最坏耗时（秒）：每角度 `attempts` 次，每次受轮询封顶
# 约束。这是"相邻两次心跳之间能有多久"的直接来源 —— 见
# `rotation_silence_bound_s()`。**不得手抄**，否则轮询封顶一改就悄悄失配。
_ROTATION_PROBE_COST_S = float(
    _ROTATION_PROBE_ATTEMPTS * poll_timeout_for_pages(1)
)
# 实测拥塞窗口的**下界**（秒）：同日 Paddle `code:10010` 连续拒绝持续 ≥18 分钟。
_ROTATION_CONGESTION_WINDOW_S = float(18 * 60)
# 单页旋转补救预算 = 上面两者取大：
# ① **至少容得下一次完整尝试** —— 否则预算在第一次尝试就必然超支，退避
#    阶梯永远拿不到执行机会（末项退避沦为死常量）。这一条是 2026-09-16
#    机检抓出来的：手写的 1200s < 一次尝试的 1260s，属自相矛盾的配置。
# ② **不小于实测拥塞窗口** —— 一个拥塞窗口之内，被阻塞的那一页仍有真实
#    机会等到上游恢复（旧实现在 61s 内即放弃）。
_ROTATION_PAGE_BUDGET_S = max(
    _ROTATION_PROBE_COST_S, _ROTATION_CONGESTION_WINDOW_S
)
# 文档级旋转补救总预算：30 分钟 —— 多页先后被拥塞阻塞时封顶整轮，
# 防止「单页预算 × 页数」把任务拖成小时级。超出后剩余目标页**如实**标记为
# 「因上游容量受限未完成」，不再尝试，也不再假装探测过。
# 必须 ≥ `_ROTATION_PAGE_BUDGET_S`（否则单页预算被 min() 架空，机检锁定）。
_ROTATION_DOC_BUDGET_S = 1800.0
# 退避期间取消响应的检查粒度（秒）：不能在 300s 的 sleep 里对取消失聪
# （本项目对取消响应性有明确要求，见 OCRCancelled 的设计说明）。
_ROTATION_COOLDOWN_CANCEL_POLL_S = 15.0

# rotation_blocked 的取值（诊断/审计共用同一字面量，避免两处漂移）。
ROTATION_BLOCKED_CONGESTION = "upstream_congestion"


class _HealCancelled(Exception):
    """自愈期间检测到用户取消（**不是**失败）。

    与 `core.ocr_client.OCRCancelled` 同款语义：取消不能被当成故障处理
    （否则会把「用户点了取消」写成探测失败/内容不可识别）。仅在本模块内部
    流转，由 `_rotation_heal` 捕获后保留已恢复页退出。
    """


def rotation_silence_bound_s() -> float:
    """旋转补救期间**相邻两次心跳之间的最大静默**（秒）。

    看门狗对 `ocr_running` 的停滞阈值必须 ≥ 本值，否则会把「正在按预算
    重试/等待」的正常 job 误判为停滞（阈值不变式）。
    机检：tests/unit/test_watchdog.py::TestThresholdsAboveUpstreamCaps。

    组成 = max(单次探测尝试封顶, 单次退避封顶)：
    - 心跳在每个角度尝试结束、以及每次退避**开始前**写入；
    - 故静默不跨尝试累加 —— 这是本设计相对「把整页预算当阈值」的关键改进
      （整页最坏 = 3 角度 × 2 次尝试 × 630s ≈ 3780s，那会把看门狗逼到
      不得不放宽到 2 小时级）。
    """
    return float(max(_ROTATION_PROBE_COST_S,
                     _ROTATION_CONGESTION_BACKOFF_S[-1]))

# 嫌疑横置页升级裕度（round-23 A3，e2e 实证）：VL 对横排文本有旋转
# 容忍度 —— 切片重跑能读出横置页的大部分表格但标题/细字乱码（e2e p3:
# 「横置二百七十度参数表」→「横直一口」），>100 字通过验收后旋转探测
# 从未运行。横向几何（aspect_ratio>1）+ 初判稀疏的切片恢复页列为嫌疑
# 横置，补跑旋转探测；旋转读取须内容量明显更优（>1.1×）才替换 —
# 正常横版宽表页（横向是合法排版）旋转后读取必然更差，不会误替换。
_ROTATION_UPGRADE_FACTOR = 1.1

# 旋转几何预筛阈值（round-23 A4 前置，e2e 实证）：原生页行/列强度投影
# 方差比 ≥ 此值判横向文本。e2e_rot.pdf 实测横向 ratio 6.1-13.1、纵向
# 0.08-0.16 —— 双峰间隔大，1.15 取中间偏保守值；两轴皆弱（空白页）或
# 比值落入灰区时返回 None（回退探测全部角度，预筛永不阻塞恢复）。
_PRESCREEN_RATIO = 1.15
# 预筛渲染 DPI：只需行/列明暗统计，48dpi 灰度足够分辨文本朝向（原型
# devlogs/_proto_prescreen.py 实证），单页渲染 <50ms。
_PRESCREEN_DPI = 48


def _variance(xs: list[float]) -> float:
    """总体方差（与原型 devlogs/_proto_prescreen.py 一致的语义）。"""
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return sum((v - m) ** 2 for v in xs) / n


def _projection_variances(pdf_path: str, page_no: int) -> tuple[float, float]:
    """原生页行/列强度投影方差（同步低 DPI 渲染，供 to_thread 调用）。

    横向文本 → 行间明暗交替 → 行方差大、列方差小；纵向文本同理反转。
    90°/270° 旋转交换两轴、180° 保持 —— 原生页单次渲染即可推出全部候选
    角度的朝向，无需逐角度渲染（预筛成本 = 每目标页一次 <50ms 渲染）。
    """
    import fitz
    from PIL import Image

    with fitz.open(pdf_path) as doc:
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=_PRESCREEN_DPI, colorspace=fitz.csGRAY)
        w, h = pix.width, pix.height
        img = Image.frombytes("L", (w, h), pix.samples[: w * h])
    # ⚠️ 用 `get_flattened_data()` 而非 `getdata()`：后者自 Pillow 12 起弃用，
    # Pillow 14（2027-10-15）移除。**必须与 `requirements.txt` 的 Pillow 升版同一提交**
    # —— Pillow 10 没有 `get_flattened_data`，提前改会让旧环境直接 AttributeError。
    # 等价性为**实测**结论（非按 API 名推测）：mode "L" + `resize(BOX)` 下两者
    # 逐元素完全一致、`get_flattened_data` 零警告、投影方差完全相同。
    # 判据与登记表在 `tests/unit/test_pillow_api_compat.py`（变异 7/7 CAUGHT）。
    row_means = [float(v) for v in
                 img.resize((1, h), Image.Resampling.BOX).get_flattened_data()]
    col_means = [float(v) for v in
                 img.resize((w, 1), Image.Resampling.BOX).get_flattened_data()]
    return _variance(row_means), _variance(col_means)


async def _prescreen_rotation_angles(
    pdf_path: str, page_no: int,
) -> tuple[int, ...] | None:
    """几何预筛：判定哪些候选角度值得 OCR 探测（round-23 A4）。

    根因（e2e 实证）：上游「系统错误-拆页」随机杀死 90°/270° 探测时，
    180° 乱序读取因长度门槛被误采纳。预筛从几何上排除不可能正确的角度：
    - 原生纵向文本（横置内容）→ 只探测 90/270（180° 仍纵向，永远跳过）
    - 原生横向文本（正常页/180° 倒置页）→ 只探测 180（90/270 变纵向）
    返回 None 表示预筛不定（灰区/空白/渲染失败）→ 探测全部候选角度。
    """
    try:
        rv, cv = await asyncio.to_thread(
            _projection_variances, pdf_path, page_no
        )
    except Exception as e:
        logger.warning(
            f"prescreen render failed p{page_no}: {redact_urls(str(e))[:120]}"
        )
        return None
    if rv < 1e-6 and cv < 1e-6:
        return None  # 空白页：无朝向信息
    horizontal = rv >= _PRESCREEN_RATIO * cv
    vertical = cv >= _PRESCREEN_RATIO * rv
    if horizontal:
        return (180,)
    if vertical:
        return (90, 270)
    return None  # 灰区（混合朝向/图像主导）：全角度探测


async def _probe_slice_text(
    slice_path: str, backend: str, job_id: str,
    attempts: int = _ROTATION_PROBE_ATTEMPTS,
) -> str:
    """单角度旋转切片 OCR（含**瞬态**上游错误重试，round-23 A4）。

    e2e 实证上游「系统错误-拆页」为随机瞬态失败：首试异常时退避 2s 重试
    一次，显著提高正确角度存活率（90°/270° 探测不再被单次瞬态错误杀死
    而让位给乱序角度）；重试耗尽后抛末次异常由调用方记录并继续下一角度。

    **拥塞类错误不在此处重试**（2026-09-16 修 #120）：直接上抛给
    `_probe_slice_angle` 按预算做分钟级退避。理由有二 ——
    ① 2s 的短退避对分钟级的拥塞窗口毫无用处，纯属白烧一次上游调用；
    ② 两层退避会**相乘**，正是实测「~15s/角度、61s 耗尽全部角度」的成因。

    **用户取消同样不重试**：`OCRCancelled` 原样上抛，由 `_rotation_heal`
    按取消语义收尾（保留已恢复页、停止后续探测）。
    """
    import core.ocr_client as ocr_client  # runtime-visible for PyInstaller

    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if backend == "mineru":
                from core.mineru_client import run_ocr_pages

                retried = await asyncio.to_thread(
                    run_ocr_pages, slice_path, [1],
                    job_id=job_id, batch_size=1,
                )
                return retried[0][1] if retried else ""
            slice_pages = await asyncio.to_thread(
                ocr_client.run_ocr, slice_path
            )
            return (
                slice_pages[0]["markdown"]["text"] if slice_pages else ""
            )
        except OCRCancelled:
            # 用户取消**不是**瞬态故障：不得退避重试（那会在用户已取消后
            # 再打一次上游，并掩盖取消语义）。OCRCancelled 是 RuntimeError
            # 子类，必须排在通用分支之前 —— 本项目在 mineru_client /
            # ocr_support 都踩过这个顺序坑。
            raise
        except Exception as e:
            if is_congestion_error(e):
                raise  # 拥塞：交外层按预算退避（不消耗内层尝试次数）
            last_err = e
            if attempt < attempts:
                logger.info(
                    f"[{job_id}] Rotation probe transient error "
                    f"({redact_urls(str(e))[:120]}) — retrying once"
                )
                await asyncio.sleep(2)
    assert last_err is not None
    raise last_err


async def _probe_slice_angle(
    slice_path: str, backend: str, job_id: str, *,
    page_deadline: float, db, is_cancelled,
) -> tuple[str, bool]:
    """探测单个角度；**拥塞类错误退避重试同一角度**（缺陷 #120 的正面修法）。

    返回 `(md, congestion_blocked)`：

    - `md`：该角度的 OCR 文本（未过 `_accept_heal_text` 验收时调用方照旧
      换下一个角度 —— 那是"内容不对"，不是"读不到"）。
    - `congestion_blocked=True`：**预算耗尽时最后一次失败仍是拥塞类**，
      即「根本没读到」而非「读到了但内容不达标」。调用方必须据此落
      **区分性**诊断（`rotation_blocked=upstream_congestion`）—— 旧实现
      把它写成 `rotation_probed=True`（"已探测全部角度未果"），等于把
      上游容量状况说成内容结论，复核者会据此认为"此页真没内容可读"。

    非拥塞异常原样上抛（由调用方记 warning 后换角度）；用户取消抛
    `_HealCancelled`。

    `page_deadline`：本页补救的单调时钟截止点（页面预算与文档预算取小），
    保证退避不会越界侵占后续页面的预算。
    """
    waited = 0.0
    backoff_idx = 0
    while True:
        try:
            return await _probe_slice_text(slice_path, backend, job_id), False
        except Exception as e:
            if not is_congestion_error(e):
                raise
            remaining = page_deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    f"[{job_id}] Rotation probe blocked by upstream "
                    f"congestion (waited {waited:.0f}s, page budget "
                    f"exhausted; {redact_urls(str(e))[:120]}) — "
                    f"marking page as not-probed"
                )
                return "", True
            nap = min(
                _ROTATION_CONGESTION_BACKOFF_S[
                    min(backoff_idx, len(_ROTATION_CONGESTION_BACKOFF_S) - 1)
                ],
                remaining,
            )
            logger.warning(
                f"[{job_id}] Rotation probe upstream congestion "
                f"({redact_urls(str(e))[:120]}) — backing off {nap:.0f}s "
                f"then retrying the same angle (waited {waited:.0f}s, "
                f"{remaining:.0f}s left on this page)"
            )
            # 心跳：进入一次**有界**的刻意等待。循环由预算封顶，所以这不是
            # 「定时器式假心跳」—— 它无法掩盖永久停滞（看门狗仍会在预算
            # 之外的真实停滞上触发）。
            await touch_activity(db, job_id)
            await _interruptible_sleep(nap, is_cancelled)
            waited += nap
            backoff_idx += 1


async def _interruptible_sleep(seconds: float, is_cancelled) -> None:
    """按 `_ROTATION_COOLDOWN_CANCEL_POLL_S` 粒度分片睡眠，保持取消响应性。

    直接 `asyncio.sleep(300)` 会让用户点取消后最多 300s 才生效 —— 本项目
    对取消响应性有明确要求（见 `core.ocr_client.OCRCancelled`）。
    """
    slept = 0.0
    while slept < seconds:
        step = min(_ROTATION_COOLDOWN_CANCEL_POLL_S, seconds - slept)
        await asyncio.sleep(step)
        slept += step
        if is_cancelled is not None and await is_cancelled():
            raise _HealCancelled("cancelled during rotation backoff")


def _write_rotated_slice(src_path: str, page_no: int, angle: int,
                          dst_path: str) -> None:
    """把源 PDF 第 page_no 页旋转 angle 度后写成单页 PDF（同步磁盘 IO）。

    目标页尺寸按角度交换宽高；show_pdf_page(rotate=) 在目标矩形内旋转
    缩放源页。元数据 /Rotate 已被 page.rect 消化（源页按显示方向取尺寸），
    本函数只处理"内容相对显示方向再横置"的场景。
    """
    import fitz
    src = fitz.open(src_path)
    try:
        page = src[page_no - 1]
        rect = page.rect
        w, h = (rect.height, rect.width) if angle in (90, 270)             else (rect.width, rect.height)
        out = fitz.open()
        try:
            np = out.new_page(width=w, height=h)
            np.show_pdf_page(np.rect, src, page_no - 1, rotate=angle)
            out.save(dst_path)
        finally:
            out.close()
    finally:
        src.close()


async def _rotation_heal(
    db, job_id: str, pdf_path: str, targets: list[int], backend: str,
    pages_by_num: dict[int, dict], prior_diags: dict[int, dict],
    upgrade_pages: dict[int, int] | None = None,
) -> dict[int, int]:
    """旋转恢复通道：切片重试仍空的页，逐角度重渲染后重 OCR（round-23 A）。

    返回 {page: 采纳角度}。每页最多 3 次单页 OCR 调用（仅 still-empty 页，
    通常 0-6 页/文档）；任一角度通过恢复验收即采纳并停止后续角度。
    采纳结果与切片自愈同款落库（清 structured_json 触发补跑分析、
    prior_diagnostics 保留、内存回写）；审计 stage1_rotation_recovered
    由调用方合并写（GMP 可追溯"此页为何多了旋转字段"）。

    upgrade_pages（round-23 A3，e2e 实证）：{page: 切片恢复内容长度} ——
    VL 对横排文本有旋转容忍度，横置页可能被切片重跑"部分恢复"（表格可
    读、标题乱码）；此类嫌疑页传入后旋转读取须内容量明显更优
    （> _ROTATION_UPGRADE_FACTOR ×现有长度）才替换，未升级成功则保留
    切片结果（诊断不动 — 页面已有内容，rotation_probed 空页语义不适用）。

    几何预筛（round-23 A4，e2e 根因）：上游「系统错误-拆页」随机杀死
    90°/270° 探测时，180° 乱序读取曾因长度门槛被误采纳。预筛按投影
    方差只探测几何上可能正确的角度（横置页跳过 180°），配合瞬态错误
    单次重试（_probe_slice_text）双保险：前者杜绝错误采纳，后者提高
    正确角度存活率。

    上游拥塞（2026-09-16，缺陷 #120）：返回 `(recovered, blocked)` ——
    `blocked` 是 `{page: 原因}`。当某个角度因**上游容量**失败时，不再换
    下一个角度（其余角度同样会被拒，只是白烧预算），而是按 `_ROTATION_
    CONGESTION_BACKOFF_S` 退避后**重试同一角度**，直到该页预算用尽才记为
    `ROTATION_BLOCKED_CONGESTION`。这样「等上游恢复」与「换个角度读」两件
    事互不挤占：角度预算是有限的（3 个），等待预算是时间。
    """
    from core.pipeline import _is_cancelled as _run_is_cancelled

    def _is_cancelled():
        return _run_is_cancelled(job_id)

    job_dir_p = Path(config["app"].output_dir) / job_id
    _upgrades = upgrade_pages or {}
    recovered: dict[int, int] = {}
    blocked: dict[int, str] = {}
    doc_deadline = time.monotonic() + _ROTATION_DOC_BUDGET_S
    for done_idx, pno in enumerate(targets, 1):
        if await _run_is_cancelled(job_id):
            logger.info(
                f"[{job_id}] Rotation heal cancelled — keeping "
                f"{len(recovered)} recovered pages"
            )
            break
        page_deadline = min(
            time.monotonic() + _ROTATION_PAGE_BUDGET_S, doc_deadline
        )
        chosen_angle, chosen_md = None, ""
        candidates = await _prescreen_rotation_angles(pdf_path, pno)
        if candidates is None:
            candidates = _ROTATION_CANDIDATES
        else:
            logger.info(
                f"[{job_id}] Rotation prescreen p{pno}: probing "
                f"{list(candidates)}deg only (geometric)"
            )
        try:
            for angle in candidates:
                slice_path = job_dir_p / f"rot{angle}-p{pno}.pdf"
                try:
                    await asyncio.to_thread(
                        _write_rotated_slice, pdf_path, pno, angle,
                        str(slice_path)
                    )
                    md, congested = await _probe_slice_angle(
                        str(slice_path), backend, job_id,
                        page_deadline=page_deadline, db=db,
                        is_cancelled=_is_cancelled,
                    )
                    if congested:
                        # 上游容量受限：本页**未被真正读到**。不再试其余
                        # 角度（同样会被拒），如实记录原因后退出本页。
                        blocked[pno] = ROTATION_BLOCKED_CONGESTION
                        logger.warning(
                            f"[{job_id}] Rotation heal p{pno}: upstream "
                            f"congestion persisted past the page budget — "
                            f"page left unprobed (NOT the same as "
                            f"'content unreadable')"
                        )
                        break
                    if _accept_heal_text(md):
                        if pno in _upgrades:
                            # 嫌疑横置页升级：旋转读取须明显富于切片恢复
                            # 结果才替换（正常横版宽表旋转后读取更差，
                            # 自然被拒）。
                            if (len(md.strip())
                                    <= _upgrades[pno] * _ROTATION_UPGRADE_FACTOR):
                                continue
                            logger.info(
                                f"[{job_id}] Rotation upgrade: p{pno} rotated "
                                f"{angle}deg read {len(md.strip())} chars vs "
                                f"slice { _upgrades[pno]} — replacing"
                            )
                        chosen_angle, chosen_md = angle, md
                        logger.info(
                            f"[{job_id}] Rotation heal: p{pno} recovered at "
                            f"{angle}deg ({len(md.strip())} chars)"
                        )
                        break
                except (OCRCancelled, _HealCancelled):
                    raise
                except Exception as rot_err:
                    logger.warning(
                        f"[{job_id}] Rotation probe p{pno}@{angle}deg failed: "
                        f"{redact_urls(str(rot_err))[:200]}"
                    )
                finally:
                    slice_path.unlink(missing_ok=True)
        except (OCRCancelled, _HealCancelled):
            # 取消（用户点了取消 / 在退避期间检测到取消）：**不是**探测失败。
            # 保留已恢复页并停止本轮 —— 旧实现把 OCRCancelled 当普通探测
            # 异常记录，于是"用户已取消"被写成"角度探测失败"，还会继续
            # 试其余角度、继续打上游。
            logger.info(
                f"[{job_id}] Rotation heal cancelled — keeping "
                f"{len(recovered)} recovered pages"
            )
            break
        if chosen_angle is not None:
            clean = _sanitize_ocr_text(chosen_md.strip())
            await db.execute(
                "UPDATE page_cache SET raw_html = ?, ocr_diagnostics = ?, "
                "structured_json = NULL, analyzed_at = NULL "
                "WHERE job_id = ? AND page = ?",
                (clean,
                 _self_heal_diag(prior_diags.get(pno), recovered=True,
                                 content_len=len(clean),
                                 rotation_deg=chosen_angle),
                 job_id, pno),
            )
            if pno in pages_by_num:
                pages_by_num[pno]["markdown"]["text"] = clean
            recovered[pno] = chosen_angle
        elif pno in blocked:
            # **区分性诊断**（缺陷 #120）：上游容量受限 → 本页根本没轮上
            # 探测。绝不与 rotation_probed 并存（互斥）—— 并存会把上游
            # 繁忙误读成"此页无内容"，而后者是内容结论。
            # raw_html 不动（保留 stage1 空页警告横幅，走人工复核路径）。
            await db.execute(
                "UPDATE page_cache SET ocr_diagnostics = ? "
                "WHERE job_id = ? AND page = ?",
                (_self_heal_diag(
                    prior_diags.get(pno), recovered=False,
                    rotation_blocked=blocked[pno],
                 ),
                 job_id, pno),
            )
        elif prior_diags.get(pno) and pno not in _upgrades:
            # 旋转探测未果（90/270/180° 重渲染后 OCR 仍稀疏/空）：落
            # rotation_probed 诊断（round-23 A2）— 复核页据此提示"系统
            # 已尝试旋转恢复未果"，区别于未探测过的空页；GMP 可追溯
            # 恢复尝试本身即是完整性证据。raw_html 不动（保留 stage1
            # 空页警告横幅，走人工复核路径）。
            await db.execute(
                "UPDATE page_cache SET ocr_diagnostics = ? "
                "WHERE job_id = ? AND page = ?",
                (_self_heal_diag(prior_diags.get(pno), recovered=False,
                                 rotation_probed=True),
                 job_id, pno),
            )
        await _report_heal_progress(
            db, job_id, done_idx, len(targets),
            [p for p in targets if p not in recovered],
        )
        await db.commit()
        if time.monotonic() >= doc_deadline:
            # 文档级预算耗尽：剩余目标页一并如实标记（不假装探测过），
            # 停止整轮 —— 上游持续拥塞时继续只是白等。
            rest = [p for p in targets[done_idx:] if p not in recovered]
            for p in rest:
                blocked.setdefault(p, ROTATION_BLOCKED_CONGESTION)
            if rest:
                logger.warning(
                    f"[{job_id}] Rotation heal stopped: document budget "
                    f"({_ROTATION_DOC_BUDGET_S:.0f}s) exhausted — "
                    f"{len(rest)} page(s) left unprobed: p{rest}"
                )
            break
    if blocked:
        await _audit_log(
            db, job_id, "stage1_rotation_blocked",
            f"pages={ {p: blocked[p] for p in sorted(blocked)} } — upstream "
            f"capacity limited; these pages were NOT probed (retrying the "
            f"job later may recover them)",
        )
        await db.commit()
    return recovered, blocked


def _self_heal_diag(prior: dict | None, *, recovered: bool = False,
                    content_len: int = 0, round_num: int = 0,
                    rotation_deg: int | None = None,
                    rotation_probed: bool = False,
                    rotation_blocked: str | None = None) -> str | None:
    """自愈恢复页的诊断 JSON：保留原始完整性证据，标记自愈状态。

    门禁 1（页级诊断可追溯）：旧实现自愈 UPDATE 把 ocr_diagnostics 置 NULL，
    该页"曾因空页/缺失占位被判不完整"的证据就此丢失。恢复页应能回答
    "此页为何被重跑" — 以 prior_diagnostics 存原始诊断 + self_healed 标记。
    新增：recovery_round / content_length / recovered 标记，便于审计追踪
    自愈效果（哪一轮恢复、恢复后内容量）。rotation_deg 为旋转恢复采纳角
    （round-23 A）；rotation_probed=True 表示旋转探测已尝试但未过验收
    （round-23 A2，复核页提示人工核对原图）。

    rotation_blocked（2026-09-16，缺陷 #120）：**上游容量受限导致未能真正
    完成探测**时取 `"upstream_congestion"`。与 rotation_probed **互斥**，
    因为两者是不同的事实：

      旋转探测跑了、结论是"这几张角度都读不出内容"  → rotation_probed
      根本没轮上跑（上游一直拒绝服务）              → rotation_blocked

    互斥是刻意的：若两者并存，任何"看 rotation_probed 就知道系统尽力了"的
    读者（含复核页提示）都会把**上游繁忙**误读成**此页无内容**。后者是内容
    结论，前者是可重试的基础设施状况 —— 在 GMP 复核里不是一回事。
    这也是本键能在 prior 缺失时**单独**存在的原因（必须留痕）。
    """
    if not prior and not rotation_blocked:
        return None
    diag: dict = {
        "self_healed": True,
        "recovered": recovered,
        "source": (prior or {}).get("source", "unknown"),
    }
    if prior:
        diag["prior_diagnostics"] = prior
    if round_num:
        diag["recovery_round"] = round_num
    if content_len:
        diag["content_length"] = content_len
    if rotation_deg is not None:
        diag["rotation_deg"] = rotation_deg
    if rotation_blocked:
        # 互斥由结构保证（见 docstring）：被上游容量挡住时**不得**声称
        # "探测过"。写成 elif 而非两个独立 if —— 即便调用方两个都传了，
        # 也不会产出自相矛盾的诊断。
        diag["rotation_blocked"] = rotation_blocked
    elif rotation_probed:
        diag["rotation_probed"] = True
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


async def _load_prior_diagnostics(db, job_id: str, pages: list[int]) -> dict[int, dict]:
    """B2-2：读取自愈目标页的**既往**页级诊断（软失败，失败不阻断自愈）。

    为什么必须留痕：恢复写库时以 ``prior_diagnostics`` 保留旧值（不置 NULL），
    旧实现两处 ``except: pass`` 让"诊断**读不出来**"与"这页**本来就没有**诊断"
    不可区分 —— GMP 追溯链需要的恰恰是这个区分。
    解析不了的页不放进结果（调用方按"无既往诊断"处理），但会记一条 warning。
    """
    out: dict[int, dict] = {}
    if not pages:
        return out
    try:
        ph = ",".join("?" * len(pages))
        cursor = await db.execute(
            f"SELECT page, ocr_diagnostics FROM page_cache "
            f"WHERE job_id = ? AND page IN ({ph})",
            [job_id, *pages],
        )
        for r in await cursor.fetchall():
            if not r["ocr_diagnostics"]:
                continue
            try:
                out[int(r["page"])] = json.loads(r["ocr_diagnostics"])
            except json.JSONDecodeError as exc:
                logger.warning(
                    f"[{job_id}] self-heal: 页 {r['page']} 的既往 ocr_diagnostics "
                    f"无法解析（软失败，该页既往诊断将无法保留）: {exc}"
                )
    except Exception as exc:
        logger.warning(
            f"[{job_id}] self-heal: 读取既往 ocr_diagnostics 失败"
            f"（软失败，自愈继续）: {type(exc).__name__}: {exc}"
        )
    return out


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
    # 切片恢复内容的长度（round-23 A3 嫌疑横置页升级的比较基线）
    healed_len: dict[int, int] = {}
    if backend in ("mineru", "paddle"):
        # 切片落盘目录保障（round-23 单测实证）：Paddle selfheal-*.pdf 与
        # 旋转 rot*-*.pdf 都写 job 目录 — 生产由上传层 mkdir（upload.py），
        # 但 retry 复用缓存 / 测试直呼 run_pipeline 的路径无保障；目录缺失
        # 时 fitz save 抛 FzErrorSystem code=2，整个自愈链静默失败。
        (Path(config["app"].output_dir) / job_id).mkdir(
            parents=True, exist_ok=True)
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
            # prior_diagnostics 保留（不置 NULL）。B2-2 起读取走**单一实现点**
            # （可单测；两处静默失败已补 warning）。
            prior_diags = await _load_prior_diagnostics(db, job_id, list(retry_targets))
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
                                healed_len[pno] = len(clean)
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
                                healed_len[pno] = len(clean)
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
                # 嫌疑横置页升级（round-23 A3，e2e 实证）：切片恢复页中横向
                # 几何（aspect_ratio>1）者 —— VL 旋转容忍度可能使其被
                # "部分恢复"（表格可读、标题乱码），补跑旋转探测择优替换。
                upgrade_pages = {
                    p: healed_len[p] for p in recovered
                    if p in healed_len
                    and (prior_diags.get(p) or {}).get("aspect_ratio", 0) > 1.0
                }
                rot_targets = list(still_empty) + list(upgrade_pages)
                rot_blocked: dict[int, str] = {}
                if rot_targets:
                    # 旋转恢复通道（round-23 A）：内容横置页切片重试必然
                    # 仍空 — 逐角度重渲染探测，采纳首个通过验收的角度。
                    # 返回 (recovered, blocked)：blocked 为因**上游容量受限**
                    # 而未真正探测的页（缺陷 #120），须与"探测无果"区分。
                    rot_recovered, rot_blocked = await _rotation_heal(
                        db, job_id, pdf_path, rot_targets, backend,
                        pages_by_num, prior_diags,
                        upgrade_pages=upgrade_pages,
                    )
                    if rot_recovered:
                        _seen = set(recovered)
                        recovered.extend(
                            p for p in rot_recovered if p not in _seen
                        )
                        await _audit_log(
                            db, job_id, "stage1_rotation_recovered",
                            f"recovered_pages={dict(rot_recovered)} — "
                            f"content was sideways; re-rendered at the "
                            f"adopted angle and re-OCR'd",
                        )
                        still_empty = [
                            p for p in still_empty if p not in rot_recovered
                        ]
                if still_empty:
                    # 区分两种"仍未恢复"（缺陷 #120）：被上游容量挡住的页
                    # **没有**被真正读过，说成"内容不可识别"会把基础设施
                    # 状况包装成内容结论，误导复核者。
                    truly_unrecognizable = [
                        p for p in still_empty if p not in rot_blocked
                    ]
                    if truly_unrecognizable:
                        logger.warning(
                            f"[{job_id}] Empty-page retry: still empty "
                            f"after re-OCR — p{truly_unrecognizable} truly "
                            f"unrecognizable by the OCR backend"
                        )
                    if rot_blocked:
                        logger.warning(
                            f"[{job_id}] Empty-page retry: {len(rot_blocked)} "
                            f"page(s) NOT probed due to upstream capacity — "
                            f"p{sorted(rot_blocked)} (retrying the job later "
                            f"may recover them)"
                        )
                # 自愈结束：清除 self_heal 子键（total<=0 时 state 层跳过写入）
                await _report_heal_progress(db, job_id, 0, 0, [])
            except Exception as retry_err:
                logger.error(
                    f"[{job_id}] Empty-page retry failed: {retry_err}"
                )
    return recovered
