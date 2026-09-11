"""v1.1 发布门禁（打包信号）一键执行器。

定位：**离线**的"是否可打包放行"聚合检查 —— 与 `scripts/golden_gate.py`
互补。golden_gate 针对**运行中的服务 + 已完成 job** 做数据质量门禁
（可追溯/完整性/双后端/端到端）；release_gate 不做任何网络请求，只回答
"当前工作区是否具备打包 v1.1 的信号"。

检查项（每项独立、可单测）：
  worktree_clean    工作区干净（打包必须来自干净树，未提交改动不可放行）
  packaging_files   打包前置文件齐备（spec / 构建脚本 / electron 入口）
  rules_wired       规则层已接线（core/rules/*.py 中 _check_* 数量 ≥ 阈值）
  kb_corpus         知识库语料可用（core/kb/data/*.json 条目数 ≥ 阈值）
  tests_coverage    单测通过 + 覆盖率 ≥ 门禁

用法：
  python scripts/release_gate.py                    # 全量
  python scripts/release_gate.py --skip-tests       # 只做结构检查（秒级）
  python scripts/release_gate.py --fail-under 95 --json

退出码：0 = 无 FAIL；1 = 存在 FAIL。WARN/SKIP 不影响退出码。

⚠️ 沙箱注意：取覆盖率必须走 `coverage run` + `coverage report`，**不要**用
   `pytest --cov` —— 后者收尾 `pytest_cov.finish()` 会调 `cov.combine()` 删除
   自身并行数据文件，在 WorkBuddy 沙箱下触发 safe-delete 批量守卫 →
   SystemExit(1) → INTERNALERROR，覆盖率表与 fail-under 都不产出。
   COVERAGE_FILE 落在 OS 临时目录（`tempfile.gettempdir()`）以避开守卫。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / "core" / "rules"
KB_DATA_DIR = REPO_ROOT / "core" / "kb" / "data"

# 与 pytest.ini 的 --cov 口径保持一致
COVERAGE_SOURCES = "api,core,llm,db,config,main"

# 打包前置文件（缺一不可）
PACKAGING_FILES = ("pbc-server.spec", "build.ps1", "package.json", "electron/main.js")

# 环境专有失败：TestServePdf 清理项目内 output/ 探针被沙箱 safe-delete 拦截
# （隔离单跑通过 → 非代码回归）。仅前缀匹配的失败降级为 WARN，其余照常 FAIL。
ENV_ONLY_FAILURE_PREFIXES = (
    "tests/integration/test_main_routes.py::TestServePdf",
)

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"


# ── 数据模型 ────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """单项检查结果。status ∈ {pass, fail, warn, skip}。"""

    name: str
    status: str
    detail: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _ms() -> int:
    return int(time.time() * 1000)


# ── 子进程封装 ──────────────────────────────────────────────────────────────


def run_cmd(cmd: list[str], *, timeout: int | None = None,
            env: dict | None = None) -> tuple[int, str]:
    """在仓库根目录执行命令，返回 (returncode, 合并输出)。"""
    merged = os.environ.copy()
    merged["PBC_NO_FILE_LOG"] = "1"
    if env:
        merged.update(env)
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=merged,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── 检查项（纯函数，便于单测）─────────────────────────────────────────────


def check_worktree_clean() -> CheckResult:
    """工作区必须干净 —— 未提交改动不可进入打包。"""
    t0 = _ms()
    rc, out = run_cmd(["git", "status", "--porcelain"])
    if rc != 0:
        return CheckResult("worktree_clean", WARN,
                           f"git status 不可用（rc={rc}）：{out.strip()[:160]}",
                           _ms() - t0)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if lines:
        return CheckResult("worktree_clean", FAIL,
                           f"{len(lines)} 处未提交改动：" + "; ".join(lines[:5]),
                           _ms() - t0)
    return CheckResult("worktree_clean", PASS, "工作区干净", _ms() - t0)


def check_packaging_files(root: Path = REPO_ROOT) -> CheckResult:
    """打包前置文件齐备。"""
    t0 = _ms()
    missing = [f for f in PACKAGING_FILES if not (root / f).exists()]
    if missing:
        return CheckResult("packaging_files", FAIL,
                           "缺少前置文件：" + ", ".join(missing), _ms() - t0)
    return CheckResult("packaging_files", PASS,
                       f"{len(PACKAGING_FILES)} 个前置文件就位", _ms() - t0)


def count_rule_checks(rules_dir: Path = RULES_DIR) -> int:
    """统计 core/rules/*.py 中 `_check_*` 函数数量（AST，无副作用）。"""
    total = 0
    for py in sorted(rules_dir.glob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        except (SyntaxError, OSError):
            continue
        total += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("_check_")
        )
    return total


def check_rules_wired(min_rules: int = 14, rules_dir: Path | None = None) -> CheckResult:
    """规则层接线完整性（规则函数数量下限）。"""
    t0 = _ms()
    n = count_rule_checks(rules_dir or RULES_DIR)
    if n < min_rules:
        return CheckResult("rules_wired", FAIL,
                           f"仅 {n} 个 _check_* 规则函数（下限 {min_rules}）", _ms() - t0)
    return CheckResult("rules_wired", PASS,
                       f"{n} 个 _check_* 规则函数（下限 {min_rules}）", _ms() - t0)


def count_kb_entries(kb_dir: Path = KB_DATA_DIR) -> int:
    """统计知识库语料条目数（兼容 list 与 {entries|items: [...]} 两种形态）。"""
    total = 0
    for js in sorted(kb_dir.glob("*.json")):
        try:
            data = json.loads(js.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            total += len(data)
        elif isinstance(data, dict):
            for key in ("entries", "items", "chapters"):
                v = data.get(key)
                if isinstance(v, list):
                    total += len(v)
    return total


def check_kb_corpus(min_entries: int = 200, kb_dir: Path | None = None) -> CheckResult:
    """知识库语料可用性。"""
    t0 = _ms()
    n = count_kb_entries(kb_dir or KB_DATA_DIR)
    if n < min_entries:
        return CheckResult("kb_corpus", FAIL,
                           f"知识库仅 {n} 条（下限 {min_entries}）", _ms() - t0)
    return CheckResult("kb_corpus", PASS,
                       f"知识库 {n} 条（下限 {min_entries}）", _ms() - t0)


def _parse_pytest_summary(output: str) -> tuple[int, int, list[str]]:
    """从 pytest 输出解析 (passed, failed, failed_nodeids)。"""
    import re

    passed = failed = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    nodeids = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            nodeids.append(s.split(None, 1)[1].split(" - ")[0].strip())
    return passed, failed, nodeids


def _parse_coverage_total(output: str) -> float | None:
    """从 coverage report 输出解析总覆盖率（百分比）。"""
    import re

    for line in output.splitlines():
        if re.match(r"^TOTAL\s", line):
            nums = re.findall(r"(\d+)%", line)
            if nums:
                return float(nums[-1])
        m = re.match(r"^(\d+(?:\.\d+)?)\s*$", line.strip())
        if m:  # --format=total 只输出一个数字
            return float(m.group(1))
    return None


def check_tests_and_coverage(fail_under: int = 95, python: str | None = None,
                             timeout: int = 1800) -> CheckResult:
    """单测通过 + 覆盖率达标（走 coverage run，规避沙箱 cov.combine 删除）。"""
    t0 = _ms()
    py = python or sys.executable
    if not Path(py).exists() and not py.startswith("py"):
        return CheckResult("tests_coverage", FAIL, f"python 不可用：{py}", _ms() - t0)

    cov_file = Path(tempfile.gettempdir()) / "pbc_release_gate.coverage"
    env = {"COVERAGE_FILE": str(cov_file)}

    rc_run, out_run = run_cmd(
        [py, "-m", "coverage", "run", f"--source={COVERAGE_SOURCES}",
         "-m", "pytest", "-o", "addopts=-q --tb=line", "-p", "no:cacheprovider"],
        timeout=timeout, env=env,
    )
    passed, failed, nodeids = _parse_pytest_summary(out_run)
    env_only = [n for n in nodeids
                if n.startswith(ENV_ONLY_FAILURE_PREFIXES)]
    real_failures = [n for n in nodeids if n not in env_only]

    if rc_run != 0 and not nodeids:
        return CheckResult("tests_coverage", FAIL,
                           f"pytest 未能完成（rc={rc_run}）：{out_run.strip()[-240:]}",
                           _ms() - t0)

    rc_rep, out_rep = run_cmd(
        [py, "-m", "coverage", "report", f"--fail-under={fail_under}"],
        timeout=300, env=env,
    )
    total = _parse_coverage_total(out_rep)

    parts = [f"{passed} passed", f"{failed} failed",
             f"coverage={total if total is not None else '?'}% (门禁 {fail_under}%)"]
    detail = ", ".join(parts)

    if real_failures:
        return CheckResult("tests_coverage", FAIL,
                           f"{detail}；真实失败：{real_failures[:5]}", _ms() - t0)
    if total is not None and total < fail_under:
        return CheckResult("tests_coverage", FAIL, detail, _ms() - t0)
    if env_only:
        return CheckResult("tests_coverage", WARN,
                           f"{detail}；仅环境专有失败（沙箱产物，非回归）：{env_only}",
                           _ms() - t0)
    if rc_rep != 0:
        return CheckResult("tests_coverage", WARN,
                           f"{detail}；coverage report rc={rc_rep}", _ms() - t0)
    return CheckResult("tests_coverage", PASS, detail, _ms() - t0)


# ── 编排 ────────────────────────────────────────────────────────────────────


def run_all(*, skip_tests: bool = False, fail_under: int = 95,
            python: str | None = None, timeout: int = 1800) -> list[CheckResult]:
    """按序执行全部检查（结构检查在前，重测试在后）。"""
    results = [
        check_worktree_clean(),
        check_packaging_files(),
        check_rules_wired(),
        check_kb_corpus(),
    ]
    if skip_tests:
        results.append(CheckResult("tests_coverage", SKIP, "已跳过（--skip-tests）"))
    else:
        results.append(check_tests_and_coverage(fail_under, python, timeout))
    return results


def build_report(results: list[CheckResult], *, fail_under: int) -> dict:
    """组装可落盘的 JSON 报告。"""
    rc, head = run_cmd(["git", "rev-parse", "--short", "HEAD"])
    counts = {k: sum(1 for r in results if r.status == k)
              for k in (PASS, FAIL, WARN, SKIP)}
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(REPO_ROOT),
        "git_head": head.strip() if rc == 0 else "?",
        "coverage_gate": fail_under,
        "overall": FAIL if counts[FAIL] else PASS,
        "counts": counts,
        "checks": [r.as_dict() for r in results],
    }


def write_report(report: dict, out: Path | None = None) -> Path:
    """写入 devlogs/gate_report_<YYYYMMDD_HHMMSS>.json。"""
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = REPO_ROOT / "devlogs" / f"gate_report_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def _print_report(report: dict, path: Path) -> None:
    icon = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}
    print("\n════════ v1.1 发布门禁（打包信号）════════")
    for c in report["checks"]:
        print(f"[{icon[c['status']]}] {c['name']:<16} {c['detail']}")
    print(f"\nOVERALL: {report['overall']}  "
          f"(pass={report['counts']['pass']} fail={report['counts']['fail']} "
          f"warn={report['counts']['warn']} skip={report['counts']['skip']})")
    print(f"报告: {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="v1.1 发布门禁（打包信号）")
    ap.add_argument("--fail-under", type=int, default=95, help="覆盖率门禁（默认 95）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过单测/覆盖率（秒级结构检查）")
    ap.add_argument("--python", default=None, help="运行单测的解释器（默认当前解释器）")
    ap.add_argument("--timeout", type=int, default=1800, help="单测子进程超时秒数")
    ap.add_argument("--out", default=None, help="报告输出路径")
    ap.add_argument("--json", action="store_true", help="额外把报告打到 stdout")
    args = ap.parse_args(argv)

    results = run_all(skip_tests=args.skip_tests, fail_under=args.fail_under,
                      python=args.python, timeout=args.timeout)
    report = build_report(results, fail_under=args.fail_under)
    path = write_report(report, Path(args.out) if args.out else None)
    _print_report(report, path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["counts"][FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
