"""KB defensive-branch coverage: missing seed, unloaded browse, enrich errors."""
import pytest


@pytest.fixture
def empty_store(monkeypatch):
    from core.kb import store, retriever

    # M5：store 从单源 _payload 改为多源 _sources 字典；空字典 ⇒ 零来源。
    monkeypatch.setattr(store, "_sources", {})
    retriever._reset_index_for_tests()
    yield store
    monkeypatch.undo()
    retriever._reset_index_for_tests()


class TestDefensiveBranches:
    @pytest.mark.asyncio
    async def test_browse_404_when_unloaded(self, test_client, empty_store):
        r = await test_client.get("/api/settings/kb")
        assert r.status_code == 404

    def test_build_ctx_empty_store(self, empty_store):
        from core.kb.retriever import build_page_kb_context

        block, refs = build_page_kb_context("批生产记录 复核 签名")
        assert block == "" and refs == []

    def test_attach_refs_swallows_index_errors(self, monkeypatch):
        from core.kb import retriever

        def boom():
            raise RuntimeError("corrupt index")

        monkeypatch.setattr(retriever, "_get_index", boom)
        f = {"type": "completeness", "description": "x"}
        out = retriever.attach_kb_refs([f])
        assert out == [f] and "kb_refs" not in f

    def test_store_missing_file_branch(self, monkeypatch, tmp_path):
        """语料目录为空 ⇒ 零来源、空版本、空条目（绝不清空调用方）。"""
        from core.kb import store

        monkeypatch.setattr(store, "_DATA_DIR", tmp_path)
        store._reset_for_tests()
        try:
            assert store._load_all() == {}
            assert store.entries() == []
            assert store.sources() == []
            assert store.kb_version() == ""
        finally:
            store._reset_for_tests()

    def test_get_entry_miss(self):
        from core.kb import store

        assert store.get_entry("gmp2010-99999") is None

    def test_search_dedupes_terms(self):
        """重复词元走 seen_terms 跳过分支（幂等计分）。"""
        from core.kb.retriever import _get_index, _bigrams

        idx = _get_index()
        terms = _bigrams("批记录 记录 批记录")
        refs = idx.search(terms + terms[:4], topk=3)
        assert refs and all(r["score"] >= 1.0 for r in refs)

    def test_json_load_refs_filters_non_dicts(self):
        from core.kb.retriever import json_load_refs

        mixed = [{"entry_id": "e", "label": "l", "excerpt": "x"}, "junk", 7]
        assert json_load_refs(mixed) == [{"entry_id": "e", "label": "l",
                                          "excerpt": "x"}]
        assert json_load_refs(123) == []

    def test_build_page_ctx_success_shape(self):
        """真实语料上成功构建参考块：预算内、条文号格式正确。"""
        from core.kb.retriever import build_page_kb_context

        text = ("批生产记录应当由质量管理部门复核并保存至药品有效期后一年，"
                "偏差应当及时调查处理，放行前应当完成各项检查。")
        block, refs = build_page_kb_context(text)
        assert block and refs
        assert len(block) <= 1500 + 200  # 预算 + 单行标签开销
        assert all(r["label"].startswith("第") for r in refs)
        assert all("第" in line for line in block.splitlines())

    def test_dedup_refs_for_report(self):
        from core.kb.retriever import dedup_refs_for_report

        f1 = {"kb_refs": [{"entry_id": "a", "label": "L1", "excerpt": "e1"}]}
        f2 = {"kb_refs": [
            {"entry_id": "a", "label": "L1", "excerpt": "dup"},
            {"entry_id": "b", "label": "L2", "excerpt": "e2"},
        ]}
        out = dedup_refs_for_report([f1, f2, {}])
        ids = [r["entry_id"] for r in out]
        assert sorted(ids) == ["a", "b"]
        assert dedup_refs_for_report([{}]) == []
