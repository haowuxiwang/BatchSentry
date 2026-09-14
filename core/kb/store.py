"""Knowledge base storage: load the bundled multi-source regulation corpus.

Layout (all committed; generated at dev time by scripts/seed_kb.py):

    core/kb/data/raw/*.md     raw authoritative text + provenance header
    core/kb/data/<id>.json    derived runtime payload, one per source

M5 turned this from a single GMP2010 source into a multi-source registry:
every entry carries its own ``source_id``, each source has its own content-hash
version, and retrieval can be filtered by source. The legacy single-source
accessors (``entries()``, ``source_meta()``, ``chapters()``) are preserved and
now delegate to the *primary* source so existing callers keep working.

Deliberately dependency-free: retrieval works purely from the in-memory copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"

# Primary source: the one legacy callers mean when they say "the knowledge base".
PRIMARY_SOURCE_ID = "gmp2010"

# source_id -> payload dict, plus the derived keys "_version"/"_file"
_sources: dict[str, dict] | None = None


def _load_all() -> dict[str, dict]:
    """Load and cache every source payload (process-wide singleton)."""
    global _sources
    if _sources is not None:
        return _sources

    loaded: dict[str, dict] = {}
    for path in sorted(_DATA_DIR.glob("*.json")):
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:  # a corrupt source must not kill the others
            logger.warning("[kb] failed to load %s: %s", path.name, e)
            continue
        sid = str(payload.get("source_id") or path.stem)
        payload["source_id"] = sid
        # RAG 审计规范：知识库版本必须可溯源 —— 每源独立内容哈希前 12 位。
        payload["_version"] = hashlib.sha256(raw).hexdigest()[:12]
        payload["_file"] = path.name
        # Denormalise provenance onto each entry so the retriever can surface a
        # self-contained citation without a second lookup.
        title = payload.get("title", "")
        kind = payload.get("text_kind", "original")
        for e in payload.get("entries", []):
            e.setdefault("source_id", sid)
            e["source_title"] = title
            e["text_kind"] = kind
        loaded[sid] = payload
        logger.info(
            "[kb] loaded %s@%s: %d entries (kind=%s lang=%s)",
            sid, payload["_version"], len(payload.get("entries", [])),
            payload.get("text_kind", "?"), payload.get("lang", "?"),
        )
    _sources = loaded
    return loaded


def _primary() -> dict:
    srcs = _load_all()
    if PRIMARY_SOURCE_ID in srcs:
        return srcs[PRIMARY_SOURCE_ID]
    # Degrade gracefully: first source by id, or an empty shell.
    if srcs:
        return srcs[sorted(srcs)[0]]
    return {"source_id": "", "title": "", "chapters": [], "entries": [],
            "_version": ""}


def _reset_for_tests() -> None:
    """Drop the cached payloads so the next use re-reads from disk."""
    global _sources
    _sources = None


# ── 版本 ────────────────────────────────────────────────────────────
def kb_version() -> str:
    """Combined corpus version: hash over every per-source version."""
    srcs = _load_all()
    if not srcs:
        return ""
    joined = "|".join(f"{sid}@{srcs[sid]['_version']}" for sid in sorted(srcs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def source_version(source_id: str) -> str:
    return str(_load_all().get(source_id, {}).get("_version", ""))


# ── 源元数据 ────────────────────────────────────────────────────────
_SOURCE_META_KEYS = (
    "source_id", "title", "effective", "promulgated", "authority",
    "document_no", "origin_url", "text_kind", "lang", "license",
    "retrieved_at",
)


def sources() -> list[dict]:
    """All sources with provenance + entry counts + per-source version."""
    srcs = _load_all()
    out = []
    for sid in sorted(srcs):
        p = srcs[sid]
        meta = {k: p.get(k, "") for k in _SOURCE_META_KEYS}
        meta["version"] = p.get("_version", "")
        meta["entries"] = len(p.get("entries", []))
        meta["file"] = p.get("_file", "")
        out.append(meta)
    return out


def source_meta(source_id: str | None = None) -> dict:
    """Metadata for one source (default: primary) — legacy-compatible shape."""
    p = _load_all().get(source_id) if source_id else None
    if p is None:
        p = _primary()
    return {"source_id": p.get("source_id", ""),
            "title": p.get("title", ""),
            "effective": p.get("effective", "")}


def source_ids() -> list[str]:
    return sorted(_load_all())


# ── 条目 ────────────────────────────────────────────────────────────
def entries(source_id: str | None = None) -> list[dict]:
    """All entries, or the entries of one source (stable order)."""
    srcs = _load_all()
    if source_id is not None:
        return list(srcs.get(source_id, {}).get("entries", []))
    out: list[dict] = []
    for sid in sorted(srcs):
        out.extend(srcs[sid].get("entries", []))
    return out


def entries_by_source() -> dict[str, list[dict]]:
    srcs = _load_all()
    return {sid: list(srcs[sid].get("entries", [])) for sid in sorted(srcs)}


def chapters(source_id: str | None = None) -> list[dict]:
    if source_id is None:
        return _primary().get("chapters", [])
    return _load_all().get(source_id, {}).get("chapters", [])


def get_entry(entry_id: str) -> dict | None:
    for e in entries():
        if e["entry_id"] == entry_id:
            return e
    return None


# ── SQLite 镜像 ─────────────────────────────────────────────────────
async def ensure_db_seeded(db) -> int:
    """Mirror every source's entries into kb_entries (idempotent).

    Called from lifespan startup; failures are logged and never fatal —
    retrieval itself only needs the in-memory copy.
    """
    rows = entries()
    if not rows:
        return 0
    cur = await db.execute("SELECT COUNT(*) FROM kb_entries")
    if (await cur.fetchone())[0] >= len(rows):
        return 0
    await db.execute("DELETE FROM kb_entries")
    await db.executemany(
        "INSERT INTO kb_entries (entry_id, source_id, chapter, "
        "article_label, no, text) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (e["entry_id"], e.get("source_id", PRIMARY_SOURCE_ID), e["chapter"],
             e["article_label"], e["no"], e["text"])
            for e in rows
        ],
    )
    await db.commit()
    logger.info("[kb] seeded kb_entries with %d rows across %d sources",
                len(rows), len(_load_all()))
    return len(rows)


# ── 设置页浏览 ──────────────────────────────────────────────────────
def search_index(q: str, limit: int = 20,
                 source_id: str | None = None) -> list[dict]:
    """Naive substring browse for the settings page (not BM25)."""
    needle = (q or "").strip()
    out = []
    for e in entries(source_id):
        if not needle or needle in e["text"] or needle in e["article_label"] \
                or needle in e["chapter"] or needle in str(e.get("title", "")):
            out.append(e)
            if len(out) >= limit:
                break
    return out
