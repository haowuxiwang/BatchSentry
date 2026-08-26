p = "tests/unit/test_pipeline.py"
lines = open(p, encoding="utf-8").read().split("\n")

# locate the after_ocr_done test's predicate (threshold form)
idx_pred = None
for i, ln in enumerate(lines):
    if 'return calls["n"] > 3' in ln:
        idx_pred = i
        break
assert idx_pred is not None, "threshold predicate not found"

# find its enclosing async def fake_cancelled start (search upward)
start = idx_pred
while start > 0 and "async def fake_cancelled" not in lines[start]:
    start -= 1
assert "async def fake_cancelled" in lines[start]

# also find the calls dict just above it
calls_idx = None
for j in range(start - 1, max(0, start - 5), -1):
    if 'calls = {"n": 0}' in lines[j]:
        calls_idx = j
        break
assert calls_idx is not None

replacement = [
    '        done = {"ocr": False}',
    "",
    "        def fake_run_sliced_mark(pdf_path, slice_pages, on_batch,",
    "                                 progress_cb, job_id=None):",
    "            res = fake_run_sliced(pdf_path, slice_pages, on_batch,",
    "                                  progress_cb, job_id=job_id)",
    '            done["ocr"] = True  # OCR 全部返回后再取消',
    "            return res",
    "",
    "        async def fake_cancelled(*a, **kw):",
    '            return done["ocr"]',
]
new_lines = lines[:calls_idx] + replacement + lines[idx_pred + 1:]
s = "\n".join(new_lines)

# point this test's patch at the marker wrapper (first usage AFTER its def)
anchor_use = "side_effect=fake_run_sliced\n"
i_def = s.find("def fake_run_sliced_mark")
i_use = s.find(anchor_use, i_def)
assert i_use != -1, "usage not found"
s = s[:i_use] + "side_effect=fake_run_sliced_mark\n" + s[i_use + len(anchor_use):]

open(p, "w", encoding="utf-8", newline="").write(s)
print("after_ocr_done rewritten with marker pattern")
