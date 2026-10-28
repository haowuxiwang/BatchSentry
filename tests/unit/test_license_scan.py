"""`scripts/license_scan.py` 的解析 / 分类 / 分桶护栏。

这个脚本产出的是**要拿去做分发决策的事实**，所以三类错误都必须被钉住：

1. **解析漏项**：TOC 是 PyInstaller 的 repr 文本，`ast.literal_eval` 在实测文件上
   直接炸（末尾多一个 `)`）。正则一旦写窄，扫出来的发行版数量就会**静默变少** ——
   而"少了几个"在报告里看不出来（数字变小仍然像个正常数字）。
2. **分类降级**：`"GNU AFFERO GPL"` 里含 `"GPL"`。顺序写反会把 AGPL 判成 GPL，
   而**降级方向是危险的**（GPL 与 AGPL 的分发义务不同）。同理：认不出的许可证
   不许算 `permissive`（词表之外一律放行 = 与 R115「锚点写窄」同型）。
3. **分桶合并**：`stdlib` / `first-party` / `unresolved` 合成一个 `unknown` 桶，
   等于把"要人工确认"的那几个藏进"不用管"的一百多个里。实测 pywin32 的
   `win32*` 就是这么被藏掉的（`packages_distributions()` 对它没有 top-level 映射）。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts import license_scan as ls  # noqa: E402


# ── 解析 ────────────────────────────────────────────────────────────────────

def test_parse_takes_top_level_names_only():
    toc = ("[('PIL.Image', 'C:\\\\x\\\\Image.py', 'PYMODULE'),\n"
           " ('PIL._tkinter_finder', 'C:\\\\x\\\\t.py', 'PYMODULE'),\n"
           " ('fastapi', 'C:\\\\x\\\\f.py', 'PYMODULE')]")
    assert ls.parse_pyz_toc(toc) == {"PIL", "fastapi"}


def test_parse_agrees_with_literal_eval_on_the_real_toc():
    """**双解析器交叉比对**：正则与 `ast.literal_eval` 必须得到同一组顶层模块名。

    这是本文件里唯一能证明"正则没写窄"的用例 —— 夹具是我自己造的，造不出
    PyInstaller 的真实排版。⚠️ 附带纠正一条**错误记录**：早先 docstring 写
    "`literal_eval` 在本文件上报 `unmatched ')'`" —— 2026-10-28 实测**不成立**
    （它能跑通）。正则的理由是"不必建 AST"，不是"literal_eval 会炸"。
    """
    import ast
    if not ls.DEFAULT_TOC.is_file():
        pytest.skip("没有构建产物（build/ 不入库）")
    text = ls.DEFAULT_TOC.read_text(encoding="utf-8", errors="replace")
    _path, entries = ast.literal_eval(text)
    expect = {n.split(".", 1)[0] for n, p, _k in entries
              if n and not n.startswith("_") and str(p).strip() != "-"}
    assert ls.parse_pyz_toc(text) == expect


def test_parse_skips_namespace_placeholders():
    """路径 `'-'` = namespace 占位行，没有文件就没有许可证义务。

    实测 `('js', '-', 'PYMODULE')`；不跳的话它会落进 `unresolved` 桶，
    把真正要人工确认的 pywin32 淹掉。
    """
    toc = ("[('js', '-', 'PYMODULE'),\n"
           " ('win32con', 'C:\\\\Lib\\\\win32\\\\win32con.py', 'PYMODULE')]")
    assert ls.parse_pyz_toc(toc) == {"win32con"}


def test_parse_skips_private_and_empty_names():
    toc = "[('_pytest', 'p.py', 'PYMODULE'), ('', 'q.py', 'PYMODULE')]"
    assert ls.parse_pyz_toc(toc) == set()


# ── 分类 ────────────────────────────────────────────────────────────────────

def test_agpl_is_matched_before_gpl():
    """`"GNU AFFERO GPL 3.0"` 含 `"GPL"` —— 顺序写反会**降级**成 GPL。"""
    got = ls.classify_license(None, None,
                              "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License")
    assert got["family"] == "AGPL", got
    assert got["basis"] == "free-text", got


def test_free_text_is_a_real_channel_not_a_fallback_decoration():
    """PyMuPDF 的形态：**没有** `License-Expression`，**没有** `License ::` classifier。

    只读结构化字段的分类器会把它判成 `unknown` —— 恰好漏掉唯一那个 AGPL 包。
    """
    got = ls.classify_license(None, [], "GNU AFFERO GPL 3.0")
    assert (got["family"], got["basis"]) == ("AGPL", "free-text"), got
    assert "AFFERO" in got["evidence"].upper()


def test_lgpl_is_not_folded_into_gpl():
    got = ls.classify_license(None, ["License :: OSI Approved :: GNU Lesser General Public License v2 (LGPLv2)"], None)
    assert got["family"] == "LGPL", got


def test_mpl_is_not_permissive():
    """MPL 是文件级弱 copyleft —— 改过的文件要开源，不能算宽松。"""
    got = ls.classify_license("MPL-2.0", None, None)
    assert got["family"] == "MPL", got


def test_declared_expression_beats_free_text():
    """PEP 639 的 `License-Expression` 是权威；自由文本里的 `AGPL` 说的是**内置组件**。"""
    got = ls.classify_license("MIT", None, "bundles GNU AFFERO GPL 3.0 code")
    assert got["family"] == "permissive", got
    assert got["basis"] == "expression", got


def test_unrecognised_declared_license_is_other_not_permissive():
    """词表之外的声明（EPL / CDDL / 自定义）**不许**默认放行。"""
    got = ls.classify_license("EPL-2.0", None, None)
    assert got["family"] == "other", got
    assert "EPL-2.0" in got["evidence"]


def test_no_metadata_at_all_is_unknown():
    got = ls.classify_license(None, None, None)
    assert got == {"family": "unknown", "basis": "none", "evidence": ""}


@pytest.mark.parametrize("text", ["MIT", "BSD-3-Clause", "Apache-2.0",
                                  "ISC", "PSF-2.0", "The Unlicense"])
def test_common_permissive_free_text_is_recognised(text):
    assert ls.classify_license(None, None, text)["family"] == "permissive"


# ── 分桶 ────────────────────────────────────────────────────────────────────

def _inv(tops, mapping):
    return ls.inventory(tops, packages_distributions=mapping,
                        first_party={"api", "core"}, stdlib={"json", "sqlite3"})


def test_unmapped_modules_are_split_into_three_kinds():
    rows = _inv({"json", "api", "win32con", "fastapi"}, {"fastapi": ["fastapi"]})
    kinds = {r["modules"][0]: r["kind"] for r in rows}
    assert kinds["json"] == ls.KIND_STDLIB, kinds
    assert kinds["api"] == ls.KIND_FIRST_PARTY, kinds
    assert kinds["win32con"] == ls.KIND_UNRESOLVED, kinds
    assert kinds["fastapi"] == ls.KIND_DIST, kinds


def test_unresolved_modules_are_named_not_merely_counted():
    """**桶里混着"不用管"和"要人工看"，这个桶就没有判别力。**"""
    s = ls.summarize(_inv({"json", "api", "win32con", "winerror"},
                          {}))
    assert s["unresolved_modules"] == ["win32con", "winerror"], s
    assert (s["stdlib_modules"], s["first_party_modules"]) == (1, 1), s


def test_by_family_counts_only_distributions():
    """未映射行一律 `family=unknown`；若一起统计，`unknown` 会虚高到 100+。"""
    s = ls.summarize(_inv({"json", "api", "win32con"}, {}))
    assert s["distributions"] == 0, s
    assert s["by_family"] == {}, s


def test_rows_put_risky_families_first(monkeypatch):
    """`AGPL` 必须排在最前 —— 报告的第一屏就该是最需要人看的那个。"""
    fams = {"pkg-gpl": "GPL", "pkg-mit": "permissive", "pkg-agpl": "AGPL"}
    monkeypatch.setattr(ls, "_dist_license",
                        lambda d: {"family": fams[d], "basis": "expression",
                                   "evidence": fams[d]})
    rows = ls.inventory({"a", "b", "c"},
                        packages_distributions={"a": ["pkg-mit"], "b": ["pkg-gpl"],
                                                "c": ["pkg-agpl"]},
                        first_party=set(), stdlib=set())
    assert [r["dist"] for r in rows] == ["pkg-agpl", "pkg-gpl", "pkg-mit"], rows


# ── CLI ─────────────────────────────────────────────────────────────────────

def _write_toc(tmp_path: Path, names=("json", "win32con")) -> Path:
    body = ",\n".join(f"  ('{n}', 'C:\\\\x\\\\{n}.py', 'PYMODULE')" for n in names)
    p = tmp_path / "PYZ-00.toc"
    p.write_text(f"('C:\\\\x', [\n{body}])", encoding="utf-8")
    return p


def test_cli_prints_unresolved_modules(tmp_path, capsys):
    rc = ls.main(["--toc", str(_write_toc(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "未解析" in out and "win32con" in out, out


def test_cli_missing_toc_is_a_tool_error_not_a_clean_report(tmp_path, capsys):
    rc = ls.main(["--toc", str(tmp_path / "nope.toc")])
    assert rc == 2, rc
    assert "FATAL" in capsys.readouterr().err


def test_cli_unparseable_toc_fails_loudly(tmp_path, capsys):
    """解析出 0 个模块时**不许**输出"0 个发行版，一切正常"。"""
    p = tmp_path / "PYZ-00.toc"
    p.write_text("garbage", encoding="utf-8")
    assert ls.main(["--toc", str(p)]) == 2


def test_cli_json_payload_has_no_unmapped_noise(tmp_path, capsys):
    import json
    rc = ls.main(["--toc", str(_write_toc(tmp_path)), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["unresolved"] == ["win32con"], payload
    assert all(p["kind"] == ls.KIND_DIST for p in payload["packages"]), payload


# ── 反空转：真实 TOC 的形状 ────────────────────────────────────────────────

@pytest.mark.skipif(not ls.DEFAULT_TOC.is_file(),
                    reason="没有构建产物（build/ 不入库）⇒ 本机没得扫")
def test_real_toc_yields_a_plausible_surface():
    """**反空转**：解析器坏掉时上面那些用例仍可能全绿（夹具是我造的）。

    这条钉住实测形状：90 个发行版 / 200 左右顶层模块。数字变了不是坏事，
    但必须**有人来看一眼**为什么变。
    """
    tops = ls.parse_pyz_toc(ls.DEFAULT_TOC.read_text(encoding="utf-8", errors="replace"))
    assert 150 < len(tops) < 400, len(tops)
    s = ls.summarize(ls.inventory(tops))
    assert 60 < s["distributions"] < 150, s
    assert s["distributions"] > len(s["unresolved_modules"]), s
