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


# ─────────────────────────────────────────────────────────────────────────────
# B2-10 前端「过渡期与口径」四处的机检
#
#   ① `type=error` 两类帧不分：瞬态「进度查询失败」（服务端发完 continue）被
#      当成终态「任务不存在」（服务端发完 return）⇒ **关流 + 文案说谎**；
#   ② 终态过渡期文案显示**裸英文 token**，与同页中文徽章自相矛盾；
#   ③ error 转态期不显示原因（要等 1.5s 自动刷新后由 SSR 横幅给出）；
#   ④ 状态中文映射在页面内多存了一份副本 ⇒ **文字与颜色不同源**。
#
# 判据取向（§二十八）：不查"文本里有没有某个字符串"，而查
#   - 纯函数的**行为**（node 实跑，可被变异打红）；
#   - 源码的**包围条件与相对位置**（花括号配对提取后再判先后）；
#   - 且每个"找不到"都配一个**正向对照**证明提取器没坏（防空断言）。
# ─────────────────────────────────────────────────────────────────────────────

# job 状态**专属**键 —— 与 finding.status（pending/confirmed/rejected/corrected）
# 及 finding.type 的键集不相交，用它才能在源码里精确定位"又冒出一份 job 状态
# 中文映射"，而不会误伤本页另外两份**合法**的映射（finding 状态/类型）。
JOB_ONLY_STATUS_KEYS = (
    "ocr_running", "ocr_done", "analyzing", "partial_review",
    "queued", "processing", "cancelling", "archived",
)


def _run_in_node(probe_body: str):
    """跑 `static/status.js` + 探针，返回探针最后一行 JSON。"""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "status_probe.js"
        p.write_text(
            STATUS_JS.read_text(encoding="utf-8") + "\n" + probe_body,
            encoding="utf-8",
        )
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _sse_error_action(frame: dict):
    return _run_in_node(
        "console.log(JSON.stringify(PbcStatus.sseErrorAction(%s)));"
        % json.dumps(frame, ensure_ascii=False)
    )


def _zh_map(statuses) -> dict:
    return _run_in_node(
        "const o = {};\nfor (const s of %s) o[s] = PbcStatus.statusZh(s);\n"
        "console.log(JSON.stringify(o));" % json.dumps(list(statuses))
    )


def _braced_block_at(src: str, open_idx: int) -> str | None:
    """从 `{` 开始做花括号配对，返回**内部**文本（不含最外层括号）。"""
    depth = 0
    for j in range(open_idx, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:j]
    return None


def _job_status_map_literals(src: str) -> list[str]:
    """找出把 job 状态枚举当**对象键**写死的字面量块。

    做法：先命中 job 专属键，再向左找最近的对象字面量 `{`（且中间不得跨语句），
    配对后统计该块里 job 专属键的数量 —— ≥3 才认定为"又一份 job 状态映射"。
    """
    out: list[str] = []
    seen: set[int] = set()
    key_pat = re.compile(r"\b(%s)\s*:" % "|".join(JOB_ONLY_STATUS_KEYS))
    for m in key_pat.finditer(src):
        open_idx = src.rfind("{", 0, m.start())
        if open_idx == -1 or open_idx in seen:
            continue
        between = src[open_idx:m.start()]
        if ";" in between or "=>" in between or "\n\n" in between:
            continue  # 跨语句/跨函数，不是同一个字面量
        seen.add(open_idx)
        blk = _braced_block_at(src, open_idx)
        if blk is None:
            continue
        keys = set(re.findall(r"(?:^|[{,\s])([a-z_]+)\s*:", blk))
        if len(keys & set(JOB_ONLY_STATUS_KEYS)) >= 3:
            out.append(" ".join(blk.split())[:120])
    return out


def _review_error_branch_block() -> str:
    """抠出 review.js 里 `if (d.type === "error") { ... }` 的**完整块**。

    只查"全文有没有 es.close()"是盲的（它同时出现在 done/onerror 分支）
    —— 必须落在**包围条件**上，故这里做配对提取后再判相对位置。
    """
    src = REVIEW_JS.read_text(encoding="utf-8")
    anchor = 'if (d.type === "error")'
    assert anchor in src, "review.js 里找不到错误帧分支（被改名/删除了？）"
    i = src.index(anchor)
    blk = _braced_block_at(src, src.index("{", i))
    assert blk is not None, "错误帧分支花括号未配平（解析失败）"
    return blk


class TestSseErrorFrameAdjudication:
    """B2-10 ①：两类 error 帧的处置判定。

    服务端（api/jobs/status.py）会发两类 `type=error` 帧：
    「进度查询失败」(terminal=false，发完 `continue`) 与
    「任务不存在」(terminal=true，发完 `return`)。复核页曾只判
    `d.type === "error"` 就把两者都当终态 ⇒ **一次 DB 抖动被谎报成
    "任务被删"，且进度流永久断开**（既丢实时更新，又理由说谎）。
    """

    def test_transient_frame_keeps_stream(self):
        assert _sse_error_action(
            {"type": "error", "terminal": False, "message": "进度查询失败"}
        ) == "transient"

    def test_terminal_frame_closes_stream(self):
        assert _sse_error_action(
            {"type": "error", "terminal": True, "message": "任务不存在"}
        ) == "terminal"

    def test_non_error_frame_is_ignored(self):
        """正常进度帧（哪怕带 terminal 字段）不得被当成错误帧。"""
        assert _sse_error_action({"status": "analyzing", "terminal": True}) is None
        assert _sse_error_action({"status": "review"}) is None

    def test_judgement_does_not_depend_on_message_text(self):
        """反向控制：**没有** terminal 字段时不得凭 message 文案判定。

        否则"改文案"会静默改变控制流 —— 这正是本缺陷的成因形态，
        也是"日志/理由说谎"一类的复发通道（B2-6/B2-12 同族）。
        """
        for msg in ("任务不存在", "进度查询失败"):
            assert _sse_error_action({"type": "error", "message": msg}) == "transient"

    def test_missing_terminal_is_fail_safe_toward_keeping_stream(self):
        """字段缺失 ⇒ 取"瞬态"（宁可多留一会儿连接，也不谎报任务被删）。

        另一侧由集成用例锁定服务端两类帧必带该字段，故不会长期缺失。
        """
        assert _sse_error_action({"type": "error"}) == "transient"


class TestSharedStatusZhIsSingleSource:
    """B2-10 ②：SSE 用的中文映射必须是共享件且覆盖终态。"""

    def test_live_terminal_statuses_are_chinese_not_raw_tokens(self):
        zh = _zh_map(["review", "partial_review", "done"])
        assert zh["review"] == "可复核"
        assert zh["partial_review"] == "部分可复核"
        assert zh["done"] == "已完成"
        for k, v in zh.items():
            assert not re.search(r"[a-z_]{4,}", v), f"{k} 仍输出裸英文 token：{v!r}"

    def test_unknown_status_is_flagged(self):
        """未知状态兜底要能看出"这是我没见过的状态"，而不是以为界面坏了。"""
        assert _zh_map(["brand_new_status"])["brand_new_status"] == (
            "未知(brand_new_status)"
        )

    def test_js_covers_every_python_job_status(self):
        """键集必须互相覆盖 —— 后端新增状态而前端漏改就会被抓到。

        ⚠️ 只锁**键集**，不锁**措辞**：SSR(Python) 与 SSE(JS) 的中文措辞目前
        确有差异（ocr_running「OCR 解析中」vs「识别中」、error「失败」vs
        「出错」等，见 docs/TODO.md B2-10 附注），统一措辞会改变用户可见文案，
        须单独评估，不在本护栏范围内。
        """
        js_keys = set(_run_in_node(
            "console.log(JSON.stringify(Object.keys(PbcStatus.STATUS_ZH)));"
        ))
        missing = sorted(set(JOB_STATUS_ZH) - js_keys)
        assert not missing, f"status.js 缺少这些 job 状态的中文映射：{missing}"


class TestReviewJsHasNoSecondStatusTruth:
    """B2-10 ④：复核页不得再自带一份 job 状态中文映射（文字与颜色同源）。"""

    def test_no_job_status_map_literal_in_review_js(self):
        src = REVIEW_JS.read_text(encoding="utf-8")
        found = _job_status_map_literals(src)
        assert not found, (
            "review.js 又出现了 job 状态中文映射副本 —— 状态点颜色走共享件、"
            f"文字却各持一份，改一处改不动另一处：\n{found}"
        )

    def test_detector_is_not_vacuous(self):
        """正向对照：同一个提取器在**真值文件**上必须找得到映射。

        否则上面的"找不到"可能只是提取器坏了（空断言）—— 这才是最常见
        的假绿来源。
        """
        found = _job_status_map_literals(STATUS_JS.read_text(encoding="utf-8"))
        assert found, "提取器在单一真值文件上也找不到映射 ⇒ 本护栏是空断言"

    def test_review_js_consumes_shared_zh_for_badge_and_label(self):
        """② 与 ④ 的共同落点：徽章与进度文案都走共享件（不再是本地副本）。"""
        src = REVIEW_JS.read_text(encoding="utf-8")
        assert src.count("PbcStatus.statusZh") >= 2, (
            "review.js 应至少有两处消费共享中文映射（状态徽章 + 进度文案兜底）"
        )
        assert "label = d.status; // 未知状态兜底显示原始值" not in src, (
            "进度文案兜底又直接显示原始英文 token 了（② 回归）"
        )


class TestReviewJsErrorBranchGuardsStreamClose:
    """B2-10 ①：`es.close()` 必须被 terminal 判定**包围**，不得裸调。

    判据落在"包围条件 + 相对位置"（§二十八：文本在 ≠ 运行期可达）：
    单看"全文含 es.close()"区分不出它到底在终态分支还是瞬态分支。
    """

    def test_close_happens_after_terminal_decision(self):
        blk = _review_error_branch_block()
        assert "sseErrorAction" in blk, "错误帧分支未使用共享判定函数"
        assert "es.close()" in blk
        assert blk.index("terminal") < blk.index("es.close()"), (
            "es.close() 出现在 terminal 判定**之前** ⇒ 瞬态抖动仍会断流"
        )
        assert 'if (action !== "terminal")' in blk, (
            "缺少显式的瞬态分支（瞬态必须保持长连）"
        )

    def test_transient_branch_does_not_close_stream(self):
        blk = _review_error_branch_block()
        head = blk[: blk.index("es.close()")]
        transient = head[head.index('if (action !== "terminal")'):]
        assert "es.close()" not in transient
        assert "clearInterval(pollTimer)" not in transient, (
            "瞬态分支不得清掉轮询兜底定时器"
        )

    def test_branch_does_not_match_on_message_text(self):
        """反向断言：错误帧分支里不得出现服务端的 message 文案字面量。"""
        blk = _review_error_branch_block()
        assert "进度查询失败" not in blk, (
            "错误帧分支按 message 文案判定 ⇒ 改文案会静默改变控制流"
        )


class TestReviewJsSurfacesErrorReason:
    """B2-10 ③：error 转态期就要能看到原因（不必等 1.5s 自动刷新）。"""

    def test_reason_is_rendered_from_frame_payload(self):
        src = REVIEW_JS.read_text(encoding="utf-8")
        assert "showJobErrorBanner" in src, "缺少按需渲染错误横幅的辅助函数"
        # ⚠️ 断言必须落在**消费点**上，不能只要求"某标识符出现过"：
        # 变异验证暴露过一次 —— 把调用改成 `showJobErrorBanner(undefined)` 时，
        # 因为**条件行** `if (d.status === "error" && d.error_message)` 里还有
        # 一处 `d.error_message`，只查"出现过"的断言仍然全绿（§二十八：
        # 文本在 ≠ 运行期可达）。
        assert "showJobErrorBanner(d.error_message)" in src, (
            "转态期原因未真正传给横幅（error_message 是服务端进度快照已带字段）"
        )
        assert 'if (d.status === "error" && d.error_message)' in src, (
            "应以 status=error 为条件，避免把无关帧的同名字段当原因渲染"
        )

    def test_banner_id_matches_ssr_template(self):
        """JS 复用 SSR 的横幅 ⇒ 两侧 id 必须一致，否则复用落空、横幅永不出现。"""
        assert 'id="job-error-banner"' in REVIEW_HTML.read_text(encoding="utf-8")
        assert '"job-error-banner"' in REVIEW_JS.read_text(encoding="utf-8")
