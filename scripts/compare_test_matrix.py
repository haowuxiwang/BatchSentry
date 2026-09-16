#!/usr/bin/env python
"""对比「CI 上收集到的用例」与「本地收集到的用例」，逐条列出差异。

**为什么需要它**：CI 与本地跑到的用例数**天然不同** —— 本机有未入库的构建产物
（`dist/pbc-server.exe`、真实 Paddle/MinerU 产物、`dist-electron*`），对应的
artifact-gated 用例在干净检出上必然 skip。要回答"到底哪些没跑、为什么"，唯一依据是
**逐条用例清单**。此前只能靠残缺的 stdout 日志猜（见 `docs/ADVERSARIAL_AUDIT.md` §13.2），
本工具把它变成一条命令。

**口径唯一**：nodeid 还原**复用** `scripts/release_gate.py::_junit_nodeid` —— 那是
唯一真值，本工具不复制第二套实现（复制必然漂移）。

用法：
    PY="C:/Users/WuSiTan/AppData/Local/Programs/Python/Python311/python.exe"

    # ① CI 侧：从 CI 的 artifact（gate-report）里取 gate_junit_<stamp>.xml
    # ② 本地侧：现场 collect（输出是 CRLF，本工具按 UTF-8 + 去 \\r 处理）
    "$PY" -m pytest tests --collect-only -q -p no:cacheprovider > local_ids.txt

    "$PY" scripts/compare_test_matrix.py --ci-junit ci/gate_junit_20260916_101530.xml \
        --local-collect local_ids.txt

退出码：0 = 两侧一致；1 = 有差异（便于把"差异必须被解释"接进流程）。
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# `pytest --collect-only -q` 的用例行：`path/to/test_x.py` 或 `...py::Cls::test_m`。
# 汇总行（`2362 tests collected in 12.34s`）与百分比行都不以 `.py` 结尾 → 天然被排除。
_COLLECT_LINE = re.compile(r"^[A-Za-z0-9_./\\-]+\.py(?:::[^\s]+)?$")


def _load_release_gate():
    """加载门禁模块，取它作为"nodeid 还原口径"的唯一真值来源。"""
    path = Path(__file__).resolve().parent / "release_gate.py"
    spec = importlib.util.spec_from_file_location("release_gate", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["release_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def parse_collect(text: str) -> set[str]:
    """从 `pytest --collect-only -q` 的输出文本提取 nodeid 集合。"""
    ids: set[str] = set()
    for line in text.splitlines():
        s = line.strip().rstrip("\r")
        if s and _COLLECT_LINE.match(s):
            ids.add(s.replace("\\", "/"))
    return ids


def parse_junit(xml_path: Path, rg) -> tuple[set[str], dict[str, int]]:
    """从 junitxml 提取 (nodeid 集合, 状态计数)。

    注意 junit 里 **skip 的用例也在**（`<testcase><skipped/>`）—— 所以
    "只被本地收集"才是真正的"CI 上不存在"，而 skip 数单独给出便于对账。
    """
    root = ET.parse(str(xml_path)).getroot()
    ids: set[str] = set()
    counts = {"passed": 0, "skipped": 0, "failed": 0}
    for case in root.iter("testcase"):
        node = rg._junit_nodeid(case.get("classname") or "", case.get("name") or "")
        if node:
            ids.add(node)
        if case.find("skipped") is not None:
            counts["skipped"] += 1
        elif case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
        else:
            counts["passed"] += 1
    return ids, counts


def diff_matrices(local: set[str], ci: set[str]) -> tuple[set[str], set[str]]:
    """返回 (只被本地收集, 只被 CI 收集)。纯函数，便于单测。"""
    return local - ci, ci - local


def group_by_file(ids: set[str]) -> dict[str, list[str]]:
    """按测试文件聚合，便于"整文件缺失"这类结论一眼可见。"""
    grouped: dict[str, list[str]] = {}
    for node in sorted(ids):
        grouped.setdefault(node.split("::", 1)[0], []).append(node)
    return grouped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="对比 CI junit 与本地 collect 的用例矩阵")
    ap.add_argument("--ci-junit", required=True,
                    help="CI artifact 里的 devlogs/gate_junit_*.xml")
    ap.add_argument("--local-collect", required=True,
                    help="本地 pytest --collect-only -q 的输出文件")
    ap.add_argument("--max-list", type=int, default=0,
                    help="每个文件最多列出几条 nodeid（0 = 全部）")
    args = ap.parse_args(argv)

    rg = _load_release_gate()
    ci_ids, counts = parse_junit(Path(args.ci_junit), rg)
    local_text = Path(args.local_collect).read_text(encoding="utf-8", errors="replace")
    local_ids = parse_collect(local_text)

    only_local, only_ci = diff_matrices(local_ids, ci_ids)

    print(f"本地 collect : {len(local_ids)}")
    print(f"CI junit     : {len(ci_ids)}  "
          f"(passed={counts['passed']} skipped={counts['skipped']} "
          f"failed={counts['failed']})")
    print()

    def _report(title: str, ids: set[str]) -> None:
        print(f"── {title}：{len(ids)} 条")
        for f, nodes in group_by_file(ids).items():
            print(f"   {f}  ({len(nodes)})")
            shown = nodes if args.max_list <= 0 else nodes[:args.max_list]
            for n in shown:
                print(f"      {n}")
            if len(shown) < len(nodes):
                print(f"      ... 另有 {len(nodes) - len(shown)} 条")
        print()

    _report("只被本地收集（CI 上不存在）", only_local)
    _report("只被 CI 收集（本地没有）", only_ci)

    return 1 if (only_local or only_ci) else 0


if __name__ == "__main__":
    raise SystemExit(main())
