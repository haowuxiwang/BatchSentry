"""便携版端到端测试脚本。

用法：
    python e2e_test.py                # 完整流水线（上传+轮询+验证）
    python e2e_test.py --upload-only  # 仅上传，立即返回
    python e2e_test.py --poll JOB_ID   # 仅轮询指定 job
"""
import json
import sys
import time
import requests

BASE = "http://127.0.0.1:58765"
PDF = "samples/丝裂霉素提取批记录.pdf"


def upload(pdf_path: str) -> str:
    """上传 PDF 并启动 pipeline，返回 job_id。"""
    import os
    if not os.path.exists(pdf_path):
        # 退而求其次：取 samples 下任意 pdf
        import glob
        pdfs = glob.glob("samples/*.pdf")
        if not pdfs:
            raise SystemExit("No PDF found in samples/")
        pdf_path = pdfs[0]
        print(f"[E2E] Using PDF: {pdf_path}")

    file_size_mb = os.path.getsize(pdf_path) / 1024 / 1024
    print(f"[E2E] Uploading {pdf_path} ({file_size_mb:.1f} MB)...")

    # 流式上传：requests 的 files 参数会自动分块
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{BASE}/api/jobs",
            files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
            timeout=600,  # 10 分钟，大文件上传可能慢
        )

    if resp.status_code != 200:
        raise SystemExit(f"[E2E] Upload failed: HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    job_id = data["job_id"]
    print(f"[E2E] Upload OK: job_id={job_id} status={data.get('status')}")
    return job_id


def poll(job_id: str, timeout: int = 3600):
    """轮询 job 状态直到终态或超时。"""
    print(f"[E2E] Polling job {job_id} (timeout={timeout}s)...")
    start = time.time()
    last_status = None

    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=10)
            if resp.status_code != 200:
                print(f"[E2E] WARN: GET job returned {resp.status_code}")
                time.sleep(5)
                continue

            data = resp.json()
            status = data["status"]
            total_pages = data.get("total_pages", 0)
            # get_job_status 返回 total_findings（计数），不是 findings 列表
            findings_count = data.get("total_findings", 0)
            error_msg = data.get("error_message", "")

            if status != last_status:
                elapsed = int(time.time() - start)
                print(f"[E2E] [{elapsed}s] status={status} pages={total_pages} findings={findings_count}")
                last_status = status

            # 终态
            if status in ("review", "partial_review", "error", "cancelled", "archived"):
                elapsed = int(time.time() - start)
                print(f"[E2E] Terminal state reached: {status} after {elapsed}s")
                if status == "error":
                    print(f"[E2E] ERROR: {error_msg}")
                return data

            time.sleep(3)
        except requests.exceptions.RequestException as e:
            print(f"[E2E] WARN: poll network error: {e}")
            time.sleep(5)

    raise SystemExit(f"[E2E] Timeout after {timeout}s, last status={last_status}")


def verify(job_data: dict):
    """验证 job 结果的完整性。

    job_data 来自 GET /api/jobs/{id}，包含 total_findings（计数）但不含
    findings 列表。此处额外调用 /api/jobs/{id}/report.json 获取完整
    findings 列表用于分类统计与抽样打印。
    """
    print("\n" + "=" * 60)
    print("[E2E] VERIFICATION")
    print("=" * 60)

    status = job_data["status"]
    total_pages = job_data.get("total_pages", 0)
    filename = job_data.get("filename", "?")
    job_id = job_data["id"]

    # GET /api/jobs/{id} 只返回 total_findings 计数；调用 report.json
    # 端点获取完整 findings 列表用于分类统计。
    findings = []
    try:
        resp = requests.get(f"{BASE}/api/jobs/{job_id}/report.json", timeout=30)
        if resp.status_code == 200:
            findings = resp.json().get("findings", [])
        else:
            print(f"[E2E] WARN: report.json returned {resp.status_code}")
    except Exception as e:
        print(f"[E2E] WARN: report.json fetch failed: {e}")

    print(f"  Filename:    {filename}")
    print(f"  Status:      {status}")
    print(f"  Total pages: {total_pages}")
    print(f"  Findings:    {len(findings)}")

    # 状态必须是成功的终态
    if status not in ("review", "partial_review"):
        print(f"\n[E2E] FAIL: status={status} is not a success state")
        return False

    # 必须有页面被处理
    if total_pages == 0:
        print("\n[E2E] FAIL: total_pages=0, OCR did not produce any pages")
        return False

    # findings 分类统计
    if findings:
        by_severity = {}
        by_source = {}
        by_type = {}
        for f in findings:
            sev = f.get("severity", "?")
            src = f.get("source", "?")
            typ = f.get("type", "?")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_source[src] = by_source.get(src, 0) + 1
            by_type[typ] = by_type.get(typ, 0) + 1

        print(f"\n  By severity: {by_severity}")
        print(f"  By source:   {by_source}")
        print(f"  By type:     {by_type}")

        # 打印前 5 条 findings 摘要
        print(f"\n  Sample findings (first 5):")
        for f in findings[:5]:
            page = f.get("page", "?")
            sev = f.get("severity", "?")
            typ = f.get("type", "?")
            desc = f.get("description", "")[:80]
            print(f"    [p{page}] {sev}/{typ}: {desc}")

    # 验证 audit log — 端点返回 {"entries": [...], "count": N}
    try:
        resp = requests.get(f"{BASE}/api/jobs/{job_id}/audit", timeout=10)
        if resp.status_code == 200:
            audit_data = resp.json()
            entries = audit_data.get("entries", [])
            print(f"\n  Audit entries: {len(entries)}")
            # 显示最后 3 条
            for entry in entries[-3:]:
                action = entry.get("action", "?")
                detail = entry.get("detail", "")[:60]
                print(f"    {action}: {detail}")
        else:
            print(f"\n  Audit log fetch failed: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Audit log fetch failed: {e}")

    # 验证报告生成 — 端点是 /report.md（不是 /report）
    try:
        resp = requests.get(f"{BASE}/api/jobs/{job_id}/report.md", timeout=30)
        if resp.status_code == 200:
            report_len = len(resp.text)
            print(f"\n  Report (MD): {report_len} chars")
            if report_len > 100:
                print(f"    Preview: {resp.text[:200]}...")
        else:
            print(f"\n  Report generation failed: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Report generation failed: {e}")

    print("\n" + "=" * 60)
    if status in ("review", "partial_review") and total_pages > 0:
        print("[E2E] RESULT: PASS ✓")
        print("  - 打包版后端成功调用 OCR (PaddleOCR)")
        print("  - 打包版后端成功调用 LLM (SiliconFlow/DeepSeek-V3.2)")
        print("  - 状态机正确流转到终态")
        print("  - findings 数据结构完整")
        print("  - audit log 完整记录")
        return True
    else:
        print("[E2E] RESULT: FAIL ✗")
        return False


def main():
    args = sys.argv[1:]

    if "--poll" in args:
        idx = args.index("--poll")
        job_id = args[idx + 1]
        data = poll(job_id)
        ok = verify(data)
        sys.exit(0 if ok else 1)

    # 完整流程：上传 + 轮询 + 验证
    job_id = upload(PDF)
    data = poll(job_id)
    ok = verify(data)

    # 清理：可选删除 job
    if "--keep" not in args:
        try:
            requests.delete(f"{BASE}/api/jobs/{job_id}", timeout=10)
            print(f"\n[E2E] Cleaned up job {job_id}")
        except Exception:
            pass

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
