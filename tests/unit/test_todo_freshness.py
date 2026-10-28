"""`docs/TODO.md` 新鲜度机检 —— 待办清单不得静默落后于 `CHANGELOG.md`。

## 为什么需要这个文件

`docs/TODO.md` 的文件头自称：

> 「这是**唯一**的待办清单。开工先读本文件……**不要**另开 TODO 文档。」

而 R66 对抗性审查实测：它的「最后更新」停在 **Round 59（2026-09-23）**，
`CHANGELOG.md` 已经到 **Round 65** —— **6 个轮次没记**。更糟的是，
**它自己的条目里就有"自述已解除却仍是 `[ ]`"**（例：`A4`「2026-09-18 已解除」、
`#162`「已在本次一并修正」），并且仍含**指向 `settings.js` 的行号锚点**
（R63/R65 把前端拆成模块后**已失效**，指不到任何东西）。

**这是最坏的一类文档腐坏**：它不只是"旧"，它**自称是权威且最新的**。
一个按它开工的人会去重做已完成的事、或去修一个不存在的行号。

> ⚠️ 本文件**刻意不写出**那种「模块名 + 冒号 + 行号」的字面形态 ——
> 写了就会被 `test_repo_hygiene.TestNoLineNumberRefsIntoFrontend` 判红
> （护栏的说明文字会**自我命中**）。要举例就描述它，不要复述它。

根因不是"忘了改"，而是**文档与任何强制点都没有连接** —— 于是它必然腐坏。
本文件就是那个强制点。

## 锁住的不变式

A. **复核戳必须存在**且可解析（`**复核戳：Round NN（`）—— 没有戳 = 无法机检 = 红；
B. **戳不得落后**：`TODO 戳 >= CHANGELOG 里出现的最大 Round`
   （**不要求相等** —— 允许 TODO 先于 changelog，不允许落后）；
C. **免责声明必须还在**：文件里必须含「未复核」字样，防止有人把
   "§A 以下未复核" 的诚实标注删掉、又让它看起来像当前事实；
D. **不空转**：两边都必须真的解析出内容；
E. **核销必须留痕**：`[x]` 且带核销尾注的条目数 == `§0.1.1 已核销` 表的行数
   （R66 加的台账 —— 防止"翻了勾却不写证据"）；
F. **`docs/TODO.md` 里不得有前端行号锚点**：R63/R65 把前端拆成模块后，
   那种「模块名 + 冒号 + 行号」的指路**必然失效**（R66 已在清单里清干净）。
   注：只查**这一份活清单**；`docs/ADVERSARIAL_REVIEW_*.md` 是历史证据，**不回改**。
G. **交叉引用必须解析到真实条目**：`见 <id>` / `见 §N 的 <id>` 不得悬空
   （`TestCrossReferencesResolve`）。
H. **CHANGELOG 的批号标题必须用 `Round <N>` 形态**（`TestChangelogHeadingForm`）：
   不得写另一套编号体系的短形态。R86 的标题曾写成 `R86`（而非 `Round 82`），
   而**不变式 B 只认 `Round NN`** ⇒ 它**静默失效**：`max` 停在 81、实际最新是
   Round 82，戳落后 1 轮却全绿。⇒ **护栏的解析形态本身就是它的攻击面**。
I. **`[Unreleased]` 内的 `Round N` 必须非递增**（`TestChangelogRoundMonotonic`）：
   B 只看 `max`，对**方向**无感 ⇒ "新增了条目但 Round 没前进"看不出来。
   R90 收尾期真实发生过一次：第三十五批的标题被插在第三十四批标题**与正文之间**，
   把后者顶成孤立标题（标题在上、正文在下）。

## 每轮要做什么

完成一轮工作后，把 `docs/TODO.md` 头部的复核戳改成该轮的 Round，并**至少**：
重排 §0 的当前 backlog。改这一行是**一轮的收尾动作**，与 CHANGELOG 同级。
核销条目时**两处都要改**：① 该条 `[ ]`→`[x]` 并加尾注；② 在 §0.1.1 补一行证据
（E 会机检这两者是否对得上）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_TODO = REPO / "docs" / "TODO.md"
_CHANGELOG = REPO / "CHANGELOG.md"

# 复核戳：`> **复核戳：Round 66（2026-09-30）** ← ...`
_STAMP_RE = re.compile(r"\*\*复核戳：\s*Round\s*(\d+)")
_ROUND_RE = re.compile(r"Round\s*(\d+)")
_DISCLOSURE = "未复核"

# ── 核销台账（不变式 E）────────────────────────────────────────────────
# 条目尾注形态：` —— **R66 已核销**（证据见 §0.1）`
_CHECKOFF_MARK = "已核销"
_CHECKOFF_LINE_RE = re.compile(r"^\s*[-*]\s*\[[xX]\].*" + _CHECKOFF_MARK)
_LEDGER_START = "### 0.1.1"
_LEDGER_END = "### 0.1.2"

# ── 前端行号锚点（不变式 F）────────────────────────────────────────────
# ⚠️ 刻意**不写出**那种字面形态（会自我命中 `test_repo_hygiene`）：
# 后缀与"冒号+数字"分开拼，源码里就不存在完整形态。
_JS_SUFFIX = ".js"
_COLON_LINENO = r":\d+"
_FRONTEND_STEMS = (
    "settings", "review", "upload", "status",
    "findings-map", "review-state", "review-pageinfo", "upload-jobs",
)
_FRONTEND_ANCHOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in _FRONTEND_STEMS) + r")[\w-]*"
    + re.escape(_JS_SUFFIX) + _COLON_LINENO
)


def _todo_stamp() -> int | None:
    m = _STAMP_RE.search(_TODO.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else None


def _changelog_max_round() -> int | None:
    rounds = [int(x) for x in _ROUND_RE.findall(_CHANGELOG.read_text(encoding="utf-8"))]
    return max(rounds) if rounds else None


def _unreleased_rounds(text: str) -> list[int]:
    """取 `## [Unreleased]` 段内**批号标题行**上的 `Round N`，按出现顺序。

    只认 `###` 标题（`_HEADING_BATCH_RE`）—— 正文里大量 `Round NN` 引用
    （"承第三十五批""Round 86 的标题…"）**不能**进来，否则序列必然乱。
    """
    out: list[int] = []
    in_unreleased = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            in_unreleased = ln.strip() == "## [Unreleased]"
            continue
        if in_unreleased and _HEADING_BATCH_RE.search(ln):
            m = _ROUND_RE.search(ln)
            if m:
                out.append(int(m.group(1)))
    return out


class TestAntiVacuity:
    """先钉住"提取器没坏"，否则下面的比较可能只是 None vs None。"""

    def test_both_files_exist(self):
        assert _TODO.is_file(), f"找不到 {_TODO}"
        assert _CHANGELOG.is_file(), f"找不到 {_CHANGELOG}"

    def test_both_sides_parsed(self):
        stamp, latest = _todo_stamp(), _changelog_max_round()
        assert stamp is not None, (
            "docs/TODO.md 里找不到复核戳（形态：`**复核戳：Round NN（YYYY-MM-DD）**`）"
        )
        assert latest is not None, "CHANGELOG.md 里一个 `Round NN` 都没解析到 —— 锚点失效了"


class TestTodoFreshness:
    def test_todo_stamp_is_not_behind_changelog(self):
        stamp, latest = _todo_stamp(), _changelog_max_round()
        assert stamp is not None and latest is not None
        assert stamp >= latest, (
            f"docs/TODO.md 的复核戳是 Round {stamp}，而 CHANGELOG.md 已到 Round {latest} —— "
            f"落后 {latest - stamp} 轮。\n"
            "  → 本轮收尾时必须复核 docs/TODO.md：至少重排 §0 的当前 backlog，"
            "并把复核戳改成当前 Round。"
        )

    def test_todo_keeps_the_not_re_reviewed_disclosure(self):
        text = _TODO.read_text(encoding="utf-8")
        assert _DISCLOSURE in text, (
            "docs/TODO.md 丢了「未复核」声明 —— §A 及以下只是 Round 59 的快照，"
            "删掉这行会让读者把未复核的历史条目当成当前事实"
        )


def _checkoff_lines() -> list[str]:
    """带核销尾注的 `[x]` 条目行。"""
    return [ln for ln in _TODO.read_text(encoding="utf-8").splitlines()
            if _CHECKOFF_LINE_RE.match(ln)]


def _ledger_rows() -> list[str]:
    """`§0.1.1 已核销` 表里的条目行（`| **B10-1** ...` 形态）。"""
    lines = _TODO.read_text(encoding="utf-8").splitlines()
    try:
        i = next(k for k, ln in enumerate(lines) if ln.startswith(_LEDGER_START))
        j = next(k for k, ln in enumerate(lines) if k > i and ln.startswith(_LEDGER_END))
    except StopIteration:
        return []
    return [ln for ln in lines[i:j] if ln.startswith("| **")]


class TestCheckoffLedger:
    """不变式 E：**翻了勾就必须在台账里留证据行**，两边数量必须相等。

    为什么锁这条：R66 之前清单腐坏的方式是"自述已解除却仍是 `[ ]`"；
    修完之后**新的腐坏方式**是"翻了 `[x]` 却没写证据" —— 那样 `[x]` 只是
    一个无法核验的断言，比 `[ ]` 更糟（它冒充"已验证"）。
    """

    def test_both_sides_parsed(self):
        marks, rows = _checkoff_lines(), _ledger_rows()
        assert marks, (
            "一条带核销尾注的 `[x]` 都没解析到 —— 要么台账被删了，要么锚点形态变了"
        )
        assert rows, f"找不到 {_LEDGER_START} 的表格行 —— 台账被删或标题改名了"

    def test_every_checkoff_has_exactly_one_ledger_row(self):
        marks, rows = _checkoff_lines(), _ledger_rows()
        assert marks and rows
        assert len(marks) == len(rows), (
            f"核销尾注有 {len(marks)} 条，而 §0.1.1 台账有 {len(rows)} 行 —— 对不上。\n"
            "  → 核销一条时必须**两处都改**：① 条目 `[ ]`→`[x]` 并加"
            f"「{_CHECKOFF_MARK}」尾注；② 在 §0.1.1 补一行证据。\n"
            "  → 只改一处会让 `[x]` 变成无法核验的断言。"
        )


class TestNoDeadFrontendAnchors:
    """不变式 F：活清单里不得残留前端行号锚点（拆模块后必失效）。"""

    def test_todo_has_no_frontend_line_number_anchor(self):
        hits = [ln.strip() for ln in _TODO.read_text(encoding="utf-8").splitlines()
                if _FRONTEND_ANCHOR_RE.search(ln)]
        assert not hits, (
            f"docs/TODO.md 里还有 {len(hits)} 处前端行号锚点 —— "
            "R63/R65 拆模块后这些行号指不到任何东西：\n  "
            + "\n  ".join(h[:110] for h in hits[:5])
            + "\n  → 改成模块级指路（说「哪个模块的哪个函数」），不要写行号。"
        )

    def test_the_anchor_detector_actually_matches(self):
        """防空转：构造一个**应当命中**的样例，证明上面那条不是恒绿。"""
        sample = "见 `settings" + _JS_SUFFIX + ":46` 的实现"
        assert _FRONTEND_ANCHOR_RE.search(sample), (
            "锚点检测器连构造样例都匹配不到 ⇒ 上一条断言是空断言"
        )
        # 反例：模块级指路（无行号）**不得**命中
        assert not _FRONTEND_ANCHOR_RE.search("见 `settings" + _JS_SUFFIX + "` 的 renderProvider"), (
            "检测器把「无行号的模块指路」也判成锚点了 ⇒ 过宽，会误报"
        )


# ── 交叉引用解析（不变式 G）────────────────────────────────────────────
# 条目 id 形态：`0-6` / `B1-14` / `B11-7` / `B9-10`
_ITEM_ID = r"(?:[A-Z]{1,2}\d+-\d+|\d+-\d+)"
# 引用形态：`见 <id>` 或 `见 §<sec> 的 <id>`（`§` 可省、id 可带反引号）
# ⚠️ 章节号**故意放宽**成「任意非空白/非`的`串」：若只认数字，`见 §B 的 X`
# 这类引用会**一条都匹配不到** ⇒ 被**静默跳过**（首版即如此，变异 M2 当场抓到）。
# 宁可捕获后判红（护栏只支持数字章节），也不要静默漏过看不懂的引用。
_XREF_RE = re.compile(
    r"见\s*(?:§(?P<sec>[^\s的`]+)\s*的\s*)?`?(?P<id>" + _ITEM_ID + r")`?"
)
# 声明形态：表格行 `| **<id>** ...` 或复选框行 `- [ ] **<id> ...`
_DECL_RE = re.compile(
    r"^(?:\|\s*\*\*(?P<t>" + _ITEM_ID + r")\*\*"
    r"|-\s*\[[ xX]\]\s*\*\*(?P<c>" + _ITEM_ID + r")[\s：:])"
)
# 标题（用于把数字章节号映射到行区间）：`## 0. ...` / `### 0.1.1 ...`
_HEADING_RE = re.compile(r"^#{2,3}\s+(\S+)")
_NUMERIC_SECTION_RE = re.compile(r"\d+(?:\.\d+)*")


def _todo_lines() -> list[str]:
    return _TODO.read_text(encoding="utf-8").splitlines()


def _declared_items() -> dict[str, int]:
    """条目 id → 它被声明的行号（1-based）。同一 id 多次声明取**首次**。"""
    out: dict[str, int] = {}
    for lineno, line in enumerate(_todo_lines(), 1):
        m = _DECL_RE.match(line)
        if m:
            out.setdefault(m.group("t") or m.group("c"), lineno)
    return out


def _xrefs() -> list[tuple[int, str | None, str]]:
    """全部 `见 <id>` 交叉引用 → `(行号, 章节号 or None, 被引 id)`。"""
    out: list[tuple[int, str | None, str]] = []
    for lineno, line in enumerate(_todo_lines(), 1):
        for m in _XREF_RE.finditer(line):
            out.append((lineno, m.group("sec"), m.group("id")))
    return out


def _section_range(sec: str) -> tuple[int, int] | None:
    """数字章节号 → `[起始行, 结束行)`；找不到该章节返回 None。

    结束行 = 下一个**非后代**标题 —— `§0` 的区**包含** `§0.1` / `§0.1.1` 这些后代，
    到 `## A.` 为止。字母章节（`A.`/`B.`/`F1.`）不参与数字章节号匹配。
    """
    lines = _todo_lines()
    heads: list[tuple[str | None, int]] = []
    for lineno, line in enumerate(lines, 1):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        token = m.group(1).rstrip(".")
        heads.append((token if _NUMERIC_SECTION_RE.fullmatch(token) else None, lineno))
    start = next((ln for num, ln in heads if num == sec), None)
    if start is None:
        return None
    end = len(lines) + 1
    for num, ln in heads:
        if ln <= start:
            continue
        if num is not None and (num == sec or num.startswith(sec + ".")):
            continue
        end = ln
        break
    return (start, end)


class TestCrossReferencesResolve:
    """不变式 G：`见 <id>` / `见 §N 的 <id>` 必须解析到**真实条目**。

    为什么锁这条：R66 实测 `#144` 的尾注引用了**不存在**的 `0-6` —— 悬空引用。
    读者按它去找会一无所获；而本文件当时只锁「台账 1:1」与「无失效行号锚点」，
    **不查交叉引用**（报告 §11.6）。

    ⚠️ 本护栏**只读 `docs/TODO.md`**。核销文字里**不要写出**「引用一个不存在的
    条目」那种**字面引用形态** —— 写了会被这条护栏**自己判红**（首版核销时就
    当场命中）。要举例就描述它，别复述它 —— 与 `TestNoDeadFrontendAnchors`
    那条「说明文字不得自我命中」是同一个约定。
    """

    def test_both_sides_parsed(self):
        decl, refs = _declared_items(), _xrefs()
        assert decl, "一个条目 id 都没解析到 —— 声明锚点形态变了"
        assert refs, (
            "一条 `见 <id>` 交叉引用都没解析到 —— 要么引用锚点形态变了，"
            "要么引用被删光（后者会让下面两条断言退化成空断言）"
        )

    def test_every_reference_resolves_to_a_declared_item(self):
        decl, refs = _declared_items(), _xrefs()
        dangling = [(ln, sec, i) for ln, sec, i in refs if i not in decl]
        assert not dangling, (
            "悬空交叉引用（引用了清单里不存在的条目）：\n  "
            + "\n  ".join(
                (f"L{ln}: §{sec} 的 {i}" if sec else f"L{ln}: 见 {i}")
                for ln, sec, i in dangling
            )
            + "\n  → 要么补上该条目，要么把引用改指向真实存在的条目。"
        )

    def test_section_qualified_reference_lives_in_that_section(self):
        """`§N 的 <id>` 还要求该条目**声明在 §N 内** —— 条目挪了章节，引用要跟着改。"""
        decl = _declared_items()
        bad: list[str] = []
        for ln, sec, i in _xrefs():
            if not sec:
                continue
            rng = _section_range(sec)
            if rng is None:
                bad.append(
                    f"L{ln}: §{sec} —— 无法解析的章节号（护栏只支持**数字**章节，"
                    f"如 §0 / §0.1.1）。fail-closed：看不懂的引用一律判红，不静默跳过"
                )
                continue
            if i not in decl:
                continue          # 悬空由上面那条用例负责报，这里不重复
            start, end = rng
            if not (start <= decl[i] < end):
                bad.append(
                    f"L{ln}: §{sec} 的 {i} —— 该条目声明在 L{decl[i]}，"
                    f"不在 §{sec} 的 [{start}, {end}) 区间内"
                )
        assert not bad, (
            "`§N 的 <id>` 指向的条目不在 §N 内：\n  "
            + "\n  ".join(bad)
            + "\n  → 条目被挪了章节，引用必须跟着改。"
        )

    def test_detectors_are_not_vacuous(self):
        """阴性对照：引用一个**不存在**的条目时必须能被报出来。"""
        # 两种引用形态都要能匹配
        assert _XREF_RE.search("见 0-99"), "引用检测器匹配不到 `见 <id>`"
        assert _XREF_RE.search("见 §0 的 0-99"), "引用检测器匹配不到 `见 §N 的 <id>`"
        # **非数字章节**也必须被捕获（否则会被静默跳过 —— 首版即此缺陷，M2 抓到）
        m = _XREF_RE.search("见 §B 的 0-99")
        assert m and m.group("sec") == "B", (
            "引用检测器漏掉了非数字章节号 ⇒ `见 §B 的 X` 会被静默跳过"
        )
        # 且该 id 确实不在已声明集合里 —— 证明"解析"这一步有判别力，不是恒绿
        assert "0-99" not in _declared_items(), "0-99 竟然被当成已声明条目"
        # 两种声明形态都要能匹配
        assert _DECL_RE.match("| **0-1** | 推送 |"), "声明检测器匹配不到表格行"
        assert _DECL_RE.match("- [ ] **B10-6 精度现状：** 无 P/R"), (
            "声明检测器匹配不到「复选框 + id 与标题同处一个粗体段」的形态"
        )
        # 反例：正文里的引用行**不得**被当成声明（否则检测器过宽、悬空引用会漏网）
        assert not _DECL_RE.match("  仍开放，见 §0 的 0-6。"), (
            "声明检测器把正文引用行也判成了声明 ⇒ 过宽"
        )
        # 章节区间必须真的收窄（否则「§N 内」这一条会退化成恒真）
        rng = _section_range("0")
        assert rng is not None, "解析不到 §0 的区间 —— 标题锚点失效"
        assert rng[1] <= len(_todo_lines()) + 1 and rng[0] < rng[1]


# ── CHANGELOG 标题编号形态（不变式 H）──────────────────────────────────
# 批号标题形态：`### …（Round NN 第M批，YYYY-MM-DD）`
_HEADING_BATCH_RE = re.compile(r"^### .*?（[^）]*?第[^）]*?批", re.MULTILINE)
#: 短形态：`### …（R86 第二十六批` —— `R<N>` 是 TODO/记忆的体系，不是 CHANGELOG 的
_HEADING_SHORT_FORM_RE = re.compile(r"^### .*?（R\s*\d+\s*第", re.MULTILINE)

#: 标题里的短形态轮次记号：`—— R126`、`（R999，2026-10-28）`、`—— R126 / TODO B3-3`
#: `(?<![\w])` 是为了不把 `Round`/`CR-1` 之类误认（`Round` 后面是字母，本来就匹配不上，
#: 这个前瞻只是把意图写出来）。
_ANY_SHORT_FORM_RE = re.compile(r"(?<![\w])R\s*(\d+)")

#: **历史遗留白名单（只减不增的棘轮）** —— TODO **#175**。
#:
#: 实测（2026-10-28，R128 收尾复核）：`CHANGELOG.md` 有 **24 条** `###` 标题用短形态
#: 标轮次，且**两种形态都让不变式 B 失效**：
#:
#:   ① **22 条**只有短形态：`### …（2026-10-27）—— R126 / TODO B3-3`
#:      ⇒ `_changelog_max_round()` 完全看不见它们（缺 110 / 112 两轮）；
#:   ② **2 条**两种形态**互相矛盾**：`### …（Round 90 第四十五批，2026-10-23）—— R102 首批`
#:      ⇒ B 看见的是 **90**，而真实轮次是 101/102 —— 这正是 #175 里"`max` 停在 90、
#:      戳落后 22 轮却全绿"的直接成因，比 ① 更隐蔽（它看起来是"有 Round 的"）。
#:
#: 为什么是白名单而不是直接判红：回改这 24 条历史标题属**编号体系决策**
#: （`R<NN>` 与 `Round NN` 是否同一套计数），按 MEMORY「两套编号不得换算」，
#: 这个决定**不由 agent 单方面做**（#175 里已列成两个候选方案）。
#:
#: 但"不回改历史"不等于"继续放行新的"：本集合是**棘轮** ——
#: 命中集必须是它的**子集**。改好一条 ⇒ 集合可以缩小（本用例仍绿）；
#: 新增一条 `—— R999` ⇒ **立刻红**。这样 #175 的验收条件
#: （"注入 `—— R999` ⇒ 必须红"）**现在就成立**，而不必等编号体系定案。
_LEGACY_SHORT_FORM_ROUNDS = frozenset({
    101, 102, 103, 104, 105, 106, 107, 108, 109, 111, 113, 114, 115,
    116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126,
})


def _short_form_headings(text: str) -> list[tuple[int, str]]:
    """返回 `(轮次, 标题)` —— 用短形态标轮次、却**没有**用规范形态标同一轮次的 `###` 标题。

    只扫 `### ` 标题行：正文里大量合法使用 `RNN`（"承第三十五批""Round 86 的标题…"），
    扫正文会立刻变成一堆误报（同 `_HEADING_SHORT_FORM_RE` 的取舍）。

    同一标题里既有 `Round 128` 又有 `—— R128` 时**不报** —— 规范形态已在，B 看得见它。
    """
    out: list[tuple[int, str]] = []
    for ln in text.splitlines():
        if not ln.startswith("### "):
            continue
        canonical = {int(x) for x in _ROUND_RE.findall(ln)}
        for m in _ANY_SHORT_FORM_RE.finditer(ln):
            n = int(m.group(1))
            if n in canonical:
                continue
            out.append((n, ln.strip()[:120]))
    return out



class TestChangelogHeadingForm:
    """不变式 H：CHANGELOG 的批号标题必须用 `Round <N>` 形态。

    为什么锁这条：R86 的标题写成 `（R86 第二十六批，…）` —— `R86` 属**另一套编号
    体系**（TODO / 记忆用 `RNN`），CHANGELOG 的体系是 `Round NN`。后果不是"写错
    一个数字"，而是**不变式 B 静默失效**：`_changelog_max_round()` 只认 `Round NN`，
    于是 `max` 停在 81、实际最新已是 Round 82 —— **戳落后 1 轮却全绿**。
    ⇒ 一般化教训：**护栏的解析形态本身就是它的攻击面**，形态漂移 = 护栏失效。
    """

    def test_no_heading_uses_the_short_r_form(self):
        text = _CHANGELOG.read_text(encoding="utf-8")
        hits = [
            ln.strip()[:120]
            for ln in text.splitlines()
            if _HEADING_SHORT_FORM_RE.search(ln)
        ]
        assert not hits, (
            "CHANGELOG 的批号标题用了短形态（应写 `Round <N>`）—— "
            "这会让 `test_todo_stamp_is_not_behind_changelog` **静默失效**：\n  "
            + "\n  ".join(hits)
        )

    def test_every_batch_heading_carries_a_round_token(self):
        """每个 `### …（…第M批…）` 标题都必须带 `Round` 记号。

        没有 `Round` 的标题**不会进入**新鲜度比较 ⇒ 等于对不变式 B 隐形。
        """
        text = _CHANGELOG.read_text(encoding="utf-8")
        heads = [
            ln.strip()[:120]
            for ln in text.splitlines()
            if _HEADING_BATCH_RE.search(ln)
        ]
        assert len(heads) >= 10, (
            f"只解析到 {len(heads)} 条批号标题（预期 >=10）—— 标题形态变了，"
            "本用例可能已退化成空断言"
        )
        bad = [h for h in heads if "Round" not in h]
        assert not bad, (
            "以下批号标题没有 `Round <N>` 记号 —— 它们不会进入新鲜度比较：\n  "
            + "\n  ".join(bad)
        )

    def test_detectors_are_not_vacuous(self):
        """防空转：两条检测器都要有判别力，且必须**只认标题行**。"""
        assert _HEADING_BATCH_RE.search("### 标题（Round 83 第二十七批，2026-10-10）"), (
            "批号标题检测器匹配不到标准形态"
        )
        assert _HEADING_SHORT_FORM_RE.search("### 标题（R86 第二十六批，2026-10-10）"), (
            "短形态检测器匹配不到 `R86` —— 它抓不到本不变式要防的那种标题"
        )
        assert not _HEADING_SHORT_FORM_RE.search(
            "### 标题（Round 82 第二十六批，2026-10-10）"
        ), "短形态检测器把正确的 `Round 82` 也判成短形态 ⇒ 过宽"
        # 正文里的短形态引用是**合法**的（本仓正文大量使用 `RNN`）—— 不得被误伤
        assert not _HEADING_SHORT_FORM_RE.search("> ⚠️ 同日更正（R69 第九批）：上文初版…"), (
            "短形态检测器命中了正文引用行 ⇒ 没有锚在 `### ` 标题上"
        )

    def test_no_new_heading_uses_the_short_r_form(self):
        """**棘轮（#175 的收尾）**：短形态标题只许减少，不许新增。

        `_HEADING_BATCH_RE` 只认「第M批」形态 ⇒ R103–R126 那 22 条 `—— RNN` 标题
        **一条都不进**不变式 H；而它们对 `_changelog_max_round()` 也隐形 ⇒
        不变式 B 的唯一强制点失效，戳可以落后 22 轮而机检全绿。
        本用例把"隐形标题"显式数出来，并只允许历史白名单内存在。
        """
        text = _CHANGELOG.read_text(encoding="utf-8")
        heads = [ln for ln in text.splitlines() if ln.startswith("### ")]
        assert len(heads) >= 100, (
            f"只扫到 {len(heads)} 条 `###` 标题（预期 >=100）—— 本用例可能已退化成空断言"
        )
        offenders = [
            (n, h) for n, h in _short_form_headings(text)
            if n not in _LEGACY_SHORT_FORM_ROUNDS
        ]
        assert not offenders, (
            "以下标题用短形态 `RNN` 标轮次（CHANGELOG 的体系是 `Round NN`）—— "
            "它们**不会进入**新鲜度比较，等于对不变式 B 隐形。"
            f"历史白名单只有 {len(_LEGACY_SHORT_FORM_ROUNDS)} 个轮次（#175），"
            "新增一律必须写成 `Round NN`：\n  "
            + "\n  ".join(f"R{n}  {h}" for n, h in offenders)
        )

    def test_short_form_ratchet_flags_a_new_heading(self):
        """**#175 的验收条件**：注入一条 `—— R999` 标题 ⇒ 必须被抓住。

        #175 原文的验收标准是"注入一条 `—— R999` 标题 ⇒ 不变式 B 必须红
        （当前全绿）"。本轮实测确认**仍然全绿**（`max` 停在 128，两条 H 检查都不认），
        故按"棘轮"补上这一层：不靠回改 22 条历史标题，也现在就能抓住新增的。
        """
        # ① 判别力：注入后必须命中，且轮次可读
        injected = (
            _CHANGELOG.read_text(encoding="utf-8")
            + "\n### 注入的假条目（R999，2026-10-28）—— 0-1\n"
        )
        new = [(n, h) for n, h in _short_form_headings(injected)
               if n not in _LEGACY_SHORT_FORM_ROUNDS]
        assert new and new[0][0] == 999, (
            "#175 验收未达成：注入 `R999` 后仍未被判红"
        )
        # ② 过宽防护：规范形态、同标题内已标规范轮次、正文引用都不得误伤
        assert not _short_form_headings("### 标题（Round 128，2026-10-28）—— 0-65"), (
            "把规范的 `Round 128` 判成短形态 ⇒ 过宽"
        )
        assert not _short_form_headings("### 标题（Round 128）—— R128 / TODO 0-65"), (
            "同一标题已用规范形态标了同一轮次，却仍被判短形态 ⇒ 过宽"
        )
        assert not _short_form_headings("正文说（R999 第九批）：上文初版…"), (
            "扫到了正文引用 ⇒ 没有锚在 `### ` 标题上"
        )


class TestChangelogRoundMonotonic:
    """不变式 I：`[Unreleased]` 内的批号标题，`Round N` 必须**非递增**。

    为什么锁这条：不变式 B 只看 `max(Round)`，而 `max` 对**方向**无感。往
    `[Unreleased]` 顶部塞一条 Round 更小的条目（复制上一批标题、忘改数字，
    R90 收尾期真实发生过一次：第三十五批的标题被插在第三十四批标题**与正文之间**，
    把后者顶成孤立标题），`max` 仍指向旧值 ⇒ **"新增了条目但 Round 没前进"看不出来**。
    `Round` 是唯一被机检的轮次记号，它不前进 = 新鲜度判据整体空转。

    方向：本仓 `[Unreleased]` 是**新的在上**（实测 88 → 87 → 86 …）⇒ 非递增。
    """

    def test_unreleased_rounds_are_non_increasing(self):
        rounds = _unreleased_rounds(_CHANGELOG.read_text(encoding="utf-8"))
        assert len(rounds) >= 2, (
            f"`[Unreleased]` 里只解析到 {len(rounds)} 条带 Round 的批号标题 —— "
            "提取器或标题形态变了，本用例会退化成空断言"
        )
        bad = [
            (a, b) for a, b in zip(rounds, rounds[1:]) if b > a
        ]
        assert not bad, (
            "`[Unreleased]` 里的 Round 不是非递增（新条目应在**上**方）："
            f"实测序列 {rounds}；逆序处 {bad}"
        )

    def test_detector_flags_a_regression(self):
        """判别力：**旧 Round 塞在顶部**必须被检出。"""
        text = (
            "## [Unreleased]\n"
            "### 甲（Round 88 第三十六批，2026-10-21）\n"
            "### 乙（Round 90 第三十七批，2026-10-22）\n"  # 逆序：90 在 88 之后
            "## [1.2.0] — 2026-09-21\n"
            "### 丙（Round 40 第一批，2026-09-01）\n"     # 已发布段不参与
        )
        rounds = _unreleased_rounds(text)
        assert rounds == [88, 90], f"提取结果不对：{rounds}"
        assert any(b > a for a, b in zip(rounds, rounds[1:])), (
            "检测器没能发现「旧 Round 塞在顶部」"
        )

    def test_detector_ignores_body_prose_and_released_sections(self):
        """防空转 / 防过宽：正文引用与已发布段都**不得**进入序列。"""
        text = (
            "## [Unreleased]\n"
            "### 甲（Round 88 第三十六批，2026-10-21）\n"
            "> 承第三十五批（Round 87）—— 正文里的 Round 不算\n"
            "## [1.2.0] — 2026-09-21\n"
            "### 丙（Round 40 第一批，2026-09-01）\n"
        )
        assert _unreleased_rounds(text) == [88], (
            f"提取器把正文引用或已发布段也算进来了：{_unreleased_rounds(text)}"
        )
