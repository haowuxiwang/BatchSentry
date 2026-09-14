"""M6 端到端验证（真实 uvicorn + 真实 HTTP，非 ASGI in-process）。

为什么要独立脚本：ASGI in-process 测试（tests/integration）拿不到真实
EventSource 的线上时序 —— 而 M6 的三项改动恰恰都是**线上行为**：
  T6.1 `retry:` 帧与推送间隔是否真的一致（2s）
  T6.2 终态 job 在聚合流里是否仍被推送（且不再重算）
  T6.3/T6.4 复核页与 findings API 是否带上三色分级

隔离：用**临时工作目录**启动服务（`cwd=tmp` + `PYTHONPATH=repo`）——
config.py 在非 frozen 模式把配置解析为 **cwd 相对的 `config.json`**，而 JSON
配置里的 `DATABASE_PATH` 会**覆盖**进程环境变量（config.py 明示 JSON 优先级 1）。
所以只设 `DATABASE_PATH` 环境变量是**无效**的：实测会把服务指回开发库
`data/pharma.db`（一次真实踩坑）。临时目录里不放 config.json，`DATABASE_PATH`
与 `OUTPUT_DIR` 便都落在临时目录内。模板/静态/知识库路径走 `__file__`，不受
cwd 影响。

用法：python tests/e2e_m6_sse.py
退出码：0 全过 / 1 有失败。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.e2e_proc import spawn_server, stop_server  # noqa: E402

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
JOB = "e2e-m6-job"
JOB_ACTIVE = "e2e-m6-active"

RESULTS: list[tuple[str, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    RESULTS.append(("PASS", name, detail))
    print(f"  OK  {name} {detail}")


def fail(name: str, detail: str = "") -> None:
    RESULTS.append(("FAIL", name, detail))
    print(f"  XX  {name} {detail}")


def _seed(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    # 终态 job 必须带 finished_at：聚合流只推"活跃 + 近 10 分钟内终态"
    # （listings.py:86-88 的时间窗），finished_at IS NULL 会被正确排除——
    # 用与生产同款的 datetime('now','localtime') 保证格式一致。
    con.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages, finished_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
        (JOB, "m6.pdf", "/tmp/m6.pdf", "review", 3),
    )
    con.execute(
        "INSERT INTO jobs (id, filename, pdf_path, status, total_pages) "
        "VALUES (?, ?, ?, ?, ?)",
        (JOB_ACTIVE, "m6b.pdf", "/tmp/m6b.pdf", "analyzing", 3),
    )
    rows = [
        (1, "time_reversal", "warning", "rule", "页内时间倒序"),
        (1, "batch_inconsistency", "critical", "rule", "跨页批号不一致"),
        (1, "completeness", "info", "llm_page", "步骤签名缺失"),
        (2, "alteration", "warning", "rule", "涂改未划改签名"),
    ]
    for page, ftype, sev, src, desc in rows:
        con.execute(
            "INSERT INTO findings (job_id, page, type, severity, source, "
            "description, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (JOB, page, ftype, sev, src, desc),
        )
    con.commit()
    con.close()


def _sse_frames(url: str, want: int, max_wait: float = 12.0):
    """读 SSE，返回 (retry_ms, [{t, ev, data}...])，最多取 want 条 data 帧。"""
    retry_ms = None
    frames: list[dict] = []
    ev_name = None
    t0 = time.monotonic()
    with requests.get(url, stream=True, timeout=max_wait) as r:
        if r.status_code != 200:
            return r.status_code, retry_ms, frames
        for raw in r.iter_lines(decode_unicode=True):
            if time.monotonic() - t0 > max_wait:
                break
            line = (raw or "").strip()
            if line.startswith("retry:"):
                retry_ms = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                ev_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                frames.append({"t": time.monotonic(), "ev": ev_name, "data": payload})
                ev_name = None
                if len(frames) >= want:
                    break
    return 200, retry_ms, frames


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pbc_e2e_m6_"))
    # 相对路径按 config.py 的默认值解析，全部落在 tmp 内（见模块 docstring）
    db_path = tmp / "data" / "pharma.db"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PBC_NO_FILE_LOG"] = "1"
    # 显式清掉可能存在的覆盖源，确保隔离成立
    for k in ("DATABASE_PATH", "OUTPUT_DIR"):
        env.pop(k, None)

    print(f"[e2e-m6] tmp={tmp}")
    # stdout 落日志文件（未排空的 PIPE 会把服务阻塞在 write，见 tests/e2e_proc.py）
    proc, _logf = spawn_server(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(tmp), env=env,
        log_path=tmp / "e2e-m6-server.log",
    )
    try:
        # 等健康检查（schema 由服务启动时建好）
        up = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
        if not up:
            fail("server_up", "uvicorn 未在 20s 内就绪")
            return 1
        if not db_path.exists():
            fail("db_isolation", f"预期库不存在：{db_path}（隔离未生效）")
            return 1
        ok("server_up", f"{BASE}（隔离库 {db_path}）")

        _seed(db_path)
        ok("seed", "2 jobs（1 终态 + 1 活跃）+ 4 findings")

        # ── T6.4 三色分级：AJAX 端点 ─────────────────────────────
        print("\n=== T6.4 findings API 携带 tier ===")
        r = requests.get(f"{BASE}/api/jobs/{JOB}/findings?page=1", timeout=5)
        d = r.json()
        tiers = {f.get("source"): f.get("tier") for f in d["findings"]}
        if tiers.get("rule") == "rule" and tiers.get("llm_page") == "llm":
            ok("api_tier", f"{tiers}")
        else:
            fail("api_tier", f"实际 {tiers}")
        if d.get("tier_counts") == {"rule": 2, "llm": 1}:
            ok("api_tier_counts", str(d["tier_counts"]))
        else:
            fail("api_tier_counts", f"实际 {d.get('tier_counts')}")

        # ── T6.4 三色面板：SSR 页面 ──────────────────────────────
        print("\n=== T6.4 复核页三色面板 ===")
        html = requests.get(f"{BASE}/jobs/{JOB}/review?page=1", timeout=5).text
        for needle, label in [
            ('id="tier-bar"', "tier_bar"),
            ("规则命中", "label_rule"),
            ("LLM 辅助", "label_llm"),
            ("系统校验通过", "label_pass"),
            ('id="rule-coverage-panel"', "coverage_panel"),
            ('data-tier="rule"', "card_tier_rule"),
            ('data-tier="llm"', "card_tier_llm"),
            ("finding-card tier-rule", "card_class_rule"),
        ]:
            if needle in html:
                ok(f"ssr_{label}")
            else:
                fail(f"ssr_{label}", f"缺少 {needle!r}")

        # ── T6.1 / T6.2 单 job SSE ──────────────────────────────
        print("\n=== T6.1 单 job SSE：retry 与帧 ===")
        code, retry_ms, frames = _sse_frames(
            f"{BASE}/api/jobs/{JOB}/stream", want=2, max_wait=8)
        if code == 200 and retry_ms == 2000:
            ok("sse_retry", f"{retry_ms}ms")
        else:
            fail("sse_retry", f"code={code} retry={retry_ms}")
        if frames and json.loads(frames[0]["data"]).get("status") == "review":
            ok("sse_terminal_frame", frames[0]["data"][:60])
        else:
            fail("sse_terminal_frame", str(frames[:1]))
        if any(f["ev"] == "done" for f in frames):
            ok("sse_done_event")
        else:
            fail("sse_done_event", f"事件名 {[f['ev'] for f in frames]}")

        # ── T6.1 推送间隔实测（活跃 job：连续两帧的间隔应 ≈2s）──
        print("\n=== T6.1 推送间隔实测（活跃 job）===")
        _, retry2, af = _sse_frames(
            f"{BASE}/api/jobs/{JOB_ACTIVE}/stream", want=2, max_wait=10)
        if len(af) >= 2:
            gap = af[1]["t"] - af[0]["t"]
            if 1.5 <= gap <= 3.0:
                ok("sse_interval", f"{gap:.2f}s（期望 ≈2s，允许网络抖动）")
            else:
                fail("sse_interval", f"{gap:.2f}s 偏离 2s 过多")
            if retry2 == 2000:
                ok("sse_retry_active", "2000ms")
            else:
                fail("sse_retry_active", str(retry2))
        else:
            fail("sse_interval", f"仅取到 {len(af)} 帧")

        # ── T6.1/T6.2 聚合流 ────────────────────────────────────
        print("\n=== T6.1/T6.2 聚合流 /api/jobs/live ===")
        _, retry3, lf = _sse_frames(f"{BASE}/api/jobs/live", want=1, max_wait=8)
        if retry3 == 2000:
            ok("live_retry", "2000ms")
        else:
            fail("live_retry", str(retry3))
        if lf:
            payload = json.loads(lf[0]["data"])
            ids = {j.get("id") for j in payload.get("jobs", [])}
            if JOB in ids:
                ok("live_includes_terminal", f"{sorted(ids)}")
            else:
                fail("live_includes_terminal", f"终态 job 未推送：{sorted(ids)}")
            if JOB_ACTIVE in ids:
                ok("live_includes_active", f"{sorted(ids)}")
            else:
                fail("live_includes_active", f"活跃 job 未推送：{sorted(ids)}")
        else:
            fail("live_frames", "未取到聚合帧")

    finally:
        stop_server(proc, _logf, timeout=5)

    passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 54)
    print(f"[e2e-m6] Total: {passed} passed, {failed} failed")
    for s, name, detail in RESULTS:
        if s == "FAIL":
            print(f"  XX {name}: {detail}")
    print("=" * 54)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
