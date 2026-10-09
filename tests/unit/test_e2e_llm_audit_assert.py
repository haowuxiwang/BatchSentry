"""e2e driver 的"LLM 真被调用且成功"断言护栏。

背景（2026-10-09 实测缺口）：``e2e_run.py`` 全文**零处**引用 ``llm_audit``。
而**终态绿不蕴含 LLM 跑通** —— 产品在 LLM 失败时**按设计降级**（把"LLM 调用
失败"写成 finding）并照常走到 ``review``。只断言终态的轮次会把"LLM 从未成功"
记成通过（``docs/TODO.md`` B11-11 第一次翻车就是这种假绿）。冻结冒烟
``tests/e2e_frozen.py`` 早已用 ``entries[].success`` 当权威判据 —— 本文件把
同一判据补到 **driver 侧**，两边证据都取**产品自己的记录**，不另起一套真值。

本文件锁住五件事（**全部只针对 ``e2e_run.py``，不看本文件自身**）：

1. :func:`e2e_run.llm_audit_verdict` 的判定语义（含 fail-closed 反例）；
2. ``run_upload`` 确实接入判定，且**判定真的 gate 住 ``ok``**；
3. 主结果字典带 ``llm_calls / llm_ok / llm_failed / llm_note`` 四个键；
4. 开关走**单一真值** ``tests.e2e_coverage.REQUIRE_LLM_ENV``（不得写字面量、
   不得自造 ``bool(os.environ.get(...))``）；
5. 判定在**共享 helper 内**计算 ⇒ 所有轮次自动继承；且包装轮次**不得**把
   ``ok`` 覆盖成"没有 AND 上原值"的表达式（否则会静默丢掉判定）。

**为什么不照抄 ``expect_backend`` 的"每个调用点都必须显式声明"级联检查**：
``expect_backend`` 的默认值 ``None`` 语义是**不校验** —— 漏传 = 静默漏洞，故必须
逐点声明；而 ``require_llm`` 的默认值 ``None`` 语义是**回落全局开关**
（``PBC_E2E_REQUIRE_LLM``）—— 漏传 ≠ 漏洞，开关照样生效。强行要求逐调用声明会
**架空**"开关是唯一全局闸门"的设计，是一条**虚有其表**的护栏（本项目纪律：
"用例存在" ≠ "断言有效"）。真正要防的是"判定被搬出共享 helper / 被包装轮次丢掉"，
故用第 5 条的 **AST 结构约束**而不是"有没有传参"的文本匹配。
"""
import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from e2e_run import llm_audit_verdict  # noqa: E402
from tests.e2e_coverage import REQUIRE_LLM_ENV  # noqa: E402

_DRIVER = _ROOT / "e2e_run.py"


def _src() -> str:
    return _DRIVER.read_text(encoding="utf-8")


def _tree() -> ast.AST:
    return ast.parse(_src())


def _run_upload() -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree())
                if isinstance(n, ast.FunctionDef) and n.name == "run_upload")


# ── 0. 防空转：锚点必须真的落在 driver 上 ────────────────────────────


def test_guard_reads_the_real_driver():
    """路径/锚点写错会让下面所有断言**恒真**（读不到 = 没检查）。

    这是本项目"反空转"纪律的落点：护栏自己也要能证明它在看真东西。
    """
    assert _DRIVER.is_file(), f"找不到 {_DRIVER}"
    assert _DRIVER.resolve().parent == _ROOT, "锚点指到了 driver 之外"
    assert len(_src()) > 10_000, "读到的不像 e2e_run.py"


# ── 1. 判定语义（纯函数） ─────────────────────────────────────────────


def test_missing_audit_endpoint_is_fail_closed_regardless_of_switch():
    """取不到审计端点 ⇒ **判不了 ≠ 已验**，与开关无关（核心反例）。"""
    for flag in (True, False):
        ok, note, detail = llm_audit_verdict(None, flag)
        assert ok is False, f"require_llm={flag} 时端点取不到竟放行"
        assert "fail-closed" in note
        assert detail == {"llm_calls": None, "llm_ok": None, "llm_failed": None}


def test_no_successful_call_fails_when_required():
    entries = [{"success": 0, "error": "402 account balance is insufficient"}]
    ok, note, detail = llm_audit_verdict(entries, True)
    assert ok is False
    assert "402" in note, "首条错误必须回显，否则归因无从下手"
    assert detail == {"llm_calls": 1, "llm_ok": 0, "llm_failed": 1}


def test_empty_audit_table_fails_when_required():
    """``entries == []``（确实一条都没调用）≠ 通过 —— 与"端点取不到"是两种事实。"""
    ok, _note, detail = llm_audit_verdict([], True)
    assert ok is False
    assert detail == {"llm_calls": 0, "llm_ok": 0, "llm_failed": 0}


def test_no_successful_call_is_only_recorded_when_not_required():
    """弱环境（无凭据的日常跑）只**记录**不判红。"""
    ok, note, detail = llm_audit_verdict([{"success": 0}], False)
    assert ok is True
    assert "未要求 LLM" in note
    assert detail["llm_ok"] == 0 and detail["llm_failed"] == 1


def test_successful_call_passes_and_counts_failures():
    entries = [{"success": 1}, {"success": 0}, {"success": 1}]
    ok, note, detail = llm_audit_verdict(entries, True)
    assert ok is True
    assert detail == {"llm_calls": 3, "llm_ok": 2, "llm_failed": 1}
    assert "2/3" in note


def test_success_flag_is_read_truthily():
    """``success`` 由产品写库（``1`` / ``True`` 都可能）⇒ 真值判断，别写 ``is 1``。"""
    ok, _note, detail = llm_audit_verdict([{"success": True}], True)
    assert ok is True and detail["llm_ok"] == 1


# ── 2. 接入（结构级：AST，不看注释/文档串） ──────────────────────────


def test_verdict_is_computed_inside_the_shared_helper():
    """判定必须在 ``run_upload`` 内 ⇒ 所有轮次自动继承，不靠各轮自觉。"""
    called = {c.func.id for c in ast.walk(_run_upload())
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "llm_audit_verdict" in called, (
        "run_upload 内必须调用 llm_audit_verdict —— 否则判定不覆盖所有轮次"
    )


def test_verdict_actually_gates_ok():
    """``if not llm_ok: ok = False`` 必须以**真形状**存在（不是注释里写写）。"""
    found = False
    for node in ast.walk(_run_upload()):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if not (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)
                and isinstance(t.operand, ast.Name) and t.operand.id == "llm_ok"):
            continue
        for sub in node.body:
            if (isinstance(sub, ast.Assign)
                    and isinstance(sub.value, ast.Constant)
                    and sub.value.value is False
                    and any(isinstance(tg, ast.Name) and tg.id == "ok"
                            for tg in sub.targets)):
                found = True
    assert found, "llm_ok 判定没有 gate 住 ok ⇒ 终态绿仍会被记成'LLM 已验'"


def test_result_dict_carries_llm_evidence():
    """主结果字典必须带 llm_* 四键（报告要能一眼看到 LLM 到底跑没跑）。

    用 **AST 取字典键**而不是文本搜 ``"llm_ok"`` —— 后者会命中
    ``llm_audit_verdict`` 内部的 detail 构造，**删掉结果字典那一行也照样绿**。
    """
    keys = None
    for node in ast.walk(_run_upload()):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            ks = {k.value for k in node.value.keys
                  if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "job_id" in ks:          # 主结果字典（上传失败时的早退字典没有它）
                keys = ks
    assert keys is not None, "找不到 run_upload 的主结果字典（含 job_id 的那个 return）"
    for k in ("llm_calls", "llm_ok", "llm_failed", "llm_note"):
        assert k in keys, f"主结果字典缺 {k!r}"


def test_wrappers_do_not_clobber_the_ok_gate():
    """包装轮次不得把 ``X["ok"]`` 覆盖成"没 AND 上原值"的表达式。

    实测形态：``run_dual`` / ``run_rot`` 都是 ``bool(res.get("ok")) and ...``；
    若改成 ``True and ...`` 就会把 ``run_upload`` 里的 LLM / 后端判定**静默丢掉**
    —— 终态照样绿，报告照样出，只是判定没了。
    """
    bad = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "ok"):
            continue
        v = node.value
        if isinstance(v, ast.Constant) and v.value is False:
            continue                                  # 只降不升 —— 安全
        if (isinstance(v, ast.BoolOp) and isinstance(v.op, ast.And)
                and any(isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "get" for c in ast.walk(v))):
            continue                                  # `... and res.get("ok") ...`
        bad.append((node.lineno, ast.unparse(v)[:70]))
    assert not bad, (
        f"以下行号把 ok 覆盖成不带原值的表达式：{bad}\n"
        "包装轮次必须 `bool(res.get('ok')) and ...`，否则会静默丢掉共享判定。"
    )


# ── 3. 单一真值：开关只能来自 tests.e2e_coverage ─────────────────────


def test_switch_is_imported_not_literalised():
    src = _src()
    assert "from tests.e2e_coverage import REQUIRE_LLM_ENV, env_flag" in src, (
        "开关必须从 tests/e2e_coverage 导入 —— 那是唯一实现（含 '0'/'false' 语义）"
    )
    lits = [n.lineno for n in ast.walk(_tree())
            if isinstance(n, ast.Constant) and n.value == REQUIRE_LLM_ENV]
    assert not lits, (
        f"第 {lits} 行出现开关名**字面量**（{REQUIRE_LLM_ENV!r}）—— "
        "必须引用 REQUIRE_LLM_ENV，否则两处必然漂移"
    )


def test_require_llm_defaults_to_the_env_switch():
    """``require_llm = env_flag(REQUIRE_LLM_ENV)`` —— "开关是唯一闸门"的落点。"""
    found = False
    for node in ast.walk(_run_upload()):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tg = node.targets[0]
        if not (isinstance(tg, ast.Name) and tg.id == "require_llm"):
            continue
        v = node.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                and v.func.id == "env_flag" and len(v.args) == 1
                and isinstance(v.args[0], ast.Name)
                and v.args[0].id == "REQUIRE_LLM_ENV"):
            found = True
    assert found, (
        "run_upload 内必须有 `require_llm = env_flag(REQUIRE_LLM_ENV)` "
        "—— 否则开关形同虚设"
    )


def test_no_hand_rolled_truthy_parsing():
    """不得自造 ``bool(os.environ.get(...))`` —— ``"0"``/``"false"`` 会被读成 True。

    ⚠️ 只看 **AST**：源码文本里有一段**引用该反例**的注释（``e2e_run.py`` 顶部），
    文本匹配会当场假红 —— 本项目已为此误报过（Round 25 / Round 29）。
    """
    hits = []
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "bool"):
            continue
        for a in node.args:
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr == "get"
                    and isinstance(a.func.value, ast.Attribute)
                    and a.func.value.attr == "environ"):
                hits.append(node.lineno)
    assert not hits, f"第 {hits} 行自造 bool(os.environ.get(...)) —— 请用 env_flag()"
