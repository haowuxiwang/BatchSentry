"""config.py 内部函数边界测试 — 补全启动期与迁移期分支。

与 test_config.py（公开 API + 用户规则/飞书）和
test_config_db_pipeline_coverage.py（frozen 路径 + update_config 字段）
互补，本文件专注"模块导入期才跑一次"的代码：JSON 加载、.env 迁移、
持久化回写、provider 自动激活 —— 这些分支此前从未被任何测试触发。

覆盖：
- _persist_env_to_config：原子写 + config.json 损坏 + 写盘失败清理
- _env_path_legacy：dev / frozen 两态
- _migrate_env_to_json：空 .env 早退 + 写盘失败上抛并清理 tmp
- _load_json_config：非 dict 载荷、.env 迁移链路、迁移失败降级、无配置降级
- load_user_rules：user_rules 非 list
- _load_all_providers：LLM_PROVIDERS 含内建名时跳过
- _env_int：非法整数回退默认
- load_config：provider 不在注册表 → deepseek；active 无 Key → 自动激活并持久化
- update_config：ocr_slices 非法、ocr_dual_compare、llm_json_mode、
  provider 增删、mineru_base_url 同步进程镜像
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ─── _persist_env_to_config ────────────────────────────────────


class TestPersistEnvToConfig:
    """启动期自动激活 provider 的持久化回写。"""

    def test_writes_new_key_and_syncs_environ(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PBC_TEST_PERSIST_KEY", raising=False)
        cfg = tmp_path / "config.json"
        with patch("config._config_path", return_value=cfg):
            from config import _persist_env_to_config
            _persist_env_to_config("PBC_TEST_PERSIST_KEY", "val-1")
        assert json.loads(cfg.read_text(encoding="utf-8"))["PBC_TEST_PERSIST_KEY"] == "val-1"
        assert os.environ["PBC_TEST_PERSIST_KEY"] == "val-1"
        monkeypatch.delenv("PBC_TEST_PERSIST_KEY", raising=False)

    def test_merges_with_existing_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"EXISTING": "keep"}), encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            from config import _persist_env_to_config
            _persist_env_to_config("PBC_TEST_MERGE", "v")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data == {"EXISTING": "keep", "PBC_TEST_MERGE": "v"}
        monkeypatch.delenv("PBC_TEST_MERGE", raising=False)

    def test_corrupt_existing_config_is_replaced(self, tmp_path, monkeypatch):
        """已有 config.json 损坏时不应崩溃 — 回退空字典重写。"""
        cfg = tmp_path / "config.json"
        cfg.write_text("{ not json", encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            from config import _persist_env_to_config
            _persist_env_to_config("PBC_TEST_CORRUPT", "v")
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"PBC_TEST_CORRUPT": "v"}
        monkeypatch.delenv("PBC_TEST_CORRUPT", raising=False)

    def test_write_failure_keeps_env_and_cleans_tmp(self, tmp_path, monkeypatch):
        """写盘失败不应阻塞启动：内存镜像仍更新，tmp 文件被清理。"""
        cfg = tmp_path / "config.json"
        with patch("config._config_path", return_value=cfg), patch(
            "json.dump", side_effect=OSError("disk full")
        ):
            from config import _persist_env_to_config
            _persist_env_to_config("PBC_TEST_FAIL", "v")  # 不应抛异常
        assert os.environ["PBC_TEST_FAIL"] == "v"
        assert not list(tmp_path.glob("config.json.tmp.*"))
        monkeypatch.delenv("PBC_TEST_FAIL", raising=False)


# ─── _env_path_legacy ──────────────────────────────────────────


class TestEnvPathLegacy:
    def test_dev_mode_returns_project_env(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        from config import _env_path_legacy
        assert _env_path_legacy() == Path(".env")

    def test_frozen_mode_returns_appdata_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        from config import _env_path_legacy
        p = _env_path_legacy()
        assert p.name == ".env"
        assert "PBC" in str(p)


# ─── _migrate_env_to_json ──────────────────────────────────────


class TestMigrateEnvToJson:
    def test_empty_env_is_noop(self, tmp_path):
        """无任何可解析键 → 直接返回，不创建 JSON。"""
        env = tmp_path / ".env"
        env.write_text("# only a comment\n\n", encoding="utf-8")
        out = tmp_path / "config.json"
        from config import _migrate_env_to_json
        _migrate_env_to_json(env, out)
        assert not out.exists()

    def test_write_failure_raises_and_cleans_tmp(self, tmp_path):
        """迁移失败应上抛（由调用方决定降级），并清理半成品 tmp。"""
        env = tmp_path / ".env"
        env.write_text("KEY=value\n", encoding="utf-8")
        out = tmp_path / "config.json"
        from config import _migrate_env_to_json
        with patch("json.dump", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                _migrate_env_to_json(env, out)
        assert not list(tmp_path.glob("config.json.tmp.*"))


# ─── _load_json_config ─────────────────────────────────────────


class TestLoadJsonConfig:
    def test_non_dict_payload_is_ignored(self, tmp_path, monkeypatch):
        """JSON 顶层是数组/标量 → 视为空配置，不写 env。"""
        monkeypatch.delenv("PBC_NDI", raising=False)
        cfg = tmp_path / "config.json"
        cfg.write_text("[1, 2, 3]", encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            from config import _load_json_config
            _load_json_config()
        assert os.environ.get("PBC_NDI") is None

    def test_non_scalar_values_not_injected_into_environ(self, tmp_path, monkeypatch):
        """list/dict 值（如 user_rules）不进 os.environ，标量进。"""
        monkeypatch.delenv("PBC_SCALAR", raising=False)
        monkeypatch.delenv("PBC_LIST", raising=False)
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"PBC_SCALAR": 42, "PBC_LIST": ["a", "b"]}), encoding="utf-8"
        )
        with patch("config._config_path", return_value=cfg):
            from config import _load_json_config
            _load_json_config()
        assert os.environ["PBC_SCALAR"] == "42"
        assert "PBC_LIST" not in os.environ

    def test_env_migration_chain(self, tmp_path, monkeypatch):
        """JSON 缺失但 .env 存在 → 迁移并加载（覆盖 180-206）。"""
        monkeypatch.delenv("PBC_MIG", raising=False)
        cfg = tmp_path / "config.json"
        env = tmp_path / ".env"
        env.write_text("PBC_MIG=migrated\n", encoding="utf-8")
        with patch("config._config_path", return_value=cfg), patch(
            "config._env_path_legacy", return_value=env
        ):
            from config import _load_json_config
            _load_json_config()
        assert cfg.exists()
        assert os.environ["PBC_MIG"] == "migrated"
        monkeypatch.delenv("PBC_MIG", raising=False)

    def test_env_migration_failure_degrades_gracefully(self, tmp_path, monkeypatch):
        """迁移抛异常 → 告警并继续（不崩溃）。"""
        cfg = tmp_path / "config.json"
        env = tmp_path / ".env"
        env.write_text("KEY=v\n", encoding="utf-8")
        with patch("config._config_path", return_value=cfg), patch(
            "config._env_path_legacy", return_value=env
        ), patch("config._migrate_env_to_json", side_effect=OSError("nope")):
            from config import _load_json_config
            _load_json_config()  # 不应抛异常
        assert not cfg.exists()

    def test_no_config_calls_load_dotenv_in_dev(self, tmp_path, monkeypatch):
        """JSON 与 .env 都不存在 → dev 模式回退 load_dotenv（209-210）。"""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        cfg = tmp_path / "nope.json"
        with patch("config._config_path", return_value=cfg), patch(
            "config._env_path_legacy", return_value=tmp_path / "nope.env"
        ), patch("config.load_dotenv") as mock_dotenv:
            from config import _load_json_config
            _load_json_config()
        assert mock_dotenv.called


# ─── load_user_rules / provider 加载 ───────────────────────────


class TestLoadUserRulesNonList:
    def test_non_list_rules_returns_empty(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"user_rules": {"not": "a list"}}), encoding="utf-8")
        with patch("config._config_path", return_value=cfg):
            from config import load_user_rules
            assert load_user_rules() == []


class TestLoadAllProviders:
    def test_custom_list_skips_builtin_names(self, monkeypatch):
        """LLM_PROVIDERS 里重复内建名（deepseek）应被跳过，不重复加载。"""
        monkeypatch.setenv("LLM_PROVIDERS", "deepseek,customprov")
        # 其他测试可能把 DEEPSEEK_BASE_URL 置空 — 显式清掉避免顺序依赖
        monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
        from config import _load_all_providers
        providers = _load_all_providers()
        assert "deepseek" in providers
        assert "customprov" in providers
        # deepseek 仍是内建配置（未被 _load_provider 覆盖为空壳）
        assert providers["deepseek"].name == "deepseek"
        assert providers["deepseek"].base_url


# ─── _env_int ──────────────────────────────────────────────────


class TestEnvInt:
    def test_valid_value_parsed(self, monkeypatch):
        monkeypatch.setenv("PBC_TEST_INT", "7")
        from config import _env_int
        assert _env_int("PBC_TEST_INT", 3) == 7

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PBC_TEST_INT_BAD", "not-a-number")
        from config import _env_int
        assert _env_int("PBC_TEST_INT_BAD", 5) == 5

    def test_missing_key_uses_default(self, monkeypatch):
        monkeypatch.delenv("PBC_TEST_INT_MISSING", raising=False)
        from config import _env_int
        assert _env_int("PBC_TEST_INT_MISSING", 9) == 9


# ─── load_config：provider 选择与自动激活 ──────────────────────


class TestLoadConfigProviderSelection:
    def test_unknown_provider_falls_back_to_deepseek(self, monkeypatch):
        """LLM_PROVIDER 不在注册表 → 回退 deepseek（544）。"""
        monkeypatch.setenv("LLM_PROVIDER", "totally-unknown-xyz")
        from config import load_config
        cfg = load_config()
        assert cfg["app"].llm_provider == "deepseek"

    def test_auto_activates_configured_provider(self, monkeypatch):
        """active provider 无 Key 但另一 provider 有 → 自动切换并持久化（559-566）。"""
        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-real-siliconflow-value-123456")
        persisted = {}

        def fake_persist(k, v):
            persisted[k] = v

        with patch("config._persist_env_to_config", side_effect=fake_persist):
            from config import load_config
            cfg = load_config()
        assert cfg["app"].llm_provider == "siliconflow"
        assert persisted.get("LLM_PROVIDER") == "siliconflow"
        # 清理进程镜像，避免污染后续测试
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)


# ─── update_config 剩余分支 ────────────────────────────────────


class TestUpdateConfigRemainingBranches:
    def test_ocr_slices_invalid_coerced_to_one(self):
        from config import config, update_config
        orig = config["app"].ocr_slices
        update_config({"ocr_slices": "abc"})
        assert config["app"].ocr_slices == 1
        update_config({"ocr_slices": orig})

    def test_ocr_slices_floor_at_one(self):
        from config import config, update_config
        orig = config["app"].ocr_slices
        update_config({"ocr_slices": 0})
        assert config["app"].ocr_slices == 1
        update_config({"ocr_slices": orig})

    def test_ocr_dual_compare_flag(self):
        from config import config, update_config
        orig = config["app"].ocr_dual_compare
        update_config({"ocr_dual_compare": "true"})
        assert config["app"].ocr_dual_compare is True
        update_config({"ocr_dual_compare": orig})

    def test_llm_json_mode_flag(self):
        from config import config, update_config
        orig = getattr(config["app"], "llm_json_mode", None)
        update_config({"llm_json_mode": "yes"})
        assert config["app"].llm_json_mode is True
        if orig is not None:
            update_config({"llm_json_mode": orig})

    def test_add_and_remove_custom_provider(self):
        from config import config, update_config
        update_config({"llm_providers_add": "testprov"})
        assert "testprov" in config["providers"]
        # 切换到它 → 再移除 → active 应回退 deepseek
        update_config({"llm_provider": "testprov"})
        update_config({"llm_providers_remove": "testprov"})
        assert "testprov" not in config["providers"]
        assert config["app"].llm_provider == "deepseek"

    def test_remove_builtin_provider_is_protected(self):
        """内建 provider（deepseek）不允许被移除。"""
        from config import config, update_config
        update_config({"llm_providers_remove": "deepseek"})
        assert "deepseek" in config["providers"]

    def test_mineru_base_url_mirrors_to_environ(self, monkeypatch):
        from config import config, update_config
        orig = config["mineru"].base_url
        update_config({"mineru_base_url": "https://mineru.example/api"})
        assert config["mineru"].base_url == "https://mineru.example/api"
        assert os.environ["MINERU_BASE_URL"] == "https://mineru.example/api"
        update_config({"mineru_base_url": orig})
        monkeypatch.delenv("MINERU_BASE_URL", raising=False)
