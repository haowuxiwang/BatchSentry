"""review-locate.js `htmlToText` 的行为护栏（此前零行为覆盖）。

`htmlToText` 把 OCR 后端的 raw HTML 变成复核面板的可读文本，是"点击 finding
卡片 → 在 OCR 面板定位原文"这条链路的输入规范化器。此前只有注释提及、
没有任何 node 实跑断言 —— 而它**纯字符串、无 DOM**，是最容易也最应该被
行为测试锁住的函数（本仓库反复强调：文本在 ≠ 运行期可达）。

覆盖的边界：
- 空/null/undefined 输入（不能抛，返回 ""）；
- 结构标签（p/div/tr/td/br/img）→ 行/列分隔；
- 字面 `\\n`（MinerU 单元格分隔）→ 真换行；
- HTML 实体解码，**含"`&amp;` 必须最后解码"的顺序回归**：
  旧实现先解 `&amp;` 再解 `&lt;`，导致 `&amp;lt;`（字面 "&lt;"）被二次
  解码成 "<" —— 单遍解码器不会如此。OCR 原文里的 `&amp;lt;` 会被显示成
  "<"，误导复核者以为原文就是尖括号。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LOCATE_JS = REPO / "static" / "review-locate.js"

# (输入, 期望输出) —— 期望值来自 node 实跑（见文件末 mutation 注释）
CASES: list[tuple[str | None, str]] = [
    ("", ""),
    (None, ""),
    ("<p>hello</p>", "hello"),
    ("a<br>b", "a\nb"),
    ('x<img src="long/path.png">y', "x[图]y"),
    ("line1\\nline2", "line1\nline2"),          # 字面 \n → 真换行
    ("<div>one</div><div>two</div>", "one\ntwo"),
    ("A &amp; B", "A & B"),
    ("A &lt; B", "A < B"),
    ("A&nbsp;B", "A B"),
    ("A &#39; B", "A ' B"),
    ("<script>alert(1)</script>", "alert(1)"),
]


def _html_to_text(inputs: list) -> list[str]:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    stub = "globalThis.PbcReview = { log: function(){} };"
    probe = (
        "const f = globalThis.PbcReview.locate.htmlToText;\n"
        "const out = %s.map((x) => f(x));\n"
        "console.log(JSON.stringify(out));\n" % json.dumps(inputs)
    )
    src = stub + "\n" + LOCATE_JS.read_text(encoding="utf-8") + "\n" + probe
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _one(value) -> str:
    return _html_to_text([value])[0]


class TestHtmlToTextBoundaries:
    def test_probe_is_not_vacuous(self):
        """防空转：一个已知非空输入必须得到非空输出。"""
        assert _one("<p>hi</p>") == "hi"

    def test_empty_inputs_return_empty_string(self):
        assert _one("") == ""
        assert _one(None) == ""

    @pytest.mark.parametrize("src,expected", CASES)
    def test_case_table(self, src, expected):
        assert _one(src) == expected, f"{src!r} → {_one(src)!r}，期望 {expected!r}"

    def test_table_cells_use_pipe_separator(self):
        out = _one("<tr><td>a</td><td>b</td></tr>")
        assert "a | b" in out, f"表格单元格分隔丢失：{out!r}"

    def test_whitespace_is_collapsed(self):
        assert _one("<p>a     b</p>") == "a b"


class TestEntityDecodingOrder:
    """`&amp;` 必须最后解码 —— 顺序回归的定点护栏。"""

    def test_amp_lt_is_not_double_decoded(self):
        """`&amp;lt;` 是字面文本 "&lt;"，不得变成 "<"。"""
        assert _one("A &amp;lt; B") == "A &lt; B", (
            "`&amp;lt;` 被二次解码成 '<' —— `&amp;` 的 replace 必须排在"
            "`&lt;`/`&gt;`/`&quot;`/`&#39;` 之后"
        )

    def test_amp_gt_is_not_double_decoded(self):
        assert _one("x &amp;gt; y") == "x &gt; y"

    def test_plain_entities_still_decode(self):
        """正向对照：真正的实体仍须解码（别为了修顺序把解码关掉）。"""
        assert _one("A &lt; B") == "A < B"
        assert _one("A &gt; B") == "A > B"
        assert _one("A &amp; B") == "A & B"
        assert _one('A &quot; B') == 'A " B'
        assert _one("A&nbsp;B") == "A B"

    def test_lone_ampersand_is_preserved(self):
        assert _one("A & B") == "A & B"


class TestHtmlToTextStripsTags:
    """标签必须被剥离为纯文本（textContent 赋值，无 XSS 面）。"""

    def test_script_tag_is_stripped_but_text_remains(self):
        # 标签被剥离；内容作为**文本**保留（赋给 textContent，不会执行）
        assert _one("<script>alert(1)</script>") == "alert(1)"
        assert "<script>" not in _one("<script>alert(1)</script>")

    def test_style_attribute_removed(self):
        assert _one('<span style="color:red">x</span>') == "x"
