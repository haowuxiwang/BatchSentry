# -*- coding: utf-8 -*-
"""`tests/e2e_unpacked.py` 的护栏（结构断言，AST 优先）。

为什么需要：这份驱动里**每一条都是踩出来的**，而且"简化"它们不会让驱动报错，
只会让它**静默失去判别力**（假绿）。典型：
  · 删掉 `env.pop("ELECTRON_RUN_AS_NODE")` → 驱动照样"跑完"，但其实整个产物
    以 Node 模式起不来，所有断言都在测空气；
  · 把"等窗口收敛"改成"取第一个可见窗口" → 关的是 splash，仍报 PASS；
  · 把内核判据换成 `tasklist` 文本匹配 → 本地化环境下列错。

故用 AST/结构断言把这几条钉死，而不是靠注释提醒。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
DRIVER = _ROOT / "tests" / "e2e_unpacked.py"


def _src() -> str:
    return DRIVER.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_src())


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"驱动里找不到函数 {name}（改名了？护栏需同步）")


def _called_names(fn: ast.AST) -> set[str]:
    """函数体内**被调用**的名字集合（结构化：不查注释/字符串）。"""
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _source_of(name: str) -> str:
    seg = ast.get_source_segment(_src(), _func(name))
    assert seg, f"取不到 {name} 的源码段"
    return seg


# ── 前置约束 1：必须删除 ELECTRON_RUN_AS_NODE ────────────────────────────
def test_driver_removes_electron_run_as_node():
    """必须真的把该变量从子进程环境里**移除**（`pop`），否则整轮无效。"""
    seg = _source_of("run_once")
    assert "pop(NODE_MODE_VAR" in seg or 'pop("ELECTRON_RUN_AS_NODE"' in seg, (
        "run_once 必须从 env 中删除 ELECTRON_RUN_AS_NODE（宿主会继承给子进程，"
        "否则 Electron 退化为 Node 模式，一切断言都在测空气）"
    )


def test_node_mode_var_constant_is_the_real_name():
    """常量本身也要对：改错了名字等于没删。"""
    consts = {}
    for n in ast.walk(_tree()):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            try:
                consts[n.targets[0].id] = ast.literal_eval(n.value)
            except Exception:  # noqa: BLE001
                pass
    assert consts.get("NODE_MODE_VAR") == "ELECTRON_RUN_AS_NODE"


# ── 前置约束 2：两把隔离钥匙都要有 ────────────────────────────────────────
def test_driver_uses_both_isolation_keys():
    """`--user-data-dir`（Chromium）与 `APPDATA`（后端）缺一不可。"""
    seg = _source_of("run_once")
    assert "--user-data-dir=" in seg, "缺少 Chromium 侧隔离（--user-data-dir）"
    assert 'env["APPDATA"]' in seg, "缺少后端侧隔离（APPDATA 环境变量）"


# ── 前置约束 3：关闭前等窗口收敛（splash 陷阱）────────────────────────────
def test_driver_waits_for_window_settle_before_closing():
    """必须等可见窗口**收敛为 1** 再取主窗口；直接取第一个会关到 splash。"""
    seg = _source_of("run_once")
    assert "== [1, 1, 1]" in seg or "[1, 1, 1]" in seg, (
        "缺少「连续 3 次采样均为 1 个可见窗口」的收敛判据 —— "
        "splash 与主窗口都是可见 Chrome_WidgetWin，直接取第一个会关掉 splash"
    )
    # 必须把 WM_CLOSE 发给**收敛后**取到的窗口
    assert "WM_CLOSE" in seg, "未使用 WM_CLOSE（应模拟用户点 X）"


# ── 前置约束 4：存活判据必须内核级 ───────────────────────────────────────
def test_liveness_uses_kernel_primitive_not_text_parsing():
    """`kernel_alive` 必须走 OpenProcess/GetExitCodeProcess。"""
    names = _called_names(_func("kernel_alive"))
    assert {"OpenProcess", "GetExitCodeProcess"} <= names, (
        "kernel_alive 必须用 OpenProcess + GetExitCodeProcess（文本解析会骗人）"
    )
    seg = _source_of("kernel_alive")
    assert "tasklist" not in seg, "内核判据里不得出现 tasklist 文本解析"


def test_kernel_alive_constant_matches_win32():
    consts = {}
    for n in ast.walk(_tree()):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            try:
                consts[n.targets[0].id] = ast.literal_eval(n.value)
            except Exception:  # noqa: BLE001
                pass
    assert consts.get("_STILL_ACTIVE") == 259
    assert consts.get("WM_CLOSE") == 0x0010


# ── 产物解析：不得写死标准目录 ───────────────────────────────────────────
def test_target_resolution_never_hardcodes_standard_dir():
    """必须支持 glob/最新解析；写死 `dist-electron/` = "测了 A 发了 B"。"""
    seg = _source_of("resolve_unpacked")
    assert "dist-electron*" in seg and "glob" in seg, (
        "resolve_unpacked 必须按 dist-electron* 通配解析最新产物"
    )
    assert "max(" in seg and "st_mtime" in seg, "必须按 mtime 取最新"


# ── D8 诚实性：真实目录被写时不得记 PASS ─────────────────────────────────
def test_d8_never_reports_pass_when_real_dir_changed():
    """隔离判据：有变化只能 `skip`（具名原因），**不得** `ok`。"""
    seg = _source_of("run_once")
    block_start = seg.find("D8_isolation")
    assert block_start >= 0, "找不到 D8 判据"
    block = seg[block_start:block_start + 900]
    assert "skip(" in block, "D8 在真实目录被写时必须记 skip"
    assert "ok(" not in block.split("skip(", 1)[0], (
        "D8 不得在「有变化」分支里调 ok()（会冒充隔离成功）"
    )


# ── D8 的灵敏度：快照必须递归（否则子目录里的写入看不见）────────────────
def test_dir_snapshot_is_recursive():
    """只扫顶层会漏掉 `logs/backend-boot.log` —— 本判据第一版就是这么写的。

    后果是 D8 **恒报"真实目录未被写入"**（假绿）：真正被写的是子目录里的
    bootLog，而它走 `app.getPath('appData')`，恰恰是本轮唯一管不住的那一项。
    """
    seg = _source_of("dir_snapshot")
    assert "rglob" in seg, (
        "dir_snapshot 必须递归（rglob）—— 被写的是子目录里的 logs/backend-boot.log，"
        "只扫顶层会让 D8 恒报「未被写入」"
    )
    assert "st_mtime" in seg, "快照必须纳入 mtime（捕捉「内容追加但长度不变」）"
    assert "iterdir" not in seg, "不得退回顶层扫描（iterdir 只列一级）"


# ── 记账口径：必须复用唯一实现，不得自造状态字符串 ──────────────────────
def test_driver_reuses_coverage_module_statuses():
    """状态只允许来自 tests.e2e_coverage（一处实现）。"""
    imported = set()
    for n in ast.walk(_tree()):
        if isinstance(n, ast.ImportFrom) and n.module == "tests.e2e_coverage":
            imported |= {a.name for a in n.names}
    assert {"STATUS_COVERED", "STATUS_FAILED", "STATUS_SKIPPED"} <= imported, (
        "驱动必须从 tests.e2e_coverage 取状态常量，不得自造"
    )


# ── 收尾纪律：强杀只能在"已确认存活"时发生，且要如实记录 ────────────────
def test_reap_only_kills_when_confirmed_alive():
    """`_reap` 必须在强杀**之前**先确认存活，否则会把"已自行退出"记成"被杀"。

    结构化判据（AST 比行号，不查文案）。⚠️ 只断言"函数里出现过 kernel_alive"
    **不够** —— `_reap` 结尾还有第二次调用（强杀后复核），它会掩盖"强杀前没确认"。
    2026-09-23 变异验证当场抓到这一点（M12 MISSED），故改为**顺序**判据：
      ① `kernel_alive` 最早已调用位置 < taskkill 位置；
      ② `if not alive: return` 早退也在 taskkill 之前。
    """
    fn = _func("_reap")

    def _is_kernel_alive(n: ast.Call) -> bool:
        f = n.func
        return (isinstance(f, ast.Name) and f.id == "kernel_alive") or (
            isinstance(f, ast.Attribute) and f.attr == "kernel_alive")

    def _is_taskkill(n: ast.Call) -> bool:
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "run"):
            return False
        for a in n.args:
            if isinstance(a, (ast.List, ast.Tuple)):
                return any(isinstance(e, ast.Constant) and isinstance(e.value, str)
                           and "taskkill" in e.value for e in a.elts)
        return False

    pre_alive = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and _is_kernel_alive(n)]
    kills = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call) and _is_taskkill(n)]
    assert pre_alive, "_reap 必须调用 kernel_alive（内核级存活判据）"
    assert kills, "_reap 必须保留 taskkill 兜底（否则残留进程没人收）"

    early_return = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp)
                and isinstance(n.test.op, ast.Not)
                and isinstance(n.test.operand, ast.Name)
                and n.test.operand.id == "alive"
                and any(isinstance(s, ast.Return) for s in n.body)):
            early_return.append(n.lineno)
    assert min(pre_alive) < min(kills), (
        "kernel_alive 必须在 taskkill **之前**调用 —— 否则「已自行退出」的进程会被"
        "记成「被杀」（归因污染；结尾那次复核调用不能算数）"
    )
    assert early_return and min(early_return) < min(kills), (
        "_reap 必须用「if not alive: return」在强杀前早退，且早于 taskkill"
    )
    assert "rec[" in _source_of("_reap"), "_reap 必须把结论写进记录（不得静默）"


@pytest.mark.parametrize("name", ["run_once", "resolve_unpacked", "kernel_alive",
                                  "main", "dir_snapshot"])
def test_driver_functions_present(name):
    """防"顺手改名"：护栏与被护对象必须同名，否则整份护栏静默失效。"""
    _func(name)
