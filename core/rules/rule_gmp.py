"""M4 —— R11–R17：GMP 运营合规规则（批记录高频、此前完全未检查的系统空洞）。

设计原则（与既有 rule_* 模块一致）
----------------------------------
1. **触发信号必须真实存在**：每条规则的触发键都经真实 51 页批记录
   （job 33495b33-761）逐字段核查后才落定，不做"理论上应该检查"的空规则。
   定位结论（原始证据见 docs/PLAN_v1.1_EXECUTION.md M4 与提交说明）：
   - 收率/物料平衡项以 `parameters[]` 承载（name=浓缩滤液收率/制备收率/
     SP-2收率/精制收率/提取总收率/PE袋物料平衡率，含 spec_range 与 value）；
   - 操作人/复核人以 `step.operator`/`step.reviewer` 与 `signatures[]`
     （role 混杂 operator/review/draft/reviewer）承载 → 1 处真实"自检自核"；
   - 设备/清洁确认项是 `parameters[]` 里的"是/否"题（如
     「设备及管路是否及时清洗干净」「状态标志使用是否正确」）；
   - 环境监测在洁净区页落为「压差是否符合要求」「洁净区层流区压差」确认项；
   - `page_info.file_code` 跨页**合法地各不相同**（R20/R22/R23… 是不同的
     表单），故**不能**做"全页同编号"判定；真实缺陷是"同一 file_code 出现
     两个版本"（R23→{R23,09}、R27→{11,10}）；
   - 异常/偏差以「生产有无异常」「偏差号」承载；
   - 涂改痕迹（涂改/划改/更正）在本记录为 0 → R17 规格驱动、真实数据不误报。
2. **保守触发、宁缺勿滥**：只报"可机检的硬信号"（已声明未填写 / 明确否 /
   版本冲突 / 同类人），模糊情形交给 LLM 语义支路。这不改变"不得静默标记
   成功"的原则——规则层沉默不等于该页合规，而是无规则级证据。
3. **不与 R3 重复**：收率/物料平衡若同时有 spec_range 与 value，由 R3 判
   越界；R11 只补 R3 判不到的"缺实测值 / 缺合格范围"。
"""
from __future__ import annotations

import logging
import re

from core.rules.parsing import _parse_spec

logger = logging.getLogger(__name__)


# ===========================================================================
# 通用工具
# ===========================================================================

# 人名归一：真实数据的操作人字段含 '#'（OCR 噪声）、空格、全角空格与标点，
# 直接比较会漏判"同一人"。归一后比较可把 "#王志清" 与 "王志清" 视为同人。
_NAME_NOISE_RE = re.compile(r"[\s\u3000.,，。;；:：'\"“”()（）/\\\-_#*]+")

# 是/否 选择题的取值范围（value 归一后）
_NEGATIVE_VALUES = frozenset({
    "否", "不", "不是", "不符合", "不合格", "不通过", "未通过", "未完成",
    "无", "没有", "异常", "有异常", "×", "x", "✗",
})

# 判定"该参数是选择题模板"的 spec 特征（是/否、√、□）
_CHOICE_SPEC_MARKS = ("是", "否", "√", "☑", "□", "✔")


def _norm_person(name) -> str:
    """归一操作人/复核人姓名以便同人判定；非字符串返回空串。"""
    if not isinstance(name, str):
        return ""
    return _NAME_NOISE_RE.sub("", name).strip()


def _norm_value(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_negative(value) -> bool:
    """value 是否表否定/异常（是/否题的"否"侧）。"""
    v = _norm_value(value).lower()
    if not v:
        return False
    if v in _NEGATIVE_VALUES:
        return True
    # "否"/"不合格"开头（如 "否（已处理）"）；"无异常"不视为否定。
    return v.startswith("否") or v.startswith("不合格") or v.startswith("不符合")


def _is_choice_spec(spec) -> bool:
    """spec 是否像"是/否"选择题模板（用于判"已声明但未填写"）。"""
    s = _norm_value(spec)
    return bool(s) and any(m in s for m in _CHOICE_SPEC_MARKS)


def _iter_params(page: dict):
    """产出 (step, param) —— 仅 dict 元素（_normalize_pages 已兜底，双保险）。"""
    for step in page.get("steps") or []:
        for p in step.get("parameters") or []:
            if isinstance(p, dict):
                yield step, p


def _iter_measurement_cols(page: dict):
    """产出 (step, col_name, cell) —— 参数矩阵列单元格。"""
    for step in page.get("steps") or []:
        for m in step.get("measurements") or []:
            if not isinstance(m, dict):
                continue
            for col, cell in (m.get("values") or {}).items():
                if isinstance(cell, dict):
                    yield step, str(col), cell


# ===========================================================================
# R11 —— 物料平衡/收率（mass_balance）
# ===========================================================================

_BALANCE_KEYWORDS = ("收率", "物料平衡", "平衡率", "得率", "回收率", "收得率")

# 工序文本里"声明"了收率/物料平衡要求（如「浓缩滤液收率应≥50%」）
_BALANCE_DECL_RE = re.compile(
    r"(收率|物料平衡|平衡率|得率|回收率)[^。；;\n]{0,16}?[≥≤<>]=?\s*\d"
)

#: 领用/使用/损耗三列表（实测 sy p9「PE袋使用的物料平衡计算」）：
#: 结构化参数名形如「透明PE袋(1100×700) 领用数」，value 尾部才是数量
#: （前面可能粘着批号，如 `0309 20`）。
_BALANCE_ROW_RE = re.compile(r"^(?P<item>.*?)\s*(领用数|使用数|损耗数)$")
_TRAILING_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*$")


def _trailing_number(text: str) -> float | None:
    """取字符串尾部的数字（`0309 20` → 20；`30` → 30）。"""
    m = _TRAILING_NUM_RE.search(_norm_value(text))
    return float(m.group(1)) if m else None


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PURE_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_ROW_MARKER_RE = re.compile(r"^\d{1,3}$")
#: 表格之后的**工序句子**（含句读的长 token）⇒ 表格区到此结束。
_SENTENCE_RE = re.compile(r"[。！？；]")
#: 表头之后只扫这么多字符，避免把页脚/下一段工序的数字误当表格行。
_BALANCE_TEXT_WINDOW = 600


def _plain_text(raw_html: str) -> str:
    """OCR 原文去标签并压缩空白（规则层拿到的 `raw_html` 是 HTML 片段）。"""
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", raw_html or "")).strip()


def _balance_triples_from_text(raw_html: str) -> list[tuple[float, float, float]]:
    """从 OCR 原文的「领用数/使用数/损耗数」表解析 (领用, 使用, 损耗) 三元组。

    为什么需要这条兜底：结构化抽取**可能漏掉**这张表（实测 sy_r7 p9 原文
    清晰含 `20 30 0`，但 `structured_json` 里没有这三个参数）—— 此时只靠
    `steps[].parameters` 的 R11 永远看不到数据，确定性判据等于失效。

    解析口径刻意保守（宁可漏、不可错）：
      - 只认**表头** `领用数/领用数量` 之后 `_BALANCE_TEXT_WINDOW` 字符内的数字；
      - 三元组之后必须是**行号**（1–3 位整数）且再下一个 token 非纯数字，
        或已到文本末尾 —— 否则视为批号/代码等无关数字（实测 `72502 0309 20`
        这类连续数字串若不设此闸门会误判成一行）；
      - 读到**工序句子**（含句读的长 token）即视为表格结束：p9 表格之后紧接
        「9 将过筛后物料转移至…」与步骤 10 的压差/温湿度表，后者的
        `20.6 51.6 11.2` 若不截断会被误读成一行平衡表。
    """
    text = _plain_text(raw_html)
    triples: list[tuple[float, float, float]] = []
    for head in re.finditer(r"领用(?:数量|数)", text):
        tokens = text[head.start(): head.start() + _BALANCE_TEXT_WINDOW].split()
        i, n = 0, len(tokens)
        found_any = False
        while i + 2 < n:
            tok = tokens[i]
            if found_any and len(tok) >= 6 and _SENTENCE_RE.search(tok):
                break
            if not (_PURE_NUM_RE.match(tok) and _PURE_NUM_RE.match(tokens[i + 1])
                    and _PURE_NUM_RE.match(tokens[i + 2])):
                i += 1
                continue
            nxt = tokens[i + 3] if i + 3 < n else None
            marker_ok = nxt is None or (
                _ROW_MARKER_RE.match(nxt)
                and (i + 4 >= n or not _PURE_NUM_RE.match(tokens[i + 4]))
            )
            if marker_ok:
                triples.append((float(tok), float(tokens[i + 1]), float(tokens[i + 2])))
                found_any = True
                i += 3
                continue
            i += 1
    return triples


def _check_mass_balance(pages: list[dict]) -> list[dict]:
    """R11：收率/物料平衡可否核定。

    与 R3 的分工：spec+value 同时存在 → R3 判越界，本规则不重复。本规则只补
    R3 覆盖不到的两类空洞：
      (a) 已声明合格范围（spec_range 可解析）但**未记录实测值** → warning；
      (b) 有实测值但**未声明合格范围** → info（无法判断是否合格）；
      (c) 工序文本声明收率/物料平衡要求，但该页**无任何**收率类参数 → warning
          （要求被声明却未见可核定的结构化实测值）。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        declared_in_text = False
        has_balance_param = False
        #: 结构化里确实解析出了「领用/使用/损耗」行（有读数）⇒ 以结构化为准，
        #: 不再跑原文兜底（避免同一张表被两条路径各报一次）。
        structured_rows_seen = False
        for step in page["steps"]:
            op = _norm_value(step.get("operation"))
            if op and _BALANCE_DECL_RE.search(op):
                declared_in_text = True
            step_no = step.get("step_no", "?")
            rows: dict[str, dict] = {}
            for p in step.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                name = _norm_value(p.get("name"))
                row_m = _BALANCE_ROW_RE.match(name)
                if row_m:
                    has_balance_param = True
                    num = _trailing_number(p.get("value"))
                    if num is not None:
                        structured_rows_seen = True
                        row = rows.setdefault(row_m.group("item"), {"handwritten": False})
                        row[row_m.group(2)] = num
                        row["handwritten"] = (
                            row["handwritten"]
                            or str(p.get("value_source") or "").strip().lower()
                            == "handwritten"
                        )
                    continue
                if not name or not any(k in name for k in _BALANCE_KEYWORDS):
                    continue
                has_balance_param = True
                value = _norm_value(p.get("value"))
                spec = _norm_value(p.get("spec_range"))
                bounds = _parse_spec(spec)
                if not value and bounds is not None:
                    findings.append({
                        "page": pno,
                        "type": "mass_balance",
                        "severity": "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} {name} 已声明合格范围 "
                            f"{spec} 但未记录实测值，无法核定物料平衡/收率是否合格"
                        ),
                        "ocr_text": f"{name}: spec={spec} value=空",
                        "operator": "",
                        "source": "rule",
                    })
                elif value and not spec:
                    # 仅在**确实未声明**合格范围时报；spec 存在但无法解析时
                    # 交给 R3 的 LLM 兜底（不得谎报"未声明"—— M4 实测发现
                    # "99%~101%" 曾被误报为未声明）。
                    findings.append({
                        "page": pno,
                        "type": "mass_balance",
                        "severity": "info",
                        "description": (
                            f"第{pno}页 工序{step_no} {name}={value} 未声明合格范围，"
                            f"无法核定物料平衡/收率是否合格，请确认规格来源"
                        ),
                        "ocr_text": f"{name}: value={value} spec=空",
                        "operator": "",
                        "source": "rule",
                    })
            for item, row in rows.items():
                issue, used = row.get("领用数"), row.get("使用数")
                if issue is None or used is None:
                    continue
                lost = row.get("损耗数") or 0.0
                # 使用数不可能超过领用数；使用+损耗超过领用同样不成立 ⇒
                # 这一处的表内算术自相矛盾（实测 sy p9：OCR 读成 20/30/0，
                # 原页为 30/30/0）。手写读数未经独立复核 ⇒ 只报 info + 附注，
                # 不得定性为偏差结论（断言措辞由读数质量决定，R130 口径）。
                if used > issue or used + lost > issue:
                    _hw = bool(row["handwritten"])
                    _caveat = (
                        "手写读数，未经独立复核 ⇒ 不作为偏差结论，请对照原页核对"
                        if _hw else "印刷值，请对照原页核对"
                    )
                    findings.append({
                        "page": pno,
                        "type": "mass_balance",
                        "severity": "info" if _hw else "warning",
                        "description": (
                            f"第{pno}页 工序{step_no} 物料平衡「{item}」"
                            f"使用数{used:g} / 损耗数{lost:g} 超过领用数{issue:g}"
                            f"（{_caveat}）"
                        ),
                        "ocr_text": (
                            f"balance_row={item}: 领用={issue:g} 使用={used:g} "
                            f"损耗={lost:g}"
                        ),
                        "operator": "",
                        "source": "rule",
                    })
        raw_rows: list[tuple[float, float, float]] = []
        if not structured_rows_seen:
            # 兜底：结构化抽取漏掉整张「领用/使用/损耗」表时，直接读 OCR 原文
            # （实测 sy_r7 p9：原文有 `20 30 0`，结构化里一条参数都没有 ⇒
            # 确定性判据在结构化缺失时本会整体失效）。
            raw_rows = _balance_triples_from_text(page.get("raw_html") or "")
            for idx, (issue_r, used_r, lost_r) in enumerate(raw_rows, 1):
                if not (used_r > issue_r or used_r + lost_r > issue_r):
                    continue
                # 原文读数**无法区分**印刷/手写，且未经独立复核 ⇒ 永远只报 info，
                # 并显式带「不作为偏差结论」（R130 口径：断言措辞由读数质量决定）。
                findings.append({
                    "page": pno,
                    "type": "mass_balance",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 物料平衡表第{idx}行：领用数{issue_r:g} / "
                        f"使用数{used_r:g} / 损耗数{lost_r:g}，表内算术不成立"
                        f"（OCR 表格读数，未经独立复核 ⇒ 不作为偏差结论，请对照原页核对）"
                    ),
                    "ocr_text": (
                        f"balance_row=text_row{idx}: 领用={issue_r:g} "
                        f"使用={used_r:g} 损耗={lost_r:g}"
                    ),
                    "operator": "",
                    "source": "rule",
                })
        if declared_in_text and not has_balance_param and not raw_rows:
            findings.append({
                "page": pno,
                "type": "mass_balance",
                "severity": "warning",
                "description": (
                    f"第{pno}页 记录文本声明收率/物料平衡要求，但未见可核定的实测值"
                    f"（请确认收率/物料平衡率是否已记录并结构化提取）"
                ),
                "ocr_text": "operation 声明收率/物料平衡，无对应参数",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R12 —— 操作人 ≠ 复核人（self_review）
# ===========================================================================

_OPERATOR_ROLES = frozenset({
    "operator", "操作人", "操作员", "操作者", "draft", "填表人", "记录人",
})
_REVIEWER_ROLES = frozenset({
    "reviewer", "review", "复核人", "复核员", "复核", "审核人", "审核",
    "workshop_reviewer", "supervisor", "qa", "qa_reviewer", "质量保证",
    "批准人", "放行人",
})


def _check_self_review(pages: list[dict]) -> list[dict]:
    """R12：操作人与复核人不得为同一人（ALCOA+ Attributable / 独立复核）。

    两个证据源合并判定：
      - `step.operator` vs `step.reviewer`（结构化字段）；
      - `signatures[]` 中 operator 角色集合 与 reviewer/QA 角色集合的交集。
    只有两侧都非空且归一后相等才进入判定（避免空字段误报）。

    **R95 聚合**：同一页同一人只出一条。旧实现逐工序各报一条 —— 实测
    （试用批记录 p4）工序 8/9/10 同一对签名被报 3 次，是复核页噪声的
    主要来源之一；工序号合并进文案，信息不丢。

    **CR-4（R98）**：只有**至少一对**参与比对的签名两侧都**不**带
    `confidence: low` 时才允许判 `self_review critical`（聚合后同页多工序共用
    一条结论，所以门槛是"有没有一对可靠证据"，不是"每一对都可靠"）。
    该人的工序对**全部**至少有一侧低置信时，结构化数据上「真的同一人复核」与
    「OCR 把两个不同的人读成同一个名字」**无法区分** ⇒ 改发
    `signature_unreadable`（info），文案**并列印出两侧的原始识别串**
    （只引用、不断言谁是谁），`operator` 留空，把判断交回复核员看原页笔迹。
    依据：真实 p4/p26/p40/p46 四条 critical 经逐页核图，全部是手写签名误读。

    **R133 收紧**：证据**只**来自签名槽时，`confidence` **缺失**也不得算可靠 ——
    必须有一对显式 `high` 才允许 `critical`。理由：签名槽天然是手写体，模型漏标
    置信度是常态（51 页实测 163 个签名里 137 个标 low、16 个 medium、仅 10 个 high），
    而 10 页记录 p4 两处签名**没有** confidence 字段，旧口径直接判 critical
    「复核人即操作人」，原页笔迹与姓名实为另一人。印刷字段（`operator`/`reviewer`
    文本）不受此收紧影响。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        # person -> {"steps": [...], "solid": 是否有**可靠**（两侧都不低）的工序对,
        #            "op_raws": [...], "rv_raws": [...]}
        hits: dict[str, dict] = {}
        for step in page["steps"]:
            step_no = step.get("step_no", "?")
            op_raw = _norm_value(step.get("operator"))
            rv_raw = _norm_value(step.get("reviewer"))
            op = _norm_person(op_raw)
            rv = _norm_person(rv_raw)
            same: set[str] = set()
            if op and rv and op == rv:
                same.add(op)
            ops: set[str] = set()
            revs: set[str] = set()
            low: set[str] = set()
            #: R133：签名槽天然是手写体 ⇒ "同一人"结论的证据强度取决于 OCR 自报，
            #: 必须**显式 high** 才允许 critical；缺失 confidence 不等于可靠。
            #: 实证：10 页记录 p4 两处签名 confidence 缺失，规则判 critical「复核人即
            #: 操作人 王小雨」，而原页笔迹与姓名均为另一人（页图核对）。
            sig_pairs: set[str] = set()
            sig_high: set[str] = set()
            sig_non_high: set[str] = set()
            field_pairs: set[str] = set()
            sig_op_raws: dict[str, list[str]] = {}
            sig_rv_raws: dict[str, list[str]] = {}
            for sig in step.get("signatures") or []:
                if not isinstance(sig, dict):
                    continue
                role = _norm_value(sig.get("role")).lower()
                name_raw = _norm_value(sig.get("name"))
                person = _norm_person(name_raw)
                if not person:
                    continue
                if role in _OPERATOR_ROLES:
                    ops.add(person)
                    sig_op_raws.setdefault(person, []).append(name_raw)
                elif role in _REVIEWER_ROLES:
                    revs.add(person)
                    sig_rv_raws.setdefault(person, []).append(name_raw)
                _conf = str(sig.get("confidence") or "").strip().lower()
                if _conf == "high":
                    sig_high.add(person)
                else:
                    # 缺失 / medium / low 都算"非 high"：签名是手写体，不是
                    # "证明同一人"的可靠证据（R133 收紧，实测 p4 两人被读成一人）。
                    sig_non_high.add(person)
                if _conf == "low":
                    low.add(person)
            same |= (ops & revs)
            sig_pairs |= (ops & revs)
            if op and rv and op == rv:
                field_pairs.add(op)
            for person in sorted(same):
                slot = hits.setdefault(
                    person,
                    {"steps": [], "solid": False, "op_raws": [], "rv_raws": []},
                )
                if str(step_no) not in slot["steps"]:
                    slot["steps"].append(str(step_no))
                # 只要**有一步**的两侧签名都不低，这条"同一人"就有可靠证据 ⇒
                # 不得因为同页另一步是低置信而把整条降成 info。
                # R133 例外：证据只来自**签名槽**时，缺失 confidence 不算可靠
                # （必须有一步显式 high）；证据来自**印刷字段**时沿用原口径。
                if person in sig_pairs and person not in field_pairs:
                    # 该步内**所有**签名槽都显式 high 才算可靠：旁证是
                    # `test_one_side_low_is_enough_to_downgrade` —— 操作人 high
                    # 而复核人 low 时不得判 critical（断言"同一人"的证据是两侧签名）。
                    _reliable = person in sig_high and person not in sig_non_high
                else:
                    _reliable = person not in low
                slot["solid"] = slot["solid"] or _reliable
                # 原始识别串：优先取 signatures[] 的原样名字，缺失时回落到结构化字段
                for raw in (sig_op_raws.get(person) or ([op_raw] if op_raw else [])):
                    if raw and raw not in slot["op_raws"]:
                        slot["op_raws"].append(raw)
                for raw in (sig_rv_raws.get(person) or ([rv_raw] if rv_raw else [])):
                    if raw and raw not in slot["rv_raws"]:
                        slot["rv_raws"].append(raw)
        for person, slot in sorted(hits.items()):
            step_nos = slot["steps"]
            steps_txt = "、".join(step_nos) if step_nos else "?"
            if not slot["solid"]:
                # CR-4：全部工序对都至少有一侧低置信 ⇒ 只能出 info，
                # 且不得把"同一人"当成结论。
                op_txt = "、".join(slot["op_raws"]) or "（未识别）"
                rv_txt = "、".join(slot["rv_raws"]) or "（未识别）"
                findings.append({
                    "page": pno,
                    "type": "signature_unreadable",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 工序{steps_txt} 操作人与复核人签名辨认不可靠"
                        f"（原始识别：操作人「{op_txt}」/ 复核人「{rv_txt}」），"
                        f"无法排除同一人复核，请核对原页笔迹"
                    ),
                    "ocr_text": (
                        f"operator_raw={op_txt} reviewer_raw={rv_txt} "
                        f"steps={step_nos}"
                    ),
                    "operator": "",
                    "source": "rule",
                })
                continue
            findings.append({
                "page": pno,
                "type": "self_review",
                "severity": "critical",
                "description": (
                    f"第{pno}页 工序{steps_txt} 操作人与复核人为同一人「{person}」，"
                    f"独立复核失效（ALCOA+ Attributable：记录须可归因到不同责任人）"
                ),
                "ocr_text": f"operator=reviewer={person} steps={step_nos}",
                "operator": person,
                "source": "rule",
            })
    return findings


# ===========================================================================
# R13 —— 设备/清洁状态完整性（equipment_state）
# ===========================================================================

_EQUIP_KEYWORDS = ("状态标志", "清洗", "清洁", "设备", "消毒", "灭菌", "清场", "管路")


def _check_equipment_state(pages: list[dict]) -> list[dict]:
    """R13：设备/清洁状态确认项的完备性与结论。

    - 确认项取值表否定（否/不合格/异常）→ warning（设备状态或清洁未通过）；
    - 确认项带"是/否"模板（spec）却**未填写** → info（已声明未填写）。
    不做"生产页必须有清洁项"的宽泛完整性判定 —— 真实记录中「清洗」字样遍布
    （本记录 314 次），宽泛判定会制造大量噪音。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        for step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            if not name or not any(k in name for k in _EQUIP_KEYWORDS):
                continue
            step_no = step.get("step_no", "?")
            value = _norm_value(p.get("value"))
            spec = _norm_value(p.get("spec_range"))
            if _is_negative(value):
                findings.append({
                    "page": pno,
                    "type": "equipment_state",
                    "severity": "warning",
                    "description": (
                        f"第{pno}页 工序{step_no} 设备/清洁确认项「{name[:40]}」"
                        f"填为「{value}」，设备或清洁状态未通过，请核实是否已处理"
                    ),
                    "ocr_text": f"{name[:40]}={value}",
                    "operator": "",
                    "source": "rule",
                })
            elif not value and _is_choice_spec(spec):
                findings.append({
                    "page": pno,
                    "type": "equipment_state",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 工序{step_no} 设备/清洁确认项「{name[:40]}」"
                        f"已声明（{spec}）但未填写，请确认设备状态与清洁结果已记录"
                    ),
                    "ocr_text": f"{name[:40]}: spec={spec} value=空",
                    "operator": "",
                    "source": "rule",
                })
    return findings


# ===========================================================================
# R14 —— 环境监测完备性（env_monitor）
# ===========================================================================

_ENV_KEYWORDS = (
    "压差", "温湿度", "湿度", "洁净度", "尘埃", "悬浮粒子", "沉降菌",
    "浮游菌", "环境监测", "层流", "换气次数",
)
# 洁净区工序文本里声明了环境要求（真实：p24/p26「洁净环境符合要求」）
_ENV_DECL_RE = re.compile(r"(洁净环境|环境监测|洁净区|层流|压差|温湿度)")


def _check_env_monitor(pages: list[dict]) -> list[dict]:
    """R14：环境监测项的完备性与结论。

    - 环境确认项取值表否定 → warning；
    - 环境确认项带"是/否"模板却未填写 → info；
    - 工序文本声明洁净环境要求，但该页**既无环境确认项也无环境测量列**
      → info（未见环境监测数据；请确认是否另行记录）。
    注意：不把「温度」纳入环境关键词 —— 该记录工艺温度测量遍布全页，
    纳入会使规则永不触发且语义混乱（工艺温度 ≠ 洁净区环境监测）。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        has_env_param = False
        has_env_measure = False
        declared = False
        for step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            if not name or not any(k in name for k in _ENV_KEYWORDS):
                continue
            has_env_param = True
            step_no = step.get("step_no", "?")
            value = _norm_value(p.get("value"))
            spec = _norm_value(p.get("spec_range"))
            if _is_negative(value):
                findings.append({
                    "page": pno,
                    "type": "env_monitor",
                    "severity": "warning",
                    "description": (
                        f"第{pno}页 工序{step_no} 环境监测项「{name[:40]}」"
                        f"填为「{value}」，环境指标不符合要求，请核实处理与放行影响"
                    ),
                    "ocr_text": f"{name[:40]}={value}",
                    "operator": "",
                    "source": "rule",
                })
            elif not value and _is_choice_spec(spec):
                findings.append({
                    "page": pno,
                    "type": "env_monitor",
                    "severity": "info",
                    "description": (
                        f"第{pno}页 工序{step_no} 环境监测项「{name[:40]}」"
                        f"已声明（{spec}）但未填写，请确认环境监测数据已记录"
                    ),
                    "ocr_text": f"{name[:40]}: spec={spec} value=空",
                    "operator": "",
                    "source": "rule",
                })
        for step in page["steps"]:
            if _ENV_DECL_RE.search(_norm_value(step.get("operation"))):
                declared = True
                break
        if not has_env_param:
            for _step, col, _cell in _iter_measurement_cols(page):
                if any(k in col for k in _ENV_KEYWORDS):
                    has_env_measure = True
                    break
        if declared and not has_env_param and not has_env_measure:
            findings.append({
                "page": pno,
                "type": "env_monitor",
                "severity": "info",
                "description": (
                    f"第{pno}页 工序文本声明洁净环境要求，但未见环境监测数据"
                    f"（压差/温湿度/洁净度），请确认环境监测是否已记录"
                ),
                "ocr_text": "operation 声明洁净环境要求，无环境监测参数/测量列",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R15 —— 文件版本一致性（doc_version）
# ===========================================================================

_VERSION_DIGIT_RE = re.compile(r"\d+")


def _norm_version(ver) -> str:
    """版本号归一：抽取数字串（去前导零）后以 '-' 连接，使 "R23"≡"23"、"09"≡"9"。

    无论文数字时退化为小写原文（如 "A版"）。
    """
    s = _norm_value(ver)
    if not s:
        return ""
    parts = _VERSION_DIGIT_RE.findall(s)
    if not parts:
        return s.lower()
    return "-".join(str(int(p)) for p in parts)


def _check_doc_version(pages: list[dict]) -> list[dict]:
    """R15：同一文件编号（file_code）不得出现多个版本。

    **不做"全页同编号"判定** —— 真实批记录里各表单编号本就不同
    （H3-MPD-10133-R20/R22/R23…），全页同编号会产生海量误报。真实缺陷形态是
    "同一 file_code 跨页出现两个版本"（本记录实测 R23→{R23,09}、R27→{11,10}），
    正是"使用了作废/旧版文件"的机器可检证据。
    """
    findings: list[dict] = []
    groups: dict[str, dict[str, list[int]]] = {}
    for page in pages:
        pi = page.get("page_info") or {}
        code = _norm_value(pi.get("file_code"))
        ver = _norm_value(pi.get("version"))
        if not code or not ver:
            continue
        norm = _norm_version(ver)
        if not norm:
            continue
        groups.setdefault(code, {}).setdefault(norm, []).append(page["page"])
    for code, vmap in groups.items():
        if len(vmap) <= 1:
            continue
        first = min(p for ps in vmap.values() for p in ps)
        detail = "；".join(
            f"{v}(第{','.join(str(x) for x in sorted(ps))}页)"
            for v, ps in sorted(vmap.items())
        )
        findings.append({
            "page": first,
            "type": "doc_version",
            "severity": "warning",
            "description": (
                f"文件编号 {code} 出现 {len(vmap)} 个不同版本：{detail} — "
                f"可能存在作废/旧版文件在用，请核对文件版本控制"
            ),
            "ocr_text": f"file_code={code} versions={sorted(vmap)}",
            "operator": "",
            "source": "rule",
        })
    return findings


# ===========================================================================
# R16 —— 超限偏差关联（deviation_link）
# ===========================================================================

_ANOMALY_NAME_KEYWORDS = ("生产有无异常", "有无异常", "是否异常", "异常情况")
_DEVIATION_REF_KEYWORDS = ("偏差号", "偏差编号", "偏差单号", "偏差记录", "deviation", "dev_no")
_POSITIVE_VALUES = frozenset({"有", "是", "异常", "有异常", "yes", "y", "√", "☑"})


def _check_deviation_link(pages: list[dict], oos_pages: set[int] | None = None) -> list[dict]:
    """R16：存在异常/超限时，必须关联偏差编号（偏差管理可追溯）。

    触发条件（保守，经真实数据与评测双重校准）：
      - **显式异常**：工序/参数声明"有异常"（如「生产有无异常=有」）→ 直接触发；
      - **超限 + 有偏差号字段**：该页存在 R3 判定的参数越界（`oos_pages`），
        **且**该页表单本身设有"偏差号"类字段（无论是否填写）→ 触发。
        若表单根本没有偏差号字段，则不报（该记录可能无此栏目，避免误报）。

    触发后：若"偏差号"类字段**未填写**（或显式异常页无该字段）→ warning。

    设计取舍：早期版本让"任何越界页"都触发，实测会与 R3 重复放大噪音
    （本记录 26 条 param_out_of_spec → +26 条），故收敛为"表单要求关联却留空"
    这一可机检的硬缺口。
    """
    oos_pages = oos_pages or set()
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        explicit = False
        has_ref_field = False
        ref_ok = False
        anomaly_desc = ""
        for _step, p in _iter_params(page):
            name = _norm_value(p.get("name"))
            value = _norm_value(p.get("value"))
            low = name.lower()
            if any(k in name for k in _ANOMALY_NAME_KEYWORDS):
                if value.lower() in _POSITIVE_VALUES:
                    explicit = True
                    anomaly_desc = f"{name}={value}"
            if any(k in name or k in low for k in _DEVIATION_REF_KEYWORDS):
                has_ref_field = True
                if value:
                    ref_ok = True
        triggered = explicit or (pno in oos_pages and has_ref_field)
        if triggered and not ref_ok:
            findings.append({
                "page": pno,
                "type": "deviation_link",
                "severity": "warning",
                "description": (
                    f"第{pno}页 存在异常/超限"
                    f"（{anomaly_desc or '参数超出规格'}）但未记录偏差编号，"
                    f"未关联偏差处理流程，请核实是否已启动偏差"
                ),
                "ocr_text": anomaly_desc or "oos on page",
                "operator": "",
                "source": "rule",
            })
    return findings


# ===========================================================================
# R17 —— 涂改规范（alteration）
# ===========================================================================

_ALTERATION_KEYWORDS = ("涂改", "划改", "修改", "更正", "改错", "作废", "误写", "笔误")
# 疑似划改标记：括号里写"误/改"，或成串的删除线/叉号
_ALTERATION_MARK_RE = re.compile(r"[（(]\s*(误|改|错)\s*[)）]|~~+|××+|✗{2,}")


def _check_alteration(pages: list[dict]) -> list[dict]:
    """R17：疑似涂改必须符合"划改 + 签名 + 日期"规范（ALCOA+ Original）。

    触发：工序操作文本、手写条目或参数取值出现涂改/划改/更正痕迹或删除线标记。
    触发后：所在工序若**既无操作人签名也无执行时间** → warning（无法追溯谁在
    何时、为何修改）；有签名/时间则不报（假定已按规定划改留痕）。
    本记录无涂改痕迹 → 真实数据零误报；规则为"含涂改记录"场景准备。
    """
    findings: list[dict] = []
    for page in pages:
        pno = page["page"]
        for step in page["steps"]:
            traces: list[str] = []
            op = _norm_value(step.get("operation"))
            if op and (any(k in op for k in _ALTERATION_KEYWORDS)
                       or _ALTERATION_MARK_RE.search(op)):
                traces.append("operation")
            notes = [
                _norm_value(n) for n in (step.get("handwritten") or [])
                if isinstance(n, (str, int, float))
            ]
            if any(any(k in n for k in _ALTERATION_KEYWORDS) for n in notes):
                traces.append("handwritten")
            for p in step.get("parameters") or []:
                if not isinstance(p, dict):
                    continue
                v = _norm_value(p.get("value"))
                if v and (_ALTERATION_MARK_RE.search(v)
                          or (any(k in v for k in _ALTERATION_KEYWORDS) and len(v) <= 30)):
                    traces.append(f"param:{_norm_value(p.get('name'))[:20]}")
            if not traces:
                continue
            has_signature = bool(_norm_person(step.get("operator"))) or bool(
                step.get("signatures")
            )
            has_time = bool(_norm_value(step.get("start_time"))
                            or _norm_value(step.get("end_time")))
            if has_signature or has_time:
                continue
            step_no = step.get("step_no", "?")
            findings.append({
                "page": pno,
                "type": "alteration",
                "severity": "warning",
                "description": (
                    f"第{pno}页 工序{step_no} 存在疑似涂改/划改痕迹"
                    f"（{'、'.join(traces)}）但缺操作人签名与日期，"
                    f"涂改未按规定留痕（ALCOA+ Original：原始数据须保留可追溯）"
                ),
                "ocr_text": f"alteration traces: {','.join(traces)}",
                "operator": "",
                "source": "rule",
            })
    return findings
