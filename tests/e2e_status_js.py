"""产物内 `status.js` 的**行为**判据 —— e2e 与变异验证共用（**只有一处实现**）。

为什么必须是"行为"而不是"文本"（Round 54 实测的教训）：

* `#127` 的视觉修复（`partial_review` 不得显示成成功态）实现在**单一真值**
  `status.js` 的 `statusDotClass()` 里，而 `upload.js` 只负责**调用**它
  （`PbcStatus.statusDotClass(st)`）。所以断言 `"bg-warning" in upload.js`
  在配色被抽走之后**恒假** —— 会把一个**已正确分发**的修复报成"缺标记"。
* 原来的辅助正则 `if \\(([^)]+)\\)\\s*return "(bg-[a-z-]+)"` 在本项目的真实写法
  `if (["review","done"].includes(st)) return "bg-success"` 上 **0 命中**
  （`[^)]+` 在 `.includes(st)` 的 `)` 处就截断）⇒ 该半条判据**恒真**。

两半分别退化成"恒假"与"恒真"，合起来零判别力（`PROJECT_PITFALLS.md` §二十六
空断言 / §二十八 文本在 ≠ 运行期可达）。行为判据同时免疫这两类失效：
函数重命名、分支重排、把映射搬去别的文件都不会让它失效。

判据本身用 node **真的跑一遍**产物里的那份字节，取回映射再断言语义关系：
    review == done            （同一语义不得两种颜色）
    partial_review != review  （否则"有页失败"会被读成"记录无异常"）
    error          != review
    partial_review != error   （用户须能区分"部分可复核"与"失败"）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

#: 断言涉及的档位。加档位时在此处补，勿在断言里散落字面量。
STATES = ("review", "done", "partial_review", "error", "cancelled", "ocr_running")


def status_dot_classes(js_text: str, states=STATES) -> dict[str, str]:
    """在 node 里运行 `status.js`，返回 `状态 -> 状态点颜色类`。

    node 不可用时抛 :class:`AssertionError`（fail-closed：**判不了 ≠ 通过**）。
    """
    td = tempfile.mkdtemp(prefix="pbc-e2e-status-")
    try:
        p = os.path.join(td, "status.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(js_text)
        script = (
            "require(process.argv[1]);"
            "const P=globalThis.PbcStatus;"
            "if(!P||typeof P.statusDotClass!=='function')"
            "{console.error('PbcStatus.statusDotClass 缺失');process.exit(3);}"
            "const o={};for(const s of JSON.parse(process.argv[2]))o[s]=P.statusDotClass(s);"
            "process.stdout.write(JSON.stringify(o));"
        )
        r = subprocess.run(
            ["node", "-e", script, p, json.dumps(list(states))],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            raise AssertionError(
                f"node 执行 status.js 失败 rc={r.returncode}: {r.stderr.strip()[:200]}")
        return json.loads(r.stdout)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def dot_semantics_problems(mapping: dict[str, str]) -> list[str]:
    """返回违规说明列表（空 = 通过）。判据见模块 docstring。"""
    problems: list[str] = []
    if mapping.get("review") != mapping.get("done"):
        problems.append(
            f"review/done 档位颜色不一致（同一语义两种颜色）: {mapping}")
    for st in ("partial_review", "error"):
        if st not in mapping:
            problems.append(f"status.js 未覆盖 {st}")
    if problems:
        return problems
    if mapping["partial_review"] == mapping["review"]:
        problems.append(
            "#127 回归：partial_review 与成功态同色"
            f"（{mapping['partial_review']}）—— 会被误读成"
            "'记录无异常'，GMP 下最危险的假阴性")
    if mapping["error"] == mapping["review"]:
        problems.append(
            f"#127 回归：error 与成功态同色（{mapping['error']}）")
    if mapping["partial_review"] == mapping["error"]:
        problems.append(
            "partial_review 与 error 同色 —— 用户无法区分'部分可复核'与'失败'")
    return problems
