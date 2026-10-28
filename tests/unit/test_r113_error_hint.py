"""R113：失败横幅的「下一步」文案**只允许有一份**，且不得说"重新提交"。

事故（用户 2026-10-27）：
    「任务处理失败的提醒不够友好，显示：应用重启恢复：原状态 analyzing 非终态，
      标记为 error 供重试。可点击右上角"重试"重新提交任务；如反复失败，请检查
      设置页面 LLM/OCR 配置或日志。」

这句话有两个错，且都来自**两处各写一份**：
1. 「重新提交任务」—— 实际是**续跑**（retry 复用 raw_html，Stage 1 走
   stage1_skipped，Stage 2 跳过已分析页）。用户看到"重新提交"会以为白跑一遍。
2. 无条件把原因归到 LLM/OCR 配置 —— 最常见的失败其实是"被中断"（构建脚本
   强杀、关机），与配置无关。

后端那半句已由 R103 改成「任务被中断（已完成 N/M 页分析）…点「重试」会从中断处
继续」（见 `tests/unit/test_r103_interruption.py`）。**前端这半句**此前仍是 SSR
与 SSE 各硬编码一份，本用例钉住"单源"这件事。

为什么不直接断言"全仓没有『重新提交任务』"：注释里必须保留这句话才能解释
为什么不能这么写。断言只针对**用户可见的字符串**，所以先抽出常量再查。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "review-progress.js"
TPL = ROOT / "templates" / "review.html"

_HINT_DECL = re.compile(r'const\s+JOB_ERROR_HINT\s*=\s*"([^"]+)"', re.S)


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _tpl() -> str:
    return TPL.read_text(encoding="utf-8")


def _hint() -> str:
    m = _HINT_DECL.search(_js())
    assert m, "static/review-progress.js 里找不到 `const JOB_ERROR_HINT = \"…\"`"
    return m.group(1)


class TestSingleSource:
    def test_hint_is_declared_exactly_once(self):
        assert len(_HINT_DECL.findall(_js())) == 1

    def test_template_does_not_hardcode_the_hint(self):
        """模板只留空占位 —— 把句子抄回去，两处必然漂移（这正是原来出错的机制）。"""
        hint = _hint()
        assert hint not in _tpl(), (
            "templates/review.html 又硬编码了提示语；它只应渲染空占位 #job-error-hint"
        )

    def test_template_placeholder_is_empty(self):
        m = re.search(r'<p id="job-error-hint"[^>]*>(.*?)</p>', _tpl(), re.S)
        assert m, "模板里缺少 #job-error-hint 占位"
        assert m.group(1).strip() == "", "占位里不该有文案（由 JS 单源填充）"

    def test_both_paths_share_the_same_element_id(self):
        """SSR 首屏与 SSE 转态都写同一个 id，否则其中一条路径永远空白。"""
        assert 'id="job-error-hint"' in _tpl()
        assert 'id="job-error-hint"' in _js()

    def test_js_fills_both_paths(self):
        js = _js()
        assert js.count("hint.textContent = JOB_ERROR_HINT") == 2, (
            "SSE 横幅与 SSR 占位两条路径都要填，少一条就有一条永远空着"
        )


class TestWordingIsHonest:
    def test_hint_does_not_say_resubmit(self):
        """retry 是续跑，不是重新提交。"""
        hint = _hint()
        assert "重新提交" not in hint
        assert "重新上传" not in hint

    def test_hint_is_conditional_not_a_blanket_blame(self):
        """必须带"如反复失败"前提，不能把一次中断说成配置问题。"""
        assert "如反复失败" in _hint()

    def test_hint_points_at_settings_and_logs(self):
        hint = _hint()
        assert "设置" in hint
        assert "日志" in hint
