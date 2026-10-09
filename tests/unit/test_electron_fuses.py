"""Electron fuses：**配置 ↔ 产物**双向绑定（R80 第二十批 → TODO 0-21）。

## 为什么需要

`docs/ENTERPRISE_READINESS_2026-10-09.md` §2 官方清单第 19 项「Fuses」此前标
**⚠️ 未核**。实测（读产物 fuse wire，`devlogs/_verify/r80_electron_fuses.py`）：
产物停在 **Electron 默认融合值** —— `RunAsNode` / `EnableNodeOptionsEnvironmentVariable`
/ `EnableNodeCliInspectArguments` **三个全开**。

两项有**实测**后果：

- `EnableNodeOptionsEnvironmentVariable` 开着 ⇒ 宿主（Electron 系的 IDE / 终端）给所有
  子进程注入的 `NODE_OPTIONS=--require <shim>` **被产物接收**，Electron 报
  `Most NODE_OPTIONs are not supported in packaged apps` —— 宿主环境**泄漏进产物主进程**。
- `RunAsNode` 开着 ⇒ 任何带 `ELECTRON_RUN_AS_NODE=1` 的环境启动产物，产物会**静默变纯
  Node**：本项目 R57 实测 `0.2–0.7s`、`rc=0`、无窗口、`--version` 打印的是 **Node** 版本
  —— 症状与「产物坏了」**完全一致**（当时误判为产物缺陷，实为环境）。关掉后产物对该
  变量**免疫**，这也顺带是"从 Electron 系终端启动双击没反应"那个 bug 的根治。

## 锁住的三件事

A. **配置绑定**：`package.json` 的 `build.electronFuses` 必须显式关掉这三项；
B. **安全性**（关掉不破坏自身启动）：**产品代码**不得引用这三个通道；
C. **产物绑定**：产物存在时 fuse wire 必须反映配置 —— 且 **count 字节保持 9**、
   第 9 位 `WasmTrapHandlers` 未被截断（证明写入是**外科式**的，不是重写整块）。

## 刻意**不**改的三个 fuse

`enableEmbeddedAsarIntegrityValidation` / `onlyLoadAppFromAsar` /
`grantFileProtocolExtraPrivileges` —— 它们改变**启动语义**（尤其 asar 完整性校验若
元数据缺失会让产物**起不来**），而本环境**无法验证 Electron 能否启动**（MEMORY §12：
Electron 层 e2e 在受限沙箱内不可复现）。**不盲改**，登记为未修（企业文档 §4）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_PKG = REPO / "package.json"
_EXE = REPO / "dist-electron" / "win-unpacked" / "BatchSentry.exe"

#: 必须关掉的三项（配置键 → 产物里的位序）
MUST_DISABLE = {
    "runAsNode": 0,
    "enableNodeOptionsEnvironmentVariable": 2,
    "enableNodeCliInspectArguments": 3,
}

#: 这三个通道的**字面**引用 —— 产品代码里出现即说明关掉会破坏自身
_CHANNEL_TOKENS = ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "--inspect")

#: 产品代码根（**不含** tests/ 与 docs/ —— 驱动里的 `env.pop("ELECTRON_RUN_AS_NODE")`
#: 是**历史 workaround**，正是本次要让它变得不必要的东西，不该算进"依赖"）
_PRODUCT_GLOBS = (
    ("electron", "**/*.js"),
    ("api", "**/*.py"),
    ("core", "**/*.py"),
    ("db", "**/*.py"),
    ("models", "**/*.py"),
    ("llm", "**/*.py"),
)
_PRODUCT_FILES = ("main.py", "server.py", "config.py", "logging_config.py")

SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
_DISABLE, _ENABLE, _REMOVED = 0x30, 0x31, 0x72
#: 第 9 位（`@electron/fuses@1.8.0` 尚不认识；当前 release 2.1.3 才命名）
_WASM_TRAP_HANDLERS = 8


def _fuse_config() -> dict:
    build = json.loads(_PKG.read_text(encoding="utf-8")).get("build", {})
    return build.get("electronFuses", {}) or {}


def _read_wire(path: Path) -> tuple[int, list[int]]:
    """返回 (版本, 融合字节列表)。"""
    data = path.read_bytes()
    i = data.find(SENTINEL)
    assert i >= 0, "找不到 fuse 哨兵 —— 不是 Electron 主二进制？"
    j = i + len(SENTINEL)
    return data[j], list(data[j + 2: j + 2 + data[j + 1]])


def _product_sources() -> list[Path]:
    out: list[Path] = []
    for d, pat in _PRODUCT_GLOBS:
        out += sorted((REPO / d).rglob(pat))
    out += [REPO / f for f in _PRODUCT_FILES]
    return [p for p in out if p.is_file()]


# ── A. 配置绑定 ──────────────────────────────────────────────────────────

def test_package_json_explicitly_disables_the_three_fuses():
    cfg = _fuse_config()
    assert cfg, "package.json 的 build.electronFuses 缺失 —— 产物会停在默认融合值"
    for key in MUST_DISABLE:
        assert cfg.get(key) is False, (
            f"electronFuses.{key} 不是显式 false（实为 {cfg.get(key)!r}）—— "
            "隐式默认会随 Electron 升级漂移，必须写死"
        )


def test_package_json_does_not_touch_startup_semantics_fuses():
    """三个**改启动语义**的 fuse 必须保持"未配置"（不盲改，见模块 docstring）。"""
    cfg = _fuse_config()
    for key in ("enableEmbeddedAsarIntegrityValidation", "onlyLoadAppFromAsar",
                "grantFileProtocolExtraPrivileges"):
        assert key not in cfg, (
            f"electronFuses.{key} 被设置了 —— 它改启动语义，而本环境无法验证 Electron "
            "能否启动；要先在真实终端验证再开"
        )


# ── B. 安全性：关掉不会破坏自身启动 ──────────────────────────────────────

def test_product_code_does_not_use_the_disabled_channels():
    hits: list[str] = []
    for p in _product_sources():
        text = p.read_bytes().decode("utf-8", errors="replace")
        for tok in _CHANNEL_TOKENS:
            if tok in text:
                hits.append(f"{p.relative_to(REPO)}: {tok}")
    assert not hits, (
        "产品代码引用了即将被关掉的通道 —— 关掉会破坏自身启动：\n  " + "\n  ".join(hits)
    )


def test_channel_scan_is_not_vacuous():
    """防空转：扫描确实覆盖到产物代码（文件数下限 + 哨兵文件在列）。"""
    files = _product_sources()
    assert len(files) >= 20, f"只扫到 {len(files)} 个产品源文件 —— 扫描口径塌缩"
    rels = {str(p.relative_to(REPO)).replace("\\", "/") for p in files}
    assert "electron/main.js" in rels, "electron/main.js 不在扫描范围（那正是要查的文件）"
    assert "api/jobs/upload.py" in rels, "api/ 未覆盖到"


# ── C. 产物绑定（无产物则 skip —— 与 test_bundle_manifest 的 asar 用例同口径）──

@pytest.mark.skipif(not _EXE.is_file(), reason="未构建产物（dist-electron 不存在）")
def test_packaged_binary_reflects_the_config():
    version, wire = _read_wire(_EXE)
    assert version == 1, f"fuse wire 版本 {version} != 1"
    assert len(wire) == 9, (
        f"fuse wire 长度变成 {len(wire)}（期望 9）—— `@electron/fuses` 的写入是"
        "**外科式**的：只改前 8 字节、不重写 count 字节。长度变了说明写入方式改了，"
        "需要重新核对该版本的实现"
    )
    for key, idx in MUST_DISABLE.items():
        assert wire[idx] == _DISABLE, (
            f"产物里 fuse #{idx}（{key}）是 0x{wire[idx]:02x}，期望 0x30（关闭）—— "
            "package.json 配了但产物没生效（构建漏了 fuses 步骤？）"
        )
    # 第 9 位必须**未被触碰**（证明不是整块重写）
    assert wire[_WASM_TRAP_HANDLERS] in (_ENABLE, _DISABLE, _REMOVED), "第 9 位是非法字节"
    assert wire[_WASM_TRAP_HANDLERS] != 0x00, "第 9 位被清零 —— 融合丝被截断/损坏"


def test_wire_reader_is_not_vacuous(tmp_path):
    """阳性 + 阴性对照：合成带哨兵的伪二进制，读回必须与写入**逐字节一致**。"""
    wire = bytes([0x31, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x31, 0x31])
    fake = tmp_path / "fake.exe"
    fake.write_bytes(b"\x00" * 32 + SENTINEL + bytes([1, len(wire)]) + wire + b"\x00" * 32)
    version, got = _read_wire(fake)
    assert version == 1, "版本字节解析错误"
    assert got == list(wire), f"读回 {got} 与写入 {list(wire)} 不一致 ⇒ 解析器不可信"

    bad = tmp_path / "not-electron.bin"
    bad.write_bytes(b"no sentinel here")
    with pytest.raises(AssertionError):
        _read_wire(bad)
