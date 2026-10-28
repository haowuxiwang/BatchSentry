"""0-41（P2）护栏：LLM fallback 必须**分批判定**，不得整份降级。

## 缺陷（改造前的行为）

``core/rules/llm_checks.py`` 在 ``len(llm_queue) > _LLM_FALLBACK_BATCH_MAX(=50)``
时**不调 LLM**，把该 job 的**全部**待判定参数一次性降级为人工复核 ⇒
**零 `param_out_of_spec` 产出**。

真实证据（``devlogs/_verify/r91b_161_real_sample_evidence.py``，只读 DB 可复跑）：
job ``670672f3-d0f``（丝裂霉素，51 页）有 **356** 条该 reason 的 findings
（分布页 5–47），而**用户标注页 page 6** 恰在其中 ⇒ 该页 **0 条
`param_out_of_spec`**，实测值 ``6.88`` 在全 job findings 中命中 **0 次**。

这不是"安全降级"，是**覆盖缺口**：产品在最需要自动判定的大记录上退化成白纸。
真实用户需求（GMP 复核）恰恰是"51 页的记录也要逐项判定"，而不是"页数一多就
全部转人工"。

## 修复后的契约（本文件钉住的不变式）

1. 队列按 ``_LLM_FALLBACK_BATCH_MAX`` 切块，**逐块**调用 LLM；
2. 单块失败/解析失败**只**降级该块（不连坐其余块）；
3. 两道闸门 —— 批数上限 ``_LLM_FALLBACK_MAX_BATCHES``、墙钟预算
   ``_LLM_FALLBACK_BUDGET_S`` —— 越界的批次**显式**降级（每批一条汇总
   finding，reason 写明总数/已用时长），不是静默丢弃；
4. **不重不漏**：每个入参恰好落在「被判定」或「被某条降级 finding 覆盖」之一。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.rules.llm_checks import (
    _LLM_FALLBACK_BATCH_MAX,
    _LLM_FALLBACK_BUDGET_S,
    _LLM_FALLBACK_MAX_BATCHES,
    _llm_fallback_check,
)


def _queue(n: int) -> list[dict]:
    """n 个待判定参数；名字唯一 ⇒ 可以按名字核对"谁被判定/谁被降级"。"""
    return [
        {"page": (i % 7) + 1, "step_no": 1, "name": f"参数{i}", "spec": "应澄清",
         "actual": f"值{i}", "unit": "", "kind": "param"}
        for i in range(n)
    ]


def _all_oos(batch_size: int = _LLM_FALLBACK_BATCH_MAX) -> list[dict]:
    """模拟"这一批全部越界" —— 让断言看得到 LLM 的判定结果真的被采用。"""
    return [{"index": i + 1, "in_spec": False, "reason": "oos"}
            for i in range(batch_size)]


@pytest.fixture
def mock_llm():
    client = MagicMock()
    client.chat_json = AsyncMock(return_value={"findings": []})
    with patch("core.rules.llm_checks.get_llm_client", return_value=client):
        yield client


def _degraded(findings: list[dict]) -> list[dict]:
    """降级型 finding（汇总口径，ocr_text 里带 params=N）。"""
    return [f for f in findings if f["ocr_text"].startswith("params=")]


def _params_in(finding: dict) -> int:
    return int(finding["ocr_text"].split()[0].split("=")[1])


# ---------------------------------------------------------------------------
# 正向：分批真的把参数判了
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_limit_queue_is_judged_in_batches(mock_llm):
    """60 个参数 ⇒ 2 批 2 次调用，且**每个**参数都拿到判定结果。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX + 10)
    mock_llm.chat_json.return_value = _all_oos()

    findings = await _llm_fallback_check(q, job_id="t")

    assert mock_llm.chat_json.await_count == 2
    assert _degraded(findings) == [], "未触闸门时不得产生降级噪音"
    assert len(findings) == len(q)
    assert {f["type"] for f in findings} == {"param_out_of_spec"}
    assert {f["source"] for f in findings} == {"llm_fallback"}


@pytest.mark.asyncio
async def test_each_batch_prompt_only_contains_its_own_params(mock_llm):
    """切块必须真切：第 2 批的 prompt 不得再含第 1 批的参数。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX + 10)
    mock_llm.chat_json.return_value = []

    await _llm_fallback_check(q, job_id="t")

    prompts = [c.args[1] for c in mock_llm.chat_json.await_args_list]
    assert f"参数{_LLM_FALLBACK_BATCH_MAX - 1} " in prompts[0]
    assert f"参数{_LLM_FALLBACK_BATCH_MAX} " not in prompts[0]
    assert f"参数{_LLM_FALLBACK_BATCH_MAX} " in prompts[1]
    assert "参数0 " not in prompts[1]


# ---------------------------------------------------------------------------
# 闸门 1：批数上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batches_beyond_cap_are_degraded_explicitly(mock_llm):
    """超批数上限的部分**显式**降级：不调 LLM，但必须留下可检索的汇总行。"""
    n = _LLM_FALLBACK_BATCH_MAX * (_LLM_FALLBACK_MAX_BATCHES + 1)   # 9 批
    q = _queue(n)
    mock_llm.chat_json.return_value = _all_oos()

    findings = await _llm_fallback_check(q, job_id="t")

    assert mock_llm.chat_json.await_count == _LLM_FALLBACK_MAX_BATCHES
    degraded = _degraded(findings)
    assert len(degraded) == 1, "越界的那一批应合成 1 条汇总"
    assert _params_in(degraded[0]) == _LLM_FALLBACK_BATCH_MAX
    assert "参数数量过多" in degraded[0]["description"]
    assert f"共 {n}" in degraded[0]["description"]
    # 不重不漏
    judged = len([f for f in findings if f not in degraded])
    assert judged + _params_in(degraded[0]) == n


# ---------------------------------------------------------------------------
# 闸门 2：墙钟预算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_degrades_the_rest(mock_llm):
    """预算耗尽后剩余批次降级 —— reason 必须写"已用时长"，不能只说"超时"。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX * 3)          # 3 批
    mock_llm.chat_json.return_value = _all_oos()
    fake_time = MagicMock()
    # 调用序：t0=0，批1 elapsed=0（放行），批2/批3 各读一次 ⇒ 均已超预算
    fake_time.monotonic.side_effect = [0.0, 0.0, _LLM_FALLBACK_BUDGET_S + 100.0,
                                       _LLM_FALLBACK_BUDGET_S + 100.0]
    with patch("core.rules.llm_checks.time", fake_time):
        findings = await _llm_fallback_check(q, job_id="t")

    assert mock_llm.chat_json.await_count == 1, "预算耗尽后不得再调 LLM"
    degraded = _degraded(findings)
    assert len(degraded) == 2
    assert sum(_params_in(f) for f in degraded) == _LLM_FALLBACK_BATCH_MAX * 2
    assert all("自动判定超时" in f["description"] for f in degraded)
    assert all(f"{_LLM_FALLBACK_BUDGET_S:.0f}s" in f["description"] for f in degraded)
    # 被放行的那一批真的被判了
    assert len([f for f in findings if f not in degraded]) == _LLM_FALLBACK_BATCH_MAX


# ---------------------------------------------------------------------------
# 失败隔离：一块坏不能连坐
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_batch_failure_does_not_contaminate_others(mock_llm):
    """第 1 批抛异常 ⇒ 只有它的参数转人工；第 2 批照常采用 LLM 判定。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX + 5)
    mock_llm.chat_json.side_effect = [RuntimeError("upstream down"), _all_oos(5)]

    findings = await _llm_fallback_check(q, job_id="t")

    assert mock_llm.chat_json.await_count == 2
    oos = [f for f in findings if f["type"] == "param_out_of_spec"]
    fallback = [f for f in findings if f["type"] == "completeness"]
    assert len(oos) == 5, "第 2 批的判定结果必须保留"
    assert len(fallback) == _LLM_FALLBACK_BATCH_MAX, "第 1 批逐参数转人工"
    assert all("LLM 调用失败" in f["description"] for f in fallback)


@pytest.mark.asyncio
async def test_parse_error_isolated_to_its_batch(mock_llm):
    """解析失败同样只影响本批。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX * 2)
    mock_llm.chat_json.side_effect = [
        {"_parse_error": True, "_raw": "not json"}, _all_oos()]

    findings = await _llm_fallback_check(q, job_id="t")

    fallback = [f for f in findings if f["type"] == "completeness"]
    oos = [f for f in findings if f["type"] == "param_out_of_spec"]
    assert len(fallback) == _LLM_FALLBACK_BATCH_MAX
    assert len(oos) == _LLM_FALLBACK_BATCH_MAX
    assert all("JSON 解析失败" in f["description"] for f in fallback)


# ---------------------------------------------------------------------------
# 反向对照（防空转：改造不能顺手改掉"小队列"的行为）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_queue_still_single_call(mock_llm):
    """≤ 上限 ⇒ **恰好 1 次**调用（改造前也是 1 次，不得变多）。"""
    q = _queue(_LLM_FALLBACK_BATCH_MAX)
    mock_llm.chat_json.return_value = _all_oos()

    findings = await _llm_fallback_check(q, job_id="t")

    assert mock_llm.chat_json.await_count == 1
    assert len(findings) == _LLM_FALLBACK_BATCH_MAX


@pytest.mark.asyncio
async def test_empty_queue_never_calls_llm(mock_llm):
    assert await _llm_fallback_check([]) == []
    mock_llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_no_degradation_when_every_batch_is_judged(mock_llm):
    """400 个参数（= 真实最大样本）= 8 批，正好在闸门内 ⇒ 零降级。"""
    n = _LLM_FALLBACK_BATCH_MAX * _LLM_FALLBACK_MAX_BATCHES
    q = _queue(n)
    mock_llm.chat_json.return_value = _all_oos()

    findings = await _llm_fallback_check(q, job_id="670672f3-d0f")

    assert mock_llm.chat_json.await_count == _LLM_FALLBACK_MAX_BATCHES
    assert _degraded(findings) == []
    assert len(findings) == n, "真实最大样本必须被**全部**判定"


# ---------------------------------------------------------------------------
# 常量本身：批大小有**上界**，而且必须有绝对值的用例
# ---------------------------------------------------------------------------
# ⚠️ 这两条是变异 M4 逼出来的（首版 4 条变异只抓住 3 条）：
# 上面所有用例都用 `_queue(_LLM_FALLBACK_BATCH_MAX + 10)` —— 队列长度**跟着常量走**
# ⇒ 把常量从 50 改成 1000，用例会一起"变长"，仍然 2 批、仍然全绿。
# 那是**自证式**断言：它证明"切块逻辑与常量一致"，证明不了"批大小没被悄悄放大"。
# 所以补两条**绝对**断言：
#   · 批大小上界 ≤ 50（取值理由见常量注释：449 个参数的单次调用实测会重试并卡住
#     Stage 3 数分钟；50 个参数 ≈ 3-4KB prompt 是已验证尺寸）；
#   · **60 个参数**（一个固定数字，与常量无关）必须切 ≥ 2 批。


def test_batch_size_has_an_absolute_upper_bound():
    """批大小不得超过实测安全尺寸 50 —— 放大它等于把 449 参数的老问题放回来。"""
    assert _LLM_FALLBACK_BATCH_MAX <= 50, (
        f"_LLM_FALLBACK_BATCH_MAX={_LLM_FALLBACK_BATCH_MAX} 超过实测安全尺寸 50："
        "449 个参数的单次调用会让上游重试并卡住 Stage 3 数分钟（0-38/0-41 实测）"
    )


@pytest.mark.asyncio
async def test_sixty_params_are_split_regardless_of_the_constant(mock_llm):
    """60 个参数 ⇒ **至少 2 次**调用（绝对数字，不跟随常量变化）。"""
    mock_llm.chat_json.return_value = []
    await _llm_fallback_check(_queue(60), job_id="t")
    assert mock_llm.chat_json.await_count >= 2

