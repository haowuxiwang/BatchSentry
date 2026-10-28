"""整栏全空 = 抽取不可判，不是"记录缺失"（R106 / P1-3）。

事故（实测 `e50e9a7e` p3）：「关键设备清单」的**确认**栏，`raw_html` 里每一行都是
空 `<td></td>`（13 个空单元格），LLM 据此发了
「关键设备清单中所有设备的'确认'栏均为空白，缺少确认签名或标记」。
用户看原页：**那一列是打了钩的** —— 是 OCR 没取到手写勾选。

为什么不能当"记录缺失"
----------------------
一整列**所有行**都没有内容，是**抽取失败**的典型形态：人手填一列，极少一行都不填。
但从文本上无法区分"真没填"与"没识别" —— 唯一诚实的结论是**不可判**，
请复核员看原页。这与 CR-4（`signature_unreadable`）是同一条原则：
**证据不可靠时不许下缺陷结论**。

本模块只做两件事
----------------
1. `blank_columns(raw_html)`：找出"表头有名字、数据行全空"的列（含行数）。
2. `retype_blank_column_claims(findings, raw_by_page)`：把"整栏空白"类断言
   改判为 `ocr_noise`（技术噪音，不是 GMP 缺陷），并换成不可判文案。

⚠️ 表头识别是**保守**的：宁可漏判（不改），不可错改。任何拿不准的表
（表头里有数字/日期、列宽对不齐、表格本身大半是空的）一律跳过。
"""
from __future__ import annotations

import re

_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S)
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
_CELL_RE = re.compile(r"<(td|th)\b([^>]*)>(.*?)</\1>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SPAN_RE = re.compile(r"(rowspan|colspan)\s*=\s*[\"']?(\d+)", re.I)
_PURE_DIGIT_RE = re.compile(r"[\d./:\-年月日\s]+")

#: 表头单元格的最大长度 —— 超过就不是表头，是正文/标题。
_HEADER_MAX = 10
#: 数据行下限：一行"全空"可能只是没填，两行起才像抽取失败。
_MIN_ROWS = 2

#: 「系统性」措辞：把"某一格空"和"整栏空"区分开。
_SYSTEMATIC_RE = re.compile(r"所有|全部|均|整栏|整列|每一|每个|各")
#: 「空」的措辞。`缺少` 单列不算 —— 它常与具体格绑定（"缺少签名"）。
_BLANK_RE = re.compile(r"空白|为空|未填|未勾选|未标记|未确认|全空")
_SENTENCE_RE = re.compile(r"[^。；;\n]+")


def _cell_text(raw: str) -> str:
    return _TAG_RE.sub("", raw).strip()


def _grid(table_html: str) -> list[list[str]]:
    """把一张 HTML 表展开成规整二维网格（处理 rowspan / colspan）。

    不展开就没法谈"第 i 列" —— 实测的「关键设备清单」第一列就有 `rowspan="2"`，
    后续行因此少一个 `<td>`，按"列数相等"筛选会把整张表筛掉。
    """
    rows: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}  # (row, col) -> 文本
    for r, tr in enumerate(_ROW_RE.findall(table_html)):
        cells = _CELL_RE.findall(tr)
        if not cells:
            continue
        grid: list[str] = []
        col = 0
        ci = 0
        while ci < len(cells) or (r, col) in pending:
            if (r, col) in pending:
                grid.append(pending.pop((r, col)))
                col += 1
                continue
            _, attrs, content = cells[ci]
            ci += 1
            text = _cell_text(content)
            span = dict((k.lower(), int(v)) for k, v in _SPAN_RE.findall(attrs))
            rs = max(1, span.get("rowspan", 1))
            cs = max(1, span.get("colspan", 1))
            for dc in range(cs):
                grid.append(text)
                for dr in range(1, rs):
                    pending[(r + dr, col + dc)] = text
            col += cs
        # 补齐行尾仍然 pending 的单元格
        while (r, col) in pending:
            grid.append(pending.pop((r, col)))
            col += 1
        rows.append(grid)
    return rows


def _looks_like_header(cells: list[str]) -> bool:
    if len(cells) < 2:
        return False
    for c in cells:
        if not c or len(c) > _HEADER_MAX or _PURE_DIGIT_RE.fullmatch(c):
            return False
    return True


def blank_columns(raw_html: str | None) -> dict[str, int]:
    """``{表头名: 数据行数}`` —— 整列为空的列。

    保守判据（全部满足才算）：
    1. 找到一行像表头的行（都非空、≤10 字、不含纯数字/日期）；
    2. 其后至少 2 行同宽数据行；
    3. 表格本身有内容：至少一半的列在多数行里有值（否则是"空表"，不是"某列空"）；
    4. 该列所有数据行都为空。
    """
    out: dict[str, int] = {}
    for table in _TABLE_RE.findall(str(raw_html or "")):
        rows = _grid(table)
        for hi, head in enumerate(rows):
            if not _looks_like_header(head):
                continue
            width = len(head)
            body = [r for r in rows[hi + 1:] if len(r) == width]
            if len(body) < _MIN_ROWS:
                continue
            filled_cols = sum(
                1 for i in range(width)
                if sum(1 for r in body if r[i]) >= max(1, len(body) // 2)
            )
            if filled_cols < max(2, width // 2):
                continue
            for i, name in enumerate(head):
                if all(not r[i] for r in body):
                    # 同一表头在多张表里出现时取最大行数（信息量更大）
                    out[name] = max(out.get(name, 0), len(body))
    return out


def claimed_blank_column(
    description: str | None, columns: dict[str, int],
) -> str | None:
    """文案是否在断言"某列整列都空"；返回命中的列名。

    判据：**同一句**里同时出现系统性措辞（所有/均/整栏…）、空措辞（空白/未勾选…）
    与列名。分句判定很关键 —— 实测文案常把"某格缺失"与"整栏空"写在一段里，
    跨句匹配会把单格问题误改成不可判。
    """
    text = str(description or "")
    if not text or not columns:
        return None
    for sentence in _SENTENCE_RE.findall(text):
        if not (_SYSTEMATIC_RE.search(sentence) and _BLANK_RE.search(sentence)):
            continue
        for name in columns:
            if name and name in sentence:
                return name
    return None


def unverifiable_text(page, column: str, rows: int) -> str:
    return (
        f"第{page}页「{column}」栏整列未识别到任何内容（{rows} 行全空），"
        f"无法判断是未填写还是 OCR 未识别，请核对原页。"
    )


def retype_blank_column_claims(
    findings: list, raw_by_page: dict | None,
) -> tuple[list, list[dict]]:
    """把"整栏空白"类断言改判为 `ocr_noise`（不可判）。

    Returns ``(out, changes)``；`changes` 供调用方留痕/审计。
    **不改入参 dict**（返回新副本）—— 与 `apply_review` 的既有约定一致。
    """
    raw_by_page = raw_by_page or {}
    out: list = []
    changes: list[dict] = []
    cache: dict = {}
    for f in findings:
        if not isinstance(f, dict):
            out.append(f)
            continue
        page = f.get("page")
        if page not in cache:
            cache[page] = blank_columns(raw_by_page.get(page))
        columns = cache[page]
        if not columns:
            out.append(f)
            continue
        column = claimed_blank_column(f.get("description"), columns)
        if not column:
            out.append(f)
            continue
        nf = dict(f)
        nf["type"] = "ocr_noise"
        nf["severity"] = "info"
        nf["description"] = unverifiable_text(page, column, columns[column])
        nf["guard_reason"] = (
            f"原判为记录缺失（{str(f.get('description') or '')[:80]}）；"
            f"「{column}」栏在原始识别结果中整列为空 ⇒ 无法区分未填写与未识别，"
            f"改判为识别问题（R106/P1-3）"
        )
        changes.append({
            "page": page, "column": column, "rows": columns[column],
            "from_type": f.get("type"), "from_severity": f.get("severity"),
            "original": str(f.get("description") or "")[:120],
        })
        out.append(nf)
    return out, changes
