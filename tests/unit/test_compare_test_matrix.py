"""`scripts/compare_test_matrix.py` 单测。

它的存在意义是**把"CI 与本地差哪些用例"变成一条命令**（见
`docs/ADVERSARIAL_AUDIT.md` §13.2：此前那 44 条差异只能钉死 17 条）。故除了功能正确性，
还要锁住"口径唯一"——nodeid 还原必须复用 `scripts/release_gate.py::_junit_nodeid`，
不许另起一套（本项目的既定纪律：同一事实只有一处真值）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CTM_PATH = Path(__file__).resolve().parents[2] / "scripts" / "compare_test_matrix.py"


def _load_ctm():
    spec = importlib.util.spec_from_file_location("compare_test_matrix", _CTM_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ctm = _load_ctm()

# 用**真实存在**的文件（`tests/unit/test_release_gate.py`）：`_junit_nodeid` 靠
# "最长且真实存在的 .py 前缀"定位文件，虚构路径会走兜底分支，测不到真实口径。
_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="4" failures="1" errors="0" skipped="1">
    <testcase classname="tests.unit.test_release_gate" name="test_a" time="0.1" />
    <testcase classname="tests.unit.test_release_gate.TestJunitParsing" name="test_b" time="0.2">
      <skipped message="no artifact" />
    </testcase>
    <testcase classname="tests.unit.test_release_gate" name="test_c" time="0.3">
      <failure message="boom">traceback</failure>
    </testcase>
    <testcase classname="tests.unit.test_release_gate" name="test_d" time="0.4">
      <error message="err" />
    </testcase>
  </testsuite>
</testsuites>
"""


class TestParseCollect:
    def test_extracts_nodeids(self):
        text = "tests/unit/test_a.py::test_x\ntests/unit/test_a.py::TestC::test_y\n"
        assert ctm.parse_collect(text) == {
            "tests/unit/test_a.py::test_x",
            "tests/unit/test_a.py::TestC::test_y",
        }

    def test_ignores_summary_lines(self):
        text = ("tests/unit/test_a.py::test_x\n"
                "2362 tests collected in 12.34s\n"
                "[100%]\n\n")
        assert ctm.parse_collect(text) == {"tests/unit/test_a.py::test_x"}

    def test_handles_crlf_and_backslashes(self):
        """实测踩过：Windows 上 `--collect-only` 输出是 **CRLF**，裸 `grep`/`comm` 会失真。"""
        text = "tests\\unit\\test_a.py::test_x\r\n"
        assert ctm.parse_collect(text) == {"tests/unit/test_a.py::test_x"}

    def test_empty_input(self):
        assert ctm.parse_collect("") == set()


class TestParseJunit:
    def _parse(self, tmp_path):
        p = tmp_path / "j.xml"
        p.write_text(_XML, encoding="utf-8")
        return ctm.parse_junit(p, ctm._load_release_gate())

    def test_nodeids_match_release_gate_mapping(self, tmp_path):
        ids, _ = self._parse(tmp_path)
        assert ids == {
            "tests/unit/test_release_gate.py::test_a",
            "tests/unit/test_release_gate.py::TestJunitParsing::test_b",
            "tests/unit/test_release_gate.py::test_c",
            "tests/unit/test_release_gate.py::test_d",
        }

    def test_counts_skipped_failed_passed(self, tmp_path):
        _, counts = self._parse(tmp_path)
        assert counts == {"passed": 1, "skipped": 1, "failed": 2}

    def test_skipped_cases_are_still_listed(self, tmp_path):
        """skip 的用例**在 junit 里**（`<skipped/>`）—— 所以"只被本地收集"才是
        "CI 上不存在"；把它误当成"没跑"会得出错误结论。"""
        ids, counts = self._parse(tmp_path)
        assert "tests/unit/test_release_gate.py::TestJunitParsing::test_b" in ids
        assert counts["skipped"] == 1


class TestDiffAndGrouping:
    def test_diff_returns_both_directions(self):
        only_local, only_ci = ctm.diff_matrices(
            {"a.py::t1", "a.py::t2"}, {"a.py::t2", "b.py::t3"})
        assert only_local == {"a.py::t1"}
        assert only_ci == {"b.py::t3"}

    def test_diff_identical_is_empty(self):
        s = {"a.py::t1"}
        assert ctm.diff_matrices(s, s) == (set(), set())

    def test_group_by_file_counts_per_file(self):
        grouped = ctm.group_by_file({"a.py::t1", "a.py::t2", "b.py::t3"})
        assert grouped == {"a.py": ["a.py::t1", "a.py::t2"], "b.py": ["b.py::t3"]}

    def test_group_by_file_is_sorted(self):
        grouped = ctm.group_by_file({"a.py::t2", "a.py::t1"})
        assert grouped["a.py"] == ["a.py::t1", "a.py::t2"]


class TestMainExitCode:
    def test_returns_1_when_matrices_differ(self, tmp_path):
        xml = tmp_path / "j.xml"
        xml.write_text(_XML, encoding="utf-8")
        collect = tmp_path / "c.txt"
        collect.write_text("tests/unit/test_release_gate.py::test_zzz\n",
                           encoding="utf-8")
        assert ctm.main(["--ci-junit", str(xml),
                         "--local-collect", str(collect)]) == 1

    def test_returns_0_when_matrices_match(self, tmp_path):
        xml = tmp_path / "j.xml"
        xml.write_text(_XML, encoding="utf-8")
        collect = tmp_path / "c.txt"
        collect.write_text(
            "\n".join([
                "tests/unit/test_release_gate.py::test_a",
                "tests/unit/test_release_gate.py::TestJunitParsing::test_b",
                "tests/unit/test_release_gate.py::test_c",
                "tests/unit/test_release_gate.py::test_d",
            ]) + "\n",
            encoding="utf-8")
        assert ctm.main(["--ci-junit", str(xml),
                         "--local-collect", str(collect)]) == 0


class TestNodeidMappingIsSingleSourced:
    """跨文件契约：口径必须复用门禁那一份实现，不得复制。"""

    def test_script_loads_release_gate_for_nodeid_mapping(self):
        src = _CTM_PATH.read_text(encoding="utf-8")
        assert "release_gate.py" in src, "应加载门禁模块以复用 nodeid 还原口径"

    def test_script_does_not_reimplement_nodeid_mapping(self):
        src = _CTM_PATH.read_text(encoding="utf-8")
        assert "def _junit_nodeid" not in src, \
            "不得在本脚本里重复实现 nodeid 还原（同一事实只能有一处真值）"

    def test_repo_root_points_at_repository(self):
        assert (ctm._REPO_ROOT / "scripts" / "release_gate.py").exists()
