"""构建脚本不变式机检（2026-09-14 M8 定位到的两个真实坑）。

背景（实测）：
1. `build.ps1` 含大量中文注释，但文件是 **UTF-8 无 BOM**。Windows PowerShell
   5.1（`-File` / `& .\\script.ps1`）对无 BOM 的 UTF-8 按 ANSI 解码 → 中文注释
   字节被误读 → 语法解析失败（实测 10~11 处假错误）→ 脚本静默不执行。
   修复：脚本必须带 UTF-8 BOM。
2. `build.ps1` 里 PyInstaller 调用曾用 `Invoke-Native`（输出 Out-Null），
   FAIL 时无任何诊断信息（实测只看到 "PyInstaller build failed"）。
   修复：PyInstaller 输出重定向到 `build/pyinstaller.log` 并在失败时回显尾部。

本测试把这两个不变式 + build.bat 的转发约定机检化。
"""
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PS1 = _ROOT / "build.ps1"
_BAT = _ROOT / "build.bat"
_BOM = b"\xef\xbb\xbf"


@pytest.fixture(scope="module")
def ps1_bytes() -> bytes:
    return _PS1.read_bytes()


def test_build_ps1_exists(ps1_bytes):
    assert ps1_bytes, "build.ps1 不应为空"


def test_build_ps1_has_utf8_bom(ps1_bytes):
    """PS 5.1 兼容：含非 ASCII 的 .ps1 必须带 UTF-8 BOM，否则中文注释被
    误读为 ANSI 导致语法解析失败（脚本静默不执行）。"""
    assert ps1_bytes.startswith(_BOM), (
        "build.ps1 缺少 UTF-8 BOM —— Windows PowerShell 5.1 会按 ANSI 解码"
        "中文注释并报语法错误，脚本将静默不执行。请在文件头写入 "
        "EF BB BF。"
    )


def test_build_ps1_declares_native_stderr_guard(ps1_bytes):
    """PS 5.1 下 native stderr 会抛 NativeCommandError；脚本必须自带 EAP 降级封装。"""
    src = ps1_bytes.decode("utf-8-sig")
    assert "$ErrorActionPreference" in src
    assert "function Invoke-Native" in src


def test_build_ps1_pyinstaller_output_is_logged(ps1_bytes):
    """PyInstaller 调用必须落盘日志（失败可诊断），不得 Out-Null 黑洞。"""
    src = ps1_bytes.decode("utf-8-sig")
    assert "pyinstaller.log" in src, (
        "PyInstaller 输出应重定向到 build/pyinstaller.log，否则 FAIL 时无从查因"
    )
    # 不得再出现"调用后直接 Write-Fail 且无日志"的旧形态
    assert "PyInstaller build failed (完整日志" in src


def test_build_ps1_electron_output_is_logged(ps1_bytes):
    """electron-builder 调用同样必须落盘日志（其真实错误常在日志尾部，
    如 app.asar 被占用）。"""
    src = ps1_bytes.decode("utf-8-sig")
    assert "electron-builder.log" in src, (
        "electron-builder 输出应重定向到 build/electron-builder.log"
    )


def test_build_ps1_electron_lock_self_heals(ps1_bytes):
    """electron 产物被常驻进程占用时，构建脚本必须**自愈**而非整体失败。

    背景（M8 实测）：`dist-electron\\win-unpacked\\resources\\app.asar` 会被
    安全软件类常驻进程长期独占（可读、可复制，但删除与重命名均失败），
    重启应用亦不释放。旧行为只打印 WARN，随后 electron-builder 仍写标准
    目录 → 以 app-builder 的 Go 内部栈失败，整次构建报废。

    **契约在 2026-09-16 被实测修正**：原实现"切到固定名备用目录 +
    构建后 best-effort 归位"有两个缺陷，都来自同一份实测：
    - 固定名备用目录是**一次性的** —— `dist-electron-locked` 自己的 app.asar
      同样会被锁住（实测 `dist-electron` / `dist-electron-locked` /
      `dist-electron-v112` / `dist-electron-m8` **四个目录全部 PermissionError 32**），
      所以第二次自愈必然失败；
    - 直接 `Remove-Item` 仍被占用的旧标准目录会在删到被持有的文件时**中途失败**
      —— 失败前已经删掉一部分文件，把一个完好的旧产物变成**残缺目录**
      （比不归位更糟，还会让"最新产物"判定指向残缺目录）。

    修正后的不变式：
    1. 备用输出目录**每次唯一**（时间戳），不再使用固定名；
    2. 归位前必须**再探一次**占用，只有确认标准目录可写才 Remove-Item/Move-Item。
    """
    src = ps1_bytes.decode("utf-8-sig")
    assert "dist-electron-out-$(Get-Date -Format" in src, (
        "备用输出目录必须每次唯一（时间戳）—— 固定名备用目录用过一次，自己就被锁"
    )
    assert '$outDir = "dist-electron-locked"' not in src, (
        "不得再使用固定名备用目录（实测已失效）"
    )
    assert '"-c.directories.output=$outDir"' in src, (
        "备用输出目录必须经 electron-builder 的 -c.directories.output 传入"
    )
    assert "$canMoveBack" in src, (
        "归位前必须重探占用：直接 Remove-Item 被占用的旧目录会留下残缺产物"
    )
    assert "Move-Item" in src and "Remove-Item -Recurse -Force $stdUnpacked" in src, (
        "确认可写后应 best-effort 归位（删旧标准目录 + 移入新产物）"
    )


def test_build_bat_delegates_to_ps1():
    """项目约定：build.bat 只转发到 build.ps1，不在 .bat 内重复构建逻辑。"""
    src = _BAT.read_text(encoding="utf-8", errors="replace")
    assert "build.ps1" in src
    assert "powershell" in src.lower()
