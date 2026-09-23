# -*- coding: utf-8 -*-
"""构建运行台账的**唯一判据实现**（B9-6）。

## 它解决什么

把 `build.ps1` 放到**后台**跑（或让长构建跨回合运行），进程会在回合结束时被
连带回收。这时的症状与"构建真的崩了"**完全一致**：

    * 任务报 exit 1 且**零输出**；
    * `build/pyinstaller.log` **戛然而止、没有 Traceback**；
    * workpath 为空。

2026-09-21 实测为此误判过一次"PyInstaller 构建失败"（真因是进程被回收；改前台
跑同一条命令 113.8 s 一次通过）。靠人肉推理区分这两者，不是能长期依赖的判据。

## 判据（"有始无终"就是被杀的签名）

`build.ps1` 起步时写 ``<run_id>.start.json``，跑完（成功**或**失败）时写
``<run_id>.finish.json``。进程被 ``TerminateProcess`` 时 **finally 不会执行**
（Windows 强杀不给收尾机会）⇒ **只有 start、没有 finish = 被终止**。

于是六态可分：

==================  ====================================================
none                接入台账之后从未跑过构建
running             start 在、无 finish，且**该 pid 仍存活**
interrupted         start 在、无 finish，且该 pid 已不存在（被杀/断电/Ctrl-C）
failed              finish 在，``rc != 0``
completed           finish 在，``rc == 0``
unreadable          台账存在但**解析不出来** —— 单列一态，**绝不退化成 none**
==================  ====================================================

两条不可省的细节：

* **pid 存活**必须查：否则"正在跑的构建"会被误报成 interrupted。
* ``unreadable`` 必须单列：**"判不了"与"确凿地没跑过"是两件事**
  （把前者读成后者，就等于把"证据缺失"当成"证据清白"）。

run_id 把一次运行与它的结局**配对**，所以上一次的 finish 不会被误配给这一次的
start（只用"文件是否存在"就做不到这点）。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

STATE_NONE = "none"
STATE_RUNNING = "running"
STATE_INTERRUPTED = "interrupted"
STATE_FAILED = "failed"
STATE_COMPLETED = "completed"
STATE_UNREADABLE = "unreadable"

_START_SUFFIX = ".start.json"
_FINISH_SUFFIX = ".finish.json"

LABELS = {
    STATE_NONE: "无记录（接入台账后未跑过构建）",
    STATE_RUNNING: "进行中",
    STATE_INTERRUPTED: "被中断（**不是构建失败**）",
    STATE_FAILED: "跑完但失败",
    STATE_COMPLETED: "跑完且成功",
    STATE_UNREADABLE: "台账不可解析（判不了 ≠ 没跑过）",
}

# 每种状态该做什么 —— 这一栏才是本工具真正的产出：把"看起来像崩了"翻译成
# "是什么、下一步做什么"。
ADVICE = {
    STATE_NONE: "先跑一次构建（build.ps1）再问状态。",
    STATE_RUNNING: "构建仍在进行，等它结束；不要在同一目录并发跑第二次。",
    STATE_INTERRUPTED: (
        "构建进程被外部终止，**没有留下退出码**。最常见原因：把长构建放到后台/"
        "跨回合运行，被会话拆卸连带回收（终端关闭、Ctrl-C、断电同属此类）。"
        "这不是构建失败 —— 产物可能停在中间状态。"
        "处置：**前台**重跑 build.ps1（不要挂后台、不要跨回合）。"),
    STATE_FAILED: "构建跑完但失败：看 finish 台账的 error 字段与该步骤的日志。",
    STATE_COMPLETED: "构建跑完且成功。接着跑 scripts/release_gate.py 验产物。",
    STATE_UNREADABLE: (
        "台账读不出来（损坏/半截写入）⇒ **判不了**。别把它当成「没跑过」或"
        "「跑成功了」；先看 build/_run/ 下的原始文件。"),
}


def process_alive(pid: int) -> bool:
    """``pid`` 是否仍存活。

    ⚠️ Windows 上**不能**用 ``os.kill(pid, 0)``：它只接受 SIGTERM/CTRL_* 一类
    信号，语义与 POSIX 的"0 号信号探测"不同。正解是 ``OpenProcess`` ——
    打不开（``ERROR_INVALID_PARAMETER``）即进程不存在；能打开则看退出码是否
    仍为 ``STILL_ACTIVE``。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle),
                                               ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在，只是不允许发信号
    return True


@dataclass(frozen=True)
class BuildRun:
    state: str
    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    rc: int | None = None
    git_head: str | None = None
    detail: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.state, self.state)

    @property
    def advice(self) -> str:
        return ADVICE.get(self.state, "")


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"台账根节点不是对象：{type(data).__name__}")
    return data


def read_state(run_dir, *, alive=process_alive) -> BuildRun:
    """判定最近一次构建的结局。``alive`` 可注入以便测试。

    选"最近"用 **run_id 的字典序**而不是 JSON 里的时间戳：run_id 以定长
    ``yyyyMMdd-HHmmss-<pid>`` 开头，字典序即时间序 —— 这样**即使 JSON 坏掉**
    也仍能挑出最新的一次（否则连"读到的是哪一次"都定不下来）。
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return BuildRun(STATE_NONE, detail=f"无台账目录：{run_dir}（接入台账后未跑过构建）")
    starts = sorted(run_dir.glob(f"*{_START_SUFFIX}"))
    if not starts:
        return BuildRun(STATE_NONE, detail=f"台账目录为空：{run_dir}")

    start_file = starts[-1]
    run_id = start_file.name[: -len(_START_SUFFIX)]
    try:
        meta = _load(start_file)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return BuildRun(STATE_UNREADABLE, run_id,
                        detail=f"{start_file.name} 不可解析：{type(e).__name__}: {e}")

    pid = meta.get("pid")
    started_at = meta.get("started_at")
    git_head = meta.get("git_head")

    finish_file = run_dir / f"{run_id}{_FINISH_SUFFIX}"
    if finish_file.is_file():
        try:
            fin = _load(finish_file)
            rc = int(fin.get("rc"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            return BuildRun(STATE_UNREADABLE, run_id, started_at, None, pid, None,
                            git_head,
                            detail=f"{finish_file.name} 不可解析：{type(e).__name__}: {e}")
        if rc == 0:
            return BuildRun(STATE_COMPLETED, run_id, started_at,
                            fin.get("finished_at"), pid, rc, git_head,
                            detail="有 start 与 finish，rc=0")
        return BuildRun(STATE_FAILED, run_id, started_at, fin.get("finished_at"),
                        pid, rc, git_head,
                        detail=f"有 start 与 finish，rc={rc}"
                               f"{'；' + str(fin.get('error')) if fin.get('error') else ''}")

    if not isinstance(pid, int):
        return BuildRun(STATE_UNREADABLE, run_id, started_at, None, None, None,
                        git_head,
                        detail="start 台账缺 pid —— 无法判断是否仍在跑，故不敢断言结局")
    if alive(pid):
        return BuildRun(STATE_RUNNING, run_id, started_at, None, pid, None,
                        git_head, detail=f"有 start 无 finish，且 pid={pid} 仍存活")
    return BuildRun(STATE_INTERRUPTED, run_id, started_at, None, pid, None, git_head,
                    detail=f"有 start 无 finish，且 pid={pid} 已不存在"
                           "（进程被终止 ⇒ 没机会落 finish）")


def render(run: BuildRun) -> str:
    lines = [f"构建状态：{run.state} —— {run.label}", f"  detail     : {run.detail}"]
    if run.run_id:
        lines.append(f"  run_id     : {run.run_id}")
    if run.started_at:
        lines.append(f"  started_at : {run.started_at}")
    lines.append(f"  finished_at: {run.finished_at or '（无 finish 台账）'}")
    if run.git_head:
        lines.append(f"  git_head   : {run.git_head}")
    if run.pid is not None:
        lines.append(f"  pid        : {run.pid}")
    if run.rc is not None:
        lines.append(f"  rc         : {run.rc}")
    lines.append(f"  → {run.advice}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="判定最近一次构建的结局（B9-6：区分「被杀」与「真失败」）")
    ap.add_argument("--run-dir", default=None,
                    help="台账目录（默认 <repo>/build/_run）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--assert-state", dest="assert_state", default=None,
                    metavar="STATE",
                    help="状态不等于该值时以退出码 1 结束（供脚本消费）")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir) if args.run_dir else \
        Path(__file__).resolve().parents[1] / "build" / "_run"
    run = read_state(run_dir)

    if args.json:
        print(json.dumps({
            "state": run.state, "label": run.label, "detail": run.detail,
            "run_id": run.run_id, "started_at": run.started_at,
            "finished_at": run.finished_at, "pid": run.pid, "rc": run.rc,
            "git_head": run.git_head, "advice": run.advice,
        }, ensure_ascii=False, indent=2))
    else:
        print(render(run))

    if args.assert_state and run.state != args.assert_state:
        print(f"\n[断言失败] 期望 {args.assert_state}，实得 {run.state}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
