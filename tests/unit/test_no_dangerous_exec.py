"""应用代码**不得**引入命令执行面（shell / eval / exec）—— 静态 AST 护栏。

## 为什么值得一条护栏

2026-10-10 对抗性审查实测：应用包内 `shell=True` / `os.system` / `os.popen` /
裸 `eval` / `exec` **一处都没有**（见 `docs/PROJECT_PITFALLS.md` §六十五）。
"现在没有"不等于"以后不会有" —— 这类面一旦引入就是**远程命令执行**级别，且
review 时很容易被"只是内部调用"放过。故用一条**可静态判定**的护栏把它钉住。

## 范围（**刻意**只扫应用包）

`scripts/**` **不在**范围内：构建/门禁工具**合法地**用 `subprocess` 调 git / pytest /
PyInstaller（那些是开发机上的工具，不随包分发、不处理用户输入）。

## 边界（如实声明）

本护栏只拦**语法上可见**的调用。它**不能**证明"没有命令注入" —— 例如
`getattr(os, "system")(...)` 或经第三方库的间接执行都绕得过去。
它是**回归网**，不是安全证明。
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: 应用包（随包分发、处理用户输入的那部分）
_APP_DIRS = ("api", "core", "llm", "db", "models")
_APP_FILES = ("main.py", "server.py", "config.py", "logging_config.py")

#: 被禁止的**调用名**（`os.system` / `os.popen` / 裸 `eval` / `exec`）
_BANNED_CALLS = {"os.system", "os.popen", "eval", "exec"}

#: 这些 subprocess 入口**只有**在带 `shell=True` 时才禁（不带 shell 的是安全的）
_SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}


def _app_files() -> list[Path]:
    out: list[Path] = []
    for d in _APP_DIRS:
        out.extend(sorted(p for p in (_ROOT / d).rglob("*.py")
                          if "__pycache__" not in p.parts))
    out.extend(_ROOT / f for f in _APP_FILES if (_ROOT / f).is_file())
    return out


def _dotted(node: ast.AST) -> str:
    """把 `os.system` 这类 Attribute 链还原成点号串（不完整则返回 ""）。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def scan_source(src: str, label: str) -> list[str]:
    """返回该源码里的违规点（`label:line:kind`）；空 = 干净。"""
    tree = ast.parse(src, filename=label)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else "")
        if name in _BANNED_CALLS:
            hits.append(f"{label}:{node.lineno}:{name}")
            continue
        if name.rsplit(".", 1)[-1] in _SUBPROCESS_FUNCS:
            for kw in node.keywords:
                if kw.arg == "shell" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False):
                    hits.append(f"{label}:{node.lineno}:{name}(shell=...)")
    return hits


def test_app_code_has_no_shell_or_eval_surface():
    files = _app_files()
    # **反空转**（PITFALLS §三十七 / Trap 87）：扫描塌缩成 0 个文件时必须**红**，
    # 否则"什么都没扫到"会被读成"很干净"。
    assert len(files) >= 30, f"扫描面塌缩：只找到 {len(files)} 个应用源文件"

    hits: list[str] = []
    for p in files:
        # ⚠️ 必须 `utf-8-sig`：本仓有 3 个应用源带 BOM（`api/jobs/actions.py`、
        # `api/settings/probe.py`、`api/settings/provider.py`）。Python 的 tokenizer
        # 容忍 BOM，但 `ast.parse` 对**保留在字符串里的** U+FEFF 会报 SyntaxError。
        hits.extend(scan_source(p.read_text(encoding="utf-8-sig"),
                                p.relative_to(_ROOT).as_posix()))
    assert not hits, (
        "应用代码出现命令执行面（shell/eval/exec）：\n  " + "\n  ".join(hits)
        + "\n若确有正当用途，请在此处登记例外并说明为何不可注入。")
