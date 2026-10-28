"""review-state.js 的行为护栏（此前零行为覆盖）。

`review-state.js` 是复核页**唯一**的共享可变状态宿主（拆分时确立的不变式：
"只能有一个，否则两份状态必然漂移"）。它的两样导出此前只有静态提及、无
node 实跑断言：

- `bool(v)` —— `structured._parse_error` 可能是 true/false/"true"/1 等
  （JSON/DB 往返后的形态），误判会直接翻转"空清单文案"分支（#136）；
- `setButtonLoading(btn, …)` —— 防重复点击，误判会让按钮永久卡住或重复提交；
- `state` 的键面（surface）—— 意外新增的键往往是"镜像了某处数据却无人读"
  的**死状态**（与实时帧漂移的温床）。`pageFindingCounts` 曾是这样一份
  死镜像，已删除；本护栏锁住键面，防止它（或同类）被无声地加回来。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATE_JS = REPO / "static" / "review-state.js"

# 共享状态键面快照 —— 每个键都必须有真实读取方（见模块 docstring）。
# 新增键时请一并在此登记并说明读取方；删除键时同步更新。
EXPECTED_STATE_KEYS = [
    "ctx",
    "currentPage",
    "currentPageFlags",
    "jobId",
    "pageLoadToken",
    "regionRefs",
    "totalPages",
]


def _run_probe(probe: str):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    stub = (
        "globalThis.__PBC__ = { job_id: 'j1', page: 3, total_pages: 5, "
        "region_refs: {}, page_parse_error: 'true', page_ocr_empty: 0 };"
    )
    src = stub + "\n" + STATE_JS.read_text(encoding="utf-8") + "\n" + probe
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestBoolCoercion:
    """`bool` 是"空清单文案分支"的判据入口（#136），必须逐值钉住。"""

    def test_truthy_forms(self):
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify([true, 'true', 1].map((v) => R.bool(v))));"
        )
        assert _run_probe(probe) == [True, True, True]

    def test_falsy_forms(self):
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify("
            "[false, 'false', 0, null, undefined, '', 'yes', 2].map((v) => R.bool(v))));"
        )
        assert _run_probe(probe) == [False] * 8

    def test_string_true_is_not_confused_with_truthy_string(self):
        """'true' 为真，'yes'/2 为假 —— 只认显式真值（保守，与后端 JSON 形态对齐）。"""
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify([R.bool('true'), R.bool('yes'), R.bool(2)]));"
        )
        assert _run_probe(probe) == [True, False, False]


class TestStateSurfaceIsIntentional:
    def test_initial_flags_are_coerced_from_ctx(self):
        """首屏标记必须经 bool() 归一 —— 'true'/0 等 JSON 形态不能原样透传。"""
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify(R.state.currentPageFlags));"
        )
        assert _run_probe(probe) == {"parseError": True, "ocrEmpty": False}

    def test_ctx_derived_scalars(self):
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify([R.state.jobId, R.state.currentPage, "
            "R.state.totalPages, R.state.pageLoadToken]));"
        )
        assert _run_probe(probe) == ["j1", 3, 5, 0]

    def test_no_unexpected_state_key(self):
        """键面快照：多出的键极可能是"镜像了数据却无人读"的死状态。"""
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify(Object.keys(R.state).sort()));"
        )
        actual = _run_probe(probe)
        assert actual == EXPECTED_STATE_KEYS, (
            f"共享状态键面变化：实际 {actual}，期望 {EXPECTED_STATE_KEYS}。\n"
            f"新增键请确认它有真实读取方（否则是与实时帧漂移的死状态），"
            f"并同步更新本清单。"
        )

    def test_dead_mirror_page_finding_counts_is_gone(self):
        """定点回归：`pageFindingCounts` 是已删除的死镜像（圆点真值来自 SSE 帧）。"""
        probe = (
            "const R = globalThis.PbcReview;\n"
            "console.log(JSON.stringify('pageFindingCounts' in R.state));"
        )
        assert _run_probe(probe) is False, (
            "pageFindingCounts 又被镜像回共享状态 —— 它无读取方，只会与"
            "SSE 帧的 d.page_finding_counts 漂移"
        )


class TestSetButtonLoading:
    """防重复点击：加载态加类并禁用，复位态还原文案并解禁。"""

    _FAKE_BTN = """
function fakeBtn(text) {
  return {
    dataset: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      has(c) { return this._s.has(c); },
    },
    textContent: text,
    disabled: false,
  };
}
"""

    def test_loading_sets_disabled_and_processing_text(self):
        probe = (
            self._FAKE_BTN
            + "const R = globalThis.PbcReview;\n"
            "const b = fakeBtn('确认');\n"
            "R.setButtonLoading(b, true);\n"
            "console.log(JSON.stringify({disabled: b.disabled, text: b.textContent,"
            " orig: b.dataset.originalText,"
            " pointerNone: b.classList.has('pointer-events-none')}));"
        )
        out = _run_probe(probe)
        assert out["disabled"] is True
        assert out["text"] == "处理中…"
        assert out["orig"] == "确认", "原文案必须暂存以便复位"
        assert out["pointerNone"] is True

    def test_reset_restores_original_text(self):
        probe = (
            self._FAKE_BTN
            + "const R = globalThis.PbcReview;\n"
            "const b = fakeBtn('确认');\n"
            "R.setButtonLoading(b, true);\n"
            "R.setButtonLoading(b, false);\n"
            "console.log(JSON.stringify({disabled: b.disabled, text: b.textContent,"
            " origCleared: b.dataset.originalText === undefined}));"
        )
        out = _run_probe(probe)
        assert out["disabled"] is False
        assert out["text"] == "确认"
        assert out["origCleared"] is True, "复位后应清掉暂存，避免下次加载读到旧值"

    def test_null_button_is_a_noop(self):
        """`e.currentTarget` 可能为 null（键盘触发等）—— 不得抛异常。"""
        probe = (
            "const R = globalThis.PbcReview;\n"
            "try { R.setButtonLoading(null, true); "
            "console.log(JSON.stringify('ok')); } "
            "catch (e) { console.log(JSON.stringify('threw:' + e.message)); }"
        )
        assert _run_probe(probe) == "ok"
