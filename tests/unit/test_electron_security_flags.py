# -*- coding: utf-8 -*-
"""Electron 外壳的安全标志护栏（R78 第十八批，对抗性审查）。

由来：安全审查发现 `electron/main.js` 只设了 `contextIsolation: true` /
`nodeIntegration: false`，**没有**显式 `sandbox`。按 Electron 官方安全清单
（<https://www.electronjs.org/docs/latest/tutorial/security>）第 4 项
「Enable process sandboxing」，沙箱自 Electron 20 起**默认**开启，故当时**并非**
活漏洞；但"默认开着"是**隐式**的 —— `nodeIntegration: true` 会**连带**关掉沙箱，
而 `sandbox: false` 是完全静默的。本护栏把"沙箱开着"变成**机检锁定**的不变式。

判据刻意覆盖**每一个** `webPreferences` 块（本文件有两处：splash 窗口与主窗口）——
只查第一处会让第二处静默漂移（本项目"同一语义写两处必然漂移"）。

本文件只读 `electron/main.js`，**不**加载 Electron、**不**启动应用。
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_MAIN_JS = REPO / "electron" / "main.js"

#: 每个 `webPreferences: { ... }` 块（非贪婪到第一个 `}`）。
_BLOCK_RE = re.compile(r"webPreferences\s*:\s*\{(.*?)\}", re.S)

#: 必须为真的标志（值必须是字面量 `true` / `false`，不接受变量 —— 变量可被覆盖）。
_REQUIRED = {
    "contextIsolation": "true",
    "nodeIntegration": "false",
    "sandbox": "true",
}

#: 绝不允许出现的危险开关（官方清单的 "Bad" 配置）。
_FORBIDDEN = ("webSecurity: false", "allowRunningInsecureContent: true",
              "experimentalFeatures: true", "enableBlinkFeatures")


def _src() -> str:
    assert _MAIN_JS.is_file(), f"找不到 {_MAIN_JS}"
    return _MAIN_JS.read_text(encoding="utf-8")


def _blocks() -> list[str]:
    blocks = _BLOCK_RE.findall(_src())
    assert blocks, "electron/main.js 里找不到任何 webPreferences 块 —— 锚点失效"
    return blocks


def test_guard_reads_the_real_electron_main():
    """反空转：锚点/正则失效会让下面所有断言**恒真**。"""
    src = _src()
    assert len(src) > 10_000, "读到的不像 electron/main.js"
    assert "webPreferences" in src


def test_every_webpreferences_block_enables_the_sandbox():
    """两处窗口都必须显式 `sandbox: true`（官方清单第 4 项）。"""
    for i, blk in enumerate(_blocks()):
        assert re.search(r"\bsandbox\s*:\s*true\b", blk), (
            f"第 {i + 1} 个 webPreferences 块缺显式 `sandbox: true` —— "
            "隐式默认（Electron ≥20 为 true）会被 `nodeIntegration: true` 连带关掉"
        )


def test_isolation_flags_are_literal_not_variables():
    """`contextIsolation`/`nodeIntegration` 必须是**字面量**，不是变量。"""
    for i, blk in enumerate(_blocks()):
        for flag, want in (("contextIsolation", "true"), ("nodeIntegration", "false")):
            assert re.search(rf"\b{flag}\s*:\s*{want}\b", blk), (
                f"第 {i + 1} 个 webPreferences 块的 {flag} 不是字面量 {want}"
            )


def test_no_dangerous_electron_switches():
    """官方清单的 "Bad" 配置一律不得出现（整个文件范围内）。"""
    src = _src()
    hits = [bad for bad in _FORBIDDEN if bad in src]
    assert not hits, f"出现被 Electron 官方列为危险的开关：{hits}"


def test_webpreferences_blocks_are_exactly_two():
    """窗口数量变了要有人复核（splash + main）。多了/少了都提示更新本护栏。"""
    assert len(_blocks()) == 2, (
        f"webPreferences 块数 = {len(_blocks())}（期望 2）—— "
        "新增/删除窗口时请复核安全标志是否都设了"
    )
