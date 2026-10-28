"""`esc` 转义器家族的**多副本行为锁**（R83 第二十三批，对抗性审查 → TODO 0-25）。

## 为什么需要

`static/*.js` 有 **38 个 `innerHTML` 赋值点**（`tests/unit/test_innerhtml_escaping.py`
机检），其安全**全部**依赖 `esc()` 纪律。但 `esc` 本身在**三个页面 bundle 里各有一份
独立实现**：

| 文件 | 形态 | 页面 |
|---|---|---|
| `findings-map.js` | `function esc(s)`（导出为 `PbcFindingsMap.esc`） | review |
| `settings-state.js` | `const esc = (s) =>` | settings |
| `upload-jobs.js` | `function esc(s)` | upload |

三份并存是**结构性的**：三个页面**互不加载**对方的 bundle，仓里没有公共基座脚本
（`templates/*.html` 的 `<script>` 列表已核）。但**没有任何机检**保证它们一致 ——
而 `findings-map.js` 的注释写着「esc 曾有 6 份副本，**收敛到这里**」，读者会以为
已单一真值（**实际还有 3 份**）。

**风险**：若日后只给一份补转义（例如属性上下文要额外转 `` ` `` / `=`），另两份
**静默**留在较弱版本 ⇒ 那两页的 `innerHTML` 站点重新开洞。这正是"同一语义写两处
必然漂移"。

## 判据

1. **副本集合已登记**：`static/*.js` 里的 esc 定义**恰好**是 `_ESC_COPIES` 登记的那几个
   （新增副本 ⇒ 红；登记的副本消失 ⇒ 红 = 陈旧登记）。
2. **行为等价**：逐份提取 `.replace(/pat/g, "entity")` 链，断言**逐项相同且顺序相同**。
3. **反空转**：断言链里**恰好**是那 5 个 (pattern, entity) 对，且扫描到的定义数 == 3。
4. **解码器镜像**（`review-locate.js` 的实体解码）：转义器**产出的**实体集合必须
   **⊆** 解码器能解的集合（解码器多出 `&nbsp;` 是允许的）；且 `&amp;` 必须**最后**解码
   （顺序陷阱，源码里已有实测注释）。

## 为什么用**文本**等价而不是行为等价

这个原语是**纯字面量替换链**（无控制流、无外部依赖）⇒ 链相同即行为相同。规范化按
`/pat/g` + 实体对提取，对引号风格与空白不敏感。行为测试要把三份各自从 IIFE 里抠出来
在 node 里跑，脆弱且并不比链等价更强。**行为**测试另有其一份（`test_findings_map_js.py`
对 `PbcFindingsMap.esc` 跑 node 断言 5 转义 + null/undefined）。

跑法：`pytest tests/unit/test_esc_single_source.py`
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"

#: esc 定义的形态（`function esc(s)` / `const esc = (s) =>` / `esc = function (s)`）
_DEF = re.compile(r"(?:function\s+esc\s*\(|(?:const|let|var)\s+esc\s*=\s*(?:function\s*)?\(|esc\s*=\s*function\s*\()")

#: `.replace(/pat/g, "entity")` —— 引号风格不限、flags 放宽到 `g[a-z]*`（解码器用 `/gi`）
_REPLACE = re.compile(r"""\.replace\(\s*/([^/]*)/g[a-z]*\s*,\s*(?:"([^"]*)"|'([^']*)')\s*\)""")

#: **连续**的 `.replace(...)` 链（至少一项）。
#: ⚠️ 不能用 `src.index(";")` 截断 —— `&amp;` 里的分号会提前截断窗口（实测踩过）。
_CHAIN = re.compile(r"(?:" + _REPLACE.pattern + r"\s*)+")

#: 允许存在的 esc 副本：(文件, 为什么不能合并到公共件)
_ESC_COPIES: dict[str, str] = {
    "findings-map.js": "review 页 bundle；review-findings/pageinfo/suppressions 从 PbcFindingsMap.esc 取",
    "settings-state.js": "settings 页 bundle —— 该页**不**加载 findings-map.js",
    "upload-jobs.js": "upload 页 bundle —— 该页**不**加载 findings-map.js",
}

#: 规范的 5 个 (字符, 实体) 对，**顺序敏感**（`&` 必须最先转义）
_CANON: tuple[tuple[str, str], ...] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#39;"),
)

#: 解码器所在文件（`review-locate.js` 的实体解码链）
_DECODER_FILE = "review-locate.js"


def _definitions() -> dict[str, int]:
    """`{文件名: 定义起始偏移}` —— 扫全部 `static/*.js`。"""
    out: dict[str, int] = {}
    for p in sorted(STATIC.glob("*.js")):
        src = p.read_text(encoding="utf-8")
        m = _DEF.search(src)
        if m:
            out[p.name] = m.start()
    return out


def _chain_pairs(src: str, start: int) -> list[tuple[str, str]]:
    """定义处之后**第一条连续 `.replace()` 链**的 (pattern, entity) 对（顺序保留）。"""
    m = _CHAIN.search(src, start)
    assert m, f"偏移 {start} 之后找不到 `.replace` 链 —— 提取器坏了"
    return [(pat, dq or sq) for pat, dq, sq in _REPLACE.findall(m.group(0))]


def _esc_chains() -> dict[str, list[tuple[str, str]]]:
    chains: dict[str, list[tuple[str, str]]] = {}
    for name, start in _definitions().items():
        src = (STATIC / name).read_text(encoding="utf-8")
        chains[name] = _chain_pairs(src, start)
    return chains


def test_scanned_definition_count_matches_registration():
    """反空转：扫到的定义集合必须**恰好**等于登记集合（新增/消失都判红）。"""
    found = set(_definitions())
    assert found == set(_ESC_COPIES), (
        f"esc 定义集合与 _ESC_COPIES 不一致。\n  实际扫描：{sorted(found)}\n"
        f"  已登记：{sorted(_ESC_COPIES)}\n"
        "新增副本 ⇒ 请先合并，或在此登记并写明**为什么不能合并**；"
        "登记项消失 ⇒ 请删除该登记（陈旧登记会让断言失明）。"
    )


def test_every_copy_escapes_exactly_the_canonical_five_in_order():
    """每份副本的替换链必须**逐项、按序**等于规范的 5 对。"""
    for name, pairs in _esc_chains().items():
        assert pairs == list(_CANON), (
            f"{name} 的 esc 替换链与规范不一致（顺序敏感：`&` 必须最先）。\n"
            f"  实际：{pairs}\n  规范：{list(_CANON)}"
        )


def test_copies_are_behaviorally_identical_to_each_other():
    """三份副本互相等价（这条是"漂移"的直接判据）。"""
    chains = _esc_chains()
    ref_name = "findings-map.js"
    ref = chains[ref_name]
    for name, pairs in chains.items():
        assert pairs == ref, (
            f"{name} 与 {ref_name} 的 esc 链不同 ⇒ 一份被改过而另一份没跟上：\n"
            f"  {name}: {pairs}\n  {ref_name}: {ref}"
        )


def test_canonical_chain_is_not_vacuous():
    """反空转：规范链非空、5 项、且首项是 `&`（防"提取器坏了 ⇒ 空链互相相等"）。"""
    assert len(_CANON) == 5
    assert _CANON[0] == ("&", "&amp;")
    # 提取器真的能从源码里抠出东西
    pairs = _esc_chains()["findings-map.js"]
    assert len(pairs) == 5, f"提取器只抠出 {len(pairs)} 项 —— 扫描坏了"


class TestEntityDecoderMirrorsTheEscaper:
    """转义器**产出的**实体必须可被解码器解开；且 `&amp;` 必须**最后**解码。"""

    @staticmethod
    def _decoder_entity_pairs() -> list[tuple[str, str]]:
        src = (STATIC / _DECODER_FILE).read_text(encoding="utf-8")
        pairs = [
            (pat, dq or sq)
            for pat, dq, sq in _REPLACE.findall(src)
            if pat.startswith("&")
        ]
        assert pairs, "解码器里没抠出实体对 —— 提取器坏了"
        return pairs

    def test_escaper_output_is_a_subset_of_decoder_input(self):
        """转义器产出的实体 ⊆ 解码器能解的实体（解码器多出 `&nbsp;` 是允许的）。"""
        escaped = {ent for _ch, ent in _CANON}
        decodable = {pat for pat, _ch in self._decoder_entity_pairs()}
        missing = escaped - decodable
        assert not missing, (
            f"esc 会产出这些实体，但解码器解不开：{sorted(missing)} ⇒ "
            "转义/解码不再互逆（多出 `&nbsp;` 属正常）"
        )

    def test_amp_is_decoded_last(self):
        """`&amp;` 必须**最后**解码 —— 否则 `&amp;lt;` 会被二次解码成 `<`（源码有实测注释）。"""
        pats = [pat for pat, _ch in self._decoder_entity_pairs()]
        assert pats[-1] == "&amp;", (
            f"实体解码链的最后一项应是 `&amp;`，实际是 {pats[-1]!r}（顺序：{pats}）"
        )


if __name__ == "__main__":                                   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
