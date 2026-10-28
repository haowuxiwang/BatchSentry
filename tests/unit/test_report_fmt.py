"""`scripts/report_fmt.py` 的护栏 —— 诊断报文**不得低报**（PITFALLS §六十四）。

实测（2026-10-10）：门禁报「**8 个**源文件与清单不符」却只列 **5** 个（`[:5]`）⇒
读者据此以为失效面是 5 个。**数量与清单互相矛盾时，读者信更具体的那个（清单）**。

这里锁三件事：
1. `fmt_list` 的**两条边界**（≤上限全列 / >上限**明说**余量）；
2. 上限处**不得**出现标记（反空转：避免"永远带标记"这种假通过）；
3. **只有一处实现** —— 各脚本不得再自造 `fmt_list` / `_fmt_paths`（否则又会各漂各的）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
# 与各脚本同一份 `report_fmt`（它们按**文件位置**互导，故这里也用顶层名导入）
sys.path.insert(0, str(_SCRIPTS))

import report_fmt as rf  # noqa: E402


class TestFmtListBehaviour:
    def test_within_cap_shows_everything_without_marker(self):
        items = [f"f{i}" for i in range(rf.DEFAULT_LIST_CAP)]
        out = rf.fmt_list(items)
        assert all(i in out for i in items)
        assert "未显示" not in out

    def test_over_cap_declares_hidden_count_and_total(self):
        n = rf.DEFAULT_LIST_CAP + 3
        items = [f"f{i}" for i in range(n)]
        out = rf.fmt_list(items)
        assert "另有 3 个未显示" in out and f"共 {n} 个" in out
        assert items[0] in out and items[-1] not in out      # 尾部确实被省略

    def test_explicit_cap_boundary_has_no_marker(self):
        """**反空转**：恰好等于 cap 时**不得**出现标记（否则本模块的用例无法证伪）。"""
        items = [f"f{i}" for i in range(5)]
        assert "未显示" not in rf.fmt_list(items, cap=5)
        # 多一个就**必须**声明余量
        assert "另有 1 个未显示，共 6 个" in rf.fmt_list(items + ["f5"], cap=5)

    def test_sep_style_keeps_the_join_and_still_declares(self):
        out = rf.fmt_list([f"l{i}" for i in range(8)], cap=5, sep="; ")
        assert out.startswith("l0; l1; l2; l3; l4")          # 保留调用方原有连接风格
        assert "另有 3 个未显示，共 8 个" in out


class TestSingleImplementation:
    """格式化**只允许一处实现** —— 否则又会各写各的、各漂各的。"""

    _DEF_RE = re.compile(r"^\s*def\s+(fmt_list|_fmt_paths)\s*\(", re.MULTILINE)

    def test_only_report_fmt_defines_the_formatter(self):
        offenders = []
        for py in sorted(_SCRIPTS.glob("*.py")):
            if py.name == "report_fmt.py":
                continue
            if self._DEF_RE.search(py.read_text(encoding="utf-8")):
                offenders.append(py.name)
        assert not offenders, f"这些脚本自造了清单格式化实现（应 import report_fmt）：{offenders}"

    def test_the_consumers_share_one_function_object(self):
        """`bundle_manifest._fmt_paths` 必须**就是** `report_fmt.fmt_list`（不是副本）。"""
        import bundle_manifest as bm
        assert bm._fmt_paths is rf.fmt_list
        assert bm._PATH_LIST_CAP == rf.DEFAULT_LIST_CAP
