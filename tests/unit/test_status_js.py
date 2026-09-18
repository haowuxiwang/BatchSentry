"""#133(P0) 状态点颜色单一真值的机检护栏。

背景：`templates/review.html` 曾把状态点写死 `bg-success`，而 `review.js` 的
SSE 更新只改 `textContent`、从不改这个点的 class ⇒ `status=error` /
`partial_review` 的复核页显示 **"绿点 + 出错"**，与"记录确实无异常"在界面上
不可区分（GMP 假阴性表面，与 #127 同类：upload 页修了、复核页漏了）。

本文件锁三件事，缺任一条那个洞就会重新打开：
1. **两侧实现逐值等价** —— `static/status.js` 的 `statusDotClass` 与
   `core/zh_map.py` 的 `status_dot_class` 对全部状态枚举（含未知值）输出同名 class。
   SSR 走 Python、SSE 走 JS，任一单边漂移都会让"首屏"和"实时"显示不一致。
2. **模板不得再出现无条件的 `bg-success`** —— 这是把硬编码改回来的直接回归门。
3. **`partial_review` 永远不是绿色** —— 它是本缺陷的核心语义（其定义即
   "存在失败页或双后端差异"），必须单独钉住，不能靠上面的通用等价性顺带覆盖。

node 不可用时跳过 JS 侧（打包链不依赖），但 Python 侧与模板静态检查**始终执行**。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.zh_map import (  # noqa: E402
    JOB_STATUS_ZH,
    status_dot_class,
)

STATUS_JS = REPO / "static" / "status.js"
REVIEW_HTML = REPO / "templates" / "review.html"
REVIEW_JS = REPO / "static" / "review.js"
UPLOAD_JS = REPO / "static" / "upload.js"

# 状态枚举全集 = 后端中文映射的键（core/zh_map.py 是 Python 侧单一真值）。
ALL_STATUSES = sorted(JOB_STATUS_ZH.keys())

# 前端可能见到但后端映射表暂未收录的状态（模板 job_status_zh 里的超集，
# 例如 queued / processing / cancelling / done）。SSR 与 JS 都必须能处理，
# 否则"status 是后端新枚举"时两侧会走不同的默认分支。
EXTRA_STATUSES = ["queued", "processing", "cancelling", "done", "confirmed",
                  "rejected", "corrected"]
FULL_SET = sorted(set(ALL_STATUSES) | set(EXTRA_STATUSES))


def _node_dot_classes(statuses: list[str]) -> dict[str, str]:
    """在 node 里跑 static/status.js，取每个状态的颜色类。"""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    body = """
const out = {};
for (const st of %s) out[st] = PbcStatus.statusDotClass(st);
console.log(JSON.stringify(out));
""" % json.dumps(statuses)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "status_probe.js"
        p.write_text(STATUS_JS.read_text(encoding="utf-8") + "\n" + body,
                     encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestSharedModuleExists:
    def test_status_js_present_and_exported(self):
        """共享件存在且挂在 PbcStatus 上（模板靠这个名字引用）。"""
        src = STATUS_JS.read_text(encoding="utf-8")
        assert "global.PbcStatus" in src
        for fn in ("statusZh", "statusDotClass", "isActiveStatus"):
            assert fn in src, f"PbcStatus 缺少 {fn}"

    def test_templates_include_status_js(self):
        """两个页面都必须引入共享件 —— 只引一个就是"修了一半"。"""
        for tpl in ("upload.html", "review.html"):
            html = (REPO / "templates" / tpl).read_text(encoding="utf-8")
            assert "/static/status.js" in html, f"{tpl} 未引入 status.js"


class TestParityWithPython:
    """SSR(Python) 与 SSE(JS) 必须逐值同色 —— 任一单边漂移都会让
    "首屏显示绿、刷新后变红"这类鬼影出现。"""

    def test_all_statuses_agree(self):
        js = _node_dot_classes(FULL_SET)
        mismatches = []
        for st in FULL_SET:
            py_cls = status_dot_class(st)
            if js.get(st) != py_cls:
                mismatches.append((st, py_cls, js.get(st)))
        assert not mismatches, (
            "status.js 与 core/zh_map.py 的颜色映射漂移："
            + ", ".join(f"{s}: py={p} js={j}" for s, p, j in mismatches)
        )

    def test_unknown_status_agrees(self):
        """未知状态（后端新增枚举、前端没跟上）两侧都走同一默认分支。"""
        assert status_dot_class("brand_new_status") == _node_dot_classes(
            ["brand_new_status"]
        )["brand_new_status"]

    def test_empty_status_is_neutral_not_green(self):
        """空值不能渲染成绿色 —— 无色点比错色更难察觉。"""
        assert status_dot_class(None) != "bg-success"
        assert status_dot_class("") != "bg-success"


class TestPartialReviewIsNeverGreen:
    """本缺陷的核心语义：partial_review 的定义就是"存在失败页或双后端差异"。"""

    def test_python_side(self):
        assert status_dot_class("partial_review") == "bg-warning"
        assert status_dot_class("partial_review") != "bg-success"

    def test_js_side(self):
        assert _node_dot_classes(["partial_review"])["partial_review"] == "bg-warning"

    def test_done_and_review_are_green(self):
        """正向对照：真正的成功态仍必须是绿的，别为了修 bug 把成功也改掉。"""
        for st in ("review", "done"):
            assert status_dot_class(st) == "bg-success", st

    def test_error_is_destructive(self):
        assert status_dot_class("error") == "bg-destructive"


class TestTemplateHasNoHardcodedDot:
    """回归门：模板里不得再出现无条件的 bg-success 状态点。

    这个洞当初就是"模板写死 + JS 只改文案"两者叠加造成的，所以两层都要钉。
    """

    def test_review_html_dot_is_bound_to_status(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        # 定位状态点所在的那一行（带 id="status-dot"）
        m = re.search(r'<span id="status-dot"[^>]*class="([^"]*)"', html)
        assert m, "review.html 里找不到 #status-dot（状态点必须可被 JS 定位）"
        cls = m.group(1)
        assert "bg-success" not in cls, (
            "状态点颜色又被写死成 bg-success —— 必须由 status_dot_class 注入"
        )
        assert "{{" in cls, f"状态点 class 未绑定变量：{cls!r}"
        assert "status_dot_class" in cls, (
            f"状态点 class 应引用 status_dot_class，实际：{cls!r}"
        )

    def test_review_js_updates_dot_class(self):
        """SSE 必须改点的 class，不能只改文字（这正是漏修的那一半）。"""
        src = REVIEW_JS.read_text(encoding="utf-8")
        assert 'getElementById("status-dot")' in src, (
            "review.js 未更新状态点的 class ⇒ 实时阶段仍会显示错色"
        )
        assert "statusDotClass" in src, "review.js 应调用共享件的 statusDotClass"

    def test_upload_js_delegates_to_shared_module(self):
        """upload.js 不得再私藏一份状态映射（#139 的收敛，也是本缺陷的成因之一）。"""
        src = UPLOAD_JS.read_text(encoding="utf-8")
        assert "PbcStatus" in src, "upload.js 应改用共享件"
        assert "const STATUS_ZH = {" not in src, (
            "upload.js 又出现了私有 STATUS_ZH 副本（应走 PbcStatus）"
        )
        assert "function statusDotClass(st) {\n    if (" not in src, (
            "upload.js 又出现了私有 statusDotClass 实现（应走 PbcStatus）"
        )
