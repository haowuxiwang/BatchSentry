"""Finding 质量单源（M2）—— 置信度评分 + 类型白名单规范化。

设计原则
--------
1. **置信度来自客观信号**，而非 LLM 自报：来源确定性（规则层 > LLM 结构化
   提取 > LLM 自然语言生成）+ 所在页完整性标记（OCR 警告/稀疏/截断/
   grounding）。同一套评分在**写入期**（stage3 落库 findings.confidence）与
   **读取期**（旧 job 无该列时兜底现算）共用，避免两处口径漂移。
2. **类型白名单**：LLM 可能产出规范外的 type。规范外的类型会污染前端
   `type_zh` 映射、按类型统计口径与评测分解，故此处统一归一到最近的规范
   类型；无法归一的落到 `uncategorized`（而非混入 `completeness`，避免
   继续膨胀最大的噪声桶）。原文 type 由调用方保留在 `raw_type`。
3. **无 DB / 无网络 / 无副作用**：纯函数模块，可在 core 与 api 两侧自由引用，
   不会引入 api→core 的反向依赖。

本模块是 `api/review.py` 原有 `_confidence_for` 的迁出目标（T2.4）：逻辑下沉到
core 后，stage3 写库与 api 读库共享同一实现。
"""
from __future__ import annotations

import re
from datetime import date
from typing import NamedTuple

# ── 类型白名单（T2.6）────────────────────────────────────────────────────────
# 取值与实际产出对齐（经全库 DISTINCT type 实测 + 规则/分析器字面量扫描确认）。
CANONICAL_TYPES: tuple[str, ...] = (
    "completeness",
    "handwritten",
    "param_out_of_spec",
    "signature_time_anomaly",
    "signature_mismatch",
    "suspicious_date",
    "time_reversal",
    "year_contradiction",
    "batch_inconsistency",
    "step_gap",
    "ocr_noise",
    "spec_unverifiable",  # M2：实测值与规格单位不一致且无换算规则 → 需人工核定
    # ── M4：R11–R17 运营合规检查（GMP 高频空洞）──
    "mass_balance",     # R11 物料平衡/收率：已声明但未记录实测值 / 无可核定范围
    "self_review",      # R12 操作人=复核人（ALCOA+ Attributable 独立复核失效）
    # R98/CR-4：签名辨认不可靠（低置信）—— 只提示"无法排除同一人复核"，
    # **不得**断言同一人（info，非缺陷结论）。
    "signature_unreadable",
    "equipment_state",  # R13 设备/清洁状态：确认项未通过或声明未填写
    "env_monitor",      # R14 环境监测：压差/洁净/温湿度项缺失或未通过
    "doc_version",      # R15 文件版本：同一文件编号出现多个版本
    "deviation_link",   # R16 超限偏差关联：有异常/超限但未记录偏差编号
    "alteration",       # R17 涂改规范：疑似划改但缺签名/日期
    "user_rule",
    "uncategorized",
)

# 未知类型 → 规范类型的显式别名（含 LLM 常见变体与中文名）。
TYPE_SYNONYMS: dict[str, str] = {
    "time_anomaly": "signature_time_anomaly",
    "signature_anomaly": "signature_time_anomaly",
    "signature_time": "signature_time_anomaly",
    "date_anomaly": "suspicious_date",
    "suspicious_time": "suspicious_date",
    "time_order": "time_reversal",
    "reverse_time": "time_reversal",
    "year_conflict": "year_contradiction",
    "year_inconsistency": "year_contradiction",
    "param_out_of_range": "param_out_of_spec",
    "out_of_spec": "param_out_of_spec",
    "parameter_violation": "param_out_of_spec",
    "batch_number_inconsistency": "batch_inconsistency",
    "batch_mismatch": "batch_inconsistency",
    "handwriting": "handwritten",
    "manual_entry": "handwritten",
    "missing_step": "step_gap",
    "step_missing": "step_gap",
    "missing_content": "completeness",
    "incomplete": "completeness",
    "ocr_sparse": "ocr_noise",
    "sparse_text": "ocr_noise",
    "user_defined": "user_rule",
    # ── M4/R11–R17 变体 ──
    "yield": "mass_balance",
    "material_balance": "mass_balance",
    "balance_rate": "mass_balance",
    "operator_reviewer_same": "self_review",
    "self_check": "self_review",
    "equipment_cleanliness": "equipment_state",
    "cleaning_state": "equipment_state",
    "cleanliness": "equipment_state",
    "env_monitoring": "env_monitor",
    "environment": "env_monitor",
    "version_mismatch": "doc_version",
    "document_version": "doc_version",
    "deviation": "deviation_link",
    "oos_link": "deviation_link",
    "tamper": "alteration",
    "correction": "alteration",
    # 中文名（LLM 偶尔直接回中文 type）
    "时间倒序": "time_reversal",
    "时间异常": "signature_time_anomaly",
    "年份矛盾": "year_contradiction",
    "可疑日期": "suspicious_date",
    "参数越界": "param_out_of_spec",
    "内容不完整": "completeness",
    "批号不一致": "batch_inconsistency",
    "手写": "handwritten",
    "步骤缺失": "step_gap",
    "收率": "mass_balance",
    "物料平衡": "mass_balance",
    "自检自核": "self_review",
    "设备状态": "equipment_state",
    "清洁状态": "equipment_state",
    "环境监测": "env_monitor",
    "文件版本": "doc_version",
    "偏差关联": "deviation_link",
    "涂改": "alteration",
    "划改": "alteration",
}

# 关键词兜底（按序匹配，先到先得）—— 覆盖未登记的变体。
# 顺序敏感：M4 新类型（R11–R17）关键词更具体，置于通用词之前，避免被
# "spec"/"missing" 等宽泛词提前截获（如 equipment_spec_mismatch 应归
# equipment_state 而非 param_out_of_spec）。
_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    # ── M4：R11–R17 ──
    ("mass_balance", "mass_balance"),
    ("material_balance", "mass_balance"),
    ("yield", "mass_balance"),
    ("收率", "mass_balance"),
    ("物料平衡", "mass_balance"),
    ("self_review", "self_review"),
    ("自检", "self_review"),
    ("equipment", "equipment_state"),
    ("clean", "equipment_state"),
    ("设备", "equipment_state"),
    ("清洁", "equipment_state"),
    ("清洗", "equipment_state"),
    ("env_monitor", "env_monitor"),
    ("environment", "env_monitor"),
    ("环境", "env_monitor"),
    ("压差", "env_monitor"),
    ("doc_version", "doc_version"),
    ("version", "doc_version"),
    ("文件版本", "doc_version"),
    ("deviation", "deviation_link"),
    ("偏差", "deviation_link"),
    ("alter", "alteration"),
    ("tamper", "alteration"),
    ("涂改", "alteration"),
    ("划改", "alteration"),
    # ── 既有 ──
    ("batch", "batch_inconsistency"),
    ("year", "year_contradiction"),
    ("sign", "signature_time_anomaly"),
    ("revers", "time_reversal"),
    ("date", "suspicious_date"),
    ("param", "param_out_of_spec"),
    ("spec", "param_out_of_spec"),
    ("hand", "handwritten"),
    ("step", "step_gap"),
    ("gap", "step_gap"),
    ("ocr", "ocr_noise"),
    ("noise", "ocr_noise"),
    ("user", "user_rule"),
    ("complet", "completeness"),
    ("missing", "completeness"),
)

UNCATEGORIZED = "uncategorized"


def normalize_finding_type(raw: str | None) -> str:
    """把任意 type 归一到 `CANONICAL_TYPES` 之一（T2.6）。

    归一顺序：已是规范类型 → 显式别名 → 关键词兜底 → `uncategorized`。
    绝不抛异常：LLM 输出不可信，此处必须 fail-safe。
    """
    if not raw:
        return UNCATEGORIZED
    t = str(raw).strip().lower()
    # ⚠️ "是否已在白名单内"只在这里判一次（B3-4）：此前内联 `t in CANONICAL_TYPES`，
    # 与 is_canonical() 是同一判定的两份实现。
    if is_canonical(raw):
        return t
    if t in TYPE_SYNONYMS:
        return TYPE_SYNONYMS[t]
    for needle, canonical in _TYPE_KEYWORDS:
        if needle in t:
            return canonical
    return UNCATEGORIZED


# ── 文案驱动的类型纠正（R95）──────────────────────────────────────────────
#: `param_out_of_spec` 在 LLM 输出里长期充当**万能兜底类型**：实测（试用批
#: 记录 job 4cffb358-3b3）「第1页批号与第2页批号不一致」与「湿度 35%RH%RH
#: 单位重复」都被标成「参数超标」⇒ 复核页标题与内容不符（R95/A2、A6）。
#: 这里按**文案**把这两类"过宽类型"纠正到具体类型；只动过宽类型，不动
#: LLM 已经明确选择的类型（用启发式覆盖一个答对的分类只会引入新错误）。
_OVERBROAD_TYPES = frozenset({"param_out_of_spec", "user_rule"})

#: 真正的"超差"必须带数值判定语义。命中即认为类型没错，不做纠正。
_SPEC_VERDICT_WORDS = (
    "超出规格", "超出范围", "超限", "超标", "超差", "不符合规格", "不符合范围",
    "低于规格", "高于规格", "低于下限", "高于上限", "未在规格",
)

#: 文案 → 类型的强信号（顺序敏感：先具体后笼统）。
_DESC_TYPE_SIGNALS: tuple[tuple[tuple[str, ...], str], ...] = (
    # R107/P1-2：**有效期的日期比较不是数值超差**。实测 `8df2f568` p2：
    # 「校验有效期(2025.05.30)早于检查日期(2025.06.03)」被标成
    # `param_out_of_spec`（UI 标题「参数超标」）—— 比的是两个日期，
    # 不存在任何规格区间。放在最前面：这几条必须优先于后面更笼统的信号。
    (("有效期", "早于"), "suspicious_date"),
    (("有效期", "晚于"), "suspicious_date"),
    (("复验期", "早于"), "suspicious_date"),
    (("复验期", "晚于"), "suspicious_date"),
    (("失效期", "早于"), "suspicious_date"),
    (("校验期", "早于"), "suspicious_date"),
    (("批号", "不一致"), "batch_inconsistency"),
    (("批号", "不同"), "batch_inconsistency"),
    (("批号", "不符"), "batch_inconsistency"),
    (("批号", "漂移"), "batch_inconsistency"),
    (("批号", "混批"), "batch_inconsistency"),
    (("版本", "不一致"), "doc_version"),
    (("版本", "冲突"), "doc_version"),
    (("工序号", "缺"), "step_gap"),
    (("工序编号", "缺"), "step_gap"),
    (("签名", "日期", "早于"), "signature_time_anomaly"),
    (("签名", "日期", "晚于"), "signature_time_anomaly"),
    (("签名", "日期", "倒置"), "signature_time_anomaly"),
    (("签字", "日期", "早于"), "signature_time_anomaly"),
    (("签字", "日期", "晚于"), "signature_time_anomaly"),
    (("签名", "不一致"), "signature_mismatch"),
    (("签名", "不同"), "signature_mismatch"),
    (("签名", "缺失"), "completeness"),
    (("签名", "未签"), "completeness"),
    (("签名", "空白"), "completeness"),
    (("签字", "缺失"), "completeness"),
    (("单位", "重复"), "completeness"),
    (("单位", "不一致"), "completeness"),
    (("单位", "不统一"), "completeness"),
)


_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:[-~–—至到])\s*\d+(?:\.\d+)?")


def is_spec_verdict(desc: str | None) -> bool:
    """文案是否**在断言「某个值超出给定区间」**（R95/A2、A6 同族判定）。

    ``_SPEC_VERDICT_WORDS`` 只认「超出规格 / 超标」这类**成词**表述；实测模型
    常写「30°C 超出用户规则 15-25°C」—— 有判定词「超出」+ 显式区间，语义等价
    却匹配不上，于是被当成"过宽类型"改写成别的类型（把真超差洗成 completeness）。

    故补一条：**判定词 + 数值区间同时出现**才算。只看「超出」会把"超出页数上限"
    也算成参数超差，必须带区间。
    """
    t = str(desc or "")
    if any(w in t for w in _SPEC_VERDICT_WORDS):
        return True
    return bool(
        re.search(r"超出|超过|高于|低于|大于|小于", t) and _RANGE_RE.search(t)
    )


#: 日期字面（点/横线/斜杠/中文年月日四种写法都出现过）。
_DATE_LITERAL_RE = re.compile(
    r"(\d{4})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})"
)

#: 「有效期」类字段名 —— 它们本身是**区间**，不是单点。
_VALIDITY_FIELD_WORDS: tuple[str, ...] = ("有效期", "复验期", "失效期", "校验期")

#: 字段名的**完整写法**（含限定前缀），用于把原文措辞原样带回文案：
#: 「设备校验有效期」→ 回写「校验有效期」，不降格成泛泛的「有效期」。
_VALIDITY_FIELD_RE = re.compile(
    r"(?:校验|复验|失效|校准|标定)?(?:有效期|复验期|失效期|校验期)"
)

#: 区间连接词 —— 两个日期之间**只允许**这些字符，才算"同一个区间"。
#: 中间夹了「生产」之类的词就说明那是两个不相干的日期（R107 实测坑）。
_RANGE_JOINER_RE = re.compile(r"\s*(?:至|到|~|～|-|–|—|to)\s*")

#: 比较锚点：**仪器被使用/检查的那一天**（落点）。有效期是容器，锚点是落点。
#:
#: 实测教训（R115）：旧实现只认「检查日期」，而 `8df2f568` p3 写的是
#: 「称量操作日期」⇒ 真实库 5 条有效期断言**一条都补不上**（命中 0/5），
#: 功能等于没上线。锚点必须是"日期角色"的词族，不是某一个词。
#:
#: 刻意**不含**「签名日期」：`4cffb358` p8 的「复核人签名日期…及设备校验有效期」
#: 会把签名日期当落点，得出"签名早于有效期 ⇒ 时间倒置"这种没有意义的比较。
#:
#: 也**不单列**「称量操作日期」：它是「操作日期」的超串，`_ANCHOR_RE` 在
#: 子串位置同样命中，落点与取到的日期完全相同（变异 N9 实测：删掉它零用例变红）。
#: 少一个词就少一处会漂移的枚举。
_ANCHOR_WORDS: tuple[str, ...] = (
    "检查日期", "操作日期", "使用日期", "称量日期",
    "检验日期", "生产日期", "复核日期",
)
_ANCHOR_RE = re.compile("|".join(sorted(_ANCHOR_WORDS, key=len, reverse=True)))

#: 断言"有效期出问题"的措辞。只用于"可证伪"抑制（见 `is_pure_validity_claim`）。
_VALIDITY_DEFECT_RE = re.compile(r"过期|超期|失效|逾期|超出|早于|晚于|不在有效")

#: 分句切分。`.` 不进分隔符 —— 日期字面里全是点。
_SENT_SPLIT_RE = re.compile(r"[。；;\n]+")


class ValidityVerdict(NamedTuple):
    """有效期判定的**唯一结论载体**（区间语义，不是单点比大小）。

    四种 ``status`` 的区别在于**证据强度**，不是措辞偏好：

    - ``"expired"``      —— 检查日期晚于区间终点（到期日）。只给到期日也能证。
    - ``"before_start"`` —— 原文给了完整区间，检查日期**早于**区间起点。
    - ``"inside"``       —— 原文给了完整区间，检查日期**落在区间内**（可证伪）。
    - ``"not_expired"``  —— 只有到期日（单点），只能证明"没超期"，
      **不能**证明"在有效期内"（起点未知，检查日期可能早于起点）。

    ``days`` = 越过最近那条边界的自然日数（``expired`` 记越过终点的天数，
    ``before_start`` 记早于起点的天数，其余为 0）。
    """

    status: str
    field: str
    start: date | None
    end: date
    checked: date
    days: int


def _to_date(parts) -> date | None:
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


def _verdict_at(text: str, anchor_pos: int) -> ValidityVerdict | None:
    """以某个锚点位置为中心，取"有效期终点"与"检查日期"两个日期并判定。"""
    before, after = text[:anchor_pos], text[anchor_pos:]
    pre = list(_DATE_LITERAL_RE.finditer(before))
    post = list(_DATE_LITERAL_RE.finditer(after))
    if not pre or not post:
        return None
    end = _to_date(pre[-1].groups())
    checked = _to_date(post[0].groups())
    if end is None or checked is None:
        return None
    field_m = _VALIDITY_FIELD_RE.search(before) or _VALIDITY_FIELD_RE.search(text)
    field = field_m.group(0) if field_m else "有效期"

    # 区间起点：只有两个日期之间**确实是连接词**时才认 —— 否则"有效期"前面
    # 那个日期可能是生产日期/清场日期，硬当起点就是编造。
    start: date | None = None
    if len(pre) >= 2:
        joiner = before[pre[-2].end():pre[-1].start()]
        if _RANGE_JOINER_RE.fullmatch(joiner):
            start = _to_date(pre[-2].groups())
            # 写反了（起点晚于终点）或退化成一个点 ⇒ 区间不可信，退回单点语义。
            if start is not None and start >= end:
                start = None

    if start is not None:
        if checked < start:
            return ValidityVerdict(
                "before_start", field, start, end, checked, (start - checked).days
            )
        if checked > end:
            return ValidityVerdict(
                "expired", field, start, end, checked, (checked - end).days
            )
        return ValidityVerdict("inside", field, start, end, checked, 0)
    if checked > end:
        return ValidityVerdict(
            "expired", field, None, end, checked, (checked - end).days
        )
    return ValidityVerdict("not_expired", field, None, end, checked, 0)


def validity_verdict(text: str | None) -> ValidityVerdict | None:
    """把"有效期 vs 检查日期"的文案解析成区间判定（判不了返回 ``None``）。

    用户 2026-10-27 的裁定：**有效期是时间段**（如 `2025.11.02-2026.11.01`），
    不是时间点；检查人所填日期要落在该时间段**内**，仪器才算处于有效期。
    判定必须按"落点是否在容器内"，不能把两个日期并排比大小就完事。

    逐个锚点尝试（而非只取第一个）：真实文案常把多个日期写在同一段里，
    第一个锚点前后可能取不到成对日期，后面的锚点可以。
    """
    t = str(text or "")
    if not any(w in t for w in _VALIDITY_FIELD_WORDS):
        return None
    for m in _ANCHOR_RE.finditer(t):
        v = _verdict_at(t, m.start())
        if v is not None:
            return v
    return None


def is_pure_validity_claim(text: str | None) -> bool:
    """整段文案**都在**说有效期与日期的比较（"可证伪"抑制的纯度门槛）。

    只要有一句在说别的事（签名缺失、批号不符…），就不算纯有效期断言 ——
    复合文案里的真缺陷不能因为前半句被证伪而连坐。
    """
    sents = [s for s in _SENT_SPLIT_RE.split(str(text or "")) if s.strip()]
    if not sents:
        return False
    return all(
        any(w in s for w in _VALIDITY_FIELD_WORDS) and _VALIDITY_DEFECT_RE.search(s)
        for s in sents
    )


def validity_overdue_clause(text: str | None) -> str | None:
    """给"有效期早于/晚于检查日期"类文案补一条**可算出来**的区间说明（R107/R115）。

    为什么只补、不改写
    ------------------
    原文案给的是**单点**日期（「校验有效期(2025.05.30)」），复核员容易读成
    "有效期就是这一天"。但区间**起点在原文里根本没有**（表里只有到期日）——
    硬写「区间 2024.05.31–2025.05.30」就是编造。所以这里只补确定的事：
    到期日、检查日期、相差多少天，以及"区间起点未识别"这个事实。

    返回 ``None`` 的条件（一律不补，宁可少说）：
    - 判不出结论（见 :func:`validity_verdict`）；
    - 检查日期落在有效期内 / 只给到期日且未到期 —— 没有"超出"可讲，
      补一句"超出 -3 天"只会更乱。
    """
    v = validity_verdict(text)
    if v is None:
        return None
    if v.status == "expired":
        if v.start is not None:
            return (
                f"（{v.field} {v.start.isoformat()} 至 {v.end.isoformat()}，"
                f"检查日期 {v.checked.isoformat()}，已超出 {v.days} 天。）"
            )
        return (
            f"（{v.field}至 {v.end.isoformat()}，检查日期 {v.checked.isoformat()}，"
            f"已超出 {v.days} 天；有效期区间起点未识别，请核对原页。）"
        )
    if v.status == "before_start":
        return (
            f"（{v.field} {v.start.isoformat()} 至 {v.end.isoformat()}，"
            f"检查日期 {v.checked.isoformat()}，早于有效期起点 {v.days} 天。）"
        )
    return None


#: 结论性"合规"表述 —— 出现即说明**这条 finding 自己在说没问题**。
#: 只认成词表述，避免把「符合规定温度区间」之类的中途修饰语当结论。
_COMPLIANT_CONCLUSIONS: tuple[str, ...] = (
    "符合要求", "符合规定", "符合标准", "符合规范",
    "无异常", "未见异常", "无问题", "均正常", "全部正常",
)

#: 缺陷/存疑词 —— 任一出现即**不是**自证合规的条目。
#:
#: 这张表的作用是**防止误抑制**（fail-open）：宁可放过一条噪声，也不能把
#: 一条真缺陷当"合规声明"吞掉。所以宁可多列 —— 多列只会让本判据失效，
#: 少列才会造成静默漏识别。
#: 特别注意 `不符合` 必须在列：`"不符合要求"` 里含 `符合要求`，
#: 少了它就等于把"不合规"读成"合规"（方向恰好反了）。
_DEFECT_MARKERS: tuple[str, ...] = (
    "不符合", "不符", "缺失", "缺少", "缺漏", "遗漏", "缺页",
    "未签", "未填", "未勾", "未注明", "未识别", "未执行", "未校验",
    "未记录", "未标注", "未及时", "无法识别", "不可辨", "漏识别",
    "超标", "超限", "超差", "超出", "超期", "过期", "失效", "逾期",
    "异常", "不一致", "矛盾", "倒置", "错误", "空白", "存疑",
    "不正常", "无日期", "无签名", "涂改", "划改", "错位", "缺失",
    "未签名", "未复核", "未审批", "笔误",
)


#: 引号内的内容是**被引用的原文 / 栏名 / 检查项名**，不是本条断言的结论。
#:
#: 反例（`0c5cfb00` p15，真实语料）：
#: 「第15页 检查项「S2103是否符合要求」勾选为"否"（◇），需确认是否已启动偏差处理」
#: —— 不剥引号就会因为**检查项名字里带"符合要求"**而把这条真缺陷当合规声明吞掉。
_QUOTED_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"|'[^']*'|【[^】]*】")


def is_compliant_claim(text: str | None) -> bool:
    """文案是否**在结论上自称合规**（R104/P1-6）。

    为什么要有它
    ------------
    实测（`8df2f568` p9）LLM 产出过这样一条 `self_review`：

        步骤8和步骤9中，操作人'赵阿珍'与复核人'王小雨'不同，符合要求。
        但需注意，第4页中'王小雨'曾作为操作人自我复核……

    **前半句自己在说"没问题"**，后半句是跨页联想。这类条目的危害不是"多一条"，
    而是复核员会认为**系统把合规项也当缺陷** ⇒ 整条流水线的可信度下降。

    判据（两条同时成立）
    -------------------
    1. 通篇**没有任何缺陷/存疑词**（`_DEFECT_MARKERS`）——
       这一条同时把"不符合要求"排除在外（方向恰好反了的那类）；
    2. 出现结论性合规表述（`_COMPLIANT_CONCLUSIONS`）。

    两条都在**剥掉引号内容之后**判定：引号里是检查项名 / 原文摘录，不是本条的
    结论（反例见 `_QUOTED_RE`）。

    ⇒ `"步骤8符合要求，但步骤9未签名"` **不会被误伤**（含 `未签`）。

    只做文案判定：不需要 prompt 改动，也不需要结构化数据，因此对
    `rule` / `user_rule` / `llm_page` / `llm_cross` **一律适用**。
    """
    t = _QUOTED_RE.sub(" ", str(text or ""))
    if not t.strip():
        return False
    if any(m in t for m in _DEFECT_MARKERS):
        return False
    return any(c in t for c in _COMPLIANT_CONCLUSIONS)


def has_business_text(finding: dict | None) -> bool:
    """finding 是否有**可复核的业务正文**（CR-1 ③ / R97）。

    空文案的 finding 落库只会让复核员看到一张空卡片。R97 之前更糟：护栏把
    降级理由拼进 ``description``，于是"空正文"被填成一句后端日志
    （实测 p22 `1563`：整条只剩「LLM 独断 critical，未经规则层确定性判据复核」）。

    ⇒ 写入口径统一：**没有正文就不写行**（stage2/stage3 共用本函数，避免
    "一处过滤一处不过滤"的漂移；`tests/unit/test_r97_empty_description.py`
    有结构断言钉住两个写入口都调用它）。

    注意：只认"空白"（空串 / 纯空白），**不**做长度阈值 —— 短正文
    （如"缺少签名"）是合法的。
    """
    if not isinstance(finding, dict):
        return False
    return bool(str(finding.get("description") or "").strip())


def type_from_description(desc: str | None) -> str | None:
    """按文案内容推断类型；推断不出返回 ``None``（不猜）。

    只用于"过宽类型"的纠正与"无指称 user_rule"的类型回填 —— 不是通用的
    分类器，也不参与 LLM 已明确给出且未命中的情形。
    """
    t = str(desc or "")
    if not t.strip():
        return None
    for needles, ftype in _DESC_TYPE_SIGNALS:
        if all(n in t for n in needles):
            return ftype
    return None


# ── 垃圾桶类型按文案重分类（R102 / CR-29）─────────────────────────────────
#: 实测 `signature_time_anomaly` 被 LLM 当成"和签名有关的一切"（§3.1d）：同一类型
#: 下面混着姓名不一致、自我复核、缺日期、年份误读、记录先于 SOP 生效。
#: 按类型一刀切抑制会**静默漏识别**，所以要先按**文案**把它们送回各自的类型，
#: 剩下的才真是"签名时间的先后"（那一类由 CR-28 判）。
_TRASH_TYPES = frozenset({"signature_time_anomaly"})

#: 文案 → 具体类型（**顺序敏感**：先具体后笼统）。只在"垃圾桶类型"上生效。
_TRASH_RECLASS_SIGNALS: tuple[tuple[tuple[str, ...], str], ...] = (
    # 签名人身份不符
    (("姓名", "不一致"), "signature_mismatch"),
    (("姓名", "不同"), "signature_mismatch"),
    (("不同人",), "signature_mismatch"),
    (("非本人",), "signature_mismatch"),
    # 自我复核（同一人既操作又复核）
    (("自我复核",), "self_review"),
    (("自己复核",), "self_review"),
    (("同一人",), "self_review"),
    # 年份不可信（未来日期 / 相差 N 年）—— 结论落在 `suspicious_date`
    (("未来日期",), "suspicious_date"),
    (("相差", "年"), "suspicious_date"),
    (("超过", "年"), "suspicious_date"),
    # 记录先于文件生效 ⇒ 文件控制问题（§3.1d 表 p2 行）
    (("文件起草",), "doc_version"),
    (("文件批准",), "doc_version"),
    (("批准日期",), "doc_version"),
    # 缺日期
    (("缺失",), "completeness"),
    (("无日期",), "completeness"),
    (("未填",), "completeness"),
    (("空白",), "completeness"),
)


#: 签名语：出现在日期/角色词附近时，说明这一侧说的是"某人的签署日期"。
_SIGNATURE_WORD_RE = re.compile(r"签名|签字|签署|签批|签认")

#: 角色语：`operator` / `复核人` 这类**签署主体**词。R9a（规则层）的文案是
#: 「reviewer 杨月萍 签名时间(2025.06.04) 早于 operator 韩思萍(2025.06.21),
#: 复核/审批顺序异常」—— **只有左侧带"签名"**，右侧是裸的角色+姓名+时间，
#: 且以「复核/审批顺序异常」收尾。要求"两侧都带签名语"在结构上不可能满足
#: ⇒ 见 `is_signature_order_claim` 的非对称判定。
_ROLE_SIDE_RE = re.compile(
    r"复核人|复核员|复核者|复核|审核人|审核|检查人|检查者|检查员|"
    r"操作人|操作员|操作者|执行人|批准人|放行人|记录人|填表人|"
    r"转移人|接受人|接收人|质量保证|"
    r"operator|reviewer|draft|review|approver|supervisor|qa",
    re.IGNORECASE,
)

#: 关系词两侧各看多少字。实测文案的形态是「复核人签名日期（2025.06.04）早于
#: 检查人签名日期（2025.06.21）」—— 签名语离关系词 4~14 字；取 24 留余量，
#: 又不至于跨到隔壁分句（中文分句普遍在 20 字内结束）。
_SIG_ORDER_WINDOW = 24


def is_signature_order_claim(text: str) -> bool:
    """文案是否只是「两处签名日期的先后」这一类断言（CR-28 唯一覆盖的形态）。

    ⚠️ **本函数在 R102 从 `core/rules/llm_finding_guard.py` 上移到这里**，
    原因只有一个：CR-29（入库归一）也要用它，而 `finding_quality` 是**叶子模块**
    （只 import `re`），`llm_finding_guard` 反过来依赖 `core.rules.*` —— 留在原处
    会形成循环。上移后两边**共用这一份**，`llm_finding_guard` 保留
    `_is_signature_order_claim` 别名，调用点与测试不受影响。

    判定：找到**第一个**先后关系词，左右各取 24 字窗口；**至少一侧明确带签名语**，
    且另一侧是签名语或角色语，才算"两处签署时间的先后"。

    ⚠️ 为什么不能只看 type：实测 28 条 `signature_time_anomaly` 里，同一类型被
    LLM 用来表达「签名姓名不一致」「签名时间缺失」「记录发放日早于文件起草日」
    「接受人签名晚于清洗日 10 年（年份写错）」—— 后四类都是真缺陷。
    按类型抑制 = 静默漏识别。

    ⚠️ 为什么不要求"两侧都有日期字面"：p7 原文是
    「复核人签名日期早于检查人签名日期。」——**一个日期都没有**，
    但它恰恰就是用户裁定的那一条。所以锚点只能是签名语，不是日期。

    ⚠️ **非对称判定（R98 补正）**：旧实现要求两侧**都**含签名语，于是 R9a 的
    规则文案（右侧只有 `operator` + 姓名 + 时间，收尾是「复核/审批顺序异常」）
    永远匹配不到 ⇒ 用户已裁定的"签名先后不算缺陷"在规则层**静默失效**。
    一侧有签名语、另一侧是角色语，语义上仍然是"两个人的签署时间"⇒ 计入。
    反向控制：另一侧是「生产日期」「清洗日」「设备校验有效期」时**不**计入。

    ⚠️ **跨年的一对也抑制**（曾有"跨年放行"的版本，已撤销）。理由不是"先后不判定"
    这么笼统，而是**原页复核**：p8 页面上根本没有 2015（复核人写的是 2025.06.30），
    p27 页面上也没有 2015 —— 跨年只可能是 OCR 年份误读，不是真缺陷。
    年份可信度由 `suspicious_date` 独立判定（CR-33 会进一步收紧）。
    """
    if not text:
        return False
    m = re.search(
        r"晚于|迟于|早于|先于|after|later\s+than|before|earlier\s+than",
        text, re.IGNORECASE,
    )
    if not m:
        return False
    left = text[max(0, m.start() - _SIG_ORDER_WINDOW): m.start()]
    right = text[m.end(): m.end() + _SIG_ORDER_WINDOW]
    left_sig = bool(_SIGNATURE_WORD_RE.search(left))
    right_sig = bool(_SIGNATURE_WORD_RE.search(right))
    left_role = bool(_ROLE_SIDE_RE.search(left))
    right_role = bool(_ROLE_SIDE_RE.search(right))
    if left_sig and right_sig:
        return True
    # 非对称：一侧说"签名"，另一侧是签署主体（角色词）
    if left_sig and right_role:
        return True
    if right_sig and left_role:
        return True
    return False


#: 「复核」侧的签署主体词。**只收复核/审核这一族** —— 用户 2026-10-26 的裁定
#: 点名的是「复核人」与「检查人」，把 `操作人`/`批准人` 卷进来会越界：
#: 操作在前、复核在后本来就是正常时序，它们的日期**不必**相同。
#:
#: ⚠️ 裸 `复核`/`审核` 必须带**排除尾巴**：`复核完成日期`、`审核结果`、
#: `复核记录` 说的是"事情"，不是"人"。不排除的话
#: 「复核人签名早于检查完成日期」这种句子会被判成"这一对的日期比较"，
#: 把一条合法 finding 无理由抬到 warning。
#: 裸 `review` 已删：它会把 `reviewed`/`review record` 一起吃掉，`reviewer` 够用。
_ROLE_TAIL_NOT = r"(?!完成|日期|时间|结果|意见|记录|项目|报告|表|单|项|的|了|过)"
_REVIEW_ROLE_RE = re.compile(
    r"复核人|复核员|复核者|审核人|审核员|审核者|reviewer"
    r"|复核" + _ROLE_TAIL_NOT + r"|审核" + _ROLE_TAIL_NOT,
    re.IGNORECASE,
)

#: 「检查」侧的签署主体词。同样的排除尾巴：`检查完成日期` / `检查结果` /
#: `检查记录` 是**事情**。裸 `check` 已删（会把 `check record` 吃掉）。
_CHECK_ROLE_RE = re.compile(
    r"检查人|检查员|检查者|checker|inspector"
    r"|检查" + _ROLE_TAIL_NOT,
    re.IGNORECASE,
)

#: 关系词两侧的取景宽度（字符）。
#:
#: **为什么是 40 而不是复用 `_SIG_ORDER_WINDOW`(24)**：英文角色词更长 ——
#: `reviewer signed 2025.06.04` 已经 27 字，24 字窗口会把 `reviewer` 截成
#: `iewer`（实测匹配不到）⇒ 英文形态静默失效。而 `_SIG_ORDER_WINDOW` 还
#: 被 `is_signature_order_claim` 用于垃圾桶类型判定，改它会连带改变那边
#: 的分类结果 —— 两件事分开，各用各的常量。
_ROLE_PAIR_WINDOW = 40

#: 取景的**截断标点**：角色归属只在同一小句内成立。
#: 不截 `.` —— 日期字面 `2025.06.04` 里就有点。
_CLAUSE_BREAKS = "，。；、,;\n"


def _clause_left(text: str, pos: int, width: int) -> str:
    """取关系词左侧同一小句内的文本（最多 `width` 字）。"""
    seg = text[max(0, pos - width):pos]
    cut = 0
    for ch in _CLAUSE_BREAKS:
        i = seg.rfind(ch)
        if i + 1 > cut:
            cut = i + 1
    return seg[cut:]


def _clause_right(text: str, pos: int, width: int) -> str:
    """取关系词右侧同一小句内的文本（最多 `width` 字）。"""
    seg = text[pos:pos + width]
    cut = len(seg)
    for ch in _CLAUSE_BREAKS:
        i = seg.find(ch)
        if i != -1 and i < cut:
            cut = i
    return seg[:cut]


def is_review_check_date_claim(text: str) -> bool:
    """文案是否是「复核人 ⇄ 检查人」这一对的签署日期比较。

    用户 2026-10-26 改判（R111）：**复核人与检查人的日期必须一致** ——
    同一张记录上这两个角色是同一轮放行里前后脚签的，日期不同（无论早于
    还是晚于）都是异常。这与"操作人 06.21 / 复核人 06.04"那种跨角色比较
    是两回事：跨角色的先后属正常时序，同一轮的两个角色不一致才是异常。

    ⚠️ **只判"这一对"**，不判"是不是异常"。判据是**角色归属**：
    关系词（早于/晚于…）两侧，一侧是复核族、另一侧是检查族。
    两个角色写在**同一侧**（如「复核人签名早于检查完成日期」）不算 ——
    那说的是"日期"而不是"检查人签署的日期"，且 `检查完成` 已被
    `_ROLE_TAIL_NOT` 排除。

    为什么不用 type 判：实测 LLM 把 `signature_time_anomaly` 当成
    "和签名有关的一切"（见 `is_signature_order_claim` 的注释），
    按类型一刀切会同时吞掉姓名不符、签名缺失等真缺陷。

    ⚠️ 反向控制见 `test_r111_signature_pair.py`：`操作人 ⇄ 复核人`、
    `复核人 ⇄ 批准人`、`生产日期 ⇄ 复核人` 都必须返回 False。
    """
    if not text:
        return False
    m = re.search(
        r"晚于|迟于|早于|先于|after|later\s+than|before|earlier\s+than",
        text, re.IGNORECASE,
    )
    if not m:
        return False
    left = _clause_left(text, m.start(), _ROLE_PAIR_WINDOW)
    right = _clause_right(text, m.end(), _ROLE_PAIR_WINDOW)
    left_review = bool(_REVIEW_ROLE_RE.search(left))
    left_check = bool(_CHECK_ROLE_RE.search(left))
    right_review = bool(_REVIEW_ROLE_RE.search(right))
    right_check = bool(_CHECK_ROLE_RE.search(right))
    return (left_review and right_check) or (left_check and right_review)


def _reclassify_trash_type(f: dict) -> str | None:
    """就地重分类"垃圾桶类型"；返回被替换掉的原始 type（未改写返回 None）。

    **先后断言优先**：文案如果本身就是"两处签名日期的先后"，保持原类型不动 ——
    否则 p18 那种「签名日期（2025.04.21）晚于复核者签名日期（无日期）」会因为
    句子里顺带出现"无日期"被改成 `completeness`，**换了个说法说同一件事**，
    而"签名日期先后"这一族在复核页上有专门的解释文案（R111 起不再被抑制）。
    """
    raw = str(f.get("type") or "").strip().lower()
    if raw not in _TRASH_TYPES:
        return None
    desc = str(f.get("description") or "")
    if not desc.strip():
        return None
    if is_signature_order_claim(desc):
        return None  # 真·签名先后 —— 保持原类型，别改判成"信息缺失"
    for needles, ftype in _TRASH_RECLASS_SIGNALS:
        if all(n in desc for n in needles):
            if ftype == raw:
                return None
            f["type"] = ftype
            return raw
    return None


def _repair_overbroad_type(f: dict) -> str | None:
    """就地纠正过宽 type；返回被替换掉的原始 type（未改写返回 None）。"""
    raw = str(f.get("type") or "").strip().lower()
    if raw not in _OVERBROAD_TYPES:
        return None
    desc = str(f.get("description") or "")
    if raw == "param_out_of_spec" and is_spec_verdict(desc):
        return None  # 文案确实在断言超差 —— 类型没错
    fixed = type_from_description(desc)
    if not fixed or fixed == raw:
        return None
    f["type"] = fixed
    return raw


def normalize_finding(f: dict) -> tuple[str, str | None]:
    """写入期类型归一（R95 / R102）：按文案纠正，再走白名单归一。

    顺序：**垃圾桶类型重分类（CR-29）→ 过宽类型纠正（R95）→ 白名单归一**。
    前两步互斥（`signature_time_anomaly` 不在 `_OVERBROAD_TYPES`，过宽类型也不在
    `_TRASH_TYPES`），所以用 `if None` 串起来即可，不存在"改两次"的链。

    Returns:
        ``(canonical_type, raw_type)`` —— ``raw_type`` 是需要落库留痕的
        **原始** type（未改写时 ``None``，与 ``findings.raw_type`` 语义一致）。
    """
    if not isinstance(f, dict):
        return UNCATEGORIZED, None
    original = f.get("type")
    repaired_from = _reclassify_trash_type(f)
    if repaired_from is None:
        repaired_from = _repair_overbroad_type(f)
    canonical = normalize_finding_type(f.get("type"))
    raw = repaired_from if repaired_from is not None else (
        original if canonical != str(original or "").strip().lower() else None
    )
    return canonical, raw


def is_canonical(raw: str | None) -> bool:
    """原始 type 是否已在白名单内（无需归一）。

    ⚠️ 这是"是否规范类型"的**唯一判定点**（B3-4）：`normalize_finding_type`
    的第一步必须经由这里，不要在调用方内联 ``x in CANONICAL_TYPES``。
    """
    return bool(raw) and str(raw).strip().lower() in CANONICAL_TYPES


# ── 复核分级（三色，M6/T6.4）────────────────────────────────────────────────
# 按**检出来源**分层，而非按严重度：严重度回答"多严重"（排序用），来源回答
# "谁判定的、可信度如何"（复核方式用）。复核者据此分配注意力 —— 规则层是
# 确定性判据（红：必须逐条看，机器已确认事实）、LLM 层是辅助提示（蓝：可
# 快速批量扫），未命中任何规则的类型计入"系统校验通过"（绿：覆盖面证明，
# 见 `core/rules/registry.py::rule_coverage`）。
#
# 取值来源经实测扫描（core/rules/*、core/pipeline/stage2|stage3 的写入点）：
#   rule / user_rule / llm_page / llm_fallback / llm_cross。
REVIEW_TIER_BY_SOURCE: dict[str, str] = {
    "rule": "rule",
    "user_rule": "rule",   # 用户自定义规则：同样是确定性判据（可复现）
    "llm_page": "llm",
    "llm_fallback": "llm",
    "llm_cross": "llm",
}
# 三色层级（顺序即面板展示顺序）。
REVIEW_TIERS: tuple[str, ...] = ("rule", "llm", "pass")
REVIEW_TIER_ZH: dict[str, str] = {
    "rule": "规则命中",
    "llm": "LLM 辅助",
    "pass": "系统校验通过",
}
# 由 finding 承载的分级（去重保序）—— `pass` 不在其中：它是覆盖面概念，
# 不来自单条 finding，见 rule_coverage()。
FINDING_TIERS: tuple[str, ...] = tuple(dict.fromkeys(REVIEW_TIER_BY_SOURCE.values()))


def review_tier(source: str | None) -> str:
    """finding.source → 复核分级（rule/llm/pass 三色中的前两色）。

    - 缺省（空）来源 → `rule`：与写入路径一致（stage3 落库时
      `f.get("source", "rule")`，置信度评分同样 `or "rule"`）。
    - 未知非空来源 → `llm`（保守）：视为需人工判读的辅助提示，绝不
      冒充确定性判据 —— 误标成"规则"会让复核者放松警惕。
    """
    src = str(source or "").strip().lower()
    if src in REVIEW_TIER_BY_SOURCE:
        return REVIEW_TIER_BY_SOURCE[src]
    return "llm" if src else "rule"


def attach_review_tier(findings: list[dict]) -> list[dict]:
    """就地给每条 finding 附 `tier` 字段，并返回同一列表。

    SSR（main.py 复核页）与 AJAX（api/review.py list_findings）共用此函数，
    前端只读 `f.tier`，不再各持一份 source→颜色映射 —— 双端映射漂移是
    `type_zh` 历史上踩过的坑（见 tests/unit/test_type_sync.py）。
    """
    for f in findings:
        if isinstance(f, dict):
            f["tier"] = review_tier(f.get("source"))
    return findings


def tier_counts(findings: list[dict]) -> dict[str, int]:
    """按分级统计条数（面板三色计数条的前两色）。

    只统计"由 finding 承载"的分级（rule/llm）；`pass` 是覆盖面概念、不来自
    finding，由 `rule_coverage()` 提供，故不在此处硬编码。
    """
    counts = {t: 0 for t in FINDING_TIERS}
    for f in findings:
        if not isinstance(f, dict):
            continue  # 与 attach_review_tier 同款容错（上游可能混入非 dict）
        t = f.get("tier") or review_tier(f.get("source"))
        if t in counts:
            counts[t] += 1
    return counts


# ── 置信度评分（T2.4 迁出 api/review.py）─────────────────────────────────────
_CONF_BASE = 0.85
_CONF_LLM_GEN_PENALTY = 0.15   # llm_cross/llm_fallback/user_rule（自然语言生成）
_CONF_LLM_PAGE_PENALTY = 0.10  # llm_page（结构化提取，稍可靠）
_CONF_PAGE_FLAG_PENALTY = 0.20  # 所在页带 OCR 警告/稀疏/截断/grounding 横幅
_CONF_MIN, _CONF_MAX = 0.30, 0.95


# 页面级完整性告警键（OCR 侧链 + 结构化裁剪 + schema 修复）。任一中招即视为
# "该页证据不完整"，置信度下调并建议人工复核。
PAGE_FLAG_KEYS: tuple[str, ...] = (
    "_ocr_warning",
    "_ocr_sparse",
    "_ocr_truncated",
    "_grounding_warn",
    "_truncated_warn",
    "_schema_warn",
)


def page_is_flagged(page_data: dict | None) -> bool:
    """该页结构化结果是否带完整性告警（写/读两侧共用同一判据）。"""
    if not isinstance(page_data, dict):
        return False
    return any(page_data.get(k) for k in PAGE_FLAG_KEYS)


def confidence_for(finding: dict, page_flagged: bool = False) -> float:
    """单条 finding 的置信度评分 [0.30, 0.95]。

    信号：来源确定性（规则层 > LLM 结构化提取 > LLM 自然语言生成）+ 所在页
    完整性标记。评分范围有上下界，防止单信号把置信度推到极端。
    """
    score = _CONF_BASE
    src = finding.get("source") or "rule"
    if src in ("llm_cross", "llm_fallback", "user_rule"):
        score -= _CONF_LLM_GEN_PENALTY
    elif src == "llm_page":
        score -= _CONF_LLM_PAGE_PENALTY
    if page_flagged:
        score -= _CONF_PAGE_FLAG_PENALTY
    return round(max(_CONF_MIN, min(_CONF_MAX, score)), 2)
