"""#127 — 配置级 LLM 故障的可见性（类型化异常 / job 级原因 / "绿色成功"假象）。

**要防的失效模式（GMP 假阴性）**：模型 API Key 失效时，每一页 Stage 2 都以
401 失败。改动前该故障只留下页级 ``_parse_error``：``jobs.error_message``
保持 NULL，前端在 partial_review 下无原因可显 —— 最终呈现为**绿点 +
"部分可复核" + 0 条 finding**，与"记录确实无异常"在界面上完全无法区分。

本文件锁四件事：

1. 非可重试错误抛**类型化**异常（且仍是 ``RuntimeError`` 子类 → 向后兼容）；
2. "非可重试"判据关键词表是**单一来源**，不再内联复制；
3. ``except LLMConfigError`` 必须排在 ``except Exception`` **之前**
   （否则被通用分支吞掉，故障退回页级 —— 这正是 #127 的根因）；
4. 前端源码层面：有失败页的 partial_review 不得显示为成功色，且必须能
   看到**原因**与**失败页**（照 ``test_review_tier`` / ``test_upload_limits``
   的源码扫描体例 —— 前端没有 JS 单测跑，护栏只能落在源码契约上）。
"""
import ast
import re
from pathlib import Path

import pytest

from llm.adapters.base import ChatResult
from llm.client import (
    LLMClient,
    LLMConfigError,
    _NON_RETRYABLE_KEYWORDS,
)
from core.pipeline.stage2 import config_error_job_message

REPO = Path(__file__).resolve().parents[2]
CLIENT_PY = REPO / "llm" / "client.py"
STAGE2_PY = REPO / "core" / "pipeline" / "stage2.py"
UPLOAD_JS = REPO / "static" / "upload.js"
REVIEW_JS = REPO / "static" / "review.js"
REVIEW_HTML = REPO / "templates" / "review.html"


class _FakeAdapter:
    """最小适配器：记录调用次数，按需抛出指定异常。"""

    def __init__(self, exc: Exception | None = None, protocol="openai"):
        self.protocol = protocol
        self.calls = 0
        self._exc = exc

    async def chat(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return ChatResult(content='{"ok": true}', model="test-model")

    def client_info(self):
        return {}


def _make_client(adapter) -> LLMClient:
    c = LLMClient.__new__(LLMClient)
    c.provider = "test"
    c.adapter = adapter
    c.model = "test-model"
    return c


# ===========================================================================
# 1. 类型化异常（llm/client.py）
# ===========================================================================


class TestTypedConfigError:
    def test_subclasses_runtimeerror_for_backward_compat(self):
        """既有 `except RuntimeError` 调用方与测试的语义必须不变。"""
        assert issubclass(LLMConfigError, RuntimeError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message",
        ["401 unauthorized: bad key", "Error code: 403 - forbidden",
         "400 Bad Request: invalid model"],
    )
    async def test_non_retryable_raises_typed_error_without_retry(self, message):
        """非可重试 → 抛 LLMConfigError 且**只调用一次**（重试无意义）。"""
        adapter = _FakeAdapter(exc=RuntimeError(message))
        client = _make_client(adapter)
        with pytest.raises(LLMConfigError) as excinfo:
            await client.chat("sys", "user", retries=3)
        assert adapter.calls == 1, "非可重试错误不得重试"
        # 原样保留 "non-retryable" 文案（既有日志/断言依赖它）
        assert "non-retryable" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_transient_error_is_not_typed_as_config_error(self):
        """瞬态错误（连接重置/超时）不得误判为配置级 —— 否则会误早停。"""
        adapter = _FakeAdapter(exc=ConnectionResetError("connection reset"))
        client = _make_client(adapter)
        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError)
        assert adapter.calls == 2, "瞬态错误应重试"

    def test_keywords_are_single_source(self):
        """判据关键词表必须只有一处定义（不得再内联复制）。"""
        src = CLIENT_PY.read_text(encoding="utf-8-sig")
        assert "_NON_RETRYABLE_KEYWORDS" in src
        assert (
            "is_non_retryable = any(kw in err_str for kw in ["
            not in src
        ), "非可重试关键词仍有内联副本 → 两处分级必然漂移"
        # 表内必须覆盖凭据类状态码（#127 的触发条件）
        for kw in ("401", "403", "invalid"):
            assert kw in _NON_RETRYABLE_KEYWORDS

    def test_job_message_is_actionable(self):
        """job 级原因文案必须同时说清：性质 / 首因 / 该做什么。"""
        msg = config_error_job_message("401 Token is invalid")
        assert "重试无效" in msg, "未说明性质（用户会以为重试能好）"
        assert "401 Token is invalid" in msg, "未带上首因"
        assert "API Key" in msg, "未给出可执行动作"


# ===========================================================================
# 2. 捕获顺序（否则类型化异常形同虚设）
# ===========================================================================


def _except_type_name(handler: ast.ExceptHandler) -> str:
    if handler.type is None:
        return "bare"
    return ast.unparse(handler.type)


class TestHandlerOrdering:
    def test_config_error_handler_precedes_generic_except(self):
        """同一 try 内 `except LLMConfigError` 必须在 `except Exception` 之前。

        LLMConfigError 是 RuntimeError 子类 —— 顺序写反就会被通用分支吞掉，
        "整份文档都分析不了"退回"某几页失败"（#127 的根因）。同理适用于
        今后任何新增的类型化异常，故做成全文件扫描而非单点断言。
        """
        tree = ast.parse(STAGE2_PY.read_text(encoding="utf-8-sig"))
        pairs_checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            names = [_except_type_name(h) for h in node.handlers]
            cfg = [i for i, n in enumerate(names) if "LLMConfigError" in n]
            gen = [i for i, n in enumerate(names) if n == "Exception"]
            if cfg and gen:
                pairs_checked += 1
                assert min(cfg) < min(gen), (
                    "stage2.py 中 except LLMConfigError 排在 except Exception "
                    "之后 → 配置级故障会被通用分支吞掉（#127 回归）"
                )
        assert pairs_checked >= 1, (
            "未找到 LLMConfigError/Exception 并存的 try — 类型化异常可能被删"
        )


# ===========================================================================
# 3. 前端源码契约
# ===========================================================================


class TestFrontendVisibilityContract:
    def _status_dot_body(self) -> str:
        js = UPLOAD_JS.read_text(encoding="utf-8")
        m = re.search(
            r"function statusDotClass\(st\)\s*\{(.*?)\n  \}", js, re.S
        )
        assert m, "upload.js 未找到 statusDotClass（改名了？同步更新本护栏）"
        return m.group(1)

    def test_partial_review_is_never_a_success_dot(self):
        """partial_review 的定义就是"有失败页/差异" → 永不可是成功色。"""
        body = self._status_dot_body()
        assert "bg-warning" in body, "partial_review 未降级为警示色"
        for cond, ret in re.findall(
            r'if \(([^)]+)\)\s*return "([^"]+)"', body
        ):
            if ret == "bg-success":
                assert "partial_review" not in cond, (
                    "partial_review 又被归入 bg-success → 0 条 finding 与"
                    "'记录无异常'不可区分（#127 回归）"
                )

    def test_failed_pages_are_rendered(self):
        """接口一直返回 failed_pages，界面此前零处渲染 → 用户看不到哪几页。

        #132：本用例只能证明"前端消费了该字段"，**证明不了类型对得上** ——
        原先只查标识符出现，而接口当时返回的其实是字符串 `"[2, 1]"`，
        于是这条"护栏"全程为绿。行为级护栏见
        `tests/integration/test_api_jobs_coverage.py::TestGetJobStatus::`
        `test_get_status_failed_pages_is_a_json_array`；此处同时锁住前端的
        类型预期，两端一起改才可能漂移。
        """
        js = UPLOAD_JS.read_text(encoding="utf-8")
        assert "job.failed_pages" in js, "failed_pages 未被前端消费"
        assert re.search(r"Array\.isArray\(\s*job\.failed_pages\s*\)", js), (
            "前端须按数组取用 failed_pages（接口契约是 list）；"
            "接口若回归成字符串，Array.isArray 会静默丢弃 → 失败页不可见"
        )

    def test_partial_review_shows_the_reason(self):
        """partial_review 下也必须显示 error_message（SSE + 历史行两处）。"""
        js = UPLOAD_JS.read_text(encoding="utf-8")
        assert '(st === "error" || st === "partial_review")' in js, (
            "SSE 实时更新仍只在 error 态显示原因"
        )
        assert '["error", "partial_review"].includes(st)' in js, (
            "历史任务行仍只在 error 态显示原因"
        )

    def test_page_banner_surfaces_actual_error_text(self):
        """页内横幅要写出真实原因，否则用户分不清坏 PDF / 网关 / 凭据失效。"""
        js = REVIEW_JS.read_text(encoding="utf-8")
        assert re.search(r"structured\._error", js), (
            "review.js 未消费 structured._error → 页级原因不可见"
        )
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert 'id="parse-error-text"' in html, (
            "review.html 横幅缺 id，JS 无处写入真实原因"
        )


STATUS_PY = REPO / "api" / "jobs" / "status.py"


class TestAnalyzedPagesSingleSource:
    """#131 — "已分析页数"只能有一个口径，不得复制判据。

    失败页**同样**会写 structured_json（带 `_parse_error` 标记），所以
    `structured_json IS NOT NULL` 并**不等于**"产出了可用结果"。该判据是
    "哪些页需要重试"的真值，权威定义在 `stage2._get_analyzed_pages`。
    接口若自行复制一份 SQL，两处迟早漂移 —— 且已经漂移过：产物级验收里
    终态 error 的原因文本写"0 页产出可用结果"，同一份响应却报
    `pages_analyzed=1`（failed_pages=[1,2]）。同一 payload 自相矛盾，
    GMP 审阅必问"到底分析了几页"。
    """

    def _exec_sql_literals(self) -> list[str]:
        """取出文件里所有 `db.execute("...")` 的 SQL 字面量。

        只扫**代码里的实参**，不扫注释/文档字符串 —— 文档里为解释口径而
        引用那段 SQL 是正常的（本文件自己的 docstring 就会引用它），
        扫文本会把它当违规，护栏就成了噪音。
        """
        tree = ast.parse(STATUS_PY.read_text(encoding="utf-8"))
        out: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "execute"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
        return out

    def test_status_api_has_no_duplicated_predicate(self):
        sqls = self._exec_sql_literals()
        assert sqls, "未扫到任何 db.execute SQL —— AST 扫描失效，护栏形同虚设"
        offenders = [s for s in sqls if "structured_json IS NOT NULL" in s]
        assert not offenders, (
            f"api/jobs/status.py 又自行统计已分析页：{offenders!r} —— "
            "必须复用 core.pipeline.stage2._get_analyzed_pages（单一真值源）"
        )

    def test_both_entrypoints_reuse_canonical_helper(self):
        src = STATUS_PY.read_text(encoding="utf-8")
        assert "from core.pipeline.stage2 import _get_analyzed_pages" in src, (
            "_count_analyzed_pages 未复用规范函数"
        )
        n = src.count("await _count_analyzed_pages(db, job_id)")
        assert n >= 2, (
            f"GET /{{job_id}} 与 _get_job_progress 都应走同一口径，实测 {n} 处"
        )
