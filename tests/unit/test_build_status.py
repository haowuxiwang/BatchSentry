# -*- coding: utf-8 -*-
"""B9-6 护栏：构建运行台账必须能把「被杀」与「真失败」分开。

为什么值得单独立护栏（2026-09-21 实测的代价）：
    把 `build.ps1` 放**后台**跑、回合结束被连带回收，症状与"PyInstaller 崩了"
    **完全一致** —— exit 1 + 零输出 + 日志戛然而止无 Traceback + workpath 空。
    当时据此误判成构建失败，白排查一轮（改前台跑同命令 113.8 s 一次通过）。
    "这次构建到底怎么了"必须由**证据**回答，不能靠人肉推理。

核心判据：进程被 ``TerminateProcess`` 时 **finally 不执行** ⇒
**有 start 无 finish = 被终止**。三条性质缺一，判据就失效：

    ① 有始无终 + pid 已死 ⇒ interrupted；
    ② 有始无终 + pid **存活** ⇒ running（否则正在跑的构建会被误报成"被杀"）；
    ③ 读不出来 ⇒ unreadable（**不得**退化成 none —— "判不了" ≠ "没跑过"）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts import build_status as bs  # noqa: E402

RUN_ID_OLD = "20260101-000000-1111"
RUN_ID_NEW = "20260923-090000-2222"


def _start(run_dir: Path, run_id: str, pid, **extra) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "pid": pid, "started_at": "2026-09-23T09:00:00",
            "git_head": "6f1de53"}
    meta.update(extra)
    (run_dir / f"{run_id}.start.json").write_text(
        json.dumps(meta), encoding="utf-8")


def _finish(run_dir: Path, run_id: str, rc, error="") -> None:
    (run_dir / f"{run_id}.finish.json").write_text(
        json.dumps({"run_id": run_id, "rc": rc, "error": error,
                    "finished_at": "2026-09-23T09:05:00"}), encoding="utf-8")


# ── 六态判定 ────────────────────────────────────────────────────────


def test_no_ledger_directory_is_none(tmp_path):
    assert bs.read_state(tmp_path / "does-not-exist").state == bs.STATE_NONE


def test_empty_ledger_directory_is_none(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    assert bs.read_state(d).state == bs.STATE_NONE


def test_completed_when_finish_says_rc_zero(tmp_path):
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    _finish(d, RUN_ID_NEW, 0)
    run = bs.read_state(d, alive=lambda pid: False)
    assert run.state == bs.STATE_COMPLETED
    assert run.rc == 0 and run.run_id == RUN_ID_NEW
    assert run.git_head == "6f1de53"


def test_failed_carries_the_reason_from_the_ledger(tmp_path):
    """失败态要把错误文本带出来 —— 否则台账只告诉你"失败了"，还是要翻日志。"""
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    _finish(d, RUN_ID_NEW, 1, error="PyInstaller build failed")
    run = bs.read_state(d, alive=lambda pid: False)
    assert run.state == bs.STATE_FAILED
    assert run.rc == 1
    assert "PyInstaller build failed" in run.detail


def test_interrupted_when_no_finish_and_pid_is_gone(tmp_path):
    """**核心**：有始无终 + 进程已不存在 ⇒ interrupted（不是 failed）。"""
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    run = bs.read_state(d, alive=lambda pid: False)
    assert run.state == bs.STATE_INTERRUPTED, (
        "有 start 无 finish 且进程已死 ⇒ 必须判 interrupted；"
        "若判成 failed，就等于把「被回收」读成「构建有缺陷」")
    assert run.rc is None, "被终止的运行**不该**有退出码 —— 那是它与 failed 的分界"


def test_running_when_no_finish_and_pid_is_alive(tmp_path):
    """**核心对照**：pid 还活着 ⇒ running。少了这条，正在跑的构建会被误报成被杀。"""
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, os.getpid())
    run = bs.read_state(d, alive=lambda pid: True)
    assert run.state == bs.STATE_RUNNING


def test_unreadable_when_start_ledger_is_corrupt(tmp_path):
    """**不得**退化成 none —— "判不了"与"确凿地没跑过"是两件事。"""
    d = tmp_path / "run"
    d.mkdir()
    (d / f"{RUN_ID_NEW}.start.json").write_text("{半截写入", encoding="utf-8")
    run = bs.read_state(d)
    assert run.state == bs.STATE_UNREADABLE
    assert run.state != bs.STATE_NONE


def test_unreadable_when_finish_ledger_is_corrupt(tmp_path):
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    (d / f"{RUN_ID_NEW}.finish.json").write_text("not json", encoding="utf-8")
    assert bs.read_state(d).state == bs.STATE_UNREADABLE


def test_unreadable_when_pid_is_missing(tmp_path):
    """缺 pid 就无法判断"是否仍在跑" ⇒ 不敢断言结局（fail-closed）。"""
    d = tmp_path / "run"
    d.mkdir()
    (d / f"{RUN_ID_NEW}.start.json").write_text(
        json.dumps({"run_id": RUN_ID_NEW, "started_at": "x"}), encoding="utf-8")
    run = bs.read_state(d, alive=lambda pid: False)
    assert run.state == bs.STATE_UNREADABLE, (
        "没有 pid 时既不能断言 running 也不能断言 interrupted")


def test_latest_run_wins_and_old_finish_is_not_mismatched(tmp_path):
    """run_id 配对：上一次的 finish **不得**被配给这一次的 start。

    只用"文件是否存在"就做不到这点 —— 那会把"上一次成功"读成"这一次成功"。
    """
    d = tmp_path / "run"
    _start(d, RUN_ID_OLD, 1111)
    _finish(d, RUN_ID_OLD, 0)          # 上一次：成功
    _start(d, RUN_ID_NEW, 2222)        # 这一次：只有 start（被打断）
    run = bs.read_state(d, alive=lambda pid: False)
    assert run.run_id == RUN_ID_NEW, "必须取**最新**的一次运行"
    assert run.state == bs.STATE_INTERRUPTED, (
        f"错把上一次的 rc=0 配给了这一次 ⇒ 被杀的构建会被报成成功（实得 {run.state}）")


def test_interrupted_advice_is_actionable_not_boilerplate(tmp_path):
    """被中断的解读必须点明"这不是构建失败"+给出"前台重跑"。

    这一栏才是本工具的价值所在：把"看起来像崩了"翻译成"是什么、下一步做什么"。
    退化成"构建出现问题，请检查"就等于没有。
    """
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    advice = bs.read_state(d, alive=lambda pid: False).advice
    assert "不是构建失败" in advice
    assert "前台" in advice


# ── 进程存活检测（真实调用） ────────────────────────────────────────


def test_process_alive_detects_the_current_process():
    assert bs.process_alive(os.getpid()) is True


def test_process_alive_is_false_for_an_absent_pid():
    # 取一个几乎不可能存在的 PID（Windows 上限约 2^32，Linux 默认 pid_max 远小）
    assert bs.process_alive(0x7FFFFFF0) is False


# ── CLI 契约 ────────────────────────────────────────────────────────


def test_cli_assert_state_returns_one_on_mismatch(tmp_path, capsys):
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    _finish(d, RUN_ID_NEW, 0)
    assert bs.main(["--run-dir", str(d), "--assert-state", "completed"]) == 0
    assert bs.main(["--run-dir", str(d), "--assert-state", "interrupted"]) == 1
    capsys.readouterr()


def test_cli_json_is_machine_readable(tmp_path, capsys):
    d = tmp_path / "run"
    _start(d, RUN_ID_NEW, 4242)
    assert bs.main(["--run-dir", str(d), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == bs.STATE_INTERRUPTED
    assert payload["advice"]


# ── build.ps1 的接线（结构性） ──────────────────────────────────────
#
# PowerShell 脚本没法用 pytest 直接单测，但"接线是否还在"必须能红 ——
# 否则将来的重构可以把台账悄悄拆掉，而 `build_status.py` 的测试照样全绿
# （判据被测了，**喂给判据的证据**没了）。


def _ps_block_at(src: str, brace_index: int) -> str:
    """从 ``{`` 处做**大括号配对**，返回含花括号的整块。"""
    depth = 0
    for k in range(brace_index, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_index:k + 1]
    raise AssertionError("大括号不配对 —— 提取失败，判据不可信")


def _ps_function_body(src: str, name: str) -> str:
    """取 PowerShell 函数体。

    只在**这一个函数体内**找关键调用 —— 全文找 token 会在别处也有同名调用时
    恒真（PITFALLS §二十八 的"文本在 ≠ 语义在"）。
    """
    i = src.index(f"function {name}(")
    return _ps_block_at(src, src.index("{", i))


def test_build_script_wires_the_ledger():
    src = (_ROOT / "build.ps1").read_text(encoding="utf-8-sig")

    # ① 台账初始化与 Write-Finish 定义必须**早于** pre-flight：
    #    pre-flight 就会 Write-Fail，那时若台账尚未就绪，"失败"与"被杀"当场不可分。
    pre = src.index('Write-Step "Pre-flight checks"')
    assert src.index("$runId = ") < pre, "台账初始化晚于 pre-flight"
    assert src.index("function Write-Finish") < pre, "Write-Finish 定义晚于 pre-flight"

    # ② 失败出口必须落 rc=1（在 **Write-Fail 函数体内**找）
    assert "Write-Finish 1" in _ps_function_body(src, "Write-Fail"), \
        "唯一失败出口没落退出码 ⇒ 真失败与「被杀」不可分"

    # ③ 成功出口必须落 rc=0（在 Summary 段里）
    summary = src[src.index("# ── Summary"):]
    assert "Write-Finish 0" in summary, "成功出口没落退出码"

    # ④ **未捕获异常**也必须落退出码（trap 块内）。
    #    2026-09-23 实测（正是这条判据发现的）：`& python --version` 在 python
    #    不在 PATH 时抛 CommandNotFoundException —— 它是 statement-terminating
    #    错误，会**绕过 Write-Fail** 直接中断脚本 ⇒ 台账停在"有始无终"，
    #    "工具缺失"于是伪装成"构建被杀"。没有 trap 这条出口，判据只剩一半。
    trap_body = _ps_block_at(src, src.index("trap {"))
    assert "Write-Finish 1" in trap_body, \
        "trap 没落退出码 ⇒ 未捕获异常会被读成「被杀」"

    # ⑤ start 台账必须真的写出去（不是只算了 runId）
    assert ".start.json" in src and "ConvertTo-Json" in src


@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows PowerShell")
def test_real_failure_writes_a_finish_ledger(tmp_path):
    """**端到端**：真的让 build.ps1 失败一次，finish 台账必须带 rc=1。

    做法：把脚本复制到 tmp（台账因此写在 tmp 里，不污染仓库），并给一个
    **不含任何工具**的 PATH ⇒ pre-flight 第一步就 Write-Fail。

    这条补齐的是判据的另一半：**"跑完但失败"会留下退出码**，从而与"被杀"
    （有始无终）区分开 —— 只测 interrupted 那半边，判据可能是靠"永远没有
    finish"通过的，那就把 failed 也吞进去了。
    """
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("找不到 powershell")

    shutil.copy(_ROOT / "build.ps1", tmp_path / "build.ps1")
    env = dict(os.environ)
    env["PATH"] = str(tmp_path)          # 空目录 ⇒ python / node / npm 全找不到
    env.pop("PSModulePath", None)

    proc = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(tmp_path / "build.ps1")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180)

    assert proc.returncode == 1, f"应当失败退出 1，实得 {proc.returncode}\n{proc.stdout[-500:]}"
    run = bs.read_state(tmp_path / "build" / "_run")
    assert run.state == bs.STATE_FAILED, (
        f"真实失败必须被判定为 failed（实得 {run.state}）；"
        f"若判 interrupted 说明 finish 台账没写出来\n{run.detail}")
    assert run.rc == 1
