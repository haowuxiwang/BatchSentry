"""签名栏「姓名 + 日期」成对判定 —— 缺哪一半，由**栏名**与**格内实际内容**共同决定。

R119 / TODO 0-48（R111 遗留 #146）
----------------------------------
用户 2026-10-26 第 12 条裁定的后半句：「表头写"签名+日期"⇒ 两者必须都有」。
`core/signature_date_need.py` 只回答了**要不要**日期（用于抑制无据断言），
没有回答**这一格到底缺了哪一半**。本模块补的正是这一半。

实测依据（两份真实批记录，61 页，`page_cache.raw_html` 逐格核对）
---------------------------------------------------------------
结构化层 `signatures[]` 里「有日期、无姓名」的条目 **12 条**，逐条回原文核对，
**没有一条是抽取器漏字**，原文本身就是空格：

    83a0219d p29  「QC 批准/日期：2025.02.21」          —— 格内只有日期
    83a0219d p4   「指令人： 日期： 2015.01.20」        —— 姓名格空着
    83a0219d p12  「负责人：2015.07.21」                —— 格内只有日期
    4cffb358 p9   「岗位负责人签名/日期：应函2025.06.03」—— 两半都有（对照组）

对照组很关键：同一份表单里既有两半都填的格，也有只填日期的格 ⇒ 「只填日期」
是**记录方的填写差异**，不是模板没设姓名格。

为什么不能只看结构化层
----------------------
「有姓名、无日期」这一半（实测 **106 条**）绝大多数是抽取器没抓 `sign_time`，
而不是记录没写日期 —— 真实语料 p13 的检验报告单日期写作「2025年01月20日09时05分」，
结构化里 `sign_time` 全空。**按结构化层判这一半会造出上百条误报**，所以本模块
只认**原文里看得见**的栏名与值（与 `signature_date_need.py` 同一条理由）。

保守原则
--------
1. 只判「栏名：值」这种**行内**写法；表格里靠列位对齐的签名格一律弃权
   （列错位在真实语料里很常见，见 p13 的「取样者 | 2025年01月21日07时34分」）。
2. 值超过 24 字 ⇒ 不是签名格，弃权（那是正文段落）。
3. 既无日期也无姓名（空/N/A）⇒ 弃权，交给既有的「空槽位」判据，不重复报。
4. 日期只认**自带年月日**的字面量 —— 不猜、不从别处补。
"""
from __future__ import annotations

import re

#: 签名位栏名族（**必须带"人/者"**）。刻意不收 `批准`/`复核`/`检验` 裸词：
#: 实测 p29 有「检验日期：2025.02.10」「复核日期」，它们是**日期格**不是签名格，
#: 收进来会把每一个日期格都报成"只填日期没有签名"。
#:
#: ⚠️ 本模块**只依赖标准库**：`core.rules` 要 import 本模块，若这里再回头
#: import `core.rules.*` 就成环（`core.rules` 包级初始化期拿不到子模块属性）。
_ROLE_WORDS: tuple[str, ...] = (
    "签名", "签字", "签署",
    "指令人", "接收人", "负责人", "检测人", "检验人", "复核人", "复核者",
    "检查人", "操作者", "操作人", "执行人", "取样者", "检验者", "批准人",
    "签发人", "审核人",
)

#: 「QC 批准/日期」这种把签名位与日期位写在同一栏名的写法（实测 p29/p30/p31/p32）。
_SLASH_SLOT_RE = re.compile(r"(?:QA|QC)[^：:\s]{0,8}/[^：:\s]{0,8}")

#: 栏名自带"日期"⇒ 这一栏要求两半都有。
_LABEL_DATE_RE = re.compile(r"日期|年月日")

#: 「栏名：值」。栏名取冒号前 1–12 个非空白非标点字符。
_LABEL_RE = re.compile(r"([^\s：:，。；、|]{1,12})\s*[：:]")

#: 自带年月日的日期字面量（`literal_has_date` 认的形态 + 中文写法）。
_DATE_RE = re.compile(
    r"\d{4}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
)

#: 值里可以剥掉的噪声：日期、栏名残留、标点、空格。
_STRIP_WORDS = _ROLE_WORDS + (
    "日期", "签名", "签", "名", "及", "和", "与", "：", ":", "/", "、",
)

#: 值长度上限：超过它就不是签名格。
_MAX_VALUE_LEN = 24

#: 看着像姓名但**不是**姓名的残留（表示"没填/无此项"），不得当成姓名痕迹。
_NOT_A_NAME: frozenset[str] = frozenset({"无", "否", "是", "有", "同上", "略", "空", "未填"})


def _plain(raw_html: str | None) -> str:
    """HTML → 单行纯文本（保留栏名与值之间的冒号，去掉标签与多余空白）。"""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


def _is_slot_label(label: str) -> bool:
    """栏名是不是"签名位"（要写姓名的那种格）。"""
    if any(w in label for w in _ROLE_WORDS):
        return True
    return bool(_SLASH_SLOT_RE.search(label))


def _needs_date(label: str) -> bool:
    """这一栏是否要求"姓名 + 日期"两半都有（栏名自带日期字样）。"""
    return bool(_LABEL_DATE_RE.search(label))


def _split_name(value: str) -> str:
    """从值里剥出姓名候选；剥不出 ⇒ 空串。

    ⚠️ **单字也算姓名**（R119 实测 p2「车间负责人审核/日期：王2025.01.30」）：
    手写姓名被 OCR 只认到姓（"王"）是常态，此时格内**有**姓名痕迹，只是不全。
    按 2–4 字收会把"王"判成"没有姓名"，进而报"只填了日期没有签名" ——
    这是拿 OCR 残缺当记录缺陷，正是本仓库反复踩过的归因错误。
    宁可少报：只要有中文残留就认为姓名位**有内容**。
    """
    rest = _DATE_RE.sub(" ", value)
    for w in _STRIP_WORDS:
        rest = rest.replace(w, " ")
    rest = re.sub(r"[^\u4e00-\u9fa5]", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    if not rest or rest in _NOT_A_NAME:
        return ""
    # 取最长的一段 ≤4 字中文；更长的说明这是正文，不是姓名
    for seg in sorted(rest.split(" "), key=len, reverse=True):
        if 1 <= len(seg) <= 4:
            return seg
    return ""


def _cells(plain: str):
    """产出 (栏名, 值)。值取到下一个栏名冒号为止。"""
    marks = list(_LABEL_RE.finditer(plain))
    for i, m in enumerate(marks):
        label = m.group(1)
        # 「QC 批准/日期：」里的 `QC` 与栏名之间**有空格**，栏名正则跨不过去 ⇒
        # 栏名只剩"批准/日期"，于是"这是签名位"的证据（QA/QC 前缀）丢了。
        # 实测 p29/p30 正是这个写法，不补这一步会静默漏掉（p31/p32 无空格，能命中）。
        pre = plain[max(0, m.start() - 4):m.start()]
        m_pre = re.search(r"(?:QA|QC)\s*$", pre, re.IGNORECASE)
        if m_pre:
            label = m_pre.group(0).strip() + label
        if not _is_slot_label(label):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(plain)
        value = plain[m.end():end].strip(" ,，;；、|")
        yield label, value[:_MAX_VALUE_LEN]


def pairing_findings(page_no, raw_html: str | None) -> list[dict]:
    """单页「签名栏只填一半」的判据。

    返回 0–2 条（每类一条，带计数）：
      · 只填日期、没有签名姓名（格内有日期 ⇒ 这格确实被填过）
      · 栏名要求日期，却只有姓名没有日期

    两类都是 `completeness` / `warning`，与既有「签名栏为空」同一族：
    那一条说的是"格全空"，这两条说的是"格填了一半"。原文已签但 OCR 只认到
    日期时也会命中 ⇒ 文案带 OCR 归因后缀，不写"未签"。
    """
    plain = _plain(raw_html)
    if not plain:
        return []

    date_only: dict[str, list[str]] = {}
    name_only: dict[str, list[str]] = {}
    for label, value in _cells(plain):
        if not value:
            continue
        m_date = _DATE_RE.search(value)
        has_date = bool(m_date)
        name = _split_name(value)
        if has_date and not name:
            date_only.setdefault(label, []).append(
                (m_date.group(0) if m_date else value).strip()
            )
        elif name and not has_date and _needs_date(label):
            name_only.setdefault(label, []).append(name)

    out: list[dict] = []
    for label, dates in date_only.items():
        out.append({
            "page": page_no,
            "type": "completeness",
            # **info 而不是 warning**：TODO 0-63 已登记「PaddleOCR-VL 对连笔手写
            # 姓名系统性漏识别，日期/印刷体正常」—— 「有日期、没姓名」正是那个
            # 已知 OCR 缺口的形态，不是"记录方没签"的证据。按 warning 报等于把
            # OCR 缺陷记到记录员头上（本仓库反复踩的归因错误）。日期半是可靠的，
            # 所以反过来那一半（有姓名、没日期）才用 warning。
            "severity": "info",
            "description": (
                f"第{page_no}页 「{label}」栏只识别到日期（{dates[0]}），"
                f"没有签名姓名"
                + (f"，共 {len(dates)} 处" if len(dates) > 1 else "")
                + "（如原页已签，属 OCR 只识别到日期；见 TODO 0-63）"
            ),
            "ocr_text": f"label={label} date_only={len(dates)}",
            "operator": "",
            "source": "rule",
        })
    for label, names in name_only.items():
        out.append({
            "page": page_no,
            "type": "completeness",
            "severity": "warning",
            "description": (
                f"第{page_no}页 「{label}」栏要求签名与日期，"
                f"只识别到签名姓名（{names[0]}），没有填写日期"
                + (f"，共 {len(names)} 处" if len(names) > 1 else "")
            ),
            "ocr_text": f"label={label} name_only={len(names)}",
            "operator": "",
            "source": "rule",
        })
    return out
