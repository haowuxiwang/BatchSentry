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

## 硬约束

- 只清理**可再生成**的构建产物，绝不碰源码；
- **不删**最新的完整 Electron 产物，也**不删** ``dist/``（它是 electron-builder
  的 ``extraResources`` 输入，删了下次打包要先重跑 PyInstaller）；
- 认不出的目录只通报、不清理（宁缺勿错）。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from ctypes import wintypes
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


# ── 占锁探测 ────────────────────────────────────────────────────────


def _rename_probe(p: Path):
    """尝试把 ``p`` 改名再改回。可改名返回 ``None``，否则返回那个 ``OSError``。

    探针名**刻意不带 ``dist`` 前缀**：本文件自己的 :func:`discover` 用
    ``startswith("dist")`` 找变体目录，若探针叫 ``dist-xxx.lockprobe`` 且
    中途被打断，它会被**误认成一个真实变体**（并把体检结论带偏）。
    """
    probe = p.with_name("__lockprobe__." + p.name)
    try:
        os.rename(p, probe)
    except OSError as e:
        return e
    try:
        os.rename(probe, p)
    except OSError as e:
        # 改回来了却失败：尽力复原，别把对象留在探针名下。
        try:
            os.rename(probe, p)
        except OSError:
            pass
        return e
    return None


def _find_locked(path: Path, out: list[str], rel: str = "") -> None:
    """在 ``path`` 子树内定位被占用者（**按目录递归二分**）。

    NTFS 拒绝重命名含被占用子项的目录 ⇒ 某目录能整体改名，就证明它**整棵子树**
    都没有占用者，可以直接跳过。于是代价从"文件数"降到"目录数 + 被占用的文件数"。

    为什么必须这样（2026-09-17 实测）：逐文件探测在本机待清理目录上约
    6500 次改名，每次都撞安全软件，实测 **>10 分钟仍无结论**；而按目录下钻
    只需数十次探测即可指名到具体文件。
    """
    if _rename_probe(path) is None:
        return
    if not path.is_dir():
        out.append(f"{rel or path.name}  (被外部句柄占用)")
        return
    try:
        children = sorted(path.iterdir())
    except OSError:
        out.append(f"{rel or path.name}  (目录不可读)")
        return
    for c in children:
        if c.is_dir():
            _find_locked(c, out, f"{rel}{c.name}/")
        else:
            e = _rename_probe(c)
            if e is not None:
                out.append(f"{rel}{c.name}  ({type(e).__name__}: errno={e.errno}"
                           f" winerror={getattr(e, 'winerror', None)})")


def locked_files(path: Path) -> list[str]:
    """返回目录内**无法重命名**的文件（=被外部句柄占用）。

    判据用"改名再改回"：Windows 上只要文件被别的进程以不含
    ``FILE_SHARE_DELETE`` 的方式打开，改名就会失败 —— 而这类文件同样会让
    **整个目录**无法删除/改名（NTFS 拒绝重命名含被占用子项的目录）。
    这也是历史构建被迫"自愈"到备用目录的根因。

    ⚠️ **先整目录探一次，失败才下钻**（2026-09-17 实测的性能修复）：
    目录级改名成功即证明内部无占用者，一次 syscall 定案；只有真被占用才
    按目录递归下钻（见 :func:`_find_locked`），只为**指名到具体文件**
    （"加白名单"要的是文件名，不是"某目录被占"）。
    """
    e = _rename_probe(path)
    if e is None:
        return []
    out: list[str] = []
    _find_locked(path, out)
    if not out:
        # 目录级被拒、却没有**任何**单文件/子目录被占用 ⇒ 大概率是安全软件的
        # **目录级**拦截（过滤驱动直接拒绝目录改名），而非"某个文件被打开"。
        # 如实说明，别让"没找到占用者"被读成"可以放心删"。
        out.append(f"(目录级改名被拒 winerror={getattr(e, 'winerror', None)}，"
                   f"但逐项探测未见被占用者 —— 可能是安全软件的目录级拦截)")
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
    是否"可删"由 :func:`locked_files` 的改名探测决定，与本函数无关。
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
    return ", ".join(f"{r['app'] or '(未具名)'}(pid={r['pid']}, {r['type']})" for r in rows)


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
    complete = [it for it in electrons if it["complete"]]
    partial = [it for it in electrons if not it["complete"]]

    if not complete:
        review.extend(electrons)
        return keep, doom, review

    # 「最新」按内嵌入口 mtime，缺失则退化成体积（完整包明显大于残缺包）
    newest = max(complete, key=lambda it: (it.get("mtime") or 0, it["bytes"]))
    for it in electrons:
        if it is newest:
            keep.append(it)
        else:
            doom.append(it)
    return keep, doom, review


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

    if args.json:
        print(json.dumps({"items": items, "keep": [i["name"] for i in keep],
                          "doom": [i["name"] for i in doom],
                          "review": [i["name"] for i in review],
                          "locks": locks}, ensure_ascii=False, indent=2))
    else:
        print(f"仓库根: {root}")
        print(f"\n{'目录':<38}{'类型':<10}{'体积':>10}  {'状态':<8}{'版本':<8}")
        print("-" * 84)
        for it in items:
            if it["kind"] == KIND_ELECTRON:
                status = "完整" if it["complete"] else "残缺"
            else:
                status = "-"
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
            # 具名持有者：把"可能是谁"变成"就是谁"（Restart Manager，实测可用）
            # ⚠️ 只能按 who_holds 的**结构化行**去重，不能对 describe_holders 的
            #    文本按 ", " 切分 —— 条目内部本身就含 ", "（如 "App(pid=1, Unknown)"），
            #    切分会把条目截断成 "App(pid=1"（2026-09-17 实测踩到）。
            holders: list[str] = []
            for one in lk:
                rel = one.split("(", 1)[0].strip()
                if not rel:                  # 目录级伪条目，没有文件路径可查
                    continue
                for r in who_holds(root / name / rel):
                    s = f"{r['app'] or '(未具名)'}(pid={r['pid']}, {r['type']})"
                    if s not in holders:
                        holders.append(s)
            if holders:
                print(f"    → 实测持有者：{'; '.join(holders)}（Restart Manager 具名，非推测）")
            else:
                print("    → 未能具名持有者（Restart Manager 查不到 ⇒ 可能是驱动级拦截）")
            print("    → 处置：**退出持有者进程后重跑本脚本**，句柄随进程消失。")
            print("      注：加杀软白名单对本例**无效**（持有者不是杀软）。")
            print("      判别（2026-09-17 实测）：文件级 winerror=32")
            print("      （ERROR_SHARING_VIOLATION，句柄未带 FILE_SHARE_DELETE）且**持续**复现；")
            print("      此时只读、可写都正常，唯独改名/删除被拒。")
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
        if not actionable:
            emit("[apply] 没有可安全清理的目录")
            return 1 if blocked else 0
        for name in actionable:
            ok, msg = to_recycle_bin(root / name)
            emit(f"[apply] {name}: {'OK' if ok else 'FAIL'} — {msg}")
    elif not args.json:
        print("\n(dry-run) 加 --apply 才真正清理；清理走回收站，可恢复。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
