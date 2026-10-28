"""R125 / TODO B3-3：严重度只有一个真值 —— **发射结果**，不是注册表声明。

## 为什么删掉 `RuleSpec.severity`（而不是"把声明改对"）

注册表曾声明 R1a/R1b `severity="warning"`，而实现会发 `critical`。这不是
"文档过时"，而是**第二处真值**；更糟的是它**没有任何消费点**（全仓唯一的
引用是"断言它是三个合法值之一"的用例）⇒ 不一致可以长期无人发现，而任何
"以注册表为准"的工具（文档生成、规则目录页、前端分级）都会拿到错的值。

更根本的是 B1-9 之后**同一规则、同一 type 可发两种严重度**（真倒序 critical /
仅重叠 warning，见本文件最后一条用例）—— **单一字段本身已不足以表达**。
所以把声明"改对"只是把第二处真值暂时调齐，下次照样漂。删字段才是收敛。

## 锁住的不变式

A. `RuleSpec` 的字段集合**固定**且不含 `severity` —— 防"加回一个没人校验的声明"；
B. 注册表构造 `RuleSpec(...)` 时不得传 severity（位置或关键字）—— 防"字段删了、
   参数还在"这种半拉状态（那会 TypeError，但更可能被顺手改成别的东西）；
C. 规则**发射**的 severity 字面量必须 ∈ 三值词表（防 `"warn"` / `"crit"` 漂移）——
   这是"真值侧"的最小机检；
D. 行为证据：R1b 对两种输入发两种严重度 ⇒ 单一字段不足以表达（删除的理由本身）。

⚠️ 本文件**不**验证"每条规则的严重度都合理"——那是评测/标注集的活，见 MEMORY §12。
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from core.rules.registry import RULE_REGISTRY, RuleSpec

REPO = Path(__file__).resolve().parents[2]
REGISTRY_SRC = REPO / "core" / "rules" / "registry.py"
RULES_DIR = REPO / "core" / "rules"
VOCAB = {"critical", "warning", "info"}
EXPECTED_FIELDS = {"id", "type", "description", "check", "enabled_by_default"}


# ─────────────────────────────────────────────────────────────────────────────
# A. 字段集合固定，且不含 severity
# ─────────────────────────────────────────────────────────────────────────────


class TestRuleSpecHasNoSeverityClaim:
    def test_field_set_is_exactly_this(self):
        names = {f.name for f in dataclasses.fields(RuleSpec)}
        assert names == EXPECTED_FIELDS, (
            f"RuleSpec 的字段变了：{sorted(names)}。\n"
            "  若是新增字段，请先想清楚它是不是**第二处真值**（B3-3 的教训）；\n"
            "  若是加回 severity —— 它没有任何校验点，只会再次与实现漂移。"
        )
        assert "severity" not in names

    def test_no_instance_exposes_severity(self):
        """连属性都不该有：`spec.severity` 能读到，就会有人当它可信。"""
        leaked = [s.id for s in RULE_REGISTRY if hasattr(s, "severity")]
        assert leaked == [], f"这些规则仍能读到 severity：{leaked}"


# ─────────────────────────────────────────────────────────────────────────────
# B. 注册表构造不得传 severity
# ─────────────────────────────────────────────────────────────────────────────


def _rulespec_calls() -> list[ast.Call]:
    tree = ast.parse(REGISTRY_SRC.read_text(encoding="utf-8"))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "RuleSpec"
    ]


class TestRegistryNeverDeclaresSeverity:
    def test_scan_is_not_vacuous(self):
        """先证"扫到了东西"，否则下面两条可能是空断言。"""
        calls = _rulespec_calls()
        assert len(calls) >= 20, f"只解析到 {len(calls)} 处 RuleSpec(...) —— 形态变了"

    def test_no_severity_argument(self):
        bad = []
        for c in _rulespec_calls():
            if any(k.arg == "severity" for k in c.keywords if k.arg):
                bad.append(ast.dump(c)[:80])
        assert bad == [], f"注册表仍在传 severity 参数：{bad}"

    def test_positional_shape_is_id_type_description_check(self):
        """位置参数必须恰好 4 个（id/type/description/check）。

        这条防的是"字段删了但参数错位"：多传一个字符串会被当成 `description`，
        然后 `check` 变字符串 —— 报错点在很远的地方，且可能被 except 吞掉。
        """
        shapes = []
        for c in _rulespec_calls():
            ok = (
                len(c.args) == 4
                and isinstance(c.args[0], ast.Constant)
                and isinstance(c.args[1], ast.Constant)
                and isinstance(c.args[2], ast.Constant)
            )
            extra_kw = {k.arg for k in c.keywords if k.arg} - {"enabled_by_default"}
            shapes.append((ok, extra_kw, ast.dump(c)[:60]))
        bad = [(kw, src) for ok, kw, src in shapes if not ok or kw]
        assert bad == [], f"RuleSpec(...) 的参数形态不对：{bad}"


# ─────────────────────────────────────────────────────────────────────────────
# C. 发射侧：severity 字面量必须落在三值词表内
# ─────────────────────────────────────────────────────────────────────────────


class TestEmittedSeverityVocabulary:
    @staticmethod
    def _emitted_literals(value: ast.expr) -> list[str]:
        """`{"severity": <value>}` 里**真的会发出去**的字符串字面量。

        - 遍历整棵子树 ⇒ 三元表达式（`"warn" if notes else "critical"`）也算
          （第一版只认 `Constant`，于是 IfExp 整条被跳过，变异 M3 实测 MISSED）；
        - **不下钻 `Subscript`** ⇒ `{"severity": r["severity"]}` 是**读**不是**写**
          （`llm_checks.py` 的历史回看就有这种形态），把它的键名当成发射值会误报。
        """
        out: list[str] = []

        def walk(node: ast.AST) -> None:
            if isinstance(node, ast.Subscript):
                return                      # 读路径，不是发射值
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
                return
            for child in ast.iter_child_nodes(node):
                walk(child)

        walk(value)
        return out

    def test_literals_are_in_vocabulary(self):
        """`{"severity": ...}` 的值里**所有**字符串字面量都必须在词表内。"""
        literals: list[tuple[str, str]] = []
        dynamic = 0
        bad: list[tuple[str, int, str]] = []
        for f in sorted(RULES_DIR.glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    if not (isinstance(k, ast.Constant) and k.value == "severity"):
                        continue
                    found = self._emitted_literals(v)
                    if not found:
                        dynamic += 1        # `"severity": sev` —— 静态判不了，不冒充已检查
                        continue
                    for val in found:
                        literals.append((f.name, val))
                        if val not in VOCAB:
                            bad.append((f.name, node.lineno, val))
        # 反空转：规则文件里确实存在这些字面量
        assert len(literals) >= 20, (
            f"只找到 {len(literals)} 处 severity 字面量（动态 {dynamic} 处）—— "
            "判据形态变了，本用例可能已退化成空断言"
        )
        assert bad == [], (
            f"发射了词表外的 severity（前端 zh_map / findings-map 都认不出）：{bad}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# D. 行为证据：同一规则、同一 type 可以发两种严重度
# ─────────────────────────────────────────────────────────────────────────────


def _page(n: int, steps: list[dict]) -> dict:
    return {"page": n, "steps": steps, "production_date": "2024-01-01", "raw_html": ""}


def _step(no: int, start: str, end: str) -> dict:
    return {"step_no": no, "start_time": start, "end_time": end, "operation": "操作"}


class TestOneRuleCanEmitTwoSeverities:
    """R1b 是"单一 severity 字段不足以表达"的实证（B1-9 的结论）。

    两条输入的差别只在**是否真的倒序**：真倒序 critical，仅重叠 warning。
    ⇒ 任何"R1b 的严重度 = X"的单一声明都是错的，无论 X 取哪个值。
    """

    def _run(self, start: str, end: str) -> list[dict]:
        from core.rules.rule_time import _check_time_reversal_cross_page as r1b

        pages = [
            _page(9, [_step(2, "2024-01-01 15:00", "2024-01-01 16:00")]),
            _page(10, [_step(3, start, end)]),
        ]
        return [f for f in r1b(pages) if f.get("type") == "time_reversal"]

    def test_true_inversion_is_critical(self):
        found = self._run("2024-01-01 14:30", "2024-01-01 15:30")
        assert found, "真倒序没有产出 finding —— 用例样本失效"
        assert [f["severity"] for f in found] == ["critical"]

    def test_mere_overlap_is_warning(self):
        found = self._run("2024-01-01 15:10", "2024-01-01 15:20")
        assert found, "仅重叠没有产出 finding —— 用例样本失效"
        assert [f["severity"] for f in found] == ["warning"]

    def test_same_type_two_severities(self):
        """同一 type（time_reversal）在两种输入下拿到两种严重度。"""
        a = {f["severity"] for f in self._run("2024-01-01 14:30", "2024-01-01 15:30")}
        b = {f["severity"] for f in self._run("2024-01-01 15:10", "2024-01-01 15:20")}
        assert a == {"critical"} and b == {"warning"}
        assert a | b == {"critical", "warning"}
