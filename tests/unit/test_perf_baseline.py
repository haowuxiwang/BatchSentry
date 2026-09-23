"""B11-5 性能判据的护栏单测。

重点不是"函数能跑"，而是**它真的只做相对判定**、且**取不到数时不冒充 PASS**。
后者用一条"同比例缩放不改变结论"的行为断言来证明 —— 若代码里藏了任何绝对阈值，
这条立刻红（绝对阈值在缩放后必然翻转）。
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass 需要先注册（同 test_gate_supply_chain）
    spec.loader.exec_module(mod)
    return mod


pb = _load("perf_baseline_under_test", "perf_baseline.py")


# ── judge_throughput 三种状态 ────────────────────────────────────────────────


def test_pass_when_equal_or_better():
    assert pb.judge_throughput(100, 100).status == pb.PASS
    assert pb.judge_throughput(150, 100).status == pb.PASS


def test_pass_at_exact_boundary():
    """阈值语义是 `>=`：比值恰好 0.8 应 PASS（不是 FAIL）。"""
    v = pb.judge_throughput(80, 100, min_ratio=0.8)
    assert v.status == pb.PASS, v.detail
    assert v.ratio == pytest.approx(0.8)


def test_fail_just_below_boundary():
    """0.799× ⇒ FAIL。证明边界是**真**边界，不是恒 PASS。"""
    v = pb.judge_throughput(79.9, 100, min_ratio=0.8)
    assert v.status == pb.FAIL, v.detail
    assert "跌破" in v.detail


def test_float_rounding_at_boundary_does_not_flip_verdict():
    """★ 浮点舍入不得把"恰好达标"判成 FAIL（本模块真实踩过的坑）。

    `(80*0.001)/(100*0.001)` 在 IEEE 下 = `0.7999999999999999`，而 `80/100` = `0.8`。
    同一个比值只因**先缩放过一次**就掉到阈值下方 —— 1 ULP 的差没有业务含义，
    必须靠容差吸收。若把容差去掉，这条立刻红。
    """
    exact = 80 / 100
    scaled = (80 * 0.001) / (100 * 0.001)
    assert exact != scaled, "前提变了：两者居然相等，本用例失去意义"
    assert pb.judge_throughput(80, 100).status == pb.PASS
    assert pb.judge_throughput(80 * 0.001, 100 * 0.001).status == pb.PASS
    # 但真实退化（0.79×）仍必须红 —— 容差不能吃掉真信号
    assert pb.judge_throughput(79.0, 100).status == pb.FAIL


def test_fail_names_both_sides():
    """FAIL 的说明必须**具名两侧数值**，而不是只说"太慢"。"""
    v = pb.judge_throughput(10, 100)
    assert v.status == pb.FAIL
    assert "10.0" in v.detail and "100.0" in v.detail


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"),
                                 "100", [], True])
def test_unjudgeable_for_unusable_numbers(bad):
    """取不到/非法数值 ⇒ UNJUDGEABLE，**绝不** PASS（"没测过" ≠ "没问题"）。"""
    assert pb.judge_throughput(bad, 100).status == pb.UNJUDGEABLE
    assert pb.judge_throughput(100, bad).status == pb.UNJUDGEABLE


def test_unjudgeable_message_warns_against_pass():
    v = pb.judge_throughput(None, None)
    assert "不得记为 PASS" in v.detail


# ── ★ 相对性证明（防止代码里藏绝对阈值）─────────────────────────────────────


@pytest.mark.parametrize("scale", [0.001, 0.5, 1, 7, 1000])
def test_verdict_is_scale_invariant(scale):
    """★ 两侧同比例缩放 ⇒ 结论**必须不变**。

    这是"判据是相对的"这一点的**行为证据**（不是读源码猜）：
    若实现里掺入任何绝对阈值（如 `if normal < 300:`），缩放后必然翻转 ⇒ 本用例红。
    """
    for n, r in [(80, 100), (100, 100), (50, 100)]:
        a = pb.judge_throughput(n, r)
        b = pb.judge_throughput(n * scale, r * scale)
        assert a.status == b.status, (
            f"缩放 {scale}× 后结论变了：{a.status} → {b.status}"
            "（说明判据里存在绝对阈值）")


# ── summarize_series ─────────────────────────────────────────────────────────


def test_summarize_uses_median_and_resists_outlier():
    """单点抖动（GC/磁盘突发）不应改变代表值 —— 中位数是抗噪的。"""
    assert pb.summarize_series([100, 100, 100, 100, 1]) == 100
    assert pb.summarize_series([10, 20, 30]) == 20


def test_summarize_none_when_no_usable_sample():
    assert pb.summarize_series([]) is None
    assert pb.summarize_series([0, -5, float("nan")]) is None


def test_summarize_one_outlier_cannot_flip_verdict():
    """5 个样本里 1 个塌到 1 MB/s：中位数仍是 100 ⇒ 仍 PASS（不被单点带崩）。"""
    samples = [100, 100, 100, 100, 1]
    assert pb.judge_series(samples, {"8MB": 100}, "8MB").status == pb.PASS


# ── load / record 基线 ───────────────────────────────────────────────────────


def test_load_reference_missing_or_broken_is_none(tmp_path):
    assert pb.load_reference(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert pb.load_reference(bad) is None
    nokey = tmp_path / "nokey.json"
    nokey.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert pb.load_reference(nokey) is None


def test_load_reference_filters_bad_values(tmp_path):
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"reference_mbps": {"8MB": 100, "4MB": 0,
                                                "1MB": "x"}}), encoding="utf-8")
    got = pb.load_reference(p)
    assert got == {"8MB": 100.0}          # 0 与非法值被剔除


def test_load_reference_all_bad_gives_none(tmp_path):
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"reference_mbps": {"8MB": 0}}), encoding="utf-8")
    assert pb.load_reference(p) is None   # 一条可用都没有 ⇒ None（不是空 dict）


def test_record_then_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "ref.json"
    pb.record_baseline(p, {"8MB": 120.5, "16MB": 110.0}, note="测试基线")
    got = pb.load_reference(p)
    assert got == {"8MB": 120.5, "16MB": 110.0}
    assert json.loads(p.read_text("utf-8"))["note"] == "测试基线"


def test_judge_series_missing_key_is_unjudgeable():
    v = pb.judge_series([100, 100], {"4MB": 100}, "8MB")
    assert v.status == pb.UNJUDGEABLE
    assert "8MB" in v.detail


def test_judge_series_none_reference_is_unjudgeable():
    assert pb.judge_series([100], None, "8MB").status == pb.UNJUDGEABLE


# ── 模块自洽：不得出现绝对吞吐常量 ───────────────────────────────────────────


def test_module_exposes_no_absolute_throughput_sla():
    """模块里不应存在"绝对 MB/s 阈值"这种常量（相对判据的全部意义所在）。

    用 AST 取**模块级数值常量**，断言没有一个是"看起来像吞吐阈值"的量级
    （>1 的纯数字常量）。`MIN_RATIO_DEFAULT = 0.8` 是比值，允许。
    —— 走 AST 而不是文本匹配，避免注释/字符串里提到 "300 MB/s" 就误报。
    """
    import ast

    tree = ast.parse((_SCRIPTS / "perf_baseline.py").read_text("utf-8"))
    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, (int, float)) \
                    and not isinstance(val.value, bool) and val.value > 1:
                offenders.append(f"{tgt.id}={val.value}")
    assert not offenders, (
        f"发现疑似绝对吞吐阈值常量：{offenders}"
        "（性能判据必须是相对的：只允许比值类常量，如 0.8）")
