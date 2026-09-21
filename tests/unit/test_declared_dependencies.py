"""源码 import 的包必须在依赖清单里声明（防"本机能跑、CI/打包挂"）。

**由来（真实隐患，2026-09-15 定位）**：`main.py` 顶层写着
``from markupsafe import Markup``，而 `markupsafe` **不在任何 requirements 清单里**
—— 它只是 Jinja2 的传递依赖，靠"Jinja2 恰好会装上它"运行。Jinja2 一旦换实现
（或改成可选导入），应用会在启动时 ImportError，而且**只在打包/CI 才暴露**。
更普遍地：CI 只安装清单里的东西 → 任何未声明的 import 都是 CI 直接红。

**判定口径**（逐档收窄）：
  1. **本地模块/包**（仓库里真有 `name.py` 或 `name/`，含 PEP 420 命名空间包）→ 跳过；
  2. **标准库**（`sys.stdlib_module_names`）→ 跳过；
  3. `_OPTIONAL` 登记的**可选依赖**（有"缺失即降级"的保护逻辑）→ 跳过；
  4. `_TOOL_ONLY` 登记的**工具专用**依赖 → 只允许出现在指定位置（精确到位）；
  5. 其余必须在 `requirements*.txt` 里找到对应分发名。

**为什么不干脆用白名单**：白名单会让"往任何地方加未声明依赖"都逃过检查。
这里只对 ①②③④ 开口，且 ③④ 各有专门护栏（见文件末尾）防止登记表变成摆设。
"""
import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# 扫描范围：产品代码 + 工程脚本 + 测试 + 仓库根目录脚本
_SCAN_DIRS = ("api", "core", "db", "llm", "models", "scripts", "tests")
_ROOT_PY_GLOB = "*.py"
_SKIP_DIR_PARTS = {
    "__pycache__", "node_modules", "build", "htmlcov", "devlogs", "output", "logs", ".git",
}
_SKIP_DIR_PREFIX = ("dist",)

# import 名 → 分发名（仅列两者不一致的）
_ALIAS = {
    "fitz": "pymupdf",
    "PIL": "pillow",
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "pytest_asyncio": "pytest-asyncio",
    "pytest_mock": "pytest-mock",
    "pytest_cov": "pytest-cov",
}

# 可选依赖：未声明是**设计使然**，但必须有"缺失即降级"的保护逻辑
_OPTIONAL = {
    "docling": "可选第三方 OCR 后端（core/docling_client.py：is_available() 保护，缺失不进链）",
}

# 工具专用：这些脚本不随包分发。值 = 允许出现的位置前缀 —— 精确到位，避免登记表
# 退化成"全局放行"。
# ⚠️ 登记在这里**不等于"可以缺"**：若某个工具**被测**，其依赖必须显式声明 ——
#    否则 `pytest.importorskip` 会让整个测试文件静默消失（见
#    `TestImportOrSkipIsDeclared` 与 numpy 的实测事故）。
_TOOL_ONLY: dict[str, tuple[str, ...]] = {
    "numpy": ("scripts/",),
    "win32com": ("scripts/",),
    "playwright": ("ui_e2e.py",),  # 真实浏览器的 UI e2e（人工执行）
}

_REQ_FILES = ("requirements.txt", "requirements-dev.txt")


def _norm(name: str) -> str:
    """PEP 503 归一化：`PyMuPDF` / `pymupdf` / `Py.MuPDF` 视为同一个名字。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_dists() -> set[str]:
    """清单里声明的分发名（已归一化）。"""
    out: set[str] = set()
    for fname in _REQ_FILES:
        p = _ROOT / fname
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)", line)
            if m:
                out.add(_norm(m.group(1)))
    return out


def _is_local(name: str, origins: list[Path] | None = None) -> bool:
    """仓库里真有这个模块/包？**动态判定**，不维护根模块白名单（那必然漂移）。

    四种形态都算本地：
      ① 根目录单文件模块（`e2e_run.py`）；
      ② 常规包（`core/__init__.py`）；
      ③ **命名空间包**（无 `__init__.py` 的目录，如 `scripts/` —— PEP 420，
         `import scripts.x` 照样成立）→ 只认 `__init__.py` 会漏判；
      ④ 与导入方**同目录**的模块（`scripts/release_gate.py` 里
         `import bundle_manifest`，加载前把该目录注入 `sys.path`）—— 故须传 `origins`。

    ④ 的由来（2026-09-20）：`scripts/release_gate.py` 与 `scripts/clean_dist.py`
    都按文件位置 `import bundle_manifest`（同目录的 `scripts/bundle_manifest.py`）。
    少了 ④，仓库**自己的**脚本会被判成"未声明的第三方依赖"，而误报的修法是
    **把本地模块塞进 requirements.txt** —— 那才是真的污染供应链清单。
    仅排除产物/缓存目录，防"恰好叫 build 的依赖被误判成本地"。
    """
    for origin in origins or ():
        if (origin.parent / f"{name}.py").is_file():
            return True
        if (origin.parent / name / "__init__.py").is_file():
            return True
    if (_ROOT / f"{name}.py").is_file():
        return True
    d = _ROOT / name
    if not d.is_dir() or name in _SKIP_DIR_PARTS or name.startswith(_SKIP_DIR_PREFIX):
        return False
    if (d / "__init__.py").is_file():
        return True
    return any(p.suffix == ".py" for p in d.iterdir() if p.is_file())


def _iter_source_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() != ".py":
                continue
            rel = p.relative_to(_ROOT)
            parts = rel.parts[:-1]
            if set(parts) & _SKIP_DIR_PARTS or any(
                    pt.startswith(_SKIP_DIR_PREFIX) for pt in parts):
                continue
            yield rel, p
    for p in sorted(_ROOT.glob(_ROOT_PY_GLOB)):
        yield Path(p.name), p


def collect_imports(rel: Path, src: str) -> set[str]:
    """纯函数：返回该源码里 import 的**顶层**模块名（相对导入不计）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入 = 本地
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _imported_map() -> dict[str, list[str]]:
    """{顶层 import 名: [出现位置, ...]}。"""
    found: dict[str, list[str]] = {}
    for rel, p in _iter_source_files():
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        for name in collect_imports(rel, src):
            found.setdefault(name, [])
            if rel.as_posix() not in found[name]:
                found[name].append(rel.as_posix())
    return found


# `pytest.importorskip("X")` 里的 X 是**隐式依赖**：import 扫描看不见它（那是字符串
# 参数，不是 import 语句），而缺包时的后果比"少跑几条"严重得多 —— **整个模块被静默
# 跳过**（junit 里只剩一条 `classname=""` 的 collection-skip 条目），门禁的
# passed / failed / 覆盖率**全都看不见**。
def collect_importorskip(rel: Path, src: str) -> set[str]:
    """纯函数：AST 提取 `pytest.importorskip("X")` 的 X（已归一化）。

    刻意用 AST 而非正则：正则会把**注释与文档里的示例**也算进来 —— 本文件的
    docstring 里就写着示例，实测被正则版本误报成"未声明依赖 x"。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "importorskip"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            names.add(_norm(node.args[0].value))
    return names


def _importorskip_map() -> dict[str, list[str]]:
    """{importorskip 的模块名（已归一化）: [出现位置, ...]}。"""
    found: dict[str, list[str]] = {}
    for rel, p in _iter_source_files():
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        for name in collect_importorskip(rel, src):
            found.setdefault(name, [])
            if rel.as_posix() not in found[name]:
                found[name].append(rel.as_posix())
    return found


def find_undeclared() -> list[str]:
    """纯函数：返回未声明且不在任何豁免档的问题描述（空列表 = 干净）。"""
    declared = _declared_dists()
    stdlib = set(sys.stdlib_module_names)
    problems: list[str] = []
    for name, files in sorted(_imported_map().items()):
        if (_is_local(name, [Path(f) for f in files])
                or name in stdlib or name in _OPTIONAL):
            continue
        if name in _TOOL_ONLY:
            allowed = _TOOL_ONLY[name]
            bad = [f for f in files if not any(f.startswith(a) for a in allowed)]
            if bad:
                problems.append(
                    f"{name} 只允许出现在 {list(allowed)}，却出现在：{bad}"
                )
            continue
        dist = _ALIAS.get(name, name)
        if _norm(dist) in declared:
            continue
        problems.append(
            f"{name}（分发名 {dist}）未在 {' / '.join(_REQ_FILES)} 中声明 —— "
            f"出现在：{sorted(files)[:3]}"
        )
    return problems


# ── 护栏本体 ────────────────────────────────────────────────────────


def test_every_imported_package_is_declared():
    """源码 import 的第三方包必须能在依赖清单里找到。"""
    problems = find_undeclared()
    assert not problems, (
        "以下 import 的包未声明 —— CI / 打包环境只会装清单里的东西，"
        "这里漏一个就是 ImportError：\n  " + "\n  ".join(problems)
    )


def test_declared_dependency_guard_positive_control():
    """阳性对照：判定逻辑必须真的会"抓"，否则全绿只是空转。"""
    # ① AST 抽取有效（`import a.b` 取顶层；`from x import y` 取 x；相对导入不计）
    names = collect_imports(Path("x.py"), "import aaa\nimport bbb.cc\nfrom ddd import e\nfrom . import f")
    assert names == {"aaa", "bbb", "ddd"}, names
    # ② 归一化有效（PEP 503：大小写、`-`/`_`/`.` 三种分隔符视为等价）
    assert _norm("PyMuPDF") == _norm("pymupdf") == _norm("PYMUPDF")
    assert _norm("python_dotenv") == _norm("python-dotenv") == _norm("python.dotenv")
    # ③ 未声明判定有效：造一个绝不存在、也不在清单里的名字
    fake = "nonexistent_pkg_zzz"
    assert not _is_local(fake)
    assert fake not in set(sys.stdlib_module_names)
    assert _norm(fake) not in _declared_dists()
    # ④ 反向：本地包（含命名空间包）与标准库不得被判为未声明
    assert _is_local("core") and _is_local("tests") and _is_local("scripts")
    assert "os" in set(sys.stdlib_module_names)
    # ⑤ 同目录本地模块（形态 ④）：`scripts/release_gate.py` 里 `import bundle_manifest`
    #    是**仓库自己的**脚本互导，不是第三方依赖。要点：必须**显式给出起点目录**
    #    —— 单参形态只认根模块/包，故它单独看不出来。
    assert _is_local("bundle_manifest", [Path("scripts/release_gate.py")])
    assert not _is_local("bundle_manifest")
    assert not [p for p in find_undeclared() if "bundle_manifest" in p], (
        "scripts/ 内的同目录互导被误判成未声明的第三方依赖"
    )
    # ⑤ 由来仍在：main.py 顶层 import markupsafe，且它**已**被声明（本次修复的目标态）
    assert "main.py" in _imported_map().get("markupsafe", []), (
        "main.py 不再 import markupsafe —— 本护栏的由来已失效，请复核后更新说明"
    )
    assert _norm("markupsafe") in _declared_dists(), (
        "markupsafe 未声明 —— 它被 main.py 顶层 import，只靠 Jinja2 传递依赖撑着"
    )


def test_optional_and_tool_only_entries_are_live():
    """登记表不得变成摆设：③④ 档每个条目都必须**确实被 import 到**。

    空豁免会让护栏悄悄失去覆盖 —— 所以豁免本身也要被检查。
    """
    imports = _imported_map()
    for table in (_OPTIONAL, _TOOL_ONLY):
        for name, why in table.items():
            assert imports.get(name), (
                f"豁免条目 {name!r}（{why}）已不再被 import —— 请删除该条目，"
                "否则它只是让未来同名依赖悄悄溜过检查"
            )
    # ③ 档登记的可选依赖必须有降级保护，不能裸导入整个模块
    for name in _OPTIONAL:
        assert [f for f in imports[name] if f.startswith("core/")], (
            f"{name} 登记为可选依赖，却不在 core/ 下 —— 请复核是否有降级保护"
        )


def test_requirements_files_are_pinned():
    """清单必须是**精确锁定**（`==`），不是下界 —— 否则复现不了构建。

    只检查依赖行；注释、extras（`pkg[extra]==x`）与空行不受影响。
    """
    loose: list[str] = []
    for fname in _REQ_FILES:
        p = _ROOT / fname
        if not p.is_file():
            continue
        for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            if "==" not in line:
                loose.append(f"{fname}:{i}  {line}")
    assert not loose, (
        "以下依赖未精确锁定（缺 `==`）—— 本项目以冻结产物分发，浮动版本会让 "
        "CI / 本地 / 发布包各跑一套依赖：\n  " + "\n  ".join(loose)
    )


def test_requirements_files_exist():
    """两个清单都必须在场（否则上面的检查会静默通过）。"""
    for fname in _REQ_FILES:
        assert (_ROOT / fname).is_file(), f"{fname} 缺失 —— 依赖清单是构建的前提"


class TestImportOrSkipIsDeclared:
    """`pytest.importorskip("X")` 的 X 必须在清单里声明（或已登记豁免）。

    实测事故（2026-09-16）：`test_anchor_orientation_tool.py` 用
    `pytest.importorskip("numpy")` 测 `scripts/verify_anchor_orientation.py`，
    而 numpy 只被登记为 `_TOOL_ONLY`（"不进 CI 环境"）→ CI 上该文件**整段**跳过，
    **27 条用例静默消失，门禁六项全绿**（junit 里该文件只剩一条 `classname=""`
    的 collection-skip 条目）。这是"覆盖率绿 ≠ 用例都跑了"的最强反例，也是
    "CI 与本地差 40+ 个用例"的真身。
    """

    def test_importorskip_dependencies_are_declared(self):
        skips = _importorskip_map()
        undeclared = {
            name: locs for name, locs in skips.items()
            if name not in _declared_dists()
            and name not in _OPTIONAL
            and not _is_local(name)
        }
        assert not undeclared, (
            "importorskip 的依赖未声明 —— 缺包时**整个文件**会被静默跳过，"
            "门禁的 passed/failed/覆盖率全都看不见。请把依赖加进 "
            f"requirements-dev.txt：{undeclared}"
        )

    def test_importorskip_guard_is_not_vacuous(self):
        """正对照：扫描器必须真能扫到已知的 importorskip（防"空转即全绿"）。"""
        skips = _importorskip_map()
        assert "numpy" in skips, "应扫到 test_anchor_orientation_tool.py 的 numpy"
        assert any("test_anchor_orientation_tool" in loc for loc in skips["numpy"])

    def test_importorskip_scan_covers_tests_dir(self):
        """范围护栏：`tests/` 必须在扫描范围内，否则本类会默默失效。"""
        scanned = {rel.as_posix() for rel, _ in _iter_source_files()}
        assert any(p.startswith("tests/") for p in scanned), \
            "扫描范围不含 tests/ —— importorskip 护栏形同虚设"

    def test_docstring_examples_are_not_counted(self):
        """必须用 AST 而非正则：文档里的示例不算真实依赖。

        正则版本实测把自己 docstring 里的示例当成了 `importorskip("x")`，
        于是这条护栏报出"未声明依赖 x"的**假警报**（假阳性会让人把护栏关掉）。
        """
        doc_only = ('def f():\n'
                    '    """示例：pytest.importorskip("ghostpkg") 用于可选依赖"""\n'
                    '    pass\n')
        assert collect_importorskip(Path("fake.py"), doc_only) == set()
        real = 'import pytest\nnp = pytest.importorskip("numpy")\n'
        assert collect_importorskip(Path("fake.py"), real) == {"numpy"}

    def test_syntax_error_source_yields_nothing(self):
        """坏源码不得让护栏崩溃（与 collect_imports 同口径）。"""
        assert collect_importorskip(Path("bad.py"), "def ( <<<") == set()
