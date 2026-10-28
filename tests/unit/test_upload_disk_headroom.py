# -*- coding: utf-8 -*-
"""上传入口的**磁盘余量守卫**护栏（R78 第十八批，对抗性审查）。

由来：安全/鲁棒性审查发现全仓**没有任何**剩余空间检查（`grep disk_usage` 为空）
⇒ 磁盘写满时会在 pipeline 中途失败（OCR JSONL / 渲染图 / SQLite 行都要落盘），
用户看到的是一个跑到一半的 `error`，而不是入口处的明确拒绝。

被测：`api.jobs.upload._ensure_disk_headroom`。三条语义：
  ① 余量不足 ⇒ 507（**入口拒绝**）；
  ② 余量充足 ⇒ 放行；
  ③ **预检自身失败**（拿不到用量）⇒ fail-open + 记 warning（**非**静默）。

③ 与项目"判不了 ⇒ fail-closed"那类判据**刻意相反**，理由写在被测函数 docstring 里：
这是**容量预检**、不是正确性判据；坏掉的预检不该拦死所有上传，且真正的写入失败
仍有既有 500 路径兜底。本护栏把这条**有意为之**的差异钉住，防有人"顺手改成 fail-closed"。
"""
import collections
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import api.jobs.upload as up  # noqa: E402

_Usage = collections.namedtuple("usage", "total used free")


class _App:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir


class _Cfg:
    """`config["app"].output_dir` 的最小替身 —— 不碰真实 appdata 目录。"""

    def __init__(self, output_dir: str) -> None:
        self._app = _App(output_dir)

    def __getitem__(self, key):
        assert key == "app"
        return self._app


@pytest.fixture()
def patched(tmp_path, monkeypatch):
    """把 `config` 指向 tmp，并返回一个设置"剩余空间"的钩子。"""
    monkeypatch.setattr(up, "config", _Cfg(str(tmp_path)))

    def _set_free(free: int):
        monkeypatch.setattr(up.shutil, "disk_usage", lambda _p: _Usage(0, 0, free))

    def _raise_oserror():
        def _boom(_p):
            raise OSError("no such device")
        monkeypatch.setattr(up.shutil, "disk_usage", _boom)

    return _set_free, _raise_oserror


def _required(size: int) -> int:
    return size * up._DISK_HEADROOM_FACTOR + up._DISK_HEADROOM_MARGIN_BYTES


# ── 0. 反空转 ─────────────────────────────────────────────────────────

def test_guard_targets_the_real_module():
    assert callable(up._ensure_disk_headroom)
    assert up._DISK_HEADROOM_FACTOR >= 1
    assert up._DISK_HEADROOM_MARGIN_BYTES > 0


# ── ① 余量不足 ⇒ 507 ──────────────────────────────────────────────────

def test_rejects_with_507_when_free_space_is_insufficient(patched):
    set_free, _ = patched
    size = 10 * 1024 * 1024
    set_free(_required(size) - 1)                 # 差 1 字节
    with pytest.raises(HTTPException) as ei:
        up._ensure_disk_headroom(size)
    assert ei.value.status_code == 507
    assert "磁盘" in ei.value.detail


def test_boundary_exactly_at_threshold_passes(patched):
    """恰好等于阈值 ⇒ 放行（阈值是**下界**，不是"必须大于"）。"""
    set_free, _ = patched
    size = 10 * 1024 * 1024
    set_free(_required(size))                     # 恰好
    up._ensure_disk_headroom(size)                # 不抛


# ── ② 余量充足 ⇒ 放行 ─────────────────────────────────────────────────

def test_allows_when_free_space_is_ample(patched):
    set_free, _ = patched
    set_free(50 * 1024 ** 3)
    up._ensure_disk_headroom(200 * 1024 * 1024)   # 不抛


# ── ③ 预检自身失败 ⇒ fail-open（**有意**，见 docstring）────────────────

def test_fails_open_when_usage_cannot_be_read(patched, caplog):
    """拿不到用量 ⇒ 放行**且**记 warning（非静默）。

    ⚠️ 这条断言的是**刻意**的 fail-open。若有人把它改成 fail-closed，
    本用例会变红 —— 那正是目的：这是设计决定，不是疏漏。
    """
    _, raise_oserror = patched
    raise_oserror()
    with caplog.at_level("WARNING"):
        up._ensure_disk_headroom(1024)            # 不抛
    assert any("磁盘余量预检跳过" in r.message for r in caplog.records), \
        "fail-open 必须留下 warning，否则就是静默放行"
