"""`scripts/clean_dist.py` 的方案判定与安全默认值护栏。

这个脚本会**删除构建产物目录**，所以它的判定逻辑必须可测、且默认必须安全：
- 认不出类型 → 只通报不动手；
- 一个完整的 Electron 产物都没有 → 什么都不删（宁可留着，也不能把唯一候选删掉）；
- `dist/`（PyInstaller 产物）永不在清理范围 —— 它是 electron-builder
  `extraResources` 的输入，删了下次打包要先重跑 PyInstaller；
- CLI 默认 dry-run，不加 `--apply` 不落盘。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts.clean_dist import (  # noqa: E402
    KIND_BACKEND, KIND_ELECTRON, KIND_UNKNOWN,
    classify, discover, main, plan, asar_version,
)


def _item(name, kind, complete=False, mtime=None, size=1000):
    return {"name": name, "kind": kind, "complete": complete,
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


def _make(tmp_path, name, *, backend=False, electron=False, complete=False, version=None):
    d = tmp_path / name
    d.mkdir()
    if backend:
        (d / "pbc-server").mkdir()
        (d / "pbc-server" / "pbc-server.exe").write_bytes(b"MZ")
    if electron:
        wu = d / "win-unpacked"
        (wu / "resources" / "pbc-server").mkdir(parents=True)
        (wu / "resources" / "pbc-server" / "pbc-server.exe").write_bytes(b"MZ")
        (wu / "resources" / "app.asar").write_bytes(b"\x00" * 16)
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


# ── 认锁探测的复杂度（2026-09-17 实测的性能修复） ────────────────────
#
# 实测：无条件逐文件"改名再改回"，在本机 3 GB / 数万文件的待清理目录上
# 跑了 **>10 分钟仍无任何结论**（每次改名都被安全软件拦一道）。而 NTFS
# 拒绝重命名含被占用子项的目录 ⇒ **目录级改名成功即证明内部无占用者**，
# 一次 syscall 就能定案。下面三条锁住这个性质。


def test_rename_probe_leaves_path_intact(tmp_path):
    """探针成功时必须把对象改回原名（不能把目录留在探针名下）。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-x"
    d.mkdir()
    (d / "a.txt").write_text("x", encoding="utf-8")
    assert cd._rename_probe(d) is None
    assert d.is_dir() and not (tmp_path / "__lockprobe__.dist-electron-x").exists()


def test_probe_name_is_not_mistakable_for_a_variant(tmp_path):
    """探针目录名不得以 ``dist`` 开头 —— 否则中途被打断会被误认成真实变体。"""
    import scripts.clean_dist as cd
    (tmp_path / "dist-electron-real").mkdir()
    (tmp_path / "__lockprobe__.dist-electron-real").mkdir()
    names = {p.name for p in cd.discover(tmp_path)}
    assert names == {"dist-electron-real"}, (
        f"discover 把探针目录也算进来了：{names} —— 探针名必须避开 dist 前缀"
    )


def test_clean_dir_probe_short_circuits_the_descent(tmp_path, monkeypatch):
    """目录级探测通过时**不得**再做下钻（这是 >10min → O(1) 的关键）。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-clean"
    (d / "win-unpacked").mkdir(parents=True)

    def _boom(*a, **k):
        raise AssertionError("目录级探测已通过，不该再下钻")

    monkeypatch.setattr(cd, "_find_locked", _boom)
    assert cd.locked_files(d) == []


def test_locked_file_is_named_when_dir_probe_fails(tmp_path, monkeypatch):
    """目录级被拒时要下钻并**指名**挡路的文件（加白名单要的是文件名）。

    注意 fake 必须符合物理现实：NTFS 拒绝重命名含被占用子项的目录，所以
    被占用文件的**全部祖先目录**也必然改名失败。若 fake 只让文件本身失败、
    却让它的父目录改名成功，那是自相矛盾的状态，测不出真实行为。
    """
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-locked"
    (d / "win-unpacked" / "resources").mkdir(parents=True)
    locked = d / "win-unpacked" / "resources" / "app.asar"
    locked.write_text("x", encoding="utf-8")
    (d / "win-unpacked" / "ok.txt").write_text("x", encoding="utf-8")

    real = cd._rename_probe

    def fake(p):
        p = Path(p)
        if p == locked or locked.is_relative_to(p):   # p 是 locked 自身或其祖先
            return OSError(13, "locked")
        return real(p)

    monkeypatch.setattr(cd, "_rename_probe", fake)
    out = cd.locked_files(d)
    assert len(out) == 1 and "app.asar" in out[0], (
        f"只应指名被占用的那一个文件，实得 {out}"
    )


def test_dir_level_denial_without_a_locked_file_is_reported_honestly(
        tmp_path, monkeypatch):
    """目录级被拒但找不到任何被占用项时，不得返回空表（那会被读成"可以删"）。"""
    import scripts.clean_dist as cd
    d = tmp_path / "dist-electron-denied"
    (d / "win-unpacked").mkdir(parents=True)
    (d / "win-unpacked" / "BatchSentry.exe").write_text("x", encoding="utf-8")

    real = cd._rename_probe

    def fake(p):
        if Path(p) == d:                 # 只有目录本身被拒，内部文件都能改名
            return OSError(13, "denied")
        return real(p)

    monkeypatch.setattr(cd, "_rename_probe", fake)
    out = cd.locked_files(d)
    assert out, "不得返回空表 —— 空表会被下游读成'无占用、可删除'"
    assert "目录级" in out[0]


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

