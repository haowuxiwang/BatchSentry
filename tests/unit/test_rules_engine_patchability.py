"""`analyze_cross_page` 的 **patch 兼容契约**护栏。

背景（对抗性审查 · 遗留清单 #6）
--------------------------------
`analyze_cross_page` 于 2026-09 从 `core/rules/__init__.py` 迁到
`core/rules/engine.py`（让包 `__init__` 回归"导出面"这一单一职责）。

迁移的**唯一真实风险**：编排体原先住在 `core.rules` 里，它解析的 7 个协作者
都是**包命名空间的全局名**。多处脚本/测试正是按**包路径**打桩：

    patch("core.rules._llm_fallback_check", new=AsyncMock(return_value=[]))
    patch("core.rules._llm_based_check",     new=AsyncMock(return_value=[]))
    patch.object(core.rules, "enabled_rule_specs", new=lambda: [...])

调用方（实测）：`scripts/replay_rules.py`（三个目标全用）、
`scripts/eval_findings.py`、`tests/unit/test_cross_page_entry_coverage.py`、
`tests/unit/test_rule_registry.py`。

若把 `engine.py` 里这 7 个改成**模块级** from-import，桩就**静默失效**：
测试会转而**调用真 LLM**、跑**全部规则** —— 不报错，只是变慢变贵或失败。
这正是"机械替换会让一批测试静默失效"的具体机制。

因此本文件**不满足于**"属性存在"这种空断言，而是对 7 个名字逐一断言
**"打桩后 `analyze_cross_page` 的可观察行为确实改变"** ——
用**哨兵值**做信号：只有真的走了包命名空间，哨兵才会出现在结果里。
"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import core.rules as CR
from core.rules import analyze_cross_page

# ── 契约面：engine.analyze_cross_page 在**调用期**从 core.rules 取的名字 ──
CONTRACT_NAMES = (
    "_normalize_pages",
    "RuleContext",
    "enabled_rule_specs",
    "_collect_per_page_findings",
    "_llm_fallback_check",
    "_build_summary",
    "_llm_based_check",
)

_PAGE = {"page": 1, "data": {}}


def _sentinel(tag: str) -> dict:
    return {"type": f"SENTINEL_{tag}", "page": 1, "severity": "info"}


class _FakeSpec:
    """替身规则：`enabled_rule_specs` 被替换后应被编排体调用。"""

    id = "FAKE-R"
    type = "test"

    def check(self, pages, ctx):
        return [_sentinel("ENABLED_SPECS")]


class _FakeCtx:
    """替身上下文：记录实例化，证明 `RuleContext` 走包命名空间解析。"""

    instances: list = []

    def __init__(self, job_id=""):
        self.job_id = job_id
        self.llm_queue: list = []
        _FakeCtx.instances.append(self)

    def record(self, found):
        pass


def _quiet(stack: ExitStack, *, specs=None, fallback=None, based=None):
    """把 LLM 入口打桩到**包路径**，避免测试触真 LLM。

    三个入口都可单独覆盖：测某个目标时不能同时把它 stub 掉。
    """
    stack.enter_context(patch(
        "core.rules._llm_fallback_check",
        new=fallback if fallback is not None else AsyncMock(return_value=[]),
    ))
    stack.enter_context(patch(
        "core.rules._llm_based_check",
        new=based if based is not None else AsyncMock(return_value=[]),
    ))
    if specs is not None:
        stack.enter_context(
            patch.object(CR, "enabled_rule_specs", new=lambda: specs)
        )


# ══════════════════════════════════════════════════════════════
# 0. 防空转：契约面与结构必须真的成立
# ══════════════════════════════════════════════════════════════
class TestContractSurfaceIsIntact:
    def test_all_contract_names_exist_on_the_package(self):
        missing = [n for n in CONTRACT_NAMES if not hasattr(CR, n)]
        assert not missing, (
            f"core.rules 缺少契约名 {missing} —— engine.py 的调用期 "
            f"`from core.rules import ...` 会抛 ImportError"
        )

    def test_entry_point_actually_moved_to_engine(self):
        """迁移确实发生：函数定义在 core.rules.engine。"""
        assert analyze_cross_page.__module__ == "core.rules.engine", (
            "analyze_cross_page 应定义在 core.rules.engine；"
            f"实际 {analyze_cross_page.__module__}"
        )

    def test_pipeline_patch_anchor_is_the_same_object(self):
        """`core.pipeline.analyze_cross_page` 是**同一个函数对象**。

        数百处 `patch("core.pipeline.analyze_cross_page")` 依赖它；
        若迁移时做了包装（lambda/wrapper），锚点会指向另一个对象，
        桩仍"生效"但拦不住真实调用路径 —— 必须断言**同一性**。
        """
        import core.pipeline as P
        assert CR.analyze_cross_page is P.analyze_cross_page

    def test_backward_compat_shim_still_re_exports(self):
        import core.cross_page_analyzer as shim
        assert shim.analyze_cross_page is CR.analyze_cross_page

    def test_package_init_no_longer_imports_dead_rule_helpers(self):
        """`__init__.py` 不得再累积"意外可用"的私有规则函数。

        编排走注册表（`spec.check`），所以 `_check_*` 在包里**无用**；
        它们只会让 `from core.rules import _check_time_reversal_in_page`
        意外可用（误导性 API）。删除依据：
        `devlogs/_verify/probe_rules_init_dead_imports.py`。

        ⚠️ 用 **AST** 而不是"行首前缀"匹配 —— 后者只能抓到多行括号式
        （`from m import (\n    _check_x,\n)`），抓不到单行式
        （`from m import _check_x`）。两种写法都能让符号泄漏进包命名空间，
        必须都覆盖（变异 M8 首轮即因此漏网）。
        """
        import ast
        init_path = (Path(__file__).resolve().parents[2]
                     / "core" / "rules" / "__init__.py")
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
        leaked = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if (a.asname or a.name).startswith("_check_"):
                        leaked.append(f"{node.module}.{a.name}")
        assert not leaked, (
            f"core/rules/__init__.py 又导入了私有规则函数：{leaked}；"
            "需要它们请从 core.rules.rule_time / rule_doc / rule_spec 导入"
        )


# ══════════════════════════════════════════════════════════════
# 1. 逐一验证：7 个协作者都**经包命名空间调用期解析**
# ══════════════════════════════════════════════════════════════
class TestCollaboratorsResolveThroughThePackage:
    async def test_enabled_rule_specs_is_read_from_the_package(self):
        """把 enabled_rule_specs 换成替身规则 ⇒ 替身的 finding 出现在结果里。"""
        with ExitStack() as stack:
            _quiet(stack, specs=[_FakeSpec()])
            out = await analyze_cross_page([_PAGE], job_id="t")
        assert any(f.get("type") == "SENTINEL_ENABLED_SPECS" for f in out), (
            "enabled_rule_specs 未被包命名空间解析 —— "
            "`patch.object(core.rules, 'enabled_rule_specs')` 会静默失效"
        )

    async def test_llm_fallback_check_is_read_from_the_package(self):
        with ExitStack() as stack:
            _quiet(stack, specs=[],
                   fallback=AsyncMock(return_value=[_sentinel("LLM_FALLBACK")]))
            out = await analyze_cross_page([_PAGE], job_id="t")
        assert any(f.get("type") == "SENTINEL_LLM_FALLBACK" for f in out), (
            "core.rules._llm_fallback_check 打桩失效 —— 测试会转而调用真 LLM"
        )

    async def test_llm_based_check_is_read_from_the_package(self):
        with ExitStack() as stack:
            _quiet(stack, specs=[],
                   based=AsyncMock(return_value=[_sentinel("LLM_BASED")]))
            out = await analyze_cross_page([_PAGE], job_id="t")
        assert any(f.get("type") == "SENTINEL_LLM_BASED" for f in out), (
            "core.rules._llm_based_check 打桩失效 —— 测试会转而调用真 LLM"
        )

    async def test_collect_per_page_findings_is_read_from_the_package(self):
        with ExitStack() as stack:
            _quiet(stack, specs=[])
            stack.enter_context(patch(
                "core.rules._collect_per_page_findings",
                new=lambda pages: [_sentinel("PER_PAGE")],
            ))
            out = await analyze_cross_page([_PAGE], job_id="t")
        assert any(f.get("type") == "SENTINEL_PER_PAGE" for f in out)

    async def test_normalize_pages_is_read_from_the_package(self):
        with ExitStack() as stack:
            _quiet(stack, specs=[])
            mock = MagicMock(return_value=[])
            stack.enter_context(patch("core.rules._normalize_pages", new=mock))
            await analyze_cross_page([_PAGE], job_id="t")
        assert mock.called, (
            "core.rules._normalize_pages 未被调用 —— 打桩失效"
        )
        assert mock.call_args[0][0] == [_PAGE], "应收到原始 page_structures"

    async def test_rule_context_is_read_from_the_package(self):
        _FakeCtx.instances.clear()
        with ExitStack() as stack:
            _quiet(stack, specs=[])
            stack.enter_context(patch("core.rules.RuleContext", new=_FakeCtx))
            await analyze_cross_page([_PAGE], job_id="job-42")
        assert len(_FakeCtx.instances) == 1, (
            "core.rules.RuleContext 未被实例化 —— 打桩失效"
        )
        assert _FakeCtx.instances[0].job_id == "job-42", "job_id 未透传"

    async def test_build_summary_is_read_from_the_package_and_flows_into_llm(
        self,
    ):
        """`_build_summary` 走包命名空间，且其返回值**确实**传给了 `_llm_based_check`。"""
        based = AsyncMock(return_value=[])
        with ExitStack() as stack:
            _quiet(stack, specs=[], based=based)
            stack.enter_context(patch(
                "core.rules._build_summary",
                new=MagicMock(return_value="SENTINEL_SUMMARY"),
            ))
            await analyze_cross_page([_PAGE], job_id="t")
        assert based.called, "_llm_based_check 未被调用，断言无从谈起"
        assert based.call_args[0][0] == "SENTINEL_SUMMARY", (
            "_build_summary 的返回值没有流入 _llm_based_check —— "
            "可能被模块级绑定遮蔽"
        )


# ══════════════════════════════════════════════════════════════
# 2. 信号有效性：哨兵**不**打桩时不得出现
# ══════════════════════════════════════════════════════════════
class TestSentinelsAreRealSignals:
    async def test_sentinels_absent_without_patching(self):
        """未打桩 ⇒ 三个哨兵一个都不该出现。

        否则上面的断言可能被"本来就有"的输出蒙对（空断言）。
        """
        with ExitStack() as stack:
            _quiet(stack, specs=[])
            out = await analyze_cross_page([_PAGE], job_id="t")
        tags = {f.get("type") for f in out}
        for t in ("SENTINEL_ENABLED_SPECS", "SENTINEL_LLM_FALLBACK",
                  "SENTINEL_LLM_BASED", "SENTINEL_PER_PAGE"):
            assert t not in tags, f"{t} 在未打桩时也出现了 —— 断言无效"

    async def test_real_rule_layer_produces_findings_without_stubbing_specs(self):
        """不打桩 `enabled_rule_specs` 时，注册表**确实**提供了规则。

        这条是防空转：若注册表为空，"替换成替身规则"的断言会变得没有意义
        （真假两种情况都只看到替身的输出）。
        """
        assert CR.enabled_rule_specs(), "注册表为空 —— 上面的替身断言失去对照"
