"""把第三方真实数据（批号 / 企业名 / 员工姓名）从**被跟踪文件**里替换成等价占位符。

## 为什么需要它

`haowuxiwang/BatchSentry` 是**公开**仓库，而仓库的文本里混进了真实批记录标识与员工姓名
（详见 `docs/TODO.md` 的 0-65）。PDF 本身从未入库（`.gitignore` 钉住了），泄露的是
**文档与测试夹具里的字面量**。

## 为什么映射表**不在仓库里**

映射表的左边就是真实数据。把它写进脚本，等于把要清理的东西再提交一遍。
所以映射表放在 **gitignored** 的 `devlogs/_verify/pii_map.json`：

    {
      "替换": {"真实串": "占位串", ...},
      "说明": "本文件含真实数据，禁止入库"
    }

代价：别人 clone 下来跑不了。这是**刻意**的 —— 这是一次性清理工具，不是产品功能。

## 用法

    python scripts/scrub_pii.py                # 预演（默认）：只报告，不写盘
    python scripts/scrub_pii.py --apply        # 真写盘
    python scripts/scrub_pii.py --check        # CI 用：有命中即 rc=1

## 设计约束

- **按字节替换**（`read_bytes`/`write_bytes`）：`read_text`/`write_text` 往返会把 LF 变 CRLF，
  而 `git diff` 因 `autocrlf=true` 看不出来（本项目的老坑）。
- **扫"已跟踪 + 未跟踪但未被忽略"两类**：只扫 `git ls-files` 有一个**致命盲区** ——
  **新建的文件在 `git add` 之前对它不可见**。R128 实测踩到：`docs/LICENSE_AND_DEPENDENCY_AUDIT.md`
  是那一轮新写的，跑 `--check` 时它还是未跟踪状态 ⇒ 报告"残留 0"，提交后才被门禁
  `no_pii_in_tracked_files` 抓到 **12 处**真实标识。判据必须是"**即将被提交的文件**"，
  而那正好是 `git ls-files` ∪ `git ls-files --others --exclude-standard`。
- **替换顺序由映射表顺序决定**：先长后短由人保证（脚本只按给定顺序执行，并在
  `--check` 时报告"替换后仍命中"的残留，避免 A→B 之后 B 又被后面的规则改掉）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "devlogs" / "_verify" / "pii_map.json"

# 只处理这些后缀：二进制（pdf/png/db）本来就不该有字面量，也不需要改写。
# ⚠️ **无后缀的文件也要处理**（`suffix == ""`）：`.gitignore` 就属于这一类，
#    而它正是真实文件名藏身的地方之一（首版漏了它 —— 逐后缀过滤把 dotfile 全跳过了）。
SUFFIXES = {".py", ".md", ".json", ".txt", ".js", ".html", ".sql", ".yml", ".yaml", ".ps1", ".bat"}


def _is_text_candidate(p: Path) -> bool:
    return p.suffix.lower() in SUFFIXES or p.suffix == ""


def _git_paths(*args: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", *args], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8")
    if out.returncode != 0:
        raise SystemExit(f"git ls-files 失败：{out.stderr[:300]}")
    return [ROOT / p for p in out.stdout.split("\0") if p]


def scan_targets() -> list[Path]:
    """**即将被提交的文件**：已跟踪 ∪ 未跟踪但未被忽略。

    为什么要并上第二类：新建的文件在 `git add` 之前不在 `git ls-files` 里，
    只扫第一类会给出**"残留 0"的假结论**（R128 实测：一份 12 处命中的新文档
    因此漏过，直到提交后被门禁抓到）。去重保序，避免同一文件被扫两遍。
    """
    seen: dict[str, Path] = {}
    for p in _git_paths() + _git_paths("--others", "--exclude-standard"):
        seen.setdefault(str(p), p)
    return list(seen.values())


def load_map(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        raise SystemExit(
            f"缺映射表：{path}\n"
            "该文件**故意**不入库（它含真实数据）。请按模块 docstring 的格式创建后再跑。"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs = data.get("替换") or {}
    if not pairs:
        raise SystemExit(f"{path} 里没有 `替换` 键或为空 ⇒ 空映射会让本工具静默变成空转")
    return list(pairs.items())


def main() -> int:
    ap = argparse.ArgumentParser(description="把真实批记录标识/姓名替换成占位符")
    ap.add_argument("--apply", action="store_true", help="真写盘（默认只预演）")
    ap.add_argument("--check", action="store_true", help="有命中即 rc=1（CI 用，不写盘）")
    ap.add_argument("--map", default=str(DEFAULT_MAP), help="映射表路径")
    args = ap.parse_args()
    if args.apply and args.check:
        raise SystemExit("--apply 与 --check 互斥")

    pairs = load_map(Path(args.map))
    total = 0
    touched: list[tuple[str, int]] = []

    for p in scan_targets():
        if not p.is_file() or not _is_text_candidate(p):
            continue
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        hits = sum(text.count(old) for old, _ in pairs)
        if not hits:
            continue
        total += hits
        rel = p.relative_to(ROOT).as_posix()
        touched.append((rel, hits))
        if args.apply:
            for old, new in pairs:
                text = text.replace(old, new)
            p.write_bytes(text.encode("utf-8"))

    print(f"映射条目 {len(pairs)} 条 | 命中文件 {len(touched)} 个 | 命中 {total} 处")
    for rel, n in sorted(touched, key=lambda x: -x[1]):
        print(f"  {n:4d}  {rel}")

    if args.apply:
        # 写盘后必须复核：若仍有命中，说明映射表有洞（例如 A→B 之后 B 又被后面的规则改掉）
        residual = 0
        for p in scan_targets():
            if not p.is_file() or not _is_text_candidate(p):
                continue
            try:
                text = p.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            residual += sum(text.count(old) for old, _ in pairs)
        print(f"写盘完成。残留命中：{residual} 处")
        if residual:
            print("⚠️ 有残留 ⇒ 映射表未覆盖全部形态，请补条目后重跑", file=sys.stderr)
            return 1
        return 0

    if args.check:
        return 1 if total else 0
    if total:
        print("\n这是**预演**。确认映射无误后加 --apply 真正写盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
