"""Seed the GMP2010 knowledge base from docs/2010版GMP.doc (dev-time only).

One-time build script — NOT part of the runtime. Extracts text via Word COM
(pywin32), splits into chapter/article entries by regex (the document uses
"正文" style throughout, so structure comes from 第X章 / 第X条 markers),
converts Chinese numerals to arabic, and writes core/kb/data/gmp2010.json.

The derived JSON is committed to git (public government regulation text);
the binary .doc itself is not tracked.

Usage:
    python scripts/seed_kb.py            # extract + write JSON + verify
    python scripts/seed_kb.py --verify   # verify existing JSON only (no COM)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "2010版GMP.doc"
OUT_PATH = ROOT / "core" / "kb" / "data" / "gmp2010.json"

SOURCE_ID = "gmp2010"
SOURCE_TITLE = "药品生产质量管理规范（2010年修订）"

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100}


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


_CHAPTER_RE = re.compile(r"^第([一二三四五六七八九十百]+)章")
_ARTICLE_RE = re.compile(r"^第([一二三四五六七八九十百]+)条")


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


def verify(payload: dict) -> list[str]:
    problems = []
    ch = payload["chapters"]
    en = payload["entries"]
    if len(ch) != 14:
        problems.append(f"chapters={len(ch)} expected 14")
    if not (280 <= len(en) <= 292):
        problems.append(f"entries={len(en)} expected ~286")
    nos = [e["no"] for e in en]
    if nos != sorted(nos):
        problems.append("entry numbers not ascending")
    art151 = next((e for e in en if e["no"] == 151), None)
    if not art151 or not ("文件" in art151["text"]):
        problems.append("article 151 missing or lacks 文件 keyword")
    dupes = {e["entry_id"] for e in en} 
    if len(dupes) != len(en):
        problems.append("duplicate entry ids")
    empty = [e["entry_id"] for e in en if len(e["text"]) < 10]
    if empty:
        problems.append(f"suspiciously short entries: {empty[:5]}")
    return problems


def main() -> int:
    args = sys.argv[1:]
    if "--verify" in args:
        payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    else:
        if not DOC_PATH.exists():
            print(f"[seed-kb] source doc missing: {DOC_PATH}")
            return 2
        print("[seed-kb] extracting via Word COM ...")
        text = extract_text_via_com(DOC_PATH)
        chapters, entries = parse_entries(text)
        payload = {
            "source_id": SOURCE_ID,
            "title": SOURCE_TITLE,
            "effective": "2011-03-01",
            "chapters": chapters,
            "entries": entries,
        }
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"[seed-kb] wrote {OUT_PATH}")

    problems = verify(payload)
    n_chars = sum(len(e["text"]) for e in payload["entries"])
    print(f"[seed-kb] chapters={len(payload['chapters'])} "
          f"entries={len(payload['entries'])} total_chars={n_chars}")
    if problems:
        for p in problems:
            print(f"[seed-kb][VERIFY-FAIL] {p}")
        return 1
    print("[seed-kb] VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
