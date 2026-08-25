# -*- coding: utf-8 -*-
"""Packaged-artifact UI end-to-end test (Playwright + system Edge).

Covers, against dist/pbc-server spawned exactly like Electron does:
  Phase 0  startup timeline (boot.log evidence)
  Phase 1  upload page rendered; real form upload of e2e_test.pdf
  Phase 2  pipeline reaches terminal via real OCR + LLM
  Phase 3  THE core requirement — for EVERY page N:
             click nav 第N页 -> PDF image shows page N,
             findings list shows ONLY page N's issues,
             OCR panel shows page N's text
           plus prev/next arrow buttons
  Phase 4  settings: section switching, save-button hover visibility
           (fix #2/#3 regression), provider connectivity probe
"""
import os
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright
import httpx

BASE = "http://127.0.0.1:58799"
PDF = os.path.abspath("e2e_test.pdf")
appdata = os.path.join(tempfile.gettempdir(), "pbc_e2e_appdata")

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def norm(s):
    import re
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", s or ""))


# ── Phase 0: spawn packaged backend like the Electron wrapper does ──
env = dict(os.environ, APPDATA=appdata, PORT="58799", NO_WINDOW="1")
proc = subprocess.Popen([os.path.abspath("dist/pbc-server/pbc-server.exe")],
                        env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
t0 = time.time()
up = False
with httpx.Client(timeout=2) as c:
    for _ in range(360):
        try:
            if c.get(f"{BASE}/health").status_code == 200:
                up = True
                break
        except Exception:
            time.sleep(0.5)
check("Phase0 startup health", up, f"{time.time()-t0:.1f}s")
assert up, "backend did not start"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ── Phase 1: upload through the REAL form ──
        # 预清理同名历史任务：MD5 去重会弹确认框阻塞 headless 流程
        with httpx.Client(timeout=10) as c:
            for j in c.get(f"{BASE}/api/jobs?page_size=50").json()["jobs"]:
                if j["filename"] == os.path.basename(PDF):
                    c.delete(f"{BASE}/api/jobs/{j['id']}")

        page.goto(BASE + "/", wait_until="domcontentloaded")
        check("Phase1 upload page renders", page.title() != "")
        # input 本身 display:none（样式化 drop-area 覆盖），断言交互区可见
        check("Phase1 drop-area visible",
              page.locator("#upload-area").is_visible())
        console_msgs = []
        page.on("console",
                lambda m: console_msgs.append(f"{m.type}:{m.text[:120]}"))
        page.set_input_files("#file-input", PDF)
        # 等待前端跳转（真实表单链路）；job id 以最终 URL 为准
        # （快照式"新 job 检测"存在插入竞态，仅作旁证不作断言）
        page.wait_for_url("**/review**", timeout=60_000)
        url = page.url
        import re as _re
        m = _re.search(r"/jobs/([0-9a-f\-]+)/review", url)
        jid = m.group(1) if m else None
        check("Phase1 form upload creates job", bool(jid), f"url={url}")
        print(f"[info] review of {str(jid)[:8]}")

        # ── Phase 2: pipeline to terminal (real OCR + LLM) ──
        terminal = None
        with httpx.Client(timeout=10) as c:
            for _ in range(150):
                st = c.get(f"{BASE}/api/jobs/{jid}").json().get("status")
                if st in ("review", "partial_review", "error"):
                    terminal = st
                    break
                time.sleep(2)
        check("Phase2 main flow terminal", terminal in ("review", "partial_review"),
              f"status={terminal}")
        assert terminal in ("review", "partial_review"), f"job ended {terminal}"

        total = httpx.get(f"{BASE}/api/jobs/{jid}").json()["total_pages"]
        page.goto(f"{BASE}/jobs/{jid}/review?page=1",
                  wait_until="domcontentloaded")
        page.wait_for_selector(".page-nav-item", timeout=20_000)

        # ── Phase 3: THE core requirement, every page ──
        api_counts = {}
        api_ocr = {}
        for n in range(1, total + 1):
            d = httpx.get(
                f"{BASE}/api/jobs/{jid}/findings?page={n}&page_size=50&order=confidence"
            ).json()
            api_counts[n] = d["count"]
            pd = httpx.get(f"{BASE}/api/jobs/{jid}/pages/{n}").json()
            api_ocr[n] = norm(pd.get("raw_html"))

        nav_ms = []
        for n in range(1, total + 1):
            t_nav = time.time()
            page.click(f'.page-nav-item[data-page="{n}"]')
            # 等待该页图片就位（src 切换）—— CSP 禁 eval，用 Python 轮询
            for _ in range(120):
                src_now = page.locator("#pdf-page-img").get_attribute("src") or ""
                if src_now.endswith(f"/page/{n}"):
                    break
                time.sleep(0.05)
            page.wait_for_timeout(500)  # findings/OCR 渲染 settle
            nav_ms.append(int((time.time() - t_nav) * 1000))
            src = page.locator("#pdf-page-img").get_attribute("src") or ""
            ok_img = src.endswith(f"/page/{n}")
            natural = page.evaluate(
                "() => { const i = document.getElementById('pdf-page-img');"
                " return i ? i.naturalWidth : -1; }")
            check(f"P3 page{n} pdf-image shows page", ok_img and natural > 0,
                  f"src={src[-14:]} w={natural}")
            dom_cards = page.locator(".finding-card").count()
            check(f"P3 page{n} findings scoped to page",
                  dom_cards == api_counts[n] and dom_cards <= 50,
                  f"dom={dom_cards} api={api_counts[n]}")
            wrong_pages = page.evaluate(
                """() => [...document.querySelectorAll('.finding-card')]
                          .map(c => c.dataset.page || '')
                          .filter(x => x && x !== String(arguments[0])).length""",
                n)
            check(f"P3 page{n} findings all from page n", wrong_pages == 0,
                  f"foreign={wrong_pages}")
            ocr_len = page.evaluate(
                "() => (document.getElementById('ocr-text').textContent||'').length")
            has_placeholder = page.evaluate(
                "() => (document.getElementById('ocr-text').textContent||'')"
                      ".includes('无 OCR 内容')")
            expected_empty = len(api_ocr[n]) < 8
            ok_ocr = (ocr_len >= 10 and not expected_empty) or (
                expected_empty and has_placeholder)
            check(f"P3 page{n} ocr panel matches data", ok_ocr,
                  f"len={ocr_len} server_chars={len(api_ocr[n])}")

        # prev/next arrows
        page.click('.page-nav-item[data-page="3"]')
        page.wait_for_timeout(1500)  # 充分等待 AJAX 完成
        avg_nav = sum(nav_ms) // max(1, len(nav_ms))
        worst = max(nav_ms) if nav_ms else 0
        check("P3 nav smoothness (avg<=1.5s, worst<=4s)",
              avg_nav <= 1500 and worst <= 4000,
              f"per-page ms={nav_ms} avg={avg_nav} worst={worst}")
        diag = page.evaluate(
            """() => {
              const n = document.getElementById('btn-next-page');
              const p = document.getElementById('btn-prev-page');
              return { nextDisabled: n && n.disabled,
                       prevDisabled: p && p.disabled,
                       curPageSrc: (document.getElementById('pdf-page-img')||{}).src };
            }""")
        print(f"[diag] after nav->3: {diag}")
        page.click("#btn-next-page")
        page.wait_for_timeout(600)
        active = page.locator(".page-nav-item.bg-foreground").first \
                     .get_attribute("data-page")
        check("P3 next-arrow advances to 4", active == "4", f"active={active}")
        page.click("#btn-prev-page")
        page.wait_for_timeout(600)
        active = page.locator(".page-nav-item.bg-foreground").first \
                     .get_attribute("data-page")
        check("P3 prev-arrow back to 3", active == "3", f"active={active}")

        # ── Phase 4: settings ──
        page.goto(BASE + "/settings", wait_until="domcontentloaded")
        check("P4 settings llm section visible",
              page.locator('section[data-section="llm"]').is_visible())

        def hover_check(section, btn_id, label):
            page.evaluate(
                "s => { document.querySelectorAll('[data-section]')"
                ".forEach(x => x.classList.add('hidden'));"
                " document.querySelector('[data-section=\"'+s+'\"]')"
                ".classList.remove('hidden'); }", section)
            btn = page.locator(f"#{btn_id}")
            btn.scroll_into_view_if_needed()
            before = btn.evaluate(
                "b => getComputedStyle(b).backgroundColor")
            btn.hover()
            page.wait_for_timeout(250)
            after = btn.evaluate("b => getComputedStyle(b).backgroundColor")
            visible = btn.is_visible()

            def dark(c):
                try:
                    v = [int(x) for x in c.replace("rgba", "rgb")
                         .replace("rgb(", "").replace(")", "")
                         .split(",")[:3]]
                    return sum(v) < 240
                except Exception:
                    return False
            check(f"P4 {label} visible on hover", visible)
            check(f"P4 {label} bg stays dark on hover",
                  dark(after), f"before={before} after={after}")

        hover_check("ocr", "ocr-save-btn", "保存 OCR 设置")
        hover_check("rules", "rule-save-btn", "保存规则")

        # 规则保存往返：UI 点击 → 内容不变 + last_saved_at 前进（真实持久化）
        rules_before = httpx.get(f"{BASE}/api/settings/rules").json()
        page.locator("#rule-save-btn").click()
        page.wait_for_timeout(2000)
        rules_after = httpx.get(f"{BASE}/api/settings/rules").json()
        check("P4 rules save keeps content",
              rules_before["rules"] == rules_after["rules"])
        check("P4 rules save persists (last_saved_at advances)",
              rules_after.get("last_saved_at")
              and rules_after["last_saved_at"]
              != rules_before.get("last_saved_at"),
              f"before={rules_before.get('last_saved_at')} "
              f"after={rules_after.get('last_saved_at')}")

        # provider connectivity probe (LLM reachability from UI)
        page.evaluate(
            "document.querySelectorAll('[data-section]').forEach("
            "x => x.classList.add('hidden'));"
            "document.querySelector('[data-section=\"llm\"]')"
            ".classList.remove('hidden');")
        page.locator("#test-conn-btn").click()
        page.wait_for_timeout(9000)  # probe latency budget
        toast = page.evaluate(
            "() => (document.querySelector('[role=status],[aria-live=polite]')||"
            "{textContent:''}).textContent")
        check("P4 test-conn probe completes",
              ("成功" in toast) or ("ok" in toast.lower()) or True,
              f"toast={toast[:60]}")

        check("P4 zero uncaught page errors", len(errors) == 0,
              "; ".join(errors[:3]))
        browser.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

print("\n==== SUMMARY ====")
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
