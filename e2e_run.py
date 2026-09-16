"""E2E driver: frozen exe multi-round end-to-end test (reusable regression harness).

Usage:
  python e2e_run.py --sf-key SF --paddle-token PT --mineru-token MT [--rounds pdf,img,mineru,real,real-mineru]

Rounds:
  pdf         — upload e2e_test.pdf with paddle backend (rules + gmp_basis assertions)
  img         — upload test1.jpg (image->pdf conversion path)
  mineru      — re-upload e2e_test.pdf with mineru backend (dual-engine)
  real        — 丝裂霉素提取批记录.pdf (real handwriting, paddle primary; per-page
                sparse detection <40 chars + baseline+per-page budget)
  real-mineru — same real pdf with mineru backend (dual-engine completeness)
  cancel      — upload then cancel immediately (OCRCancelled abort semantics;
                asserts terminal=cancelled within 180s)
  dual        — OCR_DUAL_COMPARE on: mineru primary + paddle secondary re-run of
                same synthetic pdf (asserts audit dual_compare_done; diffs force
                partial_review)
  rot         — e2e_rot.pdf (synthetic sideways pages p2=90°, p3=270°, round-23 A):
                asserts sideways markers visible in final raw_html (content not
                lost — either VL reads rotated text directly or rotation heal
                recovers; records which path + rotation_deg evidence)
  robust      — M3 尺寸鲁棒性合成样本（scripts/gen_ocr_samples.py）：断言
                "该页不得静默标记成功" —— 不可无损修复页（小字号+低 DPI /
                极端长宽比）必须显式携带非完整信号；已规范化页不得稀疏。

Every round also subscribes /api/jobs/{id}/stream and records SSE frames to
devlogs/e2e_sse_<stem>_<job_id>.jsonl (streaming-output evidence: event count / phase chain).
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

# 端口单一真值：driver、被测进程、健康检查必须一致（曾各自写死 58799）
PORT = int(os.environ.get("PBC_E2E_PORT", "58799"))
API = f"http://127.0.0.1:{PORT}"

# 大文档轮次预算：51 页 Stage 2 在上游 LLM 拥堵日（硅基流动单页排队
# 500-1000s 实测）需要 45-60min，旧固定 2400s 曾在 40/51 页处误杀整轮
# （driver finally 终止 exe）。**现在不再写死单值** —— 由
# `_round_budget_s(path, "real")` 按"基线 + 每页"算（见该函数说明），
# `E2E_REAL_TIMEOUT` 仍可显式覆盖。
_TERMINAL = ("review", "partial_review", "error", "cancelled")


def backend_mismatch(expect_backend, used_backend):
    """返回后端不一致的说明；一致（或未指定期望）时返回 ``None``。

    独立成纯函数是为了可测：这个判定是"到底哪个 OCR 引擎跑的"的唯一防线，
    必须能被单测覆盖（见 tests/unit/test_e2e_backend_assert.py）。

    为什么需要它：主后端提交失败会**自动 failover** 到备选后端，而终态、
    findings、SSE 全都照常 —— 只有 ``jobs.ocr_backend_used`` 能揭穿。
    2026-09-15 实测：Paddle 上游返回 10010「任务提交队列已满」，51 页真实
    文档整轮实际跑的却是 MinerU，报告里却写着 paddle。
    """
    if not expect_backend or used_backend == expect_backend:
        return None
    return f"expected {expect_backend}, got {used_backend}"


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


def _assert_port_free() -> None:
    """启动前确认端口没被别的实例占着 —— 否则本轮结论不可信。

    历史隐患：``wait_health`` 只看 HTTP 200，**不校验响应者是不是本进程刚拉起的那个**。
    若上一次运行残留的 ``pbc-server.exe`` 仍占着端口，新实例绑定失败，而健康检查会
    顺利连到**旧实例** → driver 把任务提交给了另一个 server（appdata/DB 都不是本次的），
    全程却"绿灯"。这与已修的"测了 A、发了 B"是同一类错，只是错在实例身份而非文件路径。
    """
    import socket

    with socket.socket() as s:
        s.settimeout(2.0)
        busy = s.connect_ex(("127.0.0.1", PORT)) == 0
    if busy:
        raise SystemExit(
            f"[e2e] FAIL: 端口 {PORT} 已被占用 —— 疑似残留 pbc-server.exe。\n"
            f"        继续跑会连到那个实例（appdata/DB 均非本次），结果不可信。\n"
            f"        请先结束残留进程，或用 PBC_E2E_PORT 换一个端口。"
        )


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


def _require(value, flag: str, env_name: str):
    """参数缺失时报错并指出环境变量注入通道。

    密钥不允许写在命令行里 —— 命令行会进 shell history、进程表
    （``tasklist`` / ``/proc/<pid>/cmdline``）与 CI 日志，等同于泄漏。
    环境变量是既有的密钥注入通道（见 tests/e2e_proc.LLM_KEY_ENV）。
    """
    if not value:
        raise SystemExit(f"缺少 {flag}（也可用环境变量 {env_name} 注入）")
    return value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf-key", default=os.environ.get("PBC_E2E_SILICONFLOW_KEY"))
    ap.add_argument("--paddle-token", default=os.environ.get("PBC_E2E_PADDLE_TOKEN"))
    ap.add_argument("--mineru-token", default=os.environ.get("PBC_E2E_MINERU_TOKEN"))
    ap.add_argument("--rounds", default="pdf,img,mineru")
    ap.add_argument(
        "--exe", default=os.environ.get("PBC_E2E_EXE", ""),
        help="被测 pbc-server.exe。默认 dist/pbc-server（PyInstaller 直接产物）；"
             "要测**真正分发的那份**请指向 "
             "dist-electron*/win-unpacked/resources/pbc-server/pbc-server.exe，"
             "或设 PBC_E2E_EXE 环境变量。",
    )
    args = ap.parse_args()
    args.sf_key = _require(args.sf_key, "--sf-key", "PBC_E2E_SILICONFLOW_KEY")
    args.paddle_token = _require(args.paddle_token, "--paddle-token", "PBC_E2E_PADDLE_TOKEN")
    args.mineru_token = _require(args.mineru_token, "--mineru-token", "PBC_E2E_MINERU_TOKEN")
    rounds = args.rounds.split(",")

    appdata = os.path.join(tempfile.gettempdir(), "pbc_e2e_appdata")
    os.makedirs(appdata, exist_ok=True)
    env = dict(os.environ, APPDATA=appdata, PORT=str(PORT), NO_WINDOW="1")

    # 别写死产物路径：默认测 PyInstaller 直接产物，但用户双击运行的是 Electron
    # 包里内嵌的那一份 —— 只测前者等于"测了 A、发了 B"（见 DEPLOYMENT.md 检查清单）。
    exe = args.exe or os.path.join("dist", "pbc-server", "pbc-server.exe")
    if not os.path.isfile(exe):
        print(f"[e2e] FAIL: 被测产物不存在 {exe}")
        sys.exit(2)
    exe = os.path.abspath(exe)
    print(f"[e2e] target exe = {exe}")
    _assert_port_free()
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
                                                force=True, expect_backend="paddle")
                elif rnd == "img":
                    # 图像→PDF 转换路径：不关心哪个 OCR 引擎（取决于前序轮次
                    # 切换后的当前设置），显式传 None 表示"本轮不校验后端"。
                    results["img"] = run_upload(c, "test1.jpg", "image/jpeg",
                                                expect_types=[], force=True,
                                                expect_backend=None)
                elif rnd == "mineru":
                    # switch backend to mineru and re-run same pdf with force
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "mineru"})
                    print(f"[e2e] switch to mineru -> {r.status_code}")
                    results["mineru"] = run_upload(c, os.environ.get("E2E_PDF", "e2e_test.pdf"),
                                                   "application/pdf",
                                                   expect_types=[], force=True,
                                                   expect_backend="mineru")
                elif rnd == "real":
                    # real-world scanned batch record (handwriting), paddle primary
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "paddle"})
                    print(f"[e2e] switch to paddle -> {r.status_code}")
                    results["real"] = run_upload(
                        c, os.environ.get("E2E_PDF", "丝裂霉素提取批记录.pdf"),
                        "application/pdf", expect_types=[], force=True,
                        page_chars=True, budget_kind="real",
                        expect_backend="paddle")
                elif rnd == "real-mineru":
                    # same real pdf with mineru backend (dual-engine completeness)
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "mineru"})
                    print(f"[e2e] switch to mineru -> {r.status_code}")
                    results["real-mineru"] = run_upload(
                        c, os.environ.get("E2E_PDF", "丝裂霉素提取批记录.pdf"),
                        "application/pdf", expect_types=[], force=True,
                        page_chars=True, budget_kind="real",
                        expect_backend="mineru")
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
                        c, os.environ.get("E2E_PDF_SMALL", "e2e_test.pdf"),
                        expect_backend="mineru")
                    c.post(f"{API}/api/settings", json={"ocr_dual_compare": False})
                elif rnd == "rot":
                    # round-23 A: sideways-page rotation self-heal (paddle primary)
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "paddle"})
                    print(f"[e2e] switch to paddle -> {r.status_code}")
                    results["rot"] = run_rot(c, "e2e_rot.pdf",
                                             expect_backend="paddle")
                elif rnd == "robust":
                    # M3 尺寸鲁棒性：合成样本 + "不得静默标记成功"断言
                    r = c.post(f"{API}/api/settings", json={"ocr_backend": "paddle"})
                    print(f"[e2e] switch to paddle -> {r.status_code}")
                    results["robust"] = run_robust(c, expect_backend="paddle")
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


def _pdf_page_count(path: str) -> int:
    """PDF 页数（用于把轮次预算表达成"基线 + 每页"）。判不出来返回 0。"""
    try:
        import fitz

        with fitz.open(path) as doc:
            return int(doc.page_count)
    except Exception as e:      # 文件缺失/损坏 → 由调用方走保守回退
        print(f"[e2e] WARN 读取页数失败 {path}: {e}")
        return 0


# 轮次预算 = 基线 + 每页 × 页数。斜率按**该轮实测每页成本**取，全部大于观测值。
#
# 为什么不再写死单值（同类缺陷已犯两次）：
#   - 2026-09-04 rot 轮 620s > 默认 600s，被误杀 20s；
#   - 2026-09-16 pdf 轮 835s > 默认 600s，被判超时（该 job 之后正常进了 review）。
# 固定值对小文档太紧、对大文档太松，必须随页数走。这套"基线 + 每页"的
# 表达与 `core/watchdog.py` 的阈值同一思路（那边是产品侧，这边是测试侧）。
#
# 实测参照（冻结产物，见 docs/ADVERSARIAL_AUDIT.md §5）：
#   pdf  835s / 6 页  ≈ 139s/页（含空页自愈）
#   rot 1031s / 4 页  ≈ 258s/页（每页最多 3 次完整上游 OCR 旋转探测）
#   real ~1975s / 51 页 ≈ 39s/页
_ROUND_BUDGETS = {
    "pdf": (900, 180),
    "rot": (1200, 300),
    "real": (1800, 120),
}
_ROUND_ENV = {
    "pdf": "E2E_PDF_TIMEOUT",
    "rot": "E2E_ROT_TIMEOUT",
    "real": "E2E_REAL_TIMEOUT",
}
# 页数读不出来时的保守回退：按本项目实测最大真实件（51 页）算 ——
# 宁可多等，也不要把一次真实长跑误判成失败（误判比慢更贵）。
_UNKNOWN_PAGE_FALLBACK = 51


def _round_budget_s(path: str, kind: str) -> int:
    """该轮的终态等待预算（秒）。`E2E_*_TIMEOUT` 显式设置时优先。"""
    override = os.environ.get(_ROUND_ENV[kind])
    if override:
        try:
            return int(override)
        except ValueError:
            print(f"[e2e] WARN {_ROUND_ENV[kind]}={override!r} 非整数，改用公式预算")
    base, per_page = _ROUND_BUDGETS[kind]
    pages = _pdf_page_count(path) or _UNKNOWN_PAGE_FALLBACK
    return base + per_page * pages


# 轮次预算一律由 `_round_budget_s(path, kind)` 按"基线 + 每页"计算；
# 显式 `timeout_s` 传参仍然优先（个别轮次需要特殊预算时用）。


def run_upload(c, path, mime, expect_types, force, timeout_s=None, page_chars=False,
               expect_backend=None, budget_kind="pdf"):
    """上传 → 跑到终态 → 汇总证据。

    ``expect_backend``：断言 ``jobs.ocr_backend_used`` 等于该值。

    **必须显式传**。主后端提交失败会自动 failover 到备选（见
    ``_get_ocr_chain``）：Paddle 上游返回 10010「任务提交队列已满」时，
    整轮会静默改用 MinerU，而终态仍是 ``review`` —— 不校验后端就会把
    一次"用 Paddle 跑"的结论记成通过（2026-09-15 实测踩到）。

    ``budget_kind``：``timeout_s`` 未显式给出时，用它选 `_ROUND_BUDGETS`
    里的斜率（``pdf`` / ``rot`` / ``real``）。
    """
    if timeout_s is None:
        timeout_s = _round_budget_s(path, budget_kind)
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
    # 证据文件按 **job_id** 分文件：同一轮复跑会产生新 job，若按 stem 命名且 append，
    # 多次运行的帧会混进同一个文件、事后无法分辨哪一帧属于哪一轮（2026-09-16 实测：
    # 一个 e2e_sse_e2e_rot.jsonl 里累积了 7 个 job 的帧）。采集器**断线重连需要在
    # while 内重新 open**，所以不能改成 "w"（会擦掉已采到的帧），只能换文件名。
    sse_log = os.path.join("devlogs", f"e2e_sse_{Path(path).stem}_{job_id}.jsonl")
    sse_thread = _sse_recorder(job_id, sse_log, sse_stats)
    st, d = wait_terminal(c, job_id, timeout_s=timeout_s)
    sse_thread.join(timeout=15)
    dur = int(time.time() - t0)
    used_backend = d.get("ocr_backend_used")
    print(f"[e2e] {path}: status={st} in {dur}s (pages={d.get('total_pages')})"
          f" backend={used_backend}")
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
    # 后端校验：failover 发生后终态依然正常，只有 ocr_backend_used 能揭穿
    backend_err = backend_mismatch(expect_backend, used_backend)
    if backend_err:
        ok = False
        print(f"[e2e] {path}: BACKEND MISMATCH — {backend_err}"
              f"（主后端失败已 failover；本轮**不得**记为 {expect_backend} 的成果）")
    # SSE 证据摘要：事件数 / phase 迁移链 / 终帧
    phases = [t.split(":")[0] for t in sse_stats["transitions"]]
    sse_ok = bool(sse_stats["events"]) and "done" in phases
    print(f"[e2e] {path}: SSE events={sse_stats['events']} phases={sse_stats['transitions']}"
          f" final={((sse_stats.get('last') or {}).get('status'))} -> {'OK' if sse_ok else 'CHECK'}")
    return {"ok": ok, "status": st, "duration_s": dur,
            "pages": d.get("total_pages"), "findings": len(fs),
            "job_id": job_id,
            "ocr_backend_used": used_backend,
            "backend_mismatch": backend_err,
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
    sse_log = os.path.join("devlogs", f"e2e_sse_cancel_{job_id}.jsonl")
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


def run_dual(c, path, mime="application/pdf", expect_backend=None):
    """Gate-3 dual-compare round: primary mineru + secondary paddle re-run.

    已知该合成 PDF 在两引擎下 p2/p4 覆盖率 <0.85 —— 预期门禁产生差异
    findings 并强制 partial_review。断言：audit 含 dual_compare_done；
    终态为 review/partial_review（差异页数仅作证据输出，不作硬断言 ——
    LLM 抽取存在轮次方差，两引擎可能偶发一致）。
    """
    res = run_upload(c, path, mime, expect_types=[], force=True,
                     expect_backend=expect_backend)
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


def run_rot(c, path, mime="application/pdf", timeout_s=None, expect_backend=None):
    """round-23 A: sideways-page rotation self-heal round.

    合成样本（gen_e2e_rot_pdf.py）：p1/p4 正常，p2 内容横置 90°、p3 横置
    270°（非 /Rotate 元数据 — show_pdf_page 旋转嵌入）。两条恢复路径均合法：
    1. 直识：PaddleOCR-VL 直接识别横置文本 → raw_html 含页内标记；
    2. 自愈：直识稀疏/空 → 切片重试仍空 → 旋转探测 90/270/180° 重渲染
       重 OCR → 采纳（ocr_diagnostics.rotation_deg + 审计
       stage1_rotation_recovered）。

    硬断言（内容不丢失 — 用户核心诉求）：p2/p3 标记文本在最终 raw_html
    可见；自愈路径额外断言 rotation_deg 落库与审计可追溯。p1 批号基准
    （B2025001）同页可见性一并校验（对照页未受旋转污染）。
    """
    if timeout_s is None:
        timeout_s = _round_budget_s(path, "rot")
    res = run_upload(c, path, mime, expect_types=[], force=True,
                     timeout_s=timeout_s, expect_backend=expect_backend)
    job_id = res.get("job_id")
    if not job_id or not res.get("ok"):
        return res
    markers = {2: "横置九十度工序表", 3: "横置二百七十度参数表"}
    paths, lost = [], []
    rot_pages = {}
    for p, marker in markers.items():
        try:
            r = c.get(f"{API}/api/jobs/{job_id}/pages/{p}", timeout=15)
            pd = r.json() if r.status_code == 200 else {}
        except Exception:
            pd = {}
        text = pd.get("raw_html") or ""
        diag = pd.get("ocr_diagnostics") or {}
        if diag.get("rotation_deg") is not None:
            rot_pages[p] = diag["rotation_deg"]
        if marker in text:
            via = (f"rotation@{diag['rotation_deg']}°"
                   if diag.get("rotation_deg") is not None else "direct")
            paths.append({"page": p, "via": via})
        else:
            lost.append({"page": p, "rotation_deg": diag.get("rotation_deg"),
                         "chars": len(text)})
    # 对照页：p1 正常封面批号基准可见（旋转链不误伤正常页）
    try:
        r = c.get(f"{API}/api/jobs/{job_id}/pages/1", timeout=15)
        p1_ok = r.status_code == 200 and "B2025001" in (r.json().get("raw_html") or "")
    except Exception:
        p1_ok = False
    # 自愈路径须审计可追溯（直识路径无旋转事件，属正常）
    audit_rot = []
    if rot_pages:
        try:
            r = c.get(f"{API}/api/jobs/{job_id}/audit?limit=300", timeout=15)
            body = r.json()
            items = body if isinstance(body, list) else body.get("entries", [])
            audit_rot = [a.get("action", "") for a in items
                         if a.get("action") == "stage1_rotation_recovered"]
        except Exception:
            audit_rot = []
    print(f"[e2e] rot: sideways pages via={paths} lost={lost} p1_batch_visible={p1_ok} "
          f"rotation_deg={rot_pages} audit_rotation_events={len(audit_rot)}")
    res["rot_paths"] = paths
    res["rot_lost"] = lost
    res["p1_batch_visible"] = p1_ok
    res["rotation_deg"] = rot_pages
    res["audit_rotation_events"] = len(audit_rot)
    res["ok"] = (bool(res.get("ok")) and not lost and p1_ok
                 and (not rot_pages or bool(audit_rot)))
    if not res["ok"]:
        print(f"[e2e] rot round FAIL: lost={lost} p1_batch_visible={p1_ok} "
              f"rot_without_audit={bool(rot_pages) and not audit_rot}")
    return res


def _ensure_ocr_samples():
    """生成/复用 M3 合成样本 → {name: path}（scripts/gen_ocr_samples.py）。"""
    gen_path = Path(__file__).resolve().parent / "scripts" / "gen_ocr_samples.py"
    spec = importlib.util.spec_from_file_location("gen_ocr_samples", gen_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.build_all(Path(__file__).resolve().parent / "devlogs" / "ocr_samples")


def _page_payload(c, job_id, pno):
    try:
        r = c.get(f"{API}/api/jobs/{job_id}/pages/{pno}", timeout=15)
        if r.status_code == 200:
            return r.json() or {}
    except Exception:
        pass
    return {}


def _has_non_ok_signal(pd):
    """页是否显式携带"非完整/已降级"信号（"不得静默标记成功"契约）。

    任一成立即视为未静默成功：完整性判定为 incomplete；自愈页保留了
    prior_diagnostics.integrity=incomplete；旋转探测已尝试但未果；或
    raw_html 带 [OCR 警告:] 横幅。
    """
    diag = pd.get("ocr_diagnostics") or {}
    if diag.get("integrity") == "incomplete":
        return True
    if (diag.get("prior_diagnostics") or {}).get("integrity") == "incomplete":
        return True
    if diag.get("rotation_probed"):
        return True
    return "[OCR 警告:" in (pd.get("raw_html") or "")


def run_robust(c, mime="application/pdf", expect_backend=None):
    """M3 尺寸鲁棒性轮（冻结包 e2e）：合成样本 + "不得静默标记成功"断言。

    - o6_small_font_low_dpi：小字号 + 低 DPI（不可无损修复）→ 页必须显式
      携带非完整信号（integrity=incomplete / prior_diagnostics / 旋转已
      探测 / [OCR 警告:] 横幅之一）；
    - o1_small_box：微型盒已放大到目标 DPI → 不得留下低密度/低 DPI 告警
      （修复有效且无假告警）。
    """
    try:
        samples = _ensure_ocr_samples()
    except Exception as e:
        return {"ok": False, "err": f"generate samples failed: {e}"}
    res = {"ok": True}

    # 1) 不可无损修复页：必须被显式标记（不得静默成功）
    o6 = run_upload(c, samples["o6_small_font_low_dpi"], mime,
                    expect_types=[], force=True, expect_backend=expect_backend)
    res["o6"] = {k: v for k, v in o6.items() if k != "findings"}
    if o6.get("ok") and o6.get("job_id"):
        pd1 = _page_payload(c, o6["job_id"], 1)
        res["o6_page1_signal"] = _has_non_ok_signal(pd1)
        res["o6_page1_diag"] = pd1.get("ocr_diagnostics") or {}
        if not res["o6_page1_signal"]:
            res["ok"] = False
            print("[e2e] robust FAIL: o6 page1 无任何非完整信号（静默成功）")
    else:
        res["ok"] = False
        print(f"[e2e] robust FAIL: o6 轮未完成 {res['o6'].get('status')!r}")

    # 2) 已规范化页：不得稀疏 / 不得误告警
    o1 = run_upload(c, samples["o1_small_box"], mime,
                    expect_types=[], force=True, expect_backend=expect_backend)
    res["o1"] = {k: v for k, v in o1.items() if k != "findings"}
    if o1.get("ok") and o1.get("job_id"):
        diag = (_page_payload(c, o1["job_id"], 1)).get("ocr_diagnostics") or {}
        prior = diag.get("prior_diagnostics") or {}
        eff = diag.get("effective_dpi")
        if eff is None:
            eff = prior.get("effective_dpi")
        low = diag.get("low_dpi")
        if low is None:
            low = prior.get("low_dpi")
        res["o1_page1_dpi"] = eff
        res["o1_page1_low_dpi"] = bool(low)
        if not (low is not True and (eff is None or eff >= 150)):
            res["ok"] = False
            print(f"[e2e] robust FAIL: o1 page1 规范化后仍低密度 "
                  f"eff={eff} low_dpi={low}")
    else:
        res["ok"] = False
        print(f"[e2e] robust FAIL: o1 轮未完成 {res['o1'].get('status')!r}")
    return res


if __name__ == "__main__":
    main()
