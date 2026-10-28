"""数据目录独占锁 —— 保证「启动恢复」只由唯一持有者执行（R104 / P0-3）。

为什么需要它
------------
`recover_stuck_jobs` 的判据只有
``status IN (非终态) AND created_at < 进程启动时刻`` —— **没有任何 liveness 检查**。
于是任何第二个进程（冒烟实例、dev uvicorn、用户手动再开一个 exe）连同一个
``%APPDATA%\\PBC\\data.db`` 启动，都会把第一个进程**正在跑**的 job 判成 error。

2026-10-26 实测：51 页 / 138 MB 的任务，OCR 在 10:50:32 就 51/51 完成，Stage 2
跑到 11:07:07 分析了 33 页 —— 后端被构建脚本的冒烟测试强杀，**同时**新起的实例
（连同一个库）执行启动恢复，把 ``analyzing`` 判成 ``error``。用户看到的是
"跑到 35 页任务处理失败"。**只隔离端口不够，必须隔离"谁能执行恢复"。**

判据（一句话）
--------------
**只有拿到 ``<db_dir>/server.lock`` 独占锁的进程，才允许执行启动恢复。**
（``<db_dir>`` = 当前配置的 ``database_path`` 所在目录 —— 被保护的是那个库。）

锁由操作系统在进程退出时释放 —— 崩溃、``TerminateProcess``、断电后的下一次启动
都一样，所以不存在"陈旧锁需要清理"的问题。

为什么不用 `jobs.owner_pid` + 心跳
--------------------------------
崩溃恢复与"另一个进程还活着"在时间上**无法区分**：进程刚被杀，它最后一次心跳
必然"很新"，按心跳判活会**漏掉真正的崩溃** ⇒ job 永远停在非终态（无终态黑洞，
比误判更糟）。锁没有这个矛盾：持锁者一死，内核立刻释放。

边界
----
本模块只解决"谁能执行恢复"。两个进程同时**写**同一个库仍然是不受支持的用法
（``uvicorn`` 必须 ``workers=1`` 是既有的正确性前提）；本模块不改变这一点，
只是让"第二个进程"从**破坏者**变成**旁观者**。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_NAME = "server.lock"

#: 进程级单例：持有者必须活到进程退出（句柄一关，锁就没了）。
_lock: "InstanceLock | None" = None


class InstanceLock:
    """基于文件字节锁的进程互斥（Windows ``msvcrt`` / POSIX ``fcntl``）。

    不引第三方依赖：``msvcrt.locking`` 与 ``fcntl.flock`` 都是标准库，
    且都是**句柄级**语义 —— 同进程内第二次 ``open()`` 同样冲突，
    因此单进程测试可以覆盖跨进程的判据。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """尝试非阻塞加锁。已持有 ⇒ 直接返回 True（幂等）。"""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                # LK_NBLCK：非阻塞独占锁；锁第 0 个字节即代表整个文件
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 别人持锁（或文件系统不支持加锁）⇒ 我们不是唯一持有者
            fh.close()
            return False
        self._fh = fh
        # 写 pid 只为排障（"谁占着数据目录"），**不是**判据 ——
        # 判据永远是内核的锁本身。写失败不影响持有。
        #
        # ⚠️ Windows 实测：持锁期间**其他句柄连读都会被拒**
        # （`PermissionError: [Errno 13]`，字节范围锁覆盖第 0 字节）。
        # 所以想看 pid 只能在持锁者退出之后 —— 这正是排障场景，
        # 也说明 pid 天生只能当事后线索，不能当"谁活着"的判据。
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode("ascii"))
            fh.flush()
        except OSError:  # pragma: no cover —— 只读挂载等极端情形
            logger.debug("instance lock: 写 pid 失败（不影响持锁）", exc_info=True)
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            logger.debug("instance lock: 解锁失败（关闭句柄同样会释放）", exc_info=True)
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None


def lock_path(data_dir: Path | str | None = None) -> Path:
    """锁文件位置。

    默认落在**当前配置的数据库所在目录**（与 ``data.db`` 同级），而不是笼统的
    ``%APPDATA%\\PBC``。原因有两条：

    1. **语义**：被保护的资源是那个库，不是那个目录。库换了位置，锁就该跟着走。
    2. **可测**：测试把 ``config["app"].database_path`` 指到 ``tmp_path`` 时，
       锁自动隔离 —— 否则跑测试会去抢用户真实 APPDATA 里的锁，既污染外部状态，
       又会在用户开着应用时让测试里的启动恢复被静默跳过。
    """
    if data_dir is None:
        from config import config

        db_path = config["app"].database_path
        data_dir = Path(db_path).parent
    return Path(data_dir) / LOCK_NAME


def acquire_data_dir_lock(data_dir: Path | str | None = None) -> bool:
    """获取进程级数据目录锁（幂等）。返回是否**由本进程**持有。"""
    global _lock
    if _lock is not None and _lock.held:
        return True
    candidate = InstanceLock(lock_path(data_dir))
    if not candidate.acquire():
        return False
    _lock = candidate
    return True


def release_data_dir_lock() -> None:
    global _lock
    if _lock is not None:
        _lock.release()
        _lock = None


def holds_data_dir_lock() -> bool:
    return _lock is not None and _lock.held


def startup_recovery_allowed(data_dir: Path | str | None = None) -> bool:
    """**启动恢复的唯一闸门**：只有拿到数据目录锁的进程才允许执行。

    拿不到锁意味着"另一个进程正在服务同一个库"，它的 job 由它自己负责 ——
    我们既不该判它失败，也不该替它重试。
    """
    if acquire_data_dir_lock(data_dir):
        return True
    logger.warning(
        "数据目录已被另一个进程持有（%s）—— 跳过启动恢复，"
        "避免把对方正在跑的任务判成失败",
        lock_path(data_dir),
    )
    return False
