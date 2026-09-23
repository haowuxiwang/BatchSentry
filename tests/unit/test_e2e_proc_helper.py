"""tests/e2e_proc.py 助手的行为测试 + "未排空 PIPE" 回归护栏。

背景（2026-09-14 M8 实测）：多个 e2e harness 用
``subprocess.Popen(..., stdout=subprocess.PIPE)`` 启动长驻服务却从不排空管道，
服务端日志写满 OS 管道缓冲(~64KB)后子进程阻塞在 write → asyncio 事件循环停摆
→ 后续请求全部超时。症状是"卡点漂移"，极易误判为产品缺陷
（``tests/e2e_frozen.py`` 一度 6/16 通过，修复后 16/16）。

本文件：
1. 行为验证 helper（落盘 / 读尾 / 幂等停止）；
2. **源码扫描护栏**：tests/ 下不得再用未排空的 PIPE 启动服务，必须走
   ``tests.e2e_proc.spawn_server``；
3. **源码扫描护栏**：tests/ 下不得写死本仓库 / 家目录的绝对路径（换台机器即失效，
   改用 ``tests.e2e_proc.REPO_ROOT`` 派生）。
"""
import re
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from tests.e2e_proc import spawn_server, stop_server, tail_log  # noqa: E402

_PIPE_RE = re.compile(r"stdout\s*=\s*subprocess\.PIPE")

# 密钥扫描护栏已收拢到 tests/unit/test_no_committed_secrets.py：
#   - 范围更广（含**仓库根目录脚本** —— 本事故正发生在根目录脚本上）；
#   - 与 DEPLOYMENT.md 的自查命令同源（有测试做绑定，防两边各自漂移）；
#   - 带阳性对照 + "config.json 被 gitignore"的前提检查。
# 本文件只保留"绝对路径"与"未排空 PIPE"两条源码扫描护栏。


_ABS_PATH_RE = re.compile(r"""["'](?:[A-Za-z]:[\\/]|/Users/|/home/)""")

# 本护栏文件豁免自身的字面量扫描：它必须写出这些模式才能描述规则。
# 为免豁免把护栏变成空转，另设 `test_path_guard_positive_control` 做阳性对照。
_PATH_GUARD_SKIP = {"tests/unit/test_e2e_proc_helper.py"}

# 家目录形态：Windows 用户目录 / macOS / Linux
_HOME_PATH_RE = re.compile("[A-Za-z]:" + r"[\\\\/]" + "Users" + r"[\\\\/]" + "|"
                           + "/" + "Users" + "/" + "|" + "/" + "home" + "/")


def _repo_path_literals(text: str):
    """返回 ``text`` 中本仓库绝对路径 / 家目录路径的出现位置（用于扫描与自测）。"""
    low = text.replace("\\", "/").lower()
    roots = (str(_ROOT).replace("\\", "/").lower(), str(_ROOT).lower())
    return [v for v in roots if v in low], _HOME_PATH_RE.search(text)


def test_no_hardcoded_paths_pointing_at_this_repo():
    """tests/ 下不得写死**本仓库自身**的绝对路径或用户家目录路径。

    护栏口径（刻意收窄，避免假阳性）：只禁两类真正会造成"换台机器即失效 /
    绑定个人目录"的字面量 ——
      1. 以本仓库根目录开头的绝对路径；
      2. 用户家目录形态（Windows 用户目录 / macOS / Linux 各一种）。

    **不禁**合成 OS 路径：比如指向系统目录的攻击载荷、以及伪造解释器内置路径
    的字符串 —— 它们是测试**数据**而非宿主绑定，属合法用法。
    """
    offenders = []
    for p in sorted((_ROOT / "tests").rglob("*.py")):
        rel = p.relative_to(_ROOT).as_posix()
        if "node_modules" in rel or rel in _PATH_GUARD_SKIP:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            roots, home = _repo_path_literals(line)
            if roots or home:
                offenders.append(f"{rel}:{i}")
    assert not offenders, (
        "以下测试写死了本仓库/家目录的绝对路径（应改用 tests.e2e_proc.REPO_ROOT 派生）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def test_path_guard_positive_control():
    """阳性对照：证明上面的护栏**真的会报**，而不是因豁免而空转。"""
    fake = "exe = r'" + str(_ROOT) + r"\dist\pbc-server\pbc-server.exe'"
    roots, _ = _repo_path_literals(fake)
    assert roots, "护栏未能识别本仓库绝对路径（会给出虚假的'干净'结论）"

    fake_home = "p = " + '"' + "C" + ":" + "\\" + "Users" + "\\" + "someone" + "\\" + "x" + '"'
    _, home = _repo_path_literals(fake_home)
    assert home, "护栏未能识别用户家目录路径"

    # 反例：合成 OS 路径（合法测试数据）不得被误报
    for ok_line in ('assert r.status_code == 403  # C:/Windows/win.ini',
                    'Path("C:/fake/meipass")'):
        roots, home = _repo_path_literals(ok_line)
        assert not roots and not home, f"误报合法测试数据: {ok_line}"


def test_no_drive_letter_literal_in_e2e_proc():
    """统一助手模块里不得出现绝对路径字面量（默认产物路径必须由 REPO_ROOT 派生）。"""
    src = (_ROOT / "tests" / "e2e_proc.py").read_text(encoding="utf-8", errors="replace")
    assert not _ABS_PATH_RE.search(src), "tests/e2e_proc.py 不应写死绝对路径"


# ── 产物路径解析 / 密钥注入 ─────────────────────────────────────────


def test_resolve_exe_prefers_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "pbc-server.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("PBC_E2E_EXE", str(fake))
    from tests.e2e_proc import resolve_exe
    assert resolve_exe() == str(fake)


def test_resolve_exe_missing_target_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PBC_E2E_EXE", str(tmp_path / "nope.exe"))
    from tests.e2e_proc import resolve_exe
    with pytest.raises(FileNotFoundError):
        resolve_exe()


def test_resolve_exe_default_is_repo_relative(monkeypatch):
    """默认产物路径必须由仓库根派生（而非写死盘符）。

    注意：运行时 ``str(DEFAULT_EXE)`` 当然带盘符 —— 那是"在这台机器上解析出来
    的绝对路径"，正确且必要。要禁的是**源码里的字面量**，故断言改为结构性 +
    源码扫描（见 ``test_no_drive_letter_literal_in_e2e_proc``）。
    """
    monkeypatch.delenv("PBC_E2E_EXE", raising=False)
    from tests.e2e_proc import DEFAULT_EXE, REPO_ROOT
    assert DEFAULT_EXE == REPO_ROOT / "dist" / "pbc-server" / "pbc-server.exe"
    assert REPO_ROOT.is_dir() and (REPO_ROOT / "main.py").is_file()


def _clear_llm_keys(monkeypatch):
    """清掉新/旧两个变量名 —— 漏清一个会让"未设置"用例在开发机上假绿。"""
    for name in ("PBC_E2E_LLM_KEY", "PBC_E2E_DEEPSEEK_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_llm_key_absent_yields_empty(monkeypatch):
    _clear_llm_keys(monkeypatch)
    from tests.e2e_proc import llm_key
    assert llm_key() == ""


def test_llm_key_from_env(monkeypatch):
    _clear_llm_keys(monkeypatch)
    monkeypatch.setenv("PBC_E2E_LLM_KEY", "sk-from-env-only")
    from tests.e2e_proc import llm_key
    assert llm_key() == "sk-from-env-only"


def test_llm_key_legacy_name_still_works(monkeypatch, capsys):
    """旧名仍可读（防已写好的发版命令静默失效），但必须**提示**已弃用。"""
    _clear_llm_keys(monkeypatch)
    monkeypatch.setenv("PBC_E2E_DEEPSEEK_KEY", "sk-legacy")
    from tests.e2e_proc import llm_key
    assert llm_key() == "sk-legacy"
    assert "已弃用" in capsys.readouterr().err, "读到旧名却未提示弃用（迁移无从推进）"


def test_llm_key_new_name_wins_over_legacy(monkeypatch):
    """两名同设时新名优先 —— 否则"改名未生效"会被旧值悄悄掩盖。"""
    monkeypatch.setenv("PBC_E2E_DEEPSEEK_KEY", "sk-legacy")
    monkeypatch.setenv("PBC_E2E_LLM_KEY", "sk-new")
    from tests.e2e_proc import llm_key
    assert llm_key() == "sk-new"


def test_llm_key_absent_falls_back_to_default_without_warning(monkeypatch, capsys):
    """两个都没设时返回 default，且**不得**报弃用（无旧名可弃）。"""
    _clear_llm_keys(monkeypatch)
    from tests.e2e_proc import llm_key
    assert llm_key(default="sk-default") == "sk-default"
    assert "已弃用" not in capsys.readouterr().err


def test_llm_key_env_names_are_not_provider_bound():
    """命名债（B9-7）：变量名不得再绑定某一提供方。

    语义是 provider-agnostic 的（配 ``PBC_E2E_LLM_PROVIDER`` 使用）；旧名
    叫 DEEPSEEK 却要装硅基流动的 key，等于把"名字骗人"固化进发布流程。
    """
    from tests.e2e_proc import LLM_KEY_ENV
    assert LLM_KEY_ENV == "PBC_E2E_LLM_KEY"
    for provider in ("DEEPSEEK", "SILICONFLOW", "GLM", "OPENAI"):
        assert provider not in LLM_KEY_ENV, f"变量名仍绑定 {provider}"


# 允许保留该字面量的文件（含说明性文字或有意演示反例），均需给出理由
_PIPE_ALLOW = {
    # 助手模块自身：docstring 中引用该反例以解释"为什么不能这么写"
    "tests/e2e_proc.py",
    # 本测试文件：正则字面量中即包含该模式（扫描时会命中自身）
    "tests/unit/test_e2e_proc_helper.py",
}


def _python_files():
    for p in sorted((_ROOT / "tests").rglob("*.py")):
        rel = p.relative_to(_ROOT).as_posix()
        if rel in _PIPE_ALLOW:
            continue
        yield rel, p


def test_no_unread_pipe_in_test_harnesses():
    """tests/ 下不得以 stdout=subprocess.PIPE 启动进程（会因管道写满而卡死）。"""
    offenders = []
    for rel, p in _python_files():
        if "node_modules" in rel:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if _PIPE_RE.search(src):
            offenders.append(rel)
    assert not offenders, (
        "以下测试脚本用未排空的 PIPE 启动进程，日志写满管道缓冲后会阻塞子进程：\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n改用 tests.e2e_proc.spawn_server(...)（stdout 落日志文件）。"
    )


def test_server_spawning_harnesses_use_helper():
    """已确认会启动长驻服务的 harness 必须复用统一助手（防多路径漂移）。"""
    expected = [
        "tests/e2e_frozen.py",
        "tests/e2e_quick.py",
        "tests/e2e_manual.py",
        "tests/e2e_m6_sse.py",
        "tests/e2e_m7_docling_absent.py",
        "tests/integration/test_frozen_smoke.py",
    ]
    missing = []
    for rel in expected:
        p = _ROOT / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if "tests.e2e_proc import" not in src:
            missing.append(rel)
    assert not missing, (
        "以下 harness 未复用 tests.e2e_proc 的统一启动助手：\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


# ── 助手行为 ────────────────────────────────────────────────────────


def test_spawn_server_writes_log_and_stop_is_idempotent(tmp_path):
    log = tmp_path / "srv.log"
    proc, logf = spawn_server(
        [sys.executable, "-c",
         "import sys,time; print('hello-from-server'); sys.stdout.flush(); time.sleep(30)"],
        log_path=log,
    )
    try:
        for _ in range(50):
            if "hello-from-server" in tail_log(log):
                break
            time.sleep(0.1)
        assert "hello-from-server" in tail_log(log)
        assert proc.poll() is None
    finally:
        stop_server(proc, logf, timeout=5)
    # 幂等：重复调用不抛异常
    stop_server(proc, logf, timeout=5)


def test_spawn_server_creates_parent_dirs(tmp_path):
    log = tmp_path / "deep" / "nested" / "dir" / "srv.log"
    proc, logf = spawn_server(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        log_path=log,
    )
    try:
        assert log.parent.is_dir()
    finally:
        stop_server(proc, logf, timeout=5)


def test_tail_log_missing_file_returns_empty(tmp_path):
    assert tail_log(tmp_path / "nope.log") == ""


def test_tail_log_truncates_to_max_chars(tmp_path):
    log = tmp_path / "big.log"
    log.write_bytes(b"x" * 5000 + b"END")
    out = tail_log(log, max_chars=10)
    # 末尾 10 字节 = 7 个 x + "END"
    assert out == "xxxxxxxEND"
    assert len(out) == 10
