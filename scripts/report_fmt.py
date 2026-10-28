"""诊断报文的**共享**清单格式化 —— **不得低报**（`docs/PROJECT_PITFALLS.md` §六十四）。

## 为什么单独一个模块

同一个缺陷在**多份** gate/诊断脚本里各写了一遍：`f"{len(x)} 个…：{x[:K]}"` —— `len()` 是
真值、`[:K]` 只显示 K 个 ⇒ **数量与清单互相矛盾**，而读者会信更具体的那个（清单）。
实测（2026-10-10）：`artifact_freshness` 报「**8 个**源文件与清单不符」却只列 **5** 个。

`scripts/` **不是包** ⇒ 各脚本按**文件位置**互导（`sys.path` 插入 `scripts/`，同
`release_gate` 的既有做法）。本模块**只做呈现**、不含任何判据 —— 判据的单一实现约束见各
gate 的 docstring。

**先例**：`check_leaked_keys.py` 早已用 `等 {len(paths)} 个 blob` 声明余量 —— 本模块把
这个既有好做法固化成唯一实现。
"""
from __future__ import annotations

#: 清单显示的默认上限。**超过它就必须显式声明还有多少未显示**（低报 = 缺陷，不是简洁）。
DEFAULT_LIST_CAP = 20


def fmt_list(items, cap: int = DEFAULT_LIST_CAP, sep: str | None = None) -> str:
    """把清单渲染成一行，**永不静默丢项**。

    * ``len(items) <= cap`` ⇒ 全列（无标记）；
    * ``> cap`` ⇒ 列前 ``cap`` 个，**并明说**「另有 M 个未显示，共 K 个」；
    * ``sep=None`` ⇒ Python 列表字面量（``str(head)``）；否则按 ``sep`` 连接。

    读者据此既拿到**可行动**的条目，也知道清单是**部分**的。
    """
    items = list(items)
    head = items[:cap]
    body = str(head) if sep is None else sep.join(str(i) for i in head)
    if len(items) <= cap:
        return body
    return f"{body} …（另有 {len(items) - cap} 个未显示，共 {len(items)} 个）"
