"""B4-3：配置双轨（仓库 config.json vs %APPDATA%/PBC/config.json）的护栏。

要锁住三件事：
  ① **来源可判定**：开发模式/冻结版各自指向哪份文件，有标签可区分；
  ② **自查读两条路径**：只读一条会在"只按装版使用"的机器上**漏报**
     （安全护栏的漏报比假阳性更糟）；
  ③ **开发模式的凭据提示真的接在链路上**（消费点，不是"文件里出现过"）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

import config as cfg

_SRC_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_leaked_keys.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_leaked_keys", _SRC_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


cl = _load_script()

_FAKE_A = "sk-" + "a" * 32
_FAKE_B = "sk-" + "b" * 32


@pytest.fixture
def two_configs(tmp_path, monkeypatch):
    """造出**两条**各带一把 key 的 config，并把 appdata 指到临时目录。"""
    repo = tmp_path / "repo"
    appdata = tmp_path / "appdata"
    (appdata / "PBC").mkdir(parents=True)
    repo.mkdir()
    (repo / "config.json").write_text(
        json.dumps({"SILICONFLOW_API_KEY": _FAKE_A}), encoding="utf-8")
    (appdata / "PBC" / "config.json").write_text(
        json.dumps({"DEEPSEEK_API_KEY": _FAKE_B}), encoding="utf-8")
    monkeypatch.setattr(cfg, "_app_data_dir", lambda: appdata / "PBC")
    return repo, appdata / "PBC" / "config.json"


# ── ① 来源判定 ───────────────────────────────────────────────────────────────


class TestConfigSource:
    def test_dev_mode_points_at_repo_config(self, monkeypatch):
        monkeypatch.setattr(cfg, "_is_frozen", lambda: False)
        label, path = cfg.config_source()
        assert label == cfg.CONFIG_SOURCE_REPO
        assert path.name == "config.json" and path.parent == Path(".")

    def test_frozen_mode_points_at_appdata(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "_is_frozen", lambda: True)
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: tmp_path / "PBC")
        label, path = cfg.config_source()
        assert label == cfg.CONFIG_SOURCE_APPDATA
        assert path == tmp_path / "PBC" / "config.json"

    def test_config_path_delegates_to_config_source(self, monkeypatch):
        """单一实现点：`_config_path()` 必须**真的调用** `config_source()`。

        ⚠️ 不能用"两者的返回值相等"来断言 —— 一份**独立实现**的
        `_config_path()`（各自写一遍 frozen 分支）返回值照样相等，
        那条断言就变成空断言。这里把 `config_source` **换成哨兵**，
        只有真的调用了它才会返回哨兵值。
        """
        sentinel = Path("__from_config_source__") / "config.json"
        monkeypatch.setattr(cfg, "config_source", lambda: (cfg.CONFIG_SOURCE_REPO, sentinel))
        assert cfg._config_path() == sentinel

    def test_search_paths_covers_both_labels(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: tmp_path / "PBC")
        labels = [label for label, _ in cfg.config_search_paths(tmp_path / "repo")]
        assert labels == [cfg.CONFIG_SOURCE_REPO, cfg.CONFIG_SOURCE_APPDATA]


# ── ② 自查必须读两条路径（漏报修复） ──────────────────────────────────────────


class TestSelfCheckReadsBothPaths:
    def test_both_configs_are_read(self, two_configs):
        """spec 的验收：造两个临时 config，断言**都被读到**。"""
        repo, _ = two_configs
        keys = cl.load_current_keys(repo)
        assert set(keys) == {cfg.CONFIG_SOURCE_REPO, cfg.CONFIG_SOURCE_APPDATA}
        assert keys[cfg.CONFIG_SOURCE_REPO]["SILICONFLOW_API_KEY"] == _FAKE_A
        assert keys[cfg.CONFIG_SOURCE_APPDATA]["DEEPSEEK_API_KEY"] == _FAKE_B

    def test_appdata_only_machine_still_finds_the_live_key(self, tmp_path, monkeypatch):
        """**漏报回归**：仓库那份不存在（只按装版使用的机器）⇒ 仍须报出生效值。

        旧实现只读仓库 `config.json` ⇒ 这种机器上返回空 ⇒ 把**生效值**判成
        "不存在" ⇒ 历史泄漏值与生效值有交集也报"无交集"（漏报）。
        """
        repo = tmp_path / "repo"
        appdata = tmp_path / "appdata" / "PBC"
        appdata.mkdir(parents=True)
        repo.mkdir()
        (appdata / "config.json").write_text(
            json.dumps({"DEEPSEEK_API_KEY": _FAKE_B}), encoding="utf-8")
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: appdata)

        keys = cl.load_current_keys(repo)
        assert keys.get(cfg.CONFIG_SOURCE_REPO, {}) == {}
        assert keys[cfg.CONFIG_SOURCE_APPDATA]["DEEPSEEK_API_KEY"] == _FAKE_B
        # 两条路径**始终登记**（缺失为空 dict）—— 报告里要看得见"查过了、是空的"
        assert set(keys) == {cfg.CONFIG_SOURCE_REPO, cfg.CONFIG_SOURCE_APPDATA}
        assert keys[cfg.CONFIG_SOURCE_REPO] == {}

    def test_missing_files_yield_empty_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: tmp_path / "nope")
        keys = cl.load_current_keys(tmp_path / "no-repo")
        # 不崩、且两条路径都在（值都为空）
        assert set(keys) == {cfg.CONFIG_SOURCE_REPO, cfg.CONFIG_SOURCE_APPDATA}
        assert all(v == {} for v in keys.values())

    def test_unparseable_config_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        """坏 JSON 不得让整个安全自查崩掉（宁可少报一条，也不能没有结论）。"""
        repo = tmp_path / "repo"
        appdata = tmp_path / "appdata" / "PBC"
        appdata.mkdir(parents=True)
        repo.mkdir()
        (repo / "config.json").write_text("{not json", encoding="utf-8")
        (appdata / "config.json").write_text(
            json.dumps({"DEEPSEEK_API_KEY": _FAKE_B}), encoding="utf-8")
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: appdata)

        keys = cl.load_current_keys(repo)
        assert keys[cfg.CONFIG_SOURCE_REPO] == {}            # 坏的被跳过（登记为空）
        assert keys[cfg.CONFIG_SOURCE_APPDATA]               # 好的仍然读到

    def test_script_uses_the_single_source_of_truth(self):
        """路径表由 `config.config_search_paths` 提供 —— 不再自己拼字符串。

        判据落在**消费点**：`load_current_keys` 的函数体里必须出现对
        `config_search_paths` 的调用，且**不得**再出现裸的 ``/ "config.json"``
        拼接（那正是漂移的来源）。
        """
        tree = ast.parse(_SRC_PATH.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "load_current_keys")
        calls = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name)
                 and c.func.id == "config_search_paths"]
        assert len(calls) == 1, "load_current_keys 应恰好调用一次 config_search_paths"
        assert not [c for c in ast.walk(fn)
                    if isinstance(c, ast.Constant)
                    and c.value == "config.json"], "不得再自行拼接 config.json 路径"


# ── ③ 开发模式凭据提示：接在链路上（消费点） ─────────────────────────────────


class TestDevCredentialHint:
    def test_hint_present_in_dev_mode(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg, "_is_frozen", lambda: False)
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: tmp_path / "PBC")
        hint = cfg.dev_config_credential_hint()
        assert hint and "仓库" in hint
        # 必须点名"另一份"在哪 —— 否则用户不知道正确的 key 在哪儿
        assert "config.json" in hint

    def test_no_hint_when_frozen(self, monkeypatch, tmp_path):
        """冻结版用的就是生效路径 ⇒ 不该出现"你用的是仓库那份"的误导提示。"""
        monkeypatch.setattr(cfg, "_is_frozen", lambda: True)
        monkeypatch.setattr(cfg, "_app_data_dir", lambda: tmp_path / "PBC")
        assert cfg.dev_config_credential_hint() == ""

    def test_hint_is_appended_to_the_config_error(self):
        """提示必须**接在** `LLMConfigError` 的构造里（消费点，非"文件里提到过"）。"""
        client_src = (Path(__file__).resolve().parents[2] / "llm" / "client.py").read_text(
            encoding="utf-8")
        tree = ast.parse(client_src)
        raised = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "LLMConfigError"]
        assert raised, "未找到 LLMConfigError 的抛出点"
        # 抛出点所在的 f-string 拼接里必须出现 dev_config_credential_hint() 调用
        assert any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "dev_config_credential_hint"
            for r in raised for c in ast.walk(r)
        ), "LLMConfigError 的构造里没有 dev_config_credential_hint() —— 提示没接上"
