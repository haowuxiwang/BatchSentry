"""scripts/release_gate.py 单测 —— 纯函数与编排（M1c）。

`scripts/` 非包 → 用 importlib 从文件路径加载，避免污染 sys.path。
不触发真实 pytest 子进程（check_tests_and_coverage 的子进程路径由
run_cmd 的解析辅助函数单独做纯逻辑验证）。
"""
from __future__ import annotations

import importlib.util
import json
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
        (tmp_path / "kb.json").write_text(
            json.dumps({"entries": [{}] * 5, "chapters": [{}] * 2}), encoding="utf-8"
        )
        assert rg.count_kb_entries(tmp_path) == 7

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


# ── 编排 / 报告 ─────────────────────────────────────────────────────────────


class TestOrchestration:
    def test_run_all_skip_tests(self, monkeypatch):
        monkeypatch.setattr(rg, "check_worktree_clean",
                            lambda: rg.CheckResult("worktree_clean", rg.PASS, "ok"))
        results = rg.run_all(skip_tests=True)
        names = [r.name for r in results]
        assert names == ["worktree_clean", "packaging_files", "rules_wired",
                         "kb_corpus", "tests_coverage"]
        assert results[-1].status == rg.SKIP

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
