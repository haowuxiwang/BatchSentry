"""dist 产物目录体检 + 安全清理（单一入口）。

## 为什么需要它

electron-builder 的输出目录名会**漂移**：``resources/app.asar`` 被外部句柄占住时，
``build.ps1`` 会自愈到备用目录 ``dist-electron-locked``；手工重打包又可能显式指定
``dist-electron-v112``。日积月累，仓库里就堆着四五个 ``dist*`` 目录，"该发哪一个"
只能靠人肉比对。

## 占用者是谁（2026-09-17 实测，**别再猜**）

本机的持有者是 **WorkBuddy 宿主进程**（``WorkBuddy.exe``），**不是安全软件**
（此前文档写作"火绒"是**未经验证的推测**，已按实测更正）。机制：宿主把 ``.asar``
当作"包"去打开，这条通道会**持久留下未带 ``FILE_SHARE_DELETE`` 的句柄**。

控制实验（可复现）：

- 新建 ``*.asar`` → 空闲；**用宿主的读取通道打开一次** → 立刻 ``winerror=32`` 且持续；
  对照读 ``*.txt`` → 仍空闲（⇒ 是 asar 专属通道，不是"读文件就锁"）；
- 范围：**只有 ``*.asar`` 被占**，同目录 ``pbc-server.exe`` / ``BatchSentry.exe`` /
  ``app.asar.unpacked`` 全部空闲 ⇒ 过滤驱动式的"整目录拦截"可以排除；
- 因此**不要用宿主的读取能力去看 ``app.asar``**（例如核验打包版本）。本文件的
  :func:`asar_version` 走子进程 ``open()``，读完即关，**不产生**这个句柄。

⇒ 释放方式：**退出持有进程**（本机＝完全退出 WorkBuddy），句柄随进程消失；
把仓库加进杀毒白名单对本例**无效**（不是杀毒软件）。持有者由 :func:`who_holds`
直接具名，不必再靠 ``tasklist`` 反推。

## 本脚本做什么

1. **体检**：把每个 ``dist*`` 目录分类 —— 后端产物 / Electron 完整 / Electron 残缺；
2. **认锁**：逐文件探测，报出被外部句柄占用而**无法移动**的文件（不是所有文件
   都能删；只有这一小撮会挡住整目录）；
3. **出方案**：保留"最新且完整"的那一个 + ``dist/``（后端产物），其余判为待清理；
4. **清理**：默认 **dry-run**，只打印；``--apply`` 才动手，且走**回收站**
   （可恢复），逐项核对。

## 整目录删不掉时：部分收敛（B9-5，2026-09-21）

宿主长期持有 ``<out>/win-unpacked/resources/app.asar`` ⇒ 整个 Electron 输出目录
**既不可删、也不可原地重建**（``build.ps1`` 自愈到时间戳目录）。此时"什么都不做"
并不安全：该目录里那份**内嵌后端** ``win-unpacked/resources/pbc-server`` 正是
``discover_artifacts`` 认得的**可分发产物**，陈旧时会让门禁**永久变红**
（恒红的门禁与恒真的判据一样零判别力），也可能被误当成要发的那个包。

故新增 ``--converge-locked``：只回收这一个**派生自** :data:`EMBEDDED_SERVER` 的目标
（不另写一份路径规则），且必须先证明**它自己能替换**（只读 DELETE 探针通过；宁缺勿错），
回收后写 ``HUSK.md`` 标出残壳身份。留一个残壳是刻意的取舍 ——
把"这份字节已不存在"变成**能被读到的事实**，而不是"目录还在，所以它大概还在"。

## 硬约束

- 只清理**可再生成**的构建产物，绝不碰源码；
- **不删**最新的完整 Electron 产物，也**不删** ``dist/``（它是 electron-builder
  的 ``extraResources`` 输入，删了下次打包要先重跑 PyInstaller）；
- 认不出的目录只通报、不清理（宁缺勿错）；
- **残壳（husk）= 有 ``BatchSentry.exe`` 却**没有**内嵌后端的 Electron 目录**：
  它跑不起来，绝不参与"最新产物"的比较（否则会保护错对象）；但一个真包都没有时
  仍然什么都不删。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# `scripts/` 非包 ⇒ 按**文件位置**互导（测试用 importlib 从文件路径加载本模块时
# 也能工作）。asar 的**格式解析只允许一处实现**（bundle_manifest.read_asar_*）：
# 两份实现里必然有一份会把「数据基址 = 8 + headerSize」写成「8 + JSON 文本长度」，
# 而那会**差 2 字节并静默读出错误字节**（见该模块 docstring 的十六进制实测）。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from bundle_manifest import read_asar_version  # noqa: E402

# ── 目录分类 ────────────────────────────────────────────────────────

KIND_BACKEND = "backend"      # PyInstaller 产物：dist/pbc-server/
KIND_ELECTRON = "electron"    # electron-builder 产物：<out>/win-unpacked/
KIND_UNKNOWN = "unknown"

# 判定"完整 Electron 产物"的入口：用户双击运行的就是它
ELECTRON_ENTRY = Path("win-unpacked") / "BatchSentry.exe"
# 内嵌后端：真正被 Electron 拉起的那份 pbc-server
EMBEDDED_SERVER = Path("win-unpacked") / "resources" / "pbc-server" / "pbc-server.exe"
# 部分收敛后留在残壳目录里的标记（沿用 PROVENANCE.txt 的"把状态写成可读到的事实"惯例）
HUSK_NAME = "HUSK.md"


def state_of(item: dict) -> str:
    """三态显示名：``完整`` / ``残壳`` / ``残缺``（非 Electron 返回 ``-``）。

    "残壳"独立成一态是 B9-5 的直接产物：有入口却**没有**内嵌后端 ⇒ 双击跑不起来。
    它既不能充当可发布候选，也不能与"文件还没生成齐"的残缺混为一谈 ——
    前者是**已经收过尾的**状态（``HUSK.md`` 会说明），后者是**构建中断**的痕迹。
    """
    if item["kind"] != KIND_ELECTRON:
        return "-"
    if not item.get("complete"):
        return "残缺"
    return "完整" if item.get("has_embedded_server") else "残壳"


def discover(root: Path) -> list[Path]:
    """返回 root 下所有以 ``dist`` 开头的目录（``dist``、``dist-electron``…）。"""
    return sorted((p for p in root.iterdir() if p.is_dir() and p.name.startswith("dist")),
                  key=lambda p: p.name)


def classify(path: Path) -> dict:
    """体检单个目录：类型 / 体积 / 是否完整 / 打包版本 / 生成时间。"""
    files = 0
    size = 0
    for r, _dirs, fs in os.walk(path):
        for f in fs:
            try:
                size += os.path.getsize(os.path.join(r, f))
                files += 1
            except OSError:
                pass
    if (path / "pbc-server").is_dir():
        kind = KIND_BACKEND
    elif (path / "win-unpacked").is_dir():
        kind = KIND_ELECTRON
    else:
        kind = KIND_UNKNOWN

    info = {
        "name": path.name,
        "kind": kind,
        "files": files,
        "bytes": size,
        "mb": round(size / 1048576, 1),
        "complete": (path / ELECTRON_ENTRY).is_file(),
        "has_embedded_server": (path / EMBEDDED_SERVER).is_file(),
        "version": None,
        "mtime": None,
    }
    info["husk"] = state_of(info) == "残壳"
    if kind == KIND_ELECTRON:
        asar = path / "win-unpacked" / "resources" / "app.asar"
        info["version"] = asar_version(asar) if asar.is_file() else None
        try:
            info["mtime"] = (path / ELECTRON_ENTRY).stat().st_mtime
        except OSError:
            pass
    return info


def asar_version(asar: Path) -> str | None:
    """从 app.asar 里读出打包的 `package.json` 版本（失败返回 None，不抛）。

    具体解析**委托** ``bundle_manifest.read_asar_version`` —— 实现只有一处。
    那两处实测坑记在彼处：版本号在 ``package.json`` **成员内容**里而不在头部索引根
    节点上；数据基址是 ``8 + headerSize``（**不能**用 JSON 文本长度推算，会差 2 字节
    并静默错位）。
    """
    return read_asar_version(asar)


# ── 占锁探测（**只读**） ───────────────────────────────────────────
#
# 判据：向系统**请求** DELETE 访问权，但**不执行任何删除**。
#   ``CreateFileW(p, DELETE, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING, ...)``
#   * 成功 ⇒ 本进程拿到了删除权 ⇒ 没有别的句柄以"不含 FILE_SHARE_DELETE"的
#     方式持有它 ⇒ 该**文件**可删/可覆盖（这正是构建需要的性质）。
#   * 失败 ⇒ 报出 winerror：``32`` = SHARING_VIOLATION（他人持有）、
#     ``5`` = ACCESS_DENIED（权限 / 只读属性）。两者都属"不可删"，如实上报。
#   句柄随即关闭：本进程不留持有、不改变对象状态、**不产生任何写操作**。
#
# ⚠️ 为什么淘汰"改名再改回"（2026-09-23 决定，B9-9）：
#   ① 它是**写操作**。门禁判定"陈旧但不可替换"时会调 :func:`locked_files`，
#      于是**检查动作本身**会改仓库目录项；中途被打断（Ctrl-C / 断电 / 被杀）
#      还会把对象留在 ``__lockprobe__.<name>`` 名下。检查不该制造风险。
#   ② 只读探针实测更快：最大的一份产物 937 个文件 0.69 s；而改名探测每次都要
#      过一遍安全软件的过滤驱动，历史实测 6500 次改名 >10 min 仍无结论。
#
# ⚠️ 只读判据**不能**用于**目录** —— 本轮最关键的一处实测（2026-09-23）：
#   目录自身的 DELETE 权限 **≠** 目录可删（NTFS 只在**递归删除时**才检查子项
#   句柄）。实测 ``dist-electron/win-unpacked/resources``：
#     改名探测 ``winerror=5``（拒绝）｜只读探针 ``winerror=0``（放行）
#   若用只读探针做"目录级短路"，会把**内有锁的目录**读成"可删" —— 这是最危险的
#   误判方向（误删；或把不可替换的陈旧产物**误降级**）。
#   ⇒ 只读方案**必须全树遍历、逐文件判定**，不得沿用旧的目录级短路。
#   证据：``devlogs/probe_lock_equivalence.py``（真实目标上两种口径报出的被锁
#   文件集合完全一致）+ ``devlogs/probe_lock_readonly.py``（MISMATCH 现场）。

_DELETE_ACCESS = 0x00010000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_KERNEL32 = None


def _kernel32():
    """惰性取 kernel32（非 Windows 上不应调用；签名只在此处声明一次）。"""
    global _KERNEL32
    if _KERNEL32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def can_delete(path) -> tuple[bool, int]:
    """**只读**判据：``path`` 能否删除/替换。返回 ``(可删, winerror)``。

    "请求权限但不执行删除"是这里的关键 —— 判据要回答的正是"能不能删"，而
    ``CreateFileW`` 的 ``DELETE`` 访问位恰好精确对应：他人若以不含
    ``FILE_SHARE_DELETE`` 的方式持有，请求就会被拒（``winerror=32``），
    这与 ``DeleteFile`` 会失败**同源**。

    非 Windows 一律 ``(True, 0)``：本项目的锁语义（宿主持有 ``.asar``）是
    Windows 特有的。
    """
    if sys.platform != "win32":
        return True, 0
    path = Path(path)
    k32 = _kernel32()
    flags = _FILE_FLAG_BACKUP_SEMANTICS if path.is_dir() else 0
    ctypes.set_last_error(0)
    handle = k32.CreateFileW(str(path), _DELETE_ACCESS, _FILE_SHARE_ALL,
                             None, _OPEN_EXISTING, flags, None)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return False, ctypes.get_last_error()
    k32.CloseHandle(handle)
    return True, 0


def _scan_locked(path: Path, out: list[str], rel: str = "") -> None:
    """遍历子树、**逐文件**只读探测，把不可删者记进 ``out``。

    不做目录级短路 —— 见上方实测（目录探针会**低估**锁定）。
    """
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as e:
        out.append(f"{rel or path.name}  (目录不可读"
                   f" winerror={getattr(e, 'winerror', None)})")
        return
    for ent in entries:
        child = Path(ent.path)
        if ent.is_dir(follow_symlinks=False):
            ok, we = can_delete(child)
            if not ok:
                # 目录自身拿不到删除权：**如实记录**（以 "(" 开头的伪条目，
                # 下游会跳过它、只拿有文件路径的条目去问"谁持有"），但仍
                # **继续下钻** —— 目录权限问题不等于子项不可删，别把
                # "查不到具体文件"读成"没问题"。
                out.append(f"({rel}{ent.name}/ 目录自身不可删 winerror={we}，已继续下钻)")
            _scan_locked(child, out, f"{rel}{ent.name}/")
        else:
            ok, we = can_delete(child)
            if not ok:
                out.append(f"{rel}{ent.name}  (winerror={we})")


def locked_files(path: Path) -> list[str]:
    """返回子树内**不可删除/替换**的文件（=被外部句柄占用），带相对路径。

    **只读**：判据是 :func:`can_delete`（请求 DELETE 权限但不删除），实现是
    :func:`_scan_locked`（全树逐文件）。两者都**不写任何东西** —— 这正是
    B9-9 的目的：把"检查"与"清理"彻底分开，让门禁可以放心调用它。

    条目格式 ``"<相对路径>  (<详情>)"``；以 ``"("`` 开头的条目是"目录自身受阻"
    这类**没有文件路径可查**的伪条目，:func:`named_holders` 会（且必须）跳过 ——
    否则"查不到人"会被读成"有人"。

    ⚠️ 条目必须带**相对路径**：下游要拿它去 Restart Manager 问"谁持有"，而
    "某目录被占"是查不到人的（2026-09-17 实测：正是这一步缺失，导致长期被
    误判成"安全软件"）。
    """
    path = Path(path)
    if not path.exists():
        return []
    if path.is_file():
        ok, we = can_delete(path)
        return [] if ok else [f"{path.name}  (winerror={we})"]
    out: list[str] = []
    ok, we = can_delete(path)
    if not ok:
        out.append(f"({path.name}/ 根目录自身不可删 winerror={we}，已继续下钻)")
    _scan_locked(path, out)
    return out


# ── 持有者具名（Restart Manager，官方接口） ───────────────────────────
#
# 为什么不用"猜"：``tasklist`` 只能告诉你"有哪些进程在跑"，推不出"谁拿着这个句柄"。
# 此前正是靠排除法误判成"安全软件"。Restart Manager（``rstrtmgr.dll``）就是
# 安装程序用来问"谁占着这个文件、要不要帮你关掉"的官方接口，一条调用直接具名。

CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234

RM_APP_TYPE_NAMES = {0: "Unknown", 1: "MainWindow", 2: "OtherWindow", 3: "Service",
                     4: "Explorer", 5: "Console", 1000: "Critical"}


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME)]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [("Process", RM_UNIQUE_PROCESS),
                ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
                ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
                ("ApplicationType", ctypes.c_uint),
                ("AppStatus", wintypes.ULONG),
                ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL)]


def who_holds(path) -> list[dict]:
    """返回正在持有 ``path`` 的进程：``[{"pid": int, "app": str, "type": str}]``。

    失败一律返回 ``[]`` —— 这只是**诊断**辅助，绝不能让清理脚本因它崩掉。
    是否"可删"由 :func:`locked_files` 的**只读探针**决定，与本函数无关。
    """
    if sys.platform != "win32":
        return []
    path = Path(path)
    if not path.exists():
        return []
    try:
        rm = ctypes.WinDLL("rstrtmgr", use_last_error=True)
        rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                      wintypes.WCHAR * (CCH_RM_SESSION_KEY + 1)]
        rm.RmRegisterResources.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(ctypes.c_uint),
                                 ctypes.POINTER(ctypes.c_uint),
                                 ctypes.POINTER(RM_PROCESS_INFO),
                                 ctypes.POINTER(wintypes.DWORD)]
        rm.RmEndSession.argtypes = [wintypes.DWORD]

        sess = wintypes.DWORD(0)
        key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
        if rm.RmStartSession(ctypes.byref(sess), 0, key) != ERROR_SUCCESS:
            return []
        try:
            files = (ctypes.c_wchar_p * 1)(str(path))
            if rm.RmRegisterResources(sess, 1, files, 0, None, 0, None) != ERROR_SUCCESS:
                return []
            need = ctypes.c_uint(0)
            have = ctypes.c_uint(0)
            reasons = wintypes.DWORD(0)
            # 首次调用只为问"要几个"：有占用者返 ERROR_MORE_DATA，无占用者返 SUCCESS
            rc = rm.RmGetList(sess, ctypes.byref(need), ctypes.byref(have), None,
                              ctypes.byref(reasons))
            if rc != ERROR_MORE_DATA or need.value == 0:
                return []
            arr = (RM_PROCESS_INFO * need.value)()
            have = ctypes.c_uint(need.value)
            if rm.RmGetList(sess, ctypes.byref(need), ctypes.byref(have), arr,
                            ctypes.byref(reasons)) != ERROR_SUCCESS:
                return []
            return [{"pid": int(arr[i].Process.dwProcessId),
                     "app": arr[i].strAppName,
                     "type": RM_APP_TYPE_NAMES.get(arr[i].ApplicationType,
                                                   str(arr[i].ApplicationType))}
                    for i in range(have.value)]
        finally:
            rm.RmEndSession(sess)
    except Exception:                      # dll 缺失 / 结构不匹配 / 平台差异
        return []


def describe_holders(path) -> str:
    """把持有者渲染成一行文本；无持有者或查询失败返回空串（供调用方判空）。"""
    try:
        rows = who_holds(path)
    except Exception:
        return ""
    return ", ".join(label_holder(r) for r in rows)


def label_holder(row: dict) -> str:
    """把一条 :func:`who_holds` 记录渲染成稳定标签（**单一渲染实现**）。"""
    return f"{row['app'] or '(未具名)'}(pid={row['pid']}, {row['type']})"


def named_holders(path, locks=None) -> tuple[list[str], list[str]]:
    """返回 ``(holder_labels, lock_entries)`` —— "谁持有"的唯一推导实现。

    门禁（:mod:`release_gate`）与清理工具共用它，**避免两处各推一遍**：
    门禁需要"具名"来决定能否把陈旧产物降级，清理工具需要它来出处置建议。
    只传 ``path`` 时内部会先跑 :func:`locked_files`。

    ⚠️ 三条实测坑（2026-09-17 / 2026-09-21）：

    1. 只能按 ``who_holds`` 的**结构化行**去重，**不能**对渲染文本按 ``", "`` 切分
       —— 条目内部本身就含 ``", "``（``App(pid=1, Unknown)``），切分会把条目截断成
       ``App(pid=1``。
    2. 以 ``"("`` 开头的**伪条目**（如 ``"(<dir>/ 目录自身不可删 winerror=5，
       已继续下钻)"``）没有文件路径可查 ⇒ **跳过**，**不能**把它当成"已具名"
       （那会把"查不到人"读成"有人"）。
    3. 外层的派生症状不能就地具名到**外层目录**：一个目录不可删**未必**是它自己
       被持有（更常见是它的某个子项被持有）⇒ 必须下钻到**文件**才能问出人。
       本实现由 :func:`_scan_locked` 全程遍历保证（**刻意不短路**，见其说明）。
    """
    path = Path(path)
    lock_entries = locked_files(path) if locks is None else list(locks)
    labels: list[str] = []
    for one in lock_entries:
        rel = one.split("(", 1)[0].strip()
        if not rel:                      # 目录级伪条目：没有文件路径可查
            continue
        for r in who_holds(path / rel):
            s = label_holder(r)
            if s not in labels:
                labels.append(s)
    return labels, lock_entries


# ── 方案（纯函数，便于单测） ─────────────────────────────────────────


def plan(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """给出一份体检清单的处置方案。

    返回 ``(keep, doom, review)``：

    - ``keep``：保留。``dist/``（后端产物，打包输入）+ **最新且完整**的
      Electron 产物各一个。
    - ``doom``：可清理。陈旧的完整 Electron 产物 + 所有残缺的 Electron 产物。
    - ``review``：认不出来 / 用不着自动判定的，只通报不动手。

    注意：如果**一个完整的 Electron 产物都没有**，就什么都不判 doom ——
    宁可留着，也不能把唯一的候选删掉。
    """
    keep: list[dict] = []
    doom: list[dict] = []
    review: list[dict] = []

    for it in items:
        if it["kind"] == KIND_BACKEND:
            keep.append(it)
        elif it["kind"] == KIND_UNKNOWN:
            review.append(it)

    electrons = [it for it in items if it["kind"] == KIND_ELECTRON]
    # 只有**完整**（有入口）**且带内嵌后端**的目录才算可发布候选。残壳
    # （B9-5 部分收敛的产物）有入口却跑不起来 ⇒ 必须排除在"最新"比较之外，
    # 否则它会把真正可发的包比下去、被当成要保留的那一个（比误删更坏）。
    candidates = [it for it in electrons if state_of(it) == "完整"]

    if not candidates:
        review.extend(electrons)
        return keep, doom, review

    # 「最新」按内嵌入口 mtime，缺失则退化成体积（完整包明显大于残缺包）
    newest = max(candidates, key=lambda it: (it.get("mtime") or 0, it["bytes"]))
    for it in electrons:
        if it is newest:
            keep.append(it)
        else:
            doom.append(it)
    return keep, doom, review


# ── 部分收敛（B9-5）：整目录删不掉时，回收其中的「可分发陈旧字节」 ──────


def convergence_target(dist_dir: Path) -> Path | None:
    """返回**可回收**的部分收敛目标；``None`` = 不该动。

    目标**派生自** :data:`EMBEDDED_SERVER`（路径规则只有一处），即
    ``<dist_dir>/win-unpacked/resources/pbc-server`` —— 它正是
    :func:`bundle_manifest.discover_artifacts` 认得的"产物"，也是唯一可能
    **被误当成要发的那个包**而流传出去的字节。

    返回 ``None`` 的两种情况（都必须**保持不动**，宁缺勿错）：

    * 目标不存在（目录本来就没有内嵌后端 —— 已经是残壳）；
    * 目标自己**不可替换**（:func:`locked_files` 报出占用项）。

    ⚠️ 判据用 :func:`locked_files`（对**目标子树**逐文件探测）而**不是**外层目录
    能不能整体回收：外层 ``win-unpacked`` 里含被持有的 ``app.asar``（在
    ``resources/`` 下，**不在**目标子树内）⇒ 整目录回收必然失败，而目标
    ``resources/pbc-server`` 的子树里没有任何占用者 ⇒ 完全可以单独回收。
    把"外层不可回收"与"目标不可替换"混为一谈，正是 B9-5 最初卡住的地方。
    """
    tgt = Path(dist_dir) / EMBEDDED_SERVER.parent
    if not tgt.is_dir():
        return None
    if locked_files(tgt):
        return None
    return tgt


def write_husk_note(dist_dir: Path, target: Path, locks: list[str]) -> str:
    """在残壳目录写 ``HUSK.md``，把"这里少了一份可分发产物"变成**读得到的事实**。

    写失败不抛（标记不该把清理带崩），但**必须把失败说出来** —— 否则又会回到
    "目录还在，所以它大概还在"的默认假设里。
    """
    holders, _ = named_holders(dist_dir, locks) if locks else ([], [])
    lock_lines = "\n".join(f"- {one}" for one in locks) or "- (未记录占用项)"
    body = (
        "# 残壳（HUSK）—— 本目录不是可分发产物\n\n"
        "本目录是一次**已过期**的 electron-builder 输出。它的内嵌后端\n"
        f"`{Path(target).relative_to(dist_dir).as_posix()}` 已被 "
        "`scripts/clean_dist.py --converge-locked`\n"
        "回收（送回收站，可恢复）。之所以只回收这一部分：整目录无法删除，占用项为\n\n"
        f"{lock_lines}\n\n"
        f"具名持有者：{'；'.join(holders) or '(未能具名)'}\n\n"
        f"回收时间：{datetime.now().isoformat(timespec='seconds')}\n\n"
        "⇒ **不要再分发本目录**：它缺少后端，双击也跑不起来。\n"
        "   要发版请重建：`powershell -NoProfile -File build.ps1`。\n"
    )
    try:
        (Path(dist_dir) / HUSK_NAME).write_text(body, encoding="utf-8")
        return f"已写 {HUSK_NAME}"
    except OSError as e:
        return f"⚠ 未能写 {HUSK_NAME}（{type(e).__name__}: {e}）—— 请手动标注本目录已失效"


# ── 回收站 ──────────────────────────────────────────────────────────

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 关键：进回收站而不是永久删除
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def to_recycle_bin(path: Path) -> tuple[bool, str]:
    """把目录移入回收站。返回 ``(成功?, 说明)``。

    本机实测：新建目录可用；但只要目录里有一个被占用的文件，整个操作就会以
    ``DE_INVALIDFILES(0x7C)`` 失败 —— 所以必须**先认锁再动手**，否则会连
    "能删的部分"也一起失败。
    """
    if sys.platform != "win32":
        return False, "仅实现 Windows 回收站"
    src = ctypes.create_unicode_buffer(str(path) + "\0\0")
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(src, wintypes.LPCWSTR)
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return False, f"SHFileOperationW rc={rc} (0x{rc & 0xFFFFFFFF:08X})"
    if op.fAnyOperationsAborted:
        return False, "操作被中止 (fAnyOperationsAborted)"
    return True, "已送入回收站"


# ── CLI ────────────────────────────────────────────────────────────


def main(argv=None):
    ap = argparse.ArgumentParser(description="dist* 产物目录体检与安全清理")
    ap.add_argument("--root", default=".", help="仓库根（默认当前目录）")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行清理（默认 dry-run，只打印方案）")
    ap.add_argument("--converge-locked", action="store_true",
                    help="（需与 --apply 同用）对整目录删不掉的待清理目录做**部分收敛**："
                         "只回收其中的内嵌后端，并写 HUSK.md 标出残壳（B9-5）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出体检结果")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    dirs = discover(root)
    items = [classify(d) for d in dirs]

    # 认锁：只对被判定为待清理的目录做（全量探测较慢）
    keep, doom, review = plan(items)
    doom_names = {it["name"] for it in doom}
    locks = {}
    for name in doom_names:
        lk = locked_files(root / name)
        if lk:
            locks[name] = lk

    # 部分收敛目标（B9-5）：只对"整目录删不掉"的待清理目录派生。**纯计算无副作用**，
    # 所以 dry-run 也能如实预告"加 --apply --converge-locked 会发生什么"。
    converge: dict[str, str] = {}
    for name in sorted(set(doom_names) & set(locks)):
        tgt = convergence_target(root / name)
        if tgt is not None:
            converge[name] = tgt.relative_to(root / name).as_posix()

    if args.json:
        print(json.dumps({"items": items, "keep": [i["name"] for i in keep],
                          "doom": [i["name"] for i in doom],
                          "review": [i["name"] for i in review],
                          "locks": locks, "converge": converge},
                         ensure_ascii=False, indent=2))
    else:
        print(f"仓库根: {root}")
        print(f"\n{'目录':<38}{'类型':<10}{'体积':>10}  {'状态':<8}{'版本':<8}")
        print("-" * 84)
        for it in items:
            status = state_of(it)
            ver = it["version"] or "-"
            print(f"{it['name']:<38}{it['kind']:<10}{it['mb']:>8} MB  {status:<8}{ver:<8}")
        print("\n保留:", ", ".join(i["name"] for i in keep) or "(无)")
        print("待清理:", ", ".join(i["name"] for i in doom) or "(无)")
        if review:
            print("需人工确认（先不动）:", ", ".join(i["name"] for i in review))
        for name, lk in locks.items():
            print(f"\n⚠ {name} 内有 {len(lk)} 个文件被外部句柄占用，整目录无法删除：")
            for one in lk[:10]:
                print(f"    {one}")
            # 具名持有者：把"可能是谁"变成"就是谁"（Restart Manager，实测可用）。
            # 推导**只有一处实现**（named_holders）—— 门禁也要用同一份结论来决定
            # 能否把陈旧产物降级，两边各推一遍必然漂移。
            holders, _ = named_holders(root / name, lk)
            if holders:
                print(f"    → 实测持有者：{'; '.join(holders)}（Restart Manager 具名，非推测）")
            else:
                print("    → 未能具名持有者（Restart Manager 查不到 ⇒ 可能是驱动级拦截）")
            if name in converge:
                print(f"    → 可**部分收敛**：只回收 {converge[name]}"
                      f"（并写 {HUSK_NAME} 标出残壳）；"
                      f"加 `--apply --converge-locked` 执行。")
            else:
                print("    → 无可部分收敛的目标（内嵌后端不存在，或它自己也删不掉）。")
            print("    → 处置：**退出持有者进程后重跑本脚本**，句柄随进程消失。")
            print("      注：加杀软白名单对本例**无效**（持有者不是杀软）。")
            print("      判别（2026-09-17 实测；2026-09-23 起改用只读探针判定）：")
            print("      文件级 winerror=32（ERROR_SHARING_VIOLATION，句柄未带")
            print("      FILE_SHARE_DELETE）且**持续**复现；此时读取与写入内容都正常，")
            print("      唯独**删除 / 替换**被拒。")
            print('      已知机制：WorkBuddy 宿主把 .asar 当"包"打开后会持久持有 ——')
            print("      所以**核验 app.asar 请走子进程读取**（见 asar_version），别用宿主读取通道。")
            print("      （它不可用 `--apply` 绕过：SHFileOperationW 会以 DE_INVALIDFILES 整单失败）")

    if args.apply:
        blocked = sorted(set(doom_names) & set(locks))
        actionable = [n for n in doom_names if n not in blocked]
        # `--json` 时进度只能走 stderr —— 混进 stdout 会让输出不再是合法 JSON
        emit = (lambda s: print(s, file=sys.stderr)) if args.json else print
        if blocked:
            emit(f"\n[跳过] {', '.join(blocked)} —— 内含被占用文件，整目录删不掉；"
                 f"先释放占用再试。")
        if args.converge_locked:
            for name in blocked:
                rel = converge.get(name)
                if not rel:
                    emit(f"[converge] {name}: 跳过 — 无可回收目标"
                         f"（内嵌后端不存在，或它自己也删不掉）")
                    continue
                ok, msg = to_recycle_bin(root / name / rel)
                if ok:
                    note = write_husk_note(root / name, root / name / rel, locks[name])
                    emit(f"[converge] {name}: OK — 已回收 {rel}；{note}")
                else:
                    emit(f"[converge] {name}: FAIL — {msg}")
        elif blocked:
            emit(f"         若要部分收敛（只回收内嵌后端 + 写 {HUSK_NAME}），"
                 f"加 --converge-locked；本轮可收敛："
                 f"{', '.join(sorted(converge)) or '(无)'}。")
        if not actionable:
            emit("[apply] 没有可安全清理的目录")
            return 1 if blocked else 0
        for name in actionable:
            ok, msg = to_recycle_bin(root / name)
            emit(f"[apply] {name}: {'OK' if ok else 'FAIL'} — {msg}")
    elif not args.json:
        print("\n(dry-run) 加 --apply 才真正清理；清理走回收站，可恢复。")
        if converge:
            print(f"         整目录删不掉的 {', '.join(sorted(converge))} 可做部分收敛"
                  f"（只回收内嵌后端 + 写 {HUSK_NAME}）：--apply --converge-locked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
