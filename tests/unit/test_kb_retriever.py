"""KB retriever tests — bigram-BM25 relevance + enrichment contract."""
import pytest

from core.kb import retriever, store


@pytest.fixture(scope="module", autouse=True)
def _ensure_index():
    # 触发索引构建（依赖已提交的种子 JSON）
    idx = retriever._get_index()
    assert idx.docs, "kb index empty — seed JSON missing?"
    retriever._reset_index_for_tests()  # module 卸载后由下个用例重建亦可
    yield


class TestSearch:
    def test_batch_record_retention_query_hits_document_management(self):
        """批记录保存年限类查询 → 命中文件管理章条文（top3 内）。"""
        refs = retriever._get_index().search(
            retriever._bigrams("批记录应当保存至药品有效期后一年"))
        assert refs, "expected hits"
        top_labels = [r["label"] for r in refs[:3]]
        joined = "".join(top_labels)
        assert "第一百五十" in joined or "第一百六十" in joined, top_labels
        top_text = store.get_entry(refs[0]["entry_id"])["text"]
        assert any(kw in top_text for kw in ("批记录", "保存", "文件"))

    def test_empty_terms_returns_nothing(self):
        assert retriever._get_index().search([]) == []

    def test_garbage_terms_return_nothing(self):
        refs = retriever._get_index().search(
            retriever._bigrams("zzzqqqxxx 隨機機隨"))
        assert refs == [] or all(r["score"] >= 1.0 for r in refs)

    def test_result_bounds_and_excerpt(self):
        refs = retriever._get_index().search(
            retriever._bigrams("批记录 复核 签名 偏差 放行 洁净"), topk=4)
        assert len(refs) <= 4
        known = set(store.source_ids())
        for r in refs:
            assert len(r["excerpt"]) <= 120
            # M5：多源语料 ⇒ 每条引用必须归属一个已注册来源（可溯源）。
            assert r["source_id"] in known
            assert r["entry_id"].startswith(r["source_id"] + "-")
            assert r["source_title"]


class TestQueryFor:
    def test_known_type_uses_curated_topics(self):
        terms = retriever.query_for({"type": "completeness", "description": ""})
        assert terms, "curated topic must expand into bigrams"

    def test_unknown_type_mines_description(self):
        desc = "工序6 的结束时间早于开始时间，批记录填写存在矛盾"
        terms = retriever.query_for({"type": "mystery_type",
                                     "description": desc})
        assert terms  # description mining produced usable bigrams


class TestAttachKbRefs:
    def test_enriches_matching_finding(self):
        f = {"type": "time_reversal", "description": "批记录中时间倒序"}
        out = retriever.attach_kb_refs([f])
        assert out[0].get("kb_refs")
        refs = out[0]["kb_refs"]
        assert all("label" in r and "excerpt" in r for r in refs)
        budget = sum(len(r["excerpt"]) for r in refs)
        assert budget <= retriever._BUDGET_CHARS

    def test_idempotent_existing_refs_untouched(self):
        sentinel = [{"entry_id": "x", "label": "L", "excerpt": "e"}]
        f = {"type": "completeness", "description": "", "kb_refs": sentinel}
        retriever.attach_kb_refs([f])
        assert f["kb_refs"] is sentinel

    def test_non_dict_rows_ignored(self):
        out = retriever.attach_kb_refs(["not-a-dict", None,
                                        {"type": "completeness"}])
        assert "not-a-dict" in out and None in out
