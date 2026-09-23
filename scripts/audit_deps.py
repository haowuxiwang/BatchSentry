"""依赖漏洞**快照**生成器（联网）—— 门禁只读快照，**自己不联网**。

为什么要有"快照"这一层（而不是让门禁直接跑 pip-audit）
------------------------------------------------------
`scripts/release_gate.py` 的定位是**离线**聚合检查（其模块 docstring 明确写着
"不做任何网络请求"）。若让门禁自己调 `pip-audit`：
  · 断网时会误红（2026-09-23 实测：直连 PyPI `Read timed out`，须走代理）；
  · CI 要吃网络依赖；
  · 而"取不到数据就判 PASS"又违反 fail-closed。
⇒ 拆成两层：
  · **本脚本**（人工/流程触发，**联网**）生成 `docs/DEPENDENCY_AUDIT.json`；
  · **门禁**只读快照，把"当前锁定版本"与快照比对 ⇒ 离线、确定、可变异验证。

⚠️ 代价（必须说清，不得假装没有）：**新披露的 CVE 不会立刻被发现** ——
只有重跑本脚本才会进入快照。所以门禁对"快照与 `requirements.txt` 脱节"与
"快照过老"都会报警。这条取舍是有意的：宁可"过期可见"，不要"断网误红"。

⚠️ **公告按 `id` 去重后再入快照**（`dedup_vulns`）。pip-audit 会在同一个包的
`vulns` 里**重复列出同一条公告**（实测每条约 2 份）⇒ 直接 `len(vulns)` 会把
"24 条唯一公告"报成"47 条"，严重度虚高约 2×。快照同时保留 `raw_entry_count`，
使"原始条目"与"唯一公告"两者都可见。

用法：
  python scripts/audit_deps.py                       # 联网扫描并写入快照
  python scripts/audit_deps.py --dry-run             # 只打印，不写文件
  python scripts/audit_deps.py --from-json <path>    # 复用已有 pip-audit JSON（离线）
  python scripts/audit_deps.py --from-json - --proxy # （代理示例见下）

⚠️ 本机实测（2026-09-23）：直连 PyPI 会超时，需要走系统代理：
  HTTPS_PROXY=http://127.0.0.1:7897 python -m pip_audit -r requirements.txt \
      --format json --progress-spinner off --timeout 90 > raw.json
  python scripts/audit_deps.py --from-json raw.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements.txt"
SNAPSHOT = REPO_ROOT / "docs" / "DEPENDENCY_AUDIT.json"

SCHEMA = 1
PIN_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([^\s;#]+)")


def locked_requirements(path: Path = REQUIREMENTS) -> dict[str, str]:
    """解析 `requirements.txt` 里的 `包==版本`（忽略注释、空行、无 `==` 的行）。

    只取**直接依赖**的锁定值 —— 与 `pip-audit -r` 的输入口径一致。
    """
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        m = PIN_RE.match(line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def _ver_tuple(v: str) -> tuple:
    """把版本串转成可比较的元组（不引入 `packaging` 新依赖）。"""
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def minimum_safe_version(fix_versions: list[str]) -> str | None:
    """**同时修掉全部公告**所需的最低版本 = 所有 `fix_versions` 里的最大值。

    坑：每条公告各有自己的修复版本，"取最小"是错的 —— 例如某包有漏洞修在
    10.2.0、另一条修在 12.2.0，升到 10.2.0 **仍然**留着后面那条。
    返回 None 表示"无修复版本可用"（更严重，门禁会如实标出来）。
    """
    vals = [v for v in fix_versions if v]
    if not vals:
        return None
    return max(vals, key=_ver_tuple)


def run_pip_audit(timeout: int = 90) -> dict:
    """联网跑 pip-audit，返回其 JSON。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pip_audit", "-r", str(REQUIREMENTS),
         "--format", "json", "--progress-spinner", "off",
         "--timeout", str(timeout)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout * 20,
    )
    if not proc.stdout.strip():
        raise SystemExit(
            "pip-audit 没有产出 JSON（rc=%s）。\n"
            "常见原因：直连 PyPI 超时 ⇒ 设 HTTPS_PROXY 后重试。\n"
            "stderr 尾部：\n%s" % (proc.returncode, proc.stderr[-1200:]))
    return json.loads(proc.stdout)


def dedup_vulns(vulns: list[dict]) -> tuple[list[dict], int]:
    """按公告 `id` 去重，返回 (去重后的公告, 原始条目数)。

    ⚠️ 为什么必须去重（2026-09-23 实测）：`pip-audit -r requirements.txt` 会在
    **同一个包**的 `vulns` 里**把同一条公告重复列出**（本机实测每个 id 正好 2 份：
    python-multipart 12 条 = 6 条唯一 ×2；pillow 31 条 = 16 条唯一 ×2）。
    若照抄 `len(vulns)`，快照就会把"47 条公告"报给用户 —— 而**真实唯一公告只有 24 条**，
    严重度被放大 ~2×。这是典型的"数字吓人但重复计数"的判据失真：它不会改变
    门禁的 FAIL 判定（判定只看"有没有"），但会污染证据链与人工判断。

    `fix_versions` 取并集 —— 同一条公告在不同来源里修复版本可能不全，取并集更保守
    （且 `minimum_safe_version` 本来就取最大值，并集不改变结论）。
    """
    order: list[str] = []
    merged: dict[str, dict] = {}
    for v in vulns:
        vid = v.get("id") or "(无 id)"
        if vid not in merged:
            order.append(vid)
            merged[vid] = {"id": v.get("id"),
                           "aliases": list(v.get("aliases") or []),
                           "fix_versions": list(v.get("fix_versions") or [])}
        else:
            cur = merged[vid]
            for a in (v.get("aliases") or []):
                if a not in cur["aliases"]:
                    cur["aliases"].append(a)
            for f in (v.get("fix_versions") or []):
                if f not in cur["fix_versions"]:
                    cur["fix_versions"].append(f)
    return [merged[i] for i in order], len(vulns)


def build_snapshot(raw: dict) -> dict:
    """把 pip-audit 的原始输出压成**门禁够用**的快照（去掉大段 description）。

    **公告按 id 去重**（见 `dedup_vulns`）。同时保留 `raw_entry_count`，
    让"pip-audit 原始列了几条"与"真实唯一公告几条"**同时可见** ——
    去重不是把信息藏起来，而是让主数字不再说谎。
    """
    packages: dict[str, dict] = {}
    advisories = 0
    raw_entries = 0
    for dep in raw.get("dependencies", []):
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        uniq, raw_n = dedup_vulns(vulns)
        fixes = [f for v in uniq for f in (v.get("fix_versions") or [])]
        packages[dep["name"].lower()] = {
            "version": dep.get("version"),
            "advisory_count": len(uniq),
            "raw_entry_count": raw_n,
            "minimum_safe_version": minimum_safe_version(fixes),
            "vulns": uniq,
        }
        advisories += len(uniq)
        raw_entries += raw_n
    locked = locked_requirements()
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "pip-audit（PyPI 公告库）",
        "requirements_sha256": hashlib.sha256(
            REQUIREMENTS.read_bytes()).hexdigest(),
        # 扫描时 requirements.txt 的锁定值：门禁据此判断"快照是否与当前锁定一致"
        "locked": locked,
        "packages_with_vulns": packages,
        "totals": {
            "dependencies_scanned": len(raw.get("dependencies", [])),
            "advisories": advisories,
            "raw_advisory_entries": raw_entries,
            "packages_with_vulns": len(packages),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="依赖漏洞快照生成器（联网）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--from-json", default=None,
                    help="复用已有 pip-audit JSON（离线；用 - 表示 stdin）")
    ap.add_argument("--timeout", type=int, default=90, help="pip-audit 读超时秒")
    args = ap.parse_args(argv)

    if args.from_json:
        text = (sys.stdin.read() if args.from_json == "-"
                else Path(args.from_json).read_text(encoding="utf-8"))
        raw = json.loads(text)
    else:
        raw = run_pip_audit(args.timeout)

    snap = build_snapshot(raw)
    t = snap["totals"]
    print(f"锁定依赖 {len(snap['locked'])} 个｜扫描条目 {t['dependencies_scanned']} 个")
    print(f"公告 {t['advisories']} 条（唯一），涉及 {t['packages_with_vulns']} 个包"
          f"；pip-audit 原始条目 {t.get('raw_advisory_entries')} 条"
          f"（重复 {t.get('raw_advisory_entries', 0) - t['advisories']} 条，已按 id 去重）")
    for name, p in sorted(snap["packages_with_vulns"].items(),
                          key=lambda kv: -kv[1]["advisory_count"]):
        safe = p["minimum_safe_version"] or "**无修复版本**"
        raw_n = p.get("raw_entry_count", p["advisory_count"])
        extra = f"（原始 {raw_n}）" if raw_n != p["advisory_count"] else ""
        print(f"  {name:22} {p['version']:10} {p['advisory_count']:>3} 条{extra}"
              f"  ⇒ 最低安全版本 {safe}")

    if args.dry_run:
        print("\n(--dry-run：未写入)")
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n已写入 {SNAPSHOT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
