# -*- coding: utf-8 -*-
"""报告查看页的轻量 Markdown → HTML 渲染器（round-61）。

为什么手写而不用 markdown 库
---------------------------
便携包是离线分发的（PyInstaller 冻结 + Electron），为渲染**一个页面**
引入第三方依赖（进打包链、进供应链审计面）不成比例。本渲染器的输入
**只有** ``api/report.py::_generate_markdown`` 的输出 —— 一个已知封闭的
md 子集：

- 标题 ``#`` / ``##`` / ``###``
- 无序列表（一级 ``- ``，二级 ``  - ``，无更深嵌套）
- 行内 ``**粗体**`` 与 ``\`行内代码\``
- 水平线 ``---``
- 普通段落（含 emoji 字符如 🔴 ⏳）

安全模型
--------
``_generate_markdown`` 的 esc() 已把 HTML（<>&）与 Markdown 图像/链接
元字符转为 HTML 实体（对抗审查 cr-7 / P1-B，防 Typora/Obsidian 的 XSS
与外链追踪）。本渲染器**信任该前置转义**，只识别结构性符号（#、-、`、
**、---）并包上标签——实体原样透传即安全。渲染器自身**不生成**任何
属性或 URL，因此不存在二次注入面。

超出子集的行（表格、引用块等）按普通段落渲染 —— 渲染器永不抛错，
最坏形态是"显示为纯文本段落"（与 .md 原样打开一致，不劣化）。
"""
from __future__ import annotations

import re

# 行内元素：先转义无关（输入已转义），只替换 **bold** 与 `code`。
# 非贪婪匹配；`**` 对内部不含换行（按行处理，天然满足）。
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)-\s+(.*)$")


def _inline(text: str) -> str:
    """行内元素渲染：**粗体** → <strong>，`代码` → <code>。"""
    out = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    return out


def md_to_html(md: str) -> str:
    """把报告 Markdown 渲染为 HTML 片段（无 <html> 外壳）。

    结构规则（与生成器输出对齐，逐行状态机）：
    - ``#``~``######``  → h1~h6
    - ``---``（单独成行）→ <hr>
    - ``- x``          → <ul><li>（连续行聚合为一个列表）
    - ``  - x``        → 嵌套 <ul>（仅一层嵌套，更深按一层渲染）
    - 其余非空行       → <p>（连续行不合并 —— 生成器每段之间有空行，
                          且逐行成段与视觉分段一致）
    - 空行             → 分段边界（不输出）
    """
    if not md:
        return ""
    parts: list[str] = []
    in_ul = False   # 处于 <ul> 内
    in_sub = False  # 处于嵌套 <ul> 内

    def _close_lists() -> None:
        nonlocal in_ul, in_sub
        if in_sub:
            parts.append("</ul></li>")
            in_sub = False
        if in_ul:
            parts.append("</ul>")
            in_ul = False

    for raw_line in md.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            _close_lists()
            continue
        m = _HEADING_RE.match(line)
        if m:
            _close_lists()
            level = min(len(m.group(1)), 6)
            parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if line.strip() == "---":
            _close_lists()
            parts.append("<hr>")
            continue
        m = _LIST_RE.match(line)
        if m:
            indent, content = m.group(1), m.group(2)
            if not indent:  # 一级列表项
                if in_sub:  # 子列表结束回到一级
                    parts.append("</ul></li>")
                    in_sub = False
                if not in_ul:
                    parts.append("<ul>")
                    in_ul = True
                parts.append(f"<li>{_inline(content)}")
                # li 的闭合延后：下一项/嵌套/列表结束时处理
                parts.append("</li>")
            else:  # 嵌套项（归属上一个未闭合的 li —— 简化为独立嵌套 ul，
                   # 生成器的嵌套项总是紧跟所属父项，视觉分组一致）
                if not in_sub:
                    if not in_ul:
                        parts.append("<ul>")
                        in_ul = True
                    parts.append("<li><ul>")
                    in_sub = True
                parts.append(f"<li>{_inline(content)}</li>")
            continue
        # 普通段落
        _close_lists()
        parts.append(f"<p>{_inline(line)}</p>")

    _close_lists()
    return "".join(parts)
