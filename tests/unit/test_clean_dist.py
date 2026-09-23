"""`scripts/clean_dist.py` 的方案判定与安全默认值护栏。

这个脚本会**删除构建产物目录**，所以它的判定逻辑必须可测、且默认必须安全：
- 认不出类型 → 只通报不动手；
- 一个完整的 Electron 产物都没有 → 什么都不删（宁可留着，也不能把唯一候选删掉）；
- `dist/`（PyInstaller 产物）永不在清理范围 —— 它是 electron-builder
  `extraResources` 的输入，删了下次打包要先重跑 PyInstaller；
- CLI 默认 dry-run，不加 `--apply` 不落盘。
"""
import ast
import ctypes
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts.clean_dist import (  # noqa: E402
    KIND_BACKEND, KIND_ELECTRON, KIND_UNKNOWN,
    classify, discover, main, plan, asar_version,
)


def _item(name, kind, complete=False, mtime=None, size=1000, embedded=True):
    """体检条目夹具。

    ``embedded`` 对应 ``has_embedded_server``：**默认 True，因为那是常态**。
    B9-5 起"有入口但无内嵌后端"是**独立的一态**（残壳），所以夹具必须能表达它，
    否则既测不到残壳、又会让旧用例因为缺字段被误判成残壳。
    """
    return {"name": name, "kind": kind, "complete": complete,
            "has_embedded_server": embedded,
            "mtime": mtime, "bytes": size, "mb": size / 1048576}


# ── 方案判定 ────────────────────────────────────────────────────────


def test_backend_artifact_is_always_kept():
    keep, doom, review = plan([_item("dist", KIND_BACKEND)])
    assert [i["name"] for i in keep] == ["dist"]
    assert not doom and not review


def test_newest_complete_electron_is_kept_others_doomed():
    items = [
        _item("dist", KIND_BACKEND),
        _item("dist-electron-v112", KIND_ELECTRON, complete=True, mtime=300),
        _item("dist-electron-m8", KIND_ELECTRON, complete=True, mtime=200),
        _item("dist-electron", KIND_ELECTRON, complete=False, mtime=100),
    ]
    keep, doom, review = plan(items)
    assert {i["name"] for i in keep} == {"dist", "dist-electron-v112"}
    assert {i["name"] for i in doom} == {"dist-electron-m8", "dist-electron"}
    assert not review


def test_partial_newer_than_complete_is_still_doomed():
    """构建失败留下的残缺目录会比完好目录**更新**，不能被"最新"规则保护。"""
    items = [
        _item("dist-electron-v112", KIND_ELECTRON, complete=True, mtime=100),
        _item("dist-electron-locked", KIND_ELECTRON, complete=False, mtime=999),
    ]
    keep, doom, _ = plan(items)
    assert [i["name"] for i in keep] == ["dist-electron-v112"]
    assert [i["name"] for i in doom] == ["dist-electron-locked"]


def test_no_complete_artifact_deletes_nothing():
    """安全阀：没有任何完整产物时，残缺目录也只通报、不清除。"""
    items = [
        _item("dist-electron", KIND_ELECTRON, complete=False, mtime=100),
        _item("dist-electron-locked", KIND_ELECTRON, complete=False, mtime=200),
    ]
    keep, doom, review = plan(items)
    assert not keep and not doom
    assert {i["name"] for i in review} == {"dist-electron", "dist-electron-locked"}


def test_unknown_kind_goes_to_review_not_doom():
    keep, doom, review = plan([_item("dist-weird", KIND_UNKNOWN)])
    assert not doom
    assert [i["name"] for i in review] == ["dist-weird"]


def test_ties_break_on_size():
    """mtime 缺失时用体积兜底（完整包显著大于残缺包）。"""
    items = [
        _item("dist-electron-a", KIND_ELECTRON, complete=True, size=400 * 1048576),
        _item("dist-electron-b", KIND_ELECTRON, complete=True, size=5 * 1048576),
    ]
    keep, doom, _ = plan(items)
    assert [i["name"] for i in keep] == ["dist-electron-a"]


# ── 目录识别 ────────────────────────────────────────────────────────


def _make(tmp_path, name, *, backend=False, electron=False, complete=False, version=None,
          embedded=True):
    d = tmp_path / name
    d.mkdir()
    if backend:
        (d / "pbc-server").mkdir()
        (d / "pbc-server" / "pbc-server.exe").write_bytes(b"MZ")
    if electron:
        wu = d / "win-unpacked"
        (wu / "resources").mkdir(parents=True)
        (wu / "resources" / "app.asar").write_bytes(b"\x00" * 16)
        if embedded:
            (wu / "resources" / "pbc-server").mkdir()
            (wu / "resources" / "pbc-server" / "pbc-server.exe").write_bytes(b"MZ")
        if complete:
            (wu / "BatchSentry.exe").write_bytes(b"MZ")
    return d


def test_discover_only_takes_dist_prefixed_dirs(tmp_path):
    _make(tmp_path, "dist", backend=True)
    _make(tmp_path, "dist-electron", electron=True)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "output").mkdir()
    names = [p.name for p in discover(tmp_path)]
    assert names == ["dist", "dist-electron"]


def test_classify_backend_and_electron(tmp_path):
    b = classify(_make(tmp_path, "dist", backend=True))
    assert b["kind"] == KIND_BACKEND and b["files"] == 1

    e = classify(_make(tmp_path, "dist-electron", electron=True, complete=True))
    assert e["kind"] == KIND_ELECTRON
    assert e["complete"] is True
    assert e["has_embedded_server"] is True
    assert e["files"] == 3


def test_classify_partial_electron_is_not_complete(tmp_path):
    e = classify(_make(tmp_path, "dist-electron", electron=True, complete=False))
    assert e["kind"] == KIND_ELECTRON and e["complete"] is False


def test_classify_unknown_dir(tmp_path):
    (tmp_path / "dist-junk").mkdir()
    assert classify(tmp_path / "dist-junk")["kind"] == KIND_UNKNOWN


# ── asar 版本解析 ───────────────────────────────────────────────────


def test_asar_version_returns_none_on_garbage(tmp_path):
    bad = tmp_path / "app.asar"
    bad.write_bytes(b"not an asar at all")
    assert asar_version(bad) is None


def test_asar_version_returns_none_on_missing(tmp_path):
    assert asar_version(tmp_path / "nope.asar") is None


# ── CLI 默认安全（dry-run）──────────────────────────────────────────


def test_cli_defaults_to_dry_run(tmp_path, capsys):
    """不加 --apply 时一个目录都不能消失。"""
    _make(tmp_path, "dist-electron-v112", electron=True, complete=True)
    _make(tmp_path, "dist-electron-m8", electron=True, complete=True)
    assert main(["--root", str(tmp_path)]) == 0
    assert (tmp_path / "dist-electron-m8").is_dir(), "dry-run 不应删除任何目录"
    assert "dry-run" in capsys.readouterr().out


def _touch_newer(dir_path: Path, ts: float):
    """把目录内入口的 mtime 设为 ts —— 让「谁最新」在测试里确定，不靠创建顺序。"""
    import os
    os.utime(dir_path / "win-unpacked" / "BatchSentry.exe", (ts, ts))


def test_cli_json_report_lists_plan(tmp_path, capsys):
    _make(tmp_path, "dist", backend=True)
    _make(tmp_path, "dist-electron-v112", electron=True, complete=True)
    _make(tmp_path, "dist-electron-m8", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-v112", 2_000_000_000)
    _touch_newer(tmp_path / "dist-electron-m8", 1_000_000_000)
    assert main(["--root", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "dist" in data["keep"] and "dist-electron-v112" in data["keep"]
    assert data["doom"] == ["dist-electron-m8"]


def test_cli_apply_skips_locked_dirs(tmp_path, capsys, monkeypatch):
    """被占用而删不掉的目录必须被跳过并说明，而不是静默失败。"""
    import scripts.clean_dist as cd
    _make(tmp_path, "dist-electron-v112", electron=True, complete=True)
    _make(tmp_path, "dist-electron-m8", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-v112", 2_000_000_000)
    _touch_newer(tmp_path / "dist-electron-m8", 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files",
                        lambda p: ["win-unpacked/resources/app.asar (fake)"] if p.name.endswith("m8") else [])
    called = []
    monkeypatch.setattr(cd, "to_recycle_bin", lambda p: (called.append(p.name), (True, "stub"))[1])
    assert main(["--root", str(tmp_path), "--apply"]) == 1
    assert called == [], "被占锁的目录不应尝试删除"
    out = capsys.readouterr().out
    assert "跳过" in out and "dist-electron-m8" in out


# ── 占锁判据（B9-9：**只读**） ──────────────────────────────────────
#
# 两条核心性质，缺一不可：
#   ① 探测**不写任何东西** —— 门禁（release_gate）会调用它，于是"检查"动作
#      本身不得改动仓库（旧实现"改名再改回"违反这条，2026-09-23 淘汰）。
#   ② 判据精确对应"能否删除/替换"，而不是"有没有人打开" —— 用**真实句柄**
#      做正负对照来钉住：以 share=0 打开的句柄会挡住删除（应报锁定）；
#      以 FILE_SHARE_DELETE 打开的句柄**不挡**删除（应报可删）。
#      跑真实内核语义比 monkeypatch 硬，也不会随实现细节漂移。
#
# ⚠️ 还有一条**方向**上的性质（2026-09-23 实测得到）：只读探针对**目录**会
#   低估锁定（目录自身的 DELETE 权限 ≠ 子项可删）⇒ 实现**必须**全树遍历、
#   不得短路。`test_descent_is_never_short_circuited` 就是它的回归钉子。

_WIN_ONLY = pytest.mark.skipif(sys.platform != "win32",
                               reason="文件锁语义是 Windows 专有的")

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_DELETE = 0x00000004
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3


def _open_real_handle(path, share_mode):
    """用**真实内核句柄**持有 ``path``（调用方负责 CloseHandle）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.CreateFileW(str(path), _GENERIC_READ, share_mode, None,
                             _OPEN_EXISTING, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError(f"无法打开测试句柄 winerror={ctypes.get_last_error()}")
    return k32, handle


def _calls_of(func_names: set[str]) -> set[str]:
    """收集这些函数（含其内部调用）里出现的**被调用名**（AST，不查字面量）。

    用 AST 而不是正则：注释/文档字符串里提到 ``os.rename`` 不该被判红，
    而真实调用必须判红。
    """
    import scripts.clean_dist as cd
    tree = ast.parse(Path(cd.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in func_names:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    names.add(f.attr if isinstance(f, ast.Attribute)
                              else getattr(f, "id", ""))
    return names


def test_lock_probe_never_writes_by_construction():
    """机检：锁探测路径上**不存在**改名/删除/写入类调用（结构性保证）。

    为什么不只靠行为测试：行为测试只能证明"这次没改到那个文件"。实现若在
    别处改名（或对**未纳入断言**的对象动手），行为测试看不见。这里直接把
    "允许的调用面"钉死：锁探测只允许读（scandir / stat / CreateFileW / CloseHandle）。
    """
    forbidden = {"rename", "replace", "remove", "unlink", "rmdir", "rmtree",
                 "mkdir", "makedirs", "write_text", "write_bytes", "open",
                 "shutil", "move", "CopyFile", "DeleteFile"}
    calls = _calls_of({"can_delete", "_scan_locked", "locked_files"})
    assert calls, "AST 没抽到任何调用 —— 函数名漂移了，判据已失效"
    bad = forbidden & calls
    assert not bad, (
        f"锁探测路径出现了写操作 {sorted(bad)} —— 检查动作不得改动被检对象"
        "（B9-9：这正是淘汰「改名再改回」的原因）")


@_WIN_ONLY
def test_probe_does_not_write_anything(tmp_path):
    """行为层：探测前后，目录项集合 / 文件内容 / mtime **逐项不变**。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-x"
    (d / "win-unpacked").mkdir(parents=True)
    f = d / "win-unpacked" / "a.bin"
    f.write_bytes(b"payload")

    before_entries = sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))
    before_stat = (f.stat().st_size, f.stat().st_mtime_ns, f.read_bytes())

    assert cd.locked_files(d) == []

    after_entries = sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))
    after_stat = (f.stat().st_size, f.stat().st_mtime_ns, f.read_bytes())
    assert before_entries == after_entries, "探测改动了目录项（写操作！）"
    assert before_stat == after_stat, "探测改动了文件内容或 mtime（写操作！）"


@_WIN_ONLY
def test_locked_when_holder_withholds_delete_sharing(tmp_path):
    """被「不给删除共享」的句柄持有 ⇒ 必须报锁定，且**指名到文件**。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-locked"
    (d / "win-unpacked" / "resources").mkdir(parents=True)
    locked = d / "win-unpacked" / "resources" / "app.asar"
    locked.write_bytes(b"x")
    (d / "win-unpacked" / "ok.txt").write_bytes(b"x")

    k32, handle = _open_real_handle(locked, _FILE_SHARE_READ)
    try:
        out = cd.locked_files(d)
    finally:
        k32.CloseHandle(handle)

    assert len(out) == 1, f"只应指名被持有的那一个文件，实得 {out}"
    assert "win-unpacked/resources/app.asar" in out[0]
    assert "winerror=32" in out[0], f"应报 SHARING_VIOLATION，实得 {out[0]}"
    assert cd.locked_files(d) == [], "句柄关闭后必须恢复为可删（判据是时点事实）"


@_WIN_ONLY
def test_not_locked_when_holder_allows_delete_sharing(tmp_path):
    """**对照**：句柄给了 ``FILE_SHARE_DELETE`` ⇒ 不挡删除 ⇒ 不得报锁定。

    这条防的是"把判据写成只要有人打开就报警"——那种判据会让可替换的产物
    被误报成不可替换，进而把门禁的可行动项**误降级**。
    """
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-parallel"
    (d / "win-unpacked").mkdir(parents=True)
    f = d / "win-unpacked" / "app.asar"
    f.write_bytes(b"x")

    k32, handle = _open_real_handle(f, _FILE_SHARE_READ | _FILE_SHARE_DELETE)
    try:
        out = cd.locked_files(d)
    finally:
        k32.CloseHandle(handle)

    assert out == [], (
        f"持有者允许删除共享时不该判为锁定，实得 {out} —— 判据退化成「有人打开就报锁」")


def test_descent_is_never_short_circuited(tmp_path, monkeypatch):
    """**回归**：不得因目录自身探针通过就跳过下钻（旧实现的目录级短路）。

    2026-09-23 实测：目录的 DELETE 权限 **≠** 目录可删 —— 含被占用子项的目录，
    其自身探针照样放行（NTFS 只在**递归删除时**才检查子项句柄）。所以让
    **根目录**探针放行、子文件探针报锁，实现仍必须指名到文件；短路实现会漏报。
    """
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-nested"
    (d / "win-unpacked" / "resources").mkdir(parents=True)
    locked = d / "win-unpacked" / "resources" / "app.asar"
    locked.write_bytes(b"x")

    def fake(p):
        if Path(p) == locked:
            return False, 32
        return True, 0                      # 所有目录（含根）都"能删"

    monkeypatch.setattr(cd, "can_delete", fake)
    out = cd.locked_files(d)
    assert any("app.asar" in one for one in out), (
        f"目录级探针通过就短路了 ⇒ 漏报被持有的文件（实得 {out}）")


def test_dir_self_denial_is_reported_honestly(tmp_path, monkeypatch):
    """目录自身拿不到删除权时必须**如实报出**（空表会被读成"可以删"）。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-denied"
    (d / "win-unpacked").mkdir(parents=True)
    (d / "win-unpacked" / "BatchSentry.exe").write_bytes(b"x")

    monkeypatch.setattr(cd, "can_delete",
                        lambda p: (False, 5) if Path(p) == d else (True, 0))
    out = cd.locked_files(d)
    assert out, "不得返回空表 —— 空表会被下游读成「无占用、可删除」"
    assert out[0].startswith("("), "必须是伪条目（下游据此跳过具名）"
    assert "目录自身不可删" in out[0]
    assert out[0].split("(", 1)[0].strip() == "", (
        "伪条目不得带路径 —— 否则下游会拿它去查持有者（查不到）")


@_WIN_ONLY
def test_locked_files_handles_a_file_target(tmp_path):
    """``path`` 直接是**文件**时也要如实判定（调用方可能传单文件目标）。"""
    import scripts.clean_dist as cd
    f = tmp_path / "app.asar"
    f.write_bytes(b"x")
    assert cd.locked_files(f) == []

    k32, handle = _open_real_handle(f, _FILE_SHARE_READ)
    try:
        out = cd.locked_files(f)
    finally:
        k32.CloseHandle(handle)
    assert len(out) == 1 and "app.asar" in out[0], out


def test_variant_discovery_only_accepts_the_dist_prefix(tmp_path):
    """``discover`` 只认 ``dist`` 前缀的目录（临时/探针名不得被误认成变体）。

    旧实现曾因此踩坑：探针名若带 ``dist`` 前缀，中途被打断就会留下一个
    "身份不明的新变体"并把体检结论带偏。探针本身已改成只读（不再产生
    临时名），但这条**发现规则**仍要钉住 —— 它防的是将来再出现同类临时名。
    """
    import scripts.clean_dist as cd
    (tmp_path / "dist-electron-real").mkdir()
    (tmp_path / "__lockprobe__.dist-electron-real").mkdir()
    (tmp_path / "tmp-scratch").mkdir()
    names = {p.name for p in cd.discover(tmp_path)}
    assert names == {"dist-electron-real"}, f"discover 收进了非变体目录：{names}"


# ── 持有者具名（Restart Manager）─────────────────────────────────────
#
# 2026-09-17 实测：`tasklist` 排除法把持有者误判成"安全软件"，于是给出的处置是
# "加白名单"——**完全无效**（真凶是宿主进程按包打开 .asar 后留下持久句柄）。
# 教训固化：① 持有者必须**具名**而不是推测；② 报告不得再出现"加白名单"这条建议。


def test_long_variant_name_does_not_run_into_the_kind_column(tmp_path, capsys):
    """时间戳变体名很长，列宽不够就会与"类型"列粘成一串（2026-09-17 实跑发现）。"""
    long_name = "dist-electron-out-20260917-142437"
    _make(tmp_path, long_name, electron=True, complete=True)
    assert main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert long_name + "electron" not in out, (
        f"长目录名挤爆列宽，与类型列粘连：{[l for l in out.splitlines() if long_name in l]}"
    )


def test_who_holds_is_fail_soft(tmp_path, monkeypatch):
    """诊断辅助绝不能把清理工具带崩：路径不存在 / dll 不可用都只返回空表。"""
    import scripts.clean_dist as cd
    assert cd.who_holds(tmp_path / "nope.asar") == []
    assert cd.who_holds(tmp_path) == []          # 目录、且无人持有

    def boom(*a, **k):
        raise OSError("rstrtmgr 不可用")

    monkeypatch.setattr(cd.ctypes, "WinDLL", boom)
    assert cd.who_holds(__file__) == [], "查询失败必须返回空表，而不是抛出去"


@pytest.mark.skipif(sys.platform != "win32", reason="Restart Manager 仅 Windows 提供")
def test_who_holds_names_the_real_holder(tmp_path):
    """阳性对照：文件**真被独占持有**时必须具名报出持有者 PID。

    为什么非要有这一条：`who_holds` 是 fail-soft 的（任何异常都返回 `[]`），
    于是"实现坏了"与"没人持有"**返回值完全一样** —— 上面那条 fail-soft 测试
    对"实现损坏"**零判别力**。这里让**本进程自己**持有该文件，把"具名"钉住；
    释放后要求回到空表，防止实现退化成"永远报一个 pid"（另一种零判别力）。

    ⚠️ 刻意**不起子进程**：`tests/` 禁止用**未排空的管道**接子进程输出
    （护栏 `test_no_unread_pipe_in_test_harnesses` 会红）。实测 RM 会把
    **调用进程自身**报成持有者，所以本进程持有即可构成阳性对照。
    （此处刻意不写出那个被禁字面量：该护栏是文本匹配，写出来会自伤。）
    """
    import scripts.clean_dist as cd

    target = tmp_path / "held.asar"
    target.write_bytes(b"x")
    handle = open(target, "r+b")           # 不含 FILE_SHARE_DELETE ⇒ 独占持有
    try:
        handle.write(b"y")
        handle.flush()
        me = os.getpid()
        holders = cd.who_holds(target)
        assert any(h["pid"] == me for h in holders), (
            f"应具名报出持有者 pid={me}，实际 {holders}")
    finally:
        handle.close()

    assert cd.who_holds(target) == [], "持有者释放后必须回到空表"


def test_describe_holders_is_empty_when_nobody_holds(tmp_path):
    """无人持有时返回空串（调用方据此判空，不必区分"查不到"与"没人"）。"""
    import scripts.clean_dist as cd
    f = tmp_path / "free.asar"
    f.write_bytes(b"x")
    assert cd.describe_holders(f) == ""


def test_describe_holders_names_the_process(tmp_path, monkeypatch):
    import scripts.clean_dist as cd
    f = tmp_path / "held.asar"
    f.write_bytes(b"x")
    monkeypatch.setattr(cd, "who_holds",
                        lambda p: [{"pid": 4242, "app": "SomeApp", "type": "Unknown"}])
    assert "SomeApp" in cd.describe_holders(f) and "4242" in cd.describe_holders(f)


def test_advice_names_the_holder_and_negates_the_ineffective_fix(
        tmp_path, capsys, monkeypatch):
    """报告必须① 具名持有者 ② 明说"加白名单无效"（旧建议已被实测否定）。"""
    import scripts.clean_dist as cd
    _make(tmp_path, "dist-electron-v112", electron=True, complete=True)
    _make(tmp_path, "dist-electron-m8", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-v112", 2_000_000_000)
    _touch_newer(tmp_path / "dist-electron-m8", 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files",
                        lambda p: ["win-unpacked/resources/app.asar  (OSError: winerror=32)"]
                        if p.name.endswith("m8") else [])
    monkeypatch.setattr(cd, "who_holds",
                        lambda p: [{"pid": 99, "app": "RealHolder", "type": "Unknown"}])
    assert main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "RealHolder(pid=99" in out, "必须具名持有者，不能只报'被外部句柄占用'"
    assert "退出持有者进程" in out, "处置应是「释放持有者」，不是加白名单"
    assert "无效" in out, "必须显式否掉已验证无效的「加白名单」建议"
    assert "dist-electron-m8" in out and "dist-electron-v112" in out


def test_module_docstring_states_the_measured_root_cause():
    """根因写错会把人引向无效操作 ⇒ 把实测结论锁进文档字符串。"""
    import scripts.clean_dist as cd
    doc = cd.__doc__ or ""
    assert "WorkBuddy" in doc, "须写明实测持有者是谁"
    assert "不是安全软件" in doc, "必须显式否掉旧的「杀软/火绒」误判"


# ── 部分收敛（B9-5）：整目录删不掉时，只回收「可分发陈旧字节」 ──────────
#
# 为什么需要：宿主长期持有 `app.asar` ⇒ 那份陈旧 Electron 目录既删不掉、也原地
# 重建不了；而它里面的**内嵌后端**才是 `discover_artifacts` 认得的"产物"
# （会让门禁**永久红**，也可能被误当成要发的那个包）。四条硬约束各有一条用例：
#   ① 目标**派生**自 EMBEDDED_SERVER（不另写路径规则）；
#   ② 目标自己不可替换 ⇒ 绝不动手（宁缺勿错）；
#   ③ 默认 dry-run，只有 `--apply --converge-locked` 才动手；
#   ④ 动了就必写 HUSK.md，且写不进去要**说出来**。


def _fake_locks(p):
    """模拟 B9-5 的真实锁形：**只有 electron 根目录**报占用（app.asar 被持有）。

    子目录（包括收敛目标 `…/resources/pbc-server`）一律空闲 —— 这正是实测结论：
    整目录删不掉 ≠ 里面每一部分都删不掉（外层是 `winerror=5` 的**派生**症状）。
    """
    return ([r"win-unpacked/resources/app.asar  (OSError: winerror=32)"]
            if Path(p).name.startswith("dist-electron") else [])


def test_state_of_distinguishes_husk_from_partial():
    """残壳（有入口、无后端）必须与"残缺"分开：前者跑不起来，且是**已收尾**状态。"""
    import scripts.clean_dist as cd
    assert cd.state_of(_item("d", KIND_ELECTRON, complete=True)) == "完整"
    assert cd.state_of(_item("d", KIND_ELECTRON, complete=True, embedded=False)) == "残壳"
    assert cd.state_of(_item("d", KIND_ELECTRON, complete=False)) == "残缺"
    assert cd.state_of(_item("d", KIND_BACKEND)) == "-"


def test_husk_never_becomes_the_kept_candidate():
    """残壳更"新"也不得被保留 —— 保留它等于保护一个双击跑不起来的目录。"""
    import scripts.clean_dist as cd
    husk = _item("dist-electron-husk", KIND_ELECTRON, complete=True, embedded=False,
                 mtime=2_000_000_000, size=999)
    real = _item("dist-electron-real", KIND_ELECTRON, complete=True,
                 mtime=1_000_000_000, size=100)
    keep, doom, review = cd.plan([real, husk])
    assert [i["name"] for i in keep] == ["dist-electron-real"]
    assert [i["name"] for i in doom] == ["dist-electron-husk"]


def test_husk_alone_deletes_nothing():
    """只剩残壳（一个真包都没有）⇒ 仍然什么都不删（继续守着"宁缺勿错"）。"""
    import scripts.clean_dist as cd
    keep, doom, review = cd.plan([_item("h", KIND_ELECTRON, complete=True, embedded=False)])
    assert doom == [] and [i["name"] for i in review] == ["h"]


def test_convergence_target_is_derived_and_requires_replaceability(tmp_path, monkeypatch):
    import scripts.clean_dist as cd
    d = _make(tmp_path, "dist-electron", electron=True, complete=True)
    assert cd.convergence_target(d) == d / "win-unpacked" / "resources" / "pbc-server"
    # 目标自己不可替换 ⇒ 不动（"能改名"不等于"能删"，这里直接锁死）
    monkeypatch.setattr(cd, "locked_files", lambda p: ["pbc-server.exe (winerror=32)"])
    assert cd.convergence_target(d) is None


def test_convergence_target_is_none_for_a_husk(tmp_path):
    """已经是残壳（没有内嵌后端）⇒ 没有可收敛的目标，而不是"再删一次"。"""
    import scripts.clean_dist as cd
    d = _make(tmp_path, "dist-electron", electron=True, complete=True, embedded=False)
    assert cd.convergence_target(d) is None


def test_cli_converge_is_dry_run_unless_apply(tmp_path, capsys, monkeypatch):
    """默认（不加 --apply）必须**只预告不动手** —— 部分收敛也是删除操作。"""
    import scripts.clean_dist as cd
    _make(tmp_path, "dist-electron-old", electron=True, complete=True)
    _make(tmp_path, "dist-electron-new", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-new", 2_000_000_000)
    _touch_newer(tmp_path / "dist-electron-old", 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files", _fake_locks)
    done = []
    monkeypatch.setattr(cd, "to_recycle_bin", lambda p: (done.append(p), (True, "stub"))[1])
    assert cd.main(["--root", str(tmp_path)]) == 0
    assert done == [], "dry-run 不得回收任何东西"
    out = capsys.readouterr().out
    assert "部分收敛" in out and "--converge-locked" in out, "dry-run 要预告可收敛的目标"
    assert not (tmp_path / "dist-electron-old" / "HUSK.md").exists(), "没动手就不该写标记"


def test_cli_apply_converge_locked_recycles_only_the_target(tmp_path, capsys, monkeypatch):
    """--apply --converge-locked：只回收内嵌后端，并写 HUSK.md 标出残壳。"""
    import scripts.clean_dist as cd
    old = _make(tmp_path, "dist-electron-old", electron=True, complete=True)
    _make(tmp_path, "dist-electron-new", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-new", 2_000_000_000)
    _touch_newer(old, 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files", _fake_locks)
    done = []
    monkeypatch.setattr(cd, "to_recycle_bin", lambda p: (done.append(p), (True, "stub"))[1])
    assert cd.main(["--root", str(tmp_path), "--apply", "--converge-locked"]) == 1
    assert done == [old / "win-unpacked" / "resources" / "pbc-server"], (
        f"只允许回收内嵌后端，实际回收 {done}")
    husk = old / "HUSK.md"
    assert husk.is_file(), "必须留下「这里少了一份产物」的标记"
    text = husk.read_text(encoding="utf-8")
    assert "win-unpacked/resources/pbc-server" in text, "标记要写明被回收的是哪一份"
    assert "不要" in text and "分发" in text, "标记要明说本目录不可分发"
    assert "[converge]" in capsys.readouterr().out


def test_cli_converge_skips_a_non_replaceable_target(tmp_path, capsys, monkeypatch):
    """目标自己也删不掉 ⇒ 不动手，也**不写** HUSK（没发生的事不许留痕）。"""
    import scripts.clean_dist as cd
    old = _make(tmp_path, "dist-electron-old", electron=True, complete=True)
    _make(tmp_path, "dist-electron-new", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-new", 2_000_000_000)
    _touch_newer(old, 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files", lambda p: [r"x  (OSError: winerror=32)"])
    done = []
    monkeypatch.setattr(cd, "to_recycle_bin", lambda p: (done.append(p), (True, "stub"))[1])
    assert cd.main(["--root", str(tmp_path), "--apply", "--converge-locked"]) == 1
    assert done == [], "目标不可替换时绝不能动手"
    assert not (old / "HUSK.md").exists(), "没真的收敛就不该写标记"
    assert "跳过" in capsys.readouterr().out


def test_cli_json_reports_converge_targets(tmp_path, capsys, monkeypatch):
    """JSON 报告要把可收敛目标报出来（机器可读，便于事后审计「删了什么」）。"""
    import scripts.clean_dist as cd
    old = _make(tmp_path, "dist-electron-old", electron=True, complete=True)
    _make(tmp_path, "dist-electron-new", electron=True, complete=True)
    _touch_newer(tmp_path / "dist-electron-new", 2_000_000_000)
    _touch_newer(old, 1_000_000_000)
    monkeypatch.setattr(cd, "locked_files", _fake_locks)
    assert cd.main(["--root", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["converge"] == {"dist-electron-old": "win-unpacked/resources/pbc-server"}


def test_named_holders_skips_pseudo_entries_and_dedupes(tmp_path, monkeypatch):
    """具名推导的**唯一实现**：伪条目跳过、重复去重、原锁清单原样保留。

    伪条目（"(目录级改名被拒 …)"）没有文件路径可查 ⇒ 跳过。若把它当成一条
    "已具名"，门禁就会把"查不到是谁"读成"有人持有"并据此**降级**。
    """
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron"
    (d / "win-unpacked").mkdir(parents=True)
    monkeypatch.setattr(cd, "who_holds",
                        lambda p: [{"pid": 7, "app": "A", "type": "Unknown"}])
    locks = ["win-unpacked/app.asar (x)", "win-unpacked/app.asar (x)",
             "(目录级改名被拒 winerror=5，但逐项探测未见被占用者)", "win-unpacked/o (y)"]
    labels, kept = cd.named_holders(d, locks)
    assert labels == ["A(pid=7, Unknown)"], f"应去重且跳过伪条目，实际 {labels}"
    assert kept == locks, "锁清单要原样保留（展示用），不得被改写"


def test_husk_note_write_failure_is_reported_not_swallowed(tmp_path):
    """标记写不进去必须**说出来** —— 否则又回到"目录还在，所以它大概还在"。"""
    import scripts.clean_dist as cd
    ghost = tmp_path / "gone" / "dist-electron"
    tgt = ghost / "win-unpacked" / "resources" / "pbc-server"
    msg = cd.write_husk_note(ghost, tgt, [])
    assert "⚠" in msg and "HUSK.md" in msg


def test_docstring_documents_the_partial_convergence_semantics():
    """B9-5 的取舍（只回收内嵌后端 + 留残壳）必须写在文档里，不能只留在代码里。"""
    import scripts.clean_dist as cd
    doc = cd.__doc__ or ""
    assert "部分收敛" in doc and "--converge-locked" in doc
    assert "残壳" in doc, "必须给出「残壳」这个可辨识的名字"

