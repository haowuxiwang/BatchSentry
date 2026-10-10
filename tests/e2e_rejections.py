r"""产物级**入口拒绝 / 输入敌意矩阵**（R81 第二十一批 → TODO 0-23）。

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

## 三组判据

**① 拒绝矩阵**（逐例断言状态码 + 文案 + 回显）

| 组 | 用例 | 期望 |
|---|---|---|
| 保留设备名 | `NUL.pdf` / `CON.pdf` / `COM1.pdf` / `LPT1.pdf` / `NUL .pdf` | **400** + 点名「保留设备名」+ 回显文件名 |
| **阴性对照** | `null.pdf` / `console.pdf` / `com10.pdf` / `com0.pdf` / `auxiliary.pdf` | **200**（证明守卫没写成前缀匹配而误伤） |
| 扩展名 | `evil.exe`（内容是合法 PDF） | 400「仅支持 PDF 或图片」 |
| magic bytes | `fake.pdf`（内容是文本） | 400「%PDF-」 |
| 过小 | `tiny.pdf`（2 字节） | 400「过小」 |
| 本机守卫 | 外来 `Origin` / 本机 `Origin` | **403** / **200** |
| **带前缀设备名** | `../../NUL.pdf` / `..\..\NUL.pdf` / `C:\dir\NUL.pdf` / `/tmp/COM1.pdf` / UNC / `a/b/c/../AUX.pdf` | **400**（证明**先剥离、后判定**） |
| 带前缀阴性对照 | `../../null.pdf` / `..\..\console.pdf` | **200** |

**② 敌意文件名**（**不**逐例写死期望名，而断言**安全不变式**）

`../../evil.pdf`、`..\..\x.pdf`、`C:\Windows\x.pdf`、`/etc/x.pdf`、
`\\srv\share\x.pdf`、`..%2f..%2f x.pdf`、`....//....//x.pdf`
⇒ 内容合法 ⇒ 必须 **200**，且回显的 `filename` **不得含** `/` `\` `:`、
不得为空 / `.` / `..`（即**落盘路径不可由客户端控制**）；
另断言**任何用例都不得 5xx**（用户输入问题报 5xx 一律是缺陷）。

**③ 去重 / `force`**：同一份内容连传两次 ⇒ 第 2 次 **409**；带 `force=1` ⇒ **200**。

⚠️ 每个被接受的用例**立刻 cancel**（1 页件；取消发生在 OCR 阶段 ⇒ 不烧 LLM 额度）。
⚠️ 每个用例的 PDF 内容**由构造保证唯一**（嵌入 `uuid4` nonce）—— 不依赖
`fitz` 的随机 `/ID`。否则 md5 去重会返回 409，把阴性对照读成失败
（且那会是一处**静默**的环境依赖：换个静态 fixture 就翻车）。

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
import uuid
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
    ("origin_foreign", "ok_foreign.pdf", "pdf", {"Origin": "http://evil.example.com"},
     403, "non-local", None),
    ("origin_local",   "ok_local.pdf",   "pdf", {"Origin": "http://localhost:1234"},
     200, None, "ok_local.pdf"),
    # ── 带路径前缀的保留设备名（R82：锁定「**先** `Path().name` 剥离、**后**判定」的顺序）──
    # 若顺序反了，`../../NUL.pdf` 的 stem 是 `../../NUL`（∉ 集合）⇒ 绕过守卫 ⇒ 仍写到
    # `NUL` 设备（0 字节静默丢失）。裸名用例锁不住这个顺序（见 r82_mutation.py：3/3 CAUGHT
    # 且旧用例保持绿）。
    ("pfx_dev_nul",       "../../NUL.pdf",        "pdf", {}, 400, "保留设备名", "NUL.pdf"),
    ("pfx_dev_win",       "..\\..\\NUL.pdf",      "pdf", {}, 400, "保留设备名", "NUL.pdf"),
    ("pfx_dev_abs_win",   "C:\\dir\\NUL.pdf",     "pdf", {}, 400, "保留设备名", "NUL.pdf"),
    ("pfx_dev_abs_posix", "/tmp/COM1.pdf",        "pdf", {}, 400, "保留设备名", "COM1.pdf"),
    ("pfx_dev_unc",       "\\\\srv\\s\\LPT1.pdf", "pdf", {}, 400, "保留设备名", "LPT1.pdf"),
    ("pfx_dev_deep",      "a/b/c/../AUX.pdf",     "pdf", {}, 400, "保留设备名", "AUX.pdf"),
    # 阴性对照：带前缀但**不是**设备名 ⇒ 必须 200（否则「凡带斜杠就拒」也能过）
    ("pfx_ctl_null",      "../../null.pdf",       "pdf", {}, 200, None, "null.pdf"),
    ("pfx_ctl_console",   "..\\..\\console.pdf",  "pdf", {}, 200, None, "console.pdf"),
)

#: 敌意文件名 —— **只断言安全不变式**（见模块 docstring ②）。
HOSTILE = (
    ("host_posix_rel",  "../../evil_rel.pdf"),
    ("host_backslash",  "..\\..\\evil_bs.pdf"),
    ("host_abs_win",    "C:\\Windows\\evil_abs.pdf"),
    ("host_abs_posix",  "/etc/passwd_abs.pdf"),
    ("host_unc",        "\\\\srv\\share\\evil_unc.pdf"),
    ("host_encoded",    "..%2f..%2fevil_enc.pdf"),
    ("host_dots",       "....//....//evil_dots.pdf"),
    ("host_drive_only", "C:evil_drive.pdf"),
)

_BAD_NAME_CHARS = ("/", "\\", ":")


def _sha12(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def _unique_pdf(tag: str) -> bytes:
    """最小合法 PDF；内容**由构造保证唯一**（嵌入 uuid4 nonce）。

    ⚠️ 不要改成静态 fixture：服务端有内容 md5 去重（同内容第二次返回 409），
    静态字节会把阴性对照读成失败 —— 而且那是**静默**的（换台机器才炸）。
    """
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), f"probe {tag} {uuid.uuid4().hex}")
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


def _name_ok(name) -> tuple[bool, str]:
    """落盘名安全不变式：非空、非 `.`/`..`、不含任何路径分隔符或盘符冒号。"""
    if not isinstance(name, str) or not name:
        return False, f"filename 为空或非字符串：{name!r}"
    for ch in _BAD_NAME_CHARS:
        if ch in name:
            return False, f"filename 含路径分隔符 {ch!r}：{name!r}"
    if name in (".", ".."):
        return False, f"filename 是 {name!r}"
    return True, ""


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


def _llm_provider_configured(c: httpx.Client) -> bool:
    """前置条件：产物必须已配置**真实** LLM provider（`configured=true`）。

    为什么必须**前置**判定（2026-10-10 实测）：`api/jobs/upload.py` 在"未配置
    provider"时对**每一个**上传都返回 400（`Upload rejected: no LLM provider
    configured`）⇒ 矩阵里"期望 400"的用例**全部假绿**、"期望 200"的用例全部变红
    ⇒ 表面 **13/34**，实则**整轮零判别力**。这正是"用例存在 ≠ 断言有效"的产物级版本；
    只有**阴性对照**能暴露它，而"13/34"极易被误读成"守卫大部分有效"。

    本产物读的是冻结路径 `%APPDATA%/PBC/config.json`（e2e 下 = `_APPDATA/PBC/`），
    **不是**仓库根的 `config.json` ⇒ 换机/清临时目录后必须重新 seed。

    ⚠️ provider 列表在响应里的路径是 **`llm.providers`**（不是顶层 `providers`）
    —— 首版按顶层取 ⇒ 永远取到 `[]` ⇒ **恒判 False**（把"配置齐全"也误判成未配置）。
    这正是本函数存在的意义所要求的：**阳性对照必须跑**（见 PITFALLS §五十四 教训 2）。
    """
    try:
        r = c.get(f"{_API}/api/settings", timeout=10)
        if r.status_code != 200:
            return False
        providers = (r.json().get("llm") or {}).get("providers") or []
        return any(p.get("configured") for p in providers)
    except Exception:                                            # noqa: BLE001
        return False


def _cancel(c: httpx.Client, r: httpx.Response):
    """立刻取消，别烧 OCR/LLM 额度。返回 cancel 的状态码（失败记字符串）。"""
    try:
        jid = r.json().get("job_id")
        return c.post(f"{_API}/api/jobs/{jid}/cancel", timeout=15).status_code
    except Exception as e:                                       # noqa: BLE001
        return f"ERR {type(e).__name__}"


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

        cancelled = _cancel(c, r) if status == 200 else None
        rows.append({
            "group": "matrix", "case": name, "filename": fname, "status": status,
            "want_status": want_status, "echo_ok": echo_ok, "cancelled": cancelled,
            "ok": not problems, "problems": problems,
            "body_head": text[:160].replace("\n", " "),
        })
        _emit(rows[-1])
    return rows


def _run_hostile(c: httpx.Client) -> list[dict]:
    """敌意文件名：合法内容 ⇒ 必须 200 且落盘名满足安全不变式；且**不得** 5xx。"""
    rows: list[dict] = []
    for name, fname in HOSTILE:
        files = {"file": (fname, _unique_pdf(name), "application/pdf")}
        try:
            r = c.post(f"{_API}/api/jobs", files=files, timeout=60)
            status, text = r.status_code, r.text
        except Exception as e:                                   # noqa: BLE001
            status, text = -1, f"{type(e).__name__}: {e}"

        problems: list[str] = []
        got = None
        if status != 200:
            problems.append(f"状态码 {status}（期望 200 —— 合法 PDF 不该被拒）")
        else:
            try:
                got = r.json().get("filename")
            except Exception:                                    # noqa: BLE001
                got = None
            good, why = _name_ok(got)
            if not good:
                problems.append(why)
        if status >= 500:
            problems.append(f"**5xx**（用户输入不得报 5xx）：{text[:120]}")

        cancelled = _cancel(c, r) if status == 200 else None
        rows.append({
            "group": "hostile", "case": name, "filename": fname, "status": status,
            "stored_name": got, "cancelled": cancelled,
            "ok": not problems, "problems": problems,
            "body_head": text[:160].replace("\n", " "),
        })
        _emit(rows[-1])
    return rows


def _run_dedup(c: httpx.Client) -> list[dict]:
    """去重 / `force`：同一份内容连传两次 ⇒ 第 2 次 409；带 force=1 ⇒ 200。"""
    payload = _unique_pdf("dedup_shared")        # 同一次运行内**刻意复用**
    rows: list[dict] = []

    def post(force: bool, tag: str):
        files = {"file": (f"dedup_{tag}.pdf", payload, "application/pdf")}
        url = f"{_API}/api/jobs" + ("?force=1" if force else "")
        r = c.post(url, files=files, timeout=60)
        if r.status_code == 200:
            _cancel(c, r)
        return r.status_code, r.text

    s1, _ = post(False, "first")
    s2, t2 = post(False, "second")
    s3, _ = post(True, "forced")
    for case, got, want, extra in (
        ("dedup_first",  s1, 200, "首次上传应被接受"),
        ("dedup_second", s2, 409, "同内容第二次应被 409 去重拒绝"),
        ("dedup_forced", s3, 200, "force=1 应绕过去重"),
    ):
        problems = [] if got == want else [f"状态码 {got}（期望 {want}）：{extra}"]
        rows.append({"group": "dedup", "case": case, "status": got,
                     "want_status": want, "ok": not problems, "problems": problems,
                     "body_head": (t2 if case == "dedup_second" else "")[:160]})
        _emit(rows[-1])
    return rows


def _emit(r: dict) -> None:
    flag = "OK  " if r["ok"] else "FAIL"
    extra = f"  stored={r['stored_name']!r}" if r.get("stored_name") is not None else ""
    print(f"  {flag} {r['case']:16s} {r.get('filename', ''):24s} -> {r['status']}"
          f"{extra}{'  cancel=' + str(r['cancelled']) if r.get('cancelled') is not None else ''}"
          f"{'  ' + '; '.join(r['problems']) if r['problems'] else ''}", flush=True)


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

    # 防空转：payload 唯一性由构造保证 —— 造两个必须不同，否则去重会把对照读成失败
    assert _unique_pdf("selfcheck") != _unique_pdf("selfcheck"), (
        "payload 唯一性自检失败 ⇒ 内容 md5 去重会把阴性对照读成 409"
    )

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
            if not _llm_provider_configured(c):
                print(
                    f"!! 前置条件未满足：产物未配置任何**真实** LLM provider"
                    f"（GET {_API}/api/settings 无 configured=true）⇒ 每个上传都会 400，"
                    f"矩阵将**无判别力**（阳性用例假绿、阴性对照全红）。\n"
                    f"   先 seed：{_APPDATA / 'PBC' / 'config.json'}"
                    f"（冻结版只读该路径，**不**读仓库根 config.json）。fail-closed。")
                return 2
            print("[probe] health OK —— ① 拒绝矩阵")
            rows += _run_matrix(c)
            print("[probe] ② 敌意文件名（安全不变式）")
            rows += _run_hostile(c)
            print("[probe] ③ 去重 / force")
            rows += _run_dedup(c)
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        logf.close()

    bad = [r for r in rows if not r["ok"]]
    _VERDICT.write_text(json.dumps(
        {"total": len(rows), "ok": len(rows) - len(bad),
         "groups": {g: sum(1 for r in rows if r["group"] == g)
                    for g in ("matrix", "hostile", "dedup")},
         "exe_sha256_12": _sha12(_DIRECT), "port": _PORT, "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 78)
    print(f"入口拒绝 / 输入敌意矩阵：{len(rows) - len(bad)}/{len(rows)} 达成"
          f"（判定源：{_VERDICT.name}）")
    for r in bad:
        print(f"  !! {r['case']}: {'; '.join(r['problems'])}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
