"""产物级**入口拒绝矩阵**（R81 第二十一批 → TODO 0-23）。

## 为什么需要

R80 新增的两条守卫（Electron fuses / Windows 保留设备名）与既有的入口守卫
（扩展名白名单 / magic bytes / 本机请求 / 路径剥离）此前**只在单测层**被验证过 ——
单测走 `ASGITransport`（**进程内**、源码树、`Path` 未被冻结）。产物层此前
**只有 `/health` 冒烟**（`devlogs/_verify/smoke_packaged_server.py`），
**零**拒绝路径覆盖。

"单测绿"与"产物绿"是两件事：冻结后 multipart 解析、异常消息编码、状态码传播、
`_WIN_RESERVED_STEMS` 的 `frozenset` 常量、`Path(...).name` 的语义都可能变。
本驱动把这条缝补上，并作为**可重跑的回归**留在仓里（`devlogs/` 被 gitignore
⇒ 放在那里的证据会随磁盘消失）。

## 矩阵

| 组 | 用例 | 期望 |
|---|---|---|
| 保留设备名 | `NUL.pdf` / `CON.pdf` / `COM1.pdf` / `LPT1.pdf` / `NUL .pdf` | **400** + 点名「保留设备名」+ 回显文件名 |
| **阴性对照** | `null.pdf` / `console.pdf` / `com10.pdf` / `com0.pdf` / `auxiliary.pdf` | **200**（证明守卫没写成前缀匹配而误伤） |
| 扩展名 | `evil.exe`（内容是合法 PDF） | 400「仅支持 PDF 或图片」 |
| magic bytes | `fake.pdf`（内容是文本） | 400「%PDF-」 |
| 过小 | `tiny.pdf`（2 字节） | 400「过小」 |
| 路径剥离 | `../../evil.pdf` | **200** 且回显 `filename == "evil.pdf"`（穿越不成立） |
| 本机守卫 | 外来 `Origin` / 本机 `Origin` | **403** / **200** |

⚠️ 每个被接受的用例**立刻 cancel**（1 页件；取消发生在 OCR 阶段 ⇒ 不烧 LLM 额度）。
⚠️ 每个用例用**内容唯一**的 PDF（否则 md5 去重会返回 409，把对照读成失败）。

跑法（**需先有产物**；与 `e2e_run.py` 同端口约定，避免撞上开发实例）：
    python tests/e2e_rejections.py
    python tests/e2e_rejections.py --verify-only      # 只重判上次存档
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
_DIRECT = REPO / "dist" / "pbc-server" / "pbc-server.exe"
_EMBEDDED = (REPO / "dist-electron" / "win-unpacked" / "resources"
             / "pbc-server" / "pbc-server.exe")
_APPDATA = Path(tempfile.gettempdir()) / "pbc_e2e_appdata"
_PORT = int(os.environ.get("PBC_E2E_PORT", "58799"))
_API = f"http://127.0.0.1:{_PORT}"
_LOG = REPO / "devlogs" / "e2e_rejections.log"
_VERDICT = REPO / "devlogs" / "e2e_rejections.json"

#: (用例名, 文件名, 内容类型, 额外头, 期望状态码, 期望子串, 期望回显文件名 or None)
#: 内容类型：'pdf' = 唯一的最小合法 PDF；'text' = 非 PDF 文本；'tiny' = 2 字节。
MATRIX = (
    # ── 保留设备名（R80 0-22 的新守卫）────────────────────────────────
    ("dev_nul",        "NUL.pdf",    "pdf",  {}, 400, "保留设备名", "NUL.pdf"),
    ("dev_con",        "CON.pdf",    "pdf",  {}, 400, "保留设备名", "CON.pdf"),
    ("dev_com1",       "COM1.pdf",   "pdf",  {}, 400, "保留设备名", "COM1.pdf"),
    ("dev_lpt1",       "LPT1.pdf",   "pdf",  {}, 400, "保留设备名", "LPT1.pdf"),
    ("dev_nul_space",  "NUL .pdf",   "pdf",  {}, 400, "保留设备名", "NUL .pdf"),
    # ── 阴性对照：**不得**被设备名守卫误伤（内容合法 ⇒ 应被接受）──────
    ("ctl_null",       "null.pdf",      "pdf", {}, 200, None, "null.pdf"),
    ("ctl_console",    "console.pdf",   "pdf", {}, 200, None, "console.pdf"),
    ("ctl_com10",      "com10.pdf",     "pdf", {}, 200, None, "com10.pdf"),
    ("ctl_com0",       "com0.pdf",      "pdf", {}, 200, None, "com0.pdf"),
    ("ctl_auxiliary",  "auxiliary.pdf", "pdf", {}, 200, None, "auxiliary.pdf"),
    # ── 其他入口守卫（既有，但在产物层从未被验过）──────────────────────
    ("ext_exe",        "evil.exe",   "pdf",  {}, 400, "仅支持 PDF 或图片", None),
    ("magic_bad",      "fake.pdf",   "text", {}, 400, "%PDF-", None),
    ("too_small",      "tiny.pdf",   "tiny", {}, 400, "过小", None),
    ("traversal",      "../../evil.pdf", "pdf", {}, 200, None, "evil.pdf"),
    ("origin_foreign", "ok_foreign.pdf", "pdf", {"Origin": "http://evil.example.com"},
     403, "non-local", None),
    ("origin_local",   "ok_local.pdf",   "pdf", {"Origin": "http://localhost:1234"},
     200, None, "ok_local.pdf"),
)


def _sha12(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def _unique_pdf(tag: str) -> bytes:
    """每个用例一份**内容唯一**的最小合法 PDF（避开 md5 去重 409）。"""
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), f"probe {tag}")
    buf = doc.tobytes()
    doc.close()
    return buf


def _body(kind: str, tag: str) -> bytes:
    if kind == "pdf":
        return _unique_pdf(tag)
    if kind == "text":
        return b"this is definitely not a pdf payload"
    if kind == "tiny":
        return b"ab"
    raise AssertionError(kind)


def _port_busy() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _wait_health(c: httpx.Client, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if c.get(f"{_API}/health", timeout=2).status_code == 200:
                return True
        except Exception:                                        # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _run_matrix(c: httpx.Client) -> list[dict]:
    rows: list[dict] = []
    for name, fname, kind, hdrs, want_status, want_sub, want_echo in MATRIX:
        files = {"file": (fname, _body(kind, name), "application/pdf")}
        try:
            r = c.post(f"{_API}/api/jobs", files=files, headers=hdrs, timeout=60)
            status, text = r.status_code, r.text
        except Exception as e:                                   # noqa: BLE001
            status, text = -1, f"{type(e).__name__}: {e}"

        problems: list[str] = []
        if status != want_status:
            problems.append(f"状态码 {status}（期望 {want_status}）")
        if want_sub and want_sub not in text:
            problems.append(f"响应未含 {want_sub!r}")
        echo_ok = None
        if want_echo is not None:
            if status == 200:
                try:
                    got = r.json().get("filename")
                except Exception:                                # noqa: BLE001
                    got = None
                echo_ok = got == want_echo
                if not echo_ok:
                    problems.append(f"回显 filename={got!r}（期望 {want_echo!r}）")
            else:
                echo_ok = want_echo in text
                if not echo_ok:
                    problems.append(f"错误消息未回显 {want_echo!r}")

        cancelled = None
        if status == 200:                       # 立刻取消，别烧 OCR/LLM 额度
            try:
                jid = r.json().get("job_id")
                cancelled = c.post(f"{_API}/api/jobs/{jid}/cancel", timeout=15).status_code
            except Exception as e:                               # noqa: BLE001
                cancelled = f"ERR {type(e).__name__}"

        rows.append({
            "case": name, "filename": fname, "status": status,
            "want_status": want_status, "echo_ok": echo_ok,
            "cancelled": cancelled, "ok": not problems,
            "problems": problems, "body_head": text[:160].replace("\n", " "),
        })
        flag = "OK  " if not problems else "FAIL"
        print(f"  {flag} {name:15s} {fname:20s} -> {status}"
              f"{'  cancel=' + str(cancelled) if cancelled is not None else ''}"
              f"{'  ' + '; '.join(problems) if problems else ''}", flush=True)
    return rows


def main(argv: list[str]) -> int:
    if "--verify-only" in argv:
        if not _VERDICT.is_file():
            print(f"!! --verify-only 找不到 {_VERDICT}（fail-closed）")
            return 2
        d = json.loads(_VERDICT.read_text(encoding="utf-8"))
        bad = [r for r in d["rows"] if not r["ok"]]
        print(f"重判：{len(d['rows']) - len(bad)}/{len(d['rows'])} 达成")
        for r in bad:
            print(f"  !! {r['case']}: {'; '.join(r['problems'])}")
        return 1 if bad else 0

    if not _DIRECT.is_file():
        print(f"!! 产物不存在：{_DIRECT}（fail-closed —— 先构建）")
        return 2
    if _EMBEDDED.is_file():
        a, b = _sha12(_DIRECT), _sha12(_EMBEDDED)
        print(f"[probe] exe sha256[:12] direct={a} embedded={b} same={a == b}")
        if a != b:
            print("!! 两份产物 exe sha256 不同 —— 一次探测不能同时代表两份")
            return 2

    if _port_busy():
        print(f"!! 端口 {_PORT} 已被占用 —— 疑似残留 pbc-server.exe，结果不可信。"
              f"请先释放（或换 PBC_E2E_PORT）。fail-closed。")
        return 2

    env = {**os.environ, "APPDATA": str(_APPDATA), "PORT": str(_PORT), "NO_WINDOW": "1"}
    logf = open(_LOG, "ab")
    proc = subprocess.Popen(
        [str(_DIRECT)], cwd=str(_DIRECT.parent), env=env,
        stdout=logf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] 产物已启动 pid={proc.pid} port={_PORT} appdata={_APPDATA}")
    rows: list[dict] = []
    try:
        with httpx.Client(timeout=30) as c:
            if not _wait_health(c):
                tail = (_LOG.read_bytes()[-1500:].decode("utf-8", "replace")
                        if _LOG.is_file() else "")
                print(f"!! /health 超时。日志尾：\n{tail}")
                return 3
            print("[probe] health OK，开始入口矩阵")
            rows = _run_matrix(c)
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        logf.close()

    bad = [r for r in rows if not r["ok"]]
    _VERDICT.write_text(json.dumps(
        {"total": len(rows), "ok": len(rows) - len(bad),
         "exe_sha256_12": _sha12(_DIRECT), "port": _PORT, "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 78)
    print(f"入口拒绝矩阵：{len(rows) - len(bad)}/{len(rows)} 达成"
          f"（判定源：{_VERDICT.name}）")
    for r in bad:
        print(f"  !! {r['case']}: {'; '.join(r['problems'])}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
