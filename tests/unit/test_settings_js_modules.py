"""settings.js 拆分（1861 行 → 6 模块）的结构护栏。

**为什么需要**：拆分把单个 IIFE 闭包切成 6 个文件，模板用 defer 按依赖序加载。
拆分后的静态结构错了**不会抛异常** —— 只会静默 undefined 或运行期 TypeError，
页面上看不出任何报错。必须靠机检钉住三类不变式：

1. **TestScriptOrder**：`templates/settings.html` 必须按依赖序引入全部模块。
   defer 按**文档顺序**执行 ⇒ 顺序即依赖序；漏引一个模块 = 该模块职责静默消失。
2. **TestStateHost**：可变状态只能有**一个宿主**（settings-state.js 的
   `S.state`）。这是拆分方案点名的风险（docs/ADVERSARIAL_REVIEW_2026-09-28.md
   §维度 4）：任何模块自建 `current`/`activeProvider` 同名变量，就会重演
   review.js 的"同名遮蔽"类缺陷。
3. **TestNamespaceContract**：`S.<fn>(...)` 的每个调用点都必须在某个模块里
   有对应导出 —— 漏导出 = 运行期 `TypeError: S.x is not a function`。

拆分依据：docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"
SETTINGS_HTML = REPO / "templates" / "settings.html"

#: 拆分模块（不含入口 settings.js —— 它单独断言"必须最后"）
SETTINGS_MODULES = [
    "settings-state.js",   # 状态宿主 + 纯工具（一切模块的依赖）
    "settings-llm.js",     # provider 卡片渲染 + 操作
    "settings-ocr.js",     # section 导航 + KB 面板 + OCR 配置 + load()
    "settings-feishu.js",  # 飞书通知
    "settings-rules.js",   # 合规规则编辑器 + 模板面板
]
ENTRY = "settings.js"

#: 可变状态名 —— 只允许出现在 settings-state.js 的 S.state 里
STATE_NAMES = ["current", "activeProvider", "pendingAdds", "removedProviders"]

#: 显式依赖序（前项必须先于后项）
_ORDER_PAIRS = (
    [("settings-state.js", m) for m in SETTINGS_MODULES[1:]]
    + [(m, ENTRY) for m in SETTINGS_MODULES]
)


def _all_settings_js() -> list[Path]:
    return sorted(STATIC.glob("settings*.js"))


def _script_srcs(html: str) -> list[str]:
    """按出现顺序取出 settings.html 里 defer script 的静态文件名。

    模板带 `?v={{ asset_ver(...) }}` 版本戳，匹配到 `.js` 为止。
    """
    return re.findall(r'<script[^>]*src="/static/([\w.-]+\.js)[?"]', html)


def _exports(src: str) -> set[str]:
    """取出 `Object.assign(S, { a, b })` 里挂到命名空间的名字。"""
    out: set[str] = set()
    for m in re.finditer(r"Object\.assign\(S,\s*\{([^}]*)\}", src):
        out |= {n.strip() for n in m.group(1).split(",") if n.strip()}
    return out


class TestScriptOrder:
    """settings.html 的 defer script 必须完整且按依赖序排列。"""

    def test_all_modules_are_referenced(self):
        """每个拆分模块 + 入口都必须被模板引用。"""
        srcs = _script_srcs(SETTINGS_HTML.read_text(encoding="utf-8"))
        needed = SETTINGS_MODULES + [ENTRY]
        missing = [m for m in needed if m not in srcs]
        assert not missing, (
            f"settings.html 漏引这些模块 —— 对应职责会静默消失：{missing}"
        )

    def test_no_unreferenced_settings_module(self):
        """反向：static 里的 settings*.js 若没被模板引用 = 死代码或漏接线。"""
        srcs = _script_srcs(SETTINGS_HTML.read_text(encoding="utf-8"))
        orphan = [p.name for p in _all_settings_js() if p.name not in srcs]
        assert not orphan, (
            f"static 里这些 settings 模块未被模板引用（死代码或漏接线）：{orphan}"
        )

    def test_dependency_order(self):
        """显式依赖序：前项的 script 位置必须先于后项。"""
        srcs = _script_srcs(SETTINGS_HTML.read_text(encoding="utf-8"))
        pos = {name: i for i, name in enumerate(srcs)}
        for before, after in _ORDER_PAIRS:
            assert before in pos and after in pos, (
                f"依赖序检查的锚点缺失：{before} / {after}"
            )
            assert pos[before] < pos[after], (
                f"加载顺序颠倒：{before} 必须先于 {after}（defer 按文档序执行，"
                f"颠倒即 undefined 静默失效）"
            )

    def test_state_module_is_first(self):
        """settings-state.js 必须**最先**加载 —— 它是其余模块的唯一状态宿主。"""
        srcs = _script_srcs(SETTINGS_HTML.read_text(encoding="utf-8"))
        settings_like = [s for s in srcs if s.startswith("settings")]
        assert settings_like, "模板未引用任何 settings*.js"
        assert settings_like[0] == "settings-state.js", (
            f"settings-state.js 必须最先加载（当前最先的是 {settings_like[0]}）"
            f" —— 其余模块在顶层就 `const S = window.PbcSettings` 并解构纯工具，"
            f"state 未先执行会 undefined"
        )

    def test_entry_point_is_last(self):
        """入口 settings.js 必须最后加载（末尾调用 S.load() 启动渲染）。"""
        srcs = _script_srcs(SETTINGS_HTML.read_text(encoding="utf-8"))
        settings_like = [s for s in srcs if s.startswith("settings")]
        assert settings_like[-1] == ENTRY, (
            f"入口 {ENTRY} 应最后加载（当前最后的是 {settings_like[-1]}）—— "
            f"它在末尾 S.load()，依赖全部模块已就绪"
        )


class TestStateHost:
    """可变状态只能有唯一宿主（防止"同名遮蔽"类缺陷）。"""

    def test_state_module_is_the_only_host(self):
        """settings-state.js 必须定义全部可变状态。"""
        src = (STATIC / "settings-state.js").read_text(encoding="utf-8")
        assert re.search(r"window\.PbcSettings\s*=", src), (
            "settings-state.js 未创建 window.PbcSettings 命名空间"
        )
        missing = [n for n in STATE_NAMES if not re.search(rf"^\s*{n}:", src, re.M)]
        assert not missing, (
            f"settings-state.js 的 S.state 缺少这些键：{missing}"
        )

    def test_no_other_module_declares_state_names(self):
        """其余模块**不得**自建同名可变状态（否则是遮蔽缺陷的根因形态）。"""
        offenders: list[str] = []
        for p in _all_settings_js():
            if p.name == "settings-state.js":
                continue
            src = p.read_text(encoding="utf-8")
            for n in STATE_NAMES:
                if re.search(rf"^\s*(?:const|let|var)\s+{n}\b", src, re.M):
                    offenders.append(f"{p.name}:{n}")
        assert not offenders, (
            f"这些模块自建了可变状态同名变量：{offenders} —— 会重演 review.js 的"
            f"同名遮蔽缺陷；一律改为 S.state.<name>"
        )

    def test_consumers_actually_use_the_namespace(self):
        """用了状态的模块必须真的经 `S.state.` 读写（防空转：别只剩注释）。"""
        users = {
            "settings-llm.js": 1, "settings-ocr.js": 1,
            "settings-feishu.js": 1, ENTRY: 1,
        }
        for name, min_hits in users.items():
            src = (STATIC / name).read_text(encoding="utf-8")
            hits = len(re.findall(r"\bS\.state\.", src))
            assert hits >= min_hits, (
                f"{name} 应经 S.state.* 读写共享状态，实际命中 {hits} 处"
            )


class TestNamespaceContract:
    """`S.<fn>` 的调用点必须有对应导出（漏导出 = 运行期 TypeError）。"""

    def test_every_module_binds_the_namespace(self):
        """除 state 外，每个模块都要有 `const S = window.PbcSettings;`。"""
        missing = []
        for p in _all_settings_js():
            if p.name == "settings-state.js":
                continue
            src = p.read_text(encoding="utf-8")
            if "const S = window.PbcSettings;" not in src:
                missing.append(p.name)
        assert not missing, f"这些模块未绑定命名空间：{missing}"

    def test_export_set_is_not_empty(self):
        """防空转：至少解析出若干导出，否则下面的子集断言恒真。"""
        exported: set[str] = set()
        for p in _all_settings_js():
            exported |= _exports(p.read_text(encoding="utf-8"))
        assert len(exported) >= 5, (
            f"只解析出 {len(exported)} 个导出（{sorted(exported)}）—— "
            f"导出解析可能已失效，子集断言会变成空断言"
        )

    def test_all_cross_module_calls_are_exported(self):
        """`S.<fn>(...)` 的每个 <fn> 必须在某个模块里被导出。"""
        exported: set[str] = set()
        for p in _all_settings_js():
            exported |= _exports(p.read_text(encoding="utf-8"))

        bad: list[str] = []
        for p in _all_settings_js():
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"\bS\.([A-Za-z_$][\w$]*)\s*\(", src):
                if m.group(1) not in exported:
                    bad.append(f"{p.name}: S.{m.group(1)}(")
        assert not bad, (
            f"这些调用点指向未导出的名字（运行期 TypeError）：{bad}"
        )
