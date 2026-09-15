"""tests/e2e_proc.py 助手的行为测试 + "未排空 PIPE" 回归护栏。

背景（2026-09-14 M8 实测）：多个 e2e harness 用
``subprocess.Popen(..., stdout=subprocess.PIPE)`` 启动长驻服务却从不排空管道，
服务端日志写满 OS 管道缓冲(~64KB)后子进程阻塞在 write → asyncio 事件循环停摆
→ 后续请求全部超时。症状是"卡点漂移"，极易误判为产品缺陷
（``tests/e2e_frozen.py`` 一度 6/16 通过，修复后 16/16）。

本文件：
1. 行为验证 helper（落盘 / 读尾 / 幂等停止）；
2. **源码扫描护栏**：tests/ 下不得再用未排空的 PIPE 启动服务，必须走
   ``tests.e2e_proc.spawn_server``。
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

# 曾经被硬编码进 3 个 e2e 脚本、随提交进入 git 历史的真实 LLM 密钥
# （2026-09-15 发现）。这里**故意用拼接**构造，使本文件自身不含 32 连串的
# `sk-` 字面量 —— 否则下面那条通用扫描规则会命中它自己，只能靠白名单放行，
# 而白名单会让"往这个文件里加密钥"也逃过检查。
_LEAKED_KEY = "sk-vprnpmjfzbcinduybbsboaw" + "tjxtrnrhfldbargfwzkieuczu"

# 通用规则：`sk-` 后跟 32+ 个不含分隔符的字符 = 形似真实密钥。
# 仓库里的占位符均显著短于该阈值（如 sk-test-key-for-unit-test-only 含短横、
# sk-realkey1234567890abcdef 仅 24 字符），故不会被误报。
_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{32,}")

# 源码扫描范围：产品与工程脚本（排除第三方、产物、本地日志目录）
_SCAN_DIRS = ("api", "core", "db", "llm", "scripts", "tests", "tools", "models")
_SCAN_SUFFIX = {".py", ".js", ".md", ".ps1", ".json", ".sql", ".html"}
# 产物/缓存目录按**前缀**排除：dist、dist-electron、dist-electron-m8 …
# 不罗列具体名 —— 安全软件占锁时 build.ps1 会自愈到备用输出目录，目录名会变；
# 写死列表就得每次回来同步（历史上正是这么积出 4 个 dist-electron* 的）。
_SCAN_SKIP = {"node_modules", "build", "htmlcov", "devlogs"}
_SCAN_SKIP_PREFIX = ("dist",)


def _source_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _SCAN_SUFFIX:
                continue
            parts = set(p.relative_to(_ROOT).parts)
            if parts & _SCAN_SKIP or any(pt.startswith(_SCAN_SKIP_PREFIX) for pt in parts):
                continue
            yield p


def test_no_true_llm_key_in_repo_sources():
    """工作树任何源文件都不得出现那把已泄漏的真实密钥。

    注意：删除**不能**抹掉 git 历史 —— 该密钥自 2026-08-24（``81964a3``）起
    就在历史中且已推送，唯一补救是到服务商处**轮换**。本用例只保证不再扩散。
    """
    offenders = []
    for p in _source_files():
        if _LEAKED_KEY in p.read_text(encoding="utf-8", errors="replace"):
            offenders.append(p.relative_to(_ROOT).as_posix())
    assert not offenders, (
        "以下文件仍硬编码着已泄漏的 LLM 密钥（应改为从环境变量读取）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


def test_no_hardcoded_long_api_keys():
    """通用护栏：源码里不得出现形似真实密钥的 32+ 连串 ``sk-`` 字面量。"""
    offenders = []
    for p in _source_files():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _KEY_RE.search(line):
                offenders.append(f"{p.relative_to(_ROOT).as_posix()}:{i}")
    assert not offenders, (
        "以下位置疑似硬编码了真实 API key（请改为环境变量注入）：\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )


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


def test_llm_key_absent_yields_empty(monkeypatch):
    monkeypatch.delenv("PBC_E2E_DEEPSEEK_KEY", raising=False)
    from tests.e2e_proc import llm_key
    assert llm_key() == ""


def test_llm_key_from_env(monkeypatch):
    monkeypatch.setenv("PBC_E2E_DEEPSEEK_KEY", "sk-from-env-only")
    from tests.e2e_proc import llm_key
    assert llm_key() == "sk-from-env-only"


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
