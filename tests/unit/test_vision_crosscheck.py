"""#159 定向互证的**判定语义**护栏（原型层，无网络）。

这里锁的不是"模型读得准不准"（那是实测的事，见 `scripts/vision_crosscheck_demo.py`），
而是**我们自己有没有把不一致用错**：

1. **不一致 ⇒ 待人工核对**，不得直接采信视觉或 OCR 任何一方；
2. **"看不清"不得被当成"一致"**（空回答若被算作确认，就是把模型的沉默
   读成合规 —— 与 #136 / #173 同类的假阴性）；
3. **中文姓名不由视觉裁决**（Round 35 实测：7 个模型给出的姓名全错）；
4. 比对口径按类型分派（数值按数值比、时间补零不算差异、**符号必须敏感**）。
"""
import pytest

from core.vision_crosscheck import (
    AGREE, DISAGREE, MUST_VERIFY, UNREADABLE,
    Cell, crosscheck_cells, values_agree,
)
from llm.adapters.base import ImagePart

JPEG = b"\xff\xd8\xff\xe0-fake-jpeg"


class _FakeVisionClient:
    """记录调用、返回固定 JSON。"""

    protocol = "openai"
    model = "fake-vlm"

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    async def chat_json(self, system_prompt, user_content, **kwargs):
        self.calls.append({
            "system": system_prompt, "content": user_content, "kwargs": kwargs,
        })
        return self.payload


def _cells():
    return [
        Cell("温度_序号3", "number", "32", "附表2 温度行 序号3"),
        Cell("时间_序号4", "time", "02:03", "附表2 时间行 序号4"),
        Cell("操作者", "text", "薛东鹏", "附表2 操作者签名"),
    ]


async def _run(payload, cells=None):
    client = _FakeVisionClient(payload)
    res = await crosscheck_cells(client, JPEG, cells or _cells())
    return client, res


# ── 1. 比对口径 ──────────────────────────────────────────────────────


class TestComparisonRules:
    def test_number_equivalence_is_numeric_not_literal(self):
        """`30` 与 `30.0` 是同一量 —— 按字面比会把结论引向错误方向。"""
        assert values_agree("number", "30", "30.0")
        assert values_agree("number", "30.0kg", "30")
        assert not values_agree("number", "30.0", "930.0")

    def test_number_sign_is_significant(self):
        """`-0.088` 与 `0.088` **不是**同一量（真空度与正压是两回事）。"""
        assert not values_agree("number", "-0.088", "0.088")
        assert values_agree("number", "-.088", "-0.088")

    def test_time_padding_is_not_a_discrepancy(self):
        assert values_agree("time", "2:03", "02:03")
        assert not values_agree("time", "02:03", "02:23")


# ── 2. 判定语义 ──────────────────────────────────────────────────────


class TestVerdictSemantics:
    @pytest.mark.asyncio
    async def test_discrepancy_is_flagged_not_resolved(self):
        """两侧不同 ⇒ DISAGREE，且**不出现**"采信某一方"的字段/语义。

        温度行正是实测里 OCR 读错的那一行（OCR 32/34/… 视觉 50/52/…）。
        """
        _, res = await _run({
            "温度_序号3": "54", "时间_序号4": "02:03", "操作者": "眭东鹏",
        })
        by_key = {v.key: v for v in res.verdicts}
        assert by_key["温度_序号3"].status == DISAGREE
        assert by_key["温度_序号3"].needs_human is True
        assert by_key["温度_序号3"].ocr_value == "32"
        assert by_key["温度_序号3"].visual_value == "54"
        assert [v.key for v in res.discrepancies] == ["温度_序号3"], (
            "discrepancies 只应含「两侧读到不同值」的单元格"
        )
        assert by_key["时间_序号4"].status == AGREE

    @pytest.mark.asyncio
    async def test_unreadable_is_not_confirmation(self):
        """模型说看不清 ⇒ 待人工核对，**绝不能**算作与 OCR 一致。"""
        _, res = await _run({
            "温度_序号3": "", "时间_序号4": "02:03", "操作者": "眭东鹏",
        })
        by_key = {v.key: v for v in res.verdicts}
        assert by_key["温度_序号3"].status == UNREADABLE, (
            "空回答被当成一致 ⇒ 把模型的沉默读成合规（GMP 假阴性）"
        )
        assert by_key["温度_序号3"].needs_human is True
        assert not res.discrepancies, "未读出不是「不一致」，不应进 discrepancies"

    @pytest.mark.asyncio
    async def test_missing_key_is_unreadable_not_agree(self):
        """模型整条键没回 ⇒ 同样是未读出。"""
        _, res = await _run({"操作者": "眭东鹏"})
        assert {v.status for v in res.verdicts} == {UNREADABLE, MUST_VERIFY}

    @pytest.mark.asyncio
    async def test_name_is_never_adjudicated_by_vision(self):
        """**最重要的一条**：中文姓名不得由视觉裁决 —— 即使模型答得"很自信"。

        Round 35 实测：7 个模型给出 `胖东鹏 / 胖东朋朋 / 胖乐鹏 / 薛东鹏 /
        周小英`，**没有一个读对"眭"**。所以姓名只能"标记待人工核对"。
        本用例刻意让模型答**与 OCR 相同**的名字，断言我们**依然不确认**：
        否则一个恰好猜中的模型会让姓名静默通过。
        """
        _, res = await _run({
            "温度_序号3": "54", "时间_序号4": "02:03", "操作者": "薛东鹏",
        })
        v = {x.key: x for x in res.verdicts}["操作者"]
        assert v.status == MUST_VERIFY, (
            f"姓名被判成 {v.status} —— 视觉不得裁决姓名（#159 实测硬约束）"
        )
        assert v.needs_human is True
        assert v in res.needs_human

    @pytest.mark.asyncio
    async def test_non_dict_response_degrades_to_need_human(self):
        """模型返回非对象（数组/字符串）时不得崩，且全部转人工。"""
        _, res = await _run(["这不是对象"])
        assert all(v.needs_human for v in res.verdicts)
        assert not res.discrepancies


# ── 3. 图像确实被送出去了（否则整条链是空转的）────────────────────────


class TestImageIsActuallySent:
    @pytest.mark.asyncio
    async def test_image_part_is_in_the_request(self):
        client, _ = await _run({"温度_序号3": "54"})
        content = client.calls[0]["content"]
        assert isinstance(content, list), "多模态必须以片段列表下发"
        imgs = [p for p in content if isinstance(p, ImagePart)]
        assert len(imgs) == 1, "图像片段缺失 —— 互证退化成「让模型凭记忆答」"
        assert imgs[0].data == JPEG
        assert imgs[0].media_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_prompt_lists_every_cell_key(self):
        """键名必须原样出现在提示里，否则模型回填的键对不上 → 全变未读出。"""
        client, _ = await _run({"温度_序号3": "54"})
        text = "".join(p for p in client.calls[0]["content"] if isinstance(p, str))
        for c in _cells():
            assert c.key in text
        assert "看不清" in text and "不要猜测" in text, (
            "缺少「看不清就留空、不要猜」的约束 —— 那会诱导模型编值"
        )
