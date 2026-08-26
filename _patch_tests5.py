p = "tests/unit/test_pipeline.py"
s = open(p, encoding="utf-8").read()

old = '''        calls = {"n": 0}

        async def fake_cancelled(*a, **kw):
            calls["n"] += 1
            # 循环内检查（845）→ False；ocr_done 后检查（858）→ True；
            # 主流程兜底（407）→ True 直接退出。
            return calls["n"] > 1

        orig_backend'''
new = '''        done = {"ocr": False}

        def fake_run_sliced_mark(pdf_path, slice_pages, on_batch,
                                 progress_cb, job_id=None):
            res = fake_run_sliced(pdf_path, slice_pages, on_batch,
                                  progress_cb, job_id=job_id)
            done["ocr"] = True  # OCR 全部返回后再取消
            return res

        async def fake_cancelled(*a, **kw):
            # Stage0 pre/post 检查点新增 → OCR 返回前一律 False；
            # run_ocr_sliced 返回后的检查命中取消（停在 ocr_done）。
            return done["ocr"]

        orig_backend'''
assert old in s, "after_ocr_done anchor missing"
s = s.replace(old, new, 1)

# point this test's patch at the marker wrapper (first usage after def)
anchor_use = "side_effect=fake_run_sliced\n"
i_def = s.find("def fake_run_sliced_mark")
i_use = s.find(anchor_use, i_def)
assert i_use != -1
s = s[:i_use] + "side_effect=fake_run_sliced_mark\n" + s[i_use + len(anchor_use):]
open(p, "w", encoding="utf-8", newline="").write(s)
print("after_ocr_done marker pattern applied")
