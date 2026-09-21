"""scripts/release_gate.py 单测 —— 纯函数与编排（M1c）。

`scripts/` 非包 → 用 importlib 从文件路径加载，避免污染 sys.path。
不触发真实 pytest 子进程（check_tests_and_coverage 的子进程路径由
run_cmd 的解析辅助函数单独做纯逻辑验证）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_RG_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_gate.py"

def _load():
    import sys

    spec = importlib.util.spec_from_file_location("release_gate", _RG_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 装饰器需在 exec 前完成模块注册（查 sys.modules[cls.__module__]）
    sys.modules["release_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


rg = _load()


# ── 解析辅助 ────────────────────────────────────────────────────────────────


class TestParsers:
    def test_pytest_summary_counts(self):
        out = (
            "FAILED                                                                   [ 23%]\n"
            "FAILED tests/x.py::TestA::test_b - AssertionError: nope\n"
            "1 failed, 1527 passed in 141.11s (0:02:21)\n"
        )
        passed, failed, nodeids = rg._parse_pytest_summary(out)
        assert passed == 1527 and failed == 1
        # 实时进度残片 "[ 23%]" 必须被丢弃，只留真 nodeid
        assert nodeids == ["tests/x.py::TestA::test_b"]

    def test_pytest_summary_whole_file_failure_kept(self):
        out = "ERROR tests/broken.py\n1 error in 0.1s\n"
        _, _, nodeids = rg._parse_pytest_summary(out)
        assert nodeids == ["tests/broken.py"]

    def test_pytest_summary_all_green(self):
        passed, failed, nodeids = rg._parse_pytest_summary("42 passed in 3.1s")
        assert (passed, failed, nodeids) == (42, 0, [])

    def test_coverage_total_from_table(self):
        out = (
            "Name                 Stmts   Miss  Cover\n"
            "------------------------------------------\n"
            "TOTAL                 7718    377    95%\n"
        )
        assert rg._parse_coverage_total(out) == 95.0

    def test_coverage_total_from_single_number(self):
        assert rg._parse_coverage_total("95.115\n") == 95.115

    def test_coverage_total_missing(self):
        assert rg._parse_coverage_total("no data here") is None


# ── 结构检查 ────────────────────────────────────────────────────────────────


class TestStructuralChecks:
    def test_count_rule_checks_ast(self, tmp_path):
        (tmp_path / "a.py").write_text(
            "def _check_x():\n    pass\n"
            "async def _check_y():\n    pass\n"
            "def _helper():\n    pass\n",
            encoding="utf-8",
        )
        (tmp_path / "broken.py").write_text("def (:", encoding="utf-8")  # 语法坏 → 跳过
        assert rg.count_rule_checks(tmp_path) == 2

    def test_count_kb_entries_list_form(self, tmp_path):
        (tmp_path / "kb.json").write_text(
            json.dumps([{"id": 1}, {"id": 2}, {"id": 3}]), encoding="utf-8"
        )
        assert rg.count_kb_entries(tmp_path) == 3

    def test_count_kb_entries_dict_form(self, tmp_path):
        """`chapters` **不得**计入条目数（B4-5）。

        ⚠️ 本用例曾断言 `== 7`（5 entries + 2 chapters）—— 那是在**锁死缺陷**：
        chapters 是无正文、检索器不索引的**章节标题元数据**，算进语料会让
        门禁数字虚高（实测 477 vs 真值 441）。现改为断言两个口径**分列**。
        """
        (tmp_path / "kb.json").write_text(
            json.dumps({"entries": [{}] * 5, "chapters": [{}] * 2}), encoding="utf-8"
        )
        counts = rg.count_kb_corpus(tmp_path)
        assert counts.entries == 5 and counts.chapters == 2
        assert rg.count_kb_entries(tmp_path) == 5      # 仅可检索条目
        assert rg.count_kb_entries(tmp_path) != 7      # 反向：绝不是相加

    def test_kb_corpus_reports_both_numbers(self, tmp_path):
        """门禁 detail 必须**同时**给出两个数字（否则口径失真无法察觉）。"""
        (tmp_path / "kb.json").write_text(
            json.dumps({"entries": [{}] * 10, "chapters": [{}] * 3}), encoding="utf-8"
        )
        r = rg.check_kb_corpus(min_entries=5, kb_dir=tmp_path)
        assert r.status == rg.PASS
        assert "10 条" in r.detail and "3 条" in r.detail

    def test_kb_floor_uses_entries_not_chapters(self, tmp_path):
        """下限只对**可检索条目**生效 —— 章节再多也不能把语料"凑够"。"""
        (tmp_path / "kb.json").write_text(
            json.dumps({"entries": [{}] * 2, "chapters": [{}] * 50}), encoding="utf-8"
        )
        assert rg.check_kb_corpus(min_entries=10, kb_dir=tmp_path).status == rg.FAIL

    def test_gate_count_equals_retriever_count(self):
        """**最强不变式**：门禁报的数字必须 == 检索器真正能取到的条目数。

        这是**派生**断言（不手写 441）—— 语料扩/缩时它自动跟随，
        而"又把 chapters 加回去"会让它立刻变红。两端都是**真实**实现：
        门禁侧 :func:`count_kb_entries`，检索侧 `core.kb.store.entries()`。
        """
        from core.kb import store

        assert rg.count_kb_entries() == len(store.entries())

    def test_count_kb_entries_bad_json_skipped(self, tmp_path):
        (tmp_path / "kb.json").write_text("{not json", encoding="utf-8")
        assert rg.count_kb_entries(tmp_path) == 0

    def test_packaging_files_missing(self, tmp_path):
        r = rg.check_packaging_files(tmp_path)
        assert r.status == rg.FAIL and "pbc-server.spec" in r.detail

    def test_packaging_files_all_present(self, tmp_path):
        for f in rg.PACKAGING_FILES:
            p = tmp_path / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        assert rg.check_packaging_files(tmp_path).status == rg.PASS

    def test_rules_wired_below_floor(self, tmp_path):
        (tmp_path / "a.py").write_text("def _check_a():\n    pass\n", encoding="utf-8")
        assert rg.check_rules_wired(min_rules=14, rules_dir=tmp_path).status == rg.FAIL
        assert rg.count_rule_checks(tmp_path) == 1

    def test_kb_corpus_floor(self, tmp_path):
        (tmp_path / "kb.json").write_text(json.dumps([{}] * 10), encoding="utf-8")
        assert rg.check_kb_corpus(min_entries=200, kb_dir=tmp_path).status == rg.FAIL
        assert rg.check_kb_corpus(min_entries=5, kb_dir=tmp_path).status == rg.PASS


class TestKbPackaging:
    """kb_packaging：每个 KB 源都必须随包分发（防多源漏包漂移）。"""

    @staticmethod
    def _kb(tmp_path, names):
        d = tmp_path / "kb"
        d.mkdir()
        for n in names:
            (d / n).write_text("{}", encoding="utf-8")
        return d

    def test_glob_in_spec_passes(self, tmp_path):
        kb = self._kb(tmp_path, ["a.json", "b.json", "c.json"])
        spec = tmp_path / "x.spec"
        spec.write_text(
            'datas = [\n'
            '    *[(str(p), "core/kb/data")\n'
            '      for p in sorted((_ROOT / "core" / "kb" / "data").glob("*.json"))],\n'
            ']\n', encoding="utf-8")
        r = rg.check_kb_packaging(spec_path=spec, kb_dir=kb)
        assert r.status == rg.PASS and "glob" in r.detail

    def test_explicit_list_covering_all_passes(self, tmp_path):
        kb = self._kb(tmp_path, ["a.json", "b.json"])
        spec = tmp_path / "x.spec"
        spec.write_text(
            'datas = [("core/kb/data/a.json", "core/kb/data"),'
            ' ("core/kb/data/b.json", "core/kb/data")]\n', encoding="utf-8")
        assert rg.check_kb_packaging(spec_path=spec, kb_dir=kb).status == rg.PASS

    def test_single_source_spec_fails_on_missing(self, tmp_path):
        """回归：单源 spec（只列 gmp2010）在多源下必须 FAIL。"""
        kb = self._kb(tmp_path, ["gmp2010.json", "alcoa_plus.json"])
        spec = tmp_path / "x.spec"
        spec.write_text(
            'datas = [("core/kb/data/gmp2010.json", "core/kb/data")]\n',
            encoding="utf-8")
        r = rg.check_kb_packaging(spec_path=spec, kb_dir=kb)
        assert r.status == rg.FAIL and "alcoa_plus.json" in r.detail

    def test_empty_kb_dir_fails(self, tmp_path):
        kb = tmp_path / "kb"
        kb.mkdir()
        spec = tmp_path / "x.spec"
        spec.write_text("glob('*.json')\n", encoding="utf-8")
        assert rg.check_kb_packaging(spec_path=spec, kb_dir=kb).status == rg.FAIL

    def test_missing_spec_fails(self, tmp_path):
        kb = self._kb(tmp_path, ["a.json"])
        assert rg.check_kb_packaging(
            spec_path=tmp_path / "nope.spec", kb_dir=kb).status == rg.FAIL

    def test_real_spec_covers_real_corpus(self):
        """实际仓库的 spec 必须覆盖实际的 6 个语料源（不 mock）。"""
        r = rg.check_kb_packaging()
        assert r.status == rg.PASS, r.detail


# ── 编排 / 报告 ─────────────────────────────────────────────────────────────


class TestOrchestration:
    def test_run_all_skip_tests(self, monkeypatch):
        monkeypatch.setattr(rg, "check_worktree_clean",
                            lambda: rg.CheckResult("worktree_clean", rg.PASS, "ok"))
        results = rg.run_all(skip_tests=True)
        names = [r.name for r in results]
        # 契约变更（2026-09-17，仓库卫生）：编排新增两项生成物检查，
        # no_build_outputs（FAIL，拦 `git add -f` 产物）与 dist_variants（WARN，
        # 提醒收敛 dist* 变体）。二者都属"工作区状态"，故紧随 worktree_clean。
        # 契约变更（2026-09-20，B7-3）：新增 artifact_freshness（产物新鲜度），
        # 紧随 dist_variants —— 同为"产物/工作区状态"，且必须在 tests_coverage 之前
        # （否则"测了一堆却发了个陈旧产物"仍会看起来全绿）。
        assert names == ["worktree_clean", "no_build_outputs", "dist_variants",
                         "artifact_freshness", "packaging_files", "rules_wired",
                         "kb_corpus", "kb_packaging", "tests_coverage"]
        assert results[-1].status == rg.SKIP
        # 结构检查必须能在**不跑测试**时给出（提交前的秒级检查路径）
        assert all(r.status != rg.SKIP for r in results[:-1])

    def test_build_report_overall_fail_when_any_fail(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (0, "abc1234"))
        results = [
            rg.CheckResult("a", rg.PASS, "ok"),
            rg.CheckResult("b", rg.FAIL, "boom"),
        ]
        rep = rg.build_report(results, fail_under=95)
        assert rep["overall"] == rg.FAIL
        assert rep["counts"] == {"pass": 1, "fail": 1, "warn": 0, "skip": 0}
        assert rep["coverage_gate"] == 95
        assert rep["git_head"] == "abc1234"

    def test_build_report_overall_pass(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (0, "deadbee"))
        rep = rg.build_report([rg.CheckResult("a", rg.WARN, "w")], fail_under=95)
        assert rep["overall"] == rg.PASS and rep["git_head"] == "deadbee"

    def test_write_report_creates_file(self, tmp_path):
        out = tmp_path / "nested" / "gate.json"
        rep = {"overall": "pass", "counts": {}, "checks": []}
        got = rg.write_report(rep, out)
        assert got == out and out.exists()
        assert json.loads(out.read_text(encoding="utf-8"))["overall"] == "pass"

    def test_check_worktree_clean_fail_on_dirty(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd",
                            lambda *a, **k: (0, "?? untracked.py\n M api/x.py\n"))
        r = rg.check_worktree_clean()
        assert r.status == rg.FAIL and "2 处" in r.detail

    def test_check_worktree_clean_pass_on_clean(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (0, ""))
        assert rg.check_worktree_clean().status == rg.PASS

    def test_check_worktree_clean_warn_on_git_error(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (128, "not a repo"))
        assert rg.check_worktree_clean().status == rg.WARN


# ── T0：junitxml 事实源 ──────────────────────────────────────────────────────


_XML_ALL_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3">
<testcase classname="tests.integration.test_main_routes" name="test_a" time="0.1" />
<testcase classname="tests.integration.test_main_routes.TestServePdf" name="test_b" time="0.1" />
</testsuite></testsuites>"""

_XML_WITH_FAILURES = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="tests.integration.test_main_routes.TestServePdf" name="test_pdf_non_local_host_returns_403" time="0.2">
  <failure message="SystemExit: 1">tb</failure>
</testcase>
<testcase classname="tests.unit.test_x" name="test_broken" time="0.1">
  <error message="ImportError">boom</error>
</testcase>
<testcase classname="tests.unit.test_x" name="test_ok" time="0.1" />
<testcase classname="tests.unit.test_x" name="test_skip" time="0.1"><skipped message="env" /></testcase>
</testsuite></testsuites>"""


_XML_ENV_ONLY = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="3">
<testcase classname="tests.integration.test_main_routes.TestServePdf" name="test_pdf_non_local_host_returns_403" time="0.2">
  <failure message="SystemExit: 1">finally: pdf_path.unlink(...)
  [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":847,"threshold":50,"scope":"turn"}
  </failure>
</testcase>
<testcase classname="tests.unit.test_x" name="test_ok" time="0.1" />
<testcase classname="tests.unit.test_x" name="test_ok2" time="0.1" />
</testsuite></testsuites>"""

# 同一 nodeid、**同样的 SystemExit**，但正文里**没有** safe-delete 标记串 ——
# 这是"看起来像环境问题"的真缺陷（例如 shim 之外的 sys.exit）。必须照旧 FAIL：
# 前缀是必要不充分条件，判据要的是「SystemExit + 标记串」双命中。
_XML_ENV_ONLY_LOOKALIKE = _XML_ENV_ONLY.replace(
    '[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":847,"threshold":50,"scope":"turn"}',
    "SystemExit(1) raised by the code under test")


_XML_CONTAINER_SKIP = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="2">
    <testcase classname="" name="tests.unit.test_anchor_orientation_tool" time="0.000">
      <skipped message="collection skipped">("path", 22, "could not import 'numpy'")</skipped>
    </testcase>
    <testcase classname="tests.unit.test_release_gate" name="test_a" time="0.1" />
  </testsuite>
</testsuites>
"""

_XML_CASE_SKIP = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="1">
    <testcase classname="tests.unit.test_release_gate" name="test_a" time="0.1" />
    <testcase classname="tests.unit.test_release_gate" name="test_b" time="0.1">
      <skipped message="no artifact" />
    </testcase>
  </testsuite>
</testsuites>
"""


class TestContainerSkips:
    """收集阶段的整文件 skip（junit 里 `classname=""`）——"用例静默消失"的签名。

    实测（2026-09-16）：CI 缺 numpy → `test_anchor_orientation_tool.py` 整段被
    `pytest.importorskip` 跳掉，**27 条用例消失而门禁六项全绿**。这类失败不进
    failed、不进覆盖率，唯一能看见它的地方就是这条检查。
    """

    def _write(self, tmp_path, xml):
        p = tmp_path / "j.xml"
        p.write_text(xml, encoding="utf-8")
        return p

    def test_detects_empty_classname_skip(self, tmp_path):
        assert rg._container_skips(self._write(tmp_path, _XML_CONTAINER_SKIP)) == [
            "tests.unit.test_anchor_orientation_tool — collection skipped"]

    def test_ignores_case_level_skips(self, tmp_path):
        """用例级 skip（有 classname）是设计使然（artifact-gated 用例）→ 不得误判。"""
        assert rg._container_skips(self._write(tmp_path, _XML_CASE_SKIP)) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert rg._container_skips(tmp_path / "nope.xml") == []

    def test_error_entry_is_not_a_container_skip(self, tmp_path):
        """收集阶段的 `<error>` 已被 `_parse_junit` 计入 failed（本就 FAIL）→ 不重复报。"""
        xml = _XML_CONTAINER_SKIP.replace(
            '<skipped message="collection skipped">("path", 22, '
            '"could not import \'numpy\'")</skipped>',
            '<error message="collection failed">boom</error>')
        assert rg._container_skips(self._write(tmp_path, xml)) == []


class TestJunitParsing:
    def test_nodeid_module_level(self):
        assert rg._junit_nodeid("tests.integration.test_main_routes", "test_a") == \
            "tests/integration/test_main_routes.py::test_a"

    def test_nodeid_with_class(self):
        got = rg._junit_nodeid("tests.integration.test_main_routes.TestServePdf", "test_b")
        assert got == "tests/integration/test_main_routes.py::TestServePdf::test_b"

    def test_nodeid_matches_env_only_prefix(self):
        got = rg._junit_nodeid(
            "tests.integration.test_main_routes.TestServePdf",
            "test_pdf_non_local_host_returns_403",
        )
        assert got.startswith(rg.ENV_ONLY_FAILURE_PREFIXES)

    def test_nodeid_unresolvable_module_falls_back(self):
        got = rg._junit_nodeid("nonexistent.pkg.mod.Cls", "test_y")
        assert got == "nonexistent/pkg/mod/Cls.py::test_y"

    def test_parse_junit_all_pass(self, tmp_path):
        p = tmp_path / "j.xml"
        p.write_text(_XML_ALL_PASS, encoding="utf-8")
        passed, failed, nodeids = rg._parse_junit(p)
        assert (passed, failed, nodeids) == (3, 0, [])

    def test_parse_junit_failures_and_errors(self, tmp_path):
        p = tmp_path / "j.xml"
        p.write_text(_XML_WITH_FAILURES, encoding="utf-8")
        passed, failed, nodeids = rg._parse_junit(p)
        assert (passed, failed) == (1, 2)  # 4 - 2 fail - 1 skip
        assert nodeids == [
            "tests/integration/test_main_routes.py::TestServePdf::test_pdf_non_local_host_returns_403",
            "tests/unit/test_x.py::test_broken",
        ]

    def test_parse_junit_missing_returns_none(self, tmp_path):
        assert rg._parse_junit(tmp_path / "nope.xml") is None

    def test_parse_junit_corrupt_returns_none(self, tmp_path):
        p = tmp_path / "bad.xml"
        p.write_text("not xml <<<", encoding="utf-8")
        assert rg._parse_junit(p) is None


class TestDumpAndReportSurfacing:
    def test_dump_pytest_log_writes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rg, "REPO_ROOT", tmp_path)
        out = rg._dump_pytest_log("hello raw", "20260101_000000")
        assert out.exists() and out.read_text(encoding="utf-8") == "hello raw"
        assert out.name == "gate_pytest_20260101_000000.log"

    def test_build_report_surfaces_env_only_and_logs(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (0, "abc1234"))
        results = [
            rg.CheckResult("tests_coverage", rg.WARN, "w",
                           raw_log="devlogs/gate_pytest_x.log",
                           env_only=["tests/a.py::T::t"]),
        ]
        rep = rg.build_report(results, fail_under=95)
        assert rep["overall"] == rg.PASS
        assert rep["env_only_failures"] == ["tests/a.py::T::t"]
        assert rep["raw_logs"] == ["devlogs/gate_pytest_x.log"]

    def test_build_report_no_env_only(self, monkeypatch):
        monkeypatch.setattr(rg, "run_cmd", lambda *a, **k: (0, "abc1234"))
        rep = rg.build_report([rg.CheckResult("a", rg.PASS, "ok")], fail_under=95)
        assert rep["env_only_failures"] == [] and rep["raw_logs"] == []


def _fake_gate_run_cmd(*, junit_xml=None, pytest_out="", cov="95.12\n", rc=0):
    """伪造 run_cmd：pytest 调用按需写 junitxml；coverage report 返回覆盖率。"""
    def fake(cmd, **kwargs):
        if "pytest" in cmd:
            if junit_xml is not None:
                for a in cmd:
                    if str(a).startswith("--junitxml="):
                        Path(str(a).split("=", 1)[1]).write_text(junit_xml, encoding="utf-8")
            return rc, pytest_out
        if "report" in cmd:
            return 0, cov
        return 0, ""
    return fake


class TestTestsCoverageCheck:
    def _patch(self, monkeypatch, tmp_path, **kw):
        monkeypatch.setattr(rg.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(rg, "run_cmd", _fake_gate_run_cmd(**kw))
        # 不落真实 devlogs/
        monkeypatch.setattr(rg, "_dump_pytest_log",
                            lambda raw, stamp=None: tmp_path / "raw.log")
        # junit 审计副本同样不落真实 devlogs/（与上一条分开注入：`_audit_dir`
        # 不能被 REPO_ROOT 的替换覆盖 —— `_junit_nodeid` 还要用真 REPO_ROOT 定位 .py）
        monkeypatch.setattr(rg, "_audit_dir", lambda: tmp_path / "devlogs")

    def test_mixed_failures_real_one_wins(self, monkeypatch, tmp_path):
        # 同时含真实失败与 env-only → 真实失败优先，判 FAIL，且 raw_log 已落盘
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_WITH_FAILURES)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.FAIL and r.raw_log.endswith("raw.log")

    def test_env_only_only_is_warn_with_annotation(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_ENV_ONLY)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.WARN
        assert r.env_only == [
            "tests/integration/test_main_routes.py::TestServePdf::test_pdf_non_local_host_returns_403"
        ]
        assert "allowlist" in r.detail

    def test_env_only_prefix_without_signature_still_fails(self, monkeypatch, tmp_path):
        """**护栏漏洞的回归用例**：nodeid 前缀在 allowlist、但失败**不是**沙箱
        safe-delete 引起 ⇒ 必须 FAIL，不得降级 WARN。

        （实测同形态：`TestServePdf` 里 403 变成 200 也会落在这个前缀下。）
        """
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_ENV_ONLY_LOOKALIKE)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.FAIL, "前缀相同但无 safe-delete 签名 ⇒ 必须 FAIL"
        assert r.env_only == []

    def test_junit_missing_falls_back_to_stdout(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=None,
                    pytest_out="FAILED tests/x.py::T::t - boom\n1 failed, 5 passed\n")
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.FAIL and "fact=stdout" in r.detail

    def test_all_green_passes(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_ALL_PASS)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.PASS and "fact=junitxml" in r.detail

    def test_coverage_below_gate_fails(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_ALL_PASS, cov="94.10\n")
        assert rg.check_tests_and_coverage(python="py").status == rg.FAIL

    def test_pytest_could_not_complete(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=None, pytest_out="", rc=3)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.FAIL and "未能完成" in r.detail

    def test_container_skip_makes_the_check_fail(self, monkeypatch, tmp_path):
        """整文件被收集阶段跳过 → FAIL（否则"用例消失"永远查不出来）。"""
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_CONTAINER_SKIP)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.FAIL and "整文件被跳过" in r.detail

    def test_case_level_skip_still_passes(self, monkeypatch, tmp_path):
        """对照组：用例级 skip 不得触发上面那条 FAIL（artifact-gated 用例靠它）。"""
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_CASE_SKIP)
        r = rg.check_tests_and_coverage(python="py")
        assert r.status == rg.PASS


class TestJunitAuditTrail:
    """junit 的**可审计副本**（T0.5）。

    背景：门禁跑 pytest 带 `-o addopts=-q` → stdout 只有点和汇总，**没有逐条 nodeid**；
    junit XML 才是逐条事实源，但它原先落在系统临时目录（CI 上随 runner 消失，也不在
    artifact 列表里）。于是"CI 上到底跑了哪些用例"无从查证 —— 实测踩过：44 条差异只
    能钉死 17 条 skip。这个类锁住"副本必须落进 devlogs/ 且 CI 必须上传它"。
    """

    def _patch(self, monkeypatch, tmp_path, **kw):
        monkeypatch.setattr(rg.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(rg, "run_cmd", _fake_gate_run_cmd(**kw))
        monkeypatch.setattr(rg, "_audit_dir", lambda: tmp_path / "devlogs")

    def test_junit_audit_copy_is_written_verbatim(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_ALL_PASS)
        r = rg.check_tests_and_coverage(python="py")
        audit = sorted((tmp_path / "devlogs").glob("gate_junit_*.xml"))
        assert len(audit) == 1, "junit 审计副本未落盘 → CI 上无法逐条核对用例"
        assert audit[0].read_text(encoding="utf-8") == _XML_ALL_PASS
        assert "junit=" in r.detail, "报告里应带上副本路径，便于从 CI 摘要直达"

    def test_audit_copy_carries_testcase_level_detail(self, monkeypatch, tmp_path):
        """副本必须保留 testcase 级信息 —— 那正是 stdout 日志缺的东西。"""
        self._patch(monkeypatch, tmp_path, junit_xml=_XML_WITH_FAILURES)
        rg.check_tests_and_coverage(python="py")
        audit = next((tmp_path / "devlogs").glob("gate_junit_*.xml"))
        parsed = rg._parse_junit(audit)
        assert parsed is not None, "副本必须仍可被门禁自己的解析器读取"
        _, _, nodeids = parsed
        assert nodeids, "副本里应能解析出逐条 nodeid"

    def test_no_audit_copy_when_xml_never_written(self, monkeypatch, tmp_path):
        """junit 缺失（回落 stdout 解析）时不应凭空造目录或假副本。"""
        self._patch(monkeypatch, tmp_path, junit_xml=None, pytest_out="1 passed\n")
        r = rg.check_tests_and_coverage(python="py")
        assert not (tmp_path / "devlogs").exists()
        assert "junit=" not in r.detail

    def test_ci_uploads_the_junit_audit_file(self):
        """**跨文件契约**：门禁落盘的 junit 必须在 CI artifact 的上传列表里。

        少了任一半，门禁写出来的副本都传不出 CI —— 盲区原样保留。
        同时锁 `always()`：一绿就不上传，等价于没有。
        """
        ci = (rg.REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        assert "devlogs/gate_junit_*.xml" in ci, "CI 未上传 junit 审计副本"
        assert "if: always()" in ci, "上传步骤必须无条件执行，否则绿了就丢日志"


class TestSandboxDeleteIsolation:
    """B7-1：门禁在**单个工具调用**里跑整套测试 ⇒ 必撞沙箱「每轮批量删除预算」。

    实测错误原样：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
    {"count":847,"threshold":50,"scope":"turn"}` —— 沙箱策略，不是应用缺陷，
    却会让门禁**假红**（同一份代码去掉该守卫 ⇒ 2801 passed/0 failed）。
    这里锁住「子进程拿到的是**放宽后**的阈值」，并且是**真的注入到子进程**里
    （跑一个真子进程打印它自己的环境变量），不是 mock 出来的假绿。
    """

    _PROBE = ("import os;print(os.environ.get("
              "'CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD',''))")
    _PROBE_OTHER = "import os;print(os.environ.get('PBC_GATE_PROBE',''))"

    def test_child_receives_raised_threshold(self):
        rc, out = rg.run_cmd([sys.executable, "-c", self._PROBE])
        assert rc == 0
        assert out.strip() == str(rg.SANDBOX_BULK_DELETE_THRESHOLD)

    def test_threshold_far_above_the_measured_peak(self):
        """"抬高"必须真的高过实测量级（峰值 847），否则是形式主义。"""
        assert rg.SANDBOX_BULK_DELETE_THRESHOLD > 847 * 10

    def test_explicit_env_override_still_applies(self):
        """调用方显式传的 env 仍生效 —— 放宽阈值不得顺手吞掉其它覆盖。"""
        rc, out = rg.run_cmd([sys.executable, "-c", self._PROBE_OTHER],
                             env={"PBC_GATE_PROBE": "ok"})
        assert rc == 0 and out.strip() == "ok"


class TestEnvOnlySignature:
    """"环境专有失败"的判据 = **前缀 + 签名**双命中（只按前缀是护栏漏洞）。

    2026-09-20 复核：`env_only` 原先只做 `nodeid.startswith(prefix)`，于是
    `TestServePdf` 里任何**真实回归**（403 变 200）都会被降级成 WARN、
    门禁退出码为 0 —— "护栏把真缺陷放行了"。
    """

    NODE = ("tests/integration/test_main_routes.py::TestServePdf"
            "::test_pdf_non_local_host_returns_403")
    _MARKED = ('SystemExit: 1\n[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] '
               '{"count":847,"threshold":50,"scope":"turn"}')

    def test_marker_plus_systemexit_is_env_only(self):
        assert rg._is_sandbox_delete_failure(self._MARKED)

    def test_systemexit_without_marker_is_not_env_only(self):
        assert not rg._is_sandbox_delete_failure(
            "SystemExit: 1\nAssertionError: assert 200 == 403")

    def test_marker_without_systemexit_is_not_env_only(self):
        assert not rg._is_sandbox_delete_failure(
            "safe-delete reported SAFE_DELETE_BULK_CONFIRM_REQUIRED in a log line")

    def test_no_signature_available_means_no_downgrade(self):
        """junit 缺失 ⇒ 取不到签名 ⇒ **不得**降级（fail-closed）。"""
        assert rg.env_only_nodeids([self.NODE], {}) == []

    def test_prefix_is_still_required(self):
        texts = {self.NODE: self._MARKED}
        assert rg.env_only_nodeids(["tests/unit/test_other.py::T::t"], texts) == []
        assert rg.env_only_nodeids([self.NODE], texts) == [self.NODE]

    def test_failure_texts_extraction(self, tmp_path):
        p = tmp_path / "j.xml"
        p.write_text(_XML_ENV_ONLY, encoding="utf-8")
        texts = rg._junit_failure_texts(p)
        assert "SAFE_DELETE_BULK_CONFIRM_REQUIRED" in texts[self.NODE]

    def test_failure_texts_extraction_survives_bad_xml(self, tmp_path):
        p = tmp_path / "bad.xml"
        p.write_text("not xml <<<", encoding="utf-8")
        assert rg._junit_failure_texts(p) == {}


class TestArtifactFreshness:
    """`check_artifact_freshness` 的**编排**语义（判据本身在 test_bundle_manifest 里测）。

    分层理由：判据（字节比对 / 集合覆盖 / asar）由 `bundle_manifest.verify_artifact`
    单测；本类只管门禁怎么把它变成 PASS/FAIL/SKIP、怎么归因、崩了怎么办。
    """

    def _artifact(self, root: Path, kind: str = "backend") -> Path:
        if kind == "backend":
            p = root / "dist" / "pbc-server"
        else:
            p = root / "dist-electron" / "win-unpacked" / "resources" / "pbc-server"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def test_no_artifact_is_skip_not_pass(self, tmp_path):
        """"没有产物"≠"产物新鲜" —— 不能判 PASS（那是把"判不了"读成"没问题"）。"""
        r = rg.check_artifact_freshness(root=tmp_path)
        assert r.status == rg.SKIP and "无构建产物" in r.detail

    def test_lagging_artifact_is_fail_with_attribution(self, tmp_path, monkeypatch):
        # 只取副作用（建出目录让 discover_artifacts 认得出），路径本身不参与断言 ——
        # 归因断言在下面用字面量 "dist/pbc-server" 表达。
        self._artifact(tmp_path)
        monkeypatch.setattr(rg, "verify_artifact",
                            lambda a, root: ["3 个产物副本与源码不一致（**产物陈旧**）"])
        r = rg.check_artifact_freshness(root=tmp_path)
        assert r.status == rg.FAIL
        assert "dist/pbc-server" in r.detail and "产物陈旧" in r.detail

    def test_synced_artifact_is_pass(self, tmp_path, monkeypatch):
        self._artifact(tmp_path)
        monkeypatch.setattr(rg, "verify_artifact", lambda a, root: [])
        r = rg.check_artifact_freshness(root=tmp_path)
        assert r.status == rg.PASS and "逐字节一致" in r.detail

    def test_every_artifact_is_checked_and_named(self, tmp_path, monkeypatch):
        """两份产物（后端 + electron 嵌入）**都要**查，且问题要能归因到具体哪一份。"""
        self._artifact(tmp_path, "backend")
        self._artifact(tmp_path, "embedded")

        def _fake(a, root):
            return ["boom"] if "win-unpacked" in str(a) else []

        monkeypatch.setattr(rg, "verify_artifact", _fake)
        r = rg.check_artifact_freshness(root=tmp_path)
        assert r.status == rg.FAIL
        assert "dist-electron/win-unpacked/resources/pbc-server" in r.detail
        assert "boom" in r.detail

    def test_verifier_crash_is_fail_closed(self, tmp_path, monkeypatch):
        """"判据自己崩了"绝不能被读成"没问题"。"""
        self._artifact(tmp_path)

        def _boom(a, root):
            raise RuntimeError("index out of range")

        monkeypatch.setattr(rg, "verify_artifact", _boom)
        r = rg.check_artifact_freshness(root=tmp_path)
        assert r.status == rg.FAIL and "校验器异常" in r.detail


class TestMainExitCode:
    def test_main_returns_1_when_fail(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rg, "run_all",
                            lambda **k: [rg.CheckResult("x", rg.FAIL, "boom")])
        monkeypatch.setattr(rg, "build_report",
                            lambda results, **k: {"overall": "fail", "counts": {"pass": 0, "fail": 1, "warn": 0, "skip": 0}, "checks": []})
        monkeypatch.setattr(rg, "write_report", lambda rep, out: tmp_path / "r.json")
        assert rg.main(["--skip-tests"]) == 1

    def test_main_returns_0_when_all_pass(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rg, "run_all",
                            lambda **k: [rg.CheckResult("x", rg.PASS, "ok")])
        monkeypatch.setattr(rg, "build_report",
                            lambda results, **k: {"overall": "pass", "counts": {"pass": 1, "fail": 0, "warn": 0, "skip": 0}, "checks": []})
        monkeypatch.setattr(rg, "write_report", lambda rep, out: tmp_path / "r.json")
        assert rg.main(["--skip-tests"]) == 0


# ── 非 UTF-8 控制台（CI 实测回归）───────────────────────────────────────────


class TestNonUtf8Console:
    """`release_gate.py` 打印中文报告，必须在非 UTF-8 控制台下也能跑完。

    2026-09-15 CI run #1（GitHub Actions `windows-latest`，控制台编码 **cp1252**）：
    门禁 6 项检查全过、报告已落盘，却死在 `_print_report` 的**第一个** `print` ——
    中文 + 制表符 `UnicodeEncodeError` → 退出码 1 → CI 直接红。
    本机**永远复现不了**（本地代码页能显示中文），所以只能靠"子进程 + 显式非 UTF-8
    编码"把它固化成机检。
    """

    def _run(self, tmp_path, io_encoding: str):
        env = {**os.environ, "PYTHONIOENCODING": io_encoding}
        # --skip-tests：只跑秒级结构检查，不启动 5 分钟的 pytest 子进程
        return subprocess.run(
            [sys.executable, str(_RG_PATH), "--skip-tests",
             "--out", str(tmp_path / "r.json")],
            cwd=str(_RG_PATH.parents[1]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=300,
        )

    def test_report_prints_under_cp1252_console(self, tmp_path):
        proc = self._run(tmp_path, "cp1252")
        combined = proc.stdout + proc.stderr
        assert "UnicodeEncodeError" not in combined, combined[-900:]
        assert "Traceback" not in combined, combined[-900:]
        # 报告本身必须真的打出来 —— "没崩但什么都没输出"同样是失败（空转不算过）
        assert "OVERALL" in proc.stdout, combined[-900:]
