"""dist 产物目录体检 + 安全清理（单一入口）。

## 为什么需要它

electron-builder 的输出目录名会**漂移**：安全软件（本机为火绒）占锁
``resources/app.asar`` 时，``build.ps1`` 会自愈到备用目录
``dist-electron-locked``；手工重打包又可能显式指定 ``dist-electron-v112``。
日积月累，仓库里就堆着四五个 ``dist*`` 目录，"该发哪一个"只能靠人肉比对。

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
import struct
import sys
from ctypes import wintypes
from pathlib import Path

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

    注意两点（都是实测踩过的）：
    - 版本号在 asar 的 ``package.json`` **成员内容**里，不在头部索引的根节点上 ——
      直接 ``obj.get("version")`` 恒为 None；
    - 数据基址是 ``8 + data_size``，**不能**用"JSON 文本长度"推算：索引后有 2 字节
      对齐填充，会差 2 字节并静默错位。
    - 只读头部并 seek 到成员，别把 380MB 整个读成字符串。
    """
    try:
        with asar.open("rb") as fh:
            head = fh.read(1 << 20)
            json_start = head.index(b'{"files":')
            data_size = struct.unpack("<I", head[4:8])[0]
            obj, _ = json.JSONDecoder().raw_decode(
                head[json_start:json_start + data_size].decode("utf-8", "replace"))
            node = obj["files"]["package.json"]
            fh.seek(8 + data_size + int(node["offset"]))
            blob = fh.read(int(node["size"]))
        return json.loads(blob.decode("utf-8", "replace")).get("version")
    except (OSError, ValueError, KeyError, UnicodeDecodeError):
        return None


# ── 占锁探测 ────────────────────────────────────────────────────────


def locked_files(path: Path) -> list[str]:
    """返回目录内**无法重命名**的文件（=被外部句柄占用）。

    判据用"改名再改回"：Windows 上只要文件被别的进程以不含
    ``FILE_SHARE_DELETE`` 的方式打开，改名就会失败 —— 而这类文件同样会让
    **整个目录**无法删除/改名（NTFS 拒绝重命名含被占用子项的目录）。
    这也是历史构建被迫"自愈"到备用目录的根因。
    """
    out = []
    for r, _dirs, fs in os.walk(path):
        for f in fs:
            p = os.path.join(r, f)
            try:
                os.rename(p, p + ".lockprobe")
                os.rename(p + ".lockprobe", p)
            except OSError as e:
                out.append(f"{os.path.relpath(p, path)}  ({type(e).__name__}: errno={e.errno})")
    return out


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
        print(f"\n{'目录':<26}{'类型':<10}{'体积':>10}  {'状态':<8}{'版本':<8}")
        print("-" * 72)
        for it in items:
            if it["kind"] == KIND_ELECTRON:
                status = "完整" if it["complete"] else "残缺"
            else:
                status = "-"
            ver = it["version"] or "-"
            print(f"{it['name']:<26}{it['kind']:<10}{it['mb']:>8} MB  {status:<8}{ver:<8}")
        print("\n保留:", ", ".join(i["name"] for i in keep) or "(无)")
        print("待清理:", ", ".join(i["name"] for i in doom) or "(无)")
        if review:
            print("需人工确认（先不动）:", ", ".join(i["name"] for i in review))
        for name, lk in locks.items():
            print(f"\n⚠ {name} 内有 {len(lk)} 个文件被外部句柄占用，整目录无法删除：")
            for one in lk[:10]:
                print(f"    {one}")
            print("    → 常见原因：杀毒/安全软件（如火绒）持有 resources/app.asar。")
            print("      处理：把本仓库目录加入其信任区/白名单，或临时退出后重跑本脚本。")

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
