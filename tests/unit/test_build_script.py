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
import re
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
    常驻进程长期独占（可读、可复制，但删除与重命名均失败），
    重启应用亦不释放。⚠️ **归因已于 2026-09-17 更正**：**不是安全软件**，是
    **宿主进程**按包打开 `.asar` 后留下未带 `FILE_SHARE_DELETE` 的句柄
    （见 `docs/PROJECT_PITFALLS.md` §二十二）。
    旧行为只打印 WARN，随后 electron-builder 仍写标准
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


def test_build_ps1_clean_preserves_run_ledger(ps1_bytes):
    """`-Clean` 不得整删 `build/` —— 否则把**本次运行自己的** start 台账抹掉。

    背景（2026-09-29 定位）：原实现是
    `Remove-Item -Recurse -Force dist, dist-electron, build`。而运行台账
    `build/_run/<runId>.start.json` 在脚本开头（`Set-Content -Path $startFile`）
    就已落盘，`build/` 又正好是它的父目录 ⇒ 整删会把「本次运行的开始记录」
    一起删掉。后果不是崩溃，而是**判据失效**：B9-6 靠「有 start 无 finish」
    识别"被 TerminateProcess"，一旦 start 被自己删掉，一次被杀的全量构建
    与「从未运行过」在证据上不可区分 —— 而 `-Clean` 恰恰是最需要该判据的场景。

    行为证据（对照实验，非本测试）：
    `devlogs/_verify/probe_build_clean_ledger.ps1` 在同一夹具上分别执行旧块与新块 ——
    旧块 `build/_run/…start.json 存活 = False`，新块 `= True`，且两者都清掉了
    `build/pbc-server`、`build/pyinstaller.log`、`dist/`、`dist-electron/`。
    """
    src = ps1_bytes.decode("utf-8-sig")
    assert "Remove-Item -Recurse -Force dist, dist-electron, build" not in src, (
        "不得整删 build/ —— 会连带删掉本次运行的 start 台账（见 docstring）"
    )
    assert '$_.Name -ne "_run"' in src, (
        "build/ 的清理必须显式排除 _run（运行台账目录）"
    )
    assert '$buildRootForClean = Join-Path $projectRoot "build"' in src, (
        "build/ 的清理应针对逐个子项，而不是整目录"
    )


def test_build_ps1_clean_still_deletes_dist_and_dist_electron(ps1_bytes):
    """防空转：`-Clean` 必须**仍然**清掉 dist/ 与 dist-electron/。

    没有这条，上面那条护栏可以用「干脆什么都不删」骗过去。

    另：必须是**逐条单路径**删除。多路径形式
    （`Remove-Item -Recurse -Force dist, dist-electron`）在受限宿主环境下的
    行为与正常 PowerShell 不一致（实测：前者静默不删，留下陈旧产物）。
    """
    src = ps1_bytes.decode("utf-8-sig")
    assert "Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue" in src, (
        "dist/ 仍须整删（陈旧 PyInstaller 产物必须清掉）"
    )
    assert "Remove-Item -Recurse -Force dist-electron -ErrorAction SilentlyContinue" in src, (
        "dist-electron/ 仍须整删（陈旧 Electron 产物会被 electron-builder 复用）"
    )
    assert "Remove-Item -Recurse -Force dist, dist-electron" not in src, (
        "不要用多路径形式：受限宿主环境下它会静默不删"
    )


def test_ledger_prune_does_not_rely_on_empty_pipeline_semantics(ps1_bytes):
    """台账裁剪不得把 `Remove-Item` 直接挂在管道尾端。

    背景（2026-09-29 定位）：原实现是
    `Get-ChildItem … | Sort-Object … | Select-Object -Skip 40 | Remove-Item -Force`。
    它依赖一个**隐晦且不对称**的行为：真实 `Remove-Item` 在**管道上下文**里
    收到 0 个对象时不抛，而**独立调用**缺 `-Path` 时抛
    「无法处理命令，因为一个或多个强制参数丢失: Path」。
    台账不足 40 个文件时管道恰好为空（实测本仓只有 10 个文件），
    于是同一份代码在不同调用方式/宿主包装器下表现不同 —— 排查成本极高。

    不变式：必须**显式收集**（`@(…)`）、**判空**（`.Count -gt 0`）、
    再以 `-LiteralPath` 删除。
    """
    src = ps1_bytes.decode("utf-8-sig")
    assert "$staleLedger = @(" in src, "台账裁剪必须先把待删项显式收集成数组"
    assert "if ($staleLedger.Count -gt 0) {" in src, (
        "台账裁剪必须判空 —— 不足 40 个文件时应是 no-op，而不是抛错"
    )
    assert "Remove-Item -LiteralPath $staleLedger.FullName" in src, (
        "应显式传 -LiteralPath，而不是依赖管道绑定"
    )
    assert not re.search(r"\|\s*\n\s*Remove-Item\b", src), (
        "不得把 Remove-Item 直接挂在管道尾端（空管道语义随环境而异）"
    )
    # 说明：上面这条刻意锚定「**行尾是 `|`、下一行行首是 `Remove-Item`**」的调用点，
    # 而不是裸的 `| Remove-Item` 子串 —— 本文件的注释里就引用了这个反面示例，
    # 用子串匹配会把注释判成违规（实测踩过：`AssertionError` 来自注释本身）。


def test_start_ledger_is_written_before_the_clean_block(ps1_bytes):
    """顺序不变式：start 台账的**写点**必须先于 `-Clean` 的删点。

    这条是上面两条的**原因**：如果先清理后写台账，整删 build/ 就无害了。
    断言锚定「调用点」而不是标识符 —— 注释里也会出现同样的名字。
    """
    src = ps1_bytes.decode("utf-8-sig")
    write_at = src.index("Set-Content -Path $startFile")
    clean_at = src.index("if ($Clean) {")
    assert write_at < clean_at, (
        "start 台账必须先落盘再执行 -Clean；否则 -Clean 一旦整删 build/，"
        "本次运行的开始记录就没了（B9-6 判据失效）"
    )


def test_build_bat_delegates_to_ps1():
    """项目约定：build.bat 只转发到 build.ps1，不在 .bat 内重复构建逻辑。"""
    src = _BAT.read_text(encoding="utf-8", errors="replace")
    assert "build.ps1" in src
    assert "powershell" in src.lower()
