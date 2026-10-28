"""R104/P0-3：数据目录独占锁 —— 启动恢复只能由唯一持有者执行。

背景（2026-10-26 实测事故）
--------------------------
`recover_stuck_jobs` 的判据只有
``status IN (非终态) AND created_at < 进程启动时刻`` —— **没有 liveness 检查**。
第二个进程（构建冒烟实例 / dev uvicorn / 用户再开一个 exe）连同一个
``%APPDATA%\\PBC\\data.db`` 启动，就会把第一个进程正在跑的 job 判成 error。
51 页任务"跑到 35 页任务处理失败"就是这么来的。

本文件锁的是**闸门本身**：拿不到锁的进程不得执行启动恢复，也不得跑看门狗
（看门狗按 `last_activity_at` 判停滞，对"别人正在跑的 job"同样会误判）。

⚠️ 这里测的是"第二个持有者拿不到锁"，不是"锁文件存在"。锁的判据永远是
内核的字节锁，pid 文本只是排障线索 —— M2 变异就是把写 pid 保留、去掉加锁。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import core.instance_lock as il
from core.instance_lock import (
    InstanceLock,
    acquire_data_dir_lock,
    holds_data_dir_lock,
    lock_path,
    release_data_dir_lock,
    startup_recovery_allowed,
)

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个用例前后都放锁，避免单例跨用例串味。"""
    release_data_dir_lock()
    yield
    release_data_dir_lock()


class TestInstanceLock:
    def test_acquire_then_second_holder_fails(self, tmp_path):
        p = tmp_path / "server.lock"
        first = InstanceLock(p)
        second = InstanceLock(p)
        assert first.acquire() is True
        assert second.acquire() is False, "同一路径的第二个持有者必须拿不到锁"
        first.release()
        assert second.acquire() is True, "释放后应可再次获取"

    def test_acquire_is_idempotent(self, tmp_path):
        p = tmp_path / "server.lock"
        lk = InstanceLock(p)
        assert lk.acquire() is True
        assert lk.acquire() is True
        assert lk.held is True

    def test_release_is_safe_when_not_held(self, tmp_path):
        lk = InstanceLock(tmp_path / "server.lock")
        lk.release()          # 不抛异常
        assert lk.held is False

    def test_lock_file_records_pid_for_diagnosis(self, tmp_path):
        """pid 是**事后**排障线索 —— Windows 持锁期间连读都不允许，故释放后再读。

        这条不是"判据"，只是"人看得懂"：真正的判据是内核的字节锁
        （`test_acquire_then_second_holder_fails`）。
        """
        p = tmp_path / "server.lock"
        lk = InstanceLock(p)
        assert lk.acquire() is True
        lk.release()
        assert p.read_text(encoding="ascii").strip().isdigit()

    def test_creates_parent_directory(self, tmp_path):
        p = tmp_path / "nested" / "deeper" / "server.lock"
        lk = InstanceLock(p)
        assert lk.acquire() is True
        try:
            assert p.exists()
        finally:
            lk.release()


class TestStartupRecoveryGate:
    """闸门：拿不到锁 ⇒ 不允许执行恢复。"""

    def test_allowed_when_free(self, tmp_path):
        assert startup_recovery_allowed(tmp_path) is True
        assert holds_data_dir_lock() is True

    def test_blocked_when_another_process_holds_it(self, tmp_path):
        other = InstanceLock(lock_path(tmp_path))
        assert other.acquire() is True
        try:
            assert startup_recovery_allowed(tmp_path) is False, (
                "另一个进程持锁时不得执行启动恢复 —— 否则会抢走它正在跑的 job"
            )
            assert holds_data_dir_lock() is False
        finally:
            other.release()

    def test_allowed_again_after_other_releases(self, tmp_path):
        other = InstanceLock(lock_path(tmp_path))
        assert other.acquire() is True
        assert startup_recovery_allowed(tmp_path) is False
        other.release()
        assert startup_recovery_allowed(tmp_path) is True

    def test_second_call_is_idempotent(self, tmp_path):
        assert startup_recovery_allowed(tmp_path) is True
        assert startup_recovery_allowed(tmp_path) is True
        assert acquire_data_dir_lock(tmp_path) is True

    def test_lock_path_lives_in_data_dir(self, tmp_path):
        assert lock_path(tmp_path) == tmp_path / il.LOCK_NAME

    def test_default_lock_path_follows_the_configured_database(self, test_db):
        """锁必须跟着**库**走，不是跟着 APPDATA。

        否则跑测试会去抢用户真实 APPDATA 里的锁：既污染外部状态，又会在用户
        开着应用时让测试里的启动恢复被静默跳过（`test_db` 把库指到 tmp_path，
        锁就该跟着进 tmp_path）。
        """
        from config import config

        db_path = Path(config["app"].database_path)
        assert lock_path() == db_path.parent / il.LOCK_NAME
        assert lock_path().parent == db_path.parent


class TestMainWiring:
    """静态不变式：`main.py` 的两条后台守护必须都在闸门之后。

    行为测试拿不到 lifespan（要起整个 app），所以这里只钉**结构**：
    闸门必须出现在 `_recover_bg` / `_watchdog_bg` 的 spawn 之前，
    且两处 spawn 都被 `_owns_data_dir` 包住。
    """

    @pytest.fixture(scope="class")
    def src(self) -> str:
        return (_ROOT / "main.py").read_text(encoding="utf-8")

    def test_gate_is_called(self, src):
        assert "startup_recovery_allowed()" in src

    def test_recovery_spawn_is_gated(self, src):
        gate = src.index("startup_recovery_allowed()")
        spawn = src.index("_spawn(_recover_bg())")
        assert gate < spawn, "闸门必须在恢复任务被创建之前"
        assert "if _owns_data_dir:\n        _spawn(_recover_bg())" in src

    def test_watchdog_spawn_is_gated(self, src):
        assert "if _owns_data_dir:\n        _spawn(_watchdog_bg())" in src, (
            "看门狗按 last_activity_at 判停滞，对另一个进程的活任务同样会误判"
        )

    def test_lock_is_released_on_shutdown(self, src):
        assert "release_data_dir_lock()" in src
        close = src.index("await close_db()\n    # R104/P0-3")
        release = src.index("release_data_dir_lock()", close)
        assert close < release, "必须先关库再放锁"
