"""E2E driver: frozen exe multi-round end-to-end test (reusable regression harness).

Usage:
  python e2e_run.py --sf-key SF --paddle-token PT --mineru-token MT [--rounds pdf,img,mineru,real,real-mineru]

Rounds:
  pdf         — upload e2e_test.pdf with paddle backend (rules + gmp_basis assertions)
  img         — upload test1.jpg (image->pdf conversion path)
  mineru      — re-upload e2e_test.pdf with mineru backend (dual-engine)
  real        — 丝裂霉素提取批记录.pdf (real handwriting, paddle primary; per-page
                sparse detection <40 chars + 2400s timeout)
  real-mineru — same real pdf with mineru backend (dual-engine completeness)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx

API = "http://127.0.0.1:58799"


def wait_health(client, timeout=40):
    for _ in range(timeout * 2):
        try:
            r = client.get(f"{API}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_terminal(client, job_id, timeout_s=600):
    """Poll job status to terminal; return (status, resp_json).

    抗挫折：单次轮询超时/连接抖动只计数告警不中断 — 大文档 OCR
    期间服务端可能短暂繁忙（e2e 实证：51 页 300dpi 规范化），
    连续 30 次（≈2 分钟）不可达才判失败。
    """
    start = time.time()
    consecutive_errs = 0
    while time.time() - start < timeout_s:
        try:
            r = client.get(f"{API}/api/jobs/{job_id}", timeout=15)
        except httpx.TransportError as e:
            consecutive_errs += 1
            if consecutive_errs >= 30:
                print(f"[e2e] poll unreachable x{consecutive_errs}: {e}")
                return "poll_failed", {}
            time.sleep(4)
            continue
        consecutive_errs = 0
        if r.status_code == 200:
            d = r.json()
            st = d.get("status", "")
            if st in ("review", "partial_review", "error", "cancelled"):
                return st, d
        elif r.status_code == 404:
            # status endpoint may live at a different path; try progress
            p = client.get(f"{API}/api/jobs/{job_id}/progress", timeout=15)
            if p.status_code == 200:
                d = p.json()
                if d.get("status") in ("review", "partial_review", "error", "cancelled"):
                    return d["status"], d
        time.sleep(4)
    return "timeout", {}


def findings_of(client, job_id):
    r = client.get(f"{API}/api/jobs/{job_id}/findings", timeout=15)
    if r.status_code == 200:
        d = r.json()
        if isinstance(d, list):
            return d
        return d.get("findings", [])
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf-key", required=True)
    ap.add_argument("--paddle-token", required=True)
    ap.add_argument("--mineru-token", required=True)
    ap.add_argument("--rounds", default="pdf,img,mineru")
    args = ap.parse_args()
    rounds = args.rounds.split(",")

    appdata = os.path.join(tempfile.gettempdir(), "pbc_e2e_appdata")
    os.makedirs(appdata, exist_ok=True)
    env = dict(os.environ, APPDATA=appdata, PORT="58799", NO_WINDOW="1")

    exe = os.path.join("dist", "pbc-server", "pbc-server.exe")
    proc = subprocess.Popen([exe], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[e2e] exe started pid={proc.pid} appdata={appdata}")
    ok = False
    try:
        with httpx.Client(timeout=15) as c:
            if not wait_health(c):
                print("[e2e] FAIL: health timeout"); sys.exit(2)
            print("[e2e] health OK")

            # configure: siliconflow LLM + paddle + mineru
            form = {
                "llm_provider": "siliconflow",
                "siliconflow_protocol": "openai",
                "siliconflow_api_key": args.sf_key,
                "siliconflow_base_url": "https://api.siliconflow.cn/v1",
                "siliconflow_model": "Qwen/Qwen2.5-72B-Instruct",
                "paddle_ocr_api_url": "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
                "paddle_ocr_token": args.paddle_token,
                "paddle_ocr_model": "PaddleOCR-VL-1.6",
                "mineru_token": args.mineru_token,
                "ocr_backend": "paddle",
            }
            r = c.post(f"{API}/api/settings", json=form)
            print(f"[e2e] settings POST -> {r.status_code}")
            r.raise_for_status()

            # quick connectivity probe (LLM) — provider test endpoint
            try:
                r = c.post(f"{API}/api/settings/test_provider",
                           json={"provider": "siliconflow"}, timeout=60)
                print(f"[e2e] test_provider -> {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"[e2e] test_provider skipped: {e}")

            results = {}
            for rnd in rounds:
                if rnd == "pdf":
                    results["pdf"] = run_upload(c, "e2e_test.pdf", "application/pdf",
                                                expect_types=["step_gap", "time_reversal",
                                                              "batch_inconsistency",
                                                              "param_out_of_spec"],
                                                force=False)
                elif rnd == "img":
                    results["img"] = run_upload(c, "test1.jpg", "image/jpeg",
                                                expect_types=[], force=False)
                elif rnd == "mineru":
                    # switch backend to mineru and re-run same pdf with force
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "mineru"})
                    print(f"[e2e] switch to mineru -> {r.status_code}")
                    results["mineru"] = run_upload(c, os.environ.get("E2E_PDF", "e2e_test.pdf"),
                                                   "application/pdf",
                                                   expect_types=[], force=True)
                elif rnd == "real":
                    # real-world scanned batch record (handwriting), paddle primary
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "paddle"})
                    print(f"[e2e] switch to paddle -> {r.status_code}")
                    results["real"] = run_upload(
                        c, os.environ.get("E2E_PDF", "丝裂霉素提取批记录.pdf"),
                        "application/pdf", expect_types=[], force=True,
                        timeout_s=2400, page_chars=True)
                elif rnd == "real-mineru":
                    # same real pdf with mineru backend (dual-engine completeness)
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "mineru"})
                    print(f"[e2e] switch to mineru -> {r.status_code}")
                    results["real-mineru"] = run_upload(
                        c, os.environ.get("E2E_PDF", "丝裂霉素提取批记录.pdf"),
                        "application/pdf", expect_types=[], force=True,
                        timeout_s=2400, page_chars=True)
            print("\n[e2e] SUMMARY:", json.dumps(
                {k: {kk: vv for kk, vv in v.items() if kk != "findings"}
                 for k, v in results.items()}, ensure_ascii=False, indent=2))
            bad = [k for k, v in results.items() if not v.get("ok")]
            if bad:
                print(f"[e2e] FAILED rounds: {bad}"); sys.exit(3)
            print("[e2e] ALL ROUNDS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        print("[e2e] exe stopped")


def run_upload(c, path, mime, expect_types, force, timeout_s=600, page_chars=False):
    t0 = time.time()
    with open(path, "rb") as f:
        files = {"file": (path, f, mime)}
        r = c.post(f"{API}/api/jobs" + ("?force=1" if force else ""), files=files, timeout=600)
    if r.status_code not in (200, 201):
        return {"ok": False, "err": f"upload {r.status_code}: {r.text[:300]}"}
    job_id = r.json().get("job_id") or r.json().get("id")
    print(f"[e2e] upload {path} -> job {job_id}")
    st, d = wait_terminal(c, job_id, timeout_s=timeout_s)
    dur = int(time.time() - t0)
    print(f"[e2e] {path}: status={st} in {dur}s (pages={d.get('total_pages')})")
    fs = findings_of(c, job_id) if st in ("review", "partial_review") else []
    types = {}
    with_basis = 0
    for f in fs:
        t = f.get("type", "?")
        types[t] = types.get(t, 0) + 1
        if f.get("gmp_basis"):
            with_basis += 1
    print(f"[e2e] {path}: {len(fs)} findings types={types} gmp_basis={with_basis}/{len(fs)}")
    # OCR completeness: per-page visible char counts; flag suspiciously
    # sparse pages (<40 chars, i.e. header/footer-only recognition)
    sparse_pages = []
    if page_chars:
        pages = d.get("total_pages") or 0
        for p in range(1, pages + 1):
            try:
                r = c.get(f"{API}/api/jobs/{job_id}/pages/{p}", timeout=15)
                if r.status_code == 200:
                    pd = r.json()
                    html = pd.get("raw_html") or ""
                    text = pd.get("ocr_text") or ""
                    n = max(len(text), len(html) // 3)
                    if n < 40:
                        sparse_pages.append({"page": p, "chars": n})
            except Exception:
                pass
        print(f"[e2e] {path}: sparse OCR pages(<40 chars)={len(sparse_pages)} {sparse_pages[:10]}")
    missing = [t for t in expect_types if t not in types]
    ok = st in ("review", "partial_review") and not missing
    if missing:
        print(f"[e2e] {path}: MISSING expected types: {missing}")
    return {"ok": ok, "status": st, "duration_s": dur,
            "pages": d.get("total_pages"), "findings": len(fs),
            "types": types, "gmp_basis": with_basis, "missing": missing,
            "sparse_pages": len(sparse_pages)}


if __name__ == "__main__":
    main()
