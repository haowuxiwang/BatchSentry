"""分发一致性机检 —— 配置 ↔ 文档 ↔ 实物 三者必须说同一件事。

背景（2026-09-15 定位结论）
--------------------------
"能不能分发"曾经只能靠人读 `package.json` + `DEPLOYMENT.md` + 手翻产物目录来判断，
而这三者**没有任何机检把它们绑定**，于是天然会漂移：

* `package.json` 里存在一个调好的 `nsis` 配置块，但 `win.target` 只有 `dir` ——
  该块**永远不会被执行**。它是"死配置"：读起来像"本应用会产出安装包"，实际不会。
  只要有人据此写下"运行 Setup.exe"的交付说明，就会当场翻车。
* Electron 包通过 `extraResources` 把 PyInstaller 产物拷进
  `win-unpacked/resources/pbc-server/`。这条链路一旦改名/改路径，**打包仍然成功**，
  只是装进去一个空的或过期的服务端 —— 直到用户双击才暴露。

因此本文件把下列不变式机检化：
1. `win.target` 声明什么形态，`DEPLOYMENT.md` 就必须按同一形态指导分发（防文档漂移）；
2. 内嵌服务端的来源必须恒为 `dist/pbc-server`（打包链的唯一定义点）；
3. `npm run build` 必须串起 css → py → win（保证一次命令得到完整便携版）；
4. **产物在场时**（本机 / 发布机）额外校验：内嵌副本与 `dist/` 逐字节一致、
   `app.asar` 内版本号等于 `main.APP_VERSION`。CI 无产物时跳过（不制造假绿）。
"""
import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _pkg() -> dict:
    return json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))


def _deployment_md() -> str:
    return (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")


# ── 文档一致性：版本戳与构建开关 ────────────────────────────────────


def test_portable_readme_version_matches_app_version():
    """随包交付的 `PORTABLE_README.txt` 版本戳必须等于 `main.APP_VERSION`。

    它是**用户解压后第一份会读的文档**，却最容易漏改（实测停留在 v1.0.0 而
    产品已到 v1.1.2）。版本号只应有 `main.APP_VERSION` 一个真值来源。
    """
    from main import APP_VERSION

    txt = (_ROOT / "PORTABLE_README.txt").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"BatchSentry\s+v(\d+\.\d+\.\d+)", txt)
    assert m is not None, "PORTABLE_README.txt 里找不到 `BatchSentry vX.Y.Z` 版本戳"
    assert m.group(1) == APP_VERSION, (
        f"PORTABLE_README.txt 版本戳 v{m.group(1)} != main.APP_VERSION {APP_VERSION}"
    )


def test_documented_build_switches_exist_in_build_ps1():
    """README/DEPLOYMENT 里写到的 `-Xxx` 构建开关必须是 build.ps1 真的声明了的。

    反例（2026-09-15）：README 写 `-SkipCss`、脚本声明的是 `-SkipCSS`。PowerShell
    参数名大小写不敏感所以能跑，但文档与实现已经不一致 —— 一旦哪天换成大小写敏感的
    解释器或改名，文档就会静默失效。
    """
    ps1 = (_ROOT / "build.ps1").read_text(encoding="utf-8", errors="replace")
    declared = set(re.findall(r"\[switch\]\$(\w+)", ps1))
    assert declared, "未从 build.ps1 解析到任何 [switch] 开关（解析方式可能已过时）"

    mentioned = set()
    for doc in ("README.md", "DEPLOYMENT.md"):
        text = (_ROOT / doc).read_text(encoding="utf-8", errors="replace")
        for tok in re.findall(r"\.\\build\.ps1\s+(-[A-Za-z]+)", text):
            mentioned.add(tok[1:])
    unknown = sorted(m for m in mentioned if m not in declared)
    assert not unknown, (
        f"文档里提到的构建开关在 build.ps1 中不存在：{unknown}（已声明：{sorted(declared)}）"
    )


# ── 产物定位助手 ────────────────────────────────────────────────────


def _artifact_dirs():
    """所有 `dist-electron*/win-unpacked` 目录（含历史残留与外部句柄占锁的备用目录）。"""
    return sorted(p for p in _ROOT.glob("dist-electron*/win-unpacked") if p.is_dir())


def _build_time(d: Path) -> float:
    """产物的"生成时间" = 目录内**文件**的最新 mtime。

    ⚠️ **不要用目录自身的 mtime。** 目录 mtime 会被任何子项增删顶到"现在"：
    2026-09-15 实测，一次逐文件"改名再改回"的占锁探测，把两个残缺目录的
    `win-unpacked` mtime 刷成了同一分钟 —— 于是本文件跑到**残缺目录**上做校验，
    误报"最新产物不完整 / 版本是 1.1.1"。改名与内容不变时 mtime 不变，
    故取"目录内文件的最新 mtime"才是稳定的构建时间信号。
    """
    newest = 0.0
    for r, _dirs, fs in os.walk(d):
        for f in fs:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(r, f)))
            except OSError:
                pass
    return newest


def _artifacts_by_recency():
    """所有产物目录，按生成时间**新→旧**排序。"""
    return sorted(_artifact_dirs(), key=_build_time, reverse=True)


def _newest_artifact():
    """最新**完整**的产物目录 —— **它就是本次要交付的那份**。

    只取完整的：残缺目录无法交付，且本文件另有 `test_latest_build_is_complete`
    专门盯"最新那次构建是否失败"，职责不重叠。
    """
    for d in _artifacts_by_recency():
        if not _completeness_problems(d):
            return d
    return None


def _label(d: Path) -> str:
    """产物目录的完整可辨标识（`win-unpacked` 本身重名，必须带父目录）。"""
    return f"{d.parent.name}/{d.name}"


def _completeness_problems(d: Path):
    """返回该产物目录缺失的关键件清单（空 = 完整）。"""
    missing = []
    for rel in ("BatchSentry.exe",
                "resources/app.asar",
                "resources/pbc-server/pbc-server.exe"):
        if not (d / rel).is_file():
            missing.append(rel)
    return missing


# ── 3. 实物校验（产物在场时才跑）─────────────────────────────────────


def test_latest_build_is_complete():
    """**最新的一次构建**必须完整 —— 缺任何一件都不是可交付的便携版。

    判据刻意用"最新"而不是"名字最标准的"：`electron-builder` 先清空再重建
    `win-unpacked`，所以**构建失败后，最新的目录恰好是那个残缺的**。这样失败构建
    会当场变红，而不会被一个更早的完好目录掩盖过去。
    """
    arts = _artifacts_by_recency()
    if not arts:
        pytest.skip("无 Electron 产物（未打包或已清理），跳过实物校验")
    newest = arts[0]
    missing = _completeness_problems(newest)
    assert not missing, (
        f"最新产物 {_label(newest)} 不完整，缺少：{missing}\n"
        f"→ 这是**构建失败后的残缺目录**。此时它比任何完好目录都新，最容易被误当作"
        f"交付物压成 zip。请重跑 electron-builder 而不是分发它。"
    )


def test_artifact_recency_ignores_directory_mtime(tmp_path):
    """产物"谁最新"只认**文件** mtime，不认目录 mtime（回归护栏）。

    实测事故（2026-09-15）：逐文件占锁探测（改名再改回）刷新了两个残缺目录的
    `win-unpacked` mtime，本文件随即在残缺目录上做校验，误报"最新产物不完整 /
    版本是 1.1.1"。改名不产生内容变化，不该影响"谁是本次交付物"的判定。
    """
    import os as _os

    d = tmp_path / "win-unpacked"
    (d / "resources").mkdir(parents=True)
    exe = d / "BatchSentry.exe"
    exe.write_bytes(b"MZ")
    inner = d / "resources" / "app.asar"
    inner.write_bytes(b"\x00" * 8)

    file_t = 1_700_000_000
    _os.utime(exe, (file_t, file_t))
    _os.utime(inner, (file_t, file_t))
    # 把目录 mtime 顶到很久以后 —— 模拟"子项被增删过"
    dir_t = 1_900_000_000
    _os.utime(d, (dir_t, dir_t))
    _os.utime(d / "resources", (dir_t, dir_t))

    assert _build_time(d) == file_t, (
        "构建时间判定被目录 mtime 污染了 —— 任何子项增删都会顶高目录 mtime，"
        "会让测试在错误（残缺）的产物上做校验。"
    )


def test_latest_artifact_embeds_the_build_output_byte_for_byte():
    """最新产物内嵌的服务端必须与 `dist/pbc-server` 逐字节一致。

    这是"用户双击运行的那份 == 我们测过的那份"的唯一硬证据。若打包时把旧产物拷了
    进去（或拷漏了），打包日志仍是绿的 —— 只有比字节能发现。
    """
    d = _newest_artifact()
    if d is None:
        pytest.skip("无 Electron 产物（未打包或已清理），跳过实物校验")

    built = _ROOT / "dist" / "pbc-server" / "pbc-server.exe"
    if not built.is_file():
        pytest.skip("dist/pbc-server/pbc-server.exe 不在场，无法比对")

    embedded = d / "resources" / "pbc-server" / "pbc-server.exe"
    if not embedded.is_file():
        pytest.skip(f"{_label(d)} 不完整（无内嵌服务端），完整性由上一个用例负责")

    want = hashlib.md5(built.read_bytes()).hexdigest()
    got = hashlib.md5(embedded.read_bytes()).hexdigest()
    assert got == want, (
        f"{_label(d)} 内嵌的 pbc-server.exe 与 dist/ 不一致\n"
        f"  dist: {want}\n  内嵌: {got}\n"
        f"→ 桌面产物装的是**另一份**服务端，用户运行的不是我们测过的版本。"
    )


def _asar_read(asar: Path, member: str) -> bytes:
    """从 app.asar 读出指定成员。

    布局：``UInt32LE(4) | UInt32LE(dataSize) | JSON 索引 | 数据``。
    数据基址 = ``8 + dataSize`` —— 注意**不要**用 ``json_start + JSON 文本长度``
    推算：索引后有 2 字节对齐填充，那个算法会差 2 字节，读出来的成员 JSON 静默错位
    （症状是 `json.loads` 报 "Expecting ',' delimiter"）。
    """
    raw = asar.read_bytes()
    json_start = raw.index(b'{"files":')
    data_size = struct.unpack("<I", raw[4:8])[0]
    base = 8 + data_size
    obj, _ = json.JSONDecoder().raw_decode(raw[json_start:].decode("utf-8", "replace"))
    node = obj["files"]
    for part in member.split("/"):
        node = node[part]
    off, size = int(node["offset"]), int(node["size"])
    return raw[base + off: base + off + size]


def test_packaged_app_version_matches_app_version():
    """app.asar 内版本必须等于 `main.APP_VERSION`，且入口指向 electron/main.js。

    服务端 `/health` 报的版本来自 main.py；Electron 侧版本来自 package.json。
    两者若不同，用户看到的"版本"取决于他看哪里 —— 排查问题时极具误导性。
    """
    d = _newest_artifact()
    if d is None:
        pytest.skip("无 Electron 产物（未打包或已清理），跳过实物校验")
    asar = d / "resources" / "app.asar"
    if not asar.is_file():
        pytest.skip(f"{_label(d)} 不完整（无 app.asar）")

    from main import APP_VERSION

    pj = json.loads(_asar_read(asar, "package.json").decode("utf-8"))
    assert pj.get("version") == APP_VERSION, (
        f"{_label(d)} 的 app.asar 内版本 {pj.get('version')!r} != "
        f"main.APP_VERSION {APP_VERSION!r}"
    )
    assert pj.get("main") == "electron/main.js", (
        f"{_label(d)} 的 asar 入口异常：{pj.get('main')!r}"
    )



# ── 1. 分发形态：配置 ↔ 文档 ─────────────────────────────────────────


def test_win_target_declares_portable_dir_only():
    """默认 win.target 只产出免安装目录。

    这不是"缺了安装包"，而是**有意的分发形态**（见 DEPLOYMENT.md「便携版分发」）：
    无需安装、无需管理员权限、解压即用。若将来改为产出安装包，会连带要求代码签名
    与首次运行白名单指引 —— 故此处显式钉住，改动即失败，逼人更新文档与决策。
    """
    target = _pkg()["build"]["win"]["target"]
    assert target == [{"target": "dir", "arch": ["x64"]}], (
        f"win.target 变为 {target!r}。若确实要改分发形态：请同步更新 DEPLOYMENT.md，"
        f"并确认安装包路径已做过端到端验证（本仓库尚无签名证书）。"
    )


def test_deployment_doc_matches_declared_distribution_form():
    """DEPLOYMENT.md 指导的分发方式，必须与 `win.target` 声明的形态一致。"""
    target = _pkg()["build"]["win"]["target"]
    targets = {t["target"] for t in target} if isinstance(target, list) else {target}
    doc = _deployment_md()

    if "nsis" in targets:
        assert re.search(r"安装包|Setup\.exe|\.blockmap", doc), (
            "win.target 含 nsis，但 DEPLOYMENT.md 未指导如何使用安装包。"
        )
    else:
        # 免安装形态：文档必须说明"文件夹便携版"，且不得指导用户运行安装包
        assert "win-unpacked" in doc, "DEPLOYMENT.md 未说明便携版产物目录（win-unpacked/）"
        assert "BatchSentry.exe" in doc, "DEPLOYMENT.md 未说明启动入口 BatchSentry.exe"
        assert not re.search(r"双击\s*Setup\.exe|运行\s*Setup\.exe", doc), (
            "DEPLOYMENT.md 指导运行 Setup.exe，但 win.target 不产出安装包 —— "
            "这会让用户去找一个不存在的文件。"
        )


def test_nsis_block_is_documented_as_inactive():
    """`nsis` 配置块存在但未被 target 启用时，文档必须明确说明"当前不产出安装包"。

    JSON 不支持注释，无法在 package.json 内就地标注，所以把这条说明**落在文档里**
    并由本用例守住 —— 否则该配置块就是一个会误导人的死配置。
    """
    target = _pkg()["build"]["win"]["target"]
    targets = {t["target"] for t in target} if isinstance(target, list) else {target}
    if "nsis" in targets:
        pytest.skip("nsis 已启用，无需说明其未启用")
    assert "nsis" in _pkg()["build"], "预期 package.json 保留了调好的 nsis 配置块"
    doc = _deployment_md()
    assert re.search(r"不产出安装包|不包含安装包|未启用\s*nsis|nsis[^\n]{0,20}未启用", doc), (
        "package.json 有 nsis 配置块但未被 target 启用（死配置），"
        "DEPLOYMENT.md 必须显式说明当前分发不含安装包，避免用户寻找 Setup.exe。"
    )


# ── 2. 打包链的唯一定义点 ───────────────────────────────────────────


def test_extra_resources_embed_pyinstaller_output():
    """Electron 包内嵌的服务端必须来自 `dist/pbc-server`（打包链的定义点）。"""
    er = _pkg()["build"]["extraResources"]
    entry = next((e for e in er if e.get("to") == "pbc-server"), None)
    assert entry is not None, (
        f"extraResources 中没有 to=pbc-server 的条目：{er!r} —— "
        f"桌面产物将不含服务端，双击后必然起不来。"
    )
    assert entry["from"] == "dist/pbc-server", (
        f"内嵌服务端来源从 dist/pbc-server 改成了 {entry['from']!r}；"
        f"PyInstaller 的输出目录是 dist/pbc-server（见 pbc-server.spec / build.ps1）。"
    )
    assert entry.get("filter") == ["**/*"], "extraResources 必须整体拷贝，不得过滤掉依赖"


def test_electron_main_is_packaged():
    assert _pkg()["build"]["files"] == ["electron/main.js"], (
        "build.files 应只打包 electron/main.js（js 依赖被一并内联）；"
        "多打或少打都会改变产物内容。"
    )


def test_npm_build_chains_css_py_and_win():
    """`npm run build` 必须一次串出完整便携版（漏掉任一步都会产出残缺包）。"""
    scripts = _pkg()["scripts"]
    assert scripts["build"] == "npm run build:css && npm run build:py && npm run build:win", (
        f"npm run build 的串联顺序变了：{scripts['build']!r}"
    )
    for step in ("build:css", "build:py", "build:win"):
        assert step in scripts, f"缺少 {step}"


def test_e2e_drivers_can_target_the_shipped_artifact():
    """端到端驱动必须能指向**将要分发的那份**（Electron 内嵌副本）。

    否则 e2e 只测 `dist/pbc-server`（PyInstaller 直接产物），而用户双击运行的是
    `win-unpacked/resources/pbc-server/` 里的副本 —— "测了 A、发了 B"。三者都曾
    把路径写死，故此处钉住"必须支持覆盖"。
    """
    drivers = ("tests/e2e_frozen.py", "tests/e2e_manual.py", "e2e_run.py")
    missing = []
    for rel in drivers:
        src = (_ROOT / rel).read_text(encoding="utf-8")
        # 两种等价实现都算过关：直接读 PBC_E2E_EXE，或复用 tests.e2e_proc.resolve_exe()
        if "PBC_E2E_EXE" not in src and "resolve_exe" not in src:
            missing.append(rel)
    assert not missing, (
        f"以下 e2e 驱动无法指向要分发的产物副本"
        f"（应支持 PBC_E2E_EXE 覆盖或复用 tests.e2e_proc.resolve_exe）：{missing}"
    )


def test_portable_readme_concurrency_matches_code_default():
    """PORTABLE_README.txt 的"并发处理 N 个任务"必须等于代码默认（防漂移）。

    实测（2026-10-10）：文档写 3，代码已是 5（`api/jobs/__init__.py`）——
    版本戳有 `test_portable_readme_version_matches_app_version` 守着，并发数
    却没有任何机检，于是**静默腐坏了整整一个批次**的交付说明。默认值从源码
    **重算**（不写死字面量），与"文档复述代码事实却无人绑定"的教训对齐。
    """
    src = (_ROOT / "api" / "jobs" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'os\.getenv\(\s*"MAX_CONCURRENT_JOBS"\s*,\s*"(\d+)"\s*\)', src)
    assert m, (
        "未从 api/jobs/__init__.py 解析到 MAX_CONCURRENT_JOBS 默认值"
        "（解析方式可能已过时）"
    )
    default = m.group(1)
    txt = (_ROOT / "PORTABLE_README.txt").read_text(encoding="utf-8", errors="replace")
    m2 = re.search(r"并发处理\s*(\d+)\s*个任务", txt)
    assert m2, "PORTABLE_README.txt 未声明并发任务数（用户解压后第一份会读的文档）"
    assert m2.group(1) == default, (
        f"PORTABLE_README.txt 写「并发处理 {m2.group(1)} 个任务」，"
        f"而代码默认是 {default}（api/jobs/__init__.py）—— "
        f"交付说明与实际不符。改代码默认值时必须同步改文档。"
    )


# ── 随包文档的数字必须与代码同源（R86 第二十六批新增）─────────────────
#
# 由来（2026-10-10）：`PORTABLE_README.txt` 是**用户解压后第一份会读的文档**，
# 却长期游离在机检之外 —— 版本戳与并发数有护栏，其余数字没有。实测查出 4 处
# 与代码相反/无出处的说法：
#   * 只写「PDF 最大 200MB」，**漏了页数上限**（config 注释：两者正交）；
#   * 「启动超时（>30秒）」而 `READY_TIMEOUT_MS = 180_000`；
#   * 「51 页…约 10-15 分钟」而单一真值 32.7 s/页 ⇒ 约 28 分钟（差 2~3 倍）；
#   * 「DeepSeek 默认 50 RPM」全仓无出处，且"请联系技术支持下调并发"是死指引。
# 这里把**其余会被用户当真的数字**一并绑定到单一真值上。


def _readme_txt() -> str:
    return (_ROOT / "PORTABLE_README.txt").read_text(encoding="utf-8", errors="replace")


def test_portable_readme_page_limit_matches_policy():
    """页数上限必须与 `UPLOAD_LIMITS` 同源。

    只写体积上限会漏掉"低密度但极长的 PDF"：50KB/页的排版件能塞进 200MB
    数千页，而体积检查毫无察觉（见 config.UPLOAD_LIMITS 上方推导）。
    """
    from config import UPLOAD_LIMITS

    txt = _readme_txt()
    assert f"不超过 {UPLOAD_LIMITS['max_pages']} 页" in txt, (
        f"PORTABLE_README.txt 未声明页数硬上限 {UPLOAD_LIMITS['max_pages']} 页"
    )
    assert f"超过 {UPLOAD_LIMITS['warn_pages']} 页" in txt, (
        f"PORTABLE_README.txt 未声明软告警阈值 {UPLOAD_LIMITS['warn_pages']} 页"
    )


def test_portable_readme_startup_timeout_matches_electron():
    """启动等待上限必须等于 `electron/main.js` 的 `READY_TIMEOUT_MS`。

    实测漂移：文档写「>30秒」，代码早已是 180s（`READY_TIMEOUT_MS = 180_000`，
    报错文案也写 "within 180s"）。用户会以为 30 秒没起来就是坏了，转而杀进程。
    """
    src = (_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    m = re.search(r"READY_TIMEOUT_MS\s*=\s*(\d[\d_]*)\s*;", src)
    assert m, "未从 electron/main.js 解析到 READY_TIMEOUT_MS（解析方式可能已过时）"
    seconds = int(m.group(1).replace("_", "")) // 1000
    txt = _readme_txt()
    assert f"等待上限 {seconds} 秒" in txt, (
        f"PORTABLE_README.txt 的启动等待上限与 READY_TIMEOUT_MS（{seconds}s）不符"
    )


def test_portable_readme_per_page_estimate_matches_single_source():
    """文档里的「秒/页」与「N 页约 M 分钟」必须能由 `sec_per_page_total` 复算。

    实测漂移：文档写「51 页约 10-15 分钟」，而单一真值 32.7 s/页 ⇒ 约 28 分钟；
    docs/UPLOAD_LIMITS.md §2 的实测全链是 1,889,115 ms（≈31.5 分钟）。差 2~3 倍
    ⇒ 用户会在第 20 分钟以为程序卡死。示例页数从文档自身解析，改示例也受检。
    """
    from config import UPLOAD_LIMITS

    v = float(UPLOAD_LIMITS["sec_per_page_total"])
    txt = _readme_txt()

    m = re.search(r"约\s*([\d.]+)\s*秒/页", txt)
    assert m, "PORTABLE_README.txt 未给出「秒/页」量级"
    assert abs(float(m.group(1)) - v) < 0.05, (
        f"PORTABLE_README.txt 写 {m.group(1)} 秒/页，单一真值 sec_per_page_total={v}"
    )

    m2 = re.search(r"(\d+)\s*页约\s*(\d+)\s*分钟", txt)
    assert m2, "PORTABLE_README.txt 未给出「N 页约 M 分钟」的量级示例"
    pages, claimed = int(m2.group(1)), int(m2.group(2))
    expected = int(pages * v / 60 + 0.5)
    assert claimed == expected, (
        f"PORTABLE_README.txt 写「{pages} 页约 {claimed} 分钟」，"
        f"按 sec_per_page_total={v} 复算应为 {expected} 分钟"
    )


def test_portable_readme_does_not_reinstate_refuted_claims():
    """已被否证的说法不得回流（与 test_claude_md_* 同款做法）。

    - `10-15 分钟`：与单一真值及实测差 2~3 倍；
    - `50 RPM`：全仓无出处（grep 只命中它自己）—— "别把借来的数字当自己的事实"；
    - `>30秒`：与 `READY_TIMEOUT_MS=180s` 相反。
    """
    txt = _readme_txt()
    for banned in ("10-15 分钟", "50 RPM", ">30秒"):
        assert banned not in txt, f"PORTABLE_README.txt 出现已被否证的说法：{banned!r}"


# ── 分发形态：**其他**文档也不得宣称配置产不出的产物（2026-10-13 补）─────


def test_other_docs_do_not_claim_a_single_file_portable_artifact():
    """`CONTRIBUTING.md` / `ARCHITECTURE.md` 不得宣称**配置产不出**的产物名。

    背景（2026-10-13 实测）：`test_deployment_doc_matches_declared_distribution_form`
    只守 `DEPLOYMENT.md`；而 `CONTRIBUTING.md` 的「分发流程」与 `ARCHITECTURE.md`
    §2.2 长期写着单文件便携版产物名（`BatchSentry-Portable-<ver>.exe`）—— 而
    `win.target` 只有 `dir`，**根本不会产出那个 exe**。照文档走的人会去找一个不存在
    的文件；且它是**静默**的（构建成功、产物齐备，只是名字对不上）。

    判据 = **配置里没有 portable target ⇒ 文档不得出现单文件便携版产物名**。
    只禁 `.exe` 形态、**不禁** `BatchSentry-Portable-` 前缀 —— 分发 zip 名
    （`BatchSentry-Portable-<ver>-<date>.zip`）正当使用该前缀。
    将来真启用 `portable` target 时本用例自动放行（前置 return），不阻碍改形态。
    """
    target = _pkg()["build"]["win"]["target"]
    targets = {t["target"] for t in target} if isinstance(target, list) else {target}
    if "portable" in targets:
        return
    for name in ("CONTRIBUTING.md", "ARCHITECTURE.md"):
        doc = (_ROOT / name).read_text(encoding="utf-8")
        m = re.search(r"BatchSentry-Portable-[\w.\-]*\.exe", doc)
        assert m is None, (
            f"{name} 提到单文件便携版产物名 {m.group(0)!r}，但 "
            f"win.target={sorted(targets)} 不产出它 ⇒ 文档与配置矛盾。"
            f"分发形态是**文件夹便携版**：跑 `python scripts/pack_portable.py --apply`。"
        )
        assert "单文件便携版" not in doc, (
            f"{name} 仍称「单文件便携版」，但 win.target={sorted(targets)} 只有目录形态。"
        )

# ── 打包图标：声明了就必须是「真实、可用、不被忽略」的资产（2026-10-13 补）──
#
# 由来（2026-10-13 实测）：图标管线**两处都没接线**，且失败是静默的 ——
#   * `electron/main.js` 找 `electron/icon.ico`（不存在）；
#   * electron-builder 默认找根 `build/icon.ico`（不存在；且根 `build/` 是
#     PyInstaller workpath、会被 `--clean` 清掉 ⇒ **默认位置不可用**）；
#   * 唯一在盘的是 `electron/build/icon.ico` = 190 B / 16x16 / 1-bpp 占位
#     （< electron-builder 的 256x256 下限），还被**未锚定**的 `.gitignore` 吃掉。
# ⇒ 产物用 Electron 默认图标，而**没有任何东西**会发现（构建成功、产物齐备）。
#
# 本类钉住的是**声明后**的不变式：一旦 `build.win.icon` 被声明，它就必须是
# 真实、够大、且在版本控制里（不被忽略）的资产，并与 `main.js` 的运行时图标**同源**。
# 现状是**未声明** ⇒ 走 `pytest.skip` 并指向 `docs/TODO.md` 0-39（诚实标注，非假绿）；
# 同时用**无条件**的正负对照证明检测器不空转（本仓库铁律：「用例存在」≠「断言有效」）。

_ICO_DIR_ENTRY = struct.Struct("<BBBBHHII")
_ICO_MIN_DIM = 256


def _ico_problems(raw: bytes) -> list[str]:
    """按 ICO **目录头**检查「够不够用」：≥1 个 ≥256x256 条目，且载荷在文件内。

    ⚠️ **只查目录头与载荷边界，不解码像素** —— 目的是挡住「16x16 占位当图标用」
    这一类（electron-builder 会因此**直接构建失败**）。可解码性由构建期兜底，
    此处刻意不做（无 PIL 依赖、确定性、秒级）。
    """
    if len(raw) < 6:
        return [f"文件过短（{len(raw)} B），不是 ICO"]
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    if reserved != 0 or kind != 1 or count == 0:
        return [f"不是 ICO（reserved={reserved} type={kind} count={count}）"]

    problems: list[str] = []
    biggest = 0
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(raw):
            problems.append(f"第 {i} 个目录项越界（头声明 {count} 项）")
            break
        w, h, _c, _r, _p, _b, size, offset = _ICO_DIR_ENTRY.unpack(raw[off:off + 16])
        w, h = w or 256, h or 256          # ICO 用 0 表示 256
        biggest = max(biggest, min(w, h))
        if offset + size > len(raw):
            problems.append(
                f"第 {i} 项载荷越界（offset={offset} size={size} 文件={len(raw)}）"
            )
    if biggest < _ICO_MIN_DIM:
        problems.append(
            f"最大边长 {biggest} < {_ICO_MIN_DIM}（electron-builder 会拒绝构建）"
        )
    return problems


def _packaging_icon(pkg: dict, repo: Path):
    """返回 `(图标绝对路径 | None, 说明)` —— 未声明 `win.icon` 时路径为 `None`。

    electron-builder 把 `win.icon` 解释为**相对 `directories.buildResources`**
    （默认 `build`）的路径。此处如实复刻该语义，不另立一套。
    """
    build = pkg.get("build", {})
    win_icon = build.get("win", {}).get("icon")
    if not win_icon:
        return None, "package.json 的 build.win.icon 未声明"
    res = build.get("directories", {}).get("buildResources", "build")
    return (repo / res / win_icon).resolve(), f"buildResources={res!r}"


def _icon_wiring_problems(pkg: dict, repo: Path, *, is_ignored=None) -> list[str]:
    """纯函数：给定 `package.json` 与仓库根，返回图标接线的全部问题（空 = 合格）。

    `is_ignored` 为可注入的「该相对路径是否被忽略清单吃掉」判定（默认不做该检查，
    以便在非 git 目录里做合成对照）。**注入点存在的原因**是「资产被忽略」这一分支
    当前**无法**在真实仓库上触发（没有合格资产可放），只能靠合成对照覆盖。
    """
    path, why = _packaging_icon(pkg, repo)
    if path is None:
        return [f"未声明打包图标（{why}）"]

    problems: list[str] = []
    if not path.is_file():
        problems.append(f"声明的图标不存在：{path}")
        return problems

    problems.extend(_ico_problems(path.read_bytes()))

    if is_ignored is not None:
        try:
            rel = path.relative_to(repo.resolve()).as_posix()
        except ValueError:
            rel = None
        if rel and is_ignored(rel):
            problems.append(
                f"图标被忽略清单吃掉（{rel}）⇒ 资产进不了版本控制，下一次重建就丢"
            )
    return problems


def _main_js_icon_path(src: str, repo: Path):
    """从 `electron/main.js` 解析运行时图标路径（`iconPath = path.join(__dirname, ...)`）。"""
    m = re.search(r"iconPath\s*=\s*path\.join\(\s*__dirname\s*,([^;]*)\)", src)
    if not m:
        return None
    parts = re.findall(r'"([^"]+)"', m.group(1))
    if not parts:
        return None
    return (repo / "electron" / Path(*parts)).resolve()


class TestPackagingIconIsWired:
    """图标一旦被声明，就必须「真实 + 够大 + 不被忽略 + 与运行时同源」。"""

    def test_declared_icon_is_real_and_not_ignored(self):
        pkg = _pkg()
        path, why = _packaging_icon(pkg, _ROOT)
        if path is None:
            pytest.skip(
                f"{why} —— 图标管线未接线（见 docs/TODO.md 0-39，待真实资产）。"
                f"本类的正负对照仍无条件运行，证明检测器不空转。"
            )

        def _git_ignored(rel: str) -> bool:
            try:
                p = subprocess.run(
                    ["git", "check-ignore", "--", rel],
                    cwd=str(_ROOT), capture_output=True,
                )
            except OSError:
                pytest.skip("git 不可用，跳过「是否被忽略」检查")
            return p.returncode == 0

        problems = _icon_wiring_problems(pkg, _ROOT, is_ignored=_git_ignored)
        assert not problems, (
            f"打包图标不合格：{problems}\n"
            f"→ electron-builder 需要 ≥{_ICO_MIN_DIM}x{_ICO_MIN_DIM} 的多尺寸 .ico，"
            f"且资产必须进版本控制。修法与退出条件见 docs/TODO.md 0-39。"
        )

    def test_runtime_and_packaging_icon_agree(self):
        """`main.js` 的窗口图标与 electron-builder 的打包图标必须**同一个文件**。

        实测（2026-10-13）：`main.js` 找 `electron/icon.ico`、builder 找
        `build/icon.ico` —— 两处**都不是**磁盘上那个 `electron/build/icon.ico`。
        两个消费者各指一处，是这条缺陷最容易被漏看的地方。
        """
        pkg = _pkg()
        path, _why = _packaging_icon(pkg, _ROOT)
        if path is None:
            pytest.skip("build.win.icon 未声明（见 docs/TODO.md 0-39）")

        src = (_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
        runtime = _main_js_icon_path(src, _ROOT)
        assert runtime is not None, (
            "electron/main.js 里找不到 `iconPath = path.join(__dirname, ...)` —— "
            "运行时图标接线被摘掉了？"
        )
        assert runtime == path, (
            f"运行时图标 {runtime} != 打包图标 {path} ⇒ 用户窗口看到的与安装包嵌入的"
            f"不是同一个图标。两处必须指向同一文件。"
        )

    # ── 无条件正负对照：证明上面两条的检测器真的会红/会绿 ──────────────

    @staticmethod
    def _ico(w: int, h: int, *, bpp: int = 1) -> bytes:
        """构造一个**头与载荷自洽**的最小 ICO（1-bpp BMP-in-ICO，与真实占位同构）。"""
        row = ((w * bpp + 31) // 32) * 4
        and_row = ((w + 31) // 32) * 4
        payload = 40 + row * h + and_row * h
        head = struct.pack("<HHH", 0, 1, 1) + struct.pack(
            "<BBBBHHII", w if w < 256 else 0, h if h < 256 else 0, 0, 0, 1, bpp, payload, 22
        )
        return head + b"\x00" * payload

    def test_checker_accepts_a_256px_icon(self, tmp_path: Path):
        f = tmp_path / "icon.ico"
        f.write_bytes(self._ico(256, 256))
        assert _ico_problems(f.read_bytes()) == [], "256x256 的合格 ICO 被判不合格"

    def test_checker_rejects_the_16px_placeholder(self, tmp_path: Path):
        """阳性对照：**当前盘上那个 190 B 占位**必须被判不合格。"""
        f = tmp_path / "icon.ico"
        f.write_bytes(self._ico(16, 16))
        problems = _ico_problems(f.read_bytes())
        assert problems and any("16" in p for p in problems), problems

    def test_checker_rejects_a_truncated_payload(self, tmp_path: Path):
        raw = self._ico(256, 256)
        problems = _ico_problems(raw[: len(raw) - 4096])   # 砍掉尾部 ⇒ 载荷越界
        assert problems and any("越界" in p for p in problems), problems

    def test_checker_rejects_non_ico_bytes(self):
        assert _ico_problems(b"MZ\x90\x00\x03\x00") != []
        assert _ico_problems(b"\x00\x01") != []

    def test_checker_flags_an_ignored_icon(self, tmp_path: Path):
        """「资产被忽略」分支：注入一个恒真的忽略判定 ⇒ 必须报出来。

        该分支在真实仓库上**当前无法**触发（没有合格资产），故只能合成覆盖 ——
        这正是 `is_ignored` 注入点存在的理由。
        """
        repo = tmp_path
        (repo / "build").mkdir()
        (repo / "build" / "icon.ico").write_bytes(self._ico(256, 256))
        pkg = {"build": {"win": {"icon": "icon.ico"}}}
        assert _icon_wiring_problems(pkg, repo) == []
        problems = _icon_wiring_problems(pkg, repo, is_ignored=lambda rel: True)
        assert problems and any("忽略清单" in p for p in problems), problems

    def test_checker_flags_a_missing_icon(self, tmp_path: Path):
        pkg = {"build": {"win": {"icon": "nope.ico"}}}
        problems = _icon_wiring_problems(pkg, tmp_path)
        assert problems and any("不存在" in p for p in problems), problems

    def test_icon_path_honours_build_resources(self, tmp_path: Path):
        pkg = {"build": {"directories": {"buildResources": "electron/build"},
                         "win": {"icon": "icon.ico"}}}
        path, why = _packaging_icon(pkg, tmp_path)
        assert path == (tmp_path / "electron" / "build" / "icon.ico").resolve(), (path, why)

    def test_main_js_icon_path_is_parsed_from_the_real_source(self):
        """解析器对**真实** `main.js` 必须能取到路径（防解析式失效后静默变空）。"""
        src = (_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
        got = _main_js_icon_path(src, _ROOT)
        assert got is not None, "解析器在真实 main.js 上取不到 iconPath —— 解析式已过时"
        assert got.name == "icon.ico", got
