"""`innerHTML` 转义纪律机检（R79 第十九批，对抗性审查 → TODO 0-19）。

## 为什么需要

`docs/RELEASE_READINESS_PLAN.md` §SEC-6 曾写「`|safe` / `Markup(` / `innerHTML` 零命中」，
**与实测相反** —— 前端 R63/R65 拆成模块后 `innerHTML` 已达 **46 处**（38 个赋值点）。
它们**逐个依赖 `esc()` 纪律**，但**没有任何机检**：新加一处
`el.innerHTML = untrusted` 不会被任何东西拦下。

本文件就是那个机检。判据（三层，逐层放宽、每层都**显式**）：

  A. **纯字面量**：RHS 是空串 / 字符串字面量 / 它们的 `+` 连接 ⇒ 安全（无插值）。
  B. **含已知转义器**：RHS（语句跨度内）出现 `esc(` / `escapeHtml(` ⇒ 安全。
  C. **受审计渲染器**：RHS 是**裸调用**且函数名在 `_AUDITED_RENDERERS` 里
     （这些渲染器只返回字面量模板）。护栏**另断言**这些名字在 `static/` 里确有定义，
     防"白名单指向一个不存在的函数"。
  D. 以上都不满足 ⇒ 必须在 `_AUDITED` 显式登记（file + 语句唯一子串 + 理由），
     否则 **FAIL**。

## 为什么不是"每个 `${...}` 都必须含 esc"

因为那会**误伤**：本仓多处插值是**数字**（页码/总数）、**字面量三元**
（`atFirst ? "disabled" : ""`）或**已在上一行转义**的变量（`review-findings.js`
的 `detail`，且 `esc` 缺席时 fail-closed 为空串）。把这类判红，护栏就会被关掉。
D 层用**显式登记 + 陈旧检测**替代：既拦住"新加的未审计站点"，又不会假装能静态证明安全。

## 扫描为什么要 tokenizer

- 注释里的 `.innerHTML = x` 不是站点（朴素正则会被注释骗）；
- 模板字面量 `` `...${esc(x)}...` `` 里的 `${}` 是**代码**，`esc(` 必须能被看见
  （把整段反引号当字符串 ⇒ 漏掉转义器 ⇒ 误伤）。故 `_code_mask` 会跟踪
  `` ` ``、`'`、`"`、`//`、`/* */` 与模板内 `${}` 的嵌套。

跑法：`pytest tests/unit/test_innerhtml_escaping.py`
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "static"

_ASSIGN = re.compile(r"\.innerHTML\s*\+?=")
_ESCAPER = re.compile(r"(?<![\w$.])(?:esc|escapeHtml)\(")
_PURE_LITERAL = re.compile(
    r"""^\s*(?:"[^"]*"|'[^']*'|`[^`$]*`)"""
    r"""(?:\s*\+\s*(?:"[^"]*"|'[^']*'|`[^`$]*`))*\s*$""",
    re.S,
)
_RENDERER_CALL = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*\((?:[^()]|\([^()]*\))*\)\s*$", re.S)

#: 受审计的渲染器：只返回**字面量模板**（无不可信插值）。护栏断言它们确有定义。
_AUDITED_RENDERERS: dict[str, str] = {
    "emptyFindingsNote": "review-findings.js：三个分支只返回字面量模板（flags 为布尔）",
    "statusBadge": "settings-state.js：ok 为布尔，两个分支都是字面量",
}

#: 未自动通过者必须在此登记。(文件, 语句唯一子串, 理由)。子串必须**恰好**命中 1 处。
_AUDITED: tuple[tuple[str, str, str], ...] = (
    ("review-findings.js", "= html;",
     "html = rows.map(overviewRow).join(\"\")；overviewRow 对所有不可信字段 esc()，"
     "page 先 Number() 归一（属性里不放未净化值）"),
    ("review-findings.js", "= list.innerHTML + html;",
     "同上：增量追加同一份已转义的 html"),
    ("review-findings.js", "跨页总览加载失败（",
     "detail 在上一行经 esc() 转义；esc 缺席时 fail-closed 为空串"),
    ("upload-jobs.js", "flex items-center justify-between px-5 py-3 border-t",
     "分页模板：全部插值为数字（页码/总数）或字面量三元，无不可信文本"),
)

#: 站点数下限（当前 38）。降到这个数以下 ⇒ 扫描器坏了（不是"没有风险"）。
_MIN_SITES = 30


def _code_mask(src: str) -> list[bool]:
    """``mask[i] is True`` ⇔ 位置 i 处于**可执行代码**中。

    字符串体 / 注释体 ⇒ False；模板字面量 ``${}`` **内部**的表达式 ⇒ True。
    """
    mask = [False] * len(src)
    n = len(src)
    i = 0
    mode = "code"
    depth = 0
    saved: list[tuple[str, int]] = []
    while i < n:
        c = src[i]
        if mode == "line_comment":
            if c == "\n":
                mode = "code"
            i += 1
            continue
        if mode == "block_comment":
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                mode = "code"
                i += 2
                continue
            i += 1
            continue
        if mode in ("sq", "dq"):
            if c == "\\":
                i += 2
                continue
            if (mode == "sq" and c == "'") or (mode == "dq" and c == '"'):
                mode = "code"
            i += 1
            continue
        if mode == "template":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                mode, depth = saved.pop()
                i += 1
                continue
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                saved.append(("template", depth))
                mode = "code"
                depth = 0
                i += 2
                continue
            i += 1
            continue
        # mode == "code"
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            mode = "line_comment"
            i += 2
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            mode = "block_comment"
            i += 2
            continue
        if c == '"':
            mode = "dq"
            i += 1
            continue
        if c == "'":
            mode = "sq"
            i += 1
            continue
        if c == "`":
            saved.append(("code", depth))
            mode = "template"
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0 and saved and saved[-1][0] == "template":
                mode, depth = saved.pop()
                i += 1
                continue
            depth -= 1
        mask[i] = True
        i += 1
    return mask


def _rhs(src: str, mask: list[bool], start: int) -> tuple[str, int]:
    """从 `start` 起到 depth-0 的 `;`（或 depth<0）为止。返回 (RHS 文本, 结束下标)。"""
    depth = 0
    i = start
    n = len(src)
    while i < n:
        if mask[i]:
            c = src[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    break
            elif c == ";" and depth == 0:
                break
        i += 1
    return src[start:i], i


def _sites(sources: dict[str, str]) -> list[tuple[str, int, str, str]]:
    """返回 [(文件, 行号, RHS, 完整语句)] —— 仅**代码里**的 `.innerHTML =` 赋值。

    `RHS`（不含 `= `）供 `_classify` 用；`完整语句`（含 `.innerHTML =`）供
    `_AUDITED` 子串匹配用 —— 后者要能区分 `.innerHTML = html;` 与
    `.innerHTML = list.innerHTML + html;`。
    """
    out: list[tuple[str, int, str, str]] = []
    for name in sorted(sources):
        src = sources[name]
        mask = _code_mask(src)
        for m in _ASSIGN.finditer(src):
            if not mask[m.start()]:
                continue  # 注释/字符串里的伪站点
            rhs, end = _rhs(src, mask, m.end())
            stmt_end = end + 1 if end < len(src) and src[end] == ";" else end
            out.append((name, src[: m.start()].count("\n") + 1, rhs, src[m.start():stmt_end]))
    return out


def _classify(rhs: str) -> str | None:
    """返回自动分类结果，或 None（⇒ 必须显式登记）。"""
    if _PURE_LITERAL.match(rhs):
        return "literal"
    if _ESCAPER.search(rhs):
        return "esc"
    m = _RENDERER_CALL.match(rhs)
    if m and m.group(1) in _AUDITED_RENDERERS:
        return "renderer"
    return None


def _scan() -> list[tuple[str, int, str, str]]:
    return _sites({p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.js")})


def _unclassified() -> list[tuple[str, int, str, str]]:
    return [s for s in _scan() if _classify(s[2]) is None]


def test_every_assignment_is_classified() -> None:
    """每个 `innerHTML =` 站点要么自动通过，要么在 `_AUDITED` 里显式登记。"""
    sites = _scan()
    unclassified = []
    for name, line, rhs, stmt in sites:
        if _classify(rhs) is not None:
            continue
        matched = [a for a in _AUDITED if a[0] == name and a[1] in stmt]
        if len(matched) != 1:
            unclassified.append(f"{name}:{line}  {stmt.strip()[:80]!r}（命中登记 {len(matched)} 条）")
    assert not unclassified, (
        "以下 innerHTML 赋值**未审计**：请给 RHS 加 esc()，或在 "
        "tests/unit/test_innerhtml_escaping.py::_AUDITED 里登记并写明理由：\n  "
        + "\n  ".join(unclassified)
    )


def test_no_stale_or_ambiguous_audit_entries() -> None:
    """`_AUDITED` 每条**恰好**命中 1 个站点（陈旧/歧义都判红）。"""
    sites = _scan()
    problems = []
    for fname, needle, _reason in _AUDITED:
        hits = [s for s in sites if s[0] == fname and needle in s[3]]
        if len(hits) != 1:
            problems.append(f"{fname} :: {needle!r} 命中 {len(hits)} 处（期望 1）")
    assert not problems, "陈旧/歧义的审计登记：\n  " + "\n  ".join(problems)


def test_audited_renderers_are_defined() -> None:
    """白名单里的渲染器必须在 `static/` 里**确有定义**（防空指）。"""
    src = "\n".join(p.read_text(encoding="utf-8") for p in STATIC.glob("*.js"))
    missing = [n for n in _AUDITED_RENDERERS if not re.search(r"function\s+" + re.escape(n) + r"\s*\(", src)]
    assert not missing, f"_AUDITED_RENDERERS 指向不存在的函数：{missing}"


def test_scanner_is_not_vacuous() -> None:
    """防空转：站点数达下限，且四种分类**都**真的出现。"""
    sites = _scan()
    kinds: dict[str, int] = {}
    for _n, _l, rhs, _s in sites:
        k = _classify(rhs) or "audited"
        kinds[k] = kinds.get(k, 0) + 1
    assert len(sites) >= _MIN_SITES, f"站点数 {len(sites)} < 下限 {_MIN_SITES}（扫描器坏了？）"
    for k in ("literal", "esc", "renderer", "audited"):
        assert kinds.get(k, 0) > 0, f"分类 {k!r} 一处都没有 ⇒ 判据/扫描塌缩（kinds={kinds}）"


def test_detector_flags_an_unregistered_raw_site() -> None:
    """阴性对照：合成一个裸 `innerHTML = userText;` ⇒ 必须被扫出且**未**自动通过。"""
    src = "function f(userText) {\n  el.innerHTML = userText;\n}\n"
    sites = _sites({"fake.js": src})
    assert len(sites) == 1, sites
    assert _classify(sites[0][2]) is None, "裸变量插值竟被判安全"


def test_tokenizer_ignores_comments_and_sees_template_escapes() -> None:
    """tokenizer 的两条关键行为：注释不算站点；模板 `${}` 里的 esc 要看得见。"""
    commented = "// el.innerHTML = raw;\n/* el.innerHTML = raw; */\n"
    assert _sites({"fake.js": commented}) == [], "注释里的伪站点被当成真站点"

    tmpl = "el.innerHTML = `<p>${esc(x)}</p>`;"
    sites = _sites({"fake.js": tmpl})
    assert len(sites) == 1
    assert _classify(sites[0][2]) == "esc", "模板 ${} 内的 esc() 未被识别"
