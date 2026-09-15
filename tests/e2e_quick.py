"""Quick e2e smoke test - dev server, no pipeline wait."""
import subprocess, time, os, sys, requests, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.e2e_proc import (  # noqa: E402
    LLM_KEY_ENV, REPO_ROOT, llm_key, spawn_server, stop_server,
)

BASE = "http://127.0.0.1:8000"
RESULTS = []

def ok(name, detail=""):
    RESULTS.append(("PASS", name, detail))
    print(f"  OK  {name} {detail}")

def fail(name, detail=""):
    RESULTS.append(("FAIL", name, detail))
    print(f"  XX  {name} {detail}")

# Start dev server（stdout 落日志文件，勿用未排空的 PIPE —— 见 tests/e2e_proc.py）
print("Starting dev server...")
proc, _logf = spawn_server(
    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
    cwd=str(REPO_ROOT),
    log_path=os.path.join(os.environ.get("TEMP", "."), "pbc-e2e-quick-server.log"),
)
time.sleep(5)

try:
    section = lambda t: print(f"\n=== {t} ===")
    
    section("Health")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        ok("health", f"{r.json()}")
    except Exception as e:
        fail("health", str(e))

    section("Pages")
    for name, path in [("upload", "/"), ("settings", "/settings"), ("review_404", "/jobs/nope/review")]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=5)
            ok(name, f"{r.status_code}")
        except Exception as e:
            fail(name, str(e))

    section("Settings API")
    try:
        r = requests.get(f"{BASE}/api/settings", timeout=5)
        ok("settings_get", f"keys={len(r.json())}")
    except Exception as e:
        fail("settings_get", str(e))

    section("Configure LLM")
    _key = llm_key()
    if not _key:
        print(f"    [WARN] 未设置 {LLM_KEY_ENV} —— LLM 不配置（不是缺陷）")
    try:
        r = requests.post(f"{BASE}/api/settings", json={
            "llm_provider": "deepseek",
            "deepseek_api_key": _key,
        }, timeout=5)
        ok("settings_post", f"{r.status_code}")
    except Exception as e:
        fail("settings_post", str(e))

    section("Upload (1-page PDF)")
    test_pdf = os.path.join(os.environ["TEMP"], "e2e-quick.pdf")
    with open(test_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n4 0 obj<</Length 44>>stream\nBT /F1 24 Tf 100 700 Td (E2E Test) Tj ET\nendstream\nendobj\n5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\nxref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000266 00000 n \n0000000360 00000 n \ntrailer<</Size 6/Root 1 0 R>>\nstartxref\n429\n%%EOF")
    
    try:
        with open(test_pdf, "rb") as f:
            r = requests.post(f"{BASE}/api/jobs?force=1", files={"file": ("e2e.pdf", f, "application/pdf")}, timeout=10)
        ok("upload", f"{r.status_code} {r.json()}")
        job_id = r.json().get("job_id") or r.json().get("id")
    except Exception as e:
        fail("upload", str(e))
        job_id = None

    if job_id:
        section("Job Status")
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            ok("job_status", f"{r.json().get('status')}")
        except Exception as e:
            fail("job_status", str(e))

        section("Wait for pipeline (max 90s)")
        final_status = "unknown"
        for i in range(45):
            time.sleep(2)
            try:
                r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10)
                status = r.json().get("status", "")
                if status in ("review", "partial_review", "error", "cancelled"):
                    final_status = status
                    print(f"  Pipeline done at {i*2}s: {status}")
                    break
            except:
                pass
        ok("pipeline_terminal", final_status)

        section("Review + Findings + Image + Report")
        try:
            r = requests.get(f"{BASE}/jobs/{job_id}/review", timeout=5)
            ok("review_page", f"{r.status_code}")
        except Exception as e:
            fail("review_page", str(e))
        
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/findings", timeout=5)
            data = r.json()
            count = len(data) if isinstance(data, list) else len(data.get("findings", []))
            ok("findings", f"count={count}")
        except Exception as e:
            fail("findings", str(e))
        
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/page/1", timeout=10)
            ok("page_image", f"{r.status_code} type={r.headers.get('content-type','?')[:20]} size={len(r.content)}")
        except Exception as e:
            fail("page_image", str(e))
        
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/report.md", timeout=5)
            ok("report", f"{r.status_code} len={len(r.text)}")
        except Exception as e:
            fail("report", str(e))

    section("Static + Docs")
    for name, path in [("app.css", "/static/app.css"), ("review.js", "/static/review.js"), ("swagger", "/docs")]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=5)
            ok(name, f"{r.status_code} len={len(r.content)}")
        except Exception as e:
            fail(name, str(e))

finally:
    stop_server(proc, _logf, timeout=5)

print("\n" + "="*50)
passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
print(f"Total: {passed} passed, {failed} failed")
for s, name, detail in RESULTS:
    marker = "OK" if s == "PASS" else "XX"
    print(f"  {marker} {name}: {detail}")
print("="*50)
sys.exit(1 if failed else 0)
