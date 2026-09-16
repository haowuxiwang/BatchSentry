"""全文档年份投票（R3 降噪）—— 用多数读法交叉约束单字符年份误读。

定位依据（真实 51 页轮次 `0c5cfb00-897`，规则层离线重放实测）
------------------------------------------------------------
`year_contradiction` 里形如 `[2015, 2025]` / `[2020, 2025]` / `[2018, 2025]`
的条目，少数年份与多数年份**恰好只差一位数字**，且页支持度远低于多数。
这不是"记录自相矛盾"，而是**同一次单字符误读在多页重复**（评审文档 §2.3
记录的 "2015 级联"）。更糟的是下游 R1-b/R5/R9a 用 `year_delta > 2` 把同一次
误读当"提取错误"再报一遍 —— 一条误读派生出十余条 finding（含 critical）。

做法（投票归一 + 交叉约束）
--------------------------
1. 统计**全文档**每个年份出现的页数 `support`（日期串 + `event_year_groups`）。
2. 批号里的 `YYMMDD` 段（`1127011N250101` → 2025）是**记录自带的独立佐证**：
   被批号佐证的年份永不被归一。
3. 归一判据（三条必须同时成立 —— 保守到不会吃掉真实的跨年差异）：
   - 与多数年份**恰好差一位数字**（单字符误读的形态）；
   - 多数年份本身出现在 ≥ `_MAJORITY_MIN_PAGES` 页（投票要有基数）；
   - 少数读法的页支持度 `< max(2, 多数页数 × _VARIANT_MAX_SHARE)`（投票要**决定性**）。
4. `resolve()` 返回 `(年份, 理由)`；理由非空 = 本次发生过一次归一，调用方据此
   **记录而非静默丢弃**（GMP 可追溯："为什么这条 finding 没了"）。

不做的事
--------
- **不改写任何原始文本**（不重写日期串）：只在规则**发射点**抑制由误读派生的结论，
  原始 OCR/LLM 产物与证据锚一字不动。
- 不做差 ≥2 位的合并 —— 真实换批号 / 换年份必须原样保留为 finding。
- 无 IO、无状态：同一 `pages` 两次调用结果必须一致（`test_year_vote.py` 锁定）。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

# 只看像年份的 4 位数，且**两侧都不是数字** —— 避免命中批号里的
# `1127011N250101`（`2501` 后面紧跟 `01`，被 (?!\d) 挡掉）。
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MIN_YEAR = 1990
_MAX_YEAR = 2100

# 批号里的 YYMMDD 段（两侧非数字）。
_BATCH_YYMMDD_RE = re.compile(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")

# 多数年份至少出现在这么多页，投票才有基数（小文档不归一，保守）。
_MAJORITY_MIN_PAGES = 3
# 少数读法的页支持度上限（占多数页数的比例）—— 超过即视为"投票不决定性"，不归一。
_VARIANT_MAX_SHARE = 0.35
# 绝对下限：少数读法在该页数及以上时，即便比例很低也不归一（小文档保护）。
_VARIANT_ABS_FLOOR = 2


def single_digit_variant(a: int, b: int) -> bool:
    """两个 4 位年份是否恰好只差一位数字（单字符误读的形态）。"""
    sa, sb = f"{a:04d}", f"{b:04d}"
    if len(sa) != len(sb):
        return False
    return sum(1 for x, y in zip(sa, sb) if x != y) == 1


def batch_year(batch_no: object) -> int | None:
    """从批号推断年份（`YYMMDD` 段）。无法推断返回 None。

    `YY` 的世纪归属：不大于"当年 + 1"的两位年视为 20YY，否则 19YY。
    例：今天是 2026 年 → `25` → 2025；`99` → 1999。
    """
    m = _BATCH_YYMMDD_RE.search(str(batch_no or ""))
    if not m:
        return None
    yy = int(m.group(1))
    cutoff = _dt.date.today().year % 100 + 1
    year = 2000 + yy if yy <= cutoff else 1900 + yy
    return year if _MIN_YEAR <= year <= _MAX_YEAR else None


def years_in_text(text: object) -> set[int]:
    """从一段文本里抽出所有像年份的 4 位数（带上下界约束）。"""
    out: set[int] = set()
    for m in _YEAR_RE.finditer(str(text or "")):
        y = int(m.group(1))
        if _MIN_YEAR <= y <= _MAX_YEAR:
            out.add(y)
    return out


def date_strings(page: dict) -> list[str]:
    """页面里参与"日期证据"的字符串。

    **必须与 `core/rules/rule_time._collect_all_date_strings` 口径一致** ——
    两者若漂移，投票看到的年份集合与规则实际比较的年份集合就会不同。
    `tests/unit/test_year_vote.py::test_collector_matches_rule_time` 机检该等价性。
    """
    out: list[str] = []
    pi = page.get("page_info") or {}
    if pi.get("production_date"):
        out.append(str(pi["production_date"]))
    for step in page.get("steps") or []:
        for k in ("start_time", "end_time"):
            if step.get(k):
                out.append(str(step[k]))
        for sig in step.get("signatures") or []:
            if sig.get("sign_time"):
                out.append(str(sig["sign_time"]))
    for years in (page.get("event_year_groups") or {}).values():
        for y in years or []:
            out.append(str(y))
    return out


@dataclass(frozen=True)
class YearVote:
    """一次"全文档年份投票"的结果（不可变，可安全共享）。"""

    support: dict[int, int]          # 年份 -> 出现的页数
    majority: int | None             # 票数最高的年份（并列时为 None，不归一）
    batch_years: tuple[int, ...]     # 批号佐证的年份（永不被归一）

    @property
    def pages_with_year(self) -> int:
        return sum(self.support.values())

    def resolve(self, year: int | None) -> tuple[int | None, str | None]:
        """把 `year` 按投票归一。

        归一必须**同时**满足下列全部条件（任一不满足即原样返回）：

        1. **有佐证**：多数年份被批号里的 `YYMMDD` 段佐证（强证据），
           或者该少数读法**只出现在 1 页**（孤页读法，弱证据但安全）；
        2. **不相邻**：与多数年份相差 >1 年 —— 相邻年很可能是真实的跨年记录
           （如 12-30→01-02），绝不能被"投票"吃掉；
        3. **单字符形态**：与多数年份恰好差一位数字；
        4. **有基数**：多数年份出现在 ≥ `_MAJORITY_MIN_PAGES` 页；
        5. **投票决定性**：少数读法页数 < `max(_VARIANT_ABS_FLOOR, 多数页数 × _VARIANT_MAX_SHARE)`。

        Returns:
            `(归一后年份, 理由)`。理由为 None = 未归一（原样返回）。
        """
        if year is None or self.majority is None or year == self.majority:
            return year, None
        if year in self.batch_years:
            # 批号自身编码了年份 → 该年份有独立证据，绝不被"投票"吞掉。
            return year, None

        support_major = self.support.get(self.majority, 0)
        support_minor = self.support.get(year, 0)

        # 1. 佐证闸：无批号佐证时只归一"孤页读法"。
        evidenced = self.majority in self.batch_years
        if not evidenced and support_minor > 1:
            return year, None
        # 2. 相邻年闸（跨年记录保护）。
        if abs(year - self.majority) <= 1:
            return year, None
        # 3. 单字符形态闸。
        if not single_digit_variant(year, self.majority):
            return year, None
        # 4. 基数闸。
        if support_major < _MAJORITY_MIN_PAGES:
            return year, None
        # 5. 决定性闸。
        threshold = max(float(_VARIANT_ABS_FLOOR), support_major * _VARIANT_MAX_SHARE)
        if support_minor >= threshold:
            return year, None

        why = "批号佐证" if evidenced else "孤页读法"
        return self.majority, (
            f"{year}→{self.majority}"
            f"（{support_minor} 页 vs {support_major} 页，单字符差异，{why}）"
        )


def build_year_vote(pages: list[dict]) -> YearVote:
    """统计全文档年份页支持度并给出多数年份（纯函数、确定性）。"""
    support: dict[int, int] = {}
    batch_years: set[int] = set()
    for page in pages or []:
        page_years: set[int] = set()
        for s in date_strings(page):
            page_years |= years_in_text(s)
        for y in page_years:
            support[y] = support.get(y, 0) + 1
        by = batch_year((page.get("page_info") or {}).get("batch_no"))
        if by is not None:
            batch_years.add(by)

    majority: int | None = None
    if support:
        ranked = sorted(support.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            majority = ranked[0][0]

    return YearVote(
        support=dict(sorted(support.items())),
        majority=majority,
        batch_years=tuple(sorted(batch_years)),
    )


def vote_report(vote: YearVote) -> dict:
    """投票结果的可审计摘要（落日志 / audit）。"""
    rows = []
    for y, n in sorted(vote.support.items(), key=lambda kv: (-kv[1], kv[0])):
        _, reason = vote.resolve(y)
        rows.append({"year": y, "pages": n, "normalized_to": vote.majority if reason else None,
                     "reason": reason})
    return {
        "majority": vote.majority,
        "batch_years": list(vote.batch_years),
        "years": rows,
        "normalized": [r for r in rows if r["reason"]],
    }
