"""procpool 单测 — 三层验证：

1. pytest 线程回退（设计行为：_in_pytest → to_thread，进程隔离是生产诉求）
2. 真实进程池 in-process（patch _in_pytest=False：创建/缓存/executor 路径/关闭）
3. spawn 隔离 subprocess 行为验证（子进程真实运行 + PID 不同 + 干净退出）

背景（2026-08-24 覆盖率核查）：Round 16 的 procpool 在 pytest 下全程线程
回退 → 进程池分支 46% 覆盖拖垮 90% 门禁。本组测试在 pytest 内显式创建
真实 spawn 池补齐父进程侧分支；worker 体内代码（仅子进程执行）标 pragma，
由 subprocess 测试锁定行为。
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _child_pid() -> int:
    """模块级可 pickle 函数：返回自己（worker）的 PID。"""
    return os.getpid()


class TestPytestThreadFallback:
    """pytest 环境：run_cpu 走 to_thread（同进程执行）。"""

    def test_in_pytest_detection(self):
        from core import procpool
        assert procpool._in_pytest() is True

    @pytest.mark.asyncio
    async def test_run_cpu_same_process(self):
        """pytest 下回退线程 → 结果正确且与当前进程同 PID。"""
        from core import procpool
        got = await procpool.run_cpu(_child_pid, label="t")
        assert got == os.getpid()

    @pytest.mark.asyncio
    async def test_unpicklable_fn_fallback(self):
        """lambda 不可 pickle → _is_picklable False → 直接线程路径。"""
        from core import procpool
        assert procpool._is_picklable(lambda: 1) is False
        assert procpool._is_picklable(_child_pid) is True
        got = await procpool.run_cpu(lambda a, b: a + b, 1, 2)
        assert got == 3


class TestRealPoolInProcess:
    """patch _in_pytest → 真实 spawn 池：创建/缓存/executor/关闭全路径。

    池生命周期严格收敛在 fixture 内（shutdown + 全局复位），不污染
    其他测试（它们依赖 _pool is None 走线程回退）。
    """

    @pytest.fixture(autouse=True)
    def _real_pool(self, monkeypatch):
        from core import procpool
        monkeypatch.setattr(procpool, "_in_pytest", lambda: False)
        procpool._pool = None
        procpool._pool_broken = False
        yield
        procpool.shutdown_pool()
        procpool._pool = None
        procpool._pool_broken = False

    @pytest.mark.asyncio
    async def test_pool_created_and_cached(self):
        from core import procpool
        pool1 = procpool._get_pool()
        assert pool1 is not None
        pool2 = procpool._get_pool()
        assert pool2 is pool1  # 惰性单例缓存（line 91）

    @pytest.mark.asyncio
    async def test_run_cpu_executes_in_child_process(self):
        """executor 路径：结果正确且 worker PID ≠ 当前进程（真隔离）。"""
        from core import procpool
        got = await procpool.run_cpu(_child_pid, label="real")
        assert isinstance(got, int)
        assert got != os.getpid()

    @pytest.mark.asyncio
    async def test_child_exception_propagates(self):
        """子进程内异常原样冒泡（与 to_thread 语义一致）。"""

        def _boom() -> int:
            raise ValueError("worker-side error")

        from core import procpool
        with pytest.raises(ValueError, match="worker-side error"):
            await procpool.run_cpu(_boom)

    @pytest.mark.asyncio
    async def test_shutdown_pool_idempotent(self):
        from core import procpool
        procpool._get_pool()
        procpool.shutdown_pool()
        assert procpool._pool is None
        procpool.shutdown_pool()  # 二次关闭无操作
        # 关闭后再次 _get_pool → 重建新池（不因关闭而 broken）
        assert procpool._get_pool() is not None


class TestSpawnIsolationSubprocess:
    """subprocess 行为验证：非 pytest 环境下 run_cpu 真走 spawn 子进程。

    覆盖率不计（子进程数据文件不合并），锁定的是生产行为：
    隔离（PID 不同）+ 结果回传 + 进程干净退出（守卫/atexit 不挂起）。
    """

    def test_real_isolation_in_clean_env(self, tmp_path):
        script = tmp_path / "pool_check.py"
        script.write_text(
            "import asyncio, os, sys\n"
            f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
            "from core import procpool\n"
            "\n"
            "def _child_pid():\n"
            "    return os.getpid()\n"
            "\n"
            "async def main():\n"
            "    mine = os.getpid()\n"
            "    theirs = await procpool.run_cpu(_child_pid)\n"
            "    procpool.shutdown_pool()\n"
            "    print(f'PARENT={mine} CHILD={theirs} "
            "ISOLATED={theirs != mine}')\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    # spawn 守卫必须：worker 启动时会重跑本脚本（__mp_main__）\n"
            "    asyncio.run(main())\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items()
               if k != "PYTEST_CURRENT_TEST"}
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=120, env=env,
            cwd=str(PROJECT_ROOT),
        )
        assert r.returncode == 0, f"stderr: {r.stderr[-500:]}"
        assert "ISOLATED=True" in r.stdout
