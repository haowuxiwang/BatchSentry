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
import re

from core.kb import store

logger = logging.getLogger(__name__)

_K1 = 1.5
_B = 0.75
_TOPK = 4
_MIN_SCORE = 1.0
_EXCERPT_CHARS = 120
_BUDGET_CHARS = 1500
_DESC_TERM_CAP = 12

# Curated query topics per finding type — regulation vocabulary likely to
# hit the governing articles (批记录/文件管理/偏差/放行…). Missing types fall
# back to description-mined bigrams alone.
TYPE_QUERIES: dict[str, list[str]] = {
    "completeness": ["批记录", "记录", "复核", "签名"],
    "time_reversal": ["批记录", "记录", "真实", "及时"],
    "param_out_of_spec": ["偏差", "质量标准", "检验", "放行"],
    "suspicious_date": ["有效期", "复验", "日期"],
    "signature_mismatch": ["签名", "复核", "批准"],
    "signature_time_anomaly": ["签名", "复核", "批准"],
    "batch_inconsistency": ["批号", "批记录"],
    "batch_logic": ["批号", "批记录"],
    "year_contradiction": ["记录", "保存", "年限"],
    "low_confidence": ["记录", "填写", "及时"],
    "handwritten": ["记录", "填写", "及时"],
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
        for i, e in enumerate(self.entries):
            toks = _bigrams(e["text"])
            self.docs.append(toks)
            df: dict[str, int] = {}
            for t in toks:
                df[t] = df.get(t, 0) + 1
            for t, f in df.items():
                self.inverted.setdefault(t, []).append((i, f))
        self.avgdl = (
            sum(len(d) for d in self.docs) / len(self.docs) if self.docs else 1.0
        )

    def search(self, terms: list[str], topk: int = _TOPK,
               min_score: float = _MIN_SCORE) -> list[dict]:
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
            idf = ((n - len(postings) + 0.5) / (len(postings) + 0.5)) + 1.0
            for i, f in postings:
                dl = len(self.docs[i])
                score = idf * (f * (_K1 + 1)) / (
                    f + _K1 * (1 - _B + _B * dl / self.avgdl)
                )
                scores[i] = scores.get(i, 0.0) + score
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out = []
        used = 0
        for i, s in ranked:
            if s < min_score or used >= _BUDGET_CHARS:
                break
            e = self.entries[i]
            excerpt = e["text"][:_EXCERPT_CHARS].replace("\n", " ")
            out.append({
                "entry_id": e["entry_id"],
                "label": e["article_label"],
                "chapter": e["chapter"],
                "excerpt": excerpt,
                "score": round(s, 2),
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


def _mine_description_bigrams(description: str, index: _Index) -> list[str]:
    """High-value bigrams from the finding's own text: present in the corpus
    AND reasonably rare (df<=60) so generic characters don't drown ranking."""
    cands = _bigrams(description or "")
    picked: list[str] = []
    for t in dict.fromkeys(cands):  # dedupe, keep order
        postings = index.inverted.get(t)
        if postings and len(postings) <= 60:
            picked.append(t)
        if len(picked) >= _DESC_TERM_CAP:
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


def attach_kb_refs(findings: list[dict]) -> list[dict]:
    """Enrich findings in place with kb_refs (list of citation dicts).

    Mirrors gmp_basis.attach_gmp_basis conventions: pure in-memory, idempotent
    (existing non-empty kb_refs untouched), safe against non-dict rows and an
    empty knowledge base (no-op). Callers JSON-serialize at INSERT time.
    """
    try:
        idx = _get_index()
    except Exception as e:  # corrupted/missing seed must never break pipeline
        logger.warning("[kb] retriever unavailable, skipping: %s", e)
        return findings
    if not idx.docs:
        return findings
    for f in findings:
        if not isinstance(f, dict) or f.get("kb_refs"):
            continue
        refs = idx.search(query_for(f))
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
