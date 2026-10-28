"""许可证事实清单（**不是**合规判定）—— 面向"真正冻进产物的东西"。

## 为什么需要它（2026-10-28，R128 收尾）

`docs/DEPENDENCY_AUDIT.json` 只回答 CVE（顶层键里**没有** license 字段），而
`requirements.txt` 锁 **14** 个直接依赖、pip-audit 扫 **43** 个条目 —— 实测**真正冻进
`pbc-server.exe` 的是 90 个发行版**（从 PyInstaller 的 `PYZ-00.toc` 反查）。"入包件"比
"依赖清单"大得多，而这正是 MEMORY 里那条教训：**评估"可选件"不等于评估"入包件"**。

## 扫描域 = PyInstaller TOC，不是 site-packages

首版我扫的是整个 `site-packages`（514 个包）⇒ 混进一堆**根本没入包**的东西
（PyQt6 / PySide6 / edge-tts / EbookLib / phonemizer-fork …），其中 4 个被判 AGPL。
那是**扫描域错了**，与 `scrub_pii.py` 当初只扫 `git ls-files` 是同一类错误：
**工具的扫描集必须等于它守护的东西**。真正的判据是"这个模块是否被冻进产物"，
唯一可信来源是 PyInstaller 的 TOC（它按实际 import 图生成）。

## ⚠️ 分类顺序里有一条**必须**遵守：自由文本也要扫

首版只读 `License-Expression`（PEP 639）+ `Classifier`，结果把 **PyMuPDF 判成
`unknown`** —— 而它恰恰是唯一那个 AGPL 包：它的元数据既没有 expression、也没有
classifier，只有一个自由文本 `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex
Commercial Lic`。**只读结构化字段的分类器，恰好漏掉最该抓的那一个**
（同 R115「锚点写窄 = 功能等于没上线」）。

实测 90 个入包发行版里**只有 7 个**没有结构化字段，其中 6 个自由文本是
`MIT` / `BSD-3-Clause` 这类宽松许可，**唯一的 copyleft 就是 PyMuPDF** ——
即"自由文本通道"不是兜底装饰，它是这条结论的**唯一来源**。

⇒ 顺序：`License-Expression` → `Classifier` → **自由文本关键词扫描**。
自由文本命中的结论标 `basis="free-text"` —— 它**可能是误报**（例如"本项目是 BSD，
但内置的 X 是 AGPL"这种句子），所以要把原文带出来给人看，而不是只给一个标签。

## ⚠️ 不认识的许可证不许算"宽松"

第二版把"有 expression 但没命中 copyleft 词表"一律算 `permissive` —— 那等于
**词表之外一律放行**（MPL / EPL / CDDL 全会被静默吞掉，与 R115 同型）。
现在分三档：命中 copyleft 词表 ⇒ 具体族；命中宽松白名单 ⇒ `permissive`；
**声明了但对不上任何词表 ⇒ `other`**，原文照抄出来等人看。

## ⚠️ `unknown` 桶必须可分

第三版（本次）实跑发现 `unknown=118`，而输出里全打印成 `(无映射)` ——
**模块名根本没显示**，等于把"认不出它是谁"这条事实丢了。更糟的是这 118 里混着
**`win32con` / `win32evtlogutil` / `winerror`（= pywin32）**：`importlib.metadata`
的 `packages_distributions()` 对 pywin32 **没有 top-level 映射** ⇒ 一个真的第三方
依赖被塞进与 stdlib 同一个桶里。**桶里混着"不用管"和"要人工看"，这个桶就没有判别力。**

⇒ 未映射模块按 `stdlib` / `first-party` / `unresolved` 三分，`unresolved`
**逐条列出模块名**。本工具**不猜**（不给 pywin32 硬编码映射表）：猜出来的结论
与"未解析"在报告里长得一样，就再也分不清哪个是查过的。

## 本工具**不做**什么

不做合规判定、不给退出码（除工具自身错误）。**要不要因为 AGPL 换掉 PyMuPDF**
（TODO 0-67：买 Artifex 商业许可 / 换 `pypdfium2` / 接受 AGPL）是**产品与法务决定**，
本工具只提供事实。把它接成门禁 FAIL 之前必须先定案，否则会得到一个**恒红**的检查
（PITFALLS §三十一：恒红的门禁与恒真的判据一样零判别力）。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOC = REPO_ROOT / "build" / "pbc-server" / "PYZ-00.toc"

#: PyInstaller TOC 里每个条目形如 `('PIL.Image', 'C:\\…\\Image.py', 'PYMODULE')`
_TOC_ENTRY_RE = re.compile(r"\('([^']+)',\s*'([^']*)',\s*'([A-Z_]+)'\)")

#: copyleft 关键词。**顺序即优先级，AGPL 必须排在 GPL 前面** —— "GNU AFFERO GPL"
#: 里含 "GPL"，先判 GPL 会把 AGPL 降级成 GPL（分发义务不同，降级方向是**危险**的）。
#: MPL 排在最后：它是文件级弱 copyleft，但**不能算宽松**（改过的文件要开源）。
_COPYLEFT_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("AGPL", re.compile(r"affero|\bagpl\b|agpl-?3", re.I)),
    ("LGPL", re.compile(r"lesser general public|\blgpl\b", re.I)),
    ("GPL", re.compile(r"general public license|\bgpl\b|\bgplv?\d", re.I)),
    ("MPL", re.compile(r"mozilla public|\bmpl\b|mpl-?2", re.I)),
)

#: 宽松白名单 —— 只有**明确认出**才叫 permissive；认不出落 `other`，不默认放行。
_PERMISSIVE_RE = re.compile(
    r"\bmit\b|\bbsd\b|bsd-[23]-clause|apache|isc\b|python software foundation|\bpsf\b"
    r"|zlib|unlicense|public domain|\bcc0\b|\b0bsd\b",
    re.I)

#: 本项目自己的顶层包（**动态推导**，不写死清单 —— 写死就会在加包时静默失效）。
_FIRST_PARTY_IGNORE = frozenset({"build", "dist", "dist-electron", "node_modules",
                                 "tests", "scripts", "devlogs", "docs", "static"})


def _first_party_tops(root: Path = REPO_ROOT) -> set[str]:
    """仓库自己的顶层模块名：根目录 `*.py` + 含 `__init__.py` 的子目录。"""
    names = {p.stem for p in root.glob("*.py")}
    for d in root.iterdir():
        if d.is_dir() and d.name not in _FIRST_PARTY_IGNORE \
                and (d / "__init__.py").is_file():
            names.add(d.name)
    return names


def parse_pyz_toc(text: str) -> set[str]:
    """从 `PYZ-00.toc` 文本里取**顶层模块名**（`a.b.c` → `a`）。

    ⚠️ 用正则而不是 `ast.literal_eval`：两者在**当前**文件上都能跑（实测
    `literal_eval` 返回 `(path, [ ... ])` 二元组）—— 早先"第 13756 行
    `unmatched ')'`"的记录 **2026-10-28 复现不了**，按未证实处理，不作为依据。
    选正则的真实理由是：只需要条目里的名字，不需要把 870 KB 建成 AST，也不依赖
    "整个文件是一个合法字面量"。正确性由 `test_license_scan.py` 里那条
    **双解析器交叉比对**用例保证（正则 vs `literal_eval`，结果必须一致）。

    ⚠️ **路径为 `'-'` 的条目要跳过**：那是 namespace 包的**占位行**（实测
    `('js', '-', 'PYMODULE')`），没有文件 ⇒ 没有代码随产物分发 ⇒ 没有许可证义务。
    不跳的话它会落进"未解析"桶，把真正的未解析项（pywin32）淹掉。其子模块是
    独立条目，不会因为跳过占位行而漏扫。
    """
    tops: set[str] = set()
    for name, path, _kind in _TOC_ENTRY_RE.findall(text):
        if not name or name.startswith("_") or path.strip() == "-":
            continue
        tops.add(name.split(".", 1)[0])
    return tops


def _copyleft_of(blob: str) -> tuple[str, str] | None:
    for family, pat in _COPYLEFT_RULES:
        m = pat.search(blob)
        if m:
            return family, m.group(0)
    return None


def _window(blob: str, needle: str, pad: int = 24) -> str:
    """把命中处前后文一起带出来 —— 只给 `AFFERO` 一词没法复核。"""
    i = blob.lower().find(needle.lower())
    if i < 0:
        return needle
    return blob[max(0, i - pad): i + len(needle) + pad].strip()


def classify_license(expression: str | None,
                     classifiers: list[str] | None,
                     free_text: str | None) -> dict:
    """按 **expression → classifier → 自由文本** 的顺序判定许可证族。

    返回 `{"family", "basis", "evidence"}`：

    * `family` ∈ `AGPL / GPL / LGPL / MPL / permissive / other / unknown`
    * `basis`  ∈ `expression / classifier / free-text / none` —— **必须带出来**，
      因为 `free-text` 的结论可能误报（句子层面提到别的许可证）。
    * `evidence` 是命中的那一小段原文，方便人复核。

    先判 copyleft 再判宽松：一个包同时说"AGPL 或商业许可"时，**不能**因为
    看到 "Commercial" 就归成宽松。**声明了但对不上词表 ⇒ `other`，不默认放行。**
    """
    classifiers = classifiers or []
    declared = [("expression", (expression or "").strip()),
                ("classifier", " ".join(classifiers).strip())]
    declared = [(b, blob) for b, blob in declared if blob]
    for basis, blob in declared:
        hit = _copyleft_of(blob)
        if hit:
            return {"family": hit[0], "basis": basis, "evidence": _window(blob, hit[1])}
        if _PERMISSIVE_RE.search(blob):
            return {"family": "permissive", "basis": basis, "evidence": blob[:60]}
        # 声明了、但词表里没有 ⇒ 既不是 copyleft 也不是已知宽松，**不许算通过**
        return {"family": "other", "basis": basis, "evidence": blob[:60]}

    text = (free_text or "").strip()
    if text:
        hit = _copyleft_of(text)
        if hit:
            return {"family": hit[0], "basis": "free-text",
                    "evidence": _window(text, hit[1])}
        if _PERMISSIVE_RE.search(text):
            return {"family": "permissive", "basis": "free-text", "evidence": text[:60]}
        return {"family": "other", "basis": "free-text", "evidence": text[:60]}
    return {"family": "unknown", "basis": "none", "evidence": ""}


def _dist_license(dist: str) -> dict:
    try:
        meta = md.metadata(dist)
    except md.PackageNotFoundError:
        return {"family": "unknown", "basis": "none",
                "evidence": "元数据不可用（未安装？）"}
    expression = meta.get("License-Expression")
    classifiers = [c for c in (meta.get_all("Classifier") or [])
                   if c.startswith("License ::")]
    free_text = meta.get("License")
    return classify_license(expression, classifiers, free_text)


#: 行的种类。`dist` 行参与许可证统计，其余三类只是"为什么它没被统计"。
KIND_DIST, KIND_STDLIB, KIND_FIRST_PARTY, KIND_UNRESOLVED = (
    "dist", "stdlib", "first-party", "unresolved")

_FAMILY_ORDER = {"AGPL": 0, "GPL": 1, "LGPL": 2, "MPL": 3,
                 "other": 4, "unknown": 5, "permissive": 6}


def inventory(tops: set[str], *, packages_distributions=None,
              first_party: set[str] | None = None,
              stdlib: set[str] | None = None) -> list[dict]:
    """顶层模块名 → 发行版 → 许可证。返回按"风险从高到低"排序的行。

    `packages_distributions()` 是 `importlib.metadata` 的公开映射（top-level → 发行版）。
    映射不到的行**不丢**，但要分清三种"没有映射"：`stdlib`（不用管）、
    `first-party`（本项目）、`unresolved`（**可能是真依赖，要人工看**）。
    合并成一个 `unknown` 桶就等于把第三种藏起来 —— pywin32 就是这么被藏掉的。
    """
    p2d = packages_distributions if packages_distributions is not None \
        else md.packages_distributions()
    first_party = first_party if first_party is not None else _first_party_tops()
    stdlib = stdlib if stdlib is not None else set(sys.stdlib_module_names)

    seen: dict[str, set[str]] = {}
    unmapped: list[str] = []
    for top in tops:
        dists = p2d.get(top) or []
        if not dists:
            unmapped.append(top)
            continue
        for d in dists:
            seen.setdefault(d, set()).add(top)

    rows: list[dict] = []
    for dist in sorted(seen, key=str.lower):
        rows.append({"kind": KIND_DIST, "dist": dist,
                     "modules": sorted(seen[dist]), **_dist_license(dist)})
    for top in sorted(unmapped):
        if top in stdlib:
            kind, note = KIND_STDLIB, "标准库"
        elif top in first_party:
            kind, note = KIND_FIRST_PARTY, "本项目模块"
        else:
            kind, note = KIND_UNRESOLVED, "无发行版映射且非标准库/本项目 ⇒ 需人工确认"
        rows.append({"kind": kind, "dist": None, "modules": [top],
                     "family": "unknown", "basis": "none", "evidence": note})
    rows.sort(key=lambda r: (r["kind"] != KIND_DIST,
                             _FAMILY_ORDER.get(r["family"], 9),
                             str(r["dist"]).lower(), str(r["modules"])))
    return rows


def summarize(rows: list[dict]) -> dict:
    dists = [r for r in rows if r["kind"] == KIND_DIST]
    counts: dict[str, int] = {}
    for r in dists:
        counts[r["family"]] = counts.get(r["family"], 0) + 1
    return {
        "distributions": len(dists),
        "stdlib_modules": len([r for r in rows if r["kind"] == KIND_STDLIB]),
        "first_party_modules": len([r for r in rows if r["kind"] == KIND_FIRST_PARTY]),
        "unresolved_modules": sorted(r["modules"][0] for r in rows
                                     if r["kind"] == KIND_UNRESOLVED),
        "by_family": counts,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="许可证事实清单（只报告，不做合规判定）")
    ap.add_argument("--toc", default=str(DEFAULT_TOC),
                    help="PyInstaller 的 PYZ TOC（默认 build/pbc-server/PYZ-00.toc）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--out", help="同时把 JSON 写到该路径")
    ap.add_argument("--show-permissive", action="store_true",
                    help="连 permissive 也打印（默认只打印 copyleft / other / unknown）")
    args = ap.parse_args(argv)

    toc = Path(args.toc)
    if not toc.is_file():
        print(f"[FATAL] 找不到 TOC：{toc}\n"
              f"        先跑一次构建（build.ps1 的 PyInstaller 阶段）再扫。",
              file=sys.stderr)
        return 2
    tops = parse_pyz_toc(toc.read_text(encoding="utf-8", errors="replace"))
    if not tops:
        print(f"[FATAL] {toc} 里没解析出任何模块名 —— TOC 形态变了？"
              f"（本工具的正则是唯一的解析实现，改格式要同步改它）", file=sys.stderr)
        return 2

    rows = inventory(tops)
    summary = summarize(rows)
    payload = {"toc": str(toc), "summary": summary,
               "packages": [r for r in rows if r["kind"] == KIND_DIST],
               "unresolved": summary["unresolved_modules"]}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"扫描域: {toc}")
        print(f"冻结发行版 {summary['distributions']} 个")
        print(f"未映射模块 {summary['stdlib_modules'] + summary['first_party_modules'] + len(summary['unresolved_modules'])} 个"
              f" = stdlib {summary['stdlib_modules']}"
              f" + 本项目 {summary['first_party_modules']}"
              f" + **未解析 {len(summary['unresolved_modules'])}**")
        print("按族: " + ", ".join(f"{k}={v}" for k, v in
                                   sorted(summary["by_family"].items())))
        shown = [r for r in rows if r["kind"] == KIND_DIST
                 and (args.show_permissive or r["family"] != "permissive")]
        if shown:
            print(f"\n{'发行版':<28}{'族':<10}{'依据':<12}证据")
            print("-" * 96)
            for r in shown:
                print(f"{r['dist']:<28}{r['family']:<10}"
                      f"{r['basis']:<12}{r['evidence'][:44]}")
        if summary["unresolved_modules"]:
            print("\n⚠️ 未解析模块（无发行版映射，且不是标准库/本项目）—— **必须人工确认**：")
            for r in rows:
                if r["kind"] == KIND_UNRESOLVED:
                    print(f"   - {r['modules'][0]}")
            print("   注：`importlib.metadata` 对 pywin32 之类的包没有 top-level 映射，"
                  "本工具**不猜**（猜出来的结论与'没查过'长得一样）。")
        print("\n⚠️ 本清单是**事实**，不是合规判定。copyleft 包是否可随本项目产物分发，"
              "\n   属法务/产品决定（TODO 0-67）。basis=free-text 的行**可能误报** —— "
              "\n   自由文本里可能只是在描述「内置组件」的许可。")

    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        if not args.json:
            print(f"\n已写出: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
