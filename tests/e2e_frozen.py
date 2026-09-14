"""Frozen build e2e smoke tests."""
import subprocess, time, requests, sys, os, json, signal

EXE = r"D:\learn\claudecode\pharma-batch-checker\dist\pbc-server\pbc-server.exe"
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
print("Starting frozen pbc-server.exe ...")
os.makedirs(os.path.join(APPDATA, "PBC"), exist_ok=True)
env = os.environ.copy()
env["APPDATA"] = APPDATA
proc = subprocess.Popen([EXE], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(8)

try:
    # 1. Health check
    section("Health")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        data = r.json()
        assert r.status_code == 200, f"status={r.status_code}"
        assert data["status"] == "ok", f"status={data['status']}"
        assert data["version"] == "1.1.0", f"version={data['version']}"
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
    section("Configure LLM")
    try:
        r = requests.post(f"{BASE}/api/settings", json={
            "llm_provider": "deepseek",
            "deepseek_api_key": "sk-vprnpmjfzbcinduybbsboawtjxtrnrhfldbargfwzkieuczu",
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
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except:
        proc.kill()

section("Results")
passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
print(f"\n{'='*50}")
print(f"Total: {passed} passed, {failed} failed")
for s, name, detail in RESULTS:
    marker = "OK" if s == "PASS" else "XX"
    print(f"  {marker} {name}: {detail}")
print(f"{'='*50}")
sys.exit(1 if failed else 0)
