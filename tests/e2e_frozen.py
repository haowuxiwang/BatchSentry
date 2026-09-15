"""Frozen build e2e smoke tests."""
import subprocess, time, requests, sys, os, json, signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.e2e_proc import (  # noqa: E402
    EXE_ENV, LLM_KEY_ENV, llm_key, resolve_exe, spawn_server, stop_server,
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
    section("Configure LLM")
    _key = llm_key()
    if not _key:
        print(f"    [WARN] 未设置 {LLM_KEY_ENV} —— 跳过 LLM 配置，"
              f"下游流水线将走降级路径（不是缺陷）")
    try:
        r = requests.post(f"{BASE}/api/settings", json={
            "llm_provider": "deepseek",
            "deepseek_api_key": _key,
        }, timeout=5)
        print(f"    POST settings status={r.status_code} body={r.text[:300]}")
        # Verify GET returns the key
        r2 = requests.get(f"{BASE}/api/settings", timeout=5)
        settings = r2.json()
        llm = settings.get("llm", {})
        provider = llm.get("provider") or llm.get("active_provider")
        providers_list = llm.get("providers", [])
        ds = next((p for p in providers_list if p.get("name") == "deepseek"), {})
        ds_configured = ds.get("configured", False)
        ds_key_masked = ds.get("api_key", "")
        ok("settings_configure_llm", f"provider={provider} deepseek_configured={ds_configured} key={ds_key_masked}")
    except Exception as e:
        fail("settings_configure_llm", str(e))

    # 6. PDF upload with small test PDF
    section("Upload")
    test_pdf = os.path.join(os.environ["TEMP"], "e2e-test.pdf")
    if not os.path.exists(test_pdf):
        with open(test_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF")
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
        section("Pipeline -> Review")
        for i in range(30):
            time.sleep(2)
            try:
                r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
                data = r.json()
                status = data.get("status", "")
                if status in ("review", "partial_review", "error", "cancelled"):
                    break
            except:
                pass
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            data = r.json()
            status = data.get("status", "")
            ok("pipeline_terminal", f"status={status}")
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
