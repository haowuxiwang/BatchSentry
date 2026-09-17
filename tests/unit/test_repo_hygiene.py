"""仓库卫生机检 —— 防「生成物进版本控制」与「规则 / 忽略清单互相不一致」。

## 为什么需要这个文件

`.gitignore` 只挡**默认**的 `git add`。它挡不住三种情形：

1. `git add -f`（或被 `--force` 的批量脚本）；
2. `.gitignore` 被误改 / 漏改；
3. **新增了一个"允许 agent 写入的产物落点"，却没登记进忽略清单** ——
   `release-archive/` 正是这样漏过一次：`CLAUDE.md`「Repo hygiene」规则 2
   明确指示"人为留存历史版本 → 打包 zip 到 `release-archive/`"，而忽略清单里
   没有它。规则让 agent 往那儿写、忽略清单却不拦，agent 会**照规则做**，
   于是几百 MB 的 zip 出现在 `git status` 里，下一次 `git add -A` 就进去了。

前两种是操作失误，第三种是**规则自身不闭合** —— 后者才是这个文件主要盯的：
凡是"文档里让 agent 写入的落点"，必须同时被忽略清单覆盖，并被门禁盯住。

## 锁住的四条不变式

A. **忽略清单实测覆盖**每个登记在案的落点（问 `git check-ignore` 的**行为**，
   不读 `.gitignore` 的**文本** —— 本项目反复踩过"断言序列化后的字符"的坑）；
B. **门禁与忽略清单双向同步**：登记表里的每个落点都要落进
   `release_gate.BUILD_OUTPUT_DIRS/PREFIXES`，否则门禁盯不住它；
C. **门禁确实注册了该检查**（防重构时被摘掉，而"检查消失"不会让任何用例变红）；
D. **判定谓词不过度匹配**（`dist-electron` 前缀不得命中 `dist-electronica/`，
   只看第一段路径 —— `docs/dist/x` 不是产物）。

## 加一个新落点时要改哪三处

1. 本文件的 `DOCUMENTED_ARTIFACT_ROOTS`（登记 + 写明它出自哪条规则）；
2. `.gitignore`；
3. `scripts/release_gate.py` 的 `BUILD_OUTPUT_DIRS` / `BUILD_OUTPUT_PREFIXES`。

三者缺一，A 或 B 会红 —— 这就是"三处联动"的强制点。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_RG_PATH = REPO / "scripts" / "release_gate.py"


def _load_release_gate():
    """从文件路径加载 `scripts/release_gate.py`。

    模块名刻意与 `test_release_gate.py` 区分（`release_gate_hygiene`）：
    `dataclass` 装饰器会查 `sys.modules[cls.__module__]`，两个测试模块若用同一个
    模块名注册会互相覆盖。
    """
    spec = importlib.util.spec_from_file_location("release_gate_hygiene", _RG_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["release_gate_hygiene"] = mod
    spec.loader.exec_module(mod)
    return mod


rg = _load_release_gate()


# ── 登记表：文档里明确允许 agent 写入的产物落点 ──────────────────────────────
# 每项都要在 .gitignore 里有一行、在 release_gate 的清单里有一项。
DOCUMENTED_ARTIFACT_ROOTS: dict[str, str] = {
    "dist": "PyInstaller 后端产物；electron-builder 的 extraResources 输入",
    "dist-electron": "Electron 打包标准输出（含 -locked/-m8/-v112/-out-* 变体）",
    "release-archive": "规则 2：人为留存的历史版本 zip 落点",
    "devlogs": "规则 6：门禁报告与 e2e 日志（结论必须引用它们，但日志本身不入库）",
    "build": "构建中间产物",
    "spike": "CLAUDE.md『Subdirectories』：一次性实验输入与报告（明确不属于应用）",
}


def _top_level_dirs() -> list[str]:
    """仓库根目录下的所有目录名（含隐藏目录，`.git` 由调用方排除）。"""
    return sorted(p.name for p in REPO.iterdir() if p.is_dir())


# 探测用的文件名由**本模块**统一决定。调用方只给"根"，不给完整路径 ——
# 这样"探针名写错 → 查表落空 → 静默判 False"这一类错误在类型上不可能发生
# （该缺陷真实发生过：一个调用方用 `__probe__.tmp`、预计算用的是
# `__hygiene_probe__.tmp`，于是忽略判定悄悄变成恒假）。
_PROBE_NAME = "__hygiene_probe__.tmp"
# 变体目录名的样例（`dist-electron` 前缀会漂移成 `dist-electron-out-<ts>` 等）
_VARIANT_SAMPLE_SUFFIX = "-out-20260101-000000"


def _roots_to_probe() -> list[str]:
    """所有值得探测的"根"：登记表落点 + 现实中的每个顶层目录 + 每个前缀的变体样例。"""
    roots = set(DOCUMENTED_ARTIFACT_ROOTS) | set(_top_level_dirs())
    for prefix in rg.BUILD_OUTPUT_PREFIXES:
        roots.add(prefix + _VARIANT_SAMPLE_SUFFIX)
    return sorted(roots)


def _ignored_paths(paths: list[str]) -> set[str] | None:
    """**批量**判定一组路径是否被忽略：一次 `git check-ignore --stdin -z`。

    为什么必须批量：逐个调用在 Windows 上每次约 0.3s，几十个目录会把一条纯结构
    检查拖到一分钟（实测 22s → 70s）。返回 `None` 表示 git 不可用（调用方 skip）。

    判定依据是**返回值**（哪些路径被忽略），**不是** `.gitignore` 的文本 ——
    读文本会把注释里提到该路径的说明也当成规则（本项目反复踩过）。
    """
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin", "-z"],
        cwd=str(REPO),
        input="\0".join(paths).encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode not in (0, 1):     # 0 = 有命中，1 = 全部未命中
        return None
    out = proc.stdout.decode("utf-8", "replace")
    return {p for p in out.split("\0") if p}


_ROOTS = _roots_to_probe()
_PROBE_PATHS = [f"{r}/{_PROBE_NAME}" for r in _ROOTS]
_IGNORED: set[str] | None = _ignored_paths(_PROBE_PATHS)


def _root_is_ignored(root: str) -> bool:
    """该**根**是否被整体忽略（含探测名一致性自检）。"""
    if _IGNORED is None:
        pytest.skip("git check-ignore 不可用")
    probe = f"{root}/{_PROBE_NAME}"
    assert root in _ROOTS, (
        f"`{root}` 未在 import 期预计算（`_roots_to_probe()` 需包含它）—— "
        f"否则查表恒落空、忽略判定会静默变成恒假"
    )
    return probe in _IGNORED


class TestIgnoreCoversDocumentedRoots:
    """不变式 A：登记表里每个落点都被忽略清单**实测**覆盖。"""

    @pytest.mark.parametrize("root", sorted(DOCUMENTED_ARTIFACT_ROOTS))
    def test_root_is_ignored(self, root: str):
        assert _root_is_ignored(root), (
            f"`{root}/` 未被 .gitignore 覆盖（{DOCUMENTED_ARTIFACT_ROOTS[root]}）。"
            f"文档让 agent 往这里写东西，忽略清单却不拦 —— "
            f"这正是 release-archive/ 曾经漏过的形态。"
        )


class TestGateStaysInSyncWithIgnoreList:
    """不变式 B：登记表 ⊆ 门禁清单（否则门禁盯不住该落点）。"""

    @pytest.mark.parametrize("root", sorted(DOCUMENTED_ARTIFACT_ROOTS))
    def test_root_is_watched_by_gate(self, root: str):
        assert rg.is_build_output(f"{root}/x/y.json"), (
            f"`{root}/` 不在 release_gate 的 BUILD_OUTPUT_DIRS/PREFIXES 里 —— "
            f"门禁不会拦它的 `git add -f`（{DOCUMENTED_ARTIFACT_ROOTS[root]}）"
        )

    def test_gate_list_is_not_wider_than_reality(self):
        """反向：门禁清单里的每一项都必须真的被忽略。

        否则门禁会在"本该放行"的路径上误报 FAIL —— 一条永远弄不绿的检查
        等于没有检查。
        """
        for d in rg.BUILD_OUTPUT_DIRS:
            assert _root_is_ignored(d), (
                f"release_gate 盯着 `{d}/`，但它并未被忽略 —— 门禁会误报"
            )
        for p in rg.BUILD_OUTPUT_PREFIXES:
            assert _root_is_ignored(p + _VARIANT_SAMPLE_SUFFIX), (
                f"release_gate 盯着 `{p}*`，但它并未被忽略 —— 门禁会误报"
            )


class TestGateWiresTheCheck:
    """不变式 C：检查被注册进编排（"检查消失"不会让别的用例变红）。"""

    def test_check_is_registered_in_run_all(self):
        names = {r.name for r in rg.run_all(skip_tests=True)}
        assert "no_build_outputs" in names, (
            "release_gate 不再执行 no_build_outputs —— 产物入库从此无人拦"
        )
        assert "dist_variants" in names, (
            "release_gate 不再执行 dist_variants —— 变体堆积从此不可见"
        )

    def test_check_fails_when_a_build_output_is_tracked(self, monkeypatch):
        """接线证明：喂给它一份"含产物的已跟踪清单"，必须 FAIL（而非默默 PASS）。"""
        monkeypatch.setattr(
            rg, "_tracked_paths",
            lambda: ["main.py", "dist-electron/win-unpacked/app.asar", "README.md"],
        )
        res = rg.check_no_build_outputs_tracked()
        assert res.status == rg.FAIL
        assert "app.asar" in res.detail

    def test_check_passes_on_a_clean_tracked_list(self, monkeypatch):
        monkeypatch.setattr(rg, "_tracked_paths", lambda: ["main.py", "core/x.py"])
        res = rg.check_no_build_outputs_tracked()
        assert res.status == rg.PASS


class TestPredicateDoesNotOverMatch:
    """不变式 D：判定只看第一段路径，前缀不得过度匹配。"""

    @pytest.mark.parametrize("path", [
        "dist/pbc-server/pbc-server.exe",
        "dist-electron/win-unpacked/BatchSentry.exe",
        "dist-electron-out-20260917-103254/win-unpacked/x",
        "dist-electron-locked/win-unpacked/x",
        "release-archive/batchsentry-1.1.6.zip",
        "devlogs/gate_report_20260917_105749.json",
        "node_modules/electron/index.js",
        "build/tmp.log",
    ])
    def test_positive(self, path: str):
        assert rg.is_build_output(path) is True

    @pytest.mark.parametrize("path", [
        "docs/dist/notes.md",          # 只判第一段 → 正文目录里的 dist 不算
        "distributions/report.md",     # 不得被 `dist` 前缀误伤
        "dist-electronica/x.js",       # 不得被 `dist-electron` 前缀误伤
        "core/dist_helper.py",
        "main.py",
    ])
    def test_negative(self, path: str):
        assert rg.is_build_output(path) is False


class TestDistVariantsCheck:
    """`dist_variants` 是 WARN 级提醒（阈值内 PASS），不是 FAIL。"""

    def test_few_variants_is_pass(self, tmp_path: Path):
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist-electron").mkdir()
        (tmp_path / "core").mkdir()
        res = rg.check_dist_variants(tmp_path)
        assert res.status == rg.PASS
        assert rg.count_dist_variants(tmp_path) == ["dist-electron"]

    def test_many_variants_warn_and_name_the_tool(self, tmp_path: Path):
        (tmp_path / "dist").mkdir()
        for i in range(rg.DIST_VARIANT_WARN_AT + 1):
            (tmp_path / f"dist-electron-out-2026{i:04d}").mkdir()
        res = rg.check_dist_variants(tmp_path)
        assert res.status == rg.WARN, (
            "变体堆积只提醒、不 FAIL —— 多轮构建会话并存几个变体是正常状态"
        )
        assert "clean_dist.py" in res.detail, "WARN 必须指出唯一清理入口"

    def test_dist_itself_is_not_counted(self, tmp_path: Path):
        """`dist/` 是 electron-builder 的输入、clean_dist 明确保留它，不计入变体。"""
        (tmp_path / "dist").mkdir()
        assert rg.count_dist_variants(tmp_path) == []


class TestRepoItselfIsClean:
    """现状机检：仓库当前确实没有生成物入库（与门禁同口径，但更快返回）。"""

    def test_no_generated_paths_are_tracked(self):
        tracked = rg._tracked_paths()
        if not tracked:
            pytest.skip("git 不可用")
        bad = [p for p in tracked if rg.is_build_output(p)]
        assert not bad, (
            f"以下生成物已入库（删文件也抹不掉历史）：{bad[:5]}；"
            f"用 `git rm -r --cached <path>` 取消跟踪并补 .gitignore"
        )


class TestNoShadowDirectories:
    """**派生式**护栏：顶层目录必须"非黑即白"。

    上面 `DOCUMENTED_ARTIFACT_ROOTS` 是**人工登记**表 —— 它只能覆盖"我已经想到的"
    落点。本类补上另一半：不列举，而是**遍历现实**，要求每个顶层目录满足
    **要么装着被跟踪的文件**（= 属于仓库），**要么整体被忽略**（= 生成物）。

    两者都不满足的叫**影子目录**：它已经存在、agent 会往里写，却既不受版本控制
    也不被忽略 —— 随便一个"没被逐个列举到"的扩展名就会立刻出现在 `git status` 里。
    `spike/` 正是这样被抓出来的：`.gitignore` 只列了 `spike/*.py` / `*.log` /
    `*.json` / `*.md` 与两个子目录，实测 `touch spike/__probe__.png` 使
    `git status` 报 `?? spike/`（**实测，非推断**）。

    派生式的价值：登记表要靠人想到才能加，而这一条**下次自己会红**。

    ⚠️ 必须用 `rg._tracked_paths()`（内部走 `git ls-files -z`）而不是裸
    `git ls-files`：后者会把非 ASCII 路径**加引号转义**（`samples/丝裂霉素…` 输出成
    `"samples/\350\235…"`），首段于是变成 `"samples` —— 判定的目录名全部对不上，
    本检查会**静默变成恒真**（实测：`samples` 因此被误报为影子目录）。
    这与 `release_gate._tracked_paths` 的 docstring 是同一条教训。
    """

    # `.git` 是仓库自身，不参与判定
    _SKIP = {".git"}

    def test_every_top_level_dir_is_tracked_or_ignored(self):
        tracked = rg._tracked_paths()
        if not tracked:
            pytest.skip("git 不可用")
        # 一次取回全部已跟踪路径（`-z` 分隔，无转义），避免"每个目录一次 git 调用"
        tracked_roots = {p.replace("\\", "/").split("/", 1)[0] for p in tracked}

        problems: list[str] = []
        for name in _top_level_dirs():
            if name in self._SKIP:
                continue
            if name in tracked_roots:
                continue                      # 属于仓库
            if _root_is_ignored(name):
                continue                      # 生成物，整体被忽略
            problems.append(name)
        assert not problems, (
            f"影子目录（既无被跟踪文件、又未被忽略）：{problems}。"
            f"它们已存在于工作区，agent 会往里写，而任何**未被逐个列举**的文件名"
            f"都会立刻显示在 git status 里 → 补一条整目录忽略（如 `spike/`），"
            f"或确属源码则纳入版本控制。"
        )
