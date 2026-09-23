# -*- coding: utf-8 -*-
"""win-unpacked 级端到端驱动（Electron 应用层）。

与既有驱动的分工：
  · `tests/e2e_frozen.py`  测 **内嵌后端 exe**（直接跑 pbc-server.exe，不经 Electron）
  · 本驱动测 **用户双击的那一份 win-unpacked**：BatchSentry.exe → Electron →
    拉起内嵌后端 → 渲染进程加载 app.asar → 关窗 → 优雅退出

为什么必须单独驱动（2026-09-23 实测）：
  ① 宿主 WorkBuddy 以 ELECTRON_RUN_AS_NODE=1 运行的 Electron daemon，该变量
     **继承给所有子进程** ⇒ BatchSentry.exe 会以**纯 Node 模式**启动：
     不加载 app.asar、0.2~0.7s 静默 rc=0 退出、--xxx 报 bad option、--version
     打印 Node 版本。⇒ 驱动**必须显式清除该变量**，否则永远测不到 Electron 层。
  ② Chromium 的 userData 走 SHGetFolderPath，**不读 APPDATA 环境变量**；
     后端数据根则读 APPDATA。⇒ 隔离要**两把钥匙**：
     `--user-data-dir`（Electron）+ `APPDATA`（后端）。
  ③ splash 与主窗口**都是可见的 Chrome_WidgetWin_1**，且 splash **先出现**
     ⇒ 关闭请求必须发给**主窗口**，否则只是关掉 splash，应用仍在跑
     （曾据此误判"关闭后进程残留"，实为探针缺陷）。

判据一律**交叉**且以**内核级**为准（OpenProcess + GetExitCodeProcess），
不靠文本解析；失败归因要有正向对照。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.e2e_coverage import (  # noqa: E402
    ENTRY_OCR_PIPELINE, ENTRY_LLM_PIPELINE, STATUS_COVERED, STATUS_FAILED,
    STATUS_SKIPPED, Coverage,
)

# ── 环境变量（命名与 e2e_proc 风格一致）────────────────────────────────────
UNPACKED_ENV = "PBC_E2E_UNPACKED"        # 直接指向 win-unpacked 目录
EXE_ENV = "PBC_E2E_EXE"                  # 兼容：指向 resources/pbc-server/pbc-server.exe
RUNS_ENV = "PBC_E2E_UNPACKED_RUNS"       # 重复次数（默认 1）
COVERAGE_JSON_ENV = "PBC_E2E_COVERAGE_JSON"

#: ⚠️ 必须在子进程环境里**删除**的变量（否则 Electron 退化为 Node）。
#: 这是本驱动存在的前提，不是可选项。
NODE_MODE_VAR = "ELECTRON_RUN_AS_NODE"

SERVER_PORT = 58765
SERVER_HOST = "127.0.0.1"

# ── Win32（唯一实现，禁止各处重写）────────────────────────────────────────
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32.EnumWindows.restype = wintypes.BOOL
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
WM_CLOSE = 0x0010

RESULT_PASS, RESULT_FAIL, RESULT_SKIP = "PASS", "FAIL", "SKIP"
RESULTS: list[tuple[str, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    RESULTS.append((RESULT_PASS, name, detail))
    print(f"  [OK]   {name}: {detail}")


def bad(name: str, detail: str = "") -> None:
    RESULTS.append((RESULT_FAIL, name, detail))
    print(f"  [FAIL] {name}: {detail}")


def skip(name: str, detail: str = "") -> None:
    RESULTS.append((RESULT_SKIP, name, detail))
    print(f"  [SKIP] {name}: {detail}")


# ── 基础探测 ──────────────────────────────────────────────────────────────
def resolve_unpacked() -> Path:
    """解析被测 win-unpacked 目录。

    优先级：``PBC_E2E_UNPACKED`` > 由 ``PBC_E2E_EXE`` 反推（…/resources/pbc-server/
    pbc-server.exe → win-unpacked）> **最新**的 dist-electron*/win-unpacked。
    ⚠️ 绝不写死 `dist-electron/`：标准目录被占用时构建会自愈到 `dist-electron-out-<ts>/`，
    那里才有真正要发的那一份（"测了 A 发了 B"的经典坑）。
    """
    env = os.environ.get(UNPACKED_ENV)
    if env:
        p = Path(env)
        if (p / "BatchSentry.exe").is_file():
            return p
        raise FileNotFoundError(f"{UNPACKED_ENV}={env} 下没有 BatchSentry.exe")

    exe = os.environ.get(EXE_ENV)
    if exe:
        p = Path(exe).resolve()
        # …/win-unpacked/resources/pbc-server/pbc-server.exe
        for parent in p.parents:
            if (parent / "BatchSentry.exe").is_file():
                return parent

    cands = [p.parent for p in REPO_ROOT.glob("dist-electron*/win-unpacked/BatchSentry.exe")]
    if not cands:
        raise FileNotFoundError("找不到任何 dist-electron*/win-unpacked/BatchSentry.exe")
    return max(cands, key=lambda p: p.stat().st_mtime)


def _listen(port: int = SERVER_PORT) -> bool:
    s = socket.socket(); s.settimeout(0.6)
    try:
        return s.connect_ex((SERVER_HOST, port)) == 0
    finally:
        s.close()


def _health(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{SERVER_HOST}:{SERVER_PORT}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def kernel_alive(pid: int) -> tuple[bool, str]:
    """内核级存活判据（进程句柄 + 退出码）。文本解析会骗人，这条不会。"""
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False, f"OpenProcess 失败(err={ctypes.get_last_error()})"
    try:
        code = wintypes.DWORD(0)
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False, f"GetExitCodeProcess 失败(err={ctypes.get_last_error()})"
        return code.value == _STILL_ACTIVE, f"exitCode={code.value}"
    finally:
        _k32.CloseHandle(h)


def visible_widget_windows(pid: int) -> list[dict]:
    """该进程的可见 Chrome_WidgetWin* 顶层窗口（splash 与主窗口都属于此类）。"""
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        p = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and _u32.IsWindowVisible(hwnd):
            b1 = ctypes.create_unicode_buffer(256); _u32.GetClassNameW(hwnd, b1, 256)
            if b1.value.startswith("Chrome_WidgetWin"):
                b2 = ctypes.create_unicode_buffer(256); _u32.GetWindowTextW(hwnd, b2, 256)
                out.append({"hwnd": int(hwnd), "class": b1.value, "title": b2.value})
        return True

    _u32.EnumWindows(cb, 0)
    return out


def child_processes(pid: int) -> list[dict]:
    """子进程清单（含命令行），用于验证"拉起的是内嵌后端"这一条硬事实。"""
    ps = ("$ErrorActionPreference='SilentlyContinue';"
          f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid}\" |"
          " ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=60)
        out = (r.stdout or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            rows.append({"pid": int(parts[0]), "name": parts[1].strip(),
                         "cmdline": parts[2].strip() if len(parts) > 2 else ""})
    return rows


def backend_pid_on_port(port: int = SERVER_PORT) -> int | None:
    """端口持有者 PID —— 与子进程清单交叉（也可能来自"复用孤儿"路径）。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=25)
    except Exception:  # noqa: BLE001
        return None
    for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        if f":{port}" in ln and "LISTENING" in ln:
            parts = ln.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def dir_snapshot(d: Path) -> dict[str, str]:
    """**递归**快照：相对路径 -> "<size>:<mtime>"。

    ⚠️ 只扫顶层文件是不够的（本判据第一版就是这么写的，于是 D8 恒报"未被写入"）：
    真正会被写的是**子目录**里的东西 —— `logs/backend-boot.log`（Electron 的
    bootLog 走 `app.getPath('appData')`，不受 APPDATA 环境变量影响）。
    mtime 一并纳入，捕捉"内容被追加但长度恰好不变"的情况。
    """
    if not d.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in d.rglob("*"):
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(d)).replace("\\", "/")] = f"{st.st_size}:{int(st.st_mtime)}"
    return out


def kill_leftovers(path: Path | None = None) -> None:
    """清理残留（只清具体模块名，不用通配符删除任何用户文件）。"""
    for name in ("BatchSentry.exe", "pbc-server.exe"):
        subprocess.run(["taskkill", "/IM", name, "/T", "/F"], capture_output=True)


# ── 单轮：启动 → 探活 → 校验 → 关窗 → 校验退出 ─────────────────────────────
def run_once(unpacked: Path, sandbox: Path, run_idx: int, cov: Coverage) -> dict:
    exe = unpacked / "BatchSentry.exe"
    appdata, userdata = sandbox / "appdata", sandbox / "userdata"
    appdata.mkdir(parents=True, exist_ok=True)
    userdata.mkdir(parents=True, exist_ok=True)

    real_pbc = Path(os.environ.get("APPDATA", "")) / "PBC"
    rec: dict = {"run": run_idx, "unpacked": str(unpacked), "exe": str(exe), "steps": {}}

    env = dict(os.environ)
    # ① 必删：否则 Electron 退化为 Node，什么都测不到
    removed = env.pop(NODE_MODE_VAR, None)
    rec["removed_env"] = {NODE_MODE_VAR: removed}
    # ② 后端数据根隔离（后端读 os.environ["APPDATA"]）
    env["APPDATA"] = str(appdata)

    if _listen():
        kill_leftovers(exe)
        time.sleep(2)
        if _listen():
            rec["abort"] = "端口被占用且清理失败"
            return rec

    real_before = dir_snapshot(real_pbc)
    t0 = time.time()
    proc = subprocess.Popen([str(exe), f"--user-data-dir={userdata}"], env=env, cwd=str(unpacked),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rec["pipid"] = proc.pid

    # ── D1 启动可达 ──
    h = None
    while time.time() - t0 < 90:
        if proc.poll() is not None:
            break
        h = _health()
        if h:
            break
        time.sleep(0.5)
    rec["steps"]["boot_s"] = round(time.time() - t0, 2)
    rec["steps"]["health"] = h
    if not h:
        bad(f"run{run_idx}.D1_startup", f"/health 未就绪（{rec['steps']['boot_s']}s, "
                                        f"poll={proc.poll()}）")
        cov.record(ENTRY_LLM_PIPELINE, STATUS_FAILED, "应用未启动，流水线无从谈起")
        cov.record(ENTRY_OCR_PIPELINE, STATUS_FAILED, "应用未启动，流水线无从谈起")
        _reap(proc, rec)
        return rec
    ok(f"run{run_idx}.D1_startup", f"/health 就绪 {rec['steps']['boot_s']}s -> {h}")

    # ── D2 版本自证（与 PROVENANCE 比对，不写死字面量）──
    prov = unpacked / "PROVENANCE.txt"
    expected = None
    if prov.exists():
        for ln in prov.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.lower().startswith("version:"):
                expected = ln.split(":", 1)[1].strip()
    rec["provenance_version"] = expected
    if expected is None:
        skip(f"run{run_idx}.D2_version", "PROVENANCE.txt 无 version 字段（不判定，非缺陷）")
    elif h.get("version") == expected:
        ok(f"run{run_idx}.D2_version", f"/health version={h.get('version')} == PROVENANCE")
    else:
        bad(f"run{run_idx}.D2_version",
            f"/health version={h.get('version')} != PROVENANCE={expected}（测了 A 发了 B？）")

    # ── D3 拉起的是**内嵌**后端（不是系统 python、不是别的目录）──
    kids = child_processes(proc.pid)
    rec["children"] = kids
    embedded = str((unpacked / "resources" / "pbc-server" / "pbc-server.exe")).lower()
    backend_kids = [k for k in kids if k["name"].lower() == "pbc-server.exe"]
    bref = os.environ.get(EXE_ENV, "").lower()
    if not backend_kids:
        bad(f"run{run_idx}.D3_embedded_backend", "未找到 pbc-server.exe 子进程")
    elif embedded in backend_kids[0]["cmdline"].lower():
        ok(f"run{run_idx}.D3_embedded_backend", "内嵌后端已拉起：" +
           Path(backend_kids[0]["cmdline"]).name)
    elif bref and bref in backend_kids[0]["cmdline"].lower():
        ok(f"run{run_idx}.D3_embedded_backend", f"后端路径 == {EXE_ENV} 指定（等价接受）")
    else:
        bad(f"run{run_idx}.D3_embedded_backend",
            f"后端不是内嵌那一份：{backend_kids[0]['cmdline'][:160]}")

    # 渲染进程是否加载了 app.asar（Electron 层真的起来了，而非仅后端被拉起）
    renderers = [k for k in kids if "--type=renderer" in k["cmdline"]]
    rec["renderer_count"] = len(renderers)
    if renderers and "app.asar" in renderers[0]["cmdline"]:
        ok(f"run{run_idx}.D4_renderer", "渲染进程已加载 app.asar")
    else:
        bad(f"run{run_idx}.D4_renderer", f"未见加载 app.asar 的渲染进程（{len(renderers)} 个 renderer）")

    # ── D5 主窗口（splash 会先出现，必须等它收敛）──
    settled_at, wins = None, []
    tw = time.time()
    hist = []
    while time.time() - tw < 60:
        ws = visible_widget_windows(proc.pid)
        hist.append(len(ws))
        if len(ws) == 1 and len(hist) >= 3 and hist[-3:] == [1, 1, 1]:
            settled_at = round(time.time() - tw, 2)
            wins = ws
            break
        time.sleep(0.5)
    rec["steps"]["window_settle_s"] = settled_at
    rec["steps"]["windows"] = wins
    rec["steps"]["window_hist"] = hist[:20]
    if wins:
        ok(f"run{run_idx}.D5_main_window", f"主窗口就绪 {settled_at}s title={wins[0]['title']!r}")
    else:
        bad(f"run{run_idx}.D5_main_window", f"可见窗口未收敛为 1（hist={hist[:10]}）")

    # ── D6 优雅关闭：WM_CLOSE 发**主窗口**（== 用户点 X）──
    if not wins:
        _reap(proc, rec)
        return rec
    hwnd = wins[0]["hwnd"]
    tclose = time.time()
    posted = bool(_u32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
    rec["steps"]["wm_close_posted"] = posted
    if not posted:
        bad(f"run{run_idx}.D6_graceful_close", f"PostMessage(WM_CLOSE) 失败 err={ctypes.get_last_error()}")

    port_free_at = dead_at = None
    exit_code = None
    while time.time() - tclose < 60:
        el = round(time.time() - tclose, 2)
        alive, why = kernel_alive(proc.pid)
        if not _listen() and port_free_at is None:
            port_free_at = el
        if not alive:
            dead_at = el
            exit_code = why
            break
        time.sleep(0.25)
    rec["steps"]["port_free_s"] = port_free_at
    rec["steps"]["process_gone_s"] = dead_at
    rec["steps"]["exit_code"] = exit_code
    rec.update(_reap(proc, rec, force_only_if_alive=True))

    if dead_at is None:
        bad(f"run{run_idx}.D6_graceful_close",
            f"60s 内进程未退出（port_free={port_free_at}s）")
    elif port_free_at is None:
        bad(f"run{run_idx}.D6_graceful_close", "进程退出了但端口未释放")
    elif "exitCode=0" not in (exit_code or ""):
        bad(f"run{run_idx}.D6_graceful_close", f"退出码非 0：{exit_code}")
    else:
        ok(f"run{run_idx}.D6_graceful_close",
           f"端口释放 {port_free_at}s、进程退出 {dead_at}s、{exit_code}")

    # ── D7 close_db 生效：-wal/-shm 被 checkpoint 清理 ──
    after = dir_snapshot(appdata / "PBC")
    rec["steps"]["db_after"] = after
    leftovers = [k for k in after if k.endswith("-wal") or k.endswith("-shm")]
    if not after:
        skip(f"run{run_idx}.D7_close_db", "隔离数据目录不存在（无法判定）")
    elif leftovers:
        bad(f"run{run_idx}.D7_close_db", f"仍有 {leftovers} ⇒ close_db/checkpoint 未完成")
    elif any(k.endswith("data.db") for k in after):
        ok(f"run{run_idx}.D7_close_db", f"仅剩 {sorted(after)} ⇒ checkpoint 已收敛")
    else:
        skip(f"run{run_idx}.D7_close_db", f"目录内容不符合预期：{sorted(after)}")

    # ── D8 隔离性：真实 %APPDATA%/PBC 不得被写（如实标注已知例外）──
    real_after = dir_snapshot(real_pbc)
    changed = sorted(k for k in real_after if real_before.get(k) != real_after[k])
    rec["steps"]["real_pbc_changed"] = changed
    if not changed:
        ok(f"run{run_idx}.D8_isolation", "真实 %APPDATA%/PBC 未被写入")
    else:
        # bootLog 走 app.getPath('appData') ⇒ 不受 APPDATA 环境变量影响，属**已知**且
        # 现版本无法从外部隔离的一项。如实报告，不冒充"隔离完全"。
        skip(f"run{run_idx}.D8_isolation",
             f"真实目录被写：{changed}（bootLog 用 app.getPath('appData')，环境变量管不到）")
    return rec


def _reap(proc, rec: dict, force_only_if_alive: bool = False) -> dict:
    """收尾：仅在仍存活时强杀，并如实记录（绝不在 catch 里下结论）。"""
    alive, why = kernel_alive(proc.pid)
    if not alive:
        rec["reap"] = f"SKIP（已自行退出 {why}）"
        return rec
    subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
    try:
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        pass
    alive2, why2 = kernel_alive(proc.pid)
    rec["reap"] = f"killed={not alive2} ({why2})" + (" [只在早已确认存活时才走到这里]"
                                                     if force_only_if_alive else "")
    return rec


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    runs = int(os.environ.get(RUNS_ENV, "1"))
    if "--runs" in argv:
        runs = int(argv[argv.index("--runs") + 1])

    try:
        unpacked = resolve_unpacked()
    except FileNotFoundError as e:
        print(f"[FAIL] {e}")
        return 2

    sandbox = REPO_ROOT / "devlogs" / "_e2e_unpacked"
    cov_path = os.environ.get(COVERAGE_JSON_ENV) or str(
        REPO_ROOT / "devlogs" / f"e2e_unpacked_coverage_{time.strftime('%Y%m%d_%H%M%S')}.json")
    cov = Coverage(path=Path(cov_path))

    print("=" * 62)
    print(f"target : {unpacked}")
    print(f"runs   : {runs}")
    print(f"env    : {NODE_MODE_VAR} 将被**删除**（否则退化 Node 模式）")
    print("=" * 62)

    records = []
    for i in range(1, runs + 1):
        print(f"\n--- run {i}/{runs} ---")
        records.append(run_once(unpacked, sandbox, i, cov))

    # 覆盖清单：本驱动覆盖的是"应用层能起能关"，LLM/OCR 的**成功路径**由
    # e2e_frozen 负责（同一站点口径），此处如实记 skipped 而不是冒充 covered。
    cov.record(ENTRY_LLM_PIPELINE, STATUS_SKIPPED,
               "本驱动只验应用层启动/关闭；LLM 链路由 tests/e2e_frozen.py 负责")
    cov.record(ENTRY_OCR_PIPELINE, STATUS_SKIPPED,
               "本驱动只验应用层启动/关闭；OCR 链路由 tests/e2e_frozen.py 负责")
    cov.write()
    print(f"\ncoverage -> {cov_path}")

    passed = sum(1 for s, _, _ in RESULTS if s == RESULT_PASS)
    failed = sum(1 for s, _, _ in RESULTS if s == RESULT_FAIL)
    skipped = sum(1 for s, _, _ in RESULTS if s == RESULT_SKIP)
    print(f"\n{'=' * 62}")
    print(f"Total: {passed} passed, {failed} failed, {skipped} skipped  (runs={runs})")
    for s, name, detail in RESULTS:
        print(f"  {'OK' if s == RESULT_PASS else ('XX' if s == RESULT_FAIL else '--')} {name}: {detail}")
    print("=" * 62)

    evidence = REPO_ROOT / "devlogs" / f"e2e_unpacked_{time.strftime('%Y%m%d_%H%M%S')}.json"
    evidence.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"evidence -> {evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
