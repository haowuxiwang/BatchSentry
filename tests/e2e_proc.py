"""E2E / 冒烟测试的服务子进程启动助手（单一来源）。

背景（2026-09-14 M8 实测定位）：
以 ``stdout=subprocess.PIPE`` 启动 long-running 服务进程却**从不排空**，是一类
静默死锁 —— 服务端日志写满 OS 管道缓冲（Windows 约 64KB）后，子进程阻塞在
``write()``；若是 asyncio 服务（uvicorn），阻塞发生在事件循环线程 → 事件循环
停摆 → 后续所有 HTTP 请求 read timeout。

症状特征：**卡点漂移**（第一次卡在请求 7，第二次卡在请求 8…），因为触发点
只取决于累计日志量，与具体请求无关 —— 极易被误判为产品缺陷。

对照实验（``devlogs/diag_pipe_block.py``，同一 APPDATA、同一请求序列）：
    stdout=DEVNULL → 10 passed / 0 failed
    stdout=PIPE    → 8 passed / 8 failed（稳定复现）

因此约定：long-running 服务进程的 stdout/stderr 一律落**日志文件**
（既保留诊断能力，又不会阻塞）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def spawn_server(cmd, *, log_path, cwd=None, env=None, extra=None):
    """启动服务子进程，stdout/stderr 追加写入 ``log_path``。

    返回 ``(proc, logfile)``；调用方结束时应调用 :func:`stop_server`
    （终止进程并关闭日志句柄）。
    """
    lp = Path(log_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "ab")
    kwargs = dict(cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT)
    if extra:
        kwargs.update(extra)
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        logf.close()
        raise
    return proc, logf


def tail_log(log_path, max_chars: int = 2000) -> str:
    """读取日志尾部（失败诊断用，绝不抛异常）。"""
    try:
        data = Path(log_path).read_bytes()
    except OSError:
        return ""
    return data[-max_chars:].decode("utf-8", errors="replace")


def stop_server(proc, logf=None, timeout: float = 10) -> None:
    """终止服务进程并关闭日志句柄（幂等，绝不抛异常）。"""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if logf is not None:
        try:
            logf.close()
        except Exception:
            pass
