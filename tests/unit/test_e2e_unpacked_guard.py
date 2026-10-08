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


# ── 启动失败必须留下 stderr（2026-09-30：DEVNULL 把真因吞了）─────────────
def _popen_kwargs() -> dict:
    """`run_once` 里 `subprocess.Popen(...)` 的关键字实参（结构化取，不查文案）。"""
    for n in ast.walk(_func("run_once")):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "Popen"):
            return {kw.arg: kw.value for kw in n.keywords}
    raise AssertionError("run_once 里找不到 subprocess.Popen（结构变了？）")


def _is_devnull(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "DEVNULL")


def test_popen_does_not_discard_stderr():
    """**不得**再把子进程 stdout/stderr 丢进 DEVNULL。

    为什么：2026-09-30 排查 `BatchSentry.exe` 以 `0x80000003` 退出时，
    Electron 自己的报错（`FATAL: GPU process isn't usable.`）**全被 DEVNULL
    吞掉**，只剩一个退出码，真因无从查起。改用日志文件后当场定位。
    判据用 AST（看真实的实参节点），不看注释 —— 本文件注释里就写着 DEVNULL。
    """
    kw = _popen_kwargs()
    for stream in ("stdout", "stderr"):
        assert stream in kw, f"Popen 必须显式给 {stream}（默认继承，会污染报告）"
        assert not _is_devnull(kw[stream]), (
            f"Popen 的 {stream} 仍是 DEVNULL ⇒ 启动失败时真因被吞，只剩退出码")


def test_devnull_detector_is_not_vacuous():
    """阳性对照：证明上面那条判据**真的会报**，而不是恒绿。"""
    bad = ast.parse("subprocess.Popen([x], stdout=subprocess.DEVNULL)").body[0].value
    assert _is_devnull(bad.keywords[0].value), "DEVNULL 探测对 DEVNULL 零反应"
    good = ast.parse("subprocess.Popen([x], stdout=fo)").body[0].value
    assert not _is_devnull(good.keywords[0].value), "误报普通变量"


def test_startup_failure_is_attributed_not_just_reported():
    """D1 失败必须调用归因函数（只丢退出码 = 把排查成本转嫁给读者）。"""
    assert "_startup_failure_attribution" in _called_names(_func("run_once")), (
        "run_once 未调用 _startup_failure_attribution")
    assert "_startup_failure_attribution" in {n.name for n in ast.walk(_tree())
                                              if isinstance(n, ast.FunctionDef)}, (
        "归因函数不存在（被删了？）")


def test_attribution_reads_the_stderr_log():
    """归因必须**读证据**（stderr 日志），不得凭退出码猜。

    ⚠️ 2026-10-08：读取被抽成 `_stderr_text()`（D3 的归因复用同一份证据）。
    判据随之改为**两段**，比原先的单点 `read_text` 子串**更强**：
      ① 归因函数必须**调用** `_stderr_text`（AST 调用名，注释/字符串干扰不了）；
      ② `_stderr_text` 自己必须真的读文件（`read_text`）。
    只查 `_startup_failure_attribution` 里有没有 `read_text`，会在抽取后**假红** ——
    那正是「把纯重构当缺陷」的噪声（同 e2e_frozen 13b 的教训）。
    """
    assert "_stderr_text" in _called_names(_func("_startup_failure_attribution")), (
        "归因未调用 _stderr_text ⇒ 可能退回凭退出码猜")
    assert "read_text" in _source_of("_stderr_text"), "_stderr_text 没有真的读日志"
    assert "_GPU_FATAL" in _source_of("_startup_failure_attribution"), \
        "归因未使用 GPU 致命行常量"


def test_stderr_log_guard_is_not_vacuous():
    """防空转：锚点必须真的在函数体里 —— 换成内联读取后不得再命中。"""
    seg = _source_of("_startup_failure_attribution")
    mutated = seg.replace("_stderr_text(err_path)",
                          'err_path.read_text(encoding="utf-8")')
    assert mutated != seg, "锚点 `_stderr_text(err_path)` 不在源码里 ⇒ 判据空转"
    assert "_stderr_text" not in mutated, "替换后仍命中 ⇒ 判据空转"


def test_gpu_attribution_does_not_prescribe_disable_gpu_as_the_fix():
    """🔴 **不得**把 `--disable-gpu` 说成解法 —— 它已被 2×2 对照否证。

    2026-09-30 实测（同一产物、同一开关矩阵）：
      不加 + 非沙箱 → 3.05s 就绪；加 + 非沙箱 → 1.67s 就绪；
      加 + **沙箱内** → 崩 0x80000003；不加 + **沙箱内** → 崩 0x80000003。
    ⇒ 唯一自变量是"是否运行在受限沙箱里"，开关与结果无关。曾据此误判为
    "GPU 开关问题"并写进提示文案，被否证后撤回。本护栏防止它再被写回去。
    """
    seg = _source_of("_startup_failure_attribution")
    assert "沙箱" in seg, "归因文案必须点明真正的自变量（受限沙箱）"
    # 反面：不得出现"设 ARGS_ENV=--disable-gpu 后复跑"这类处方
    assert "设 {ARGS_ENV}=--disable-gpu" not in seg, (
        "归因又把 --disable-gpu 当解法了（已被 2×2 否证）")
    assert "PBC_E2E_UNPACKED_ARGS=--disable-gpu" not in seg, (
        "归因又把 --disable-gpu 当解法了（已被 2×2 否证）")


def test_extra_app_args_reads_the_documented_env_var():
    """逃生口必须从常量读，不得写死变量名（改名后静默失效）。

    ⚠️ 锚点必须落在**调用点** ``os.environ.get(ARGS_ENV)``，不能只查裸标识符
    ``ARGS_ENV`` —— ``ast.get_source_segment`` **会把 docstring 一起返回**，
    而本函数的 docstring 里就写着 ``:data:`ARGS_ENV```。只查子串时，把调用点改成
    字面量后断言**照样成立**（变异验证实测 MISSED，本文件已第三次踩这个坑）。
    """
    seg = _source_of("_extra_app_args")
    assert "os.environ.get(ARGS_ENV)" in seg, (
        "_extra_app_args 必须用常量 ARGS_ENV 取环境变量（写死名字会在改名后静默失效）")
    consts = {}
    for n in ast.walk(_tree()):
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            try:
                consts[n.targets[0].id] = ast.literal_eval(n.value)
            except Exception:  # noqa: BLE001
                pass
    assert consts.get("ARGS_ENV") == "PBC_E2E_UNPACKED_ARGS"


def test_args_env_guard_is_anchored_on_the_call_site():
    """防空转：把调用点换成字面量后，锚点必须**不再命中**。"""
    anchor = "os.environ.get(ARGS_ENV)"
    seg = _source_of("_extra_app_args")
    assert anchor in seg
    mutated = seg.replace(anchor, 'os.environ.get("PBC_E2E_UNPACKED_ARGS")')
    assert mutated != seg, "锚点未命中源码"
    assert anchor not in mutated, "锚点其实落在 docstring 上 ⇒ 护栏空转"


# ── D3 归因：判不了时必须说清「为什么判不了」（判据强度不变，仍是 FAIL）──────
# 2026-10-08 实测的矛盾：同一轮里 `/health` 已应答 `{status: ok, version: 1.2.1}`、
# 产品自身 stdout 也写着拉起内嵌后端，而 D3 的子进程枚举**什么都没看到**
# ⇒ 只报「没找到」会把排查引向一个并不存在的拉起缺陷（错误归因）。
# 这与 `llm_failure_attribution` / `_startup_failure_attribution` 要治的是同一个病。

_DRIVER_MOD = None


def _driver():
    """按路径加载驱动模块（`tests/e2e_unpacked.py` 不匹配 pytest 的收集规则）。

    只用于**行为**断言；结构断言一律走 AST，免得把驱动跑起来。
    """
    global _DRIVER_MOD
    if _DRIVER_MOD is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_e2e_unpacked_under_test", DRIVER)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DRIVER_MOD = mod
    return _DRIVER_MOD


def test_d3_absent_hint_is_wired_into_the_driver():
    """D3 判「没找到内嵌后端」时必须调用 `_backend_absent_hint`。

    ⚠️ 判据落在 **AST 的调用名**上，不查字符串 —— 函数的 docstring 里就写着
    该名字（查子串会被自己的注释满足，本仓已踩过多次）。
    """
    assert "_backend_absent_hint" in _called_names(_func("run_once")), (
        "run_once 未调用 _backend_absent_hint ⇒ D3 的失败归因又变成裸的「没找到」")
    assert "_backend_absent_hint" in {
        n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)
    }, "驱动里没有 _backend_absent_hint 的定义（改名了？护栏需同步）"


def test_d3_wiring_guard_is_not_vacuous():
    """防空转：`_called_names` 只报**被调用**的名字（正反两向都验）。"""
    called = _called_names(_func("run_once"))
    assert "child_processes" in called, "run_once 明明调用了 child_processes"
    assert "_backend_absent_hint" in called, "run_once 明明调用了 _backend_absent_hint"
    assert "_no_such_helper_anywhere" not in called, (
        "`_called_names` 会把不存在的名字也算进来 ⇒ 上面的命中不作数")


def test_backend_absent_hint_is_silent_without_gpu_fatal(tmp_path):
    """无 GPU FATAL 时**必须返回空串**。

    否则会给一个真实的后端拉起缺陷贴上「环境问题」标签 —— 那是比漏报更坏的
    错误归因（本仓的 `llm_failure_attribution` 就吃过这个亏）。
    """
    log = tmp_path / "run1_stderr.log"
    log.write_text("some unrelated error\n", encoding="utf-8")
    assert _driver()._backend_absent_hint(log) == ""


def test_backend_absent_hint_explains_the_sandbox_when_gpu_fatal(tmp_path):
    """有 GPU FATAL 时，归因必须点明真正的自变量（受限沙箱）。"""
    log = tmp_path / "run1_stderr.log"
    log.write_text(
        "FATAL:content\\browser\\gpu\\gpu_data_manager_impl_private.cc:416] "
        "GPU process isn't usable. Goodbye.\n", encoding="utf-8")
    msg = _driver()._backend_absent_hint(log)
    assert "沙箱" in msg, f"归因没点明真正的自变量（受限沙箱）：{msg!r}"
    assert "--disable-gpu" not in msg, (
        "归因又把 --disable-gpu 当解法了（2×2 实测已否证：加与不加在沙箱内都崩）")


def test_backend_absent_hint_survives_a_missing_log(tmp_path):
    """日志不存在也不得抛 —— D3 恰恰是在**崩溃轮次**里读它。"""
    assert _driver()._backend_absent_hint(tmp_path / "nope.log") == ""
