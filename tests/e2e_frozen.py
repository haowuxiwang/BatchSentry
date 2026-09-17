"""Frozen build e2e smoke tests."""
import subprocess, time, requests, sys, os, json, signal, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.e2e_proc import (  # noqa: E402
    EXE_ENV, LLM_KEY_ENV, llm_key, llm_provider, resolve_exe, spawn_server,
    stop_server,
)

# 被测产物：默认 PyInstaller 的直接产物；用 PBC_E2E_EXE 指向 Electron 打包后
# **内嵌**的那份副本（dist-electron*/win-unpacked/resources/pbc-server），
# 那才是用户双击 BatchSentry.exe 时实际运行的东西。
EXE = resolve_exe()
BASE = "http://127.0.0.1:58765"
APPDATA = os.path.join(os.environ["TEMP"], "pbc-e2e-frozen")
RESULTS = []

def ok(name, detail=""):
    RESULTS.append(("PASS", name, detail))
    print(f"  PASS  {name} {detail}")

def fail(name, detail=""):
    RESULTS.append(("FAIL", name, detail))
    print(f"  FAIL  {name} {detail}")

def section(title):
    print(f"\n=== {title} ===")

# --- Start server ---
# 关键：stdout/stderr 落日志文件（不得用未排空的 PIPE —— 服务端日志写满
# 管道缓冲后子进程阻塞在 write，事件循环停摆，后续请求全超时）。
print(f"Starting frozen pbc-server.exe ...\n  target = {EXE}")
os.makedirs(os.path.join(APPDATA, "PBC"), exist_ok=True)
env = os.environ.copy()
env["APPDATA"] = APPDATA
proc, _logf = spawn_server(
    [EXE], env=env,
    log_path=os.path.join(APPDATA, "PBC", "frozen-e2e-server.log"),
)
time.sleep(8)

try:
    # 1. Health check
    section("Health")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        data = r.json()
        assert r.status_code == 200, f"status={r.status_code}"
        assert data["status"] == "ok", f"status={data['status']}"
        # 版本必须与唯一真值 main.APP_VERSION 一致（禁止硬编码 —— 硬编码会在
        # 升版本时静默失配，把"包内版本已更新"的验证变成假通过）。
        # 冻结包从 exe 内取名，故用源码侧真值对齐（同一次发布流水线）。
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from main import APP_VERSION as _EXPECT
        assert data["version"] == _EXPECT, (
            f"version={data['version']} 期望 {_EXPECT}"
        )
        ok("health", f"v{data['version']}")
    except Exception as e:
        fail("health", str(e))

    # 1.5 看门狗自述 + 阈值不变式（**在产物上**复核，不硬编码任何常量）
    #
    # 为什么必须在这里做：看门狗阈值是"停滞后杀任务"的唯一依据，而它一旦
    # 低于它所覆盖的上游封顶，就会**抢在上游超时之前**误报（把"上游还在
    # 正常等待"判成卡死）。源码侧有单测，但产物里跑的是**冻结的那份代码** ——
    # 只有让产物自己报出判定口径，才能证明"要发出去的这个包"里不变式成立。
    # 自述字段：stall_limits_s / ocr_upstream_cap_s / cpu_task_cap_s /
    # rotation_silence_bound_s（#120 引入的旋转补救静默上界）。
    section("Watchdog self-report (threshold invariant)")
    try:
        r = requests.get(f"{BASE}/api/health/watchdog", timeout=5)
        assert r.status_code == 200, f"status={r.status_code}"
        wd = r.json()
        limits = wd["stall_limits_s"]
        ocr_cap = float(wd["ocr_upstream_cap_s"])
        cpu_cap = float(wd["cpu_task_cap_s"])
        rot_bound = float(wd["rotation_silence_bound_s"])
        # 不变式①：OCR 基准 ≥ 单次轮询封顶（否则上游正常等待会被判停滞）
        assert limits["ocr_running"] >= ocr_cap, (
            f"ocr_running={limits['ocr_running']} < ocr_upstream_cap_s={ocr_cap}"
        )
        # 不变式②：OCR 基准 ≥ 旋转补救静默上界（#120；同上道理）
        assert limits["ocr_running"] >= rot_bound, (
            f"ocr_running={limits['ocr_running']} < rotation_silence_bound_s={rot_bound}"
        )
        # 不变式③：cancelling 基准 ≥ CPU 任务封顶
        assert limits["cancelling"] >= cpu_cap, (
            f"cancelling={limits['cancelling']} < cpu_task_cap_s={cpu_cap}"
        )
        # 巡检存活证据：last_scan_at 必须最终被写上一次（证明后台巡检任务
        # 在**这个冻结包里**真的起来了 —— 一个没启动的循环永远不会写它）。
        # ⚠️ 不能启动后立刻断言：循环是"先等一个 interval 再扫"，故头 60s 内
        # 本就为 null。等待预算由**自述的 interval_s** 派生（不硬编码 60）。
        interval = float(wd.get("interval_s") or 60.0)
        budget = interval + 30.0
        deadline = time.time() + budget
        while not wd.get("last_scan_at") and time.time() < deadline:
            time.sleep(2)
            wd = requests.get(f"{BASE}/api/health/watchdog", timeout=5).json()
        assert wd.get("last_scan_at"), (
            f"{budget:.0f}s 内 last_scan_at 仍为空 —— 巡检循环未运行"
        )
        ok("watchdog_invariants",
           f"ocr_running={limits['ocr_running']} >= "
           f"max(cap={ocr_cap}, rot_bound={rot_bound})"
           f"; cancelling={limits['cancelling']} >= cpu_cap={cpu_cap}"
           f"; scanned@t+{interval:.0f}s")
    except Exception as e:
        fail("watchdog_invariants", str(e))

    # 2. Upload page served
    section("Pages")
    try:
        r = requests.get(f"{BASE}/", timeout=5)
        assert r.status_code == 200
        assert "BatchSentry" in r.text or "upload" in r.text.lower()
        ok("upload_page")
    except Exception as e:
        fail("upload_page", str(e))

    try:
        r = requests.get(f"{BASE}/settings", timeout=5)
        assert r.status_code == 200
        ok("settings_page")
    except Exception as e:
        fail("settings_page", str(e))

    # 3. Settings API
    section("Settings API")
    try:
        r = requests.get(f"{BASE}/api/settings", timeout=5)
        assert r.status_code == 200
        data = r.json()
        ok("settings_get", f"keys={len(data)}")
    except Exception as e:
        fail("settings_get", str(e))

    # 4. Jobs API
    section("Jobs API")
    try:
        r = requests.get(f"{BASE}/api/jobs", timeout=5)
        assert r.status_code == 200
        data = r.json()
        ok("jobs_list", f"count={len(data.get('jobs', data))}")
    except Exception as e:
        fail("jobs_list", str(e))

    # 5. Configure LLM provider
    # 密钥只从环境取（PBC_E2E_DEEPSEEK_KEY），绝不写进仓库。
    # 未提供时如实登记为"未配置"，不伪造通过。
    #
    # ⚠️ 提供方**不得写死**：字段名必须由提供方名派生（f"{prov}_api_key"）。
    # 反例（2026-09-17 实测，本段曾在真实产物上 401）：原先固定写
    #   {"llm_provider": "deepseek", "deepseek_api_key": key}
    # 而注入的是**硅基流动**的 key ⇒ 请求打到 api.deepseek.com 得
    # `401 Authentication Fails, Your api key: ****ucgz is invalid`。
    # 这一错配长期"绿"着，因为样例 PDF 曾是**空白页**：Stage 2 无内容可分析
    # ⇒ 从不调用 LLM ⇒ 401 从未发生。夹具改为含真实文字后**当场暴露** ——
    # 这正是"断言必须能真的失败"为什么必须成立。
    section("Configure LLM")
    _key = llm_key()
    _prov = llm_provider()
    if not _key:
        print(f"    [WARN] 未设置 {LLM_KEY_ENV} —— 跳过 LLM 配置，"
              f"下游流水线将走降级路径（不是缺陷）")
    try:
        r = requests.post(f"{BASE}/api/settings", json={
            "llm_provider": _prov,
            f"{_prov}_api_key": _key,
        }, timeout=5)
        print(f"    POST settings status={r.status_code} body={r.text[:300]}")
        # Verify GET returns the key for THAT provider
        r2 = requests.get(f"{BASE}/api/settings", timeout=5)
        settings = r2.json()
        llm = settings.get("llm", {})
        provider = llm.get("provider") or llm.get("active_provider")
        providers_list = llm.get("providers", [])
        p = next((x for x in providers_list if x.get("name") == _prov), {})
        p_configured = p.get("configured", False)
        p_key_masked = p.get("api_key", "")
        ok("settings_configure_llm",
           f"provider={provider} {_prov}_configured={p_configured} key={p_key_masked}")
        # 判别性前置：密钥非空却"没配上"或"活动提供方不是它" ⇒ 后续任何
        # 结论都无意义（会被误报成产品缺陷），故当场 FAIL。
        if _key:
            if provider and provider != _prov:
                fail("settings_llm_provider_matches_key",
                     f"活动提供方={provider}，密钥却是给 {_prov} 的 —— 配置未生效")
            elif not p_configured:
                fail("settings_llm_provider_matches_key",
                     f"{_prov} 收到密钥后仍 configured=False（密钥被拒写？）")
            else:
                ok("settings_llm_provider_matches_key", _prov)
    except Exception as e:
        fail("settings_configure_llm", str(e))

    # 5b. Configure OCR backend —— 只配 LLM 不配 OCR 是**假的绿**：
    # 实测（2026-09-16）Paddle 的 api_url 为空时提交即失败
    # （`Invalid URL '': No scheme supplied`），pipeline 一路走到 error，
    # 而冒烟此前把 error 也判 PASS。凭据来自环境（PBC_E2E_*），绝不入库。
    section("Configure OCR")
    _paddle = os.environ.get("PBC_E2E_PADDLE_TOKEN", "")
    _mineru = os.environ.get("PBC_E2E_MINERU_TOKEN", "")
    OCR_CONFIGURED = bool(_paddle or _mineru)
    if OCR_CONFIGURED:
        payload = {"ocr_backend": "paddle" if _paddle else "mineru"}
        if _paddle:
            payload["paddle_ocr_api_url"] = os.environ.get(
                "PBC_E2E_PADDLE_URL",
                "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs")
            payload["paddle_ocr_token"] = _paddle
            payload["paddle_ocr_model"] = os.environ.get(
                "PBC_E2E_PADDLE_MODEL", "PaddleOCR-VL-1.6")
        if _mineru:
            payload["mineru_token"] = _mineru
        try:
            r = requests.post(f"{BASE}/api/settings", json=payload, timeout=5)
            assert r.status_code == 200, r.text[:200]
            ok("settings_configure_ocr", f"backend={payload['ocr_backend']}")
        except Exception as e:
            fail("settings_configure_ocr", str(e))
    else:
        print("    [WARN] 未提供 PBC_E2E_PADDLE_TOKEN / PBC_E2E_MINERU_TOKEN —— "
              "OCR 未配置，pipeline 无法跑通（下游将如实标注为降级，不冒充 PASS）")

    # 6. PDF upload —— 样例必须**含真实文字**，不能是空白页。
    # 为什么（2026-09-17 实测，两处盲区同根）：
    #   (a) 空白页 ⇒ 每页被判为"空/稀疏" ⇒ 触发**旋转自愈** ⇒ 冒烟的时长与结果
    #       被**上游 Paddle 状况**支配。实测同一样例：paddle 健康时 2m54s 通过；
    #       拥塞时 >5min 仍停在 `Rotation probe upstream congestion … backing off`
    #       （#120 的退避阶梯本身工作正常，但它让冒烟变得**不可重复**）。
    #   (b) 空白页 ⇒ Stage 2 **无内容可分析** ⇒ 下面 `error and OCR_CONFIGURED`
    #       分支**永不可达** —— "LLM 凭据失效"在冒烟里**报不出来**（实测：用已失效的
    #       key 跑，仍得 status=review）。这违反项目自己的规矩"断言必须能真的失败"。
    # 用 PyMuPDF 现生成一页带文字的小 PDF（项目已依赖 fitz），且**每次都重写** ——
    # 此前是 `if not os.path.exists`，陈旧的空白件会被一直沿用。
    section("Upload")
    test_pdf = os.path.join(os.environ["TEMP"], "e2e-test-text.pdf")
    try:
        import fitz  # PyMuPDF（项目依赖，随包分发）
        _doc = fitz.open()
        _pg = _doc.new_page()                       # A4
        _pg.insert_text((72, 100),
                        "Batch Production Record   batch no 72408119", fontsize=12)
        _pg.insert_text((72, 130),
                        "Step 1 Charge API  spec 10.0 mg  actual 9.8 mg", fontsize=12)
        _pg.insert_text((72, 160),
                        "Operator ZHANG   Reviewed by LI   2026-09-17", fontsize=12)
        _doc.save(test_pdf)
        _doc.close()
        print(f"    smoke fixture = {test_pdf} "
              f"({os.path.getsize(test_pdf)} B, 含真实文字)")
    except Exception as e:
        fail("smoke_pdf_fixture", f"生成含文字样例失败：{e}（下游上传将一并失败）")
    try:
        with open(test_pdf, "rb") as f:
            r = requests.post(f"{BASE}/api/jobs?force=1", files={"file": ("test.pdf", f, "application/pdf")}, timeout=10)
        assert r.status_code == 200, f"status={r.status_code} body={r.text[:200]}"
        data = r.json()
        job_id = data.get("job_id") or data.get("id")
        assert job_id, f"no job_id in response: {data}"
        ok("upload", f"job_id={job_id}")
    except Exception as e:
        fail("upload", str(e))
        job_id = None

    # 7. Job status
    if job_id:
        section("Job Status")
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            assert r.status_code == 200
            data = r.json()
            ok("job_status", f"status={data.get('status')}")
        except Exception as e:
            fail("job_status", str(e))

        # 8. Wait for pipeline and check review page
        # 断言强度取决于**环境是否具备跑通条件**（这是本用例的核心纪律）：
        #   - 已配 OCR 凭据 → 必须走成功路径（review/partial_review）；
        #     出现 error 即真实缺陷 → FAIL 并带出 error_message。
        #   - 未配凭据 → 如实标注"降级"，**绝不冒充 PASS**。
        #     历史缺陷：此前 `status in (..., "error", ...)` 直接 ok() ——
        #     "pipeline 完全跑不起来"在冒烟里也是绿的（与 importorskip 同源的
        #     "静默成功"）。
        section("Pipeline -> Review")
        terminal, err_msg = "", ""
        # 终态等待预算**派生**而非写死（`docs/RUNTIME_WATCHDOG.md` §8.4 同源教训：
        # 写死单值会因上游排队把真实长跑误判为失败）。组成 = 一次 1 页轮询封顶
        # （`poll_timeout_for_pages(1)`，当前 630s）+ 分析阶段基线；env 可覆盖。
        # 2026-09-17 实测：上游 Paddle 排队时，单页 2 分钟仍在 `ocr_running`
        # —— 旧的写死 60s 当场把一次**完全正常**的作业判成 FAIL。
        from core.ocr_client import poll_timeout_for_pages
        _budget_s = float(os.environ.get(
            "E2E_FROZEN_TERMINAL_TIMEOUT", poll_timeout_for_pages(1) + 300))
        _deadline = time.time() + _budget_s
        while time.time() < _deadline:
            time.sleep(2)
            try:
                r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
                data = r.json()
                terminal = data.get("status", "")
                err_msg = data.get("error_message") or ""
                if terminal in ("review", "partial_review", "error", "cancelled"):
                    break
            except Exception:
                pass
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            data = r.json()
            terminal = data.get("status", terminal)
            err_msg = data.get("error_message") or err_msg
            if terminal in ("review", "partial_review"):
                ok("pipeline_terminal", f"status={terminal}")
            elif terminal == "error" and OCR_CONFIGURED:
                fail("pipeline_terminal",
                     f"status=error（已配 OCR 仍失败 — 真实缺陷）"
                     f"error_message={err_msg[:200]}")
            elif terminal in ("error", "cancelled"):
                print(f"    [SKIP] pipeline_terminal status={terminal} —— 环境未配 OCR "
                      f"凭据，属预期的降级路径（error_message={err_msg[:160]}）")
            else:
                fail("pipeline_terminal",
                     f"未在 {_budget_s:.0f}s 内到达终态: status={terminal!r}")
        except Exception as e:
            fail("pipeline_terminal", str(e))

        # 9. Review page
        try:
            r = requests.get(f"{BASE}/jobs/{job_id}/review", timeout=5)
            assert r.status_code == 200
            ok("review_page")
        except Exception as e:
            fail("review_page", str(e))

        # 10. Findings API
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/findings", timeout=5)
            assert r.status_code == 200
            data = r.json()
            count = len(data) if isinstance(data, list) else len(data.get("findings", []))
            ok("findings_api", f"count={count}")
        except Exception as e:
            fail("findings_api", str(e))

        # 11. Page image
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/page/1", timeout=10)
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("image/")
            ok("page_image", f"size={len(r.content)}")
        except Exception as e:
            fail("page_image", str(e))

        # 12. Report
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/report.md", timeout=5)
            assert r.status_code == 200
            ok("report_md", f"len={len(r.text)}")
        except Exception as e:
            fail("report_md", str(e))

    # 13. Static assets
    section("Static Assets")
    try:
        r = requests.get(f"{BASE}/static/app.css", timeout=5)
        assert r.status_code == 200
        ok("app_css", f"len={len(r.text)}")
    except Exception as e:
        fail("app_css", str(e))

    try:
        r = requests.get(f"{BASE}/static/review.js", timeout=5)
        assert r.status_code == 200
        ok("review_js", f"len={len(r.text)}")
    except Exception as e:
        fail("review_js", str(e))

    # 13b. #127 的可见性修复必须**在产物里**。
    #     这才是"验产物而非验源码"的实质：修复有没有到达用户手上，是一个
    #     **分发事实**，源码树干净并不蕴含它。静态资源由冻结包直接提供，
    #     故这几条断言证明的正是"要分发的那份东西带着修复"。
    section("Frontend #127 Visibility (shipped bundle)")
    try:
        up = requests.get(f"{BASE}/static/upload.js", timeout=5).text
        # 成功色分支不得包含 partial_review（它定义上就不是成功态）
        success_branch = re.findall(
            r'if \(([^)]+)\)\s*return "(bg-[a-z-]+)"', up
        )
        bad_success = [
            c for c, ret in success_branch
            if ret == "bg-success" and "partial_review" in c
        ]
        checks = {
            "partial_review_not_green": not bad_success and "bg-warning" in up,
            "failed_pages_rendered": "job.failed_pages" in up,
            "reason_shown_for_partial_review": (
                '(st === "error" || st === "partial_review")' in up
                or '["error", "partial_review"].includes(st)' in up
            ),
        }
        missing = [k for k, v in checks.items() if not v]
        assert not missing, f"产物内缺 #127 修复标记: {missing}"
        ok("upload_js_127", "非绿点 / 显失败页 / 显原因")
    except Exception as e:
        fail("upload_js_127", str(e))

    try:
        rj = requests.get(f"{BASE}/static/review.js", timeout=5).text
        assert "structured._error" in rj, "review.js 未消费 structured._error"
        ok("review_js_127", "页内横幅显真实原因")
    except Exception as e:
        fail("review_js_127", str(e))

    # 14. API docs
    section("API Docs")
    try:
        r = requests.get(f"{BASE}/docs", timeout=5)
        assert r.status_code == 200
        ok("swagger_ui")
    except Exception as e:
        fail("swagger_ui", str(e))

finally:
    stop_server(proc, _logf, timeout=5)

section("Results")
passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
print(f"\n{'='*50}")
print(f"target: {EXE}")
print(f"Total: {passed} passed, {failed} failed")
for s, name, detail in RESULTS:
    marker = "OK" if s == "PASS" else "XX"
    print(f"  {marker} {name}: {detail}")
print(f"{'='*50}")
sys.exit(1 if failed else 0)
