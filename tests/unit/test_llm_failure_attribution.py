# -*- coding: utf-8 -*-
"""LLM 失败归因的护栏：**账户欠费(402) 不得被报成「产品缺陷」**。

由来（2026-09-30 实测的第二次同类误归因）：
``tests/e2e_frozen.py`` 的归因探测原本只用**免费**端点 ``GET {base_url}/models``。
该端点不消耗额度，所以它只能回答"这把 key 是不是真的"，**回答不了**"这个账户
还付得起钱吗"。账户余额耗尽时上游返回 ``402 code=30001``，而 ``/models`` 照旧
200 ⇒ 判 ``ok`` ⇒ 报告写"凭据有效，故此处失败是**产品缺陷**"；而**同一份报告
的另一行**写着 ``首条错误：402 ... balance is insufficient`` —— 自相矛盾，且把
排查方向引向代码。

实测证据（``devlogs/_verify/probe_llm_balance_vs_credential.py``）：
``GET /models`` → 200；``POST /chat/completions``（max_tokens=1）→ 402；
换 3 个模型（DeepSeek-V3.2 / V4-Flash / Qwen3.8-27B）**全部 402**。

本文件把两条纪律钉死：

  1. **免费端点 200 ≠ 可用** —— 没有计费探测就只能 ``unknown``，**不得**升格
     成 ``ok``（这正是原缺陷的形状）；
  2. **归因与状态正交** —— ``billing`` / ``invalid`` 必须写明"非产品缺陷"，
     ``ok`` 必须写明「探测证到了什么 ＋ 盲区 ＋ 下一步判据」（**不得**无条件
     断言产品缺陷 —— 探针是极小请求，长请求仍可能被上游网关 5xx），
     ``unknown`` 必须 fail-closed（不得被读成放行）。
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tests.e2e_proc import (  # noqa: E402
    VERDICT_BILLING, VERDICT_INVALID, VERDICT_OK, VERDICT_UNKNOWN,
    classify_llm_probe, llm_failure_attribution,
)

_ALL_VERDICTS = (VERDICT_INVALID, VERDICT_BILLING, VERDICT_OK, VERDICT_UNKNOWN)

_FROZEN_PATH = _ROOT / "tests" / "e2e_frozen.py"
_FROZEN = _FROZEN_PATH.read_text(encoding="utf-8")

#: 归因探测的**调用点**形态：必须传第三个实参（应用上报的模型）。
#: 定义为模块常量，使护栏与它的阳性对照共用同一个正则 —— 正则写错时两者会
#: 一起失败，而不是"护栏恒绿、对照也恒绿"。
_PROBE_CALL_RE = re.compile(
    r"probe_llm_credential\(\s*_base_url\s*,\s*_key\s*,\s*_model_used\s*\)"
)

#: 计费端点的**赋值语句**（锚点落在调用点，而非裸标识符 —— 后者会命中 docstring）。
_METERED_URL_ASSIGN = 'metered_url = root + "/chat/completions"'


# ── 判定规则（纯函数，唯一实现）──────────────────────────────────────


def test_free_200_with_metered_402_is_billing():
    """核心新规则：免费端点 200 + 计费 402 ⇒ ``billing``（凭据有效、账户欠费）。"""
    assert classify_llm_probe(200, 402) == VERDICT_BILLING


def test_free_200_alone_is_never_ok():
    """**反回归**：只拿到免费端点 200 时不得判 ``ok``。

    这就是原缺陷的确切形状 —— 当时该分支直接 ``return "ok"``，把欠费报成了
    产品缺陷。若有人把计费探测删掉/漏调，本条必须变红。
    """
    assert classify_llm_probe(200, None) == VERDICT_UNKNOWN
    assert classify_llm_probe(200, None) != VERDICT_OK


def test_ok_requires_both_stages_to_pass():
    """``ok``（⇒ 结论"产品缺陷"）是最重的断言，必须两段都过才给。"""
    assert classify_llm_probe(200, 200) == VERDICT_OK


@pytest.mark.parametrize("code", [401, 403])
def test_auth_rejection_is_invalid_from_either_stage(code):
    """401/403 出现在哪一段都算"上游确凿拒绝该凭据"。"""
    assert classify_llm_probe(code, None) == VERDICT_INVALID
    assert classify_llm_probe(200, code) == VERDICT_INVALID


@pytest.mark.parametrize("free,metered", [
    (None, None),      # 两段都没拿到
    (500, None),       # 免费端点服务端错
    (404, None),       # 端点不对
    (200, 500),        # 计费端点服务端错
    (200, 404),        # 计费端点不存在
    (200, 429),        # 限流：既非欠费也非无效
])
def test_unclassifiable_is_unknown(free, metered):
    """fail-closed：判不了就是判不了，**不得滑向 ok**（也不得滑向 billing）。"""
    assert classify_llm_probe(free, metered) == VERDICT_UNKNOWN


# ── 归因文案（承重措辞）─────────────────────────────────────────────

#: 原缺陷的确切措辞。``billing`` / ``invalid`` / ``unknown`` 分支都**不得**含它。
_PRODUCT_DEFECT_CLAIM = "故此处失败是产品缺陷"  # attr-history: 反面样例（被禁断言原文）


def test_billing_text_says_environment_not_product_defect():
    t = llm_failure_attribution(
        VERDICT_BILLING, "免费端点 HTTP 200 + 计费端点 HTTP 402")
    assert "账户欠费" in t, "未点明'欠费'这一真因"
    assert "402" in t, "未带上游状态码"
    assert "非产品缺陷" in t, "未明确排除产品缺陷"
    assert _PRODUCT_DEFECT_CLAIM not in t, "欠费被判成了产品缺陷（原缺陷复现）"


def test_invalid_text_says_environment_not_product_defect():
    t = llm_failure_attribution(VERDICT_INVALID, "免费端点 HTTP 401")
    assert "非产品缺陷" in t
    assert _PRODUCT_DEFECT_CLAIM not in t


def test_ok_text_does_not_overclaim_from_a_tiny_probe():
    """``ok`` 只证"凭据 + 额度可用"，**不足以**断言产品缺陷（Round 70 第十四批，第三次同类修正）。

    实测（2026-10-08）：同一凭据两段探测双双 200，而真实 ``page_analysis`` 长请求
    （~3127 prompt tokens / 77s）被上游 ALB 以 ``<title>504 Gateway Time-out</title>``
    的 **HTML** 页截断（``llm_call_audit.error`` 因此不是 JSON）⇒ 旧文案
    "故此处失败是**产品缺陷**" 与**同一份报告的另一行**（``首条错误：<html>…504…``）
    **自相矛盾** —— 与 2026-09-30 那次（402 被报成产品缺陷）是同一形状。

    故 ``ok`` 文案必须：① 说清它**证到了什么**；② **点名盲区**（极小请求看不到
    长请求的网关超时）；③ 给出**下一步判据**（看 ``error`` 是不是 JSON）；
    ④ 仍**不得**倒向"环境问题"放行。
    """
    t = llm_failure_attribution(VERDICT_OK, "免费端点 HTTP 200 + 计费端点 HTTP 200")
    assert "凭据有效" in t and "额度可用" in t, "未说清探测证到了什么"
    assert "排除" in t and "401/402" in t, "未排除凭据/额度类环境问题"
    assert "极小请求" in t, "未点名探测盲区（极小请求 ≠ 真实长请求）"
    assert "504" in t and "llm_call_audit.error" in t, "未给出可执行的下一步判据"
    assert "才是产品缺陷" in t, "条件式归因被删掉了（满足条件时仍须敢归因到产品）"
    assert _PRODUCT_DEFECT_CLAIM not in t, "又回到无条件断言产品缺陷（本次修正的缺陷）"
    assert "非产品缺陷" not in t, "凭据与额度都可用时却倒向'环境问题'放行"


def test_unknown_text_is_fail_closed():
    """判不了 ⇒ 按真实缺陷处理；**不得**被写成"环境问题"而放行。"""
    t = llm_failure_attribution(VERDICT_UNKNOWN, "计费探测异常 TimeoutError")
    assert "按真实缺陷处理" in t
    assert "fail-closed" in t
    assert "非产品缺陷" not in t, "判不了却被读成'环境问题' ⇒ 静默放行"


def test_every_verdict_has_its_own_text():
    """防空转 + 防塌缩：四个 verdict 必须给出**互不相同**且非空的文案。

    若某人把分支写漏（例如新增 verdict 却没加分支，全部落到最后兜底），
    文案会塌缩成一份 ⇒ 归因失效但测试仍"全绿"。故必须断言互异。
    """
    texts = {v: llm_failure_attribution(v, "D") for v in _ALL_VERDICTS}
    assert all(t.strip() for t in texts.values()), f"存在空文案：{texts}"
    assert len(set(texts.values())) == len(_ALL_VERDICTS), (
        "归因文案塌缩：不同 verdict 给出了相同文本 ⇒ 分支写漏")


def test_verdict_constants_are_distinct_and_match_the_documented_names():
    """常量本身不得被写成同值（否则上面的"互异"断言会因上游错误而失去意义）。"""
    assert len(set(_ALL_VERDICTS)) == len(_ALL_VERDICTS)
    assert set(_ALL_VERDICTS) == {"invalid", "billing", "ok", "unknown"}


# ── 驱动接线（调用点传参 —— 可选参静默降级陷阱）──────────────────────


def test_driver_passes_the_reported_model_to_the_probe():
    """调用点**必须**传第三个实参（应用上报的模型）。

    ⚠️ 本项目立法过的陷阱：「新增可选参改进输出」一旦漏传**不报错**，只会静默
    退回"只有免费端点"的旧行为（欠费 ⇒ ``unknown`` 而非 ``billing``），改进被
    一次重构无声抹掉。故必须机检**调用点传参**，而不是只查函数定义里有没有这个参。
    """
    assert _PROBE_CALL_RE.search(_FROZEN), (
        "归因探测未传应用上报的模型 ⇒ 计费探测不生效，欠费会退化成 unknown")


def test_model_arg_guard_is_not_vacuous():
    """阳性对照：证明上面那条护栏**真的会报**，而不是正则写错后恒绿。"""
    bad = "verdict, why = probe_llm_credential(_base_url, _key)"
    assert not _PROBE_CALL_RE.search(bad), "护栏对「漏传模型」的写法零反应"
    good = "verdict, why = probe_llm_credential(_base_url, _key, _model_used)"
    assert _PROBE_CALL_RE.search(good), "护栏误报正确写法"


def test_probe_has_a_metered_second_stage():
    """两段式探测必须真的存在：计费端点 + 最小 max_tokens + 走共用判定函数。

    ⚠️ 端点锚点必须落在**赋值那一行**，不能只查裸字符串 ``"/chat/completions"``
    —— 该字符串在**本仓库的 docstring**里也出现（说明文字刻意引用了它），
    只查子串则把调用点改回 ``/models`` 后断言**照样成立**（变异验证实测 MISSED）。
    """
    assert _METERED_URL_ASSIGN in _FROZEN, (
        "计费端点未接到 metered_url（无法区分 401 与 402）")
    assert '"max_tokens": 1' in _FROZEN, "计费探测未用最小 max_tokens"
    assert "classify_llm_probe(" in _FROZEN, "未走共用的判定函数（规则会分叉）"


def test_metered_stage_guard_is_anchored_on_the_call_site():
    """防空转：把调用点改回免费端点后，锚点必须**不再命中**。

    证明 ``_METERED_URL_ASSIGN`` 只出现在代码里、不出现在说明文字里 ——
    否则上面那条护栏就是空转的（本条即为它的阳性对照）。
    """
    assert _METERED_URL_ASSIGN in _FROZEN
    mutated = _FROZEN.replace(_METERED_URL_ASSIGN, 'metered_url = root + "/models"')
    assert mutated != _FROZEN, "锚点未命中源码（锚点过期）"
    assert _METERED_URL_ASSIGN not in mutated, (
        "锚点其实落在说明文字上 ⇒ 护栏空转（改回 /models 也照样绿）")


def test_driver_delegates_attribution_to_the_shared_implementation():
    """驱动不得自造 ``verdict → 文案`` 的字典：判定与文案都只应有一处。

    锚定**调用点**而非裸标识符 —— 本仓库的注释会引用错误写法作为反例，
    子串匹配会把说明文字判成缺陷（已踩过多次）。
    """
    assert "llm_failure_attribution(" in _FROZEN, "未调用共用的归因文案实现"
    assert "attribution = {" not in _FROZEN, (
        "e2e_frozen.py 里仍有内联的 verdict→文案 字典（应改用 llm_failure_attribution）")
