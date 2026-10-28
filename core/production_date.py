"""`page_info.production_date` 只取**生产**语义（CR-24 / R109）。

问题（实测 `0c5cfb00` p1）：整页没有「生产日期」这一栏，只有
「执行日期：2022.06.01」（SOP 执行日期）与「产品批号：1127011N 25010…」。
LLM 把执行日期填进了 `production_date`，于是这一页的日期锚点变成 **2022.06.01**
—— 比真实生产日期早 2.5 年。下游所有"时间先后"判定都建在这个错锚点上。

用户 2026-10-22 已确认：**2022 年生效的空白记录用于 2025 年批次是合规的**，
所以不能靠"文件过期"去解释，只能把**字段**分清楚。

为什么不用 LLM 自己说清楚
-------------------------
prompt 里确实该写（本批也加了），但抽取是概率性的：同一页重跑两次，一次填
执行日期、一次留空，是常态。所以这里再做一次**确定性**的消歧 —— 页面原文里
有没有「生产日期」这个**标签**，是可以用正则问清楚的，不该交给模型判断。

三态（与 `signature_date_need` 同一风格：看不清就弃权）
------------------------------------------------------
| 情形 | 结果 | `source` |
|---|---|---|
| 原文有「（开始）生产日期」标签 + 日期 | 取**该日期** | `literal` |
| 原文没有该标签，且当前值出现在执行/生效/批准…标签之后 | **清空** | `cleared_nonproduction` |
| 原文没有该标签，当前值也不在那些标签旁 | 保留 | `model` |
| 当前值本来就是空的 | 保持空 | `absent` |

⚠️ **不修 OCR 读错的年份**。`0c5cfb00` p2/p3 原文写的就是「2015.01.20」
（批号却是 `1127011N250101` ⇒ 2025）。那属于识别错误，由 `suspicious_date`
报"相差 10 年"给复核员 —— 本模块**不得**悄悄把它改成 2025，那是编造。
"""
from __future__ import annotations

import re

_TAG_RE = re.compile(r"<[^>]+>")

#: 日期字面（点/横线/斜杠/中文年月日）。语料里四种都出现过。
_DATE = r"\d{4}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2}\s*日?"

#: 「生产日期」标签。`开始生产日期` 优先于裸 `生产日期`（p2 两者都有，
#: 取开始日期才与"生产从哪天开始"的语义一致）。
_PROD_LABEL_RE = re.compile(r"(?:开始\s*)?生产日期[^0-9]{0,12}(" + _DATE + r")")
_PROD_START_RE = re.compile(r"开始\s*生产日期[^0-9]{0,12}(" + _DATE + r")")

#: **非生产**语义的日期标签。当前值若出现在这些标签之后，说明 LLM 取错了栏。
#: 只列**确定**与生产无关的：文件执行/生效/批准/审核/发放/起草/修订/颁发/版本。
#: 故意**不含**「检验日期」「清场日期」—— 那些虽不是生产日期，但语料里没有
#: 把它们填进 production_date 的实例，多列只会增加误清空的机会。
_NONPROD_LABEL = (
    r"(?:执行日期|生效日期|执行|生效|批准|审核|发放|起草|修订|颁发|版本)"
)
_NONPROD_NEAR_RE = re.compile(_NONPROD_LABEL + r"[^0-9]{0,12}(" + _DATE + r")")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


_PARTS_RE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")


def _parts(s: str | None) -> tuple[int, int, int] | None:
    """取 ``(年, 月, 日)``；解析不出返回 ``None``。"""
    m = _PARTS_RE.search(str(s or ""))
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except ValueError:
        return None


def _same_date(a: str | None, b: str | None) -> bool:
    """两个日期字面是否**同一天**（忽略写法：`2025-01-20` == `2025年01月20日`）。

    为什么要这个：语料里同一页的 `production_date` 有 `2025-01-20`、
    `2025.01.20`、`2025年01月20日` 三种写法。若"取字面"时不比语义，
    会把 **80 页的日期写法**统统改一遍 —— 语义没变、diff 却铺满全库，
    复核员看到的日期格式也跟着变。**只在与原文不一致时才改。**
    """
    pa, pb = _parts(a), _parts(b)
    return pa is not None and pa == pb


def _plain(raw_html: str | None) -> str:
    """去标签但**保留标签间的一个空格** —— 否则 `开始生产日期</td><td>2015.01.20`
    会粘成 `开始生产日期2015.01.20`，虽然仍能匹配，但 `[^0-9]{0,12}` 的边界
    在不同排版下会漂移。"""
    return _norm(_TAG_RE.sub(" ", str(raw_html or "")))


def resolve_production_date(
    raw_html: str | None, current: str | None,
) -> tuple[str, str]:
    """返回 ``(production_date, source)``；``source`` 落 `page_info.production_date_source`。

    纯函数：不改入参，不做 IO。``source`` 取值见模块文档的四态。
    """
    cur = str(current or "").strip()
    text = _plain(raw_html)
    if not text:
        return cur, ("absent" if not cur else "model")

    # ① 原文有「开始生产日期」⇒ 以它为准（最贴"生产"语义）
    m = _PROD_START_RE.search(text) or _PROD_LABEL_RE.search(text)
    if m:
        literal = m.group(1).strip()
        if cur and _same_date(cur, literal):
            return cur, "literal"  # 同一天，只是写法不同 ⇒ 不动格式
        return literal, "literal"

    if not cur:
        return "", "absent"

    # ② 当前值出现在非生产标签之后 ⇒ 取错栏，清空（**不猜**它应该是哪天）
    cur_key = _norm(cur)
    for m2 in _NONPROD_NEAR_RE.finditer(text):
        if _norm(m2.group(1)) == cur_key:
            return "", "cleared_nonproduction"

    return cur, "model"


def apply_to_page(page: dict, raw_html: str | None) -> str:
    """就地修正一页的 ``page_info``，返回 ``source``（供日志/审计）。

    ``page`` 不是 dict 或没有 ``page_info`` 时返回 ``"absent"``，**不抛异常** ——
    抽取结果不可信，这里必须 fail-safe。
    """
    if not isinstance(page, dict):
        return "absent"
    info = page.get("page_info")
    if not isinstance(info, dict):
        return "absent"
    value, source = resolve_production_date(raw_html, info.get("production_date"))
    info["production_date"] = value
    info["production_date_source"] = source
    return source
