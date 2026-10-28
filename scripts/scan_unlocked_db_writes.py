# -*- coding: utf-8 -*-
"""
扫描：应用源里「裸 DB 写」（未包在 `async with db_lock` 内的 execute/executemany/commit）。

动机
----
项目不变式（.workbuddy-ai/memory/MEMORY.md §6）：
    「后端所有 DB 写必须走 db_lock」
理由（多处注释自陈）：aiosqlite 是单连接，事务边界会被并发协程穿插 ——
一个写可能被另一协程的 commit 提前提交、或混进别人的事务。

⚠️ 已核：全仓 3 处 `db.rollback()` **都在 `db_lock` 内**
（`stage2.py:96-118` / `upload.py:460` / `actions.py:264`）⇒
「A 的写被 B 的 rollback 回滚」这条最坏路径**不成立**。
本脚本因此只报告**结构偏差**，不主张已知的运行期故障。

本脚本只做**语法层**判定，不证明运行时无并发问题；它回答的是
「哪些写语句在结构上**没有**被锁保护」。

用法：
    python scripts/scan_unlocked_db_writes.py [--json | --baseline]

    --json      逐条列出命中（证据用）
    --baseline  输出 `tests/unit/db_write_lock_baseline.json` 的内容（到 stdout）
    --write-baseline  直接写基线文件（二进制 + LF；**别**用 `>` 重定向，Windows 会变 CRLF）

⚠️ 放在 `scripts/`（受版本控制）而**不是** `devlogs/`（被 `.gitignore:163` 吃掉）
—— 否则证据会随磁盘消失，结论无法从一份 clone 复现（同 R81 的处置理由）。

判据（刻意保守，宁可少报）：
  · 只看 `await db.<method>(...)` / `await conn.<method>(...)` 形态；
  · method ∈ {execute, executemany, commit, executescript}；
  · **只判写**：SQL 首关键字 ∈ {INSERT, UPDATE, DELETE, REPLACE, CREATE, DROP,
    ALTER, VACUUM, PRAGMA}。SELECT 是读，不需要锁（项目不变式只约束写事务边界）；
  · `commit` 一律算写（它闭合事务边界）；
  · SQL 无法静态取到（f-string / 变量）⇒ 归入 UNKNOWN 单列，不混入写计数；
  · 若该语句**词法上**位于某个 `async with <name>:` 且 name 以 `db_lock` 结尾
    的 With 体内 ⇒ 视为已保护；
  · 其余一律报告（含函数内条件分支里的）。
  · `_audit_log` / `transition_status` 等**内部**取锁的封装不在此列 ——
    它们不是裸 db 调用，不会命中。
"""
from __future__ import annotations

import ast
import json
import os
import sys

def _find_root(start: str) -> str:
    """向上找到同时含 api/ 与 core/ 的目录 —— 与脚本自身位置解耦。"""
    cur = start
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "api")) and \
                os.path.isdir(os.path.join(cur, "core")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise SystemExit("找不到仓库根（需含 api/ 与 core/）：%s" % start)


ROOT = _find_root(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("api", "core", "db")
# 基线落点：**唯一**定义。脚本与 tests/unit/test_db_write_lock_discipline.py 都读它，
# 避免"脚本写 A、护栏读 B"这种谁也发现不了的错位。
BASELINE_PATH = os.path.join(ROOT, "tests", "unit", "db_write_lock_baseline.json")
METHODS = {"execute", "executemany", "commit", "executescript"}
WRITE_KW = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP",
            "ALTER", "VACUUM", "PRAGMA", "BEGIN", "SAVEPOINT"}


def _sql_text(call: ast.Call) -> str | None:
    """取静态可得的 SQL 文本；取不到返回 None（f-string 只取常量片段拼接）。"""
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    if isinstance(a, ast.JoinedStr):
        # f-string：静态部分若以写关键字开头仍可判定
        parts = [v.value for v in a.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if parts:
            return "".join(parts)
    return None


def _sql_head(call: ast.Call, limit: int = 48) -> str:
    """SQL 的**归一化前缀**，用作稳定标识（行号会随无关编辑漂移）。

    归一化：折叠所有空白为单空格、截断到 ``limit``。`commit` 无 SQL ⇒ ``""``。
    """
    text = _sql_text(call)
    if text is None:
        return ""
    return " ".join(text.split())[:limit]


def _sql_kind(call: ast.Call) -> str:
    """'write' | 'read' | 'unknown'"""
    text = _sql_text(call)
    if text is None:
        return "unknown"
    head = text.lstrip().split(None, 1)
    if not head:
        return "unknown"
    kw = head[0].upper().strip("(")
    if kw in WRITE_KW:
        return "write"
    if kw in ("SELECT", "WITH", "EXPLAIN"):
        return "read"
    return "unknown"


def _is_db_lock_with(node: ast.AsyncWith | ast.With) -> bool:
    for item in node.items:
        ctx = item.context_expr
        name = ""
        if isinstance(ctx, ast.Name):
            name = ctx.id
        elif isinstance(ctx, ast.Attribute):
            name = ctx.attr
        if name.endswith("db_lock"):
            return True
    return False


class Scanner(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.depth = 0          # 当前是否处于 db_lock 内（计数，支持嵌套）
        self.hits: list[dict] = []
        self.func: list[str] = []

    # --- 锁作用域 -------------------------------------------------
    def visit_AsyncWith(self, node: ast.AsyncWith):
        locked = _is_db_lock_with(node)
        self.depth += 1 if locked else 0
        for child in node.body:
            self.visit(child)
        self.depth -= 1 if locked else 0

    def visit_With(self, node: ast.With):
        locked = _is_db_lock_with(node)
        self.depth += 1 if locked else 0
        for child in node.body:
            self.visit(child)
        self.depth -= 1 if locked else 0

    # --- 函数上下文（只用于报告可读性） ---------------------------
    def _visit_func(self, node):
        # ⚠️ 进入新函数 ⇒ 锁作用域**必须重置为 0**。
        # 嵌套 `async def g()` 写在 `async with db_lock:` 体内，只是"**定义**在那里"；
        # 它被**调用**时外层 with 早已退出 ⇒ 不能继承外层的 depth。
        # 不重置会**少报**（把 g 里的裸写当成已保护）—— 这是本扫描器唯一的
        # 静默漏报方向，护栏 test_nested_def_does_not_inherit_the_lock 盯着它。
        saved = self.depth
        self.depth = 0
        self.func.append(node.name)
        self.generic_visit(node)
        self.func.pop()
        self.depth = saved

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node)

    # --- 目标：await db.execute(...) ------------------------------
    def visit_Await(self, node):
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            attr = call.func.attr
            recv = call.func.value
            recv_name = ""
            if isinstance(recv, ast.Name):
                recv_name = recv.id
            elif isinstance(recv, ast.Attribute):
                recv_name = recv.attr
            if attr in METHODS and recv_name in ("db", "conn", "cursor"):
                if self.depth == 0:
                    if attr == "commit":
                        kind = "write"
                    else:
                        kind = _sql_kind(call)
                    self.hits.append({
                        "file": os.path.relpath(self.path, ROOT).replace("\\", "/"),
                        "line": node.lineno,
                        "method": attr,
                        "kind": kind,
                        "func": self.func[-1] if self.func else "<module>",
                        # 稳定标识用（护栏按它比对，不按行号）
                        "sql": _sql_head(call),
                    })
        self.generic_visit(node)


def collect() -> dict:
    """跑一遍扫描，返回结构化结果。

    **唯一实现**：`main()`（命令行/证据）与
    `tests/unit/test_db_write_lock_discipline.py`（护栏）都调它 ——
    护栏若自己再写一遍 AST 遍历，两份判据必然漂移（一份改了另一份没改，
    护栏恒绿而脚本仍报 121）。

    Returns:
        ``{"files": int, "writes": [...], "unknown": [...], "reads": int,
        "parse_errors": [str, ...]}``，每条 hit 形如
        ``{"file", "line", "method", "kind", "func", "sql"}``。
        ``sql`` 是归一化后的 SQL 前缀（`commit` 为空串），供护栏做**行号无关**的比对。
        ``parse_errors`` 非空 ⇒ 有文件**没进统计**，计数偏低（护栏会据此判红）。
    """
    hits: list[dict] = []
    files = 0
    parse_errors: list[str] = []
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                files += 1
                with open(p, "rb") as fh:
                    raw = fh.read()
                # 仓库有 3 个应用源带 BOM（R89 实测）⇒ 必须 utf-8-sig
                src = raw.decode("utf-8-sig")
                try:
                    tree = ast.parse(src, filename=p)
                except SyntaxError as e:
                    print("PARSE FAIL %s: %s" % (p, e), file=sys.stderr)
                    # 必须回传：解析失败的文件**静默退出统计**，
                    # 护栏会因此少看一个文件的裸写 ⇒ 假绿。
                    parse_errors.append("%s: %s" % (os.path.relpath(p, ROOT), e))
                    continue
                s = Scanner(p)
                s.visit(tree)
                hits.extend(s.hits)

    hits.sort(key=lambda h: (h["file"], h["line"]))
    return {
        "files": files,
        "writes": [h for h in hits if h["kind"] == "write"],
        "unknown": [h for h in hits if h["kind"] == "unknown"],
        "reads": len([h for h in hits if h["kind"] == "read"]),
        "parse_errors": parse_errors,
    }


def key_of(hit: dict) -> str:
    """行号无关的稳定标识：``文件|函数|方法|SQL前缀``。

    为什么不带行号：F1/F4 之类的改动会让整文件行号漂移，带行号的基线
    **每次无关编辑都要重生成**，最终没人看 diff，基线退化成橡皮图章。
    """
    return "%s|%s|%s|%s" % (hit["file"], hit["func"], hit["method"], hit["sql"])


def baseline_from(res: dict) -> dict:
    """把扫描结果压成「基线快照」：``key -> 出现次数``（排序，便于 diff）。

    同一函数里可能有**同形态的多处**裸写（实测 12 个键有重复，最多 5 处），
    所以值必须是计数，否则少一处也发现不了。
    """

    def _count(group: list[dict]) -> dict:
        out: dict[str, int] = {}
        for h in group:
            k = key_of(h)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))

    return {
        "_note": "已知债务快照，**不是**「已审计安全」清单；"
                 "由 `python scripts/scan_unlocked_db_writes.py --baseline` 生成。",
        "writes": _count(res["writes"]),
        "unknown": _count(res["unknown"]),
    }


def main() -> int:
    res = collect()
    files = res["files"]
    writes = res["writes"]
    unknown = res["unknown"]
    if "--baseline" in sys.argv:
        # 供 tests/unit/db_write_lock_baseline.json 重生成；格式由 baseline_from 单点决定，
        # 护栏侧调同一个函数 —— 两边格式不可能漂移。
        print(json.dumps(baseline_from(res), ensure_ascii=False, indent=2))
        return 0
    if "--write-baseline" in sys.argv:
        # 直接写盘（**二进制 + LF**）。刻意不支持 `> 重定向`：
        # Windows 上 Python 的 stdout 是文本模式，重定向会把 \n 翻成 \r\n，
        # 于是基线文件与仓库其余 LF 文件不一致，每次重生成都是一次全文件 diff。
        data = json.dumps(baseline_from(res), ensure_ascii=False, indent=2) + "\n"
        raw = data.encode("utf-8").replace(b"\r\n", b"\n")
        with open(BASELINE_PATH, "wb") as fh:
            fh.write(raw)
        print("wrote %s (%d bytes, LF)" % (
            os.path.relpath(BASELINE_PATH, ROOT), len(raw)))
        return 0
    if "--json" in sys.argv:
        print(json.dumps({
            "files": files,
            "unlocked_writes": writes,
            "unlocked_unknown": unknown,
            "unlocked_reads": res["reads"],
        }, ensure_ascii=False, indent=2))
        return 0

    print("scanned %d .py files under %s" % (files, "/".join(SCAN_DIRS)))
    if res["parse_errors"]:
        print("PARSE FAILURES         : %d   <- 这些文件**没进统计**，计数会偏低"
              % len(res["parse_errors"]))
        for e in res["parse_errors"]:
            print("    " + e)
    print("unlocked raw DB writes : %d   <- 违反「所有 DB 写走 db_lock」" % len(writes))
    print("unlocked unknown-SQL   : %d   <- 静态取不到首关键字，需人工判" % len(unknown))
    print("unlocked reads (SELECT): %d   <- 读，不需锁，仅计数" % res["reads"])

    for label, group in (("WRITE", writes), ("UNKNOWN", unknown)):
        if not group:
            continue
        print("\n=== %s ===" % label)
        cur = None
        for h in group:
            if h["file"] != cur:
                cur = h["file"]
                print("\n%s" % cur)
            print("  L%-5d %-14s %s()" % (h["line"], h["method"], h["func"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
