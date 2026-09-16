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
  kb_packaging      每个 KB 源都随包分发（spec datas 覆盖 core/kb/data/*.json）
  tests_coverage    单测通过 + 覆盖率 ≥ 门禁

失败事实源：优先 `--junitxml`（机器可读，免疫 `log_cli` 日志交错）；XML 缺失/
损坏才回落 stdout 文本解析。任何失败都会把原始 pytest 输出落盘
`devlogs/gate_pytest_<ts>.log`；环境专有失败（`ENV_ONLY_FAILURE_PREFIXES`）在
报告里显式登记为 `env_only_failures` 并降级 WARN。

用法：
  python scripts/release_gate.py                    # 全量
  python scripts/release_gate.py --skip-tests       # 只做结构检查（秒级）
  python scripts/release_gate.py --fail-under 95 --json

退出码：0 = 无 FAIL；1 = 存在 FAIL。WARN/SKIP 不影响退出码。

⚠️ 输出编码：本脚本打印**中文**报告，`main()` 一开始就把 stdout/stderr 切成 UTF-8
   （`errors="replace"`）。Windows 控制台的默认编码取决于代码页 —— 本地开发机
   （936/65001）能显示中文，而 **GitHub Actions 的 Windows runner 是 cp1252**，
   不切就会死在第一个 `print` 上，把"门禁全过"变成"CI 失败"（2026-09-15 CI run #1
   实测：检查跑满 3.5 分钟、报告已落盘，只死在打印那一步）。

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
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
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
# 该失败有 flaky 性（与沙箱 safe-delete 状态相关），且此前因文本解析脆弱而
# 导致 failed>0 但 nodeid 解析为空 → 误判 FAIL（见 T0）。现改以 junitxml 为准。
ENV_ONLY_FAILURE_PREFIXES = (
    "tests/integration/test_main_routes.py::TestServePdf",
)

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"


# ── 数据模型 ────────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """单项检查结果。status ∈ {pass, fail, warn, skip}。

    raw_log  失败的原始 pytest 输出落盘路径（T0.2，可事后回溯）
    env_only 被判定为"环境专有、非回归"的失败 nodeid（T0.3，机器可读标注）
    """

    name: str
    status: str
    detail: str = ""
    duration_ms: int = 0
    raw_log: str = ""
    env_only: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _ms() -> int:
    return int(time.time() * 1000)


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 的编码强制为 UTF-8（`errors="replace"`）。

    为什么必须有：本脚本打印**中文**报告，而 Windows 文本流的默认编码来自代码页 ——
    本地开发机（936/65001）能显示中文，**GitHub Actions 的 Windows runner 是 cp1252**
    → `_print_report` 第一行 `print` 就 `UnicodeEncodeError` → 退出码 1，把"门禁 6 项
    全过"变成"CI 红"（2026-09-15 CI run #1 实测：检查跑满 3.5 分钟、报告已落盘，
    只死在打印那一步）。

    修在脚本自身、而不是 workflow 的环境变量里 —— 它是唯一入口，谁调用都该拿到
    正确行为（本地、CI、将来别的调度器都一样）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # 已被重定向/替换成非 TextIOWrapper 时静默跳过


# ── 子进程封装 ──────────────────────────────────────────────────────────────


def run_cmd(cmd: list[str], *, timeout: int | None = None,
            env: dict | None = None) -> tuple[int, str]:
    """在仓库根目录执行命令，返回 (returncode, 合并输出)。"""
    merged = os.environ.copy()
    merged["PBC_NO_FILE_LOG"] = "1"
    # 子进程（pytest）也要按 UTF-8 输出：它的中文日志经管道回传，若按控制台代码页
    # 编码（Windows runner = cp1252）会撞上与 `_force_utf8_stdio` 同一个坑。
    # 显式统一，让本地与 CI 行为一致，而不是靠各自的区域设置碰运气。
    merged["PYTHONIOENCODING"] = "utf-8"
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


_KB_SPEC_GLOB_RE = re.compile(
    r"""["']core["']\s*/\s*["']kb["']\s*/\s*["']data["']"""
    r"""[\s\S]{0,200}?glob\(\s*["']\*\.json["']\s*\)"""
)


def check_kb_packaging(spec_path: Path | None = None,
                       kb_dir: Path | None = None) -> CheckResult:
    """每个 KB 源都必须随包分发（spec 的 datas 必须覆盖 core/kb/data/*.json）。

    防的漂移：M5 把知识库从单源扩成多源后，`pbc-server.spec` 仍只硬编码
    `gmp2010.json` → 冻结版其余 5 源静默缺失（引用退化、无法溯源）。
    两种合法形态：① datas 用 `core/kb/data` + `glob("*.json")` 自动枚举；
    ② 逐个显式列出每个源文件名。二者皆缺任一源即 FAIL。
    """
    t0 = _ms()
    spec = spec_path or (REPO_ROOT / "pbc-server.spec")
    kb_dir = kb_dir or KB_DATA_DIR

    try:
        text = spec.read_text(encoding="utf-8")
    except OSError as e:
        return CheckResult("kb_packaging", FAIL,
                           f"无法读取 {spec.name}: {e}", _ms() - t0)

    sources = sorted(p.name for p in kb_dir.glob("*.json"))
    if not sources:
        return CheckResult("kb_packaging", FAIL,
                           f"{kb_dir} 下无 KB 源 JSON", _ms() - t0)

    if _KB_SPEC_GLOB_RE.search(text):
        return CheckResult(
            "kb_packaging", PASS,
            f"{len(sources)} 个 KB 源经 core/kb/data glob 自动入包", _ms() - t0)

    missing = [n for n in sources if n not in text]
    if missing:
        return CheckResult(
            "kb_packaging", FAIL,
            f"{spec.name} 未覆盖 KB 源：{', '.join(missing)}（共 {len(sources)} 源）",
            _ms() - t0)
    return CheckResult("kb_packaging", PASS,
                       f"{len(sources)} 个 KB 源已显式入包", _ms() - t0)


def _parse_pytest_summary(output: str) -> tuple[int, int, list[str]]:
    """从 pytest 输出解析 (passed, failed, failed_nodeids)。

    注意：`-q` 的实时进度行也以 "FAILED" 开头（形如 `FAILED ... [ 23%]`），
    它不是 nodeid —— 只保留形如 `path::Case::test` 或 `path.py` 的条目。
    """
    import re

    passed = failed = 0
    m = re.search(r"(\d+) passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        failed = int(m.group(1))
    nodeids: list[str] = []
    for line in output.splitlines():
        s = line.strip()
        if not (s.startswith("FAILED ") or s.startswith("ERROR ")):
            continue
        node = s.split(None, 1)[1].split(" - ")[0].strip()
        # 进度行残片（如 "[ 23%]"）无 :: 且非 .py → 丢弃
        if "::" not in node and not node.endswith(".py"):
            continue
        if node not in nodeids:
            nodeids.append(node)
    return passed, failed, nodeids


def _junit_nodeid(classname: str, name: str) -> str:
    """把 junit 的 (classname, name) 还原成 pytest nodeid。

    pytest junitxml：`classname` = 模块点分路径[.类名]（如
    `tests.integration.test_main_routes.TestServePdf`），`name` = 用例函数名。
    还原：先按"最长且真实存在的 .py 前缀"确定文件，余下段落作为类名链，
    再拼 `name`。这样得到的 nodeid 才能与 `ENV_ONLY_FAILURE_PREFIXES`
    之类的文件级前缀匹配（避免"点分路径无 .py"导致的匹配失败）。
    """
    segs = [s for s in classname.split(".") if s] if classname else []
    file_rel, rest_start = None, len(segs)
    for cut in range(len(segs), 0, -1):
        cand = "/".join(segs[:cut]) + ".py"
        if (REPO_ROOT / cand).exists():
            file_rel, rest_start = cand, cut
            break
    if file_rel is None:  # 兜底：无法定位真实文件 → 点分退斜杠
        file_rel, rest_start = ("/".join(segs) + ".py") if segs else "", len(segs)
    quals = segs[rest_start:] + ([name] if name else [])
    return f"{file_rel}::{'::'.join(quals)}" if quals else file_rel


def _parse_junit(xml_path: Path) -> tuple[int, int, list[str]] | None:
    """从 junitxml 解析 (passed, failed, failed_nodeids)。

    返回 None 表示 XML 缺失/损坏 → 调用方回落到文本解析（T0.1/T0.4）。
    这是**首选事实源**：不再依赖 stdout 文本，免疫 `log_cli` 日志交错。
    """
    try:
        root = ET.parse(str(xml_path)).getroot()
    except (ET.ParseError, OSError):
        return None
    suites = root.findall(".//testsuite")
    if not suites and root.tag == "testsuite":
        suites = [root]
    total = failures = errors = skipped = 0
    nodeids: list[str] = []
    for suite in suites:
        total += int(suite.get("tests") or 0)
        failures += int(suite.get("failures") or 0)
        errors += int(suite.get("errors") or 0)
        skipped += int(suite.get("skipped") or 0)
        for case in suite.findall("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            node = _junit_nodeid(case.get("classname") or "", case.get("name") or "")
            if node and node not in nodeids:
                nodeids.append(node)
    failed = failures + errors
    passed = max(0, total - failed - skipped)
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


def _audit_dir() -> Path:
    """门禁产物的落盘目录（`devlogs/`）：报告、junit 审计副本、pytest 落盘日志。

    刻意做成函数而非常量：测试需要替换它，但**不能**直接 monkeypatch `REPO_ROOT`
    —— `_junit_nodeid` 依赖 `REPO_ROOT` 定位真实 `.py` 文件，一旦被替换，nodeid 会
    退化成"点分转斜杠"，env-only 前缀匹配随之失效。审计目录必须是独立的注入点。
    """
    return REPO_ROOT / "devlogs"


def _dump_pytest_log(raw: str, stamp: str | None = None) -> Path:
    """失败时把原始 pytest 输出落盘（T0.2），供事后回溯，避免"未解析出用例"。"""
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    out = _audit_dir() / f"gate_pytest_{stamp}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8", errors="replace")
    return out


def check_tests_and_coverage(fail_under: int = 95, python: str | None = None,
                             timeout: int = 1800) -> CheckResult:
    """单测通过 + 覆盖率达标（走 coverage run，规避沙箱 cov.combine 删除）。

    失败事实源优先取 **junitxml**（T0.1）；XML 缺失/损坏才回落到 stdout 文本解析。
    """
    t0 = _ms()
    py = python or sys.executable
    if not Path(py).exists() and not py.startswith("py"):
        return CheckResult("tests_coverage", FAIL, f"python 不可用：{py}", _ms() - t0)

    tmp = Path(tempfile.gettempdir())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cov_file = tmp / "pbc_release_gate.coverage"
    junit = tmp / f"pbc_release_junit_{os.getpid()}_{stamp}.xml"
    env = {"COVERAGE_FILE": str(cov_file)}

    rc_run, out_run = run_cmd(
        [py, "-m", "coverage", "run", f"--source={COVERAGE_SOURCES}",
         "-m", "pytest", "-o", "addopts=-q --tb=line", "-p", "no:cacheprovider",
         f"--junitxml={junit}"],
        timeout=timeout, env=env,
    )

    # ① 首选：junitxml（机器可读，免疫日志交错）
    parsed = _parse_junit(junit)
    source = "junitxml"
    if parsed is None:
        # ② 回落：stdout 文本解析
        parsed = _parse_pytest_summary(out_run)
        source = "stdout"
    passed, failed, nodeids = parsed

    # ③ junit 的**可审计副本**（T0.5）：原件落在系统临时目录，CI 上随 runner 一起消失，
    #    也不在 artifact 上传列表里 → "CI 到底跑了哪些用例"将无从查证。这个盲区是实测
    #    踩到的：只拿得到 stdout 落盘日志（`-o addopts=-q` 下只有点和汇总），44 条差异
    #    里只能钉死 17 条 skip，其余无法归因。故在 devlogs/ 另存一份，由 CI 的 artifact
    #    带上 —— 于是任何一次 CI 运行都能逐条核对"跑了什么、跳了什么"。
    audit_junit: Path | None = None
    if junit.exists():
        try:
            audit_dir = _audit_dir()
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_junit = audit_dir / f"gate_junit_{stamp}.xml"
            audit_junit.write_bytes(junit.read_bytes())
        except OSError:
            audit_junit = None

    env_only = [n for n in nodeids if n.startswith(ENV_ONLY_FAILURE_PREFIXES)]
    real_failures = [n for n in nodeids if n not in env_only]

    # ③ 失败必落原始输出（T0.2）
    raw_log = ""
    if failed or rc_run != 0:
        raw_log = str(_dump_pytest_log(
            f"$ rc={rc_run}  source={source}  junit={junit}\n"
            f"$ passed={passed} failed={failed} nodeids={nodeids}\n"
            f"{'=' * 70}\n{out_run}",
            stamp,
        ))

    if rc_run != 0 and not nodeids:
        return CheckResult("tests_coverage", FAIL,
                           f"pytest 未能完成（rc={rc_run}）：{out_run.strip()[-240:]}",
                           _ms() - t0, raw_log)

    rc_rep, out_rep = run_cmd(
        [py, "-m", "coverage", "report", "--format=total", "--precision=2"],
        timeout=300, env=env,
    )
    total = _parse_coverage_total(out_rep)
    if total is None:
        # 旧版 coverage 无 --format=total → 回落到表格解析（整数精度）
        rc_rep, out_rep = run_cmd([py, "-m", "coverage", "report"],
                                  timeout=300, env=env)
        total = _parse_coverage_total(out_rep)

    parts = [f"{passed} passed", f"{failed} failed",
             f"coverage={total if total is not None else '?'}% (门禁 {fail_under}%)",
             f"fact={source}"]
    if audit_junit is not None:
        try:
            parts.append(f"junit={audit_junit.relative_to(REPO_ROOT).as_posix()}")
        except ValueError:  # REPO_ROOT 被重定向（测试）时仍给出可用路径
            parts.append(f"junit={audit_junit}")
    detail = ", ".join(parts)

    if real_failures:
        return CheckResult("tests_coverage", FAIL,
                           f"{detail}；真实失败：{real_failures[:5]}", _ms() - t0, raw_log)
    if failed and not nodeids:
        # 失败计数与用例解析不一致 → 事实源不可信，fail-closed
        return CheckResult("tests_coverage", FAIL,
                           f"{detail}；pytest 报告 {failed} 项失败但未能列出用例"
                           f"（事实源 {source}）", _ms() - t0, raw_log)
    if total is None:
        return CheckResult("tests_coverage", WARN,
                           f"{detail}；未能解析覆盖率（rc={rc_rep}）", _ms() - t0, raw_log)
    if total < fail_under:
        return CheckResult("tests_coverage", FAIL, detail, _ms() - t0, raw_log)
    if env_only:
        return CheckResult(
            "tests_coverage", WARN,
            f"{detail}；仅环境专有失败（沙箱产物，非回归，已登记 allowlist）：{env_only}",
            _ms() - t0, raw_log, env_only)
    return CheckResult("tests_coverage", PASS, detail, _ms() - t0, raw_log)


# ── 编排 ────────────────────────────────────────────────────────────────────


def run_all(*, skip_tests: bool = False, fail_under: int = 95,
            python: str | None = None, timeout: int = 1800) -> list[CheckResult]:
    """按序执行全部检查（结构检查在前，重测试在后）。"""
    results = [
        check_worktree_clean(),
        check_packaging_files(),
        check_rules_wired(),
        check_kb_corpus(),
        check_kb_packaging(),
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
        # T0.3：环境专有失败（已登记 allowlist）与原始日志显式上浮到报告顶层
        "env_only_failures": [n for r in results for n in (r.env_only or [])],
        "raw_logs": [r.raw_log for r in results if r.raw_log],
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
    if report.get("env_only_failures"):
        print("注意：以下失败为环境专有（沙箱产物，非回归，已登记 allowlist）：")
        for n in report["env_only_failures"]:
            print(f"  - {n}")
    for lg in report.get("raw_logs", []):
        print(f"原始 pytest 输出: {lg}")
    print(f"报告: {path}")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
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
