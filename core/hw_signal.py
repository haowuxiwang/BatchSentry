"""OCR handwriting structured-signal extraction (Round 7).

MinerU renders low-confidence (likely handwritten) table cells as '###',
which core.mineru_client sanitizes to '[手写内容未识别]'. That marker is the
only cell-level OCR structured signal available end-to-end (content_list_v2
carries no row-level score; PaddleOCR-VL VLM output provides no confidence by
design, per its maintainers; middle.json scores would need extra API params).

This module turns the marker into deterministic value_source evidence without
LLM transcription:

1. **Column signal**: a marker cell inside a pipe/HTML table maps to its column
   header (naive visual index alignment; any colspan/rowspan in the table
   disables column mapping — variable-width cells break index alignment).
2. **Label signal**: a marker cell whose text carries a label prefix
   ('审核意见:[手写内容未识别]') yields the label as a low-confidence token.
   Covers key-value cells that are not inside a header'd table (e.g. GMP
   signature approval lines, where the printed label and the handwritten date
   share one cell).

The rule layer (core.rules.base._backfill_value_source) then forces
value_source='handwritten' for any parameter name / measurement column that
matches a token — overriding even an explicit LLM 'printed' annotation
(machine fact beats model guess; forcing handwritten is the conservative
direction: it only downgrades edge-case severities, never escalates).

Tokens are stored in page results under the internal key
`_ocr_low_conf_cols` (underscore prefix = pipeline-internal, not user-visible
schema) so Stage 3 receives them without re-reading raw page text.
"""
from __future__ import annotations

import re

# Must equal the marker written by core.mineru_client._sanitize_unrecognized_handwriting.
_UNRECOGNIZED_MARKER = "手写内容未识别"

_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S)
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.S)
_CELL_RE = re.compile(r"<(td|th)\b([^>]*)>(.*?)</\1>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# A label immediately before the marker, optionally colon-separated:
# '审核意见:[手写内容未识别]' and '签名[手写内容未识别]' both occur in real
# records (key-value cells vs header'd signature columns). The optional-colon
# form relies on CJK punctuation (，。；) terminating description text so the
# captured run stays label-shaped. Min 2 chars avoids noise tokens ('A:');
# max 40 keeps tokens bounded; char class covers CJK + ASCII alnum + GMP
# label symbols.
_LABEL_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9()（）/·._%+\-]{2,40})[：:]?\s*\[?"
    + _UNRECOGNIZED_MARKER
    + r"\]?"
)

# Separator rows in markdown pipe tables: | --- | :--: |
_PIPE_SEP_CELL_RE = re.compile(r"\s*:?-{2,}:?\s*")
# Pure date/time/number tokens ('2025.01.1', '250101') are not column names.
_PURE_DIGIT_RE = re.compile(r"[\d./\-:年月日\s]+")


def _has_unrecognized_marker(text) -> bool:
    """Cell value carries MinerU's low-confidence handwriting marker."""
    return isinstance(text, str) and _UNRECOGNIZED_MARKER in text


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _clean_token(raw: str) -> str:
    """Normalize a candidate token: strip markdown/tags/whitespace."""
    t = _strip_tags(raw)
    t = t.strip().strip("*`").strip()
    return t.replace(" ", "")


def _valid_token(t: str) -> bool:
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if _PURE_DIGIT_RE.fullmatch(t):
        return False
    if _UNRECOGNIZED_MARKER in t:
        return False
    return True


def _label_before_marker(text: str) -> list[str]:
    """Labels directly preceding a marker occurrence ('审核意见:[标记]')."""
    return [m.group(1) for m in _LABEL_RE.finditer(text)]


def _html_table_marker_cols(table_html: str) -> list[str]:
    """Map marker cells to header names inside one HTML table.

    Any colspan/rowspan anywhere in the table disables column mapping —
    visual column indices cannot be aligned reliably (GMP signature lines are
    exactly such variable-width tables; their marker cells still yield label
    tokens via _label_before_marker).
    """
    cells = _CELL_RE.findall(table_html)
    if not cells:
        return []
    if any("colspan" in attrs or "rowspan" in attrs for _, attrs, _ in cells):
        return []
    rows = _ROW_RE.findall(table_html)
    if not rows:
        return []
    header_cells = _CELL_RE.findall(rows[0])
    headers = [_clean_token(content) for _, _, content in header_cells]
    if not headers:
        return []
    out = []
    # 跳过表头行单元格：标记单元格的列索引从首个数据行算起
    idx = 0
    for _, _, content in cells[len(header_cells):]:
        if _has_unrecognized_marker(content):
            if idx < len(headers) and _valid_token(headers[idx]):
                out.append(headers[idx])
        idx += 1
    return out


def _pipe_table_marker_cols(lines: list[str], start: int) -> tuple[list[str], int]:
    """Map marker cells to header names in a contiguous pipe-table block.

    Returns (tokens, next_line_index). Lines[0] is the header row; separator
    rows (| --- |) are skipped; short rows are aligned by index.
    """
    rows: list[list[str]] = []
    j = start
    while j < len(lines) and lines[j].strip().startswith("|"):
        cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
        rows.append(cells)
        j += 1
    if not rows:
        return [], j
    header = [_clean_token(c) for c in rows[0]]
    out: list[str] = []
    for row in rows[1:]:
        if row and all(_PIPE_SEP_CELL_RE.fullmatch(c) for c in row):
            continue  # separator row ('|---|---|')
        for idx, cell in enumerate(row):
            if _has_unrecognized_marker(cell):
                if idx < len(header) and _valid_token(header[idx]):
                    out.append(header[idx])
    return out, j


def _extract_low_conf_tokens(text) -> list[str]:
    """Extract handwriting-signal tokens from page text, in appearance order.

    Combines label signals (anywhere) and column signals (header'd tables).
    Returns [] when there is no marker or nothing usable — callers degrade to
    the keyword/LLM fallback chain unchanged.
    """
    if not isinstance(text, str) or not text or _UNRECOGNIZED_MARKER not in text:
        return []
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        t = _clean_token(raw)
        if not _valid_token(t):
            return
        # 'QA/生产审核' style labels: both parts are meaningful tokens.
        for part in t.split("/"):
            if _valid_token(part) and part not in seen:
                seen.add(part)
                tokens.append(part)

    for label in _label_before_marker(text):
        _add(label)

    lines = text.splitlines()
    i = 0
    body = "\n".join(lines)
    for table_html in _TABLE_RE.findall(body):
        for token in _html_table_marker_cols(table_html):
            _add(token)
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            cols, i = _pipe_table_marker_cols(lines, i)
            for token in cols:
                _add(token)
        else:
            i += 1
    return tokens