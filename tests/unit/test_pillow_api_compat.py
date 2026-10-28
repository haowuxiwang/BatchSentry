"""Pillow 大版本升级会把旧 API 变成"未来删除"——把这条变成可机检。

**由来（2026-09-23 Round 59，W2 预演时实测）**：在隔离 venv 里把 `Pillow`
从 **10.1.0 升到 12.3.0**（= B11-7 的目标版本）后跑全量 `tests/unit tests/integration`
—— **3223 passed / 0 failed**，与升级前**用例数完全一致**，升级本身**没有破坏任何东西**。
但 warnings summary 里冒出 **22 条**：

    core/pipeline/self_heal.py:184: DeprecationWarning:
        Image.Image.getdata is deprecated and will be removed in Pillow 14
        (2027-10-15). Use get_flattened_data instead.

**为什么必须写成护栏**：`Pillow 10.1.0`（= **门禁当前跑的那个环境**）**根本不报这条**
—— 它在 `getdata` 被真正删除（Pillow 14）之前，会一直是"**在旧环境恒绿**"的判据。
这与 PITFALLS §二十六/§四十 记的那类失效**同源**：**判据在采样环境里没有判别力**。
不写成护栏，它就会在 2027-10-15 那天变成 `AttributeError`。

**判定口径（唯一真值 ⇒ 派生，不手写）**：
  1. 从 `requirements.txt` 读出 **Pillow 的锁定版本** —— 本项目以冻结产物分发，
     **锁定的版本就是用户机上跑的那个版本**，所以"清单里的版本"才是判据的输入；
  2. 仅当锁定版本 **>= 该弃用开始生效的版本**（`_DEPRECATED` 登记）时，才要求
     产品源码不得调用被弃用 API；
  3. 低于该阈值时本护栏**有意惰性通过** —— 这条性质由
     `judge((11, 99, 99), …) == []` 直接钉住（见 `test_guard_discriminates_high_vs_low_pinned_version`），
     与"当前锁定版本已达标后源码必须真的干净"
     （`test_no_deprecated_usage_once_threshold_is_reached`）**成对**，缺一即失效。
  4. 🔴 **本护栏的由来（B11-18）已达成**：`requirements.txt` 现锁 `Pillow==12.3.0`，
     且 `core/pipeline/self_heal.py` 已改用 `get_flattened_data()`（实测两者
     在 mode "L" + `resize(BOX)` 下逐元素完全一致）。故**新增弃用要重新走一遍
     第 1–3 步**（把新 API 补进 `_DEPRECATED`），本护栏不会自动发现。

⚠️ **本护栏的诚实边界**（不写成"已覆盖全部弃用"）：
  - `_DEPRECATED` 是**具名登记表**，不是自动发现。**发现新弃用的方法**：
    在升版后的环境里跑一次 `pytest -W error::DeprecationWarning:core.*`（见
    `docs/TODO.md` B11-18 的验收步骤），把命中的 API 补进这张表。
  - 阈值取 `>= 12` 的依据是**实测**：12.3.0 报、10.1.0 不报。11.x 未测 ⇒
    阈值**只会偏早、不会偏晚**（早拦一个版本无副作用；本项目的升级是 10 → 12.3.0）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# 扫描范围 = **随包分发的产品源码**。
# 刻意排除：`scripts/`（构建期工具，不入包）、`tests/`（不发货）、
# `devlogs/`（原型/探针，其中 `_proto_prescreen.py` 也用了 `getdata`，但不进产物）。
_SCAN_DIRS = ("api", "core", "db", "llm", "models")
_SKIP_DIR_PARTS = {"__pycache__", "node_modules", "build", "htmlcov", "output", "logs", ".git"}
_SKIP_DIR_PREFIX = ("dist",)

# 被弃用 API 登记表：`方法名: (开始生效的 Pillow 次版本, 移除版本, 替代品)`
#
# 只登记**有实测证据**的条目：
#   `getdata` —— 12.3.0 实测发 DeprecationWarning（22 条，全部来自 self_heal.py），
#   官方文案给出移除版本与日期 "Pillow 14 (2027-10-15)"。
_DEPRECATED: dict[str, tuple[int, str, str]] = {
    "getdata": (12, "Pillow 14（2027-10-15）", "get_flattened_data"),
}

_REQ_FILE = "requirements.txt"


def _parse_version(text: str) -> tuple[int, ...] | None:
    """`"12.3.0"` → `(12, 3, 0)`；取不到数字则 None。"""
    nums = re.findall(r"\d+", text)
    if not nums:
        return None
    return tuple(int(n) for n in nums[:3])


def _pinned_version(dist: str) -> tuple[int, ...] | None:
    """从 `requirements.txt` 读出某分发的**锁定版本**（PEP 503 归一化匹配）。

    只认 `==` 精确锁定的那一行 —— 本项目的清单必须精确锁定
    （另见 `tests/unit/test_declared_dependencies.py::test_requirements_files_are_pinned`）。
    """
    p = _ROOT / _REQ_FILE
    if not p.is_file():
        return None
    want = re.sub(r"[-_.]+", "-", dist).lower()
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)[^=]*==\s*([^\s;]+)", line)
        if not m:
            continue
        if re.sub(r"[-_.]+", "-", m.group(1)).lower() == want:
            return _parse_version(m.group(2))
    return None


def _iter_source_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(_ROOT)
            parts = rel.parts[:-1]
            if set(parts) & _SKIP_DIR_PARTS or any(
                    pt.startswith(_SKIP_DIR_PREFIX) for pt in parts):
                continue
            yield rel, p
    for p in sorted(_ROOT.glob("*.py")):
        yield Path(p.name), p


def collect_calls(rel: Path, src: str) -> set[str]:
    """纯函数：该源码里**被调用方法**的名字集合（`x.y(...)` 取 `y`）。

    刻意用 AST 而非正则：正则会把注释/文档里的示例也算成真实调用 ——
    `tests/unit/test_declared_dependencies.py` 的 docstring 就实测踩过这个坑。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def collect_call_sites(rel: Path, src: str) -> dict[str, list[int]]:
    """纯函数：{被调用方法名: [行号, ...]}。用于把问题**精确报到行**。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.setdefault(node.func.attr, []).append(node.func.lineno)
    return out


def find_deprecated_usage() -> list[str]:
    """纯函数：返回**当前锁定版本下**必须修掉的问题描述（空列表 = 干净）。

    刻意把 `(版本, 调用点)` 作为**纯输入**拆出来，好让对照测试能在
    不碰真实清单的前提下证明判别力（见 `test_guard_discriminates_...`）。
    """
    usages: dict[str, list[str]] = {}
    for rel, p in _iter_source_files():
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        for api, lines in collect_call_sites(rel, src).items():
            if api in _DEPRECATED:
                usages.setdefault(api, []).extend(
                    f"{rel.as_posix()}:{ln}" for ln in sorted(lines)
                )
    return judge(_pinned_version("pillow"), usages)


def judge(pinned: tuple[int, ...] | None, usages: dict[str, list[str]]) -> list[str]:
    """纯判定：给定锁定版本与调用点，返回问题清单。**判据与取材分离**。"""
    if pinned is None:
        return [f"读不到 {_REQ_FILE} 里 Pillow 的锁定版本 —— 判据无法成立（fail-closed）"]
    problems: list[str] = []
    for api, (since, removal, replacement) in sorted(_DEPRECATED.items()):
        if pinned < (since,):
            continue  # 该弃用在此锁定版本下不生效 ⇒ 有意惰性
        for site in usages.get(api, []):
            problems.append(
                f"{site} 调用了 Pillow 已弃用的 `{api}()`"
                f"（自 Pillow {since} 起发警告，{removal} 将移除）"
                f" ⇒ 请改用 `{replacement}()`"
            )
    return problems


# ── 护栏本体 ────────────────────────────────────────────────────────


def test_pinned_pillow_version_is_readable():
    """阳性对照：判据的**输入**必须真的取得到，否则整套判定是空转。"""
    v = _pinned_version("pillow")
    assert v is not None, (
        f"{_REQ_FILE} 里找不到精确锁定的 Pillow —— 判据的输入缺失，"
        "本文件其余断言全部无意义"
    )
    assert v[0] >= 10, f"Pillow 锁定版本解析异常：{v}"
    # 归一化必须生效（大小写/分隔符）：换一种写法也要能取到同一个值
    assert _pinned_version("Pillow") == _pinned_version("pillow") == v


def test_no_deprecated_pillow_api_under_pinned_version():
    """锁定版本 >= 弃用生效版本时，产品源码不得再调用被弃用 API。"""
    problems = find_deprecated_usage()
    assert not problems, (
        "在**锁定版本**下这些调用会产生弃用警告（并将在未来版本被移除）：\n  "
        + "\n  ".join(problems)
        + "\n  改法：`getdata()` → `get_flattened_data()`"
        "\n  ⚠️ 必须与 `requirements.txt` 的版本变更**同一提交**："
        "Pillow 10 没有 `get_flattened_data`，提前改会让当前环境跑不起来。"
    )


def test_scan_actually_sees_known_call_sites(tmp_path, monkeypatch):
    """防"空转即全绿"：扫描→判定这条**完整链路**必须仍能报出 `getdata()`。

    ⚠️ B11-18 修完后，产品源码里**已无** `getdata()`，于是"扫到了调用点"这件事
    **不能再靠真实源码来证明** —— 原实现正是靠 `self_heal.py` 里的两处真实调用，
    修完后它会永远找不到，从而把本护栏变成恒真断言。
    改为**注入一个合成的产品源文件**，端到端跑一遍"扫描 + 判定"。
    """
    fake = tmp_path / "fake_mod.py"
    fake.write_text("v = img.getdata()\n", encoding="utf-8")
    M = sys.modules[__name__]
    monkeypatch.setattr(
        M, "_iter_source_files",
        lambda: iter([(Path("api/fake_mod.py"), fake)]),
    )
    problems = M.find_deprecated_usage()
    assert any("fake_mod.py" in p for p in problems), problems
    assert any("get_flattened_data" in p for p in problems), problems


def test_scan_reaches_product_dirs_and_extracts_real_calls():
    """**真实源码**阳性对照（与上一条成对）：扫描器确实走进了产品目录并取到调用。

    锚点选 `resize` —— 它与原来的两处 `getdata()` 在**同一行**，不会随 B11-18 消失。
    """
    seen: dict[str, set[str]] = {}
    for rel, p in _iter_source_files():
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        seen[rel.as_posix()] = collect_calls(rel, src)
    assert "core/pipeline/self_heal.py" in seen, "扫描器没走进 core/pipeline/"
    assert "resize" in seen["core/pipeline/self_heal.py"], (
        "AST 提取在真实源码上取不到调用 —— 提取器坏了，本护栏已失去判别力"
    )


def test_guard_covers_production_dirs():
    """范围护栏：产品目录必须在扫描范围内（否则护栏形同虚设）。"""
    scanned = {rel.as_posix() for rel, _ in _iter_source_files()}
    assert any(p.startswith("core/") for p in scanned), "扫描范围不含 core/"
    assert any(p.startswith("api/") for p in scanned), "扫描范围不含 api/"
    # 反向：不发货的东西不该被算进来（否则会报出修不了的"问题"）
    assert not any(p.startswith("tests/") for p in scanned), "tests/ 不该进扫描范围"
    assert not any(p.startswith("devlogs/") for p in scanned), "devlogs/ 不该进扫描范围"


def test_guard_discriminates_high_vs_low_pinned_version():
    """对照组成立：同一份调用点，**锁定版本不同 ⇒ 结论必须不同**。

    这是本护栏**唯一**能证明"它真的会在未来拦下来"的实验 ——
    且它**在当前环境（Pillow 10）就能跑**，不需要装 Pillow 12。
    """
    usages = {"getdata": ["core/pipeline/self_heal.py:184", "core/pipeline/self_heal.py:186"]}
    # ① 锁定 12+（= 升版后）⇒ 必须报出来
    high = judge((12, 3, 0), usages)
    assert len(high) == 2, high
    assert all("get_flattened_data" in p for p in high), high
    # ② 锁定 10.x（= 现在）⇒ 同一份调用点**应当**不报（该弃用尚未生效）
    assert judge((10, 1, 0), usages) == []
    # ③ 边界**成对**：下侧惰性 / 阈值上侧必须报。
    #    ⚠️ 这里必须含**退化形态** `(12,)`（= `Pillow==12`，只有主版本号）：
    #    元组比较里 `(12,0,0) > (12,)`（长的更大）⇒ 只用 `(12,0,0)` 的话，
    #    `<` 与 `<=` **完全等价**，边界变异就成了"等价变异"、打不红（实测 M5 首轮 MISSED）。
    assert judge((11, 99, 99), usages) == [], "阈值下侧（11.x）应当惰性"
    assert judge((12,), usages) != [], (
        "恰好等于阈值（`Pillow==12`）时必须报 —— 判定写成 `<=` 就会在这里漏"
    )
    assert judge((12, 0, 0), usages) == judge((12, 3, 0), usages)
    # ④ 干净输入 ⇒ 不报（防"恒报"）
    assert judge((12, 3, 0), {}) == []


def test_no_deprecated_usage_once_threshold_is_reached():
    """B11-18 落地后：锁定版本已 >= 12，产品源码必须**真的干净**。

    ⚠️ 本条的前身是 `test_guard_is_inert_below_threshold_on_purpose`
    （"当前锁 10.x ⇒ 有意惰性"）。B11-7/B11-18 同一提交改完后**前提变了**，
    故按原注释的要求**改写而不是删掉**：
    - 现在断言"锁定版本已达阈值 **且** 源码真的干净"；
    - "阈值下侧惰性"这条性质仍由 `judge((11, 99, 99), …) == []` 单独守着
      （见 `test_guard_discriminates_high_vs_low_pinned_version`）。
    """
    pinned = _pinned_version("pillow")
    assert pinned is not None and pinned >= (12,), (
        f"锁定版本是 {pinned}，低于弃用生效阈值 —— 若你回退了 B11-7，"
        "请连同 `core/pipeline/self_heal.py` 的 `get_flattened_data()` 一起回退"
    )
    assert find_deprecated_usage() == [], (
        "锁定版本 >=12 却仍报出问题 —— 产品源码还在调用被弃用 API"
    )


def test_threshold_is_derived_not_hardcoded_in_judge():
    """判据必须**从登记表派生**，不能在 judge 里写死 12 —— 否则改登记表不生效。

    手法：把 `_DEPRECATED` 临时改成一个**生效版本为 99** 的条目，
    自造的 `getdata` 调用点必须**不再**被报（说明阈值真的来自登记表）。
    """
    usages = {"getdata": ["x.py:1"]}
    assert judge((12, 3, 0), usages), "登记表里 12 生效的条目应报出"
    # 用 monkeypatch 改登记表：生效版本抬到 99 ⇒ 同样的调用点不再被报
    import pytest

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setitem(_DEPRECATED, "getdata", (99, "?", "?"))
        assert judge((12, 3, 0), usages) == [], "阈值写死在 judge 里了（无判别力）"
    finally:
        monkeypatch.undo()
    assert judge((12, 3, 0), usages), "monkeypatch 未还原"


def test_fail_closed_when_pin_is_unreadable():
    """读不到锁定版本 ⇒ **fail-closed**（报问题），不得静默放行。

    与 PITFALLS §三十六"取不到签名一律 fail-closed"同源：读不到就当作"判不了"，
    而"判不了"**绝不能**被当成"通过"。
    """
    assert judge(None, {"getdata": ["x.py:1"]}) != []


def test_syntax_error_source_yields_nothing():
    """坏源码不得让护栏崩溃（与 collect_imports 同口径）。"""
    assert collect_calls(Path("bad.py"), "def ( <<<") == set()
    assert collect_call_sites(Path("bad.py"), "def ( <<<") == {}


def test_docstring_examples_are_not_counted():
    """必须用 AST 而非正则：注释/文档里的示例不算真实调用。"""
    doc_only = 'def f():\n    """示例：img.getdata() 已被弃用"""\n    pass\n'
    assert "getdata" not in collect_calls(Path("fake.py"), doc_only)
    real = "v = img.getdata()\n"
    assert collect_call_sites(Path("fake.py"), real) == {"getdata": [1]}
