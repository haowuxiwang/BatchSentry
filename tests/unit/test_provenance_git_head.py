# -*- coding: utf-8 -*-
"""产物出处护栏：`PROVENANCE.txt` 的 `git_head` **不得为空**。

由来（2026-09-30 实测）
----------------------
`build.ps1` 取 HEAD 的写法是::

    $gitHead = ""
    try { $gitHead = (& git rev-parse --short HEAD) 2>$null } catch {}

`try/catch` 把失败**吞掉**。在一台 `git` 不在 PATH 上的宿主里执行时，写出来的
`PROVENANCE.txt` 就是 ``git_head: ``（**空值**，行尾一个空格）—— 而全链路绿灯：

  · `release_gate.count_complete_artifacts` 只查该文件**存在**；
  · `e2e_unpacked` 的 D2 只比对 ``version``（产品版本号）。

于是产物**声称**能自证"由哪次提交构建"，实际证不了。这正是本项目最怕的形状：
**契约写在注释里，没有任何东西守着它**。

本文件钉三条：
  1. 字段在但**为空** ⇒ ``empty``（必须 FAIL，不是 SKIP）；
  2. 字段**缺失** ⇒ ``missing``（早于本契约的产物，不判定）；
  3. 判据**只要求非空**，不得要求等于当前 HEAD —— 构建通常发生在提交**之前**
     （本次拆分就是先打包后 commit）⇒ 用相等判会制造假红。
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tests.e2e_proc import (  # noqa: E402
    PROV_EMPTY, PROV_MISSING, PROV_OK, classify_provenance_git_head,
)

_UNPACKED_PATH = _ROOT / "tests" / "e2e_unpacked.py"
_UNPACKED = _UNPACKED_PATH.read_text(encoding="utf-8")

#: 驱动的**调用点**形态（与阳性对照共用同一正则：正则写错时两者一起失败，
#: 而不是"护栏恒绿、对照也恒绿"）。
_CALL_RE = re.compile(r"classify_provenance_git_head\(\s*prov_text\s*\)")

#: 真实产物里的 `PROVENANCE.txt`（本机打包后才存在；不存在时跳过，不算缺陷）。
_REAL_PROV = _ROOT / "dist-electron" / "win-unpacked" / "PROVENANCE.txt"

#: `build.ps1` 写出的行样式（注意 `version:` 后是**两个**空格，不要"修正"它）。
_PROV_OK_TEXT = "\r\n".join([
    "standard output: dist-electron was NOT locked at build time",
    "built_at: 2026-09-30 10:40:52",
    "git_head: 11e8e03",
    "version:  1.2.1",
]) + "\r\n"

#: 同一份文件在 `git` 不可解析的宿主上被写出来的样子 —— 行尾一个空格，值为空。
_PROV_EMPTY_TEXT = _PROV_OK_TEXT.replace("git_head: 11e8e03", "git_head: ")


# ── 判定规则（纯函数，唯一实现）──────────────────────────────────────


def test_populated_git_head_is_ok():
    """正常形态 ⇒ ``ok``，且 detail 就是那串哈希。"""
    status, detail = classify_provenance_git_head(_PROV_OK_TEXT)
    assert status == PROV_OK
    assert detail == "11e8e03"


def test_empty_git_head_is_not_ok():
    """★ 核心规则：字段在但值为空 ⇒ ``empty``（**不得**被读成通过）。

    这是本轮真实发生的形态：`build.ps1` 的 `& git rev-parse` 静默失败。
    """
    status, _ = classify_provenance_git_head(_PROV_EMPTY_TEXT)
    assert status == PROV_EMPTY


def test_missing_field_is_not_ok():
    """整个字段都没有 ⇒ ``missing``（老产物，不判定）。"""
    status, _ = classify_provenance_git_head("version:  1.2.1\r\n")
    assert status == PROV_MISSING


@pytest.mark.parametrize("text", ["", None, "\r\n", "完全无关的内容\r\n"])
def test_absent_or_garbage_text_is_missing_not_ok(text):
    """空文本/垃圾文本 ⇒ ``missing``（**不得**因"没报错"就被读成 ok）。"""
    assert classify_provenance_git_head(text)[0] == PROV_MISSING


def test_whitespace_only_value_is_empty_not_ok():
    """只有空白的值与空值等价（`git_head:    ` 也是空）。"""
    assert classify_provenance_git_head("git_head:    \r\n")[0] == PROV_EMPTY


def test_ok_does_not_require_matching_current_head():
    """★ 判据**只要求非空**：一个与当前 HEAD 不同的哈希照样 ``ok``。

    反例防护：`PROVENANCE.txt` 是**构建期**写的，而构建通常发生在提交**之前**
    （本次拆分即：先打包、后 commit，产物里的 git_head 是父提交）⇒ 若判据写成
    "必须等于当前 HEAD"，正常的产物会**恒红**。这条把该误解钉死。
    """
    text = _PROV_OK_TEXT.replace("11e8e03", "c0ffee1")   # 一个"不存在的"哈希
    status, detail = classify_provenance_git_head(text)
    assert status == PROV_OK, "不得因 git_head ≠ 当前 HEAD 而报红"
    assert detail == "c0ffee1"


def test_field_name_matching_is_case_insensitive_and_first_wins():
    """`build.ps1` 写小写；但大小写不同的写法也应被识别（与 D2_version 同口径）。"""
    assert classify_provenance_git_head("GIT_HEAD: abc1234\r\n") == (PROV_OK, "abc1234")


def test_colon_in_value_is_preserved():
    """值里含 `:` 时只按**第一个**冒号切分（别把哈希切坏）。"""
    assert classify_provenance_git_head("git_head: a:b:c\r\n") == (PROV_OK, "a:b:c")


# ── 防空转（提取器没坏）─────────────────────────────────────────────


def test_extractor_is_not_vacuous():
    """阳性对照：证明"提取器真的在读字段"，而不是对任何输入都返回同一结论。"""
    assert classify_provenance_git_head(_PROV_OK_TEXT)[0] == PROV_OK
    assert classify_provenance_git_head(_PROV_EMPTY_TEXT)[0] == PROV_EMPTY
    assert classify_provenance_git_head(_PROV_OK_TEXT)[0] != \
        classify_provenance_git_head(_PROV_EMPTY_TEXT)[0], (
            "两种形态被判成同一结论 ⇒ 判据没有判别力")


def test_empty_branch_text_explains_the_consequence():
    """空值分支的说明必须点出**后果**（否则读日志的人不知道为何要红）。"""
    _, detail = classify_provenance_git_head(_PROV_EMPTY_TEXT)
    assert "无法自证" in detail and "git" in detail


def test_status_constants_are_distinct():
    assert len({PROV_OK, PROV_MISSING, PROV_EMPTY}) == 3


# ── 驱动接线（调用点 —— 可选参/未接线静默降级陷阱）────────────────────


def test_driver_calls_the_shared_classifier():
    """`e2e_unpacked.py` **必须真的调用**共用判定函数。

    ⚠️ 只断言"函数存在于 `e2e_proc.py`"是**空断言** —— 定义了不调用，产物照样
    绿灯。故钉**调用点**（并把读取到的文本 `prov_text` 作为实参一并钉住，
    否则调用点可能被改成"传空串"而永不报警）。
    """
    assert _CALL_RE.search(_UNPACKED), (
        "unpacked 驱动未调用 classify_provenance_git_head(prov_text) ⇒ "
        "git_head 为空时无人报警")


def test_driver_call_guard_is_not_vacuous():
    """阳性对照：证明上面那条护栏**真的会报**，而不是正则写错后恒绿。"""
    bad = "status, detail = classify_provenance_git_head(prov.read_text())"
    assert not _CALL_RE.search(bad), "护栏对「未传 prov_text」的写法零反应"
    assert _CALL_RE.search(_UNPACKED), "护栏误报正确写法"


def test_driver_reports_a_named_check_for_git_head():
    """检查项必须**具名**出现在报告里（否则红了也说不清是哪一项）。

    锚定 f-string 的**调用点**，而非裸标识符 `D2b_provenance_git_head`
    —— 后者在本仓库的注释里也会出现（本文件顶部就引用了它）。
    """
    assert 'f"run{run_idx}.D2b_provenance_git_head"' in _UNPACKED, (
        "D2b 检查未具名上报（无法从报告定位到 git_head）")


def test_driver_does_not_reimplement_the_rule_inline():
    """驱动不得自造一份"git_head 非空"的内联判据（规则会分叉）。

    ⚠️ 锚点用 ``startswith("git_head"``（内联解析必然的写法），**不能**用子串
    ``"git_head:"`` —— 驱动里的说明文字就引用了 ``git_head: ``（空值形态）作为
    反例，子串匹配会把注释判成缺陷（本仓库已踩过多次同类坑）。
    """
    assert 'startswith("git_head"' not in _UNPACKED, (
        "e2e_unpacked.py 里仍有内联的 git_head 解析（应改用共用判定函数）")


# ── 真实产物护栏（本机打包后才有；不存在则跳过，不算缺陷）────────────


def test_real_artifact_provenance_is_never_empty():
    """★ 真值护栏：磁盘上那份产物的 `PROVENANCE.txt` **不得**是空 git_head。

    这条直接守**交付物**，而不是只测函数：若有人用一台 git 不可解析的宿主重新
    打包，这里会红。旧产物没有该字段 ⇒ 判 ``missing``，本用例放行（不判定）。

    ⚠️ 必须**独立于判定函数**再查一遍原始字节：只断言"判定函数说不是 empty"
    是**空断言** —— 判定函数一旦退化成宽松（把空值当 ok），这条就跟着瞎
    （变异 M2 实测 MISSED）。故这里另加一条**直接看文本**的正则。
    """
    if not _REAL_PROV.is_file():
        pytest.skip(f"本机无产物：{_REAL_PROV}（未打包，不判定）")
    raw = _REAL_PROV.read_text(encoding="utf-8", errors="replace")

    status, detail = classify_provenance_git_head(raw)
    assert status != PROV_EMPTY, f"产物出处为空：{detail}"

    # 独立判据：文本里必须真的有一行「git_head: <非空值>」。
    assert re.search(r"^git_head:[ \t]*(\S+)", raw, re.M), (
        "产物 PROVENANCE.txt 里没有非空的 git_head 行 ⇒ 无法自证由哪次提交构建")
