# -*- coding: utf-8 -*-
"""#145 护栏：**批记录正文不得进入日志、也不得进入持久化载荷**。

背景：`llm/client.py` 有 3 处把模型原始输出（= 批记录正文）前 200 字写进
`pharma.log`；`_raw` 载荷另有 3 个生产者，会经 `stage2._error` 落到
`page_cache` 并由 `api/review.get_page_data` 整份回给浏览器；
`page_analyzer._parse_error_payload`（2000 字正文）更是**零消费者**的死载荷。

## 判据分层（诚实边界）

* **行为级**（强）：`raw_digest` 的输入输出、`_parse_json` 的真实日志记录、
  `setup_logging` 的真实落盘结果、`analyze_page` 的真实返回结构。
* **结构级**（弱，仅补行为级够不到的地方）："3 个生产者是否都收口"用
  **AST** 判定 —— 不用子串，因为本仓库的注释与 docstring 里会**引用**被禁写法，
  子串判定会被自己的说明文字绊倒（verification-integrity Trap 57，
  本会话实测踩了 4 次）。

⚠️ **已知残余风险（显式登记）**：`_parse_error` 分支的 `_raw` 是**透传**的
（不重新摘要 —— 摘要的摘要会毁掉"两次失败是不是同一份响应"的可关联性）。
所以该分支的安全性**依赖生产者自觉**。生产者在生产侧只有一处
（`llm/client.py`），已由 `TestAllRawProducersAreClosed` 结构锁定。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT_PY = ROOT / "llm" / "client.py"
PAGE_ANALYZER_PY = ROOT / "core" / "page_analyzer.py"
MAIN_JS = ROOT / "electron" / "main.js"

# 一段**像真批记录**的正文。任何出口只要带上它就算泄漏。
RECORD_BODY = "批号 1127011N250101 含量 99.1% 结论：合格"
SECRET_TOKEN = "1127011N250101"
SECRET_MEASURE = "99.1"


# ── AST 工具：让结构判据对注释 / docstring 免疫 ────────────────────────────


def _subscript_slice_lines(src: str, var: str) -> list[int]:
    """对名为 `var` 的变量做**切片**的行号（`var[...]`，AST 级）。"""
    hits: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == var
                and isinstance(node.slice, ast.Slice)):
            hits.append(node.lineno)
    return hits


def _call_slice_lines(src: str, func: str) -> list[int]:
    """对 `func(...)` 的**返回值**做切片的行号（`func(...)[...]`，AST 级）。"""
    hits: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == func
                and isinstance(node.slice, ast.Slice)):
            hits.append(node.lineno)
    return hits


def _dict_keys(src: str) -> set[str]:
    """源码里所有 dict **字面量**的字符串键。"""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


# ── 1. raw_digest 本身 ────────────────────────────────────────────────────


class TestRawDigest:
    """摘要函数的四条不变式。"""

    def test_detector_is_not_vacuous(self):
        """防空转：确认这串正文里**确实**有可被判为"泄漏"的东西。"""
        assert SECRET_TOKEN in RECORD_BODY
        assert SECRET_MEASURE in RECORD_BODY

    def test_no_body_leaks_through(self):
        from llm.client import raw_digest

        d = raw_digest(RECORD_BODY)
        assert SECRET_TOKEN not in d
        assert SECRET_MEASURE not in d
        assert "合格" not in d

    def test_is_deterministic(self):
        from llm.client import raw_digest

        assert raw_digest(RECORD_BODY) == raw_digest(RECORD_BODY)

    def test_distinguishes_different_responses(self):
        """可关联性：不同响应必须给出不同摘要，否则排障价值归零。"""
        from llm.client import raw_digest

        assert raw_digest("abc") != raw_digest("abd")

    def test_keeps_the_response_length_token(self):
        """`response_length=` 刻意保留 —— 既有排障习惯靠它 grep。"""
        from llm.client import raw_digest

        d = raw_digest(RECORD_BODY)
        assert "response_length=" in d
        assert f"response_length={len(RECORD_BODY)}" in d


# ── 2. 真实日志出口不含正文（行为级） ──────────────────────────────────────


class TestParseFailureLogCarriesNoBody:
    """`_parse_json` 的真实失败路径。"""

    def test_log_and_payload_exclude_the_body(self, caplog):
        from llm.client import LLMClient

        with caplog.at_level(logging.WARNING, logger="llm.client"):
            result = LLMClient._parse_json("这不是 JSON " + RECORD_BODY)

        assert isinstance(result, dict) and result.get("_parse_error") is True, \
            "没走到 _parse_error 分支 ⇒ 后面的断言会变成空断言"
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert msgs, "一条日志都没捕到 ⇒ 断言会变成空断言"
        assert SECRET_TOKEN not in msgs, "正文进了日志"
        assert SECRET_MEASURE not in msgs, "正文进了日志"
        # 回传给上层的 `_raw` 同样不得含正文
        assert SECRET_TOKEN not in str(result.get("_raw", ""))
        assert "sha256=" in str(result.get("_raw", ""))


# ── 3. 三个生产者是否都收口（结构级，AST） ─────────────────────────────────


class TestAllRawProducersAreClosed:
    """只修先发现的那一处出口等于没修（#146 的教训）。"""

    @pytest.fixture(scope="class")
    def client_src(self) -> str:
        return CLIENT_PY.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def analyzer_src(self) -> str:
        return PAGE_ANALYZER_PY.read_text(encoding="utf-8")

    def test_detector_is_not_vacuous(self, client_src):
        """防空转：确认这两个文件里确实有会写原始输出的地方。"""
        assert client_src.count("logger.warning") >= 3
        assert "raw_digest(raw)" in client_src

    def test_no_content_truncation_in_client(self, client_src):
        assert _subscript_slice_lines(client_src, "raw") == [], \
            "`llm/client.py` 里仍有对 raw 的切片（内容截断）出口"
        assert "first_200" not in client_src

    def test_every_client_site_routes_through_the_helper(self, client_src):
        # 3 个日志出口 + 1 个 `_raw` 载荷
        assert client_src.count("raw_digest(raw)") == 4, (
            f"raw_digest(raw) 调用点 = {client_src.count('raw_digest(raw)')}，期望 4"
        )

    def test_dead_payload_key_is_gone(self, analyzer_src):
        assert "_parse_error_payload" not in _dict_keys(analyzer_src), \
            "零消费者的 2000 字正文载荷又回来了"

    def test_analyzer_digests_its_own_producers(self, analyzer_src):
        assert analyzer_src.count("raw_digest(str(result))") == 2, \
            "list 分支与非 dict 分支都要收口"
        assert _call_slice_lines(analyzer_src, "str") == [], \
            "`str(result)[:500]` 这类内容截断又回来了"


# ── 4. 日志级别：第三方降噪，但不误伤自家 DEBUG（行为级） ──────────────────


class TestThirdPartySilencedWithoutLosingOurOwnDebug:
    """必须**成对**断言：只证"第三方静了"会被"把 root 抬到 WARNING"蒙混过关。"""

    @pytest.fixture(autouse=True)
    def restore_logging_state(self, monkeypatch):
        """`setup_logging` 会改全局 logger 状态，测完必须还原。"""
        import logging_config as lc

        monkeypatch.delenv("PBC_NO_FILE_LOG", raising=False)
        root = logging.getLogger()
        before_handlers = list(root.handlers)
        before_root_level = root.level
        names = list(lc._NOISY_THIRD_PARTY_LOGGERS) + ["core.pipeline"]
        before_levels = {n: logging.getLogger(n).level for n in names}
        before_pipeline_handlers = list(logging.getLogger("core.pipeline").handlers)
        yield
        root.handlers[:] = before_handlers
        root.setLevel(before_root_level)
        for n, lv in before_levels.items():
            logging.getLogger(n).setLevel(lv)
        logging.getLogger("core.pipeline").handlers[:] = before_pipeline_handlers

    def test_noisy_loggers_are_at_warning(self, tmp_path):
        import logging_config as lc

        lc.setup_logging(log_dir=str(tmp_path / "logs"), level="INFO")
        for name in lc._NOISY_THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.WARNING, \
                f"{name} 未被降噪（第三方 DEBUG 会灌满 pharma.log）"

    def test_root_stays_at_debug(self, tmp_path):
        """防空转：证明我们是**逐库降级**，不是把 root 抬高（那会连自家 DEBUG 一起丢）。"""
        import logging_config as lc

        lc.setup_logging(log_dir=str(tmp_path / "logs"), level="INFO")
        assert logging.getLogger().level == logging.DEBUG

    def test_our_own_debug_still_reaches_the_file(self, tmp_path):
        """成对断言的另一半：自家 DEBUG 必须**还在**。"""
        import logging_config as lc

        log_dir = tmp_path / "logs"
        lc.setup_logging(log_dir=str(log_dir), level="INFO")
        logging.getLogger("core.pipeline.fake").debug("OWN_DEBUG_SENTINEL")
        text = (log_dir / "pharma.log").read_text(encoding="utf-8")
        assert "OWN_DEBUG_SENTINEL" in text, "自家 DEBUG 被误伤 ⇒ 排障信息丢失"

    def test_third_party_debug_does_not_reach_the_file(self, tmp_path):
        import logging_config as lc

        log_dir = tmp_path / "logs"
        lc.setup_logging(log_dir=str(log_dir), level="INFO")
        logging.getLogger("httpx").debug("THIRD_PARTY_DEBUG_SENTINEL")
        text = (log_dir / "pharma.log").read_text(encoding="utf-8")
        assert "THIRD_PARTY_DEBUG_SENTINEL" not in text, \
            "第三方 DEBUG 仍落盘（10MB × 5 份很快滚完）"


class TestPbcLogLevel:
    """`PBC_LOG_LEVEL` 只作用于文件 handler，且非法值不静默。"""

    @pytest.fixture(autouse=True)
    def enable_file_logging(self, monkeypatch):
        """conftest 设了 `PBC_NO_FILE_LOG=1`；不变量它 `setup_logging` 会提前返回、
        **一个文件 handler 都不建** —— 那样"级别生效"的断言就是空断言。"""
        monkeypatch.delenv("PBC_NO_FILE_LOG", raising=False)

    def test_absent_defaults_to_debug(self):
        from logging_config import resolve_file_log_level

        level, warn = resolve_file_log_level({})
        assert level == logging.DEBUG
        assert warn is None

    def test_honours_a_valid_level(self):
        from logging_config import resolve_file_log_level

        level, warn = resolve_file_log_level({"PBC_LOG_LEVEL": "warning"})
        assert level == logging.WARNING
        assert warn is None

    def test_invalid_level_falls_back_but_warns(self):
        """不静默：一个拼错的变量名不能被读成"我已经调低日志了"。"""
        from logging_config import resolve_file_log_level

        level, warn = resolve_file_log_level({"PBC_LOG_LEVEL": "VERBOSE"})
        assert level == logging.DEBUG
        assert warn and "VERBOSE" in warn

    def test_file_handler_level_follows_the_env(self, tmp_path, monkeypatch):
        import logging_config as lc

        root = logging.getLogger()
        before = list(root.handlers)
        try:
            monkeypatch.setenv("PBC_LOG_LEVEL", "WARNING")
            log_dir = tmp_path / "logs"
            lc.setup_logging(log_dir=str(log_dir), level="INFO")
            logging.getLogger("core.pipeline.fake").debug("SHOULD_NOT_BE_WRITTEN")
            logging.getLogger("core.pipeline.fake").warning("SHOULD_BE_WRITTEN")
            text = (log_dir / "pharma.log").read_text(encoding="utf-8")
            assert "SHOULD_BE_WRITTEN" in text
            assert "SHOULD_NOT_BE_WRITTEN" not in text, \
                "PBC_LOG_LEVEL=WARNING 没有拦住 DEBUG 落盘"
        finally:
            root.handlers[:] = before


# ── 5. analyze_page 的返回结构（行为级） ──────────────────────────────────


class TestAnalyzePagePayload:
    """`analyze_page` 的 `_parse_error` 返回体不得携带正文。"""

    def _mock(self, value):
        client = MagicMock()
        client.chat_json = AsyncMock(return_value=value)
        return client

    @pytest.mark.asyncio
    async def test_list_branch_digests_the_body(self):
        from core.page_analyzer import analyze_page

        payload = ["不是 dict " + RECORD_BODY, 42]
        with patch("core.page_analyzer.get_llm_client", return_value=self._mock(payload)):
            result = await analyze_page("<table></table>", page_num=4)

        assert result["_parse_error"] is True
        assert SECRET_TOKEN not in str(result.get("_raw", "")), "list 分支漏正文"
        assert "sha256=" in str(result.get("_raw", ""))

    @pytest.mark.asyncio
    async def test_scalar_branch_digests_the_body(self):
        from core.page_analyzer import analyze_page

        with patch("core.page_analyzer.get_llm_client",
                   return_value=self._mock("纯文本 " + RECORD_BODY)):
            result = await analyze_page("<table></table>", page_num=5)

        assert result["_parse_error"] is True
        assert SECRET_TOKEN not in str(result.get("_raw", "")), "非 dict 分支漏正文"

    @pytest.mark.asyncio
    async def test_dead_payload_is_not_returned(self):
        from core.page_analyzer import analyze_page

        malformed = {"_parse_error": True, "_raw": "response_length=3 sha256=abc"}
        with patch("core.page_analyzer.get_llm_client", return_value=self._mock(malformed)):
            result = await analyze_page("<table></table>", page_num=6)

        assert result["_parse_error"] is True
        assert "_parse_error_payload" not in result, "零消费者的死载荷又回来了"
        # 契约：该分支**透传**生产者的 `_raw`（生产者负责摘要，见模块 docstring）
        assert result["_raw"] == "response_length=3 sha256=abc"


# ── 6. Electron 启动日志上限（**静态**判据，见下方说明） ────────────────────


class TestBootLogHasASizeCap:
    """`electron/main.js` 的 `backend-boot.log` 不得无上限增长。

    ⚠️ 这里只能是**静态**判据：`main.js` 顶层 `require("electron")`，在 node 里
    无法直接加载。本仓库对该文件的既有约定就是"静态契约 + 产物级 e2e 互补"
    （见 `test_shutdown_graceful_contract.py` 的 docstring）。运行期由打包后的
    端到端测试覆盖（真实启动会写这个文件）。
    """

    @pytest.fixture(scope="class")
    def js(self) -> str:
        return MAIN_JS.read_text(encoding="utf-8")

    def test_detector_is_not_vacuous(self, js):
        assert "function bootLog(" in js
        assert "backend-boot.log" in js

    def test_cap_constant_exists_and_is_used(self, js):
        assert "BOOT_LOG_MAX_BYTES" in js
        # 定义 + 启动时检查 + 运行期检查
        assert js.count("BOOT_LOG_MAX_BYTES") == 3, \
            f"BOOT_LOG_MAX_BYTES 出现 {js.count('BOOT_LOG_MAX_BYTES')} 次，期望 3"

    def test_rotation_is_wired_into_the_writer(self, js):
        assert "function openBootLog(" in js
        body_start = js.index("function bootLog(")
        body = js[body_start:]
        assert "openBootLog(" in body, "bootLog 没有走带轮转的打开路径"
        assert "bootLogBytes" in body, "bootLog 没有在累计字节数（运行期轮转会失效）"

    def test_logging_was_not_removed_outright(self, js):
        """防空转：确认没有用"干脆不写日志"来让上面的断言通过。

        用**代码形状** `{ flags: "a" }` 判定 —— 注释里引用的 `flags: "a"` 不算数。
        """
        assert '{ flags: "a" }' in js
        assert "bootLogStream.write(" in js
