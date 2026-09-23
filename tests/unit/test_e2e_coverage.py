"""B9-7 覆盖清单的护栏：``skipped`` 不得被读成 ``covered``，硬要求必须真的能卡住。

为什么这些断言值得单独存在（2026-09-21 B9-7）：
"因环境跳过"与"真的验过"在**报告上长得一样**（都是一条没失败的记录），
于是"关键路径从未运行"的发版冒烟可以冒充"全绿"。本文件把三条纪律钉死：

  1. 缺凭据 ⇒ 只能 ``skipped``，**任何路径都不得**升级成 ``covered``；
  2. ``skipped`` / 缺项 ⇒ 开了硬要求就**必须**产生 gap（并让退出码非 0）；
  3. 硬要求**默认关闭** ⇒ 日常弱环境冒烟不能被它误红。

另含两条**结构性**护栏（AST 而非文案扫描）：
  * e2e 驱动里 ``llm_provider`` 的取值不得是字符串字面量（必须派生）；
  * ``e2e_frozen.py`` 必须真的**调用**覆盖清单入口（不是只在注释里提到）。
"""
import ast
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tests.e2e_coverage import (  # noqa: E402
    ENTRY_LLM_CONFIG, ENTRY_LLM_PIPELINE, ENTRY_OCR_CONFIG, ENTRY_OCR_PIPELINE,
    REQUIRE_LLM_ENV, STATUS_COVERED, STATUS_FAILED, STATUS_SKIPPED,
    Coverage, classify_pipeline, env_flag, exit_code, required_gaps,
)

_ALL = (ENTRY_LLM_CONFIG, ENTRY_LLM_PIPELINE, ENTRY_OCR_CONFIG, ENTRY_OCR_PIPELINE)


# ── 纪律 1：skipped ≠ covered ───────────────────────────────────────


def test_skipped_is_not_covered():
    cov = Coverage(path=Path("unused.json"))
    cov.record(ENTRY_LLM_PIPELINE, STATUS_SKIPPED, "没凭据")
    assert cov.status(ENTRY_LLM_PIPELINE) == STATUS_SKIPPED
    assert cov.status(ENTRY_LLM_PIPELINE) != STATUS_COVERED
    assert cov.counts()[STATUS_COVERED] == 0


def test_missing_key_never_reads_as_covered():
    """没给凭据时，哪怕流水线走到了成功终态，LLM 链路也只能记 skipped。

    这是本模块存在的理由：成功的终态**不蕴含** LLM 被调用过（Stage 2 在无
    凭据时走降级），二者必须分开记账。
    """
    got = classify_pipeline("review", llm_ready=False, ocr_ready=True,
                            ocr_backend_used="mineru")
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_SKIPPED
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_COVERED
    assert "无法断定" in got[ENTRY_LLM_PIPELINE][1]


def test_unknown_entry_defaults_to_missing_not_covered():
    cov = Coverage(path=Path("unused.json"))
    assert cov.status("never_recorded") == "missing"
    assert cov.status("never_recorded") != STATUS_COVERED


# ── 纪律 2：硬要求必须真能卡住 ──────────────────────────────────────


def test_require_llm_on_and_skipped_produces_gap():
    cov = Coverage(path=Path("unused.json"))
    cov.record(ENTRY_LLM_CONFIG, STATUS_SKIPPED, "没凭据")
    cov.record(ENTRY_LLM_PIPELINE, STATUS_SKIPPED, "没凭据")
    gaps = required_gaps(cov, require_llm=True)
    assert len(gaps) == 2, gaps
    assert all("skipped" in g for g in gaps)
    assert exit_code(0, gaps) == 1, "开了硬要求却退出 0 —— 等于没卡住"


def test_require_llm_on_and_covered_produces_no_gap():
    cov = Coverage(path=Path("unused.json"))
    cov.record_many(classify_pipeline(
        "partial_review", llm_ready=True, ocr_ready=True,
        llm_call_succeeded=True, ocr_backend_used="mineru"))
    cov.record(ENTRY_LLM_CONFIG, STATUS_COVERED, "凭据被接受")
    assert required_gaps(cov, require_llm=True) == []
    assert exit_code(0, []) == 0


def test_require_llm_covers_two_entries_config_and_pipeline():
    """硬要求是**两条**：光配上凭据（config）或光有成功终态（pipeline）都不够。

    只查其中一条会留下一个洞：凭据配上了但流水线从未跑过 ⇒ 记为
    "LLM 已覆盖"，而实际上一行 LLM 代码都没被执行。
    """
    # 只覆盖 pipeline（模拟"凭据未记录"）⇒ 仍应产生 gap
    cov = Coverage(path=Path("unused.json"))
    cov.record_many(classify_pipeline(
        "review", llm_ready=True, ocr_ready=True,
        llm_call_succeeded=True, ocr_backend_used="mineru"))
    gaps = required_gaps(cov, require_llm=True)
    assert len(gaps) == 1 and "llm_config" in gaps[0], gaps

    # 只覆盖 config（模拟"流水线没跑"）⇒ 也应产生 gap
    cov2 = Coverage(path=Path("unused.json"))
    cov2.record(ENTRY_LLM_CONFIG, STATUS_COVERED, "凭据被接受")
    gaps2 = required_gaps(cov2, require_llm=True)
    assert len(gaps2) == 1 and "llm_pipeline" in gaps2[0], gaps2


def test_require_llm_on_and_failed_produces_gap():
    cov = Coverage(path=Path("unused.json"))
    cov.record_many(classify_pipeline("error", llm_ready=True, ocr_ready=True))
    gaps = required_gaps(cov, require_llm=True)
    assert any("failed" in g for g in gaps), gaps


def test_require_llm_on_but_unrecorded_produces_gap():
    """一条都没记（例如脚本提前崩了）也必须算未覆盖 —— 不能"没记录=没问题"。"""
    gaps = required_gaps(Coverage(path=Path("unused.json")), require_llm=True)
    assert len(gaps) == 2, gaps
    assert all("missing" in g for g in gaps)


# ── 纪律 3：硬要求默认关闭 ──────────────────────────────────────────


def test_require_llm_off_by_default(monkeypatch):
    monkeypatch.delenv(REQUIRE_LLM_ENV, raising=False)
    cov = Coverage(path=Path("unused.json"))
    cov.record(ENTRY_LLM_PIPELINE, STATUS_SKIPPED, "没凭据")
    assert required_gaps(cov) == [], "硬要求默认开启会把弱环境冒烟误红"
    assert exit_code(0, []) == 0


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " y "])
def test_env_flag_truthy(value, monkeypatch):
    monkeypatch.setenv(REQUIRE_LLM_ENV, value)
    assert env_flag(REQUIRE_LLM_ENV) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2"])
def test_env_flag_falsy(value, monkeypatch):
    """⚠️ ``"0"`` / ``"false"`` 必须是 False。

    用 ``bool(os.environ.get(...))`` 会把它们读成 True —— 一个"看起来能关
    其实关不掉"的静默陷阱（关闭开关无效 = 弱环境冒烟恒红，人就会开始无视它）。
    """
    monkeypatch.setenv(REQUIRE_LLM_ENV, value)
    assert env_flag(REQUIRE_LLM_ENV) is False


def test_require_llm_env_actually_drives_required_gaps(monkeypatch):
    cov = Coverage(path=Path("unused.json"))
    cov.record_many(classify_pipeline("review", llm_ready=False, ocr_ready=True))
    monkeypatch.setenv(REQUIRE_LLM_ENV, "1")
    assert required_gaps(cov), "环境变量置 1 后仍未产生 gap"
    monkeypatch.setenv(REQUIRE_LLM_ENV, "0")
    assert required_gaps(cov) == []


# ── classify_pipeline：事实 → 状态 ──────────────────────────────────


def test_classify_success_with_both_credentials_covers_both():
    """**凭据齐备 ≠ 链路已验** —— 必须同时给出各链路的权威证据。"""
    got = classify_pipeline("review", llm_ready=True, ocr_ready=True,
                            llm_call_succeeded=True, ocr_backend_used="mineru")
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_COVERED
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_COVERED


# ── 🔴 回归护栏：真实事故「成功终态掩盖链路失败」（2026-09-23）──────────
#
# 事故经过：一把 SiliconFlow key **账户余额耗尽**（`402 code=30001
# account balance is insufficient`）。逐页分析先成功、跨页 LLM 被拒；产品
# **按设计降级**（把"LLM 调用失败"写成 severity=info/warning 的 finding），
# 流水线照常走到 `review`。旧判据只看 `terminal == review` ⇒ 记 `covered`
# ⇒ `PBC_E2E_REQUIRE_LLM=1` 下**退出码 0** —— 发版门禁被假绿放行。
#
# 这三条用例钉住"终态绿但链路没成功"必须**红**，且必须能被 REQUIRED 开关兜住。


def test_classify_terminal_green_but_llm_never_succeeded_is_failed():
    """`review` + 凭据齐备 + **审计表里没有成功调用** ⇒ 必须 failed（不是 covered）。"""
    got = classify_pipeline("review", llm_ready=True, ocr_ready=True,
                            llm_call_succeeded=False, ocr_backend_used="mineru")
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_FAILED, got[ENTRY_LLM_PIPELINE]
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_COVERED, "OCR 侧不受影响"
    assert "降级" in got[ENTRY_LLM_PIPELINE][1]


def test_classify_missing_llm_evidence_is_fail_closed():
    """证据**取不到**（None）不得当成已验 —— 判不了 ⇒ failed。"""
    got = classify_pipeline("review", llm_ready=True, ocr_ready=True,
                            ocr_backend_used="mineru")
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_FAILED, got[ENTRY_LLM_PIPELINE]
    assert "fail-closed" in got[ENTRY_LLM_PIPELINE][1]


def test_classify_missing_or_empty_ocr_backend_is_failed():
    """OCR：没记录真实后端（空串/None）⇒ failed；且空白串不得被当成有值。"""
    for backend in (None, "", "   "):
        got = classify_pipeline("review", llm_ready=True, ocr_ready=True,
                                llm_call_succeeded=True, ocr_backend_used=backend)
        assert got[ENTRY_OCR_PIPELINE][0] == STATUS_FAILED, (backend, got)
        assert got[ENTRY_LLM_PIPELINE][0] == STATUS_COVERED


def test_require_llm_gap_when_terminal_green_but_llm_degraded(monkeypatch):
    """端到端口径：真实事故场景下 `PBC_E2E_REQUIRE_LLM=1` **必须**产生 gap、
    退出码必须为 1 —— 这正是当年被放行的那一次。"""
    cov = Coverage(path=Path("unused.json"))
    cov.record(ENTRY_LLM_CONFIG, STATUS_COVERED, "凭据被写入（余额不足，但写入成功）")
    cov.record_many(classify_pipeline(
        "review", llm_ready=True, ocr_ready=True,
        llm_call_succeeded=False, ocr_backend_used="mineru"))
    monkeypatch.setenv(REQUIRE_LLM_ENV, "1")
    gaps = required_gaps(cov)
    assert any("llm_pipeline" in g for g in gaps), gaps
    assert exit_code(0, gaps) == 1, "终态绿但 LLM 降级 ⇒ 退出码必须非 0"


def test_classify_error_with_credentials_marks_failed():
    got = classify_pipeline("error", llm_ready=True, ocr_ready=True)
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_FAILED
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_FAILED


def test_classify_timeout_is_failed_not_skipped():
    """未达终态（超时）**不得**被读成"环境跳过" —— 那是把"没跑"混进"跑了没成"。"""
    got = classify_pipeline("", llm_ready=True, ocr_ready=True)
    for entry in (ENTRY_LLM_PIPELINE, ENTRY_OCR_PIPELINE):
        assert got[entry][0] == STATUS_FAILED, entry


def test_classify_distinguishes_no_job_from_timeout():
    """"作业从未建立"与"作业跑了没跑完"同属失败，但**归因必须分开**。

    2026-09-21 实测：产品在未配置 LLM 时直接 400 拒绝上传 ⇒ 作业根本没建立，
    而旧 reason 写成"未在预算内到达终态"，把排查方向引向超时（错误归因）。
    """
    no_job = classify_pipeline("", llm_ready=True, ocr_ready=True, upload_failed=True)
    timeout = classify_pipeline("", llm_ready=True, ocr_ready=True)
    assert no_job[ENTRY_LLM_PIPELINE][0] == STATUS_FAILED, "仍是 fail-closed"
    assert "从未建立" in no_job[ENTRY_LLM_PIPELINE][1]
    assert (no_job[ENTRY_LLM_PIPELINE][1] != timeout[ENTRY_LLM_PIPELINE][1]), \
        "两种「没跑」的归因被混为一谈"


def test_classify_cancelled_is_skipped_on_both():
    got = classify_pipeline("cancelled", llm_ready=True, ocr_ready=True)
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_SKIPPED
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_SKIPPED


def test_classify_covers_every_entry_with_a_legal_status():
    """表驱动：任意终态 × 凭据组合 × **证据组合**都必须给出合法状态且覆盖两条条目。

    证据维度不可省 —— 它正是本轮的修复点：漏传证据必须走 fail-closed，
    而不是悄悄退回旧行为（"终态绿即覆盖"）。
    """
    for terminal in ("review", "partial_review", "error", "cancelled", "", "weird"):
        for llm in (True, False):
            for ocr in (True, False):
                for llm_ev in (True, False, None):
                    for ocr_ev in ("mineru", "", None):
                        got = classify_pipeline(
                            terminal, llm_ready=llm, ocr_ready=ocr,
                            llm_call_succeeded=llm_ev, ocr_backend_used=ocr_ev)
                        assert set(got) == {ENTRY_LLM_PIPELINE, ENTRY_OCR_PIPELINE}, terminal
                        for entry, (status, reason, _who) in got.items():
                            assert status in (STATUS_COVERED, STATUS_SKIPPED, STATUS_FAILED)
                            assert reason, f"{terminal}/{entry} 缺 reason（不可审计）"


def test_classify_covered_implies_evidence_was_present():
    """**反身判据**：`covered` 只可能出现在证据齐备的那些组合里。

    这条比逐例断言更强：它把"covered ⇒ 有证据"变成一条不变量，
    将来新增分支若绕过证据，会在这里被抓住。
    """
    for terminal in ("review", "partial_review", "error", "cancelled", "", "weird"):
        for llm_ev in (True, False, None):
            for ocr_ev in ("mineru", "", None):
                got = classify_pipeline(
                    terminal, llm_ready=True, ocr_ready=True,
                    llm_call_succeeded=llm_ev, ocr_backend_used=ocr_ev)
                if got[ENTRY_LLM_PIPELINE][0] == STATUS_COVERED:
                    assert llm_ev is True, f"{terminal}: LLM covered 但证据={llm_ev}"
                if got[ENTRY_OCR_PIPELINE][0] == STATUS_COVERED:
                    assert (ocr_ev or "").strip(), f"{terminal}: OCR covered 但证据={ocr_ev!r}"


# ── 落盘 ────────────────────────────────────────────────────────────


def test_coverage_write_and_read_back(tmp_path):
    cov = Coverage(path=tmp_path / "deep" / "cov.json")
    cov.record_many(classify_pipeline(
        "review", llm_ready=True, ocr_ready=False, llm_call_succeeded=True))
    out = cov.write()
    assert out.is_file(), "父目录未自动创建"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"][STATUS_COVERED] == 1
    assert data["items"][ENTRY_LLM_PIPELINE]["status"] == STATUS_COVERED
    assert data["items"][ENTRY_OCR_PIPELINE]["status"] == STATUS_SKIPPED
    assert data["items"][ENTRY_OCR_PIPELINE]["meaning"], "落盘需自解释"


def test_coverage_write_failure_does_not_raise(tmp_path):
    """记账失败不得反过来打断冒烟（否则一个只读磁盘会掩盖真结论）。"""
    target = tmp_path / "as_a_dir"
    target.mkdir()
    Coverage(path=target).write()  # 目标是目录 ⇒ 写失败，但必须静默返回


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        Coverage(path=Path("unused.json")).record("k", "verified")


def test_last_write_wins():
    cov = Coverage(path=Path("unused.json"))
    cov.record(ENTRY_LLM_PIPELINE, "skipped", "先验的猜测")
    cov.record(ENTRY_LLM_PIPELINE, STATUS_COVERED, "终局事实")
    assert cov.status(ENTRY_LLM_PIPELINE) == STATUS_COVERED


# ── 结构性护栏（AST，不查字面量文案）────────────────────────────────


def _dict_string_literal_values(src: str, key: str) -> list[int]:
    """返回源码中 `{key: "<字符串字面量>"}` 出现的行号。"""
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == key
                    and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                hits.append(node.lineno)
    return hits


def _e2e_drivers():
    for p in sorted((_ROOT / "tests").glob("e2e_*.py")):
        if p.name in {"e2e_proc.py", "e2e_coverage.py", "e2e_status_js.py"}:
            continue
        yield p


def test_llm_provider_is_never_hardcoded_in_drivers():
    """``llm_provider`` 的取值必须是**派生的**，不得是字符串字面量。

    为什么用 AST 而不是 grep：grep 会在注释里命中（本仓库的注释**刻意**引用了
    那个错误写法作为反例），把说明文字当成缺陷报；AST 只看真实的 dict 字面量。
    """
    offenders = []
    for p in _e2e_drivers():
        for line in _dict_string_literal_values(
                p.read_text(encoding="utf-8", errors="replace"), "llm_provider"):
            offenders.append(f"{p.relative_to(_ROOT).as_posix()}:{line}")
    assert not offenders, (
        "以下位置把 LLM 提供方写死成字面量（应改用 tests.e2e_proc.llm_provider()）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def test_guard_detects_a_hardcoded_provider():
    """阳性对照：证明上面的护栏**真的会报**，而不是因找不到文件而空转。"""
    src = 'requests.post(u, json={"llm_provider": "deepseek", "deepseek_api_key": k})'
    assert _dict_string_literal_values(src, "llm_provider"), "护栏对写死写法零反应"
    # 反例：派生写法不得被误报
    ok = 'json={"llm_provider": _prov, f"{_prov}_api_key": k}'
    assert not _dict_string_literal_values(ok, "llm_provider"), "误报派生写法"


def _called_names(src: str) -> set:
    return {n.func.id for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_frozen_driver_actually_wires_the_coverage_ledger():
    """冻结包冒烟必须**真的调用**覆盖清单入口，而不是只在注释里提到它。

    仅断言"import 了"不够：import 之后不调用、或调用后不用其结果决定退出码，
    清单就会退化成一份没人看的附件（本轮要防的正是"记录了但不生效"）。
    """
    src = (_ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")
    called = _called_names(src)
    for fn in ("classify_pipeline", "required_gaps", "exit_code"):
        assert fn in called, f"e2e_frozen.py 未调用 {fn}（覆盖清单未真正接线）"
    assert "Coverage" in src, "e2e_frozen.py 未构造 Coverage 实例"
    # 退出码必须由 exit_code(...) 决定，不得退回 `1 if failed else 0`
    assert "sys.exit(exit_code(" in src, "退出码未接入覆盖硬要求"


def _string_constants(src: str) -> set:
    """源码里出现过的字符串**字面量**（注释与说明文字不计入 —— 只看真实代码）。"""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


def test_no_driver_reads_the_legacy_key_env_directly():
    """命名债（B9-7）：驱动不得在代码里直接引用已弃用的旧变量名。

    这只是"过渡期别再扩散"的护栏 —— 旧名的兼容读取**唯一**发生在
    ``tests.e2e_proc.llm_key()`` 里，驱动一律走该函数。
    用 AST 取字符串字面量：注释里说明改名历史（本仓库刻意保留）不算违规。
    """
    offenders = []
    for p in _e2e_drivers():
        if "PBC_E2E_DEEPSEEK_KEY" in _string_constants(
                p.read_text(encoding="utf-8", errors="replace")):
            offenders.append(p.relative_to(_ROOT).as_posix())
    assert not offenders, (
        "以下驱动仍在代码里直接引用已弃用的 PBC_E2E_DEEPSEEK_KEY"
        "（改用 tests.e2e_proc.LLM_KEY_ENV / llm_key_env_display()）："
        + "\n" + "\n".join(f"  - {o}" for o in offenders)
    )


def test_legacy_env_name_guard_has_positive_control():
    """阳性对照：证明上一条护栏不是空转。"""
    assert "PBC_E2E_DEEPSEEK_KEY" in _string_constants(
        'k = os.environ["PBC_E2E_DEEPSEEK_KEY"]')
    # 注释/文档里的历史说明不得被判违规
    assert "PBC_E2E_DEEPSEEK_KEY" not in _string_constants(
        "# 旧名 PBC_E2E_DEEPSEEK_KEY 已弃用\nk = llm_key()")


# ── 🔴 链路证据的**接线**护栏（2026-09-23 假绿事故的配套）────────────
#
# 判据修好了，还要保证驱动**真的把证据传进去**、且证据来自**产品的审计记录**
# 而不是由终态反推。判据侧默认 fail-closed，所以"漏传"是安全的（会红）；
# 但"传错"（拿 terminal/status 当证据）会让修复形同虚设 —— 这一组钉住它。


def _call_nodes(src: str, func_name: str) -> list:
    """所有 ``func_name(...)`` 的调用节点（要连同其关键字**取值**一起看）。"""
    return [n for n in ast.walk(ast.parse(src))
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == func_name)]


def _keyword_args(src: str, func_name: str) -> list:
    """每个 ``func_name(...)`` 调用点的关键字参数名集合。"""
    return [{k.arg for k in c.keywords if k.arg} for c in _call_nodes(src, func_name)]


def _is_upload_failed_call(call) -> bool:
    """该调用点是否显式声明 ``upload_failed=True``（作业从未建立那条路径）。

    这条路径下终态恒为空串，判据在 :func:`classify_pipeline` 开头就返回
    "两侧 failed"，**根本不看链路证据** ⇒ 要求它传证据是无意义的
    （而且此处的 ``_llm_ok`` 变量尚未定义）。
    """
    for k in call.keywords:
        if (k.arg == "upload_failed" and isinstance(k.value, ast.Constant)
                and k.value.value is True):
            return True
    return False


def _assign_rhs_sources(src: str, name: str) -> list:
    """所有 ``name = <expr>`` 的右值源码文本。"""
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(ast.unparse(n.value))
    return out


def _evidence_wiring_problems(src: str) -> list:
    """返回接线缺陷清单（空 = 合格）。抽成函数以便阳性对照直接喂合成源码。

    规则：**断言了真实终态的调用点**（即没有 ``upload_failed=True`` 的那些）
    必须把两条链路的证据传进去；证据必须由事实**推导**，不得由终态/状态反推。
    """
    problems: list[str] = []
    calls = _call_nodes(src, "classify_pipeline")
    if not calls:
        return ["找不到 classify_pipeline 调用 ⇒ 护栏空转"]
    live = [c for c in calls if not _is_upload_failed_call(c)]
    if not live:
        return ["所有调用点都标了 upload_failed=True ⇒ 护栏空转"]
    for c in live:
        kw = {k.arg for k in c.keywords if k.arg}
        for key in ("llm_call_succeeded", "ocr_backend_used"):
            if key not in kw:
                problems.append(
                    f"第 {c.lineno} 行未传 {key}=（判据会 fail-closed，但覆盖率也随之丢失）")
    rhs = _assign_rhs_sources(src, "_llm_ok")
    if not rhs:
        problems.append("没有给 _llm_ok 赋值")
    elif all("success" not in s for s in rhs if s != "None"):
        problems.append(f"_llm_ok 的取值未引用 success 字段：{rhs}")
    for key in ("_llm_ok", "_ocr_backend"):
        for s in _assign_rhs_sources(src, key):
            if s == "None":
                continue
            if "terminal" in s or "status" in s:
                problems.append(f"{key} 由终态/状态推导（{s}）—— 正是假绿事故的成因")
    return problems


def test_frozen_driver_wires_link_evidence_into_the_judge():
    src = (_ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")
    problems = _evidence_wiring_problems(src)
    assert not problems, "驱动未正确接线链路证据：\n" + "\n".join(f"  - {p}" for p in problems)


def test_frozen_driver_reads_llm_evidence_from_the_product_audit_endpoint():
    """LLM 证据必须来自**产品的审计端点**，不另起第二份真值。"""
    src = (_ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")
    assert "/llm_audit" in _string_constants(src) or "/llm_audit" in src, \
        "驱动未读取 /api/jobs/{id}/llm_audit"


def test_evidence_wiring_guard_has_positive_control():
    """阳性对照：证明接线护栏**真的会报**，而不是因解析失败而空转。"""
    bad = (
        'COV.record_many(classify_pipeline(t, llm_ready=True, ocr_ready=True))\n'
        '_llm_ok = terminal == "review"\n'
    )
    problems = _evidence_wiring_problems(bad)
    assert problems, "护栏对「只传终态」的写法零反应"
    assert any("未传" in p for p in problems), problems
    assert any("终态" in p or "success" in p for p in problems), problems

    good = (
        'r = requests.get(f"{BASE}/api/jobs/{jid}/llm_audit")\n'
        '_llm_ok = any(e.get("success") for e in r.json()["entries"])\n'
        '_ocr_backend = requests.get(u).json().get("ocr_backend_used")\n'
        'COV.record_many(classify_pipeline(t, llm_ready=True, ocr_ready=True,\n'
        '    llm_call_succeeded=_llm_ok, ocr_backend_used=_ocr_backend))\n'
    )
    assert not _evidence_wiring_problems(good), "误报合格写法"

    # 豁免面必须**窄**：只有显式 upload_failed=True 的调用点才免传证据。
    # （同时含一个「有终态」的调用点，否则护栏本身会因"全在豁免分支"而空转。）
    exempt = (
        '_llm_ok = any(e.get("success") for e in es)\n_co = backend_of(j)\n'
        'COV.record_many(classify_pipeline(t, llm_ready=True, ocr_ready=True,\n'
        '    llm_call_succeeded=_llm_ok, ocr_backend_used=_co))\n'
        'COV.record_many(classify_pipeline("", llm_ready=False, ocr_ready=False,\n'
        '    upload_failed=True))\n'
    )
    assert not _evidence_wiring_problems(exempt), "upload_failed 分支应被豁免"

    # 但"没有 upload_failed、却漏传证据"必须仍然报 —— 豁免不得外溢。
    leaked = (
        '_llm_ok = any(e.get("success") for e in es)\n_co = None\n'
        'COV.record_many(classify_pipeline(t, llm_ready=True, ocr_ready=True))\n'
    )
    assert _evidence_wiring_problems(leaked), "豁免外溢：漏传证据未被报出"
