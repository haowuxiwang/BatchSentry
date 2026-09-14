"""Knowledge base retrieval: character-bigram BM25 over the GMP2010 entries.

Design constraints that shaped this module:
- Chinese text without a tokenizer -> character bigrams are the standard
  dependency-free unit; at 29K chars of corpus the inverted index builds in
  milliseconds and lives entirely in memory.
- Query terms come from two sources: curated per-finding-type regulation
  topics (TYPE_QUERIES) plus high-value bigrams mined from the finding's own
  description (terms that actually exist in the corpus index).
- Output is bounded: TopK entries, each excerpted, total char budget capped —
  this feeds citations only in v1 (post-hoc enrichment), never prompts.
"""
from __future__ import annotations

import logging
import math
import re

from core.kb import store

logger = logging.getLogger(__name__)

_K1 = 1.5
# 长度归一化强度。标准 BM25 常用 0.3–0.75；本语料是"条文级"短文本
# （avgdl≈87 字符，最短 ~10、最长 ~600），取 0.4 而非教科书默认 0.75 ——
# 权威条款往往较长（列举式条文），过强的长度惩罚会把它挤出前列。
# 该值经 docs/KB_GOLDEN_QUERIES.json 金标集实测标定（见 scripts/eval_kb_queries.py）。
_B = 0.4
_TOPK = 4
_MIN_SCORE = 1.0
_EXCERPT_CHARS = 120
_BUDGET_CHARS = 1500
_DESC_TERM_CAP = 12

# Curated query topics per finding type — regulation vocabulary likely to
# hit the governing articles (批记录/文件管理/偏差/放行…). Missing types fall
# back to description-mined bigrams alone.
#
# 词表纪律：必须使用**语料自身的用词**。GMP 2010 第二百五十条讲偏差时用的
# 是"偏离"（"任何偏离生产工艺…均应当有记录"），"偏离"在语料中 df=4（≈1%，
# 高 idf），而"偏差" df=24（6%）。只收"偏差"会让第二百五十条被高频泛词挤出
# 前列（实测：param_out_of_spec 金标命中率因此下降）。新增类型时按同样方式
# 核对语料用词，不要凭常识写同义词。
TYPE_QUERIES: dict[str, list[str]] = {
    "completeness": ["批记录", "记录", "复核", "签名"],
    "time_reversal": ["批记录", "记录", "真实", "及时"],
    "param_out_of_spec": ["偏离", "偏差", "质量标准", "检验", "放行"],
    "suspicious_date": ["有效期", "复验", "日期"],
    "signature_mismatch": ["签名", "复核", "批准"],
    "signature_time_anomaly": ["签名", "复核", "批准"],
    "batch_inconsistency": ["批号", "批记录"],
    "batch_logic": ["批号", "批记录"],
    "year_contradiction": ["记录", "保存", "年限"],
    "low_confidence": ["记录", "填写", "及时"],
    "handwritten": ["记录", "填写", "及时"],
    "step_gap": ["批记录", "工艺规程", "工序", "完整"],
    "ocr_noise": ["批记录", "记录", "文件"],
    "spec_unverifiable": ["质量标准", "检验", "规格", "复核"],
    # ── M4：R11–R17（配 gmp_basis 同源类型）──
    "mass_balance": ["物料平衡", "收率", "生产管理", "批记录"],
    "self_review": ["复核", "签名", "职责", "记录"],
    "equipment_state": ["设备", "清洁", "状态标志", "校验"],
    "env_monitor": ["厂房设施", "洁净", "环境", "监测"],
    "doc_version": ["文件管理", "版本", "文件", "作废"],
    "deviation_link": ["偏离", "偏差", "处理", "纠正", "预防"],
    "alteration": ["记录", "填写", "更改", "签名"],
}

_GENERIC_TERMS = ["批记录", "记录"]

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _bigrams(text: str) -> list[str]:
    """CJK bigrams; non-CJK runs collapse to nothing (regulation is zh)."""
    cleaned = _CJK_RE.findall(text)
    return [cleaned[i] + cleaned[i + 1] for i in range(len(cleaned) - 1)]


class _Index:
    def __init__(self) -> None:
        self.entries = store.entries()
        self.docs: list[list[str]] = []
        self.inverted: dict[str, list[tuple[int, int]]] = {}
        # 多源（M5）：逐条记录归属源，供按源过滤与引用溯源。
        self.src_of: list[str] = []
        self.title_of: list[str] = []
        self.kind_of: list[str] = []
        for i, e in enumerate(self.entries):
            # 索引 正文 + 条款标题。标题是文档内容的一部分：labeled 语料
            # （ALCOA+/21 CFR）把概念名写在标题里（"同步记录 Contemporaneous"），
            # 正文却是意译（"即时记录"），只索引正文会让这类条款检索不到。
            # gmp2010 的标题是"第X条"，其 bigram 在语料中高频（idf 极低），
            # 纳入后对排序无可感影响。
            toks = _bigrams(f"{e.get('article_label', '')} {e['text']}")
            self.docs.append(toks)
            self.src_of.append(str(e.get("source_id", "")))
            self.title_of.append(str(e.get("source_title", "")))
            self.kind_of.append(str(e.get("text_kind", "original")))
            df: dict[str, int] = {}
            for t in toks:
                df[t] = df.get(t, 0) + 1
            for t, f in df.items():
                self.inverted.setdefault(t, []).append((i, f))
        self.avgdl = (
            sum(len(d) for d in self.docs) / len(self.docs) if self.docs else 1.0
        )

    def search(self, terms: list[str], topk: int = _TOPK,
               min_score: float = _MIN_SCORE,
               excerpt_chars: int = _EXCERPT_CHARS,
               budget_chars: int = _BUDGET_CHARS,
               source_ids: set[str] | None = None) -> list[dict]:
        if not terms or not self.docs:
            return []
        n = len(self.docs)
        scores: dict[int, float] = {}
        seen_terms: set[str] = set()
        for t in terms:
            postings = self.inverted.get(t)
            if not postings or t in seen_terms:
                continue
            seen_terms.add(t)
            # 标准 BM25 idf 的**对数**形式。早期实现漏了对数，df=1 的词权重
            # 高达 (N+0.5)/1.5+1 ≈ 277，而真正稀有的多为描述挖掘出的噪声
            # bigram（"数超"/"围但"），会把权威条款挤出前列。取对数后
            # df=1 仅 ≈5.6，稀有但仍与泛词可比。
            idf = math.log(
                1 + (n - len(postings) + 0.5) / (len(postings) + 0.5)
            )
            for i, f in postings:
                if source_ids is not None and self.src_of[i] not in source_ids:
                    continue
                dl = len(self.docs[i])
                score = idf * (f * (_K1 + 1)) / (
                    f + _K1 * (1 - _B + _B * dl / self.avgdl)
                )
                scores[i] = scores.get(i, 0.0) + score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        used = 0
        for i, s in ranked:
            if s < min_score or used >= budget_chars:
                break
            e = self.entries[i]
            excerpt = e["text"][:excerpt_chars].replace("\n", " ")
            out.append({
                "entry_id": e["entry_id"],
                "label": e["article_label"],
                "chapter": e["chapter"],
                "excerpt": excerpt,
                "score": round(s, 2),
                # 条款级引用溯源（T5.5）：引用必须能落到具体法规与版本
                "source_id": self.src_of[i],
                "source_title": self.title_of[i],
                "text_kind": self.kind_of[i],
            })
            used += len(excerpt)
            if len(out) >= topk:
                break
        return out


_index: _Index | None = None


def _reset_index_for_tests() -> None:
    """Drop the cached index so the next use rebuilds from current entries."""
    global _index
    _index = None


def _get_index() -> _Index:
    global _index
    if _index is None:
        _index = _Index()
    return _index


def _mine_description_bigrams(description: str, index: _Index,
                              cap: int = _DESC_TERM_CAP,
                              max_df: int = 60) -> list[str]:
    """High-value bigrams from the finding's own text: present in the corpus
    AND reasonably rare (df<=max_df) so generic characters don't drown
    ranking."""
    cands = _bigrams(description or "")
    picked: list[str] = []
    for t in dict.fromkeys(cands):  # dedupe, keep order
        postings = index.inverted.get(t)
        if postings and len(postings) <= max_df:
            picked.append(t)
        if len(picked) >= cap:
            break
    return picked


def query_for(finding: dict) -> list[str]:
    """Curated type topics first, then description-mined bigram terms."""
    ftype = str(finding.get("type", ""))
    terms: list[str] = list(TYPE_QUERIES.get(ftype, []))
    if not terms:
        terms = list(_GENERIC_TERMS)
    idx = _get_index()
    # expand curated topics into their bigrams so BM25 can use them directly
    expanded: list[str] = []
    for t in terms:
        expanded.extend(_bigrams(t))
    expanded.extend(_mine_description_bigrams(
        str(finding.get("description") or ""), idx))
    return list(dict.fromkeys(expanded))


# 页面级注入主题词（KB-2）：批记录审核场景最常涉及的法规域
PAGE_TOPIC_TERMS = ["批记录", "记录", "复核", "签名", "偏差", "放行", "文件"]
_PAGE_TERM_CAP = 24
_PAGE_EXCERPT_CHARS = 200
_PAGE_BUDGET_CHARS = 1500


def enabled_source_ids() -> set[str] | None:
    """Sources currently switched on (config.json kb.disabled_sources).

    Returns None when everything is enabled — callers then skip the filter
    entirely, which keeps the common path free of per-doc set lookups.
    """
    try:
        from config import load_disabled_kb_sources

        disabled = load_disabled_kb_sources()
    except Exception:  # config unavailable (tests) -> fail open, keep all
        return None
    if not disabled:
        return None
    keep = set(store.source_ids()) - set(disabled)
    return keep or None  # never disable the whole corpus


def build_page_kb_context(page_text: str, topk: int = 5,
                          min_score: float = _MIN_SCORE,
                          source_ids: set[str] | None = None) -> tuple[str, list]:
    """KB-2：为整页 OCR 文本构建条文参考块（RAG grounding）。

    返回 (block_text, refs)：block 供 user prompt 注入（system 保持静态，
    维持 prompt caching 不变量）；refs 随 audit_ctx 落 llm_call_audit.kb_used
    （RAG 审计规范：记录本次检索命中的条目与知识库版本）。
    语料无关/低分/超预算时返回 ("", [])，调用方零成本跳过。
    """
    idx = _get_index()
    if not idx.docs:
        return "", []
    terms: list[str] = []
    for t in PAGE_TOPIC_TERMS:
        terms.extend(_bigrams(t))
    terms.extend(_mine_description_bigrams(
        page_text or "", idx, cap=_PAGE_TERM_CAP, max_df=80))
    terms = list(dict.fromkeys(terms))
    if source_ids is None:
        source_ids = enabled_source_ids()
    refs = idx.search(terms, topk=topk, min_score=min_score,
                      excerpt_chars=_PAGE_EXCERPT_CHARS,
                      budget_chars=_PAGE_BUDGET_CHARS,
                      source_ids=source_ids)
    if not refs:
        return "", []
    lines = [f"{r['label']}（{r['source_title']}）：{r['excerpt']}" for r in refs]
    return "\n".join(lines), refs


def attach_kb_refs(findings: list[dict],
                   source_ids: set[str] | None = None) -> list[dict]:
    """Enrich findings in place with kb_refs (list of citation dicts).

    Mirrors gmp_basis.attach_gmp_basis conventions: pure in-memory, idempotent
    (existing non-empty kb_refs untouched), safe against non-dict rows and an
    empty knowledge base (no-op). Callers JSON-serialize at INSERT time.

    ``source_ids`` restricts retrieval to a subset of sources (None = use the
    config-driven enabled set, so a disabled source never gets cited).
    """
    try:
        idx = _get_index()
    except Exception as e:  # corrupted/missing seed must never break pipeline
        logger.warning("[kb] retriever unavailable, skipping: %s", e)
        return findings
    if not idx.docs:
        return findings
    if source_ids is None:
        source_ids = enabled_source_ids()
    for f in findings:
        if not isinstance(f, dict) or f.get("kb_refs"):
            continue
        refs = idx.search(query_for(f), source_ids=source_ids)
        if refs:
            f["kb_refs"] = refs
    return findings


def dedup_refs_for_report(findings: list[dict]) -> list[dict]:
    """Unique citations across a job for the report appendix (full text)."""
    seen: dict[str, dict] = {}
    for f in findings:
        raw = f.get("kb_refs") if isinstance(f, dict) else None
        refs = json_load_refs(raw)
        for r in refs:
            seen.setdefault(r["entry_id"], r)
    return list(seen.values())


def group_refs_by_source(refs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Group report citations by source for the appendix (T5.5).

    Returns [(source_meta, [ref, ...]), ...] ordered by source_id, with the
    per-source meta carrying title/document_no/version so a citation trace
    resolves to a concrete regulation edition. Refs missing a source_id fall
    into a synthetic "unknown" bucket rather than being dropped.
    """
    meta_by_id = {m["source_id"]: m for m in store.sources()}
    buckets: dict[str, list[dict]] = {}
    for r in refs:
        buckets.setdefault(str(r.get("source_id") or ""), []).append(r)

    grouped: list[tuple[dict, list[dict]]] = []
    for sid in sorted(buckets):
        meta = meta_by_id.get(sid) or {
            "source_id": sid, "title": _fallback_title(buckets[sid]),
            "document_no": "", "version": "", "text_kind": "original",
        }
        items = sorted(buckets[sid], key=lambda r: r.get("entry_id", ""))
        grouped.append((meta, items))
    return grouped


def _fallback_title(refs: list[dict]) -> str:
    """Best-effort source title from a bucket of refs (unknown-source case)."""
    for r in refs:
        if r.get("source_title"):
            return str(r["source_title"])
    return ""


def json_load_refs(raw) -> list[dict]:
    """Decode findings.kb_refs (JSON string or already-list) defensively."""
    import json as _json
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, str) and raw:
        try:
            data = _json.loads(raw)
            return [r for r in data if isinstance(r, dict)] if isinstance(
                data, list) else []
        except ValueError:
            return []
    return []
