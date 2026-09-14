"""M5 知识库多源：store 多源维度、检索按源过滤、配置开关、金标命中率。

覆盖 T5.1–T5.3 / T5.7 的可机检不变量：
- store 从「单源」升级为多源注册表，且旧单源调用方语义不变（默认主源）
- 每源独立内容哈希版本；kb_version 为各源版本的组合哈希
- 检索可按 source_ids 过滤；引用携带可溯源信息（源/标题/文本性质）
- config.json ``kb.disabled_sources`` 的读取降级（读失败 = 全部启用）
- 金标查询集命中率 ≥85%（T5.7 验收口径，机检）
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from core.kb import retriever, store

_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN = _ROOT / "docs" / "KB_GOLDEN_QUERIES.json"
_EVAL_PATH = _ROOT / "scripts" / "eval_kb_queries.py"


def _load_eval():
    """scripts/ 非包 → 从文件路径加载（与其他 scripts 单测同模式）。"""
    import sys

    spec = importlib.util.spec_from_file_location("eval_kb_queries", _EVAL_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["eval_kb_queries"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── store：多源注册表 ────────────────────────────────────────────────────
class TestMultiSourceStore:
    def test_registry_has_at_least_six_sources(self):
        srcs = store.sources()
        assert len(srcs) >= 6, f"T5.1 要求 ≥6 源，实得 {len(srcs)}"
        assert store.PRIMARY_SOURCE_ID in {s["source_id"] for s in srcs}

    def test_every_source_has_provenance_and_version(self):
        """每条源必须可溯源：来源机构/文号/生效期 + 独立内容哈希版本。"""
        for s in store.sources():
            sid = s["source_id"]
            assert s["title"], f"{sid} 缺 title"
            assert s["authority"], f"{sid} 缺 authority"
            assert s["document_no"], f"{sid} 缺 document_no"
            assert s["effective"], f"{sid} 缺 effective"
            assert s["origin_url"], f"{sid} 缺 origin_url"
            assert s["retrieved_at"], f"{sid} 缺 retrieved_at"
            assert s["text_kind"] in ("original", "digest")
            assert len(s["version"]) == 12, f"{sid} 版本应为 12 位内容哈希"
            assert s["entries"] > 0

    def test_source_ids_sorted_and_unique(self):
        ids = store.source_ids()
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)

    def test_source_version_matches_registry(self):
        for s in store.sources():
            assert store.source_version(s["source_id"]) == s["version"]

    def test_entries_default_is_union_of_sources(self):
        """无参 entries() = 全部源条目之和（旧调用方拿到全量，不缩水）。"""
        all_e = store.entries()
        by_src = store.entries_by_source()
        assert len(all_e) == sum(len(v) for v in by_src.values())
        assert {e["source_id"] for e in all_e} == set(store.source_ids())

    def test_entries_filtered_by_source(self):
        for sid in store.source_ids():
            rows = store.entries(sid)
            assert rows, f"{sid} 无条目"
            assert all(e["source_id"] == sid for e in rows)
            assert all(e["entry_id"].startswith(sid + "-") for e in rows)

    def test_entry_ids_globally_unique(self):
        ids = [e["entry_id"] for e in store.entries()]
        assert len(set(ids)) == len(ids), "跨源 entry_id 冲突"

    def test_kb_version_is_combined_and_content_derived(self):
        v = store.kb_version()
        assert len(v) == 12
        # 组合版本必须由各源版本决定（同一语料重复调用稳定）
        assert store.kb_version() == v
        joined = "|".join(f"{s}@{store.source_version(s)}"
                          for s in store.source_ids())
        import hashlib

        assert v == hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
        # 任一源版本变化都应改变组合版本（用替换验证）
        assert v != hashlib.sha256(
            (joined + "|x").encode("utf-8")).hexdigest()[:12]

    def test_source_meta_defaults_to_primary(self):
        m = store.source_meta()
        assert m["source_id"] == store.PRIMARY_SOURCE_ID
        assert m["title"] and m["effective"]

    def test_search_index_browse_filters_source(self):
        rows = store.search_index("记录", limit=5,
                                  source_id="nmpa_di_2020")
        assert rows
        assert all(r["source_id"] == "nmpa_di_2020" for r in rows)

    def test_unknown_source_returns_empty_not_error(self):
        assert store.entries("no_such_source") == []
        assert store.source_version("no_such_source") == ""
        assert store.chapters("no_such_source") == []

    def test_degrade_without_primary_source(self, monkeypatch):
        """主源缺失时退化到首个源（而非崩溃/空）。"""
        srcs = dict(store._load_all())
        srcs.pop(store.PRIMARY_SOURCE_ID, None)
        monkeypatch.setattr(store, "_sources", srcs)
        try:
            assert store.source_meta()["source_id"] == sorted(srcs)[0]
            # 退化后的主源可能本就没有章结构（如 alcoa_plus），但必须返回 list
            assert isinstance(store.chapters(), list)
            assert store.entries()  # 全量条目仍在，不因主源缺失而丢失
        finally:
            store._reset_for_tests()


# ── retriever：按源过滤 + 溯源 ───────────────────────────────────────────
class TestRetrieverMultiSource:
    def test_refs_carry_provenance(self):
        refs = retriever._get_index().search(
            retriever._bigrams("批记录 偏差 复核"), topk=6)
        assert refs
        for r in refs:
            assert r["source_id"] in set(store.source_ids())
            assert r["source_title"]
            assert r["text_kind"] in ("original", "digest")

    def test_search_filtered_to_single_source(self):
        terms = retriever._bigrams("记录 偏差 复核 签名 质量")
        for sid in ("gmp2010", "alcoa_plus", "nmpa_di_2020"):
            refs = retriever._get_index().search(terms, topk=8,
                                                 source_ids={sid})
            assert refs, f"{sid} 单源检索不应为空"
            assert {r["source_id"] for r in refs} == {sid}

    def test_search_filter_to_unknown_source_is_empty(self):
        refs = retriever._get_index().search(
            retriever._bigrams("记录"), source_ids={"nope"})
        assert refs == []

    def test_attach_kb_refs_respects_source_filter(self):
        f = {"type": "completeness", "description": "批记录缺少复核签名"}
        retriever.attach_kb_refs([f], source_ids={"alcoa_plus"})
        assert f.get("kb_refs")
        assert {r["source_id"] for r in f["kb_refs"]} == {"alcoa_plus"}

    def test_enabled_source_ids_none_when_nothing_disabled(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "load_disabled_kb_sources", lambda: [])
        assert retriever.enabled_source_ids() is None

    def test_enabled_source_ids_excludes_disabled(self, monkeypatch):
        import config

        monkeypatch.setattr(config, "load_disabled_kb_sources",
                            lambda: ["fda_21cfr_part11"])
        keep = retriever.enabled_source_ids()
        assert keep is not None
        assert "fda_21cfr_part11" not in keep
        assert store.PRIMARY_SOURCE_ID in keep

    def test_enabled_source_ids_never_empties_corpus(self, monkeypatch):
        """全部禁用 → 返回 None（宁可不生效，也不能让检索恒空）。"""
        import config

        monkeypatch.setattr(config, "load_disabled_kb_sources",
                            lambda: store.source_ids())
        assert retriever.enabled_source_ids() is None

    def test_enabled_source_ids_fails_open_on_error(self, monkeypatch):
        import config

        def boom():
            raise RuntimeError("config broken")

        monkeypatch.setattr(config, "load_disabled_kb_sources", boom)
        assert retriever.enabled_source_ids() is None

    def test_attach_uses_config_filter(self, monkeypatch):
        import config

        # 禁用主源 → 引用必须全部来自其他源（配置开关真实生效）
        monkeypatch.setattr(config, "load_disabled_kb_sources",
                            lambda: [store.PRIMARY_SOURCE_ID])
        f = {"type": "completeness", "description": "批记录缺少复核签名"}
        retriever.attach_kb_refs([f])
        assert f.get("kb_refs")
        assert all(r["source_id"] != store.PRIMARY_SOURCE_ID
                   for r in f["kb_refs"])

    def test_type_queries_covers_every_rule_type(self):
        """T5.4：每个规则/分析类型都必须有策展查询词（无遗漏）。

        例外：``user_rule`` 与 ``uncategorized`` 是开放/兜底桶 —— 前者规则由
        用户自由填写、后者按定义无主题，无法预置词表，只能走描述挖掘。
        """
        from core.finding_quality import CANONICAL_TYPES

        open_ended = {"user_rule", "uncategorized"}
        missing = [t for t in CANONICAL_TYPES
                   if t not in retriever.TYPE_QUERIES and t not in open_ended]
        assert not missing, f"TYPE_QUERIES 缺类型: {missing}"
        assert len(CANONICAL_TYPES) - len(open_ended) >= 19

    def test_deviation_vocabulary_uses_corpus_wording(self):
        """词表纪律：偏差类查询必须含语料用词『偏离』（第二百五十条）。"""
        for t in ("param_out_of_spec", "deviation_link"):
            assert "偏离" in retriever.TYPE_QUERIES[t], (
                f"{t} 词表缺『偏离』—— GMP2010 第二百五十条的原文用词"
            )


# ── 报告引用分组 ─────────────────────────────────────────────────────────
class TestGroupRefsBySource:
    def test_groups_and_orders_by_source(self):
        # 生产路径的 refs 一定带 source_id（检索时注入），此处按同样形状构造
        refs = [
            {"entry_id": "nmpa_di_2020-2", "label": "第二条",
             "source_id": "nmpa_di_2020"},
            {"entry_id": "gmp2010-2", "label": "第二条", "source_id": "gmp2010"},
            {"entry_id": "gmp2010-1", "label": "第一条", "source_id": "gmp2010"},
        ]
        groups = retriever.group_refs_by_source(refs)
        ids = [m["source_id"] for m, _ in groups]
        assert ids == sorted(ids)
        by_id = {m["source_id"]: (m, items) for m, items in groups}
        assert [r["entry_id"] for r in by_id["gmp2010"][1]] == [
            "gmp2010-1", "gmp2010-2"]
        # 分组元数据必须带 title/document_no/version 供溯源
        meta = by_id["gmp2010"][0]
        assert meta["title"] and meta["version"]

    def test_ref_without_source_falls_into_unknown_bucket(self):
        groups = retriever.group_refs_by_source([
            {"entry_id": "legacy-1", "label": "旧数据",
             "source_title": "未知来源"}])
        assert len(groups) == 1
        meta, items = groups[0]
        assert meta["source_id"] == ""
        assert meta["title"] == "未知来源"
        assert items[0]["entry_id"] == "legacy-1"

    def test_empty_refs(self):
        assert retriever.group_refs_by_source([]) == []


# ── config：来源开关读取（与 load_disabled_rule_ids 同构） ───────────────
class TestLoadDisabledKbSources:
    @pytest.fixture
    def cfg_file(self, tmp_path, monkeypatch):
        import config

        path = tmp_path / "config.json"
        monkeypatch.setattr(config, "_config_path", lambda: path)
        return path

    def test_reads_list(self, cfg_file):
        import config

        cfg_file.write_text(json.dumps(
            {"kb": {"disabled_sources": ["fda_21cfr_part11"]}}), encoding="utf-8")
        assert config.load_disabled_kb_sources() == ["fda_21cfr_part11"]

    def test_missing_file_returns_empty(self, cfg_file):
        import config

        assert config.load_disabled_kb_sources() == []

    def test_missing_key_returns_empty(self, cfg_file):
        import config

        cfg_file.write_text(json.dumps({"ocr": {}}), encoding="utf-8")
        assert config.load_disabled_kb_sources() == []

    def test_non_list_value_returns_empty(self, cfg_file):
        import config

        cfg_file.write_text(json.dumps({"kb": {"disabled_sources": "oops"}}),
                            encoding="utf-8")
        assert config.load_disabled_kb_sources() == []

    def test_non_dict_kb_returns_empty(self, cfg_file):
        import config

        cfg_file.write_text(json.dumps({"kb": ["oops"]}), encoding="utf-8")
        assert config.load_disabled_kb_sources() == []

    def test_corrupt_json_returns_empty(self, cfg_file):
        """读失败 = 全部启用（绝不因配置损坏而清空检索）。"""
        import config

        cfg_file.write_text("{not json", encoding="utf-8")
        assert config.load_disabled_kb_sources() == []

    def test_non_string_entries_are_dropped(self, cfg_file):
        import config

        cfg_file.write_text(json.dumps(
            {"kb": {"disabled_sources": ["a", 7, None, {"x": 1}]}}),
            encoding="utf-8")
        assert config.load_disabled_kb_sources() == ["a", "7"]


# ── 金标查询集（T5.7 验收口径） ──────────────────────────────────────────
class TestGoldenQueries:
    @pytest.fixture(scope="class")
    def golden(self) -> dict:
        return json.loads(_GOLDEN.read_text(encoding="utf-8"))

    def test_shape_and_size(self, golden):
        qs = golden["queries"]
        assert len(qs) >= 30, f"金标集应 ≥30 条，实得 {len(qs)}"
        ids = [q["id"] for q in qs]
        assert len(set(ids)) == len(ids), "查询 id 重复"
        for q in qs:
            assert q["type"] and q["description"] and q["expect_any"]

    def test_expectations_reference_existing_entries(self, golden):
        """金标期望的 entry_id 必须真实存在 —— 防语料变更后金标悄悄失效。"""
        known = {e["entry_id"] for e in store.entries()}
        dangling = [(q["id"], e) for q in golden["queries"]
                    for e in q["expect_any"] if e not in known]
        assert not dangling, f"金标引用了不存在的条目: {dangling}"

    def test_annotations_cover_multiple_sources(self, golden):
        """期望条款应覆盖多源（否则多源语料没被真正验证）。"""
        hit_srcs = {s for s in store.source_ids()
                    if any(e.startswith(s + "-")
                           for q in golden["queries"] for e in q["expect_any"])}
        assert len(hit_srcs) >= 3, f"金标只覆盖 {hit_srcs}，多源未被验证"

    def test_hit_rate_meets_threshold(self, golden):
        ev = _load_eval()
        res = ev.evaluate(golden)
        assert res["graded_total"] >= 30
        assert res["graded_rate"] >= float(golden["threshold"]), (
            f"金标命中率 {res['graded_rate']:.1%} < {golden['threshold']:.0%}；"
            f"未命中: {[d['id'] for d in res['misses']]}"
        )

    def test_known_gaps_are_documented(self, golden):
        """每条 known_gap 必须写明原因，避免缺口被无声掩盖。"""
        for q in golden["queries"]:
            if q.get("known_gap"):
                assert q.get("gap_reason"), f"{q['id']} 标了 known_gap 但无原因"

    def test_eval_reports_both_rates(self, golden):
        ev = _load_eval()
        res = ev.evaluate(golden)
        assert res["rate"] <= 1.0 and res["graded_rate"] <= 1.0
        assert res["hits"] <= res["total"]
        assert res["graded_total"] == res["total"] - res["known_gaps"]

    def test_topk_window_is_bounded(self, golden):
        """命中窗口必须等于生产使用的 top-k（不能靠放宽窗口凑指标）。"""
        assert retriever._TOPK <= 5, "引用窗口过大会撑爆报告/prompt 预算"


# ── 语料可复现 / 可入库（T5.2「可重复种子」的落地不变量）────────────────
class TestCorpusReproducibility:
    _RAW_DIR = _ROOT / "core" / "kb" / "data" / "raw"

    def _raw_source_ids(self) -> set[str]:
        import re

        ids: set[str] = set()
        for md in sorted(self._RAW_DIR.glob("*.md")):
            m = re.search(r"^source_id:\s*(\S+)", md.read_text(encoding="utf-8"),
                          re.M)
            assert m, f"{md.name} 缺 source_id 头"
            ids.add(m.group(1))
        return ids

    def test_every_non_primary_source_has_curated_raw_text(self):
        """每个非主源都必须有可审计的原始文本（否则种子不可复现）。

        gmp2010 正文来自 docs/2010版GMP.doc（Word COM 提取），不在 raw/ 内；
        其余源全部由 raw/*.md 手工策展 —— 缺一个即无法从仓库重建语料。
        """
        expected = set(store.source_ids()) - {store.PRIMARY_SOURCE_ID}
        raw = self._raw_source_ids()
        assert raw == expected, (
            f"raw 源 {sorted(raw)} != 注册表源 {sorted(expected)}（缺 "
            f"{sorted(expected - raw)}，多余 {sorted(raw - expected)}）")

    def test_raw_headers_are_complete(self):
        """溯源头必须完整：合规引用要能回答"依据哪份文件的哪个版本"。"""
        import re

        required = ("title", "effective", "authority", "origin_url",
                    "text_kind", "lang", "license", "retrieved_at")
        for md in sorted(self._RAW_DIR.glob("*.md")):
            head = md.read_text(encoding="utf-8").split("---")[1]
            for key in required:
                assert re.search(rf"^{key}:\s*\S", head, re.M), \
                    f"{md.name} 头缺 {key}"

    def test_corpus_is_version_controlled(self):
        """语料必须可入 git —— 回归：`.gitignore` 的 `data/` 曾误吞语料目录。

        证据链：CLAUDE.md 记「派生 gmp2010.json 入 git」，.gitignore 亦注明
        「derived JSON ships instead」。若 `data/` 未锚定到仓库根，语料会在
        克隆后消失 → 冻结版只剩空知识库。
        """
        import shutil
        import subprocess

        if not shutil.which("git"):
            pytest.skip("git 不可用")
        targets = [self._RAW_DIR / "alcoa_plus.md",
                   _ROOT / "core" / "kb" / "data" / "gmp2010.json"]
        targets = [t for t in targets if t.exists()]
        assert targets, "语料文件缺失"
        p = subprocess.run(["git", "check-ignore", *map(str, targets)],
                           cwd=str(_ROOT), capture_output=True, text=True)
        assert p.returncode != 0, (
            f"语料被 gitignore 吞掉：{p.stdout.strip()}")


# ── 设置页前端（T5.6）：多源开关/版本/命中预览 ──────────────────────────
class TestKbSettingsFrontend:
    """前端契约机检 —— 后端接口齐了但前端没接线，验收同样不成立。"""

    _HTML = _ROOT / "templates" / "settings.html"
    _JS = _ROOT / "static" / "settings.js"

    def test_html_has_multisource_controls(self):
        html = self._HTML.read_text(encoding="utf-8")
        assert 'id="kb-sources"' in html, "缺少来源开关容器"
        assert 'id="kb-source-filter"' in html, "缺少按源过滤下拉"
        assert 'id="kb-meta"' in html and 'id="kb-list"' in html

    def test_html_drops_stale_readonly_copy(self):
        """旧文案「来源变更需更新应用内置数据」已不成立（现可在线开关）。"""
        html = self._HTML.read_text(encoding="utf-8")
        assert "来源变更需更新应用内置数据" not in html

    def test_js_wires_sources_endpoint(self):
        js = self._JS.read_text(encoding="utf-8")
        assert "/api/settings/kb/sources" in js
        assert re.search(r'"/api/settings/kb/sources"\s*,\s*\{[^}]*method:\s*"PUT"',
                         js, re.S), "来源开关必须走 PUT"

    def test_js_reads_disabled_state_and_guards_last_source(self):
        js = self._JS.read_text(encoding="utf-8")
        # 回填开关状态依赖后端返回的 disabled_sources（否则重载后全显"已启用"）
        assert "disabled_sources" in js
        # 前端也要拦"停用全部来源"，避免无谓请求 + 口径与后端一致
        assert "至少保留一个来源" in js

    def test_js_shows_version_and_per_source_meta(self):
        js = self._JS.read_text(encoding="utf-8")
        assert "kb_version" in js
        assert "s.version" in js and "s.entries" in js
