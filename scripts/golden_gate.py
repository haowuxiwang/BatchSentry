"""OCR_GOLDEN_CORPUS 发布门禁一键执行器（docs/OCR_GOLDEN_CORPUS.md）。

对指定 job（或新上传样本）执行四条发布门禁并输出 PASS/FAIL 报告：

  gate 1  可追溯性：每页 ocr_diagnostics 非空 + job 目录含后端原始产物
  gate 2  完整性：每页 integrity=ok 或带人工豁免（exemption）
  gate 3  双后端对比：audit 中 dual_compare_done 且 diffs=0（跳过=警告）
  gate 4  端到端：终态 + 页数一致 + 指定页包含必需 token

用法（对运行中的 dev/frozen 服务执行，仅用 HTTP API + 本地文件系统）：
  python scripts/golden_gate.py --job-id <id> \
      --check 48:丝裂霉素 --check 48:清洗记录 --expect-pages 51
  python scripts/golden_gate.py --upload samples/xxx.pdf --expect-pages 51

退出码：0 = 全部通过；1 = 任一 FAIL。WARN 不影响退出码。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib import error, request as urlreq

TERMINAL_STATES = {"review", "partial_review", "error", "cancelled"}


class GateRunner:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _get(self, path: str, timeout: float = 30):
        req = urlreq.Request(self.base + path, headers={"Accept": "application/json"})
        with urlreq.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── API 封装 ────────────────────────────────────────────────
    def get_job(self, job_id):
        return self._get(f"/api/jobs/{job_id}")

    def get_page(self, job_id, page):
        return self._get(f"/api/jobs/{job_id}/pages/{page}")

    def get_audit(self, job_id):
        try:
            return self._get(f"/api/jobs/{job_id}/audit")
        except error.HTTPError:
            return []

    def upload(self, pdf_path: str) -> str:
        boundary = "----goldenGateBoundary"
        data = Path(pdf_path).read_bytes()
        name = Path(pdf_path).name.encode("utf-8")
        body = (
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="file"; filename="'
            + name
            + b'"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urlreq.Request(
            self.base + "/api/jobs",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urlreq.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))["job_id"]

    def wait_terminal(self, job_id: str, max_minutes: int = 60) -> dict:
        deadline = time.time() + max_minutes * 60
        while time.time() < deadline:
            job = self.get_job(job_id)
            if job.get("status") in TERMINAL_STATES:
                return job
            print(
                f"  ... status={job.get('status')} "
                f"ocr={ (job.get('ocr_progress') or {}).get('done') }/"
                f"{ (job.get('ocr_progress') or {}).get('total') } "
                f"analyzed={job.get('pages_analyzed')}/{job.get('total_pages')}",
                flush=True,
            )
            time.sleep(20)
        raise TimeoutError(f"job {job_id} not terminal within {max_minutes} min")


def run_gates(runner: GateRunner, job_id: str, output_dir: Path | None,
              expect_pages: int | None, checks: list[tuple[int, str]]) -> int:
    failures: list[str] = []
    warnings: list[str] = []

    job = runner.get_job(job_id)
    status = job.get("status")
    total = int(job.get("total_pages") or 0)
    backend = job.get("ocr_backend_used")
    print(f"\n=== job {job_id} ===")
    print(f"status={status} pages={total} backend={backend}")
    if not total:
        failures.append("gate-4: total_pages=0（OCR 未产出任何页面）")

    # ── gate 1 可追溯性 ──
    missing_diag = []
    for p in range(1, total + 1):
        try:
            pg = runner.get_page(job_id, p)
        except error.HTTPError:
            missing_diag.append(p)
            continue
        if not pg.get("ocr_diagnostics"):
            missing_diag.append(p)
    if missing_diag:
        failures.append(f"gate-1: {len(missing_diag)} 页缺 ocr_diagnostics: {missing_diag[:10]}")
    else:
        print("gate-1 diagnostics: OK（全部页面有诊断）")
    if output_dir is not None:
        jd = output_dir / job_id
        artifacts = [x for x in ("mineru_original.zip", "paddle_original.json",
                                 "paddle_original.jsonl") if (jd / x).exists()]
        if artifacts:
            print(f"gate-1 raw artifact: OK（{artifacts[0]}）")
        elif backend in ("mineru", "paddle"):
            warnings.append(
                f"gate-1: job 目录无后端原始产物（backend={backend}）— "
                f"旧版本任务或落盘失败"
            )

    # ── gate 2 完整性 ──
    incomplete, exempted = [], []
    for p in range(1, total + 1):
        try:
            diag = runner.get_page(job_id, p).get("ocr_diagnostics") or {}
        except error.HTTPError:
            continue
        if "exemption" in diag:
            exempted.append(p)
        elif diag.get("integrity") != "ok":
            incomplete.append((p, diag.get("reasons")))
    if incomplete:
        failures.append(
            f"gate-2: {len(incomplete)} 页 integrity!=ok 且无豁免: "
            f"{[(p, r) for p, r in incomplete[:5]]}"
        )
    else:
        msg = "gate-2 integrity: OK"
        if exempted:
            msg += f"（{len(exempted)} 页人工豁免: {exempted}）"
        print(msg)

    # ── gate 3 双后端对比 ──
    audit = runner.get_audit(job_id)
    dual_done = [a for a in audit if a.get("action") == "dual_compare_done"]
    dual_skip = [a for a in audit if a.get("action") == "dual_compare_skipped"]
    if dual_done:
        detail = dual_done[-1].get("detail", "")
        if "diffs=0" in detail:
            print("gate-3 dual compare: PASS（diffs=0）")
        else:
            failures.append(f"gate-3: 双后端存在差异 — {detail[:200]}")
    elif dual_skip:
        warnings.append(f"gate-3: 对比被跳过 — {dual_skip[-1].get('detail', '')[:120]}")
    else:
        warnings.append(
            "gate-3: 无 dual_compare 审计（OCR_DUAL_COMPARE 未开启或旧任务）"
        )

    # ── gate 4 端到端 ──
    if status == "error":
        failures.append(f"gate-4: 任务以 error 终态 — {job.get('error_message', '')[:150]}")
    elif status not in TERMINAL_STATES:
        failures.append(f"gate-4: 任务未到终态（{status}）")
    if expect_pages is not None and total != expect_pages:
        failures.append(f"gate-4: 页数不符 — 期望 {expect_pages}，实际 {total}")
    for page_no, token in checks:
        try:
            raw = runner.get_page(job_id, page_no).get("raw_html") or ""
        except error.HTTPError:
            failures.append(f"gate-4: 第{page_no}页不存在，无法核对 token「{token}」")
            continue
        if token not in raw:
            failures.append(f"gate-4: 第{page_no}页缺少必需 token「{token}」")
    if status in ("review", "partial_review") and not any(
        f.startswith("gate-4:") for f in failures
    ):
        print("gate-4 e2e: OK（终态 + 页数 + token 核对通过）")

    # ── 报告 ──
    print("\n════════ 金标门禁结果 ════════")
    for w in warnings:
        print(f"WARN: {w}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"\n结果: FAIL（{len(failures)} 项失败, {len(warnings)} 警告）")
        return 1
    print(f"\n结果: PASS（{len(warnings)} 警告）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR_GOLDEN_CORPUS gate runner")
    ap.add_argument("--base-url", default="http://127.0.0.1:58765")
    ap.add_argument("--job-id", help="检查已有任务")
    ap.add_argument("--upload", help="上传样本 PDF 并等待完成后检查")
    ap.add_argument("--output-dir", default="output", help="job 产物目录（gate 1 原始产物检查）")
    ap.add_argument("--expect-pages", type=int, default=None)
    ap.add_argument("--check", action="append", default=[],
                    metavar="PAGE:TOKEN", help="第 N 页必须包含 token（可重复）")
    ap.add_argument("--max-wait-minutes", type=int, default=60)
    args = ap.parse_args()

    checks = []
    for c in args.check:
        page_no, _, token = c.partition(":")
        checks.append((int(page_no), token))

    runner = GateRunner(args.base_url)
    if args.upload:
        print(f"上传样本: {args.upload}")
        job_id = runner.upload(args.upload)
        print(f"job_id: {job_id} — 等待终态（最长 {args.max_wait_minutes} 分钟）…")
        runner.wait_terminal(job_id, args.max_wait_minutes)
    elif args.job_id:
        job_id = args.job_id
    else:
        ap.error("需要 --job-id 或 --upload")

    out_dir = Path(args.output_dir) if args.output_dir else None
    return run_gates(runner, job_id, out_dir, args.expect_pages, checks)


if __name__ == "__main__":
    sys.exit(main())
