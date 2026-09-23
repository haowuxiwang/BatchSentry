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
    got = classify_pipeline("review", llm_ready=False, ocr_ready=True)
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
    cov.record_many(classify_pipeline("partial_review", llm_ready=True, ocr_ready=True))
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
    cov.record_many(classify_pipeline("review", llm_ready=True, ocr_ready=True))
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
    got = classify_pipeline("review", llm_ready=True, ocr_ready=True)
    assert got[ENTRY_LLM_PIPELINE][0] == STATUS_COVERED
    assert got[ENTRY_OCR_PIPELINE][0] == STATUS_COVERED


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
    """表驱动：任意终态 × 凭据组合都必须给出**合法**状态且覆盖两条条目。"""
    for terminal in ("review", "partial_review", "error", "cancelled", "", "weird"):
        for llm in (True, False):
            for ocr in (True, False):
                got = classify_pipeline(terminal, llm_ready=llm, ocr_ready=ocr)
                assert set(got) == {ENTRY_LLM_PIPELINE, ENTRY_OCR_PIPELINE}, terminal
                for entry, (status, reason, _who) in got.items():
                    assert status in (STATUS_COVERED, STATUS_SKIPPED, STATUS_FAILED)
                    assert reason, f"{terminal}/{entry} 缺 reason（不可审计）"


# ── 落盘 ────────────────────────────────────────────────────────────


def test_coverage_write_and_read_back(tmp_path):
    cov = Coverage(path=tmp_path / "deep" / "cov.json")
    cov.record_many(classify_pipeline("review", llm_ready=True, ocr_ready=False))
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
