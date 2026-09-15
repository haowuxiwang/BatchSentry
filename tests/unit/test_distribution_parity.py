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
import re
import struct
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _pkg() -> dict:
    return json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))


def _deployment_md() -> str:
    return (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")


# ── 产物定位助手 ────────────────────────────────────────────────────


def _artifact_dirs():
    """所有 `dist-electron*/win-unpacked` 目录（含历史残留与安全软件占锁的备用目录）。"""
    return sorted(p for p in _ROOT.glob("dist-electron*/win-unpacked") if p.is_dir())


def _newest_artifact():
    """最新（按 mtime）的产物目录 —— **它就是本次要交付的那份**。

    这个选择规则本身就是护栏：`electron-builder` 会先清空再重建 `win-unpacked`，
    所以**构建失败后，最新目录恰好是那个残缺的**。用"最新"而不是"名字最标准的"
    去校验，失败构建才会当场变红，而不是被一个更早的完好目录掩盖过去。
    """
    dirs = _artifact_dirs()
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


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


def test_latest_artifact_is_a_complete_portable_package():
    """最新产物必须完整 —— 缺任何一件都不是可交付的便携版。"""
    d = _newest_artifact()
    if d is None:
        pytest.skip("无 Electron 产物（未打包或已清理），跳过实物校验")
    missing = _completeness_problems(d)
    assert not missing, (
        f"最新产物 {_label(d)} 不完整，缺少：{missing}\n"
        f"→ 这是**构建失败后的残缺目录**。此时它比任何完好目录都新，最容易被误当作"
        f"交付物压成 zip。请重跑 electron-builder 而不是分发它。"
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
