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
    @pytest.fixture(autouse=True)
    def _restore_notice(self, monkeypatch):
        """`load_config()` 会改写模块级 `AUTO_ACTIVATE_NOTICE`（供 settings API 读）。

        它是**全局单源** ⇒ 测试必须还原，否则会把"某个测试想定的启动事实"
        泄漏给后续用例 / 集成测试的 GET /api/settings 断言。

        同时清掉 `LLM_PROVIDERS`：别的测试会注册自定义 provider（如
        `test_api_settings.py` 的 `anthropictest`，且其 key 非空），
        不清就会**跨测试污染**这里的候选集（实测：本组用例单独跑绿、
        合并跑红）。
        """
        import config as _c
        saved = _c.AUTO_ACTIVATE_NOTICE
        monkeypatch.delenv("LLM_PROVIDERS", raising=False)
        yield
        _c.AUTO_ACTIVATE_NOTICE = saved

    def test_unknown_provider_falls_back_to_deepseek(self, monkeypatch):
        """LLM_PROVIDER 不在注册表 → 回退 deepseek（544）。"""
        monkeypatch.setenv("LLM_PROVIDER", "totally-unknown-xyz")
        from config import load_config
        cfg = load_config()
        assert cfg["app"].llm_provider == "deepseek"

    def test_auto_activates_when_not_explicitly_chosen(self, monkeypatch):
        """**未显式选择** + active 无 Key + 另一家有 Key ⇒ 自动切换 + 持久化 + 提示。

        🔴 B4-4：触发条件里**不再**包含 `_is_real_key`（占位启发式），
        只看 **Key 字面为空**。故本用例把 `LLM_PROVIDER` **删掉**
        （= 用户从未选过，用的是默认值）——
        旧版正是靠 setenv("LLM_PROVIDER","deepseek") 来"模拟默认"，
        在新规则下那已经是**显式选择**了（见下一个用例）。
        """
        import config as _c
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-real-siliconflow-value-123456")
        persisted = {}

        with patch("config._persist_env_to_config", side_effect=lambda k, v: persisted.__setitem__(k, v)):
            cfg = _c.load_config()

        assert cfg["app"].llm_provider == "siliconflow"
        assert persisted.get("LLM_PROVIDER") == "siliconflow"
        # 界面可见性：决策事实必须被记录（否则就是"静默换 provider"）
        assert _c.AUTO_ACTIVATE_NOTICE == {
            "applied": True, "from": "deepseek", "to": "siliconflow",
            "reason": "deepseek 未配置 API Key，已自动切换到 siliconflow。",
        }
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    def test_explicit_choice_not_overridden(self, monkeypatch):
        """**验收 ①**：用户**显式选过**（`LLM_PROVIDER` 已设置）⇒ 绝不改写。

        即便 active 无 Key、另一家有 Key，也只**如实上报**（applied=False），
        由界面提示用户去填 Key 或手动切换 —— 不得覆盖用户的显式选择。
        """
        import config as _c
        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-real-siliconflow-value-123456")

        def _must_not_persist(k, v):   # pragma: no cover - 触发即失败
            raise AssertionError(f"不得改写用户显式选择，却持久化了 {k}={v!r}")

        with patch("config._persist_env_to_config", side_effect=_must_not_persist):
            cfg = _c.load_config()

        assert cfg["app"].llm_provider == "deepseek", "显式选择必须被尊重"
        assert _c.AUTO_ACTIVATE_NOTICE is not None
        assert _c.AUTO_ACTIVATE_NOTICE["applied"] is False
        assert _c.AUTO_ACTIVATE_NOTICE["from"] == "deepseek"
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    def test_placeholder_substring_key_does_not_trigger_switch(self, monkeypatch):
        """**验收 ②**：active 的 Key 含 `placeholder` 子串但**格式合法**
        ⇒ **不得**触发切换。

        这正是旧版的生产事故：`_is_real_key` 用子串匹配 ⇒ 把真实 key 判成
        "未配置" ⇒ 启动时静默换到别的 provider（**你以为的模型 ≠ 实际跑的模型**）。
        """
        import config as _c
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abc-placeholder-xyz-1234567890")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-real-siliconflow-value-123456")

        def _must_not_persist(k, v):   # pragma: no cover - 触发即失败
            raise AssertionError(f"含 placeholder 子串的真实 key 不得触发切换：{k}={v!r}")

        with patch("config._persist_env_to_config", side_effect=_must_not_persist):
            cfg = _c.load_config()

        assert cfg["app"].llm_provider == "deepseek"
        assert _c.AUTO_ACTIVATE_NOTICE is None, "没有发生任何需要提示的事"
        monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    def test_no_key_anywhere_leaves_choice_alone(self, monkeypatch):
        """**一个 Key 都没配** ⇒ 无处可回退，保持现状 + 不提示。"""
        import config as _c
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("SILICONFLOW_API_KEY", "")
        with patch("config._persist_env_to_config", side_effect=AssertionError("不应持久化")):
            cfg = _c.load_config()
        assert cfg["app"].llm_provider == "deepseek"
        assert _c.AUTO_ACTIVATE_NOTICE is None


class TestIsRealKeyExactMatch:
    """🔴 B4-4 ③：`_is_real_key` 必须是**精确格式校验**，不得子串匹配。"""

    def test_real_key_containing_placeholder_substring_is_real(self):
        """真实 key 恰好含占位词的**子串** ⇒ 仍必须是"真"（旧实现的致命误判）。"""
        from config import _is_real_key
        for key in (
            "sk-abc-placeholder-xyz-1234567890",
            "sk-xxxxxlivekey0123456789",
            "sk-ant-testing-real-key-abcdef",
            "sk-changeme-but-actually-real-123",
        ):
            assert _is_real_key(key) is True, key

    def test_exact_placeholder_values_rejected(self):
        from config import _is_real_key
        for key in ("placeholder", "PLACEHOLDER", "changeme", "xxxxx", "your-api-key-here"):
            assert _is_real_key(key) is False, key

    def test_template_prefixes_rejected(self):
        from config import _is_real_key
        for key in ("sk-test", "sk-test-1234", "sk-glm-test",
                    "sk-example-123", "test-key", "sk-placeholder-abc"):
            assert _is_real_key(key) is False, key
        # 词模板走精确值（后接字母，无法用前缀边界区分）
        for key in ("sk-your-api-key", "your-api-key-here", "your_api_key"):
            assert _is_real_key(key) is False, key

    def test_prefix_must_end_at_non_letter(self):
        """前缀边界：`sk-test` 是占位，但 `sk-testing-...` 是**真实 key**。

        朴素 `startswith("sk-ant-test")` 会把 `sk-ant-testing-real-key-abcdef`
        判成占位 ⇒ 又是一次"真实 key 被误判"（与 B4-4 要修的是同一个错）。
        本用例由护栏当场抓出过该写法，钉住边界语义。
        """
        from config import _is_real_key
        assert _is_real_key("sk-testing-real-key-abcdef") is True
        assert _is_real_key("sk-ant-testing-real-key-abcdef") is True
        assert _is_real_key("test-keying-something-real") is True
        assert _is_real_key("sk-test-1234") is False
        assert _is_real_key("sk-ant-test") is False

    def test_empty_and_whitespace_rejected(self):
        from config import _is_real_key
        assert _is_real_key("") is False
        assert _is_real_key("   ") is False
        assert _is_real_key(" sk-real-key-123456 ") is False, "首尾空白一定是粘贴事故"

    def test_single_implementation_point(self):
        """`api/settings/read.py` 不得再手抄一份（B3-4 同族：单一实现点）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "api" / "settings" / "read.py").read_text(
            encoding="utf-8")
        assert "_is_real_key" in src
        assert "def _is_real_api_key" not in src, "不得保留手抄副本"


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
