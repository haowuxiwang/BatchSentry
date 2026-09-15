#!/usr/bin/env python
"""检索 git 历史里曾经入库的疑似密钥，并与当前 config.json 比对。

用途（`DEPLOYMENT.md`「Secret 轮换流程」第 4 步的工具化）：
  回答两个问题 ——
    ① **历史上**有哪些形如真实密钥的串进过版本控制？
    ② 其中哪些**至今仍是 config.json 里生效的值**？（= 必须立即轮换）

为什么必须查历史：删掉文件**不能**抹除 git 历史。`tests/e2e_frozen.py` 曾把真实
DeepSeek key 写死并推送（自 `81964a3` 起），删文件后该值仍可从历史取回。

安全约定：**只输出指纹**（长度 + sha256 前 12 位 + 前 6 字符），从不打印明文 ——
排查工具本身不该成为新的泄露源。

用法：
    python scripts/check_leaked_keys.py            # 需要 cwd 在仓库内
    python scripts/check_leaked_keys.py --repo DIR

退出码：0 = 历史泄漏值与当前配置无交集；2 = **有交集（需立即轮换）**；1 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

# 密钥形态 —— 与 tests/unit/test_no_committed_secrets.py 的 `_KEY_RE`、以及
# DEPLOYMENT.md 第 4 步的自查命令**同一个模式**：`sk-` 后跟 ≥32 个连续字母数字。
# 口径收窄是刻意的："连续长度"天然排除夹具（`sk-test-fake-key-…` 在 4 个字符后遇
# `-`）与词中/URL 里的 `sk-`（`risk-controlled-…`），**不需要夹具白名单** ——
# 那种白名单一旦用短子串判定就会漏报，而安全工具漏报比假阳性更糟。
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{32,}")


def fp(secret: str) -> str:
    """指纹：不泄露明文的可比对摘要。"""
    return (
        f"len={len(secret):<3} "
        f"sha256={hashlib.sha256(secret.encode()).hexdigest()[:12]} "
        f"head={secret[:6]!r}…"
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def scan_history(repo: Path) -> dict[str, set[str]]:
    """扫**所有可达 blob**（而非逐提交 diff）—— 快且不会漏掉"加进去又删掉"的串。

    返回 {疑似密钥: {出现的相对路径, ...}}。路径仅作线索，可能不完整。
    """
    rev = _git(repo, "rev-list", "--objects", "--all")
    if rev.returncode != 0:
        raise RuntimeError(f"git rev-list 失败：{rev.stderr.strip()}")

    # 第一遍：对象 id -> 路径（同一 blob 可能对应多个路径，取先出现的即可）
    oid_to_path: dict[str, str] = {}
    oids: list[str] = []
    for line in rev.stdout.splitlines():
        oid, _, path = line.partition(" ")
        if not path:  # 只有 tree/commit 行没有路径
            continue
        oids.append(oid)
        oid_to_path.setdefault(oid, path)

    found: dict[str, set[str]] = {}
    # 第二遍：批量读取 blob 内容（--batch 按需流式返回，一次进程搞定）
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout
    payload = ("\n".join(oids) + "\n").encode()
    try:
        out, _ = proc.communicate(payload, timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("git cat-file 超时（历史过大？）")

    # 解析 --batch 输出：`<oid> <type> <size>\n<content>\n`
    pos, n = 0, len(out)
    while pos < n:
        nl = out.find(b"\n", pos)
        if nl < 0:
            break
        header = out[pos:nl].decode("utf-8", "replace")
        pos = nl + 1
        parts = header.split()
        if len(parts) != 3:
            continue
        oid, obj_type, size_s = parts
        try:
            size = int(size_s)
        except ValueError:
            continue
        body = out[pos:pos + size]
        pos += size + 1  # 跳过内容与其后的换行
        if obj_type != "blob":
            continue
        text = body.decode("utf-8", "replace")
        for m in _SECRET_RE.finditer(text):
            found.setdefault(m.group(0), set()).add(oid_to_path.get(oid, "<未知路径>"))
    return found


def load_current_keys(repo: Path) -> dict[str, str]:
    """config.json 中当前生效的密钥（字段名 -> 值）。文件被 gitignore，可能不存在。"""
    cfg = repo / "config.json"
    if not cfg.is_file():
        return {}
    data = json.load(io.open(cfg, encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(v, str) and _SECRET_RE.fullmatch(v)
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检索历史中曾入库的疑似密钥并与当前配置比对")
    ap.add_argument("--repo", default=".", help="仓库根目录（默认 cwd）")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"[ERROR] {repo} 不是 git 仓库（本工具依赖历史）", file=sys.stderr)
        return 1

    print(f"仓库：{repo}")
    leaked = scan_history(repo)
    print(f"\n=== ① 历史中曾入库的疑似密钥（{len(leaked)} 个）===")
    if not leaked:
        print("  （无命中）")
    for secret, paths in sorted(leaked.items(), key=lambda kv: -len(kv[0])):
        shown = sorted(paths)[:3]
        more = "" if len(paths) <= 3 else f" 等 {len(paths)} 个 blob"
        print(f"  {fp(secret)}\n      线索: {shown}{more}")

    current = load_current_keys(repo)
    print(f"\n=== ② config.json 当前生效的密钥（{len(current)} 个）===")
    if not current:
        print("  （config.json 不存在或无 sk- 形态字段）")
    for name, val in current.items():
        print(f"  {name:<22} {fp(val)}")

    hits = [(s, name) for s in leaked for name, v in current.items() if s == v]
    print("\n=== ③ 结论 ===")
    if hits:
        for secret, name in hits:
            print(f"  [!!] 历史泄露的值**仍是** {name} 的生效值 —— 立即轮换（{fp(secret)}）")
        print("\n  轮换后请重跑本工具确认 ③ 为空。删除文件无效，只能到服务商处作废重发。")
        return 2
    if leaked:
        print("  历史中的泄漏值与当前配置**无交集**（已替换）。")
        print("  仍建议到服务商处作废历史 key —— 曾推送即等同已泄露。")
    else:
        print("  未发现历史泄漏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
