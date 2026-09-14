"""Seed the knowledge base corpus (dev-time only).

Two build paths, both writing committed JSON under core/kb/data/:

1. **gmp2010 正文** — extracted from docs/2010版GMP.doc via Word COM
   (pywin32). The document uses "正文" style throughout, so structure comes
   from 第X章 / 第X条 markers, converted from Chinese numerals to arabic.

2. **Multi-source corpus** (M5) — every ``core/kb/data/raw/*.md`` file is one
   source: a YAML-ish provenance header followed by chapter/article bodies.
   The raw text is committed verbatim (or, for foreign-language sources, as a
   clearly-labelled Chinese digest plus the verbatim original); the derived
   JSON is what the runtime loads. This makes every source independently
   reproducible and auditable — a reviewer can diff the raw text against the
   official document.

Runtime never needs Word COM: end-user machines load only the JSON.

Usage:
    python scripts/seed_kb.py               # build everything that is buildable
    python scripts/seed_kb.py --from-raw    # build raw/*.md sources only (no COM)
    python scripts/seed_kb.py --verify      # verify existing JSON only (no COM)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "2010版GMP.doc"
DATA_DIR = ROOT / "core" / "kb" / "data"
RAW_DIR = DATA_DIR / "raw"
OUT_PATH = DATA_DIR / "gmp2010.json"

# ── gmp2010 正文 ─────────────────────────────────────────────────────
SOURCE_ID = "gmp2010"
SOURCE_TITLE = "药品生产质量管理规范（2010年修订）"

# Per-source structural expectations: source_id -> (chapters, min_entries,
# max_entries). Checked by verify_payload; new sources are added here so a
# truncated extraction fails loudly instead of silently shrinking the corpus.
_EXPECT: dict[str, tuple[int, int, int]] = {
    "gmp2010": (14, 313, 313),
    "gmp2010_annex_cs": (6, 24, 24),
    "gmp2010_annex_vv": (10, 54, 54),
    "nmpa_di_2020": (6, 30, 30),
    "alcoa_plus": (0, 10, 10),
    "fda_21cfr_part11": (0, 10, 10),
}

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100}

# 中文数字字符类 —— **必须含"零"**。历史缺陷：早期正则写作
# [一二三四五六七八九十百]，漏了"零"，导致编码含零的条文
# （第一百零一~一百零九条 / 第二百零一~二百零九条 / 第三百零一~三百零九条，
# 共 27 条）永远匹配不上，其正文被静默并入前一条（如第二百条正文里混进了
# 第二百零一条及之后的内容）。对合规产品而言这是硬伤：这些条款无法被引用，
# 且前一条正文被污染。单一常量 + 回归测试（test_kb_seed）锁定该不变量。
_CN_DIGITS = "零一二三四五六七八九十百"


def cn_to_int(label: str) -> int:
    """Parse a Chinese-numeral article/chapter number, e.g. 一百五十一 -> 151."""
    s = label.strip()
    if not s:
        raise ValueError(f"empty numeral: {label!r}")
    total = 0
    current = 0
    for ch in s:
        if ch in _CN_NUM:
            current = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if current == 0:
                current = 1  # 十X == 10+X
            total += current * unit
            current = 0
        elif ch == "零":
            continue
        else:
            raise ValueError(f"unexpected char {ch!r} in {label!r}")
    return total + current


_CHAPTER_RE = re.compile(rf"^第([{_CN_DIGITS}]+)章")
_ARTICLE_RE = re.compile(rf"^第([{_CN_DIGITS}]+)条")


def extract_text_via_com(doc_path: Path) -> str:
    import win32com.client

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(doc_path), ReadOnly=True)
        try:
            text = doc.Content.Text
        finally:
            doc.Close(False)
    finally:
        word.Quit()
    # Word cell/paragraph markers: \r (\x0d) paragraph, \x07 cell end.
    text = text.replace("\x07", "\n").replace("\r", "\n").replace("\x0b", "\n")
    return re.sub(r"\n{2,}", "\n", text)


def parse_entries(text: str) -> tuple[list[dict], list[dict]]:
    """Split raw text into chapter list and article entries.

    An article STARTS only where its label begins a line AND that label has
    not been captured before — later mentions of 第X条 inside other articles'
    body are cross-references, and duplicate labels come from TOC-like lines.
    """
    lines = text.split("\n")

    chapters: list[dict] = []
    seen_chapters: set[str] = set()
    for ln in lines:
        m = _CHAPTER_RE.match(ln.strip())
        if m and m.group(1) not in seen_chapters:
            seen_chapters.add(m.group(1))
            title = ln.strip().split(maxsplit=1)
            name = title[1].strip() if len(title) > 1 else ""
            chapters.append({
                "no": cn_to_int(m.group(1)),
                "label": f"第{m.group(1)}章",
                "name": name,
            })

    entries: list[dict] = []
    cur_chapter = ""
    seen_articles: set[str] = set()
    buf_label = None
    buf_no = 0
    buf_lines: list[str] = []

    def flush():
        nonlocal buf_label, buf_no, buf_lines
        if buf_label is not None:
            body = "\n".join(buf_lines).strip()
            entries.append({
                "entry_id": f"{SOURCE_ID}-{buf_no}",
                "source_id": SOURCE_ID,
                "chapter": cur_chapter,
                "article_label": f"第{buf_label}条",
                "no": buf_no,
                "text": body,
            })
        buf_label, buf_no, buf_lines = None, 0, []

    prev_chapter_seen: set[str] = set()
    for ln in lines:
        stripped = ln.strip()
        cm = _CHAPTER_RE.match(stripped)
        if cm and cm.group(1) not in prev_chapter_seen:
            prev_chapter_seen.add(cm.group(1))
            flush()
            cur_chapter = f"第{cm.group(1)}章"
            continue
        am = _ARTICLE_RE.match(stripped)
        if am and am.group(1) not in seen_articles:
            seen_articles.add(am.group(1))
            flush()
            buf_label = am.group(1)
            buf_no = cn_to_int(am.group(1))
            rest = stripped[_ARTICLE_RE.match(stripped).end():].strip()
            if rest:
                buf_lines.append(rest)
            continue
        if buf_label is not None:
            buf_lines.append(stripped)
    flush()
    return chapters, entries


# ── 多源语料：core/kb/data/raw/*.md ──────────────────────────────────
_HEADER_KEYS = (
    "source_id", "title", "effective", "promulgated", "authority",
    "document_no", "origin_url", "text_kind", "lang", "license",
    "retrieved_at", "style",
)

_LABEL_RE = re.compile(r"^@@\s+(.+?)\s*$")
_HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")


def parse_raw_md(path: Path) -> dict:
    """Parse one raw source file into the same payload shape as gmp2010.

    Header is a ``---`` fenced block of ``key: value`` lines. Body uses
    ``## 章节`` headings plus either ``第X条`` article markers (style=articles,
    the default) or ``@@ 标签`` entry markers (style=labeled).
    """
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.M)
    if len(parts) < 3:
        raise ValueError(f"{path.name}: missing --- fenced provenance header")
    header: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        header[k.strip()] = v.strip()
    body = parts[2]

    missing = [k for k in _HEADER_KEYS if k != "style" and not header.get(k)]
    if missing:
        raise ValueError(f"{path.name}: header missing {missing}")

    source_id = header["source_id"]
    style = header.get("style", "articles")
    text_kind = header["text_kind"]
    lang = header["lang"]

    chapters: list[dict] = []
    entries: list[dict] = []
    chapter = ""
    cur: dict | None = None  # {"entry": {...}, "buf": [str, ...]}

    def flush():
        nonlocal cur
        if cur is not None:
            entry = cur["entry"]
            entry["text"] = re.sub(
                r"\n{2,}", "\n", "\n".join(cur["buf"])
            ).strip()
            entries.append(entry)
        cur = None

    def start(entry: dict, first: str = ""):
        nonlocal cur
        cur = {"entry": entry, "buf": [first] if first else []}

    seen_chapters: set[str] = set()
    seen_articles: set[str] = set()

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if cur is not None:
                cur["buf"].append("")
            continue
        hm = _HEAD_RE.match(line)
        if hm:
            flush()
            chapter = hm.group(1).strip()
            cm = _CHAPTER_RE.match(chapter)
            if cm and cm.group(1) not in seen_chapters:
                seen_chapters.add(cm.group(1))
                rest = chapter.split(maxsplit=1)
                chapters.append({
                    "no": cn_to_int(cm.group(1)),
                    "label": f"第{cm.group(1)}章",
                    "name": rest[1].strip() if len(rest) > 1 else "",
                })
            continue
        if line.startswith("# "):
            continue

        if style == "labeled":
            lm = _LABEL_RE.match(line)
            if lm:
                flush()
                start({
                    "entry_id": f"{source_id}-{len(entries) + 1:03d}",
                    "source_id": source_id,
                    "chapter": chapter,
                    "article_label": lm.group(1),
                    "no": len(entries) + 1,
                    "text_kind": text_kind,
                    "lang": lang,
                })
                continue
            if cur is not None:
                cur["buf"].append(line)
            continue

        # style == "articles"
        am = _ARTICLE_RE.match(line)
        if am and am.group(1) not in seen_articles:
            seen_articles.add(am.group(1))
            flush()
            no = cn_to_int(am.group(1))
            start({
                "entry_id": f"{source_id}-{no}",
                "source_id": source_id,
                "chapter": chapter,
                "article_label": f"第{am.group(1)}条",
                "no": no,
                "text_kind": text_kind,
                "lang": lang,
            }, first=line[am.end():].strip())
            continue
        if cur is not None:
            cur["buf"].append(line)

    flush()

    return {
        "source_id": source_id,
        "title": header["title"],
        "effective": header["effective"],
        "promulgated": header.get("promulgated", ""),
        "authority": header.get("authority", ""),
        "document_no": header.get("document_no", ""),
        "origin_url": header.get("origin_url", ""),
        "text_kind": text_kind,
        "lang": lang,
        "license": header.get("license", ""),
        "retrieved_at": header.get("retrieved_at", ""),
        "chapters": chapters,
        "entries": entries,
    }


def build_raw_sources() -> list[Path]:
    """Build one JSON per core/kb/data/raw/*.md. Returns written paths."""
    written: list[Path] = []
    for raw in sorted(RAW_DIR.glob("*.md")):
        payload = parse_raw_md(raw)
        out = DATA_DIR / f"{payload['source_id']}.json"
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"[seed-kb] built {out.name}: "
              f"entries={len(payload['entries'])} kind={payload['text_kind']}")
        written.append(out)
    return written


# ── 校验 ────────────────────────────────────────────────────────────
def verify_payload(payload: dict) -> list[str]:
    """Generic structural verification, parameterised by _EXPECT.

    Invariants that must hold for every source:
    - entry_id unique; non-empty text; source_id present and consistent
    - article-style sources have ascending, gap-free numeric `no`
    - the declared chapter count and entry-count band match _EXPECT
    """
    problems: list[str] = []
    sid = payload.get("source_id", "")
    en = payload.get("entries", [])
    ch = payload.get("chapters", [])

    if not sid:
        problems.append("missing source_id")
    if not payload.get("title"):
        problems.append("missing title")

    ids = [e.get("entry_id") for e in en]
    if len(set(ids)) != len(ids):
        problems.append("duplicate entry ids")
    bad_src = [e.get("entry_id") for e in en if e.get("source_id") != sid]
    if bad_src:
        problems.append(f"entries with wrong source_id: {bad_src[:3]}")

    empty = [e.get("entry_id") for e in en if len(e.get("text", "")) < 10]
    if empty:
        problems.append(f"suspiciously short entries: {empty[:5]}")

    nos = [e.get("no") for e in en]
    if nos and all(isinstance(n, int) for n in nos):
        if nos != sorted(nos):
            problems.append("entry numbers not ascending")
        # 修掉"零"字符类缺陷后，gmp2010 正文应为 1..313 连续；仍允许
        # 合法缺号（其他源可能省略交叉引用条），截断由下面 _EXPECT 的
        # 条目数区间兜底。

    exp = _EXPECT.get(sid)
    if exp is None:
        problems.append(f"source not registered in _EXPECT: {sid}")
    else:
        exp_ch, lo, hi = exp
        if exp_ch and len(ch) != exp_ch:
            problems.append(f"chapters={len(ch)} expected {exp_ch}")
        if not (lo <= len(en) <= hi):
            problems.append(f"entries={len(en)} expected {lo}..{hi}")

    return problems


def verify_all() -> int:
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        print("[seed-kb] no source JSON found")
        return 1
    rc = 0
    total = 0
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        problems = verify_payload(payload)
        n = len(payload.get("entries", []))
        total += n
        status = "OK" if not problems else "FAIL"
        print(f"[seed-kb][{status}] {f.name}: "
              f"source={payload.get('source_id')} entries={n} "
              f"chars={sum(len(e.get('text', '')) for e in payload.get('entries', []))}")
        for p in problems:
            print(f"[seed-kb][VERIFY-FAIL] {f.name}: {p}")
            rc = 1
    print(f"[seed-kb] sources={len(files)} entries_total={total}")
    # 多源门禁（T5.1）：≥6 源
    if len(files) < 6:
        print(f"[seed-kb][VERIFY-FAIL] only {len(files)} sources, need >= 6")
        rc = 1
    return rc


def _verify_gmp2010_specific() -> list[str]:
    """The one content-level check that predates the generic verifier."""
    payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    art151 = next((e for e in payload["entries"] if e["no"] == 151), None)
    if not art151 or "文件" not in art151["text"]:
        return ["article 151 missing or lacks 文件 keyword"]
    return []


def main() -> int:
    args = sys.argv[1:]

    if "--verify" in args:
        rc = verify_all()
        if OUT_PATH.exists():
            for p in _verify_gmp2010_specific():
                print(f"[seed-kb][VERIFY-FAIL] gmp2010: {p}")
                rc = 1
        return rc

    if "--from-raw" in args:
        build_raw_sources()
        return verify_all()

    # default: build everything buildable
    if DOC_PATH.exists():
        print("[seed-kb] extracting gmp2010 via Word COM ...")
        text = extract_text_via_com(DOC_PATH)
        chapters, entries = parse_entries(text)
        payload = {
            "source_id": SOURCE_ID,
            "title": SOURCE_TITLE,
            "effective": "2011-03-01",
            "promulgated": "2011-01-17",
            "authority": "卫生部",
            "document_no": "卫生部令第79号",
            "origin_url": "https://www.nmpa.gov.cn/",
            "text_kind": "original",
            "lang": "zh",
            "retrieved_at": "2026-09-11",
            "chapters": chapters,
            "entries": entries,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"[seed-kb] wrote {OUT_PATH}")
    else:
        print(f"[seed-kb] skip gmp2010 (source doc missing: {DOC_PATH})")

    build_raw_sources()
    return verify_all()


if __name__ == "__main__":
    sys.exit(main())
