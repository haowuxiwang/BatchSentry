import subprocess, time, os, sys, requests, json

EXE = r"D:\learn\claudecode\pharma-batch-checker\dist\pbc-server\pbc-server.exe"
BASE = "http://127.0.0.1:58765"
APPDATA = os.path.join(os.environ["TEMP"], "pbc-e2e-v2")
os.makedirs(os.path.join(APPDATA, "PBC"), exist_ok=True)

env = os.environ.copy()
env["APPDATA"] = APPDATA

print("Starting server...")
proc = subprocess.Popen([EXE], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(8)

try:
    # Health
    r = requests.get(f"{BASE}/health", timeout=5)
    print(f"Health: {r.status_code} {r.json()}")
    
    # Configure LLM
    r = requests.post(f"{BASE}/api/settings", json={
        "llm_provider": "deepseek",
        "deepseek_api_key": "sk-vprnpmjfzbcinduybbsboawtjxtrnrhfldbargfwzkieuczu",
    }, timeout=10)
    print(f"Settings POST: {r.status_code}")
    
    # Verify
    r2 = requests.get(f"{BASE}/api/settings", timeout=5)
    s = r2.json()
    ds = next((p for p in s.get("llm",{}).get("providers",[]) if p.get("name")=="deepseek"), {})
    print(f"LLM configured: provider={s['llm']['provider']} deepseek={ds.get('configured')}")
    
    # Create unique test PDF (different from previous run)
    import random
    test_pdf = os.path.join(os.environ["TEMP"], f"e2e-{random.randint(10000,99999)}.pdf")
    with open(test_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n4 0 obj<</Length 44>>stream\nBT /F1 24 Tf 100 700 Td (E2E Test) Tj ET\nendstream\nendobj\n5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\nxref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000266 00000 n \n0000000360 00000 n \ntrailer<</Size 6/Root 1 0 R>>\nstartxref\n429\n%%EOF")
    
    # Upload
    with open(test_pdf, "rb") as f:
        r = requests.post(f"{BASE}/api/jobs", files={"file": ("e2e_test.pdf", f, "application/pdf")}, timeout=10)
    print(f"Upload: {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        print("Upload failed, aborting")
        sys.exit(1)
    
    job_id = r.json().get("job_id") or r.json().get("id")
    print(f"Job ID: {job_id}")
    
    # Wait for pipeline (up to 120s)
    print("Waiting for pipeline...")
    for i in range(60):
        time.sleep(2)
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10)
            data = r.json()
            status = data.get("status", "")
            print(f"  [{i*2}s] status={status}")
            if status in ("review", "partial_review", "error", "cancelled"):
                break
        except Exception as e:
            print(f"  [{i*2}s] poll error: {e}")
    
    # Final status
    r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10)
    data = r.json()
    print(f"\nFinal status: {data.get('status')}")
    print(f"Total pages: {data.get('total_pages')}")
    print(f"Failed pages: {data.get('failed_pages')}")
    print(f"OCR backend: {data.get('ocr_backend_used')}")
    
    # Review page
    r = requests.get(f"{BASE}/jobs/{job_id}/review", timeout=10)
    print(f"Review page: {r.status_code}")
    
    # Findings
    r = requests.get(f"{BASE}/api/jobs/{job_id}/findings", timeout=10)
    findings = r.json()
    count = len(findings) if isinstance(findings, list) else len(findings.get("findings", []))
    print(f"Findings: {count}")
    
    # Page image
    r = requests.get(f"{BASE}/api/jobs/{job_id}/page/1", timeout=15)
    print(f"Page image: {r.status_code} type={r.headers.get('content-type','?')} size={len(r.content)}")
    
    # Report
    r = requests.get(f"{BASE}/api/jobs/{job_id}/report.md", timeout=10)
    print(f"Report: {r.status_code} len={len(r.text)}")
    
    # Static assets
    for path in ["/static/app.css", "/static/review.js", "/docs"]:
        r = requests.get(f"{BASE}{path}", timeout=10)
        print(f"{path}: {r.status_code} len={len(r.content)}")
    
    print("\n=== E2E COMPLETE ===")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except:
        proc.kill()
    # Print server output
    if proc.stdout:
        output = proc.stdout.read().decode(errors="replace")
        if output.strip():
            print("\n--- Server output (last 20 lines) ---")
            for line in output.strip().split("\n")[-20:]:
                print(f"  {line}")
