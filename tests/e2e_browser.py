"""浏览器层 e2e（Playwright + chromium）—— 补上"源码级 JS 护栏"看不见的那一层。

## 为什么需要这一层（这是它不可替代的地方）

仓库里已有 **23 个 JS 护栏**，但它们全是**源码级 / 假 DOM**（`js_sources` 口径）。
它们能证明"函数被调用"，证明不了：

- 上传成功后**真的**跳到了复核页；
- 在复核页点「首页」**真的**回到上传区，而且能**再上传第二份**（这是审计 Q1 的原问题）；
- 点「确认」**真的**改了状态（不是按钮没绑事件）；
- 页面加载过程中**有没有 JS 报错**。

这几件事只有真浏览器 + 真 DOM 能回答。

## 覆盖（8 条 / 19 个判据）

| # | 用例 | 判据 |
|---|---|---|
| B1 | 上传第一份（**3 页**）→ 跳复核页 | URL 匹配 `/jobs/{id}/review`，且 `#page-total` 真的是 3 |
| B2 | 复核页翻页 / 缩放（在 3 页 job 上） | `#page-num` 1→2 且 `#page-total`=3；缩放 100%→125%→100% |
| B3 | 复核页点「首页」→ 回上传区 → **再上传第二份** | URL 回到 `/`、`#upload-area` 可见、第二次 job_id **不同** |
| B4 | 两个任务都跑到**终态** | `jobs.status ∈ TERMINAL_STATUSES`（从源码取，不 import） |
| B5 | 确认 finding —— **两条渲染路径** | 首屏（SSR）与第 2 页（AJAX）各点一次「确认」，两条都落库 `confirmed` |
| B6 | 报告页 + 设置页保存 | 真实 job 的**路由契约**按终态分支（`review`/`partial_review` ⇒ 200；其他 ⇒ **303 → 复核页**）；**夹具** job 的正文含文件名 / Job ID / GMP 抬头；设置页切到 OCR 区改 `ocr.backend` 并保存后 GET 读得回 |
| B7 | 页面 JS / HTTP 健康 | 无**未捕获异常**（`pageerror`）；除 `/api/jobs/{id}/pages/N` 的 404（该页尚无分析产物，接口按设计如此）外**无 4xx/5xx** |
| B8 | 审计面板（C9 / C10） | 点「审计记录」面板真的打开；「变更历史」有数据且是 3 列；「已归档」**空态文案**可见（不是空白）、种一条归档行后表格是 **6 列且类型已中文化**；「关闭」真的移除面板 |

### 八条刻意的设计决定（都是被"假绿 / 假红"逼出来的）

1. **B1 用运行时生成的 3 页 PDF**（`_make_multipage`，PyMuPDF 拼同一页 3 次），
   不是仓库里的单页样本。原因：`devlogs/ocr_samples/` **全是单页** ⇒ 翻页按钮
   恒 `disabled` ⇒ 旧版 `B3_page_nav` 走的是"单页文档，按钮禁用（符合预期）"
   这条**跳过分支**，它绿了但**什么都没验**。要验翻页就得有第 2 页。
2. **B4 显式等终态**，不是"跑完 B1–B3 差不多就好了"。原因：上传接口是**立即
   303 重定向**的（不等分析），复核页/报告页被打开时任务往往**还在 Stage 1**。
   实测撞上过：报告页对非终态任务 `303 → 复核页`，`page.goto` 直接 30s 超时
   （本文件第一版就是这么红的）。等终态既消除竞态，也顺带成了一条真断言。
3. **B5 在终态之后才种夹具**。原因：Stage 3 收尾会重写 `findings`（0-44 的
   归档-重建路径），流水线还在跑时种进去的行可能被**清掉** ⇒ 点确认时按钮
   已经不存在（随机红）。等终态再种，顺序是确定的。
4. **B5 用 `onclick` 里的 fid 精确定位按钮**，不是 `button:has-text('确认')`。
   原因：findings 条数属 0-15 非确定项 ⇒ job2 完全可能有自己的 finding，
   取"第一个确认按钮"会点到**别人的** finding，然后断言我种的那条没变 ⇒ **随机红**。
5. **B5 的 finding 是夹具**：同上，拿"真实产出"当前置条件会让这条随机红。
   夹具直接种一条 `pending`，验的是"点了确认按钮之后状态真的落库"。
   B6 的内容断言只认**恒定字段**（文件名 / Job ID / GMP 抬头），**不认条数**。
6. **B6 拆成"路由契约"与"渲染内容"两条**。原因：实测 `ocr_samples` 的合成样本
   OCR 出来是**空页**（自愈重试后仍空）⇒ 终态可能是 `error`，而 `error` 在报告
   路由上是**303 跳转**（`main.py::report_page` 的设计）⇒ 拿它断言"报告有正文"
   必然随机红（实测撞上过一次）。⇒ 契约按终态分支断言（两条都是真断言），
   渲染内容改由**夹具 job**（`status=review`）承载，永远真跑。
7. **设置页必须深链 `#ocr`，且断言载体用分区级控件**。原因：设置页是**分区切换**的
   （`<section data-section="ocr" class="hidden">`），默认只显示 `llm` 区 ⇒ OCR 区控件
   **attached 但 not visible**，而 `wait_for_selector` 默认 `state="visible"` ⇒ 30s 超时
   （实测就是这么红的）。另外 `#ocr_slices` 在 `data-backend-form="mineru"` 子表单里，
   后端设成 paddle 时它**永远**不显示 ⇒ 断言载体换成始终可见的 `#ocr-backend-seg`。
8. **B5 必须同时验 SSR 与 AJAX 两条渲染路径**。原因：首屏 findings 由
   `templates/review.html` **服务端渲染**，第 2 页起才由 `static/review-findings.js`
   的 AJAX 渲染 —— 两条路径各有一个「确认」按钮。实测证据：只验 SSR 时，把
   **AJAX 那侧**的 onclick 摘成空操作，整个用例**仍然 12/12 全绿**（变异 MISSED）
   ⇒ 光验首屏等于漏掉「第 2 页起无法确认」这一整类缺陷。

### 环境变量（变异验证用）

| 变量 | 作用 |
|---|---|
| `PBC_E2E_EXE` | 换被测 exe —— 变异验证跑**产物副本**，绝不改交付物 |
| `PBC_E2E_WAIT_REVIEW_MS` | 等复核页超时；变异验证期望失败时调小，别白等 |
| `PBC_E2E_WAIT_TERMINAL_S` | 等任务终态超时（默认 900s） |

用法::

    python tests/e2e_browser.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_DEFAULT_EXE = (ROOT / "dist-electron" / "win-unpacked" / "resources"
                / "pbc-server" / "pbc-server.exe")
# 变异验证要跑**产物副本**（绝不改交付物）⇒ 允许外部指定被测 exe。
EXE = Path(os.environ.get("PBC_E2E_EXE") or _DEFAULT_EXE)
PDF1 = ROOT / "devlogs" / "ocr_samples" / "o6_small_font_low_dpi.pdf"
PDF2 = ROOT / "devlogs" / "ocr_samples" / "o1_small_box.pdf"
PORT = 58765  # 与 tests/e2e_frozen.py 的 BASE 一致
BASE = f"http://127.0.0.1:{PORT}"
# 单页 OCR+LLM 实测 130–300s，3 页留足余量。变异验证期望失败时可调小。
WAIT_REVIEW = int(os.environ.get("PBC_E2E_WAIT_REVIEW_MS") or 1_800_000)
WAIT_TERMINAL = int(os.environ.get("PBC_E2E_WAIT_TERMINAL_S") or 900)
MULTIPAGE = 3  # B1/B2 的文档页数 —— 必须 >1，否则翻页断言退化成"按钮禁用"

_results: list[tuple[str, bool, str]] = []


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ok(name: str, detail: str = "") -> None:
    _results.append((name, True, detail))
    print(f"  OK {name}{': ' + detail if detail else ''}")


def fail(name: str, detail: str) -> None:
    _results.append((name, False, detail))
    print(f"  FAIL {name}: {detail}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def wait_health(c, timeout=90) -> bool:
    for _ in range(timeout * 2):
        try:
            if c.get(f"{BASE}/health", timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _db(db_path: Path):
    return sqlite3.connect(str(db_path), timeout=15)


def _make_multipage(dst: Path, src: Path, pages: int = MULTIPAGE) -> Path:
    """把单页样本复制成 `pages` 页 —— 翻页断言需要**真的第 2 页**。

    仓库里的 `devlogs/ocr_samples/*` **全是单页**（实测 6 份均 1 页），
    直接拿它们测 `#btn-next-page` 只会走进"单页文档，按钮禁用"的跳过分支。
    运行时生成而不入库：不新增二进制资产，测试自包含。
    """
    # PyMuPDF 是 requirements.txt 里已声明的依赖；pypdf 没有 ⇒
    # 不能为了拼一份测试文件再引一个未声明的包
    # （test_declared_dependencies 会直接红）。
    import fitz

    with fitz.open(str(src)) as doc:
        if doc.page_count == 0:
            raise AssertionError(f"样本无页：{src}")
        with fitz.open() as out:
            for i in range(pages):
                src_page = i % doc.page_count
                out.insert_pdf(doc, from_page=src_page, to_page=src_page)
            out.save(str(dst))
    # fail-closed：生成物页数不对就不要往下跑（否则 B2 会静默退化成跳过）
    with fitz.open(str(dst)) as chk:
        got = chk.page_count
    if got != pages:
        raise AssertionError(f"多页样本生成失败：期望 {pages} 页，实得 {got}")
    return dst


def _seed_pending(db_path: Path, job_id: str, page: int = 1) -> int:
    con = _db(db_path)
    try:
        cur = con.execute(
            "INSERT INTO findings (job_id, page, type, severity, description, ocr_text,"
            " operator, status, source, created_at)"
            " VALUES (?, ?, 'completeness', 'warning', ?, '',"
            " '', 'pending', 'rule', datetime('now','localtime'))",
            (job_id, page, f"e2e 夹具：待确认项 p{page}"))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _seed_archived(db_path: Path, job_id: str, page: int = 6) -> int:
    """给 job 种一条**归档台账**行（B8 的「已归档」非空路径）。

    为什么必须种：归档只在 `review → retry`（`api/jobs/actions.py`）与 Stage 3
    的 LLM 层重算时发生，浏览器层两个 job 都没走过这条路 ⇒ 不种就只能验空态。
    种进去验的是"表格真的按 6 列渲染、类型/状态走了中文单一真值"。
    """
    con = _db(db_path)
    try:
        cur = con.execute(
            "INSERT INTO findings_archive (finding_id, job_id, page, type, severity,"
            " description, status, reviewer_note, archive_reason)"
            " VALUES (?, ?, ?, 'param_out_of_spec', 'critical', ?, 'confirmed', ?,"
            " 'review_retry_full_reanalysis')",
            (page * 1000, job_id, page, f"e2e 夹具：归档项 p{page}", "已与车间确认"))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _finding_status(db_path: Path, fid: int) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT status FROM findings WHERE id=?", (fid,)).fetchone()
        return row[0] if row else "(missing)"
    finally:
        con.close()


def _seed_review_job(db_path: Path, job_id: str, pdf_path: Path,
                     total_pages: int) -> None:
    """种一个 `status=review` 的夹具 job（+ 1 条 finding）—— 只为"报告能渲染"。

    为什么不用真实 job：实测 `devlogs/ocr_samples/` 的合成样本 OCR 出来是**空页**
    （自愈重试后仍空），终态可能是 `error`。而 `error` 在报告路由上是**303 跳转**
    （设计如此，见 `main.py::report_page`）⇒ 拿它断言"报告有正文"必然随机红。
    夹具让"渲染内容"这条**永远真跑**，同时真实 job 的路由契约由 httpx 单独断言。
    """
    con = _db(db_path)
    try:
        con.execute(
            "INSERT INTO jobs (id, filename, status, total_pages, pdf_path,"
            " created_at, last_activity_at)"
            " VALUES (?, ?, 'review', ?, ?, datetime('now','localtime'),"
            " datetime('now','localtime'))",
            (job_id, pdf_path.name, total_pages, str(pdf_path)))
        con.execute(
            "INSERT INTO findings (job_id, page, type, severity, description,"
            " ocr_text, operator, status, source, created_at)"
            " VALUES (?, 1, 'completeness', 'warning', '夹具：报告渲染用', '',"
            " '', 'pending', 'rule', datetime('now','localtime'))", (job_id,))
        con.commit()
    finally:
        con.close()


def _terminal_statuses() -> tuple[str, ...]:
    """从 `core/pipeline/state.py` **源码**取终态集合（单一真值，不抄一份）。

    为什么用 `ast` 读源码而不是 `import`：`core.pipeline.state` 会拉进
    `config` / `db` 的**导入期**副作用（读 config.json、按 APPDATA 建目录），
    而本脚本的进程 APPDATA 与**被测服务**的 APPDATA 是两个不同的临时目录 ——
    import 一次就会在测试进程侧多建一套现场，让"到底谁写了磁盘"变得不可判。
    """
    import ast

    src = (ROOT / "core" / "pipeline" / "state.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "TERMINAL_STATUSES" for t in node.targets):
            val = ast.literal_eval(node.value)
            if not val:
                raise AssertionError("TERMINAL_STATUSES 解析为空 —— 护栏不能空转")
            return tuple(val)
    raise AssertionError("core/pipeline/state.py 里找不到 TERMINAL_STATUSES")


def _wait_terminal(db_path: Path, job_id: str, timeout_s: int) -> tuple[str | None, str]:
    """轮询 `jobs.status` 直到终态。返回 `(终态 | None, 末次看到的状态)`。

    只读 URI 打开，不碰被测服务的写锁；DB 抖动不算失败（继续等）。
    """
    terms = _terminal_statuses()
    deadline = time.monotonic() + timeout_s
    last = "(unreadable)"
    while time.monotonic() < deadline:
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            try:
                row = con.execute("SELECT status FROM jobs WHERE id = ?",
                                  (job_id,)).fetchone()
            finally:
                con.close()
            last = row[0] if row else "(missing)"
            if last in terms:
                return last, last
        except sqlite3.Error as e:
            last = f"(db {type(e).__name__})"
        time.sleep(3)
    return None, last


def main() -> int:
    for p in (EXE, PDF1, PDF2):
        if not p.is_file():
            print(f"[FAIL] 缺少 {p}")
            return 2

    proc_mod = _load("_e2e_proc", ROOT / "tests" / "e2e_proc.py")
    wrap = _load("_e2e_wrap", ROOT / "devlogs" / "_verify" / "run_e2e_with_config_creds.py")

    appdata = Path(tempfile.mkdtemp(prefix="pbc-e2e-browser-"))
    (appdata / "PBC").mkdir(parents=True, exist_ok=True)
    db_path = appdata / "PBC" / "data.db"

    env = wrap.build_env()
    env["APPDATA"] = str(appdata)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONUNBUFFERED"] = "1"
    provider = env.get("PBC_E2E_LLM_PROVIDER", "siliconflow")
    model = env.get("PBC_E2E_LLM_MODEL", "")
    key = env.get("PBC_E2E_LLM_KEY", "")

    print(f"[browser-e2e] exe={EXE.name} appdata={appdata}")
    proc, logf = proc_mod.spawn_server(
        [str(EXE)], env=env,
        log_path=str(appdata / "PBC" / "browser-e2e-server.log"))

    rc = 1
    try:
        with httpx.Client(timeout=httpx.Timeout(30, read=180)) as c:
            if not wait_health(c):
                print("[FAIL] health 超时")
                print(proc_mod.tail_log(appdata / "PBC" / "browser-e2e-server.log", 1500))
                return 2

            payload = {"llm_provider": provider, "ocr_backend": "paddle"}
            if key:
                payload[f"{provider}_api_key"] = key
            if model:
                payload[f"{provider}_model"] = model
            for env_key, field in (("PBC_E2E_PADDLE_TOKEN", "paddle_ocr_token"),
                                   ("PBC_E2E_MINERU_TOKEN", "mineru_token")):
                if env.get(env_key):
                    payload[field] = env[env_key]
            r = c.post(f"{BASE}/api/settings", json=payload, timeout=60)
            assert r.status_code == 200, f"settings {r.status_code}"
            print("[browser-e2e] settings -> 200")

            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                # 两条独立信号，都取**结构化真值**，不解析 console 文案：
                #  · pageerror  = 未捕获异常（"前端接线断了"的信号）
                #  · response   = HTTP 状态（4xx/5xx 才是硬事实；console 里那条
                #                 "Failed to load resource" 不带 URL，靠它判不了）
                page_errors: list[str] = []
                http_errors: list[str] = []
                page.on("pageerror", lambda e: page_errors.append(str(e)))
                page.on("response",
                        lambda r: http_errors.append(f"{r.status} {r.url}")
                        if r.status >= 400 else None)

                # ---------- B1: 上传第一份（3 页）→ 复核页 ----------
                section(f"B1 上传第一份（{MULTIPAGE} 页）")
                big = _make_multipage(appdata / "multi.pdf", PDF1)
                page.goto(f"{BASE}/", wait_until="domcontentloaded")
                page.wait_for_selector("#file-input", state="attached", timeout=30_000)
                page.set_input_files("#file-input", str(big))
                try:
                    page.wait_for_url(re.compile(r"/jobs/[^/]+/review"),
                                      timeout=WAIT_REVIEW)
                    job1 = re.search(r"/jobs/([^/]+)/review", page.url).group(1)
                except Exception as e:
                    fail("B1_upload_redirects_to_review", f"{type(e).__name__}: {e}")
                    print(proc_mod.tail_log(appdata / "PBC" / "browser-e2e-server.log", 1500))
                    raise
                # 光"跳到复核页"还不够 —— 得确认服务端认到的页数真是 3，
                # 否则下面的翻页断言又会退化成"单页跳过"。
                page.wait_for_selector("#page-total", timeout=60_000)
                total = page.inner_text("#page-total").strip()
                if total == str(MULTIPAGE):
                    ok("B1_upload_redirects_to_review",
                       f"job={job1[:8]} 页数={total}")
                else:
                    fail("B1_upload_redirects_to_review",
                         f"复核页 #page-total={total!r}，期望 {MULTIPAGE}")

                # ---------- B2: 翻页 / 缩放（真 3 页） ----------
                section("B2 复核页翻页 / 缩放")
                pn0 = page.inner_text("#page-num").strip()
                nxt = page.query_selector("#btn-next-page")
                if nxt is None:
                    fail("B2_page_nav", "#btn-next-page 不存在")
                elif not nxt.is_enabled():
                    fail("B2_page_nav",
                         f"{MULTIPAGE} 页文档的下一页按钮却是 disabled（当前页 {pn0}）")
                else:
                    nxt.click()
                    page.wait_for_timeout(1500)
                    pn1 = page.inner_text("#page-num").strip()
                    if pn1 != pn0:
                        ok("B2_page_nav", f"{pn0} -> {pn1}（共 {total} 页）")
                    else:
                        fail("B2_page_nav", f"点下一页后页码未变（仍 {pn0}）")

                z0 = page.inner_text("#pdf-zoom-label").strip()
                page.click("#btn-zoom-in")
                page.wait_for_timeout(600)
                z1 = page.inner_text("#pdf-zoom-label").strip()
                page.click("#btn-zoom-reset")
                page.wait_for_timeout(600)
                z2 = page.inner_text("#pdf-zoom-label").strip()
                if z1 != z0 and z2 == z0:
                    ok("B2_zoom", f"{z0} -> {z1} -> {z2}")
                else:
                    fail("B2_zoom", f"缩放未按预期变化：{z0} / {z1} / {z2}")

                # ---------- B3: 回首页 → 再上传第二份（审计 Q1） ----------
                section("B3 回首页再上传（Q1）")
                page.get_by_role("link", name="首页").first.click()
                page.wait_for_url(re.compile(r"^" + re.escape(BASE) + r"/$"), timeout=30_000)
                page.wait_for_selector("#upload-area", state="visible", timeout=30_000)
                ok("B3_back_to_home", "URL=/ 且 #upload-area 可见")

                page.wait_for_selector("#file-input", state="attached", timeout=30_000)
                page.set_input_files("#file-input", str(PDF2))
                page.wait_for_url(re.compile(r"/jobs/[^/]+/review"), timeout=WAIT_REVIEW)
                job2 = re.search(r"/jobs/([^/]+)/review", page.url).group(1)
                if job2 != job1:
                    ok("B3_second_upload_works", f"job={job2[:8]}（与第一份不同）")
                else:
                    fail("B3_second_upload_works", f"第二份上传拿到同一个 job：{job2}")

                # ---------- B4: 等终态（报告页 / 种夹具的前提） ----------
                # 上传接口是**立即 303** 的 ⇒ 此刻任务多半还在 Stage 1。不等终态就
                # 开报告页，会撞上「非终态 303 → 复核页」并让 goto 超时（实测 30s 红）。
                section("B4 等两个任务终态")
                st, last = _wait_terminal(db_path, job2, WAIT_TERMINAL)
                if st is None:
                    fail("B4_job_reaches_terminal",
                         f"{job2[:8]} 在 {WAIT_TERMINAL}s 内未到终态（末次 {last}）")
                else:
                    ok("B4_job_reaches_terminal", f"status={st}")

                # 第一份（3 页）也必须终态：否则 Stage 3 收尾会在 B5 种完夹具之后
                # 重写 findings（0-44 的归档-重建路径）⇒ 点确认时按钮已不存在。
                st1, last1 = _wait_terminal(db_path, job1, WAIT_TERMINAL)
                if st1 is None:
                    fail("B4_first_job_reaches_terminal",
                         f"{job1[:8]} 在 {WAIT_TERMINAL}s 内未到终态（末次 {last1}）")
                else:
                    ok("B4_first_job_reaches_terminal", f"status={st1}")

                # ---------- B5: 确认 finding —— 两条渲染路径 ----------
                # ⚠️ 首屏 findings 是**服务端渲染**（`templates/review.html`），
                # 第 2 页起才由 `static/review-findings.js` 的 AJAX 渲染。
                # 两条路径各有一个「确认」按钮 —— 只验一条会漏掉另一条（实测：
                # 只验 SSR 时，把 AJAX 那侧的 onclick 摘掉，用例**照样全绿**）。
                section("B5 确认 finding（SSR / AJAX 两条路径）")
                fid_ssr = _seed_pending(db_path, job1, page=1)
                fid_ajax = _seed_pending(db_path, job1, page=2)
                page.goto(f"{BASE}/jobs/{job1}/review", wait_until="domcontentloaded")
                page.wait_for_selector("#findings-list", timeout=60_000)

                def _confirm(fid: int, label: str, kind: str) -> None:
                    """按 onclick 里的 fid 精确定位 —— 不能用 button:has-text('确认')，
                    那会点到"列表里第一个"，而真实产出可能已有 0-15 条 finding。"""
                    sel = f"#findings-list button[onclick*=\", {fid}, 'confirmed'\"]"
                    btn = page.query_selector(sel)
                    if btn is None:
                        page.wait_for_timeout(3000)
                        btn = page.query_selector(sel)
                    if btn is None:
                        fail(label, f"找不到夹具 id={fid} 的「确认」按钮（{kind} 渲染）")
                        return
                    btn.click()
                    page.wait_for_timeout(2500)
                    got = _finding_status(db_path, fid)
                    if got == "confirmed":
                        ok(label, f"id={fid} status=confirmed（{kind} 渲染）")
                    else:
                        fail(label, f"点确认后 status={got}（{kind} 渲染）")

                _confirm(fid_ssr, "B5_confirm_ssr", "SSR")

                # 翻到第 2 页 —— 由 loadPageData() 拉 `/findings?page=2` 后 AJAX 渲染
                nxt = page.query_selector("#btn-next-page")
                if nxt is None or not nxt.is_enabled():
                    fail("B5_confirm_ajax", "第 2 页不可达（下一页按钮缺失/禁用）")
                else:
                    nxt.click()
                    sel_ajax = (f"#findings-list button"
                                f"[onclick*=\", {fid_ajax}, 'confirmed'\"]")
                    # ⚠️ 必须把"等不到"收敛成**断言失败**，不能让异常打断整轮 ——
                    # 实测：变异把 AJAX 侧的 onclick 摘掉时，这里抛 TimeoutError，
                    # 后面的 B6/B7 全被跳过，判据变成 NOT_REACHED（看着像"没抓住"）。
                    try:
                        page.wait_for_selector(sel_ajax, timeout=30_000)
                    except Exception as e:
                        fail("B5_confirm_ajax",
                             f"第 2 页渲染后找不到夹具 id={fid_ajax} 的「确认」按钮"
                             f"（{type(e).__name__}）")
                    else:
                        _confirm(fid_ajax, "B5_confirm_ajax", "AJAX")

                # ---------- B6: 报告页 + 设置页保存 ----------
                section("B6 报告页 / 设置页")
                # (a) 真实 job 的**路由契约**：按终态分支，两条都是真断言。
                # 同时这条 httpx 直连是本用例的**诊断探针** —— 若下面的浏览器
                # goto 超时，用它区分"服务端卡住"还是"浏览器侧问题"。
                probe_ms = -1.0
                probe_code = "n/a"
                if st is None:
                    fail("B6_report_contract", "终态未知（B4 未到终态）⇒ 无法判定契约")
                else:
                    t0 = time.monotonic()
                    probe = c.get(f"{BASE}/jobs/{job2}/report",
                                  follow_redirects=False, timeout=120)
                    probe_ms = (time.monotonic() - t0) * 1000
                    probe_code = probe.status_code
                    if st in ("review", "partial_review"):
                        if probe.status_code == 200:
                            ok("B6_report_contract", f"{st} ⇒ 200（{probe_ms:.0f}ms）")
                        else:
                            fail("B6_report_contract",
                                 f"{st} ⇒ {probe.status_code}，期望 200")
                    else:
                        loc = probe.headers.get("location", "")
                        if probe.status_code == 303 and loc.endswith(
                                f"/jobs/{job2}/review"):
                            ok("B6_report_contract",
                               f"{st} ⇒ 303 → 复核页（{probe_ms:.0f}ms）")
                        else:
                            fail("B6_report_contract",
                                 f"{st} ⇒ {probe.status_code} loc={loc!r}，"
                                 f"期望 303 → /jobs/{job2}/review")

                # (b) 渲染内容 —— 夹具 job 保证这条**永远真跑**（不依赖上游成败）。
                fix_id = "e2e0000rpt1"
                _seed_review_job(db_path, fix_id, big, MULTIPAGE)
                try:
                    resp = page.goto(f"{BASE}/jobs/{fix_id}/report",
                                     wait_until="domcontentloaded", timeout=60_000)
                    body = page.inner_text("#report-body")
                except Exception as e:
                    resp, body = None, ""
                    fail("B6_report_renders",
                         f"{type(e).__name__}: {e}；同时 httpx 直连真实 job = "
                         f"{probe_code}（{probe_ms:.0f}ms）⇒ 可据此区分服务端/浏览器侧")
                else:
                    missing = [k for k in ("GMP", big.name, fix_id) if k not in body]
                    if resp and resp.status == 200 and not missing:
                        ok("B6_report_renders",
                           f"HTTP {resp.status}, {len(body)} 字符，含文件名/JobID")
                    else:
                        fail("B6_report_renders",
                             f"status={resp.status if resp else '?'} 缺 {missing} "
                             f"body[:120]={body[:120]!r}")

                # 设置页是**分区切换**的（`<section data-section="ocr" class="hidden">`），
                # 默认只显示 llm 区 ⇒ 必须深链到 #ocr，否则 OCR 区的控件 attached
                # 但 not visible，`wait_for_selector` 会按默认 state=visible 超时。
                # 深链本身也是产品能力（可收藏/可后退），顺带一并验了。
                page.goto(f"{BASE}/settings#ocr", wait_until="domcontentloaded")
                page.wait_for_selector("#ocr-backend-seg button.active",
                                      state="visible", timeout=30_000)
                # ⚠️ 不拿 `#ocr_slices` 做断言载体：它在 `data-backend-form="mineru"`
                # 子表单里，而本测试把后端设成 paddle ⇒ 那个表单是 hidden 的。
                # 用**分区级**的分段控件（`#ocr-backend-seg`，始终可见）才稳。
                before = c.get(f"{BASE}/api/settings").json()["ocr"]["backend"]
                want = "mineru" if before != "mineru" else "paddle"
                page.click(f'#ocr-backend-seg button[data-value="{want}"]')
                page.wait_for_timeout(300)
                save = page.query_selector("button:has-text('保存全部设置')")
                if save is None:
                    fail("B6_settings_save", "找不到「保存全部设置」按钮")
                else:
                    save.click()
                    page.wait_for_timeout(2500)
                    after = c.get(f"{BASE}/api/settings").json()["ocr"]["backend"]
                    if after == want:
                        ok("B6_settings_save",
                           f"ocr.backend {before} -> {after}（GET 读得回）")
                    else:
                        fail("B6_settings_save",
                             f"点了保存但 ocr.backend 仍是 {after}（期望 {want}）")

                # ---------- B8: 审计面板（C9 / C10） ----------
                # 三个端点里两个（/audit、/llm_audit）自 Phase 7 就存在，但引入面板前
                # `grep -rn audit static/ templates/` 是 **0 命中** —— 后端活着、界面全黑。
                # 这里验的是"**用户能看见**"，所以必须真点按钮、真读表格：
                # 断言"端点返回 200"只能证明后端没坏，证明不了接线。
                section("B8 审计面板（变更历史 / 已归档）")
                page.goto(f"{BASE}/jobs/{job1}/review", wait_until="domcontentloaded")
                try:
                    page.wait_for_selector("#audit-btn", state="visible", timeout=30_000)
                except Exception as e:
                    fail("B8_audit_opens", f"找不到「审计记录」按钮（{type(e).__name__}）")
                else:
                    page.click("#audit-btn")
                    try:
                        page.wait_for_selector("#audit-panel", state="visible",
                                              timeout=15_000)
                    except Exception as e:
                        fail("B8_audit_opens", f"面板未打开（{type(e).__name__}）")
                    else:
                        ok("B8_audit_opens", "「审计记录」按钮打开面板")
                        # 变更历史：这个 job 跑过流水线 ⇒ audit_log 必有行
                        # （pipeline_start 是上传后第一条），空表就是接线断了。
                        try:
                            page.wait_for_selector("#audit-body table tbody tr",
                                                   timeout=20_000)
                            rows = page.query_selector_all("#audit-body table tbody tr")
                            heads = page.query_selector_all("#audit-body table thead th")
                            if len(heads) == 3:
                                ok("B8_audit_rows", f"{len(rows)} 行 / {len(heads)} 列")
                            else:
                                fail("B8_audit_rows",
                                     f"变更历史应为 3 列（时间/动作/详情），实得 {len(heads)}")
                        except Exception as e:
                            fail("B8_audit_rows", f"变更历史没有数据（{type(e).__name__}）")

                        # 「已归档」：本任务**没有**走过 review→retry ⇒ 必须显示空态文案。
                        # 空态本身是判据：静默空白会被读成"没有变更"（错误的合规结论）。
                        page.click('#audit-tabs button[data-tab="archived"]')
                        try:
                            page.wait_for_selector("#audit-body p", timeout=15_000)
                            page.wait_for_timeout(800)
                            txt = page.inner_text("#audit-body")
                        except Exception as e:
                            fail("B8_archived_empty_state", f"读取失败（{type(e).__name__}）")
                        else:
                            if "没有归档记录" in txt:
                                ok("B8_archived_empty_state", "空态文案可见")
                            else:
                                fail("B8_archived_empty_state",
                                     f"没有空态文案，面板内容={txt[:120]!r}")

                        page.click("#audit-close")
                        page.wait_for_timeout(400)
                        if page.query_selector("#audit-overlay") is None:
                            ok("B8_audit_closes", "关闭按钮移除面板")
                        else:
                            fail("B8_audit_closes", "点关闭后面板仍在")

                        # 非空路径：job2 种一条归档行 ⇒ 表格必须真的按 6 列渲染，
                        # 且类型/状态走中文单一真值（不是把 code 原样印出来）。
                        _seed_archived(db_path, job2)
                        page.goto(f"{BASE}/jobs/{job2}/review",
                                  wait_until="domcontentloaded")
                        page.click("#audit-btn")
                        page.click('#audit-tabs button[data-tab="archived"]')
                        try:
                            page.wait_for_selector("#audit-body table tbody tr",
                                                   timeout=20_000)
                            heads = page.query_selector_all("#audit-body table thead th")
                            body_txt = page.inner_text("#audit-body")
                        except Exception as e:
                            fail("B8_archived_rows", f"归档表格没渲染（{type(e).__name__}）")
                        else:
                            if len(heads) != 6:
                                fail("B8_archived_rows",
                                     f"归档表应为 6 列，实得 {len(heads)}")
                            elif "参数超标" not in body_txt:
                                fail("B8_archived_rows",
                                     f"类型没走中文单一真值：{body_txt[:120]!r}")
                            else:
                                ok("B8_archived_rows", "6 列且类型已中文化")

                # ---------- 收尾：JS / HTTP 健康（两条独立信号） ----------
                section("JS / HTTP 健康")
                if page_errors:
                    fail("no_page_errors",
                         f"{len(page_errors)} 条：{[e[:120] for e in page_errors[:3]]}")
                else:
                    ok("no_page_errors", "无未捕获异常")

                # 允许的唯一 4xx：`/api/jobs/{id}/pages/N` 的 404 —— 该页**尚无分析
                # 产物**时接口按设计返回 404（`api/review.py`），UI 已按空态处理。
                # 本用例在 B2 主动翻到尚未分析的页 ⇒ 必然产生这一条。
                # 其余一切 4xx/5xx（静态资源 404、接口 500…）**一律红**。
                real = [e for e in http_errors
                        if not (e.startswith("404 ") and "/pages/" in e)]
                if real:
                    fail("no_http_errors", f"{len(real)} 条：{real[:3]}")
                else:
                    allowed = len(http_errors) - len(real)
                    ok("no_http_errors",
                       f"无意外 4xx/5xx（按设计放行 /pages/N 404 × {allowed}）")

                browser.close()

        rc = 0 if all(okf for _, okf, _ in _results) else 1
    except AssertionError as e:
        print(f"[FAIL] {e}")
        print(proc_mod.tail_log(appdata / "PBC" / "browser-e2e-server.log", 2000))
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        print(proc_mod.tail_log(appdata / "PBC" / "browser-e2e-server.log", 2000))
    finally:
        proc_mod.stop_server(proc, logf)

    print("\n" + "=" * 60)
    passed = sum(1 for _, o, _ in _results if o)
    print(f"browser e2e: {passed}/{len(_results)} OK")
    for name, o, detail in _results:
        if not o:
            print(f"  FAIL {name}: {detail}")
    if rc == 0:
        shutil.rmtree(appdata, ignore_errors=True)
    else:
        print(f"[keep] 现场保留：{appdata}")
    print(f"BROWSER_E2E_RC={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
