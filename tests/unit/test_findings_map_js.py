"""Finding 级中文映射单一真值的机检护栏（R63 P2 收敛）。

背景：severity / source / finding-type / finding-status 四份映射此前有
**两份活副本** —— `static/review.js` 的 renderFindings 局部字面量（AJAX
翻页渲染）与 `templates/review.html` 头部的 Jinja2 `set`（SSR 首屏渲染）。
新增枚举漏改一处即出现"首屏英文、翻页后变中文"（SSR/AJAX 不同源）。

本文件锁三件事（模式与 tests/unit/test_status_js.py 对 job 状态的做法一致）：
1. **共享件 ↔ 模板逐值一致** —— PbcFindingsMap 与 review.html 的四个
   Jinja2 set 字典逐值相等（措辞漂移即用户可见的不一致）；
2. **键集覆盖后端** —— TYPE_ZH / SEVERITY_ZH / FINDING_STATUS_ZH 的键集
   必须覆盖 core/zh_map.py 对应表（后端有的前端必须能显示）。**只锁键集
   不锁措辞**：后端措辞供 report/notify 使用，与前端存在已知历史差异
   （如 param_out_of_spec「参数越界」vs「参数超标」），统一措辞会改变
   用户可见文案，须单独评估；
3. **行为兜底** —— 未知枚举显示「未知(x)」而非裸英文；esc 全量转义
   （LLM 输出属不可信文本）。

node 不可用时跳过 JS 侧（打包链不依赖），但 Jinja2 解析与 Python 侧
键集检查始终执行。
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.zh_map import (  # noqa: E402
    FINDING_STATUS_ZH,
    FINDING_TYPE_ZH,
    SEVERITY_ZH,
)

FINDINGS_MAP_JS = REPO / "static" / "findings-map.js"
REVIEW_HTML = REPO / "templates" / "review.html"


def _run_in_node(probe_body: str):
    """跑 `static/findings-map.js` + 探针，返回最后一行 JSON。"""
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "findings_map_probe.js"
        p.write_text(
            FINDINGS_MAP_JS.read_text(encoding="utf-8") + "\n" + probe_body,
            encoding="utf-8",
        )
        r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                           timeout=30)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _jinja2_sets(html: str) -> dict[str, dict]:
    """提取 review.html 头部 `{% set xxx = {...} %}` 的字典字面量。

    返回 {变量名: dict}。字面量是标准 JS/Python 兼容的 dict 语法
    （单引号键值、无表达式），用 ast.literal_eval 解析。
    """
    out: dict[str, dict] = {}
    for m in re.finditer(r"\{%\s*set\s+(\w+)\s*=\s*(\{.*?\})\s*%\}", html):
        name, lit = m.group(1), m.group(2)
        try:
            out[name] = ast.literal_eval(lit)
        except (ValueError, SyntaxError) as exc:
            raise AssertionError(f"解析 review.html 的 set {name} 失败：{exc}") from exc
    return out


# JS 侧四个映射表（node 实跑，一次取回）
_JS_MAPS = None


def _js_maps() -> dict[str, dict]:
    global _JS_MAPS
    if _JS_MAPS is None:
        _JS_MAPS = _run_in_node(
            "console.log(JSON.stringify({"
            "severity: PbcFindingsMap.SEVERITY_ZH,"
            "source: PbcFindingsMap.SOURCE_ZH,"
            "type: PbcFindingsMap.TYPE_ZH,"
            "status: PbcFindingsMap.FINDING_STATUS_ZH,"
            "confidence: PbcFindingsMap.CONFIDENCE_ZH}));"
        )
    return _JS_MAPS


class TestSharedModuleExists:
    def test_findings_map_js_present_and_exported(self):
        """共享件存在且挂在 PbcFindingsMap 上（页面模块靠这个名字引用）。"""
        src = FINDINGS_MAP_JS.read_text(encoding="utf-8")
        assert "global.PbcFindingsMap" in src
        for fn in ("zhOrUnknown", "esc", "typeZh", "severityZh"):
            assert fn in src, f"PbcFindingsMap 缺少 {fn}"

    def test_review_html_includes_findings_map_js(self):
        """复核页必须引入共享件 —— 否则收敛名存实亡（仍各持副本）。"""
        html = REVIEW_HTML.read_text(encoding="utf-8")
        assert "/static/findings-map.js" in html, (
            "review.html 未引入 findings-map.js"
        )


class TestParityWithTemplate:
    """共享件 ↔ SSR 模板逐值一致 —— 任一单边漂移 = 首屏与翻页后文案不一致。"""

    # Jinja2 set 变量名 ↔ JS 映射名 的对应关系
    PAIRS = [
        ("severity_zh", "severity"),
        ("source_zh", "source"),
        ("status_zh", "status"),
        ("type_zh", "type"),
        ("confidence_zh", "confidence"),
    ]

    def test_all_maps_agree(self):
        html = REVIEW_HTML.read_text(encoding="utf-8")
        sets = _jinja2_sets(html)
        js = _js_maps()
        for tpl_name, js_name in self.PAIRS:
            assert tpl_name in sets, f"review.html 缺少 set {tpl_name}"
            py: dict = sets[tpl_name]
            assert py == js[js_name], (
                f"{tpl_name}（模板）与 PbcFindingsMap.{js_name} 漂移："
                f"模板独有 { {k: v for k, v in py.items() if js[js_name].get(k) != v} }，"
                f"JS 独有 { {k: v for k, v in js[js_name].items() if py.get(k) != v} }"
            )

    def test_detector_is_not_vacuous(self):
        """正向对照：Jinja2 set 提取器必须真的能提出四个映射（防空断言）。"""
        sets = _jinja2_sets(REVIEW_HTML.read_text(encoding="utf-8"))
        for tpl_name, _ in self.PAIRS:
            assert tpl_name in sets
            assert sets[tpl_name], f"{tpl_name} 提取结果为空"


class TestCoversPythonSide:
    """键集覆盖 core/zh_map.py —— 后端有的枚举前端必须能显示中文。

    只锁键集不锁措辞（见模块 docstring 第 2 点）。
    """

    def test_type_keys_cover_python(self):
        missing = sorted(set(FINDING_TYPE_ZH) - set(_js_maps()["type"]))
        assert not missing, f"TYPE_ZH 缺少后端已收录的类型：{missing}"

    def test_severity_keys_cover_python(self):
        missing = sorted(set(SEVERITY_ZH) - set(_js_maps()["severity"]))
        assert not missing, f"SEVERITY_ZH 缺少后端枚举：{missing}"

    def test_finding_status_keys_cover_python(self):
        missing = sorted(set(FINDING_STATUS_ZH) - set(_js_maps()["status"]))
        assert not missing, f"FINDING_STATUS_ZH 缺少后端枚举：{missing}"

    def test_js_may_have_extra_llm_enum_keys(self):
        """正向对照：JS 超集是**允许的** —— low_confidence / time_anomaly 是
        LLM 自由输出枚举，前端必须能兜底。锁住它们真的在表里（否则退化成
        "未知(x)"，会让用户误以为数据坏了）。"""
        js_types = _js_maps()["type"]
        for key in ("low_confidence", "time_anomaly"):
            assert key in js_types, f"TYPE_ZH 应收录 LLM 自由枚举 {key}"


class TestBehaviorFallbacks:
    """行为兜底：未知枚举 / 转义 —— node 实跑（可被变异打红）。"""

    def test_unknown_enum_is_flagged_not_raw(self):
        out = _run_in_node(
            "console.log(JSON.stringify(["
            "PbcFindingsMap.typeZh('brand_new_type'),"
            "PbcFindingsMap.severityZh('weird_sev'),"
            "PbcFindingsMap.sourceZh(''),"
            "PbcFindingsMap.findingStatusZh(null)]));"
        )
        assert out == [
            "未知(brand_new_type)",
            "未知(weird_sev)",
            "",
            "",
        ]

    def test_known_enum_translates(self):
        out = _run_in_node(
            "console.log(JSON.stringify(["
            "PbcFindingsMap.typeZh('time_reversal'),"
            "PbcFindingsMap.severityZh('critical'),"
            "PbcFindingsMap.sourceZh('llm_cross'),"
            "PbcFindingsMap.findingStatusZh('pending')]));"
        )
        assert out == ["时间倒序", "严重", "LLM跨页", "待复核"]

    def test_esc_escapes_all_five(self):
        out = _run_in_node(
            "console.log(JSON.stringify(PbcFindingsMap.esc("
            "'<img src=x onerror=alert(1)>\"&\\'')));"
        )
        assert out == "&lt;img src=x onerror=alert(1)&gt;&quot;&amp;&#39;"

    def test_esc_handles_null_and_undefined(self):
        out = _run_in_node(
            "console.log(JSON.stringify([PbcFindingsMap.esc(null),"
            "PbcFindingsMap.esc(undefined)]));"
        )
        assert out == ["", ""]
