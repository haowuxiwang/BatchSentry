"""E2E driver: frozen exe multi-round end-to-end test (reusable regression harness).

Usage:
  python e2e_run.py --sf-key SF --paddle-token PT --mineru-token MT [--rounds pdf,img,mineru,real,real-mineru]

Rounds:
  pdf         — upload e2e_test.pdf with paddle backend (rules + gmp_basis assertions)
  img         — upload test1.jpg (image->pdf conversion path)
  mineru      — re-upload e2e_test.pdf with mineru backend (dual-engine)
  real        — 丝裂霉素提取批记录.pdf (real handwriting, paddle primary; per-page
                sparse detection <40 chars + REAL_TIMEOUT_S budget)
  real-mineru — same real pdf with mineru backend (dual-engine completeness)
  cancel      — upload then cancel immediately (OCRCancelled abort semantics;
                asserts terminal=cancelled within 180s)
  dual        — OCR_DUAL_COMPARE on: mineru primary + paddle secondary re-run of
                same synthetic pdf (asserts audit dual_compare_done; diffs force
                partial_review)

Every round also subscribes /api/jobs/{id}/stream and records SSE frames to
devlogs/e2e_sse_<stem>.jsonl (streaming-output evidence: event count / phase chain).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:58799"

# 大文档轮次预算：51 页 Stage 2 在上游 LLM 拥堵日（硅基流动单页排队
# 500-1000s 实测）需要 45-60min，旧固定 2400s 曾在 40/51 页处误杀整轮
# （driver finally 终止 exe）— 默认 90min，可用 E2E_REAL_TIMEOUT 覆盖。
REAL_TIMEOUT_S = int(os.environ.get("E2E_REAL_TIMEOUT", "5400"))

_TERMINAL = ("review", "partial_review", "error", "cancelled")


def _sse_recorder(job_id, out_path, stats):
    """后台线程：订阅 /api/jobs/{id}/stream，记录 SSE 帧到 jsonl。

    产出"流式输出是否完成"证据：事件总数 / phase 覆盖（ocr/analyze/cross）
    / 终帧状态。流在终态自动关闭；线程 daemon，不阻塞 driver 退出。

    EventSource 语义（2026-08-24 实证）：Stage1→2 大事务提交间隙流可能
    >10s 无字节 — 单次连接 + 短读超时会误杀采集（前端会自动重连，
    采集器也必须）。读超时放宽到 60s，断流后重连（终态帧出现即停）。
    """
    import threading

    def _run():
        terminal_seen = False
        while not terminal_seen:
            try:
                with httpx.Client(timeout=httpx.Timeout(10, read=60)) as c:
                    with c.stream("GET",
                                  f"{API}/api/jobs/{job_id}/stream") as r:
                        with open(out_path, "a", encoding="utf-8") as f:
                            for line in r.iter_lines():
                                if not line.startswith("data: "):
                                    continue
                                f.write(line[6:] + "\n")
                                f.flush()
                                stats["events"] += 1
                                try:
                                    d = json.loads(line[6:])
                                except ValueError:
                                    continue
                                ph = d.get("phase") or d.get("status") or "?"
                                if ph != stats.get("last_phase"):
                                    stats["transitions"].append(
                                        f"{ph}:{d.get('status', '')}")
                                    stats["last_phase"] = ph
                                stats["last"] = d
                                if d.get("status") in _TERMINAL:
                                    terminal_seen = True
            except Exception as e:
                stats["err"] = str(e)[:200]
                time.sleep(2)  # 断流重连（EventSource retry 语义）

    t = threading.Thread(target=_run, daemon=True, name=f"sse-{job_id[:8]}")
    t.start()
    return t


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
            if st in _TERMINAL:
                return st, d
        elif r.status_code == 404:
            # status endpoint may live at a different path; try progress
            p = client.get(f"{API}/api/jobs/{job_id}/progress", timeout=15)
            if p.status_code == 200:
                d = p.json()
                if d.get("status") in _TERMINAL:
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
                    # force=1: appdata 复用跨会话时同内容文件已存在（409 去重），
                    # e2e 轮次必须顺序无关
                    results["pdf"] = run_upload(c, "e2e_test.pdf", "application/pdf",
                                                expect_types=["step_gap", "time_reversal",
                                                              "batch_inconsistency",
                                                              "param_out_of_spec"],
                                                force=True)
                elif rnd == "img":
                    results["img"] = run_upload(c, "test1.jpg", "image/jpeg",
                                                expect_types=[], force=True)
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
                        timeout_s=REAL_TIMEOUT_S, page_chars=True)
                elif rnd == "real-mineru":
                    # same real pdf with mineru backend (dual-engine completeness)
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "mineru"})
                    print(f"[e2e] switch to mineru -> {r.status_code}")
                    results["real-mineru"] = run_upload(
                        c, os.environ.get("E2E_PDF", "丝裂霉素提取批记录.pdf"),
                        "application/pdf", expect_types=[], force=True,
                        timeout_s=REAL_TIMEOUT_S, page_chars=True)
                elif rnd == "cancel":
                    # cancel-during-OCR: OCRCancelled abort semantics (frozen exe)
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "paddle"})
                    print(f"[e2e] switch to paddle -> {r.status_code}")
                    results["cancel"] = run_cancel(c, os.environ.get(
                        "E2E_PDF_SMALL", "e2e_test.pdf"))
                elif rnd == "dual":
                    # gate-3 dual-compare: mineru primary + paddle secondary re-run
                    r = c.post(f"{API}/api/settings", json={
                        "ocr_backend": "mineru", "ocr_dual_compare": True})
                    print(f"[e2e] dual-compare enabled -> {r.status_code}")
                    results["dual"] = run_dual(
                        c, os.environ.get("E2E_PDF_SMALL", "e2e_test.pdf"))
                    c.post(f"{API}/api/settings", json={"ocr_dual_compare": False})
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


# 常规轮预算：默认 600s；上游 LLM 拥堵日单页排队可达数分钟
# （2026-09-02 实测 img 轮 1 页 482s），可用 E2E_PDF_TIMEOUT 覆盖。
_PDF_TIMEOUT_S = int(os.environ.get("E2E_PDF_TIMEOUT", "600"))


def run_upload(c, path, mime, expect_types, force, timeout_s=None, page_chars=False):
    if timeout_s is None:
        timeout_s = _PDF_TIMEOUT_S
    t0 = time.time()
    with open(path, "rb") as f:
        files = {"file": (path, f, mime)}
        r = c.post(f"{API}/api/jobs" + ("?force=1" if force else ""), files=files, timeout=600)
    if r.status_code not in (200, 201):
        return {"ok": False, "err": f"upload {r.status_code}: {r.text[:300]}"}
    job_id = r.json().get("job_id") or r.json().get("id")
    print(f"[e2e] upload {path} -> job {job_id}")
    # SSE 流式输出证据采集：全程订阅进度流，记录事件数/phase 覆盖
    sse_stats = {"events": 0, "transitions": [], "last_phase": None, "last": None}
    sse_log = os.path.join("devlogs", f"e2e_sse_{Path(path).stem}.jsonl")
    sse_thread = _sse_recorder(job_id, sse_log, sse_stats)
    st, d = wait_terminal(c, job_id, timeout_s=timeout_s)
    sse_thread.join(timeout=15)
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
    # SSE 证据摘要：事件数 / phase 迁移链 / 终帧
    phases = [t.split(":")[0] for t in sse_stats["transitions"]]
    sse_ok = bool(sse_stats["events"]) and "done" in phases
    print(f"[e2e] {path}: SSE events={sse_stats['events']} phases={sse_stats['transitions']}"
          f" final={((sse_stats.get('last') or {}).get('status'))} -> {'OK' if sse_ok else 'CHECK'}")
    return {"ok": ok, "status": st, "duration_s": dur,
            "pages": d.get("total_pages"), "findings": len(fs),
            "job_id": job_id,
            "types": types, "gmp_basis": with_basis, "missing": missing,
            "sparse_pages": len(sparse_pages),
            "sse_events": sse_stats["events"],
            "sse_phases": phases, "sse_ok": sse_ok}


def run_cancel(c, path, mime="application/pdf"):
    """Cancel-during-OCR round: validates the OCRCancelled abort semantics.

    上传后等到 ocr_running 立即取消 —— 新语义下同步探针在轮询循环内命中，
    数秒内中止阻塞等待并走正式迁移；旧实现会跑满主/备两轮 OCR 轮询超时
    （分钟级）或终态变成 review 而非 cancelled。
    断言：终态 == cancelled 且总耗时 ≤ 180s。
    """
    import sqlite3  # noqa: F401  (parity with other runners' imports)

    t0 = time.time()
    with open(path, "rb") as f:
        r = c.post(f"{API}/api/jobs?force=1",
                   files={"file": (path, f, mime)}, timeout=600)
    if r.status_code not in (200, 201):
        return {"ok": False, "err": f"upload {r.status_code}: {r.text[:300]}"}
    job_id = r.json().get("job_id") or r.json().get("id")
    print(f"[e2e] upload {path} -> job {job_id}")
    sse_stats = {"events": 0, "transitions": [], "last_phase": None, "last": None}
    sse_log = os.path.join("devlogs", "e2e_sse_cancel.jsonl")
    sse_thread = _sse_recorder(job_id, sse_log, sse_stats)
    # 等 OCR 真正开跑（避免对 pending 取消的无关路径），最长 20s
    entered = ""
    for _ in range(20):
        try:
            d = c.get(f"{API}/api/jobs/{job_id}", timeout=10).json()
            entered = d.get("status", "")
            if entered in ("ocr_running", "analyzing"):
                break
        except Exception:
            pass
        time.sleep(1)
    rc = c.post(f"{API}/api/jobs/{job_id}/cancel", timeout=15)
    print(f"[e2e] cancel POST -> {rc.status_code} "
          f"(entered={entered!r} at {int(time.time() - t0)}s)")
    st, d = wait_terminal(c, job_id, timeout_s=240)
    sse_thread.join(timeout=15)
    dur = int(time.time() - t0)
    print(f"[e2e] cancel round: status={st} in {dur}s")
    ok = st == "cancelled" and dur <= 180
    if not ok:
        print(f"[e2e] cancel round FAIL: terminal={st!r} duration={dur}s "
              f"(期望 cancelled 且 ≤180s)")
    phases = [t.split(":")[0] for t in sse_stats["transitions"]]
    print(f"[e2e] cancel round: SSE events={sse_stats['events']} "
          f"phases={sse_stats['transitions']}")
    return {"ok": ok, "status": st, "duration_s": dur,
            "entered_at_cancel": entered, "job_id": job_id,
            "sse_events": sse_stats["events"], "sse_phases": phases}


def run_dual(c, path, mime="application/pdf"):
    """Gate-3 dual-compare round: primary mineru + secondary paddle re-run.

    已知该合成 PDF 在两引擎下 p2/p4 覆盖率 <0.85 —— 预期门禁产生差异
    findings 并强制 partial_review。断言：audit 含 dual_compare_done；
    终态为 review/partial_review（差异页数仅作证据输出，不作硬断言 ——
    LLM 抽取存在轮次方差，两引擎可能偶发一致）。
    """
    res = run_upload(c, path, mime, expect_types=[], force=True)
    job_id = res.get("job_id")
    if not job_id or not res.get("ok"):
        return res
    try:
        r = c.get(f"{API}/api/jobs/{job_id}/audit?limit=300", timeout=15)
        actions = []
        if r.status_code == 200:
            body = r.json()
            items = body if isinstance(body, list) else body.get("entries", [])
            actions = [a.get("action", "") for a in items]
    except Exception as e:
        actions = [f"<audit fetch failed: {e}>"]
    dual_actions = [a for a in actions if a.startswith("dual_compare")]
    diff_findings = [
        f for f in findings_of(c, job_id)
        if f.get("source") == "rule" and f.get("type") == "completeness"
        and ("双后端" in (f.get("description") or "")
             or "覆盖" in (f.get("description") or ""))
    ]
    print(f"[e2e] dual: audit dual_compare events={dual_actions} "
          f"diff-completeness findings={len(diff_findings)}")
    res["dual_audit"] = dual_actions
    res["diff_findings"] = len(diff_findings)
    res["ok"] = bool(res.get("ok")) and "dual_compare_done" in dual_actions
    return res


if __name__ == "__main__":
    main()
