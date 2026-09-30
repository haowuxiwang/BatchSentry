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
