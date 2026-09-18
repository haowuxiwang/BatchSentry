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
    _CONFIG_ERROR_KEYWORDS,
)
from core.pipeline.stage2 import config_error_job_message

REPO = Path(__file__).resolve().parents[2]
CLIENT_PY = REPO / "llm" / "client.py"
STAGE2_PY = REPO / "core" / "pipeline" / "stage2.py"
UPLOAD_JS = REPO / "static" / "upload.js"
REVIEW_JS = REPO / "static" / "review.js"
# #133：状态→颜色/中文的单一真值（upload.js 与 review.js 共同依赖）
STATUS_JS = REPO / "static" / "status.js"
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
        assert "_CONFIG_ERROR_KEYWORDS" in src
        assert (
            "is_config_error = any(kw in err_str for kw in ["
            not in src
        ), "配置级关键词仍有内联副本 → 两处分级必然漂移"
        # 表内必须覆盖凭据类状态码（#127 的触发条件）。#143 后状态码
        # **必须带词边界** —— 裸 "400" 会命中限流文本里的 "Limit 40000"。
        for kw in (r"\b401\b", r"\b403\b"):
            assert kw in _CONFIG_ERROR_KEYWORDS, f"{kw} 缺失，凭据故障会漏判"

    def test_job_message_is_actionable(self):
        """job 级原因文案必须同时说清：性质 / 首因 / 该做什么。"""
        msg = config_error_job_message("401 Token is invalid")
        assert "重试无效" in msg, "未说明性质（用户会以为重试能好）"
        assert "401 Token is invalid" in msg, "未带上首因"
        assert "API Key" in msg, "未给出可执行动作"


# ===========================================================================
# 1b. 「配置级」判据的精度（#143）
# ===========================================================================


def _sdk_error(cls, status: int, message: str):
    """构造真实的 openai SDK HTTP 异常（带 .status_code / .request）。

    必须用**真实异常对象**而不是 `RuntimeError("429 ...")`：#143 的要害
    正是"异常自带状态码、却被丢开不用，改去猜错误文本"。用裸 RuntimeError
    测不出这一点（它压根没有 status_code）。
    """
    import httpx
    import openai  # noqa: F401  (确认 SDK 可用，异常类型来自它)

    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    resp = httpx.Response(status, request=req, json={"message": message})
    return cls(message, response=resp, body=None)


class TestConfigErrorClassificationPrecision:
    """#143 — 「配置级」与「可重试」必须分得清，否则误早停 + 误导排障。

    **失效模式（实测复现，非推测）**：判据曾是整段错误串的子串匹配，词表含
    裸 `"400"` 与裸 `"invalid"`。于是：

    ① 真实限流 `429 Rate limit reached ... Limit 40000` 命中 `"400"`
       ⇒ 判成配置级 ⇒ Stage 2 **整份早停** + 提示"请检查 API Key"。
       用户遇到的是一次可自愈的限流，看到的却是"凭据失效、重试无效"。
    ② 本地 `ValueError("invalid literal for int() ...")` 命中 `"invalid"`
       ⇒ 同样是配置级。同一进程内每个页/每次重试都重复这一误报。

    ⚠️ 反向风险同等重要：**不能**为了修 ① ② 就把判据放宽成"一律可重试" ——
    那会让真正的 401 退化成重试 N 次后仍以页级失败收场，正是 #127 要消灭的
    假阴性。所以本类**同时**锁住两侧（含 401/400 的对照组）。
    """

    @pytest.mark.asyncio
    async def test_real_rate_limit_is_retryable_not_config_error(self):
        """真实 openai.RateLimitError(429)：限流是瞬态，必须重试、不得早停。"""
        import openai

        exc = _sdk_error(
            openai.RateLimitError, 429,
            "Rate limit reached for requests: Limit 40000, Used 40000.",
        )
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError), (
            "429 限流被判成配置级 ⇒ Stage 2 会整份早停并提示『请检查 API Key』"
            "（限流本可自愈）。判据必须先看 exception.status_code，"
            "而不是在错误文本里找 '400' —— 'Limit 40000' 里就有个 400"
        )
        assert adapter.calls == 2, "限流必须重试"

    @pytest.mark.asyncio
    async def test_server_5xx_is_retryable(self):
        """上游 5xx 同为瞬态（status_code 结构化判据的对照组）。"""
        import openai

        exc = _sdk_error(openai.InternalServerError, 500, "internal error")
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError)
        assert adapter.calls == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("invalid literal for int() with base 10: 'N/A'"),
            KeyError("invalid key: 'choices'"),
            TypeError("unsupported operand type(s) for -: 'NoneType' and 'int'"),
        ],
    )
    async def test_local_programming_error_is_not_config_error(self, exc):
        """本地解析/类型错误与凭据无关：不得据此提示"请检查 API Key"。

        这类错误会以同样方式重复出现，但**性质**是代码缺陷，不是配置故障；
        把它当配置级会（a）给出与真实原因不符的处置建议，（b）让整份文档
        在第一个本地错误上就停摆。
        """
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=1)
        assert not isinstance(excinfo.value, LLMConfigError), (
            f"本地错误 {type(exc).__name__} 被判成配置级 —— 判据仍在做"
            "裸子串匹配（'invalid' 会命中 'invalid literal'）"
        )

    @pytest.mark.asyncio
    async def test_rate_limit_with_lone_400_in_text_is_still_retryable(self):
        """限流文本里出现**独立成词**的 400 ⇒ 只有状态码能救。

        这是"词边界收窄"治不了的那种：`400 requests per minute` 是很常见的
        限额写法，`\\b400\\b` 会命中它。若判据只看文本，这一刻流又会被升级成
        "凭据失效、整份早停"。⇒ 有 status_code 时**必须**以它为准，文本判据
        一行都不该参与。
        """
        import openai

        exc = _sdk_error(
            openai.RateLimitError, 429,
            "Rate limit reached: 400 requests per minute",
        )
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(RuntimeError) as excinfo:
            await client.chat("sys", "user", retries=2)
        assert not isinstance(excinfo.value, LLMConfigError), (
            "429 被判成配置级 —— 判据仍在用错误文本覆盖 HTTP 状态码"
        )
        assert adapter.calls == 2

    @pytest.mark.asyncio
    async def test_real_auth_error_is_still_config_error(self):
        """对照组：真实 401 仍必须是一次即止的配置级故障（#127 不得回归）。"""
        import openai

        exc = _sdk_error(openai.AuthenticationError, 401, "Token is invalid")
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(LLMConfigError):
            await client.chat("sys", "user", retries=3)
        assert adapter.calls == 1, "凭据失效重试无意义"

    @pytest.mark.asyncio
    async def test_real_bad_request_is_still_config_error(self):
        """对照组：请求本身非法（400）重试同样无意义。"""
        import openai

        exc = _sdk_error(openai.BadRequestError, 400, "invalid model")
        adapter = _FakeAdapter(exc=exc)
        client = _make_client(adapter)
        with pytest.raises(LLMConfigError):
            await client.chat("sys", "user", retries=3)
        assert adapter.calls == 1

    @pytest.mark.asyncio
    async def test_text_fallback_still_catches_credential_errors(self):
        """兜底路径：拿不到 status_code 时（自研/包装 SDK），文本判据仍需生效。"""
        adapter = _FakeAdapter(exc=RuntimeError("401 unauthorized: bad key"))
        client = _make_client(adapter)
        with pytest.raises(LLMConfigError):
            await client.chat("sys", "user", retries=3)
        assert adapter.calls == 1

    @pytest.mark.asyncio
    async def test_fallback_judges_type_not_only_text(self):
        """**同一句文本**，编程错误类型与包装层异常必须得到相反判定。

        这是"本地错误不得报成凭据故障"的最小充分结构：只收窄关键词
        （去掉裸 `invalid`）治不了 `KeyError("invalid key")` 这类
        "文本恰好长得像凭据错误"的代码缺陷，必须在类型上划清界限。
        """
        for exc, expect_config in (
            (RuntimeError("invalid key"), True),       # 包装层：可能是凭据
            (KeyError("invalid key"), False),          # 本地：字典键缺失
        ):
            adapter = _FakeAdapter(exc=exc)
            client = _make_client(adapter)
            with pytest.raises(RuntimeError) as excinfo:
                await client.chat("sys", "user", retries=1)
            got = isinstance(excinfo.value, LLMConfigError)
            assert got is expect_config, (
                f"{type(exc).__name__}({exc!r}) 判成 {'配置级' if got else '可重试'}，"
                f"应为 {'配置级' if expect_config else '可重试'}"
            )

    def test_classifier_consults_status_code_before_text(self):
        """结构判据必须**优先**且独立成函数（单一真值），不得内联回子串匹配。

        只断言行为不够：把 status_code 分支写成"文本匹配之后再补一刀"也能
        让上面的用例全绿，却留下'文本里恰好没关键词的真实 429'这类漏网。
        故额外锁住结构。
        """
        src = CLIENT_PY.read_text(encoding="utf-8-sig")
        assert "status_code" in src, "分级判据未使用 exception.status_code"
        assert "def is_config_error(" in src, (
            "分级判据未抽成独立函数 —— 内联在重试循环里就没有单一真值可测"
        )
        # 裸 "invalid" 必须从词表消失（它是本地 ValueError 误判的成因）
        assert '"invalid"' not in _CONFIG_ERROR_KEYWORDS, (
            '词表含裸 "invalid" ⇒ 本地 ValueError("invalid literal …") '
            "会被判成凭据故障（#143 实测复现）"
        )
        assert not any(k in _CONFIG_ERROR_KEYWORDS for k in ("400", "401", "403")), (
            '词表含无词边界的裸状态码（"400" 会命中 "Limit 40000"）'
            "—— 状态码应走结构化判据，文本兜底一律带 \\b 词边界"
        )


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
        """取状态点颜色映射的实现体。

        #133：真值源已从 `upload.js` 抽到共享件 `static/status.js`
        （原因见那个文件头部——`upload.js` 与 `review.js` 各持一份，
        导致复核页画了**硬编码的绿点**）。本护栏因此改为读共享件；
        断言内容（partial_review 永不为绿）不变。

        ⚠️ 若哪天共享件又改名，`assert m` 会立刻报出来，不会静默通过。
        """
        js = STATUS_JS.read_text(encoding="utf-8")
        m = re.search(
            r"function statusDotClass\(st\)\s*\{(.*?)\n  \}", js, re.S
        )
        assert m, "static/status.js 未找到 statusDotClass（改名了？同步更新本护栏）"
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
