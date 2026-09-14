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
