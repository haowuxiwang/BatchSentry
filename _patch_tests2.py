p = "tests/unit/test_pipeline.py"
s = open(p, encoding="utf-8").read()

# 1) after_ocr_done: threshold 1 -> 3 (stage0 pre/post consume two calls)
old1 = 'return calls["n"] > 1'
new1 = 'return calls["n"] > 3'
assert s.count(old1) == 1, f"anchor1 count={s.count(old1)}"
s = s.replace(old1, new1, 1)

# 2) inside_ocr_loop: always-True -> False until first batch persisted
old2 = '''        calls = {"n": 0}

        async def fake_cancelled(*a, **kw):
            calls["n"] += 1
            return True  # 循环内第一次检查即取消'''
new2 = '''        fired = {"batch": False}

        def fake_run_sliced_wrapper(pdf_path, slice_pages, on_batch,
                                    progress_cb, job_id=None):
            res = fake_run_sliced(pdf_path, slice_pages, on_batch,
                                  progress_cb, job_id=job_id)
            fired["batch"] = True  # 首片已落库，之后循环内检查才取消
            return res

        async def fake_cancelled(*a, **kw):
            return fired["batch"]'''
assert old2 in s, "inside-loop anchor missing"
s = s.replace(old2, new2, 1)

old3 = '''            with patch(
                "core.mineru_client.run_ocr_sliced", side_effect=fake_run_sliced
            ), patch(
                "core.pipeline._is_cancelled", new=AsyncMock(side_effect=fake_cancelled)
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 取消时状态停留在 ocr_running（未 transition 至 ocr_done）'''
new3 = '''            with patch(
                "core.mineru_client.run_ocr_sliced",
                side_effect=fake_run_sliced_wrapper,
            ), patch(
                "core.pipeline._is_cancelled", new=AsyncMock(side_effect=fake_cancelled)
            ), patch(
                "core.pipeline.analyze_page", new=AsyncMock(return_value={})
            ), patch(
                "core.pipeline.analyze_cross_page", new=AsyncMock(return_value=[])
            ):
                await run_pipeline(job_id, pdf_path)
        finally:
            pipeline_mod.config["app"].ocr_backend = orig_backend
            pipeline_mod.config["app"].ocr_slices = orig_slices
            pipeline_mod._SLICE_QUEUE_TIMEOUT = orig_timeout

        # 取消时状态停留在 ocr_running（未 transition 至 ocr_done）'''
assert old3 in s, "patch target missing"
s = s.replace(old3, new3, 1)
open(p, "w", encoding="utf-8", newline="").write(s)
print("sliced tests patched")
