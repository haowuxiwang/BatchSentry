"""M2 评测层单测：金标打分 / 汇总 / 校验 + 语料与金标一致性。

`scripts/` 非包 → importlib 按路径加载。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_EVAL = _REPO / "scripts" / "eval_findings.py"
_CORPUS = _REPO / "scripts" / "synthetic_corpus.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


ef = _load("eval_findings", _EVAL)
sc = _load("synthetic_corpus", _CORPUS)


# ── 合成语料 ────────────────────────────────────────────────────────────────


class TestSyntheticCorpus:
    def test_build_cases_deterministic(self):
        assert sc.build_cases() == sc.build_cases()

    def test_each_case_is_page_structures(self):
        for cid, pages in sc.build_cases().items():
            assert pages, cid
            for p in pages:
                assert set(p) >= {"page", "data"}
                assert isinstance(p["data"].get("steps"), list)


# ── 金标打分（纯函数）──────────────────────────────────────────────────────


class TestScoreCase:
    NOISE = {"completeness"}

    def test_perfect(self):
        case = {"case_id": "c", "expect": [{"page": 1, "type": "time_reversal"}]}
        got = ef.score_case(case, [{"page": 1, "type": "time_reversal"}], self.NOISE)
        assert got["tp"] == [[1, "time_reversal"]] and not got["fp"] and not got["fn"]
        assert got["precision"] == 1.0 and got["recall"] == 1.0

    def test_miss_is_fn(self):
        case = {"case_id": "c", "expect": [{"page": 1, "type": "time_reversal"}]}
        got = ef.score_case(case, [], self.NOISE)
        assert got["fn"] == [[1, "time_reversal"]]
        assert got["recall"] == 0.0

    def test_unexpected_is_fp(self):
        case = {"case_id": "c", "expect": []}
        got = ef.score_case(case, [{"page": 2, "type": "param_out_of_spec"}], self.NOISE)
        assert got["fp"] == [[2, "param_out_of_spec"]]
        assert got["precision"] == 0.0

    def test_allowed_extra_type_not_fp(self):
        case = {"case_id": "c", "expect": [], "allowed_extra_types": ["completeness"]}
        got = ef.score_case(case, [{"page": 1, "type": "completeness"}], self.NOISE)
        assert got["fp"] == [] and got["noise"] == 1

    def test_noise_counted_singly(self):
        case = {"case_id": "c", "expect": [], "allowed_extra_types": ["completeness"]}
        got = ef.score_case(
            case,
            [{"page": 1, "type": "completeness"}, {"page": 2, "type": "completeness"}],
            self.NOISE,
        )
        assert got["noise"] == 2 and got["fp"] == []

    def test_no_expected_no_produced_gives_none_rates(self):
        got = ef.score_case({"case_id": "c", "expect": []}, [], self.NOISE)
        assert got["precision"] is None and got["recall"] is None


class TestAggregate:
    def test_excludes_known_gap(self):
        scored = [
            {"known_gap": True, "tp": [], "fp": [[1, "x"]], "fn": [[1, "y"]],
             "n_findings": 1, "noise": 1},
            {"known_gap": False, "tp": [[1, "a"]], "fp": [], "fn": [],
             "n_findings": 2, "noise": 0},
        ]
        agg = ef.aggregate(scored)
        assert agg["cases_scored"] == 1 and agg["cases_known_gap"] == 1
        assert agg["tp"] == 1 and agg["fp"] == 0 and agg["fn"] == 0
        assert agg["precision"] == 1.0 and agg["recall"] == 1.0 and agg["f1"] == 1.0

    def test_micro_pooling_and_f1(self):
        scored = [
            {"known_gap": False, "tp": [[1, "a"], [2, "b"]], "fp": [[3, "c"]],
             "fn": [[4, "d"]], "n_findings": 3, "noise": 1},
        ]
        agg = ef.aggregate(scored)
        assert agg["tp"] == 2 and agg["fp"] == 1 and agg["fn"] == 1
        assert agg["precision"] == round(2 / 3, 4)
        assert agg["recall"] == round(2 / 3, 4)

    def test_all_known_gap_yields_none_f1(self):
        agg = ef.aggregate([{"known_gap": True, "tp": [], "fp": [], "fn": [],
                             "n_findings": 0, "noise": 0}])
        assert agg["f1"] is None

    def test_noise_ratio(self):
        scored = [{"known_gap": False, "tp": [], "fp": [], "fn": [],
                   "n_findings": 4, "noise": 3}]
        assert ef.aggregate(scored)["noise_ratio"] == 0.75


class TestSafeDiv:
    def test_zero_denominator(self):
        assert ef._safe_div(0, 0) is None

    def test_rounding(self):
        assert ef._safe_div(1, 3) == 0.3333


# ── 金标 / 语料一致性 ──────────────────────────────────────────────────────


class TestGroundTruthContract:
    def test_load_and_shape(self):
        gt = ef.load_ground_truth()
        assert gt["schema_version"] == 1
        assert isinstance(gt["noise_types"], list) and gt["noise_types"]
        assert gt["cases"]

    def test_case_ids_match_corpus(self):
        gt = ef.load_ground_truth()
        corpus = sc.build_cases()
        ef.validate_cases(gt, corpus)  # 不抛即通过

    def test_mismatch_raises(self):
        gt = {"cases": [{"case_id": "a"}]}
        with pytest.raises(RuntimeError, match="不一致"):
            ef.validate_cases(gt, {"b": []})

    def test_real_baseline_present_but_not_truth(self):
        gt = ef.load_ground_truth()
        rb = gt["real_baselines"]
        assert rb["truth"] is False and rb["jobs"]


# ── 端到端：规则层跑通且基线可复现 ─────────────────────────────────────────


def test_evaluate_end_to_end_baseline():
    """跑全语料（离线规则层），断言基线达标且 known_gap 被正确排除。"""
    report = ef.evaluate()
    s = report["summary"]
    assert s["cases_known_gap"] == 1
    assert s["fp"] == 0 and s["fn"] == 0
    assert s["precision"] == 1.0 and s["recall"] == 1.0 and s["f1"] == 1.0
    assert s["noise_findings"] > 0
