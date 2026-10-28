from __future__ import annotations

import logging

import html
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers: spec / time parsing
# ---------------------------------------------------------------------------

@dataclass
class SpecBounds:
    op: str  # between | lt | le | gt | ge
    low: Optional[float] = None
    high: Optional[float] = None

# Match 2015.01.27 / 2015-01-27 / 2015/01/27 / 2015.1.7 (with optional HH:MM or HH:MM:SS).
# Year prefix (19|20) lets us detect suspiciously old years (e.g. 1990) so R4
# can flag them — restricting to 20xx would hide them.
_DATE_RE = re.compile(
    r"(?<!\d)"                                       # no leading digit (avoid matching inside long digit run)
    r"((?:19|20)\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})"  # year-mon-day
    r"(?:[\sT]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"      # optional HH:MM or HH:MM:SS (ISO T separator)
)
# Match Chinese date format: 2024年5月7日 / 2024年05月07日 (with optional HH:MM)
_CN_DATE_RE = re.compile(
    r"((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日"
    r"(?:\s*(\d{1,2})[时:](\d{2})分?)?"
)
# Match a standalone HH:MM or HH:MM:SS
_TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
# Match MM-DD HH:MM (no year — needs fallback_date for year)
# Used when high-quality scan has "07-17 14:30" without year prefix
_MD_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)
# 「仅时刻 + 中文单位」（B1-16）：`09 时 00 分` / `08时 09分` / `19 时 15 分`。
# 需 fallback_date 补年月日（与 `_TIME_ONLY_RE` 同契约）。
#
# ⚠️ 三条硬约束，改动前务必读完（B1-16 实测）：
#   ① **整串锚定**。同一语料里还有 `22108时26分`、`2109时31分`、
#      `15时36分008232-2412017` 这类"批号/日期与时刻粘连"的噪声 ——
#      锚定后它们**仍然判不可解析**（判不了 ≠ 判据确凿；宁可缺，不可错）。
#   ② **分钟必须两位**（与 `_TIME_ONLY_RE` / `_CN_DATE_RE` 同一约定）：
#      放成 `\d{1,2}` 会让 `13时6` 被读成 13:06，而一位分钟在 OCR 语料里
#      与"被截断的数字"不可区分（p6 的 `13:6 m^3` 正是该风险的真实样本）。
#   ③ 不认"时/分"以外的单位，也不接受 `~` / `-` 区间形态（`01:01 ~ 01:27`）。
_CN_TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2})\s*时\s*(\d{2})\s*分?\s*$")
# 「N日 + 时刻」：`21日 00 时36分` / `21日07时49分` / `20 日 17 时 29 分`。
# 月份/年份由 fallback_date 推定，故必须保守（见 `_DAY_WINDOW_DAYS`）。
_CN_DAY_TIME_RE = re.compile(
    r"^\s*(\d{1,2})\s*日\s*(\d{1,2})\s*时\s*(\d{2})\s*分?\s*$"
)
# `N日H时M分` 里"日"与 fallback 生产日期允许相差的天数。
#
# 为什么是 1：生产记录跨午夜是常态（同日或前后一天），此范围内**月份是确定的**。
# 再放宽就必须在"用本月还是相邻月"之间猜 —— 而 TODO B1-16 明确要求
# **不得猜月**，且实测 p15 的 `21日` 配 `production_date=1月27日`（差 6 天）、
# p46 的 `23日` 配一个伪造的当天日期（差 5 天）都属"输入里没有可用锚点"。
# ⇒ 超窗口一律返回 None：降级为"判不了"，不伪造结论。
_DAY_WINDOW_DAYS = 1
# Glued name+date like "庞明女署2027.01.17" — we already extract via _DATE_RE,
# but keep a fallback for cases without separator inside the date.
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# OCR串扰: "2022/4/202205.07" -> take the trailing "2022.05.07".
# Allow optional separator between MM and DD (e.g. ".07" instead of "07").
_OCR_NOISE_DATE_RE = re.compile(
    r"(20\d{2})[/.\-]?(\d{1,2})?[/.\-](20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})"
)


def _parse_time(s: Optional[str], fallback_date: Optional[str] = None) -> Optional[datetime]:
    """Parse a time string into datetime.

    Supported formats (in priority order):
      - "2015.01.27 14:30" / "2015-01-27 14:30" / "2015/1/7 9:05"
      - "2015.01.27 14:30:45" (with seconds)
      - "2015-01-27T14:30:00" (ISO 8601)
      - "2015.01.27"        / "2015-01-27"      / "2015/1/7"
      - "2024年5月7日" / "2024年05月07日 14时30分" (Chinese format)
      - "07-17 14:30"        -> uses fallback_date for year (no year prefix)
      - "11:04" / "11:04:30" -> uses fallback_date (e.g. page production_date)
      - "09 时 00 分" / "08时 09分" -> 中文单位时刻, uses fallback_date 的年月日
      - "21日07时49分" / "20 日 17 时 29 分" -> 日 + 中文单位时刻, uses fallback_date
        的年月, **仅当"日"与生产日期相差 <= `_DAY_WINDOW_DAYS` 天**才接受
      - "2022/4/202205.07"   -> OCR noise, clean to "2022.05.07"
      - "庞明女署2027.01.17"  -> extract via regex

    Returns None on parse failure (rule layer skips, does NOT raise).
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None

    # 1) Standalone HH:MM or HH:MM:SS
    m = _TIME_ONLY_RE.match(raw)
    if m and fallback_date:
        fb = _parse_time(fallback_date)
        if fb:
            ss = int(m.group(3)) if m.group(3) else 0
            # 对抗审查 P1-5：_TIME_ONLY_RE 允许 "99:99" 类越界值（OCR 手写
            # 时间噪声高频出现），datetime.replace(hour=99) 抛 ValueError —
            # 本分支是全部 5 个分支中唯一漏掉 try/except 的，异常会传播出
            # 规则层直接炸掉整个 Stage 3（job error 而非 partial_review）。
            # docstring 承诺 "parse failure → None, does NOT raise"。
            try:
                return fb.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=ss)
            except ValueError:
                return None
        return None
    if m and not fallback_date:
        return None

    # 1b) MM-DD HH:MM (no year — needs fallback_date)
    m = _MD_TIME_RE.match(raw)
    if m and fallback_date:
        fb = _parse_time(fallback_date)
        if fb:
            try:
                ss = int(m.group(5)) if m.group(5) else 0
                return fb.replace(month=int(m.group(1)), day=int(m.group(2)),
                                  hour=int(m.group(3)), minute=int(m.group(4)), second=ss)
            except ValueError:
                return None
        return None

    # 1c) 「N日 + 时刻」`21日07时49分` / `20 日 17 时 29 分`（B1-16）
    #     年月靠 fallback 生产日期推定 ⇒ 只在"日"与生产日期足够近时才接受，
    #     超出窗口返回 None（不猜月）。阈值依据见 `_DAY_WINDOW_DAYS` 注释。
    m = _CN_DAY_TIME_RE.match(raw)
    if m:
        fb = _parse_time(fallback_date) if fallback_date else None
        if not fb:
            return None
        try:
            cand = fb.replace(day=int(m.group(1)), hour=int(m.group(2)),
                              minute=int(m.group(3)), second=0)
        except ValueError:
            return None
        if abs((cand.date() - fb.date()).days) > _DAY_WINDOW_DAYS:
            return None
        return cand

    # 1d) 「仅时刻 + 中文单位」`09 时 00 分` / `08时 09分`（B1-16）
    #     与 `_TIME_ONLY_RE` 同契约：整个年月日都取自 fallback 生产日期。
    m = _CN_TIME_ONLY_RE.match(raw)
    if m:
        fb = _parse_time(fallback_date) if fallback_date else None
        if not fb:
            return None
        try:
            return fb.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        except ValueError:
            return None

    # 2) OCR noise pattern "2022/4/202205.07"
    m = _OCR_NOISE_DATE_RE.search(raw)
    if m:
        year = m.group(3)
        mon = m.group(4)
        day = m.group(5)
        try:
            return datetime(int(year), int(mon), int(day))
        except ValueError:
            pass

    # 3) Chinese date format "2024年5月7日" / "2024年05月07日 14时30分"
    m = _CN_DATE_RE.search(raw)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            return datetime(year, mon, day, hh, mm)
        except ValueError:
            return None

    # 4) Normal date / datetime (YYYY.MM.DD / YYYY-MM-DD / YYYY/MM/DD, optional HH:MM:SS)
    m = _DATE_RE.search(raw)
    if m:
        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        try:
            return datetime(year, mon, day, hh, mm, ss)
        except ValueError:
            return None

    # 5) Bare year "2022"
    m = _YEAR_RE.search(raw)
    if m:
        try:
            return datetime(int(m.group(1)), 1, 1)
        except ValueError:
            return None

    return None


def _extract_year(s: Optional[str]) -> Optional[int]:
    """Extract just the year from a date-ish string."""
    dt = _parse_time(s)
    if dt:
        return dt.year
    m = _YEAR_RE.search(str(s or ""))
    if m:
        return int(m.group(1))
    return None


# Match whether a time string carries an explicit time-of-day part
# ("14:30", "9:05", "14时30分"). Used to distinguish point-in-time values
# from date-only values (which span the whole day).
#
# ⚠️ B1-16：分隔符两侧**必须容忍空白**。实测本语料大量时刻写作 `09 时 00 分` /
# `08时 09分`（时/分两侧带空格）—— 旧式 `\d{1,2}[:时]\d{2}` 认不出它们，
# 于是**同一种形态只因空格**被判成两种精度：`21日07时49分` 是精确点，
# `09 时 00 分` 却成了"整天"区间 ⇒ 区间被放宽 ⇒ `_interval_after` 更难成立
# ⇒ 时序倒序被**系统性漏报**（判据与解析器不同源）。
_TIME_OF_DAY_RE = re.compile(r"\d{1,2}\s*[:时]\s*\d{2}")


# ---------------------------------------------------------------------------
# Batch number normalization — collapses OCR variants of the same batch no
# (separator/whitespace noise, full-width glyphs, digit confusions like
# "25010i" → "250101"). R7 groups by the normalized value so a 15-way
# "batch_inconsistency" spam collapses back to the real distinct batches.
# ---------------------------------------------------------------------------

_FULLWIDTH_MAP = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabc",
)
# OCR digit confusions seen in real records: i/I/l read as 1, o/O as 0.
# Letters with real meaning (N, X, B, 8) are NOT mapped — a genuine
# cross-batch difference must not be silently merged.
_OCR_DIGIT_MAP = str.maketrans({"i": "1", "I": "1", "l": "1", "o": "0", "O": "0"})
_BATCH_SEP_RE = re.compile(r"[\s\u3000·°.,;]+")
# 符号噪声：OCR 常把上标/标点混进批号（实测 `1127011N^*250101`、`11270111/250101`）。
# 批号的有效内容只可能是字母数字 —— 故剥离**除 `-` 之外**的全部非字母数字，
# `-` 保留给工序后缀（`-02`…`-06`）切分用（调用方 split("-")[0]）。
_BATCH_SYMBOL_RE = re.compile(r"[^0-9A-Za-z\-]+")


def _normalize_batch_no(raw: str) -> str:
    """Normalize a batch number string: strip separators, map full-width
    glyphs and OCR digit confusions, uppercase. Returns "" when empty."""
    s = html.unescape(str(raw or "")).strip()
    if not s:
        return ""
    s = _BATCH_SEP_RE.sub("", s)
    s = s.translate(_FULLWIDTH_MAP)
    s = s.translate(_OCR_DIGIT_MAP)
    s = _BATCH_SYMBOL_RE.sub("", s)
    return s.upper()


def _edit_distance_le1(a: str, b: str) -> bool:
    """两个串的编辑距离是否 ≤1（纯函数，无依赖）。

    R2 投票归一用它判定"单字符近邻变体"：真实混批的批号差异通常 ≥2 位，
    而 OCR 误读（`N`↔`1`、丢一位）只差 1 位。**长度差 >1 直接否决**。
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    short, long_ = (a, b) if la < lb else (b, a)
    i = j = 0
    diffs = 0
    while i < len(short) and j < len(long_):
        if short[i] != long_[j]:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


# ---------------------------------------------------------------------------
# Batch-number **identity** — `<product prefix><YYMMDD>` decomposition.
#
# 现场批号普遍形如 `<产品/工序前缀><YYMMDD>`（实测 `1127011N250101`、
# `1071011N260501`、`B2024001`）。把归一化串拆成 (前缀, 日期段) 后，日期段成了
# **独立佐证**：两串日期段不同（实测 p50 `241203` vs 其余页 `250101`）就不可能是
# 同一批号；反过来日期段相同而前缀只差一个字符（`N`↔`1`）则几乎必然是 OCR 误读。
#
# 这解决了旧实现的**顺序依赖**缺陷：旧代码把核心串按长度降序、用"前缀包含"互相
# 吸收，于是噪声最重的那串（`1127011N4250101`，`^4` 角标被读成数字）成了 host，
# 把 14 页干净的截断读法（`112701`）挂在它名下 —— 组标签本身就是噪声。
# ---------------------------------------------------------------------------

_BATCH_DATE6_LEN = 6
# 前缀短于此长度就不做"前缀包含"匹配，避免 `1127` 之类残串吞掉一切。
_BATCH_MIN_PREFIX_LEN = 6
_BATCH_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")


def parse_batch_key(base: str) -> tuple[str, Optional[str]]:
    """把归一化批号 ``base`` 拆成 ``(prefix, date6)``。

    ``date6`` 取**末尾 6 位连续数字**且能解释为合法 YYMMDD（月 1–12、日 1–31）
    时的那 6 位，否则为 ``None``（此时 ``prefix == base``）。

    先扫出末尾**整段**数字再取后 6 位，因为 OCR 会把角标/批注数字粘进批号
    （实测 `1127011N^4 250101` 归一成 `1127011N4250101`，末尾数字段是 `4250101`，
    后 6 位才是日期段 `250101`）。
    """
    base = str(base or "")
    m = _BATCH_TRAILING_DIGITS_RE.search(base)
    if not m:
        return base, None
    digits = m.group(1)
    if len(digits) < _BATCH_DATE6_LEN:
        return base, None
    tail = digits[-_BATCH_DATE6_LEN:]
    try:
        month, day = int(tail[2:4]), int(tail[4:6])
    except ValueError:  # pragma: no cover - 正则已保证是数字
        return base, None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return base, None
    return base[: len(base) - _BATCH_DATE6_LEN], tail


def _batch_prefixes_compatible(
    a: str, b: str, *, allow_edit_distance: bool
) -> bool:
    """两侧**都有日期段**时，前缀是否可能是同一产品的不同读法。

    ``allow_edit_distance=False`` 只认**结构关系**（相等 / 一方是另一方前缀），
    用在"证据可判定"的那一档；``True`` 额外认单字符近邻（`N`↔`1`），属**弱证据**，
    调用方必须再加投票闸。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith(b) or b.startswith(a):
        return min(len(a), len(b)) >= _BATCH_MIN_PREFIX_LEN
    if not allow_edit_distance:
        return False
    if len(a) < _BATCH_MIN_PREFIX_LEN or len(b) < _BATCH_MIN_PREFIX_LEN:
        return False
    return _edit_distance_le1(a, b)


def batch_keys_compatible(
    a: str, b: str, *, allow_edit_distance: bool = True
) -> bool:
    """两串**归一化**批号是否可能是同一批号的不同读法（对称、无顺序依赖）。

    1. **双方都有日期段**：日期段必须**完全相等**（最强独立佐证 —— 两串日期段
       不同，如实测 p50 的 `241203` vs 其余页 `250101`，就绝不可能是同一批号），
       且前缀相等 / 一方是另一方前缀 /（仅当 ``allow_edit_distance``）编辑距离 ≤1。
    2. **一方无日期段**（LLM/OCR 丢了尾段，实测 14 页只读出 `112701`）：无日期
       一方必须是**有日期一方前缀的前缀**（或与之相等）。反向不成立 —— 残串不能
       反过来吞掉完整串。
    3. **双方都无日期段**：只认前缀包含关系，**从不认编辑距离** —— 否则
       `B202201`/`B202202` 这种"真实混批、各 1 页"会被静默并掉。
    """
    pa, da = parse_batch_key(a)
    pb, db = parse_batch_key(b)
    if da and db:
        return da == db and _batch_prefixes_compatible(
            pa, pb, allow_edit_distance=allow_edit_distance
        )
    if da or db:
        dated_prefix, bare = (pa, pb) if da else (pb, pa)
        if len(bare) < _BATCH_MIN_PREFIX_LEN:
            return False
        return dated_prefix.startswith(bare)
    if len(pa) < _BATCH_MIN_PREFIX_LEN or len(pb) < _BATCH_MIN_PREFIX_LEN:
        return pa == pb
    return pa == pb or pa.startswith(pb) or pb.startswith(pa)


def _parse_time_interval(
    s: Optional[str], fallback_date: Optional[str] = None
) -> Optional[tuple[datetime, datetime, bool]]:
    """Parse a time string into a (start, end, precise) interval.

    date-only strings ("2024-01-01") span the whole day:
    (00:00:00, next-day 00:00:00), precise=False — the true moment is
    somewhere in that day. Strings with an explicit time of day
    ("2024-01-01 14:30") collapse to a point (t, t), precise=True.

    Rule layer must compare intervals, not raw datetimes: comparing a
    date-only value (parsed as 00:00) against a datetime on the same day
    produces false time_reversal/signature_time_anomaly findings (a
    date-only "2024-01-01" would falsely look earlier than "2024-01-01 14:30").

    Returns None on parse failure (same contract as _parse_time).
    """
    dt = _parse_time(s, fallback_date)
    if dt is None:
        return None
    if _TIME_OF_DAY_RE.search(str(s or "")):
        return (dt, dt, True)
    return (dt, dt + timedelta(days=1), False)


def _interval_after(
    a: tuple[datetime, datetime, bool], b: tuple[datetime, datetime, bool]
) -> bool:
    """True when interval A lies entirely after interval B (start-side after end-side).

    Comparison semantics for date-only values (a[2] or b[2] is False) use the
    open interval bound: e.g. start="2024-01-02" (whole day) vs
    end="2024-01-01 14:30" is a real reversal, while start="2024-01-01 14:30"
    vs end="2024-01-01" (whole day) is NOT (they may coincide that day).
    """
    a_start, _a_end, a_prec = a
    b_start, b_end, b_prec = b
    if a_start > b_end:
        return True
    if a_start == b_end and not b_prec:
        return True
    return False


def _interval_before(
    a: tuple[datetime, datetime, bool], b: tuple[datetime, datetime, bool]
) -> bool:
    """True when interval A lies entirely before interval B (end-side before start-side).

    Mirror of _interval_after: a date-only A (open end bound) that touches
    B's start still counts as entirely before (e.g. whole-day "2024-01-01"
    vs point "2024-01-02 00:00"); two precise equal points do not.
    """
    a_start, a_end, a_prec = a
    b_start, _b_end, _b_prec = b
    if a_end < b_start:
        return True
    if a_end == b_start and not a_prec:
        return True
    return False


# ---------------------------------------------------------------------------
# 页级日期锚点 / 跨零点判定（R99：CR-5、CR-6）
# ---------------------------------------------------------------------------

def literal_has_date(lit) -> bool:
    """时刻字面量是否**自带**日期成分（年-月-日 或 月-日）。

    `_parse_time` 对"只有时刻"的输入（`11:04`）以及"无年份"的输入
    （`07-17 14:30`）都会用 `fallback_date` 补全。补出来的日期**不是记录里
    写的事实** ⇒ 跨日/跨页判序不成立。
    """
    if not lit or not isinstance(lit, str):
        return False
    return bool(
        _DATE_RE.search(lit)
        or _CN_DATE_RE.search(lit)
        or _MD_TIME_RE.search(lit)
    )


def _date_key(value) -> Optional[str]:
    """把日期/时刻字面量归一成 `YYYY-MM-DD`；取不到 ⇒ None（不猜）。"""
    if not value:
        return None
    text = value if isinstance(value, str) else str(value)
    dt = _parse_time(text)
    return dt.strftime("%Y-%m-%d") if dt else None


def page_anchor_date(page: dict) -> Optional[str]:
    """页级**日期锚点**：`production_date`，缺失时回落到该页**签名日期众数**。

    CR-5（R99）实测：10 页语料的 `production_date` **10/10 页全空**，于是
    `_parse_time("12:00", "")` 一律返回 None ⇒ 时序检查**静默全线失效**，
    却把正常的 `12:00`/`14:00` 报成"时间格式无法解析"（4 条）。

    签名栏上的日期是**记录里写的事实**，比"没有锚点"强，故作为兜底：
    取该页 `steps[].signatures[].sign_time` 里出现次数最多的日期；
    票数相同取**先出现**的那个（按阅读顺序，可复现，不做随机选择）。

    ⚠️ 签名日期 ≠ 作业日期（可能是次日复核签署）。调用方必须把"日期来自
    推定"写进证据注、且**不得**据此给 critical —— 见
    `rule_time._evidence_notes`。

    仍取不到 ⇒ None。调用方**必须**如实说"无法核对"，不得说"格式错"。
    """
    info = page.get("page_info") or {}
    pd = info.get("production_date")
    if isinstance(pd, str) and pd.strip():
        return pd.strip()
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for step in page.get("steps") or []:
        for sig in step.get("signatures") or []:
            if not isinstance(sig, dict):
                continue
            key = _date_key(sig.get("sign_time"))
            if not key:
                continue
            if key not in counts:
                first_seen[key] = len(first_seen)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: (counts[k], -first_seen[k]))


#: "跨零点"允许的最大**隐含时长**（小时）。
#:
#: 跨零点的解释力有上限：它说的是"这条记录一直做到过了午夜"。若隐含时长
#: 逼近 24 小时，这个解释就退化成"第二天的同一钟点"——对**相邻**记录而言
#: 不是正常形态，此时原来的"倒序"判定应当保留。
#: 实测标定（真实语料）：p24 `14:12→08:18` 隐含 **18.1h**（浓缩过夜，真跨零点）；
#: p11 `23:04→00:04` 1.0h；p33 `20:21→00:17` 3.9h；而 `09:10→08:50`（同一天
#: 参数矩阵倒退）隐含 **23.7h** ⇒ 必须仍报。20h 把 18.1 与 23.7 分开。
_MAX_MIDNIGHT_WRAP_HOURS = 20.0


def crosses_midnight(prev_raw, curr_raw, prev_dt, curr_dt) -> bool:
    """两个**都不带日期**的时刻若 curr < prev，按**跨零点**处理，而不是"倒序"。

    CR-6（R99）：批记录里 `14:12 → 08:18`、`23:04 → 00:04` 是**跨日生产**的
    正常写法（含参数矩阵跨午夜继续取样）。缺日期时，"同一天倒序"与"跨零点"
    在结构化数据上**无法区分** ⇒ 按跨零点处理，不报。

    任一侧自带日期 ⇒ 事实可判，不走本分支（真正的跨日倒序仍会报）。
    ⚠️ 隐含时长 > `_MAX_MIDNIGHT_WRAP_HOURS` ⇒ 跨零点的解释力不够，仍按倒序处理
    （否则"同日倒退 20 分钟"会被当成"跨了 23 小时 40 分"，等于把参数矩阵的
    行序错误全部静默掉 —— 实测 `09:10 → 08:50` 就是这种形态）。
    """
    if prev_dt is None or curr_dt is None:
        return False
    if literal_has_date(prev_raw) or literal_has_date(curr_raw):
        return False
    delta_hours = (curr_dt - prev_dt).total_seconds() / 3600.0
    # 只有"往回不到一天"的两个时刻才谈得上跨零点。差值已超过 24 小时 ⇒ 两者
    # 本就不在同一天（锚点不同），"跨零点"无从谈起，交给调用方的锚点闸门。
    # ⚠️ 少了这一条，(curr−prev)+24 会变成**负数**，`<= 20` 恒真 ⇒ 任何
    # 跨页倒退都被当成跨零点（R99 变异 M11 实测暴露）。
    if delta_hours <= -24.0 or delta_hours >= 0.0:
        return False
    wrap_hours = delta_hours + 24.0
    return wrap_hours <= _MAX_MIDNIGHT_WRAP_HOURS


def _parse_number(s: Optional[str]) -> Optional[float]:
    """Extract the first number from a string, handle percentages and units.

    Examples: "0.974" -> 0.974 ; "53.6%" -> 53.6 ; "<0.3" -> 0.3 ; "17次" -> 17
    Supports power notation: "10^3" -> 1000, "10⁵" -> 100000
    """
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    text = str(s).strip()
    # Strip $ and {{ }} delimiters but keep content (e.g. "$0.974$" -> "0.974")
    text = re.sub(r"\$+", "", text)
    text = text.replace("{{", "").replace("}}", "")
    text = html.unescape(text)
    # Power notation: "10^3" / "10³" / "10⁵" (unicode super) -> 10**exp
    text = _expand_power_notation(text)
    m = re.search(r"-?\d+\.?\d*", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# Unicode superscript digits -> normal digits (for 10⁵ etc.)
_SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _expand_power_notation(text: str) -> str:
    """Expand scientific notation to literal numeric values.

    Supports:
    - "10^3" / "10³" → "1000"
    - "1.5×10^3" / "1.5*10^3" → "1500"
    - "1.5e3" / "1.5E3" → "1500"
    - "10**3" → "1000"

    Only expands when the base is 10 (common in pharma specs like ≤10^3 cfu/g).
    Other bases (e.g. 2^5) are left untouched to avoid surprising semantics.
    """
    # Step 1: Expand "10**3" / "10^3" / "10³" to literal (base must be 10)
    def repl_caret(m):
        base = float(m.group(1))
        exp = int(m.group(2))
        if base == 10:
            return str(int(base ** exp))
        return m.group(0)
    text = re.sub(r"\b(10)\s*(?:\*\*|\^)+\s*(\d+)", repl_caret, text)
    # 10⁵ (unicode superscript, no caret) — must run before generic digit
    # translate to avoid "10³" collapsing to "103" instead of "1000".
    text = re.sub(r"\b(10)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",
                  lambda m: str(10 ** int(m.group(2).translate(_SUPERSCRIPT_MAP))), text)

    # Step 2: Expand "系数×10^指数" or "系数*10^指数" patterns.
    # Matches "1.5×1000" or "1.5*1000" (after step 1 expanded 10^3 to 1000).
    def repl_sci(m):
        coeff = float(m.group(1))
        base = float(m.group(2))
        # Only handle if base is a power of 10 (10, 100, 1000, 10000, ...)
        if base != int(base) or base <= 0:
            return m.group(0)
        exp = math.log10(base)
        if exp != int(exp):
            return m.group(0)
        result = coeff * base
        # Return as int if whole number, else float
        return str(int(result)) if result == int(result) else str(result)
    text = re.sub(r"(\d+\.?\d*)\s*[×*]\s*(\d+\.?\d*)", repl_sci, text)

    # Step 3: Expand "1.5e3" / "1.5E3" notation
    def repl_e(m):
        mantissa = float(m.group(1))
        exp = int(m.group(2))
        result = mantissa * (10 ** exp)
        return str(int(result)) if result == int(result) else str(result)
    text = re.sub(r"(\d+\.?\d*)[eE]([-+]?\d+)", repl_e, text)

    return text


# ---------------------------------------------------------------------------
# spec_range 书写变体归一化（M8-spike：真实落库 spec_range 实测定位）
#
# 三类写法与既有语义完全等价，只是"同一规格的不同书写"，此前无法解析 →
# 整条规格被降级为 spec_unverifiable（送人工复核 = 噪音）。归一化只改形状、
# 不改判定语义，因此不会软化真实的超差判定。
#
#   1. LaTeX ± 变体：PaddleOCR-VL 把印版「±」读成 `\pm`（51 页真实输出出现
#      16 次 / 分布 12 页），外层还可能裹 `$`：`温度(15 \pm 5°C)`。
#      LLM 目前多数会归一成 ±，但规则层是判定权威，必须自己读得懂 ——
#      否则一旦 LLM 原样回填 `15 \pm 5` 就静默丢判定能力。
#   2. 全角 / Unicode 变体：`～`(U+FF5E 全角波浪)、`－`(全角连字符)、
#      `−`(U+2212 数学减号)、`—`(em dash)。真实库有 `99%～101%`（1 处）
#      因全角波浪线解析失败，而同语义的 `99%~101%` 有 15 处成功。
#   3. 单位夹在数字与分隔符之间：`972 μg/mg ~1020 μg/mg`（含量/效价规格，
#      真实库 20 处，全部解析失败）。两侧单位一致时折叠掉中间那个单位，
#      **串尾单位必须保留** —— `_try_unit_normalize` 依赖 spec 串里的单位
#      做实测值单位换算，丢了单位会让比较本身出错。
# ---------------------------------------------------------------------------
# 外层包裹括号："(1300~3200)" -> "1300~3200"（真实库 1 处）
_PAREN_WRAP_RE = re.compile(r"^[（(\[【]\s*(.+?)\s*[）)\]】]$")
# `\pm` / `\mp` / `+/-` / `+-` / `+−` -> ±
_PM_RE = re.compile(r"\\pm|\\mp|\+/-|\+-|\+\s*−")
_FULLWIDTH_SPEC_TRANS = str.maketrans({
    "～": "~", "％": "%", "－": "-", "−": "-", "—": "-", "﹣": "-", "＋": "+",
})
# 两侧同单位夹分隔符：组1=左数，组2=单位，组3=右数（组2 须与串尾单位逐字一致）
_UNIT_BETWEEN_RE = re.compile(
    r"^(-?\d+\.?\d*)\s*([^\d\s\-~–—−～]+)\s*[-~–—−～]\s*(-?\d+\.?\d*)\s*\2\s*$"
)


def _normalize_spec_notation(s: str) -> str:
    """归一化 spec_range 的书写变体（见上方说明；不改变判定语义）。"""
    m = _PAREN_WRAP_RE.match(s.strip())
    if m:
        s = m.group(1).strip()
    s = s.translate(_FULLWIDTH_SPEC_TRANS)
    s = _PM_RE.sub("±", s)
    m = _UNIT_BETWEEN_RE.match(s)
    if m:
        s = f"{m.group(1)}~{m.group(3)}{m.group(2)}"
    return s


def _parse_spec(spec: Optional[str]) -> Optional[SpecBounds]:
    """Parse a spec_range string into SpecBounds.

    Returns None when the spec is non-numeric / free-text (e.g. "应澄清",
    "符合要求"), which means the rule layer cannot judge and the param goes
    to LLM fallback.

    Preprocessing:
      - html.unescape: &lt; -> <, &le; -> ≤, &gt; -> >, &ge; -> ≥
      - strip $...$ LaTeX残片
      - strip {{...}} template residue
      - 药典简写 NMT/NLT -> </>=
      - _normalize_spec_notation: LaTeX ± / 全角符号 / 单位夹分隔符（见上）
    """
    if not spec or not isinstance(spec, str):
        return None
    s = spec.strip()
    if not s:
        return None
    s = html.unescape(s)
    # Strip $...$ LaTeX delimiters but keep content (e.g. "$0.5-1.0$" -> "0.5-1.0")
    s = re.sub(r"\$+", "", s)
    # Strip {{...}} markers but keep content (e.g. "{{0.5-1.0}}" -> "0.5-1.0")
    s = s.replace("{{", "").replace("}}", "")
    # 书写变体归一化（LaTeX ± / 全角符号 / 单位夹分隔符）——须在 ≤/≥ 归一
    # 之前执行：± 分支依赖字符串里保留的「±」字形来区分「±」与「≤」。
    s = _normalize_spec_notation(s)
    s = s.replace("NMT", "<=").replace("NLT", ">=")
    s = s.replace("≤", "<=").replace("≥", ">=")
    # Expand power notation BEFORE numeric matching: "≤10^3cfu/g" -> "<=1000cfu/g"
    s = _expand_power_notation(s)
    s = s.strip(" .,;")

    # Numeric patterns are matched at start of string; trailing unit suffix
    # (e.g. " m^{3}/h", "MPa", "%") is allowed and ignored. Without this,
    # "0.5-1.0 $m^{3}/h$" would fail to match the between pattern.

    # between: "0.5-1.0" / "1300~3200" / "0.5–1.0" (en-dash)
    # 也支持带单位/百分号的写法："99%~101%"（首个界限后可选 %/％）——
    # 真实批记录中物料平衡率/收率常写成 "99%~101%"，此前因 "%" 夹在 99 与 ~
    # 之间而解析失败，整条规格被降级为 LLM 兜底（M4 实测发现）。
    m = re.match(r"^(-?\d+\.?\d*)\s*[%％]?\s*[-~–]\s*(-?\d+\.?\d*)", s)
    if m:
        low, high = float(m.group(1)), float(m.group(2))
        # OCR 常把「±」认成「-」：印版 "15±5℃" 被读成 "15-5"。左界大于右界的
        # 区间在数学上无意义，唯一合理解释是 A±B 形式，按 [A-B, A+B] 重建
        # （否则 between(15,5) 恒为假 → 该列每个值都被误报超差；M8 真实 51 页
        # 实测 p08 因此产生约 30 条误报）。公差取绝对值，避免 OCR 连负号一起读错。
        if low > high:
            tol = abs(high)
            low, high = low - tol, low + tol
        return SpecBounds(op="between", low=low, high=high)

    # ± 形式（印刷体）："40±3" / "15±5℃" / "7.5±0.2" -> [center-tol, center+tol]
    # 真实批记录大量使用「标称值±公差」写法，此前完全不支持，整条规格被降级
    # 为 LLM 兜底（而 LLM 不做可判性校验，见 M8 审计）。
    m = re.match(r"^(-?\d+\.?\d*)\s*[%％]?\s*±\s*(-?\d+\.?\d*)", s)
    if m:
        c, t = float(m.group(1)), abs(float(m.group(2)))
        return SpecBounds(op="between", low=c - t, high=c + t)

    # 空格分隔的 A B 形式：OCR/版式把「±」整体丢成空格（M8 真实 p14 原文
    # "浓缩液在 40 3°C 保温"、"用盐酸调缓冲液 pH 至 7.5 0.2"，印版应为 40±3 /
    # 7.5±0.2）。仅当第二数明显小于第一数（公差 < 中心值）才按 A±B 解释，
    # 避免把真正的 "10 20" 区间误读为 10±20；不满足则返回 None → 交人工复核。
    m = re.match(r"^(-?\d+\.?\d*)\s+(-?\d+\.?\d*)", s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if abs(b) < abs(a):
            return SpecBounds(op="between", low=a - abs(b), high=a + abs(b))

    # <0.3 / <=0.3 / < 0.3 / <=0.3MPa / <=1000cfu/g (after power expand)
    m = re.match(r"^<(=)?\s*(-?\d+\.?\d*)", s)
    if m:
        high = float(m.group(2))
        op = "le" if m.group(1) == "=" else "lt"
        return SpecBounds(op=op, low=None, high=high)

    # >5 / >=30 / ≥30% / >=30%
    m = re.match(r"^>(=)?\s*(-?\d+\.?\d*)", s)
    if m:
        low = float(m.group(2))
        op = "ge" if m.group(1) == "=" else "gt"
        return SpecBounds(op=op, low=low, high=None)

    # Chinese: 不超过100 / 不少于30 / 不超过10^3 (already expanded)
    m = re.match(r"^不超过\s*(-?\d+\.?\d*)", s)
    if m:
        return SpecBounds(op="le", low=None, high=float(m.group(1)))
    m = re.match(r"^不少于\s*(-?\d+\.?\d*)", s)
    if m:
        return SpecBounds(op="ge", low=float(m.group(1)), high=None)

    # ── CR-35（R100）：括号写法（真实 10 页语料 8 处）────────────────────
    # 两种形态此前**整条解析失败** ⇒ 这些监控值从未被判过（不是误报，是
    # 静默不判）：
    #   ① "(18-26)°C"        —— 括号只包住区间本体，单位在括号外；
    #   ② "(洁净走廊→过筛室) ≥5Pa" —— 括号内是方位说明（无数值），规格在后。
    # 只在本函数**本会返回 None** 的位置尝试，因此任何原本可解析的写法行为
    # 逐字不变（新增能力，不改既有语义）。
    m = re.match(r"^[（(\[【]\s*(-?\d+\.?\d*(?:\s*[-~–—−]\s*-?\d+\.?\d*)?)\s*[）)\]】]", s)
    if m:
        inner = _parse_spec(m.group(1))
        if inner is not None:
            return inner
    m = re.match(r"^[（(\[【][^）)\]】]*[）)\]】]\s*(\S.*)$", s)
    if m:
        rest = _parse_spec(m.group(1).strip())
        if rest is not None:
            return rest

    return None


def _judge(bounds: SpecBounds, actual: float) -> bool:
    if bounds.op == "between":
        return bounds.low <= actual <= bounds.high
    if bounds.op == "lt":
        return actual < bounds.high
    if bounds.op == "le":
        return actual <= bounds.high
    if bounds.op == "gt":
        return actual > bounds.low
    if bounds.op == "ge":
        return actual >= bounds.low
    # Fail-closed: unknown ops are treated as non-compliant to trigger
    # human review (GMP safety principle — never silently pass unknown rules).
    return False


def _sign_convention_uncertain(bounds: SpecBounds, actual: float) -> bool:
    """单侧规格 + 实测为负、限值为正 → 符号约定存疑（真空度/负压）。

    真实批记录中真空度/负压常以**负表压**记录（如 ``-0.090 MPa``），而印版规格
    写成 ``≤0.08MPa``——按绝对值理解才是"工作真空度 ≤0.08"。此时数值比较
    （``-0.09 ≤ 0.08``）恒真，会把真实偏差**静默放过**（``≥`` 方向则相反，会把
    合规值判成超差）。工具无法确知印版意图，故 fail-closed 降级为
    ``spec_unverifiable`` 交人工，不静默下结论。

    M8 真实 51 页实测：``≤0.08MPa`` × 负实测值共 6 处（p14/p24，-0.068~-0.096），
    两层此前一致静默放过。仅覆盖"负实测值 + 正限值"这一实际歧义形态；反向
    （负限值 + 正实测值）在制药场景罕见且通常无歧义，不参与判定，避免噪声。
    """
    if actual is None or actual >= 0:
        return False
    if bounds.low is None and bounds.high is not None and bounds.high > 0:
        return True
    if bounds.high is None and bounds.low is not None and bounds.low > 0:
        return True
    return False


# OCR 丢小数点会使数值整体放大 10/100 倍（4.6 -> 46 / 0.46 -> 46）。
_DECIMAL_LOSS_FACTORS = (0.1, 0.01)
# 超差倍数须与"单次/双次小数点位移"一致（比值 ≈ 10 或 100），容差 ±60%。
_DECIMAL_LOSS_RATIO_LOW = 0.6
_DECIMAL_LOSS_RATIO_HIGH = 1.7


def _violated_bound(bounds: SpecBounds, actual: float) -> Optional[float]:
    """返回被越过的那个界（用于评估超差倍数）；无法判断返回 None。"""
    if bounds.op == "between":
        if bounds.low is not None and actual < bounds.low:
            return bounds.low
        if bounds.high is not None and actual > bounds.high:
            return bounds.high
        return None
    if bounds.op in ("le", "lt"):
        return bounds.high
    if bounds.op in ("ge", "gt"):
        return bounds.low
    return None


def _decimal_loss_factor(bounds: SpecBounds, actual: float, raw: str = ""):
    """若实测值疑似 OCR 丢失小数点、缩放后能落入规格，返回该因子，否则 None。

    两条判据同时成立才算：
    1. **"丢点后数字串不变"**：仅当实测值本身没有小数点时才可能丢点
       （``4.6`` 丢点即 ``46``）。自带小数点的 ``45.6`` vs ``0~5°C``（冷库失控）
       属真实偏差，不软化。
    2. **超差倍数与单次/双次位移一致**（≈10×/100×）：``46 vs ≤5.0`` 比值 9.2 ✓；
       而 ``25 vs ≤5.0`` 比值仅 5 —— 更可能是真实超差而非丢点，仍维持 warning。

    作为软信号供调用方降级 severity 或去重，**不**据此静默丢弃偏差。
    """
    if actual == 0:
        return None
    if raw:
        if any(ch in raw for ch in ".。．"):
            return None           # 已带小数点 → 不存在"丢点"
    elif actual != int(actual):
        return None               # 数值本身有小数部分 → 不存在"丢点"
    bound = _violated_bound(bounds, actual)
    if bound is None or bound == 0:
        return None
    ratio = abs(actual) / abs(bound)
    for f in _DECIMAL_LOSS_FACTORS:
        shifted = ratio * f
        if not (_DECIMAL_LOSS_RATIO_LOW <= shifted <= _DECIMAL_LOSS_RATIO_HIGH):
            continue              # 超差倍数与位移不符 → 大概率真实偏差
        if _judge(bounds, actual * f):
            return f
    return None


# ---------------------------------------------------------------------------
# Unit-aware comparison — avoids false positives when spec and actual use
# different but convertible units (e.g. spec="≤50%" vs actual="99ppm").
# ---------------------------------------------------------------------------

# Regex to extract trailing unit from a spec/value string (after the number).
# First char must be a letter or % (not / or .) so we don't capture standalone
# separators; subsequent chars may include / for compound units (mg/L, mg/mL).
_UNIT_RE = re.compile(r"-?\d+\.?\d*\s*([a-zA-Z%²³μµ][a-zA-Z%²³μµ/^\.]*)")

# Known conversions: actual_unit -> {spec_unit: multiplier}
# actual_value_in_spec_unit = actual_value * multiplier
# All multipliers express "1 actual_unit = N spec_unit"
_UNIT_CONVERSIONS = {
    # === 浓度类 ===
    "ppm": {"%": 0.0001, "ppb": 1000},
    "ppb": {"%": 0.0000001, "ppm": 0.001},
    "%":  {"ppm": 10000, "ppb": 10000000},

    # === 质量类（制药常用）===
    # 1 g = 1000 mg = 1000000 µg = 0.001 kg
    "g":  {"mg": 1000, "µg": 1000000, "ug": 1000000, "kg": 0.001},
    "mg": {"g": 0.001, "µg": 1000, "ug": 1000, "kg": 0.000001},
    "µg": {"mg": 0.001, "g": 0.000001, "ug": 1},
    "ug": {"mg": 0.001, "g": 0.000001, "µg": 1},
    "kg": {"g": 1000, "mg": 1000000},

    # === 体积类（制药常用）===
    # 1 L = 1000 mL
    "l":  {"ml": 1000},
    "ml": {"l": 0.001},

    # === 浓度（质量/体积）===
    # mg/L = mg/L ；mg/mL = g/L （1 mg/mL = 1 g/L = 1000 mg/L）
    "mg/l":   {"mg/ml": 0.001, "g/l": 0.001},
    "mg/ml":  {"mg/l": 1000, "g/l": 1},
    "g/l":    {"mg/l": 1000, "mg/ml": 1},

    # === 压力类 ===
    # 1 MPa = 10 bar = 1000 kPa
    "mpa": {"bar": 10, "kpa": 1000},
    "bar": {"mpa": 0.1, "kpa": 100},
    "kpa": {"mpa": 0.001, "bar": 0.01},
}


def _extract_unit(text: Optional[str]) -> str:
    """Extract trailing unit from a value/spec string.

    "99.0ppm" -> "ppm" ; "≤50%" -> "%" ; "0.974" -> "" ; "46.0bar" -> "bar"
    "1.5mg/L" -> "mg/l" ; "100mg/mL" -> "mg/ml"
    """
    if not text:
        return ""
    m = _UNIT_RE.search(str(text))
    return (m.group(1) if m else "").lower().strip(" .,;")


def _try_unit_normalize(actual_num: float, actual_unit: str, spec_str: str):
    """If actual and spec use convertible units, return (converted_actual, note).

    If units differ but no conversion exists, return (None, "unit_mismatch") to
    trigger fail-closed behavior (force human review — never silently compare
    incompatible units, which is a GMP safety risk).
    """
    au = (actual_unit or "").lower().strip(" .,;")
    if not au:
        return actual_num, ""  # no unit on actual — compare as-is
    spec_unit = _extract_unit(spec_str)
    if not spec_unit or spec_unit == au:
        return actual_num, ""  # same unit or no spec unit
    conv = _UNIT_CONVERSIONS.get(au, {})
    if spec_unit in conv:
        converted = actual_num * conv[spec_unit]
        return converted, f"unit normalized: {actual_num}{au} → {converted}{spec_unit}"
    # Units differ but no conversion available — fail-closed
    logger.warning(
        f"Unit mismatch without conversion rule: "
        f"actual={actual_num}{au} vs spec={spec_str} "
        f"(flagging for human review — GMP safety)"
    )
    return None, "unit_mismatch"


# ---------------------------------------------------------------------------
# R100（CR-12）：能否据此断言「超差」的三段判据
#
# 背景：规则层此前只有「判得出来 / 判不出来」两态，判得出来就一律发
# `param_out_of_spec`。真实 51 页语料里 10 条该类型有 7 条属于**规格或数值串
# 本身读错**（p8 46bar、p28 ≤0、p31 <99ppm、p38 18时30分、p39 7800L/7400L），
# 于是复核员看到的是「实测值不在规格内」这种确定性断言，而系统其实**知道**
# 这个比较不成立（文案里自己写着"疑似 OCR 丢失小数点"）—— 自证矛盾。
#
# 三个谓词分别对应三段，**顺序不可换**：
#   1. value_shape_reason      值槽形状（与是否超差无关，必须最前）
#   2. spec_shape_reason       规格形状（`_parse_spec` 失败后才问）
#   3. bound_plausibility_reason  越界可信度（`_judge` 判超差后才问）
#
# 返回的是**原因码**（字符串），不是结论：结论（发不发、发什么类型）由
# `rule_spec` 决定，文案由 `finding_aggregate` 决定 —— 三层各自单一职责，
# 且 `spec_guard` 复用同一组谓词，保证「规则层拒判」与「抑制 LLM 断言」
# 两个判据不会漂移。
# ---------------------------------------------------------------------------

# 值槽里的时刻字面：18时30分 / 18:30 / 18 时 30 分
_CLOCK_VALUE_RE = re.compile(r"(?<!\d)\d{1,2}\s*[:时]\s*\d{1,2}(?:\s*分)?(?!\d)")
# 检测限/上限式实测字面："<0.02%" "<99ppm" "≤5"
_LIMIT_LITERAL_RE = re.compile(r"^\s*[<≤]")
# 规格里出现任一"允许范围"记号 ⇒ 它是区间/限值，而不是单一目标值
_SPEC_OPERATOR_RE = re.compile(r"[<≤>≥±~～]|不超过|不少于|不低于|不高于|以上|以下")
_NUM_ALL_RE = re.compile(r"-?\d+\.?\d*")
# 整数实测值与被越过界限的倍数达到此值 ⇒ 数值串本身可疑（不是小数点位移）
_RATIO_IMPLAUSIBLE = 5.0


def value_shape_reason(actual) -> Optional[str]:
    """值槽**形状**级判据：不是数值列 ⇒ 不可判。可判返回 ``None``。

    与是否超差无关 —— 记录值是「18 时 30 分」时，无论它是否恰好落在规格内，
    都是列串位（真实 p38：pH 列取到了搅拌结束时刻）。因此本判据必须在
    `_judge` **之前**执行：否则「07 时 30 分」会被读成 7.5 并「恰好合规」，
    静默放过一次真实的列串位。
    """
    if actual is None:
        return None
    if _CLOCK_VALUE_RE.search(str(actual)):
        return "time_literal"
    return None


def spec_shape_reason(spec, actual) -> Optional[str]:
    """规格栏**形状**级判据：规格不是可比较的限值 ⇒ 不可判。可判返回 ``None``。

    只在 `_parse_spec` 已失败时调用。判据要求「规格里**恰好一个**数字」：
    ``50L`` 是目标值（无允许范围），而 ``收集前 0.1~0.2，收集后 1.0~1.5`` 是
    分段规格 —— 后者若也被说成「目标值、无允许范围」，就是又一个错解释，
    所以它必须落到 LLM 兜底（fail-closed 保留）。
    """
    if not spec or not isinstance(spec, str):
        return None
    if _SPEC_OPERATOR_RE.search(spec):
        return None
    nums = _NUM_ALL_RE.findall(spec)
    if len(nums) != 1:
        return None
    target = _parse_number(nums[0])
    got = _parse_number(actual)
    if target is None or got is None:
        return None
    if abs(got - target) < 1e-12:
        return None          # 实测值等于目标值 ⇒ 无可报之事
    return "spec_not_a_limit"


def bound_plausibility_reason(bounds, actual: float, raw_actual) -> Optional[str]:
    """越界**可信度**判据：这个「越界」本身是否可信。可信返回 ``None``。

    仅在 `_judge` 判为超差之后调用。四种形态说明「不是记录值超差，而是规格
    或数值串读错了」：

    * ``spec_zero_bound`` —— 被越过的界恰为 0。印版限值写 0 不合常理
      （真实 p28「其他单个杂质≤0」，同页同类项均为 ``≤0.5%``，实为截断）。
      **只对单侧限值成立** —— ``0~5°C`` 的下界 0 是正常区间端，负温是真偏差。
    * ``limit_literal_beyond`` —— 实测值是检测限写法（``<99ppm``）且其数值
      已越界。检测限高于限值时**无法证明合规**，但同样不能断言超差
      （真实 p31「乙酸乙酯≤50 / <99ppm」）。
    * ``ratio_implausible`` —— 实测是**无小数点的整数**、与界限相差 ≥5 倍、
      又不是十进制位移。真实 p39：规格 ``800~1000L`` 记 7800L、
      ``400~500L`` 记 7400L —— **两处都多一个前导 7**，不是丢小数点。
    * ``decimal_loss`` —— 十进制位移后落回规格内（真实 p8：4.6 读成 46）。
    """
    raw = str(raw_actual or "")
    bound = _violated_bound(bounds, actual)
    if bound == 0 and bounds.op in ("le", "lt", "ge", "gt"):
        return "spec_zero_bound"
    if bound is not None and _LIMIT_LITERAL_RE.match(raw):
        return "limit_literal_beyond"
    if bound:
        ratio = abs(actual) / abs(bound)
        if (ratio >= _RATIO_IMPLAUSIBLE and "." not in raw
                and _decimal_loss_factor(bounds, actual, raw) is None):
            return "ratio_implausible"
    if _decimal_loss_factor(bounds, actual, raw) is not None:
        return "decimal_loss"
    return None
