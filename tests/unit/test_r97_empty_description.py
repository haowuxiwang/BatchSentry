"""CR-1 ③（R97）：没有业务正文的 finding 不落库。

实测形态（p22 `1563`）：LLM 给出的 `description` 是空串，护栏把降级理由拼上去，
于是**整条 finding 只剩一句后端日志**——复核员看不到任何记录缺陷，只看到
「LLM 独断 critical，未经规则层确定性判据复核」。

两道防线：
  - 写路径（stage2/stage3）用 `has_business_text` 过滤 —— 空文案不写行；
  - 纯函数（`apply_review`）不再往 description 里拼理由（见 `test_llm_finding_guard.py`）。

本文件钉**第一道**：判定本身 + "两个写入口都用它"（避免一处过滤一处不过滤）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.finding_quality import has_business_text

ROOT = Path(__file__).resolve().parents[2]


class TestPredicate:
    def test_empty_string_has_no_business_text(self):
        assert has_business_text({"description": ""}) is False

    def test_whitespace_only_has_no_business_text(self):
        assert has_business_text({"description": "   \n\t "}) is False

    def test_missing_key_has_no_business_text(self):
        assert has_business_text({}) is False

    def test_none_value_has_no_business_text(self):
        assert has_business_text({"description": None}) is False

    def test_non_dict_is_false(self):
        assert has_business_text(None) is False
        assert has_business_text("缺少签名") is False

    def test_short_but_real_text_is_kept(self):
        """不做长度阈值：短正文（"缺少签名"）是合法的，不得被当成空。"""
        assert has_business_text({"description": "缺少签名"}) is True

    def test_text_with_padding_is_kept(self):
        assert has_business_text({"description": "  缺少签名  "}) is True


class TestBothWritePathsUseIt:
    """结构断言：两个写入口都必须调用同一个判定。

    为什么不用行为测试：stage2/stage3 的写库路径依赖 LLM/OCR 全链路，单测里
    只能整段 mock，mock 出来的"写入口"和真实代码已经不是一回事（本仓踩过：
    mock 过的护栏全绿、真机仍漏）。所以这里钉**结构**，行为由变异验证兜底。
    """

    @pytest.mark.parametrize("rel", ["core/pipeline/stage2.py", "core/pipeline/stage3.py"])
    def test_stage_calls_has_business_text(self, rel):
        src = (ROOT / rel).read_text(encoding="utf-8")
        # 必须真的**用它做闸门**（只 import 不调用 = 没过滤）
        assert "if not has_business_text(" in src, (
            f"{rel} 没有用统一判定做闸门 ⇒ 空文案会落库")
        # 反向：不得再出现手写的 `.strip()` 空判定（两处各写一份必然漂移）
        assert not re.search(
            r'if not str\(f\.get\("description"\) or ""\)\.strip\(\)', src
        ), f"{rel} 仍在手写空文案判定"

    @pytest.mark.parametrize("rel,needle", [
        ("core/pipeline/stage2.py", "_empty_desc_skipped += 1"),
        ("core/pipeline/stage3.py", "skipped_empty_desc += 1"),
    ])
    def test_drop_is_counted_not_silent(self, rel, needle):
        """丢弃必须**计数并进日志** —— 静默少一条，事后无法解释条数差异。"""
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert needle in src, f"{rel} 丢弃空文案时没有计数"
        assert "empty-description dropped" in src, f"{rel} 日志没报丢弃条数"
