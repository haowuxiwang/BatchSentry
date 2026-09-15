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
