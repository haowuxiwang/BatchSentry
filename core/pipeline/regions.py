"""区域级证据锚（P0-3）—— 把 finding 锚回 OCR 原始版面区域。

为什么只能到"区域级"
-------------------
spike 实测（``docs/NOISE_REDUCTION_SPIKE.md``）：Paddle 与 MinerU **都不回传
单元格级 bbox**，只回传块/区域级。因此本模块提供的是"表级 bbox 粗筛 + 字段级
标签匹配"的**区域锚**，不是精确到单元格的定位。把区域级自称单元格级，等于给
复核员一个看起来精确、实际错位的框 —— 不如不做。

坐标系、服务端旋转与宽高比闸门（实测结论）
------------------------------------------
真实 51 页样本实测：

* 页面几何 3000×4000（aspect 0.75）；归一化工作副本 720×960（0.75）；
* Paddle 坐标空间 1440×1920（0.75）—— 与页面**同向等比**，故按自身空间归一
  化后可直接映射到渲染页，无畸变（3000/1440 == 4000/1920 == 2.0833）；
* MinerU 坐标空间取自源页自身 page_size（595×842 输入 → 595×842 输出）；
* **例外**：51 页中第 8 页 Paddle 返回 1920×1440（横向），而页面是竖向 ——
  服务端做了旋转。

**旋转不是猜出来的，服务端会明说**：Paddle 的文档前处理默认开启了朝向分类
（``prunedResult.doc_preprocessor_res.model_settings.use_doc_orientation_classify``），
并逐页回传 ``angle``（度，逆时针；取值 ``0|90|180|270``，未启用为 ``-1`` ——
见 PaddleOCR 官方文档）。命中时服务端**先把图转正再做版面检测**，于是
``prunedResult.width/height`` 与全部 ``block_bbox`` 都落在**转正后**的坐标系里。
第 8 页实测 ``angle=270``，而 270° 逆时针 == 90° 顺时针，与页面恰好是转置关系。

**该角度为何可信（独立核验，勿凭直觉）**：Paddle 会在响应里回传
``inputImage``（未旋转的原图）与 ``outputImages.layout_det_res``（它在"旋转后 +
画好检测框"的图上渲染的可视化，URL 授权串为 ``/-1/`` 无过期）。把两者配准即可
**实测**服务端施加了多少旋转，再与 ``angle`` 对表。实测：第 7 页 0°（对照）、
第 8 页 270°CCW，均与上报角一致，且命中率领先异轴候选 2.4~3.2 倍。

⚠️ 不要试图用渲染图的便宜统计量反推旋转 —— 本批记录（密排表格 + 稀疏文字）已实测
**四种候选判据全部不具鉴别力**（墨迹密度 58.8%、投影空白带奇偶、框内文本行方差
12%、整页区域并集 F1 29.4%）。可复现的核验工具是
``scripts/verify_anchor_orientation.py``；完整过程与负面结论见
``docs/REGION_ANCHOR_VISUAL_CHECK.md``。

故写入的坐标**必须**连同其坐标系一起保存（``space`` / ``space_aspect`` /
``space_rotation``）：已知 ``angle`` 时用 :func:`map_bbox_to_page` 做确定性逆
映射（可复算、可测试）；``angle`` 未知时**不得猜测方向** —— 保留原 bbox 并交给
呈现层的宽高比闸门兜底，宁缺勿错。裸坐标（无坐标系）属禁止写入的形态 ——
无法归一化即无法对齐，也就无法在任何方向上被纠正。

纯函数、无 DB、无网络：可在 core/api 两侧自由引用，也可离线复算。
"""
from __future__ import annotations

import re

# ── 标签归一 ───────────────────────────────────────────────────────────────
# 两后端的原始标签词汇表（实测枚举，见模块 docstring）：
#   Paddle : table seal text doc_title header_image vision_footnote footer
#            vertical_text image number figure_title header paragraph_title
#   MinerU : page_header page_footer page_number paragraph table title image
#            equation_interline page_aside_text ...
# 归一到**小集合**：呈现层只需知道"这是表 / 是文本 / 是图 / 是页眉页脚"，
# 原始标签的细粒度差异对复核者无意义（且两后端不互通）。未知标签一律归
# ``other`` —— 用白名单而非原样透传，避免上游新增标签时把脏字符串写进库。
CANONICAL_LABELS = frozenset({
    "table", "text", "title", "image", "seal", "footer", "header", "other",
})

_LABEL_MAP = {
    "table": "table",
    "text": "text", "vertical_text": "text", "page_aside_text": "text",
    "vision_footnote": "text", "paragraph": "text", "list": "text",
    "index": "text", "table_footnote": "text", "image_footnote": "text",
    "title": "title", "doc_title": "title", "paragraph_title": "title",
    "figure_title": "title", "image_caption": "title", "table_caption": "title",
    "image": "image", "header_image": "image",
    "seal": "seal",
    "footer": "footer", "page_footer": "footer", "page_number": "footer",
    "header": "header", "page_header": "header",
}

# 坐标规范：bbox 必须是 [x0, y0, x1, y1] 且已归一化到 [0,1]。
_BBOX_LEN = 4
_TEXT_SNIPPET = 400          # 单区域文本上送上限（锚定只需少量特征词）
_MAX_REGIONS_PER_PAGE = 200  # 单页区域数上限（Paddle 实测单页 ≤17，预留充足）

# 服务端文档朝向分类的合法取值（度，逆时针）。官方文档：未启用时为 -1，
# 故 -1 与任何其它取值一律视为**未知**（不得据以旋转）。
_ROTATIONS = (0, 90, 180, 270)


def canonical_label(raw) -> str:
    """把后端原始标签归一到 :data:`CANONICAL_LABELS` 中的规范标签。"""
    key = re.sub(r"[^a-z_]", "", str(raw or "").strip().lower())
    return _LABEL_MAP.get(key, "other")


def _as_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / inf 会让归一化坐标失去意义（且 JSON 不合法）→ 视为无效
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def normalize_bbox(raw_bbox, space) -> list[float] | None:
    """把原始 bbox 按坐标系 ``space=(w, h)`` 归一化到 [0,1]。

    同时完成：坐标对序修正（``x0 > x1`` 时交换）、越界裁剪、退化框剔除
    （零宽/零高框画不出可辨区域，留着只会误导）。
    """
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != _BBOX_LEN:
        return None
    if not isinstance(space, (list, tuple)) or len(space) != 2:
        return None
    w, h = _as_float(space[0]), _as_float(space[1])
    if not w or not h or w <= 0 or h <= 0:
        return None
    vals = [_as_float(v) for v in raw_bbox]
    if any(v is None for v in vals):
        return None
    x0, y0, x1, y1 = vals
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    x0, x1 = max(0.0, x0), min(w, x1)
    y0, y1 = max(0.0, y0), min(h, y1)
    nx0, nx1 = x0 / w, x1 / w
    ny0, ny1 = y0 / h, y1 / h
    if nx1 - nx0 <= 0 or ny1 - ny0 <= 0:
        return None
    return [round(nx0, 5), round(ny0, 5), round(nx1, 5), round(ny1, 5)]


def map_bbox_to_page(bbox, rotation) -> list[float] | None:
    """把（可能被服务端转正的）OCR 空间 bbox 映射回**页面**空间。

    ``rotation`` 取服务端上报的 ``doc_preprocessor_res.angle``（逆时针度数）。
    逆时针旋转 θ 的逆是顺时针 θ，对归一化坐标即：

    ==========  ==================
    rotation    页面空间 (nx, ny)
    ==========  ==================
    0           (u, v)
    90          (1 - v, u)
    180         (1 - u, 1 - v)
    270         (v, 1 - u)
    ==========  ==================

    其中 ``(u, v)`` 是 bbox 在 OCR 空间里的归一化坐标。表由不变量独立推出，
    并与真实产物逐点核对（第 8 页 ``angle=270``：logo 落在页面左下、页码落在
    右上、标题沿左边缘 —— 三处地标同时成立，见
    ``tests/unit/test_regions_anchor.py`` 的真值复算用例）。

    ``rotation`` 未知（``None`` 或非 ``0|90|180|270``）→ 返回 ``None``：调用方
    **不得**猜测方向，应保留原 bbox 交由呈现层的宽高比闸门兜底。
    """
    if rotation not in _ROTATIONS:
        return None
    if not isinstance(bbox, (list, tuple)) or len(bbox) != _BBOX_LEN:
        return None
    vals = [_as_float(v) for v in bbox]
    if any(v is None for v in vals):
        return None
    u0, v0, u1, v1 = vals
    # 两个角点分别变换后再取 min/max —— 90° 的整数倍旋转仍是轴对齐矩形，
    # 故结果就是该区域的真实矩形，不会被撑大。
    if rotation == 0:
        xs, ys = (u0, u1), (v0, v1)
    elif rotation == 90:
        xs, ys = (1 - v0, 1 - v1), (u0, u1)
    elif rotation == 180:
        xs, ys = (1 - u0, 1 - u1), (1 - v0, 1 - v1)
    else:  # 270
        xs, ys = (v0, v1), (1 - u0, 1 - u1)
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    return [round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)]


def _deep_text(v) -> str:
    """递归取可见文本。

    MinerU 的块文本藏在嵌套结构里（``content.paragraph_content[].content``），
    直接 ``str(dict)`` 会得到带花括号/引号的伪文本，特征词匹配随之失效。
    """
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(t for t in (_deep_text(x) for x in v) if t)
    if isinstance(v, dict):
        for key in ("content", "text", "table_body", "text_content"):
            if key in v:
                t = _deep_text(v[key])
                if t:
                    return t
        return " ".join(
            t for t in (_deep_text(x) for x in v.values()) if t
        )
    return ""


def _snippet(v) -> str:
    text = re.sub(r"<[^>]+>", " ", _deep_text(v))       # 块内容常为 HTML 表格
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_TEXT_SNIPPET]


def _finish(backend: str, space, raw_regions: list[tuple],
            rotation: int | None = None) -> dict | None:
    """组装载荷：归一化 + 标签归一 + 去重 + 上限。无有效区域则返回 None。"""
    w, h = _as_float(space[0] if len(space) else None), _as_float(
        space[1] if len(space) > 1 else None
    )
    if not w or not h or w <= 0 or h <= 0:
        return None
    regions, seen = [], set()
    for label, bbox, text in raw_regions:
        nb = normalize_bbox(bbox, (w, h))
        if nb is None:
            continue
        key = (tuple(nb),)
        if key in seen:
            continue
        seen.add(key)
        regions.append({
            "label": canonical_label(label),
            "bbox": nb,
            "text": _snippet(text),
        })
        if len(regions) >= _MAX_REGIONS_PER_PAGE:
            break
    if not regions:
        return None
    return {
        "backend": backend,
        "space": [int(w), int(h)],
        "space_aspect": round(w / h, 4),
        # 服务端转正时用的逆时针角度；None = 未上报/不合法（不得据以旋转）
        "space_rotation": rotation if rotation in _ROTATIONS else None,
        "regions": regions,
    }


def _rotation_of(pruned) -> int | None:
    """取文档前处理上报的朝向角（``doc_preprocessor_res.angle``）。

    官方文档：未启用朝向分类时为 ``-1``。故只有落在 ``0|90|180|270`` 才认，
    其余一律 ``None`` —— 拿 ``-1`` 当"没转"会静默画出错位的框。
    """
    if not isinstance(pruned, dict):
        return None
    dpr = pruned.get("doc_preprocessor_res")
    if not isinstance(dpr, dict):
        return None
    try:
        angle = int(dpr.get("angle"))
    except (TypeError, ValueError):
        return None
    return angle if angle in _ROTATIONS else None


def extract_regions(page: dict) -> dict | None:
    """从归一化 page dict 抽出区域级 bbox 载荷（Paddle / MinerU 两形态）。

    * Paddle：``prunedResult.width/height`` + ``parsing_res_list[].block_bbox``
      （``block_label`` / ``block_content``），并记下
      ``doc_preprocessor_res.angle`` 作为 ``space_rotation``（服务端可能转正过）；
    * MinerU：``_space``（来自 zip 内 ``layout.json`` 的 ``page_size``）+
      ``_regions``（``_split_pages_by_content_list`` 保留的块 bbox）；该后端
      不做朝向转正，故 ``space_rotation`` 为 ``None``。

    取不到任何有效区域返回 ``None`` —— 调用方据此**不得**写 regions_json，
    也不得挂证据锚（宁缺勿错）。
    """
    if not isinstance(page, dict):
        return None

    pruned = page.get("prunedResult")
    if isinstance(pruned, dict):
        blocks = pruned.get("parsing_res_list")
        if isinstance(blocks, list) and blocks:
            raw = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bbox = b.get("block_bbox")
                if not isinstance(bbox, (list, tuple)):
                    continue
                raw.append((
                    b.get("block_label"),
                    list(bbox)[:_BBOX_LEN],
                    b.get("block_content"),
                ))
            payload = _finish(
                "paddle", (pruned.get("width"), pruned.get("height")), raw,
                _rotation_of(pruned),
            )
            if payload is not None:
                return payload

    blocks = page.get("_regions")
    if isinstance(blocks, list) and blocks and page.get("_space"):
        raw = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bbox = b.get("bbox")
            if not isinstance(bbox, (list, tuple)):
                continue
            raw.append((b.get("label"), list(bbox)[:_BBOX_LEN], b.get("text")))
        return _finish("mineru", tuple(page["_space"]), raw)
    return None


# ── 证据锚：把 finding 锚到具体区域 ─────────────────────────────────────────

# 特征词：数值字面量（规格/实测值是最强信号）与字母数字标识（批号/设备号）。
# 数值两端加"非字母数字"边界："t2101a" 里的 2101 不是独立数值，误当特征词会
# 把锚定引到无关区域（批号里恰好含 2101 时尤其危险）。
_TOKEN_NUM = re.compile(r"(?<![a-z0-9])\d+(?:[.,]\d+)?(?![a-z0-9])")
_TOKEN_ID = re.compile(r"[a-z]{1,4}\d{2,}[a-z]*")
_TOKEN_MIN_LEN = 2
_ANCHOR_MIN_SCORE = 1
_WS = re.compile(r"\s+")
# 匹配期用：抹掉空白，使 OCR 插入的 "0 . 16" 仍能与特征词 "0.16" 命中。
# **提取期不得用它** —— 全删空白会把 "0.16 MPa" 粘成 "0.16mpa"，数值边界
# 判定随之失效（曾因此把全部数值特征词漏掉）。
_SPACE_STRIP = str.maketrans({c: "" for c in " \t\u3000"})


def anchor_tokens(text: str) -> set[str]:
    """从 finding 文案抽出用于区域匹配的特征词（小写；空白归一为单空格）。

    空白归一而非删除：正则的字母数字边界依赖分隔符，删除会把数值与单位
    粘连成一个词元，等于放弃数值特征词。
    """
    norm = _WS.sub(" ", str(text or "").lower())
    toks = {m.group(0).replace(",", ".") for m in _TOKEN_NUM.finditer(norm)}
    toks |= {m.group(0) for m in _TOKEN_ID.finditer(norm)}
    return {t for t in toks if len(t) >= _TOKEN_MIN_LEN}


def region_anchor(text: str, payload: dict) -> dict | None:
    """把一段 finding 文案锚到 ``payload`` 中最匹配的区域。

    判定是**确定性**的（可离线复算、可写测试）：

    1. 取文案中的数值/标识特征词；
    2. 对每个区域计算"命中特征词个数"作为得分；
    3. 取得分最高且 ≥1 的区域；同分时取**面积最小**者（更具体）;
    4. 无任何命中 → ``None``（不锚；宁缺勿错 —— 锚错会把复核员的注意力
       引到无关区域，比不锚更糟）。

    返回值里有三个与坐标系有关的键，职责**不重叠**：

    * ``bbox`` —— OCR 空间（服务端给出的原样框，留痕可回查）；
    * ``page_bbox`` —— **页面空间**，呈现层画的就是它。服务端上报了
      ``angle``（旋转过）时已经过确定性逆映射；未知 ``angle`` 时与 ``bbox``
      相同（此时是否可画由闸门决定）；
    * ``page_aspect`` —— 页面**应当**具有的宽高比（旋转 90/270 时为
      ``space_aspect`` 的倒数）。呈现层拿它与实际渲染图比对，不一致即明示
      "无法定位"，而不是画一个错位的框。
    """
    if not isinstance(payload, dict):
        return None
    regions = payload.get("regions")
    if not isinstance(regions, list) or not regions:
        return None
    toks = anchor_tokens(text)
    if not toks:
        return None
    best, best_key = None, None
    for i, r in enumerate(regions):
        if not isinstance(r, dict):
            continue
        rtext = _WS.sub(" ", str(r.get("text") or "").lower()).translate(
            _SPACE_STRIP
        )
        if not rtext:
            continue
        score = sum(1 for t in toks if t in rtext)
        if score < _ANCHOR_MIN_SCORE:
            continue
        bbox = r.get("bbox")
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) if (
            isinstance(bbox, list) and len(bbox) == _BBOX_LEN
        ) else 1.0
        # 主序：得分高；次序：面积小（更具体）
        key = (-score, area, i)
        if best_key is None or key < best_key:
            best, best_key = r, key
    if best is None:
        return None
    rotation = payload.get("space_rotation")
    rotation = rotation if rotation in _ROTATIONS else None
    ocr_bbox = best.get("bbox")
    space_aspect = payload.get("space_aspect")
    # 服务端转正过 → 用上报的 angle 做确定性逆映射，把框换回页面空间。
    # 未知 angle 时不得猜测方向：保留原 bbox，交给呈现层闸门（比对
    # page_aspect 与渲染图宽高比）决定显示还是明示"无法定位"。
    page_bbox = map_bbox_to_page(ocr_bbox, rotation)
    if page_bbox is None:
        page_bbox = ocr_bbox
    page_aspect = space_aspect
    if rotation in (90, 270) and isinstance(space_aspect, (int, float)):
        if space_aspect > 0:
            page_aspect = round(1.0 / space_aspect, 4)
    return {
        "index": next(
            i for i, r in enumerate(regions) if r is best
        ),
        "label": best.get("label"),
        # bbox = OCR 空间（审计留痕：可回查服务端当时给出的原始框）
        "bbox": ocr_bbox,
        # page_bbox = 页面空间（呈现层画这个；已经过旋转逆映射）
        "page_bbox": page_bbox,
        "score": -best_key[0],
        "space_aspect": space_aspect,
        # 页面**应当**具有的宽高比：旋转 90/270 时为 space_aspect 的倒数。
        # 呈现层拿它与实际渲染图比对 —— 不一致说明坐标系与页面不同源。
        "page_aspect": page_aspect,
        "space_rotation": rotation,
        "rotated": rotation in (90, 270),
        "backend": payload.get("backend"),
    }
