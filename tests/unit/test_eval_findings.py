"""M2 评测层单测：金标打分 / 汇总 / 校验 + 语料与金标一致性。

`scripts/` 非包 → importlib 按路径加载。
"""
from __future__ import annotations

import ast
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


class TestRealBaselineContract:
    """`real_baselines` 是**惰性数据**：不进判据，且数字自带可比性 / 非确定披露。

    为什么锁这条（R77 第十七批，报告 §6 R-B / 待办 **0-15**）：该块的
    `total_findings: 784` 是**历史版本 + `mineru`** 的实测，而当前版本 + `paddle`
    对**同一份**文件实测只有 **208** 条；且真实 job 计数**非确定**（同输入 / 同产物 /
    同后端两次跑 **41 vs 51**）。该块**没有任何代码消费者**（判据只走合成语料的 F1），
    但 JSON 的 `description` 自称"金标"、块的 `note` 自称"回归对照"
    ⇒ 一个照着 784 去"修回归"的人会白干几天。
    """

    def _rb(self) -> dict:
        return ef.load_ground_truth()["real_baselines"]

    def test_block_declares_it_is_not_a_criterion(self):
        rb = self._rb()
        assert rb.get("criterion") is False, (
            "real_baselines 必须**显式**声明 `criterion: false` —— 缺了它，这一块读起来就像阈值"
        )
        assert rb.get("criterion_note"), "必须写明'为什么不是判据'，否则下一个人还会去比"

    def test_criterion_path_never_reads_real_baselines(self):
        """**唯一判据是合成语料的 F1** —— 判据路径不得引用 `real_baselines`。

        用 **AST 查名字**（不是文本搜）：注释 / docstring 里提到它不算消费。
        为什么必须挡：真实 job 计数**非确定**（同输入两次跑差 >20%）⇒ 一旦接进
        pass/fail，门禁会**随机红 / 绿**，而"随机红"会被当成"产品回归"去修。
        """
        tree = ast.parse(_EVAL.read_text(encoding="utf-8"))
        judge_fns = {"evaluate", "aggregate", "score_case", "validate_cases",
                     "load_ground_truth", "main"}
        consumers = []
        for fn in (n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name in judge_fns):
            refs = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            refs |= {n.value for n in ast.walk(fn)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            if "real_baselines" in refs:
                consumers.append(fn.name)
        assert not consumers, (
            f"判据路径引用了 real_baselines：{consumers} —— 真实 job 计数**非确定**，"
            "把它接进 pass/fail 会让门禁随机红 / 绿"
        )

    def test_consumer_detector_is_not_vacuous(self):
        """防空转：给一段**确实**消费它的源码，检测器必须报出来。"""
        bad = "def evaluate():\n    gt = load()\n    return gt['real_baselines']\n"
        parsed = ast.parse(bad)
        refs = {n.id for n in ast.walk(parsed) if isinstance(n, ast.Name)}
        refs |= {n.value for n in ast.walk(parsed)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "real_baselines" in refs, "检测器连构造样例都认不出 ⇒ 上一条是空断言"

    def test_every_job_declares_provenance_and_comparability(self):
        """每个条目必须自报**测量环境** + 是否与当前版本可比。"""
        for j in self._rb()["jobs"]:
            jid = j.get("job_id")
            p = j.get("provenance")
            assert isinstance(p, dict) and p, f"{jid} 缺 provenance"
            for k in ("measured_at", "ocr_backend", "llm_model", "app_version",
                      "reproducible", "reason"):
                assert k in p, f"{jid} 的 provenance 缺 {k!r}"
            assert isinstance(p["reproducible"], bool), f"{jid}: reproducible 必须是布尔"
            assert "comparable_to_current" in j, (
                f"{jid} 必须显式声明是否与当前版本可比（缺了就会有人默认'可比'）"
            )

    def test_counts_carry_runs_and_a_range(self):
        """计数必须带 `runs` + `observed_total_range` —— **单次计数不得当阈值**。"""
        for j in self._rb()["jobs"]:
            jid = j.get("job_id")
            runs = j.get("runs")
            assert isinstance(runs, int) and runs >= 1, f"{jid}: runs 缺失或非法"
            rng = j.get("observed_total_range")
            assert (isinstance(rng, list) and len(rng) == 2
                    and all(isinstance(x, int) for x in rng)
                    and rng[0] <= rng[1]), f"{jid}: observed_total_range 非法：{rng!r}"
            if "total_findings" in j:
                assert rng[0] <= j["total_findings"] <= rng[1], (
                    f"{jid}: total_findings={j['total_findings']} 不在 {rng} 内"
                )
            if "by_type" in j:
                assert isinstance(j["by_type"], dict) and all(
                    isinstance(v, int) for v in j["by_type"].values()
                ), f"{jid}: by_type 必须是 {type: int}"

    def test_a_multi_run_entry_exists(self):
        """至少要有一条 `runs >= 2` 的条目 —— 否则"区间"这个概念是空转的。"""
        multi = [j for j in self._rb()["jobs"] if j.get("runs", 1) >= 2]
        assert multi, (
            "没有任何多跑条目 ⇒ observed_total_range 永远退化成点，"
            "「真实 job 非确定」这件事就没有现场证据"
        )
        for j in multi:
            lo, hi = j["observed_total_range"]
            assert lo != hi, (
                f"{j['job_id']}: runs={j['runs']} 却给出退化的区间 [{lo}, {hi}] —— "
                "若真的一样，请改回 runs=1 并在 reason 里说明"
            )


# ── 端到端：规则层跑通且基线可复现 ─────────────────────────────────────────


def test_evaluate_end_to_end_baseline():
    """跑全语料（离线规则层），断言基线达标。

    M4 起 R12 落地，`self_review` 用例由 known_gap 转为验收 → 无 known_gap；
    用例总数 11（原 7 + M4 新增 4）。
    """
    report = ef.evaluate()
    s = report["summary"]
    assert s["cases_known_gap"] == 0
    assert s["cases_scored"] == 11
    assert s["fp"] == 0 and s["fn"] == 0
    assert s["precision"] == 1.0 and s["recall"] == 1.0 and s["f1"] == 1.0
    assert s["noise_findings"] > 0
