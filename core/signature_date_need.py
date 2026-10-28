"""签名是否需要日期 —— 由**该栏表头文案**决定，不是由"签名就该带日期"的常识决定。

用户 2026-10-26 第 12 条裁定：表头写"签名"⇒ 不要求日期；表头写"签名和日期"
⇒ 两者都要有。这条裁定有实测依据 —— 真实表头只有两种写法，泾渭分明：

    「序号 | 岗位操作指导 | 操作参数 | 记录数据 | 签名」          104 张表
    「步骤 | 执行人签名和日期 | 检查人签名和日期」                 4 + 4 张表

前者签名栏里就是「操作者郑旭 复核者吴伟胜」，没有日期格可填。系统却按
"签名必须带日期"的常识报「所有签名均无日期，无法进行时间逻辑检查」
（`0c5cfb00` p14/p26/p40/p46/p48、`83a0219d` p40/p45/p46、`e50e9a7e` p21 等）
—— **表单没设这一栏，记录方无从填起**。

为什么放在守卫链而不是规则层
----------------------------
规则层拿到的是结构化 `signatures[]`，表头文案在结构化时就丢了。只有守卫链同时
握着 `raw_by_page`（与 `core/blank_column.py` 同一条理由）。

保守原则
--------
"表头只有签名列"是一个**需要正面证据**的结论：必须真的看到一张带签名列表头、
且该行**没有**任何日期字样。看不到表头、或表头里有日期字样 ⇒ 一律弃权（`None`），
**不抑制**。错抑制会把真缺陷（签名缺失、姓名识别不清）洗掉，比漏判严重。
"""
from __future__ import annotations

import re

from core.blank_column import _TABLE_RE, _grid, _looks_like_header

#: 签名类列名。`签批` 在语料里没出现过，列进来是因为它是同族词，
#: 多列只会让判据更保守（更容易判成"有签名列"），不会造成漏识别。
_SIG_WORDS: tuple[str, ...] = ("签名", "签字", "签批")

#: 日期类表头写法。**不含"时间"** —— 实测「水洗开始时间」这类列是工序时刻，
#: 与签名是否要写日期无关；把它算进来会让判据几乎永不触发（等于没修）。
_DATE_HEADER_RE = re.compile(r"日期|年月日|年\s*月\s*日|日\s*期")

#: 「签名缺日期」类断言的**分句**判据。分句很关键：实测文案常把
#: 「步骤6缺少复核者签名」与「步骤5签名无日期」写在同一段，跨句匹配会把
#: 前者一起吞掉。
_SENTENCE_RE = re.compile(r"[^。；;\n]+")

#: 日期缺失的措辞 —— **必须紧邻**，中间不许隔词。
#:
#: 这条限制是实测撞出来的：放宽成"日期…6 字内…缺失"会误命中两类**真缺陷**：
#:   「已记录操作**时间**但**缺少**操作者签名」（`0c5cfb00` p24/p38/p39 的规则层
#:     条目，说的是签名没签，不是日期没写）
#:   「签名字段…中**姓名缺失**，仅识别出**日期**」（`0c5cfb00` p12，说的是姓名没写）
#: 两类的共同点是"日期/时间"出现在**主语**位置而不是"缺失"的宾语。只有
#: 「缺少日期」「无日期」「日期缺失」这类**宾语形态**才是本判据要的。
_DATE_MISSING_RE = re.compile(
    r"(?:缺失|缺少|未填|未注|没有|无)(?:签名|签字|签批|签署)?(?:日期|时间)"
    r"|(?:日期|时间)[）)]?(?:信息|记录|部分)?(?:缺失|缺少|未填|未注|为空|不全|未记录)"
)

#: **日期之外**的缺陷措辞 —— 出现即说明这一句不止在说日期，不是"纯日期断言"。
#: 宁可多列（fail-safe：多列只会让抑制不发生）。
_OTHER_DEFECT_RE = re.compile(
    r"不完整|不准确|不清|识别不出|识别不清|粘连|姓氏|不符|不一致|矛盾|"
    r"N/?A|为空|空白|未签|漏签|缺失签名|无签名|涂改|划改|错位|异常"
)


def signature_date_required(raw_html: str | None) -> bool | None:
    """该页是否有"签名必须带日期"的表头。

    Returns:
        ``True``  —— 找到「签名和日期」这类表头，或签名列同行另有日期列；
        ``False`` —— 只找到「签名」列，同行无任何日期字样；
        ``None``  —— 看不到签名列表头（弃权，不抑制）。
    """
    verdict: bool | None = None
    for table in _TABLE_RE.findall(str(raw_html or "")):
        for row in _grid(table):
            if not _looks_like_header(row):
                continue
            sig_cells = [c for c in row if any(w in c for w in _SIG_WORDS)]
            if not sig_cells:
                continue
            # ① 同一格写「签名和日期」/「签名/日期」
            if any(_DATE_HEADER_RE.search(c) for c in sig_cells):
                return True
            # ② 签名列同行另有独立日期列
            if any(_DATE_HEADER_RE.search(c) for c in row if c not in sig_cells):
                return True
            verdict = False
    return verdict


def claims_missing_signature_date(description: str | None) -> bool:
    """文案是否在断言"签名缺日期"（分句判定，日期断言与其他缺陷分开看）。"""
    for sentence in _SENTENCE_RE.findall(str(description or "")):
        if not any(w in sentence for w in _SIG_WORDS):
            continue
        if _DATE_MISSING_RE.search(sentence):
            return True
    return False


def is_pure_date_claim(description: str | None) -> bool:
    """文案的**全部**缺陷断言是否只是"签名缺日期"。

    逐分句看：每个提到签名与日期缺失的分句，都不得再含其他缺陷措辞
    （不清/姓氏/不符/为空/N/A…）。只要有一句在说别的事 ⇒ 不是纯日期断言，
    **不抑制** —— 复合文案里的"签名缺失"是另一回事，不能连坐。
    """
    text = str(description or "")
    sentences = [s for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return False
    for sentence in sentences:
        if not any(w in sentence for w in _SIG_WORDS):
            return False
        if not _DATE_MISSING_RE.search(sentence):
            return False
        if _OTHER_DEFECT_RE.search(sentence):
            return False
    return True


def unfounded_reason(
    description: str | None, raw_html: str | None, source: str | None = None,
) -> str | None:
    """返回抑制理由；不该抑制时返回 ``None``（判据全在这里，调用方只判空）。

    ``source`` 只允许 LLM 三种（``llm_page`` / ``llm_cross`` / ``llm_fallback``）——
    与 R104/R105 同一条边界：**规则层与用户规则的判定不得被系统自己吞掉**。
    规则层是确定性判据，它的"缺签名"是数出来的，不是读出来的。
    """
    if source is not None and not str(source).startswith("llm_"):
        return None
    if signature_date_required(raw_html) is not False:
        return None  # True = 表头要求日期；None = 看不清 ⇒ 都不抑制
    if not claims_missing_signature_date(description):
        return None
    if not is_pure_date_claim(description):
        return None
    return (
        "该页签名栏表头只写「签名」、同行没有日期栏 ⇒ 表单本就不要求签名带日期，"
        "「所有签名均无日期」不构成记录缺陷（用户 2026-10-26 第 12 条裁定）"
    )
