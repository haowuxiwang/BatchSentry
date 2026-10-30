"""`scripts/eval_findings.py` 的判分契约（R133）。

为什么要有：评分脚本是"准确性"的唯一度量入口。它自己错（例如把"没命中"
判成通过、或把空输入判成通过）会让后续所有拟合都失去意义 —— 这类"判据本身
fail-open"在本项目已出过多次（见 release_gate 的 fail-closed 先例）。
"""
from __future__ import annotations

import json

import pytest

import scripts.eval_findings as ev


def _f(page=8, ftype="suspicious_date", severity="warning", desc="日期读数异常", source="llm_cross"):
    return {"page": page, "type": ftype, "severity": severity,
            "description": desc, "source": source}


GOLD = {
    "record": "unit",
    "forbid_text": ["篡改", "伪造"],
    "labels": [
        {
            "id": "L1", "criterion": True, "evidence": "test", "description": "日期异常只报一条",
            "match": {"page": 8, "type_any": ["suspicious_date"]},
            "expect": {"min": 1, "max": 1, "severity_max": "warning"},
        },
        {
            "id": "L2", "criterion": False, "evidence": "pin", "description": "回归钉",
            "match": {"page": 9}, "expect": {"min": 0},
        },
    ],
}


def test_load_findings_accepts_three_shells(tmp_path):
    body = [_f()]
    for name, payload in {
        "e2e.json": {"job": {"id": "x"}, "findings": {"findings": body}},
        "wrap.json": {"findings": body},
        "bare.json": body,
    }.items():
        p = tmp_path / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        assert ev.load_findings(str(p)) == body, name


def test_load_findings_rejects_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"job": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        ev.load_findings(str(p))


def test_recall_counts_only_criterion_labels():
    rep = ev.evaluate([_f()], GOLD)
    assert rep["criterion_labels"] == 1
    assert rep["criterion_passed"] == 1
    assert rep["recall_proxy"] == 1.0
    # 非判据标签也参与判定，但不进 recall 分母
    assert rep["labels_total"] == 2


def test_duplicate_over_max_fails():
    """同一误读报两条 ⇒ 必须判红（0-75 的验收点）。"""
    rep = ev.evaluate([_f(), dict(_f(), page=8, description="另一条")], GOLD)
    l1 = next(r for r in rep["labels"] if r["id"] == "L1")
    assert not l1["pass"]
    assert any("重复" in p for p in l1["problems"])
    assert not rep["ok"]


def test_missing_under_min_fails():
    rep = ev.evaluate([_f(page=3)], GOLD)
    l1 = next(r for r in rep["labels"] if r["id"] == "L1")
    assert not l1["pass"]
    assert any("漏报" in p for p in l1["problems"])


def test_severity_ceiling_is_enforced():
    rep = ev.evaluate([_f(severity="critical")], GOLD)
    l1 = next(r for r in rep["labels"] if r["id"] == "L1")
    assert not l1["pass"]
    assert any("过度定性" in p for p in l1["problems"])


def test_forbidden_wording_anywhere_fails():
    rep = ev.evaluate([_f(), _f(page=8, desc="存在记录篡改嫌疑")], GOLD)
    assert len(rep["forbidden_hits"]) == 1
    assert not rep["ok"], "禁用措辞命中不得判通过"


def test_text_and_negative_filters():
    gold = {
        "labels": [{
            "id": "T", "criterion": True, "evidence": "t", "description": "带附注且不含断言",
            "match": {"text_all": ["不作为偏差结论"], "text_none": ["存在逻辑错误"]},
            "expect": {"min": 1, "max": 1},
        }],
    }
    ok = ev.evaluate([_f(desc="读数不平衡（不作为偏差结论）")], gold)
    assert ok["ok"]
    bad = ev.evaluate([_f(desc="存在逻辑错误（不作为偏差结论）")], gold)
    assert not bad["ok"]


def test_main_fails_closed_on_empty_inputs(tmp_path, capsys):
    gold_p = tmp_path / "gold.json"
    gold_p.write_text(json.dumps(GOLD), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert ev.main(["--findings", str(empty), "--gold", str(gold_p)]) == 2
    assert "不得判通过" in capsys.readouterr().err

    no_labels = tmp_path / "nolabels.json"
    no_labels.write_text(json.dumps({"labels": []}), encoding="utf-8")
    findings = tmp_path / "f.json"
    findings.write_text(json.dumps([_f()]), encoding="utf-8")
    assert ev.main(["--findings", str(findings), "--gold", str(no_labels)]) == 2


def test_main_exit_code_reflects_verdict(tmp_path):
    gold_p = tmp_path / "gold.json"
    gold_p.write_text(json.dumps(GOLD), encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps([_f()]), encoding="utf-8")
    assert ev.main(["--findings", str(good), "--gold", str(gold_p)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([_f(), _f()]), encoding="utf-8")
    assert ev.main(["--findings", str(bad), "--gold", str(gold_p)]) == 1
