"""M7 T7.3 —— 三引擎对比脚本（scripts/compare_ocr_engines.py）。

覆盖：可用性判定、引擎选择、单引擎运行容错、两两对比报告、main 退出码
与 JSON 落盘（用假 runner，不触真实 OCR）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "compare_ocr_engines", REPO / "scripts" / "compare_ocr_engines.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_mod()


def _page(text):
    return {"markdown": {"text": text}, "page_count": 1, "_source": "x"}


class TestEngineAvailability:
    def test_unknown_backend(self, mod):
        ok, reason = mod.engine_available("nope")
        assert ok is False and "未注册" in reason

    def test_docling_delegates(self, mod, monkeypatch):
        monkeypatch.setattr("core.docling_client.is_available", lambda: True)
        assert mod.engine_available("docling")[0] is True
        monkeypatch.setattr("core.docling_client.is_available", lambda: False)
        ok, reason = mod.engine_available("docling")
        assert ok is False and "未安装" in reason

    def test_remote_backend_needs_credentials(self, mod, monkeypatch):
        from config import config

        old = config["mineru"].token
        try:
            config["mineru"].token = "tok"
            assert mod.engine_available("mineru")[0] is True
            config["mineru"].token = ""
            ok, reason = mod.engine_available("mineru")
            assert ok is False and "凭据" in reason
        finally:
            config["mineru"].token = old


class TestSelectEngines:
    def test_default_all(self, mod):
        assert set(mod._select_engines(None)) == set(mod._RUNNER_TARGETS)

    def test_requested_normalized(self, mod):
        assert mod._select_engines([" MinerU ", "PADDLE", ""]) == ["mineru", "paddle"]


class TestRunEngine:
    def test_ok(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod, "_resolve_runner", lambda n: (lambda pdf: [_page("a"), _page("b")])
        )
        r = mod.run_engine("paddle", "/tmp/x.pdf")
        assert r["ok"] is True and len(r["pages"]) == 2

    def test_error_captured(self, mod, monkeypatch):
        def boom(pdf):
            raise RuntimeError("down")

        monkeypatch.setattr(mod, "_resolve_runner", lambda n: boom)
        r = mod.run_engine("mineru", "/tmp/x.pdf")
        assert r["ok"] is False and "RuntimeError" in r["error"]


class TestCompareEngines:
    def test_identical_no_diffs(self, mod):
        results = {
            "paddle": {"ok": True, "pages": [_page("hello")], "elapsed_s": 0.1},
            "mineru": {"ok": True, "pages": [_page("hello")], "elapsed_s": 0.2},
        }
        rep = mod.compare_engines(results)
        assert rep["pairs"]["mineru|paddle"]["diff_count"] == 0
        assert rep["diff_pages"] == []
        assert rep["engines"]["paddle"]["chars"] == 5

    def test_differing_detected(self, mod):
        results = {
            "paddle": {"ok": True, "pages": [_page("alpha beta gamma")], "elapsed_s": 0},
            "mineru": {"ok": True, "pages": [_page("完全不同的内容")], "elapsed_s": 0},
        }
        rep = mod.compare_engines(results)
        assert rep["pairs"]["mineru|paddle"]["diff_count"] == 1
        assert rep["diff_pages"][0]["page"] == 1

    def test_page_count_mismatch(self, mod):
        results = {
            "paddle": {"ok": True, "pages": [_page("a"), _page("b")], "elapsed_s": 0},
            "mineru": {"ok": True, "pages": [_page("a")], "elapsed_s": 0},
        }
        rep = mod.compare_engines(results)
        assert rep["pairs"]["mineru|paddle"]["pages"] == 2
        assert rep["pairs"]["mineru|paddle"]["diff_count"] >= 1

    def test_single_engine_no_pairs(self, mod):
        results = {"paddle": {"ok": True, "pages": [_page("a")], "elapsed_s": 0}}
        rep = mod.compare_engines(results)
        assert rep["pairs"] == {}


class TestMain:
    def test_returns_one_when_fewer_than_two(self, mod, tmp_path, monkeypatch):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        monkeypatch.setattr(mod, "engine_available", lambda n: (False, "nope"))
        out = tmp_path / "r.json"
        rc = mod.main([str(pdf), "--engines", "paddle,mineru", "--json", str(out)])
        assert rc == 1
        assert json.loads(out.read_text(encoding="utf-8"))["skipped"]

    def test_returns_zero_and_writes_report(self, mod, tmp_path, monkeypatch):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        pages = {"paddle": [_page("same")], "mineru": [_page("same")]}
        monkeypatch.setattr(mod, "engine_available", lambda n: (True, ""))
        monkeypatch.setattr(mod, "_resolve_runner", lambda n: (lambda p: pages[n]))

        out = tmp_path / "r.json"
        rc = mod.main([str(pdf), "--engines", "paddle,mineru", "--json", str(out)])
        assert rc == 0
        rep = json.loads(out.read_text(encoding="utf-8"))
        assert set(rep["engines"]) == {"paddle", "mineru"}
        assert rep["pairs"]["mineru|paddle"]["diff_count"] == 0

    def test_missing_pdf_returns_one(self, mod, tmp_path):
        rc = mod.main([str(tmp_path / "nope.pdf")])
        assert rc == 1
