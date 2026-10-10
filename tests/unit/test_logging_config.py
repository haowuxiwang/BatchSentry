"""日志系统单元测试。

覆盖：
- RequestIdFilter 注入 request_id
- generate_request_id 格式
- setup_logging 初始化
- 第三方库噪声抑制（#145 名单完整性）
"""
import pytest
import logging

from logging_config import (
    setup_logging,
    generate_request_id,
    request_id_var,
    RequestIdFilter,
)


class TestGenerateRequestId:
    """generate_request_id。"""

    def test_returns_string(self):
        rid = generate_request_id()
        assert isinstance(rid, str)

    def test_length_is_8(self):
        """request_id 应为 8 位短 ID。"""
        rid = generate_request_id()
        assert len(rid) == 8

    def test_uniqueness(self):
        """连续调用应产生不同 ID。"""
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100  # 全部唯一


class TestRequestIdFilter:
    """RequestIdFilter。"""

    def test_filter_injects_request_id(self):
        """应注入 request_id 到 record。"""
        filter = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test message", args=(), exc_info=None,
        )
        assert filter.filter(record) is True
        assert hasattr(record, "request_id")
        assert hasattr(record, "job_id")

    def test_filter_preserves_existing_request_id(self):
        """已设置 request_id 的 record 应保留。"""
        filter = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="test", args=(), exc_info=None,
        )
        record.request_id = "custom-id"
        filter.filter(record)
        assert record.request_id == "custom-id"

    def test_filter_uses_context_var(self):
        """应从 context-var 读取 request_id。"""
        token = request_id_var.set("ctx-id-123")
        try:
            filter = RequestIdFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="test.py", lineno=1,
                msg="test", args=(), exc_info=None,
            )
            filter.filter(record)
            assert record.request_id == "ctx-id-123"
        finally:
            request_id_var.reset(token)


class TestSetupLogging:
    """setup_logging。

    注意：conftest.py 设置 PBC_NO_FILE_LOG=1 跳过文件 handler，
    这两个测试需要清除该环境变量才能验证文件 handler 创建逻辑。
    """

    @pytest.fixture(autouse=True)
    def enable_file_logging(self, monkeypatch):
        """临时清除 PBC_NO_FILE_LOG，让 setup_logging 创建文件 handler。"""
        monkeypatch.delenv("PBC_NO_FILE_LOG", raising=False)
        yield

    def test_setup_creates_handlers(self, tmp_path):
        """应创建 console + file + pipeline + error handlers。"""
        log_dir = str(tmp_path / "logs")
        setup_logging(log_dir=log_dir, level="INFO")
        root = logging.getLogger()
        assert len(root.handlers) >= 3  # console + file + error

    def test_pipeline_loggers_have_pipeline_handler(self, tmp_path):
        """pipeline 相关 logger 应有 pipeline handler。"""
        log_dir = str(tmp_path / "logs")
        setup_logging(log_dir=log_dir, level="INFO")
        pipeline_logger = logging.getLogger("core.pipeline")
        assert any("pipeline" in str(h.__class__.__name__).lower() or
                   "RotatingFileHandler" in str(h.__class__.__name__)
                   for h in pipeline_logger.handlers)

    def test_no_file_log_env_skips_file_handlers(self, tmp_path, monkeypatch):
        """PBC_NO_FILE_LOG=1 时应跳过文件 handler 创建。"""
        monkeypatch.setenv("PBC_NO_FILE_LOG", "1")
        log_dir = str(tmp_path / "logs")
        # 记录调用前的 handler 数量（root logger 是全局单例，可能有残留）
        root = logging.getLogger()
        before_file_handlers = [h for h in root.handlers
                                if isinstance(h, logging.FileHandler)]
        setup_logging(log_dir=log_dir, level="INFO")
        after_file_handlers = [h for h in root.handlers
                               if isinstance(h, logging.FileHandler)]
        # 新增的文件 handler 应为 0（PBC_NO_FILE_LOG=1 跳过文件 handler）
        assert len(after_file_handlers) == len(before_file_handlers)


class TestThirdPartyNoiseSuppression:
    """#145 逐库降噪 —— 名单必须覆盖**实测刷屏者**。

    实测（2026-10-10，logs/pharma.log）：`chardet` 是当天 613 行日志的 100%；
    `aiosqlite` 历史累计 8,116 行。两者都**不在**名单里 ⇒ 有效级别仍是 DEBUG
    （继承 root）⇒ 正是 #145 要解决的"10MB×5 很快滚完、把自家 DEBUG 挤出去"。
    """

    # 实测在 pharma.log 里刷屏的 emitter（前者 SQL、后者字符集探测）。
    REQUIRED = ("aiosqlite", "chardet")

    def test_suppression_list_covers_measured_offenders(self):
        from logging_config import _NOISY_THIRD_PARTY_LOGGERS

        missing = [n for n in self.REQUIRED if n not in _NOISY_THIRD_PARTY_LOGGERS]
        assert not missing, f"噪声抑制名单缺 {missing}（实测在 pharma.log 刷屏）"

    def test_setup_logging_actually_raises_their_level(self, tmp_path, monkeypatch):
        """行为断言：在名单里还不够，setup_logging 必须**真的**把它们抬到 ≥WARNING。"""
        from logging_config import _NOISY_THIRD_PARTY_LOGGERS

        monkeypatch.setenv("PBC_NO_FILE_LOG", "1")
        saved = {n: logging.getLogger(n).level for n in _NOISY_THIRD_PARTY_LOGGERS}
        try:
            # ⚠️ 先清到 NOTSET：别的用例调过 setup_logging（logger 是全局单例）会把
            # 级别留在 WARNING ⇒ 即使抑制逻辑被删，本断言也会绿（变异漏检）。
            for n in _NOISY_THIRD_PARTY_LOGGERS:
                logging.getLogger(n).setLevel(logging.NOTSET)
            setup_logging(log_dir=str(tmp_path / "logs"), level="INFO")
            for n in _NOISY_THIRD_PARTY_LOGGERS:
                eff = logging.getLogger(n).getEffectiveLevel()
                assert eff >= logging.WARNING, (
                    f"{n} 有效级别 {logging.getLevelName(eff)} < WARNING —— 抑制未生效"
                )
        finally:
            for n, lvl in saved.items():
                logging.getLogger(n).setLevel(lvl)

    def test_charset_prober_child_inherits_suppression(self, tmp_path, monkeypatch):
        """真正刷屏的是子 logger `chardet.charsetprober` —— 它必须继承抑制。

        父 logger 降级只在子 logger 自身为 NOTSET 时才传播到子；本测试把这个
        隐含前提变成显式断言（子 logger 若被某处 setLevel 就会当场变红）。
        """
        monkeypatch.setenv("PBC_NO_FILE_LOG", "1")
        child = logging.getLogger("chardet.charsetprober")
        parent = logging.getLogger("chardet")
        saved = (child.level, parent.level)
        try:
            child.setLevel(logging.NOTSET)
            parent.setLevel(logging.NOTSET)
            setup_logging(log_dir=str(tmp_path / "logs"), level="INFO")
            assert child.getEffectiveLevel() >= logging.WARNING, (
                "chardet.charsetprober 未被抑制 —— 当天全部日志都来自它"
            )
        finally:
            child.setLevel(saved[0])
            parent.setLevel(saved[1])
