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


def _hang_forever() -> None:
    """模块级（**必须**——局部函数不可 pickle，会静默走线程回退而不是池路径）。

    worker 内长时间阻塞，用于制造"超时 → 回收池"场景。
    """
    import time
    time.sleep(300)


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


class TestCpuTaskTimeout:
    """超时兜底：run_cpu 不得让"worker 挂死"退化成"永久等待"。

    为什么必须有：``run_in_executor`` / ``to_thread`` 的等待**没有内建上限**。
    外部 HTTP 调用均有 timeout（LLM 180s / MinerU 60-300s / Paddle 轮询封顶
    3600s），唯独此处原本无上限 —— 它是"job 永久停在非终态、SSE 无限等待"
    的唯一真实入口。

    超时后**必须回收进程池**：``max_workers=1`` 的池里，一个挂死的 worker
    永久占住唯一槽位，会把"某个 job 卡住"扩散成"整个应用不再处理新任务"。
    """

    def test_default_timeout_is_generous(self):
        """默认值必须远大于正常 Stage 0 耗时（138MB/51 页实测百秒量级），
        否则会误杀正常任务 —— 这个常量是"永久等待兜底"而非"性能限制"。"""
        from core import procpool
        assert procpool._DEFAULT_TIMEOUT_S >= 300

    @pytest.mark.asyncio
    async def test_thread_path_timeout_raises(self):
        """线程回退路径超时 → TimeoutError（线程无法强杀，但 job 不再永久非终态）。"""
        import time

        from core import procpool

        def _sleep_long():
            time.sleep(5)

        with pytest.raises(TimeoutError, match="timed out"):
            await procpool.run_cpu(_sleep_long, label="sleepy", timeout=0.2)

    @pytest.mark.asyncio
    async def test_timeout_zero_means_unlimited(self):
        """``timeout=0`` = 不设限（测试/特殊场景逃生口）。"""
        from core import procpool
        got = await procpool.run_cpu(_child_pid, timeout=0)
        assert got == os.getpid()

    @pytest.mark.asyncio
    async def test_normal_path_unaffected_by_timeout(self):
        """正常任务在超时包装下结果不变（不得引入行为漂移）。"""
        from core import procpool
        got = await procpool.run_cpu(_child_pid, label="ok", timeout=60)
        assert got == os.getpid()

    def test_recycle_pool_with_no_pool_is_noop(self):
        from core import procpool
        procpool._pool = None
        procpool._recycle_pool("no pool")  # 不得抛异常
        assert procpool._pool is None


class TestProcessPoolTimeoutRecycle:
    """真实 spawn 池下的超时 → 回收（父进程侧分支）。"""

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
    async def test_timeout_recycles_pool_and_raises(self):
        """池路径超时：抛 TimeoutError 且单例被丢弃（后续任务可重新建池）。

        用**模块级** ``_hang_forever`` —— 局部函数不可 pickle，会走线程回退
        而非池路径，测不到回收逻辑（这个坑本用例第一次写时就踩到了）。
        """
        from core import procpool

        assert procpool._is_picklable(_hang_forever) is True
        assert procpool._get_pool() is not None
        with pytest.raises(TimeoutError, match="process pool recycled"):
            await procpool.run_cpu(_hang_forever, label="hang", timeout=0.5)
        assert procpool._pool is None, "超时后必须丢弃池 —— 否则唯一槽位被挂死 worker 占住"
        # 回收后仍可重建（不因此进入 broken 状态）
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
