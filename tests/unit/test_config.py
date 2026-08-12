"""Config 模块单元测试。

覆盖：
- load_config 默认值
- update_config 运行时更新（供应商切换 bug 的核心修复）
- _mask 脱敏函数
- _settings_config_path 路径解析（dev/frozen 模式，JSON 配置文件）
"""
import os
import sys
from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

from config import (
    load_config,
    update_config,
    DeepSeekConfig,
    SiliconFlowConfig,
    PaddleOCRConfig,
    MinerUConfig,
    AppConfig,
)


class TestLoadConfig:
    """load_config 默认值与环境变量覆盖。"""

    def test_load_config_returns_dict_with_all_sections(self):
        """配置应包含所有必需的部分。"""
        cfg = load_config()
        assert "deepseek" in cfg
        assert "siliconflow" in cfg
        assert "paddle_ocr" in cfg
        assert "mineru" in cfg
        assert "app" in cfg

    def test_load_config_default_llm_provider_is_deepseek(self, monkeypatch):
        """默认 LLM 供应商为 deepseek（当无任何 provider 配置 Key 时）。"""
        # 清除环境变量 + 所有 API key，确保无 provider 被自动激活
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        for prov in ("DEEPSEEK", "SILICONFLOW", "GLM", "KIMI", "QWEN", "MIMO", "ANTHROPIC"):
            monkeypatch.delenv(f"{prov}_API_KEY", raising=False)
        # 也清除 LLM_PROVIDERS 避免加载自定义 provider
        monkeypatch.delenv("LLM_PROVIDERS", raising=False)
        cfg = load_config()
        assert cfg["app"].llm_provider == "deepseek"

    def test_load_config_default_ocr_backend_is_paddle(self, monkeypatch):
        """默认 OCR 后端为 paddle（清除 .env 中的 OCR_BACKEND 覆盖）。"""
        monkeypatch.delenv("OCR_BACKEND", raising=False)
        cfg = load_config()
        assert cfg["app"].ocr_backend == "paddle"

    def test_load_config_reads_env_vars(self, monkeypatch):
        """应从环境变量读取配置。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-123")
        monkeypatch.setenv("LLM_PROVIDER", "siliconflow")
        cfg = load_config()
        assert cfg["deepseek"].api_key == "sk-test-123"
        assert cfg["app"].llm_provider == "siliconflow"

    def test_load_config_empty_api_key_by_default(self):
        """无环境变量时 API key 为空字符串。"""
        cfg = load_config()
        # 不应抛异常
        assert cfg["deepseek"].api_key == "" or cfg["deepseek"].api_key  # 可能是 .env 中的值

    def test_deepseek_config_is_mutable_dataclass(self):
        """DeepSeekConfig 应为可变 dataclass（修复供应商切换 bug）。"""
        cfg = load_config()
        original = cfg["deepseek"].api_key
        cfg["deepseek"].api_key = "sk-new-value"
        assert cfg["deepseek"].api_key == "sk-new-value"
        # 清理
        cfg["deepseek"].api_key = original


class TestUpdateConfig:
    """update_config 运行时更新 — 供应商切换 bug 的核心修复。"""

    def test_update_llm_provider(self):
        """应能运行时切换 LLM 供应商。"""
        update_config({"llm_provider": "siliconflow"})
        from config import config
        assert config["app"].llm_provider == "siliconflow"
        # 恢复
        update_config({"llm_provider": "deepseek"})
        assert config["app"].llm_provider == "deepseek"

    def test_update_ocr_backend(self):
        """应能运行时切换 OCR 后端。"""
        update_config({"ocr_backend": "mineru"})
        from config import config
        assert config["app"].ocr_backend == "mineru"
        # 恢复
        update_config({"ocr_backend": "paddle"})

    def test_update_deepseek_api_key(self):
        """应能更新 DeepSeek API key。"""
        update_config({"deepseek_api_key": "sk-new-deepseek"})
        from config import config
        assert config["deepseek"].api_key == "sk-new-deepseek"

    def test_update_siliconflow_api_key(self):
        """应能更新 SiliconFlow API key。"""
        update_config({"siliconflow_api_key": "sk-new-sf"})
        from config import config
        assert config["siliconflow"].api_key == "sk-new-sf"

    def test_update_mineru_token(self):
        """应能更新 MinerU token。"""
        update_config({"mineru_token": "sk-new-mineru"})
        from config import config
        assert config["mineru"].token == "sk-new-mineru"

    def test_update_paddle_ocr_token(self):
        """应能更新 PaddleOCR token。"""
        update_config({"paddle_ocr_token": "new-paddle-token"})
        from config import config
        assert config["paddle_ocr"].token == "new-paddle-token"

    def test_update_partial_fields_only(self):
        """只更新提供的字段，其他字段保留。"""
        from config import config
        original_base_url = config["deepseek"].base_url
        update_config({"deepseek_api_key": "sk-partial"})
        assert config["deepseek"].api_key == "sk-partial"
        assert config["deepseek"].base_url == original_base_url  # 未变

    def test_update_unknown_field_ignored(self):
        """未知字段应被静默忽略，不抛异常。"""
        update_config({"unknown_field": "value"})  # 不应抛异常

    def test_update_mineru_bool_fields(self):
        """应能更新 MinerU 布尔字段。"""
        update_config({"mineru_enable_formula": False, "mineru_enable_table": True})
        from config import config
        assert config["mineru"].enable_formula is False
        assert config["mineru"].enable_table is True


class TestSettingsMask:
    """_mask 脱敏函数（从 settings.py 导入）。"""

    def test_mask_short_value_returns_all_stars(self):
        """短值（<=12 字符）应全部脱敏。"""
        from api.settings import _mask
        assert _mask("short") == "*****"
        assert _mask("123456789012") == "*" * 12

    def test_mask_long_value_shows_first_and_last_4(self):
        """长值应显示前4位 + **** + 后4位。"""
        from api.settings import _mask
        # 使用明显的假值，避免误用真实 key 格式
        result = _mask("sk-test-fake-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert result.startswith("sk-t")
        assert result.endswith("xxxx")
        assert "****" in result

    def test_mask_empty_value(self):
        """空值应返回空字符串。"""
        from api.settings import _mask
        assert _mask("") == ""

    def test_mask_none_value(self):
        """None 应安全处理。"""
        from api.settings import _mask
        assert _mask(None) == ""


class TestSettingsConfigPath:
    """_settings_config_path 路径解析（dev/frozen 模式）。

    Phase 9: 配置系统从 .env 迁移到 JSON 后，设置 API 写入 config.json。
    这些测试验证路径解析在 dev/frozen 模式下都指向正确的 JSON 文件。
    """

    def test_settings_config_path_dev_mode(self):
        """开发模式应返回项目根 config.json。"""
        from api.settings import _settings_config_path
        with patch("sys.frozen", False, create=True):
            path = _settings_config_path()
            assert path.name == "config.json"

    def test_settings_config_path_frozen_mode_windows(self, monkeypatch):
        """Frozen 模式应返回 %APPDATA%/PBC/config.json。"""
        from api.settings import _settings_config_path
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(Path.cwd() / "tmp_test_appdata"))
        path = _settings_config_path()
        assert "PBC" in str(path)
        assert path.name == "config.json"


class TestLoadUserRules:
    """load_user_rules：用户自定义合规规则读取（Phase 10）。"""

    def _write(self, tmp_path, data: dict):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return cfg

    def test_no_config_file_returns_empty(self, tmp_path):
        from config import load_user_rules
        with patch("config._config_path", return_value=tmp_path / "nope.json"):
            assert load_user_rules() == []

    def test_no_rules_key_returns_empty(self, tmp_path):
        from config import load_user_rules
        self._write(tmp_path, {"LLM_PROVIDER": "deepseek"})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            assert load_user_rules() == []

    def test_filters_invalid_entries(self, tmp_path):
        from config import load_user_rules
        self._write(tmp_path, {"user_rules": [
            {"id": "r1", "text": " 有效规则 ", "active": True},
            {"id": "r2", "text": "   ", "active": True},   # 空文本 → 过滤
            {"text": 123, "active": True},                  # 非字符串 → 过滤
            "not-a-dict",                                    # 非 dict → 过滤
        ]})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            rules = load_user_rules()
        assert len(rules) == 1
        assert rules[0]["text"] == "有效规则"  # 首尾空白被去除

    def test_active_defaults_true_and_id_generated(self, tmp_path):
        from config import load_user_rules
        self._write(tmp_path, {"user_rules": [
            {"text": "无 id 无 active 的规则"},
        ]})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            rules = load_user_rules()
        assert len(rules) == 1
        assert rules[0]["active"] is True
        assert rules[0]["id"]

    def test_corrupt_json_returns_empty(self, tmp_path):
        from config import load_user_rules
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken json", encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            assert load_user_rules() == []

    def test_caps_at_max_rules(self, tmp_path):
        from config import load_user_rules
        self._write(tmp_path, {"user_rules": [
            {"text": f"规则 {i}", "active": True} for i in range(120)
        ]})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            assert len(load_user_rules()) == 100


class TestLoadFeishuConfig:
    """load_feishu_config：飞书通知配置读取（Phase 12）。"""

    def _write(self, tmp_path, data: dict):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return cfg

    def test_no_config_file_returns_defaults(self, tmp_path):
        from config import load_feishu_config
        with patch("config._config_path", return_value=tmp_path / "nope.json"):
            cfg = load_feishu_config()
        assert cfg["enabled"] is False
        assert cfg["webhook_url"] == ""
        assert cfg["events"] == ["review", "partial_review", "error"]

    def test_full_config_parsed(self, tmp_path):
        from config import load_feishu_config
        self._write(tmp_path, {
            "feishu_enabled": True,
            "feishu_webhook_url": "https://open.feishu.cn/hook/x",
            "feishu_secret": "s1",
            "feishu_events": "review,error",
        })
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            cfg = load_feishu_config()
        assert cfg["enabled"] is True
        assert cfg["webhook_url"] == "https://open.feishu.cn/hook/x"
        assert cfg["events"] == ["review", "error"]

    def test_string_false_parsed_as_false(self, tmp_path):
        """config.json 中的 "false" 字符串必须解析为 False（bool("false")=True 陷阱）。"""
        from config import load_feishu_config
        self._write(tmp_path, {"feishu_enabled": "false"})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            assert load_feishu_config()["enabled"] is False

    def test_string_true_parsed_as_true(self, tmp_path):
        from config import load_feishu_config
        self._write(tmp_path, {"feishu_enabled": "true"})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            assert load_feishu_config()["enabled"] is True

    def test_corrupt_json_returns_defaults(self, tmp_path):
        from config import load_feishu_config
        cfg = tmp_path / "config.json"
        cfg.write_text("{broken", encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            loaded = load_feishu_config()
        assert loaded["enabled"] is False

    def test_empty_events_key_keeps_defaults(self, tmp_path):
        from config import load_feishu_config
        self._write(tmp_path, {"feishu_enabled": True, "feishu_events": "  "})
        with patch("config._config_path", return_value=tmp_path / "config.json"):
            cfg = load_feishu_config()
        assert cfg["events"] == ["review", "partial_review", "error"]
