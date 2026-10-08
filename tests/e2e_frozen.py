"""Frozen build e2e smoke tests."""
import subprocess, time, requests, sys, os, json, signal
import urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.e2e_coverage import (  # noqa: E402
    Coverage, ENTRY_LLM_CONFIG, ENTRY_OCR_CONFIG,
    STATUS_COVERED, STATUS_FAILED, STATUS_SKIPPED,
    classify_pipeline, exit_code, required_gaps,
)
from tests.e2e_proc import (  # noqa: E402
    EXE_ENV, LLM_KEY_ENV, classify_llm_probe, llm_failure_attribution, llm_key,
    llm_key_env_display, llm_model, llm_provider, ocr_ready_from_settings,
    resolve_exe, spawn_server, stop_server,
)
from tests.e2e_status_js import (  # noqa: E402
    dot_semantics_problems, status_dot_classes,
)

# 被测产物：默认 PyInstaller 的直接产物；用 PBC_E2E_EXE 指向 Electron 打包后
# **内嵌**的那份副本（dist-electron*/win-unpacked/resources/pbc-server），
# 那才是用户双击 BatchSentry.exe 时实际运行的东西。
EXE = resolve_exe()
BASE = "http://127.0.0.1:58765"
APPDATA = os.path.join(os.environ["TEMP"], "pbc-e2e-frozen")
RESULTS = []

# 覆盖清单（B9-7）：把"真的验过"与"因环境跳过"分开记账并落盘。
# 为什么必须落盘：本冒烟的多数断言与外部凭据无关，缺凭据时照样全绿；
# 若"最贵的 LLM 链路其实没跑"不落成可机读的事实，一次关键路径从未运行的
# 发版冒烟与一次真正跑通的冒烟在报告上**完全一样**。
COV = Coverage()

def ok(name, detail=""):
    RESULTS.append(("PASS", name, detail))
    print(f"  PASS  {name} {detail}")

def fail(name, detail=""):
    RESULTS.append(("FAIL", name, detail))
    print(f"  FAIL  {name} {detail}")

def section(title):
    print(f"\n=== {title} ===")

def probe_llm_credential(base_url, key, model="", timeout=15):
    """对"应用自己上报的 base_url + 我们交给它的凭据"做**两段式**正向对照。

    用途只有一个：把"流水线以 error 收场"的**归因**说清楚，而不是一律贴
    "真实缺陷"的标签（Round 54 实测：一份已失效的 key 会让报告写成"真实缺陷"，
    把排查方向引到代码上）。

    🔴 **为什么必须两段（2026-09-30 实测的第二次误归因）**：
    只用**免费**端点 `GET /models` 时，它不消耗额度 ⇒ 只能回答"这把 key 是不是
    真的"，**回答不了**"这个账户还付得起钱吗"。于是上游 402 欠费时 `/models`
    照旧 200 ⇒ 判 `ok` ⇒ 报告写"凭据有效，故此处失败是**产品缺陷**"，而同一份
    报告的另一行写着 `402 ... balance is insufficient` —— 自相矛盾，且把排查
    引向代码。故第二段改用**计费**端点（`POST /chat/completions`，`max_tokens=1`）
    发一次最小请求，用**应用自己上报的模型**：只有它能把 401 与 402 分开。

    返回 `(verdict, detail)`；verdict ∈ {invalid, billing, ok, unknown}，判定规则
    **只此一处**（`tests/e2e_proc.classify_llm_probe` 的纯函数行为表）：
      * `invalid` —— 确凿的 401/403：**上游拒绝该凭据** ⇒ 环境问题。
      * `billing` —— 402：**凭据有效、账户欠费** ⇒ 环境问题（**非**产品缺陷）。
      * `ok`      —— 凭据**与额度**均可用。⚠️ 这**只是必要条件**，**不足以**
                     断定产品缺陷：探针发的是**极小请求**，上游网关对**长请求**
                     仍可能超时返回 5xx（2026-10-08 实测 ALB 返回
                     `<title>504 Gateway Time-out</title>` 的 HTML 页）。
                     文案与判据的**唯一实现**在
                     `tests/e2e_proc.llm_failure_attribution`。
      * `unknown` —— 判不了（网络/异常/其它状态码/未上报模型）。**fail-closed**：
                     调用方按真实缺陷处理，绝不因"探测不可用"而放行。

    ⚠️ 不得改成"只要 error_message 里出现 401 就降级"：那是按**文案**判定，
    文案属展示层，改文案会静默改变控制流（本项目已因同类做法踩过坑）。
    """
    if not base_url:
        return VERDICT_UNKNOWN, "应用未上报 base_url"
    root = base_url.rstrip("/")

    # ── 第一段：免费端点。只回答"key 是不是真的"（余额为 0 时它照样 200）。
    free_url = root + "/models"
    try:
        req = urllib.request.Request(
            free_url, headers={"Authorization": "Bearer " + (key or "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            free_status = r.status
    except urllib.error.HTTPError as e:
        free_status = e.code
    except Exception as e:  # noqa: BLE001 — 判不了 ≠ 通过
        return VERDICT_UNKNOWN, f"{type(e).__name__}: {str(e)[:80]}"

    # ── 第二段：仅在 key 被上游认下、且知道模型时，发一次**计费**最小请求。
    #     没有它就无法把 401 与 402 分开 —— 这正是本函数存在两段的原因。
    metered_status = None
    metered_note = ""
    if free_status != 200:
        metered_note = "免费端点未通过，无需计费探测"
    elif not model:
        metered_note = "应用未上报模型，无法计费探测"
    else:
        metered_url = root + "/chat/completions"
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,          # 最小化计费；只为一个状态码
            }).encode("utf-8")
            req = urllib.request.Request(
                metered_url, data=payload, method="POST",
                headers={"Authorization": "Bearer " + (key or ""),
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                metered_status = r.status
        except urllib.error.HTTPError as e:
            metered_status = e.code
        except Exception as e:  # noqa: BLE001 — 判不了 ≠ 通过
            metered_note = f"计费探测异常 {type(e).__name__}"

    verdict = classify_llm_probe(free_status, metered_status)
    detail = (f"免费端点 HTTP {free_status} + 计费端点 "
              f"HTTP {metered_status if metered_status is not None else '未取得'}"
              f"{('（' + metered_note + '）') if metered_note else ''}")
    return verdict, detail

# --- Start server ---
# 关键：stdout/stderr 落日志文件（不得用未排空的 PIPE —— 服务端日志写满
# 管道缓冲后子进程阻塞在 write，事件循环停摆，后续请求全超时）。
print(f"Starting frozen pbc-server.exe ...\n  target = {EXE}")
os.makedirs(os.path.join(APPDATA, "PBC"), exist_ok=True)
env = os.environ.copy()
env["APPDATA"] = APPDATA
proc, _logf = spawn_server(
    [EXE], env=env,
    log_path=os.path.join(APPDATA, "PBC", "frozen-e2e-server.log"),
)
time.sleep(8)

try:
    # 1. Health check
    section("Health")
    try:
        r = requests.get(f"{BASE}/health", timeout=5)
        data = r.json()
        assert r.status_code == 200, f"status={r.status_code}"
        assert data["status"] == "ok", f"status={data['status']}"
        # 版本必须与唯一真值 main.APP_VERSION 一致（禁止硬编码 —— 硬编码会在
        # 升版本时静默失配，把"包内版本已更新"的验证变成假通过）。
        # 冻结包从 exe 内取名，故用源码侧真值对齐（同一次发布流水线）。
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from main import APP_VERSION as _EXPECT
        assert data["version"] == _EXPECT, (
            f"version={data['version']} 期望 {_EXPECT}"
        )
        ok("health", f"v{data['version']}")
    except Exception as e:
        fail("health", str(e))

    # 1.5 看门狗自述 + 阈值不变式（**在产物上**复核，不硬编码任何常量）
    #
    # 为什么必须在这里做：看门狗阈值是"停滞后杀任务"的唯一依据，而它一旦
    # 低于它所覆盖的上游封顶，就会**抢在上游超时之前**误报（把"上游还在
    # 正常等待"判成卡死）。源码侧有单测，但产物里跑的是**冻结的那份代码** ——
    # 只有让产物自己报出判定口径，才能证明"要发出去的这个包"里不变式成立。
    # 自述字段：stall_limits_s / ocr_upstream_cap_s / cpu_task_cap_s /
    # rotation_silence_bound_s（#120 引入的旋转补救静默上界）。
    section("Watchdog self-report (threshold invariant)")
    try:
        r = requests.get(f"{BASE}/api/health/watchdog", timeout=5)
        assert r.status_code == 200, f"status={r.status_code}"
        wd = r.json()
        limits = wd["stall_limits_s"]
        ocr_cap = float(wd["ocr_upstream_cap_s"])
        cpu_cap = float(wd["cpu_task_cap_s"])
        rot_bound = float(wd["rotation_silence_bound_s"])
        # 不变式①：OCR 基准 ≥ 单次轮询封顶（否则上游正常等待会被判停滞）
        assert limits["ocr_running"] >= ocr_cap, (
            f"ocr_running={limits['ocr_running']} < ocr_upstream_cap_s={ocr_cap}"
        )
        # 不变式②：OCR 基准 ≥ 旋转补救静默上界（#120；同上道理）
        assert limits["ocr_running"] >= rot_bound, (
            f"ocr_running={limits['ocr_running']} < rotation_silence_bound_s={rot_bound}"
        )
        # 不变式③：cancelling 基准 ≥ CPU 任务封顶
        assert limits["cancelling"] >= cpu_cap, (
            f"cancelling={limits['cancelling']} < cpu_task_cap_s={cpu_cap}"
        )
        # 巡检存活证据：last_scan_at 必须被写上一次（证明后台巡检任务在**这个
        # 冻结包里**真的起来了 —— 一个没启动的循环永远不会写它）。
        #
        # B4 / #125：巡检是**启动即首扫** ⇒ 后端能应答 /health 时首扫早已完成。
        # 故预算必须**严格小于 interval**：旧实现「先睡一个周期再首扫」要到
        # t≈interval 才写 last_scan_at，若预算 ≥ interval（原为 interval + 30）
        # 则**新旧实现同样绿** ⇒ 这条产物级断言对 B4 是**瞎的**。
        # 预算仍由**自述的 interval_s** 派生（不硬编码 60）。
        interval = float(wd.get("interval_s") or 60.0)
        _wd_budget = interval * 0.8
        _wd_t0 = time.time()
        while not wd.get("last_scan_at") and time.time() - _wd_t0 < _wd_budget:
            time.sleep(0.5)
            wd = requests.get(f"{BASE}/api/health/watchdog", timeout=5).json()
        _wd_elapsed = time.time() - _wd_t0
        assert wd.get("last_scan_at"), (
            f"{_wd_budget:.0f}s（=0.8×interval）内 last_scan_at 仍为空 —— "
            f"巡检未「启动即首扫」（B4 / #125）"
        )
        ok("watchdog_invariants",
           f"ocr_running={limits['ocr_running']} >= "
           f"max(cap={ocr_cap}, rot_bound={rot_bound})"
           f"; cancelling={limits['cancelling']} >= cpu_cap={cpu_cap}"
           f"; first_scan@t+{_wd_elapsed:.1f}s (interval={interval:.0f}s)")
    except Exception as e:
        fail("watchdog_invariants", str(e))

    # 2. Upload page served
    section("Pages")
    try:
        r = requests.get(f"{BASE}/", timeout=5)
        assert r.status_code == 200
        assert "BatchSentry" in r.text or "upload" in r.text.lower()
        ok("upload_page")
    except Exception as e:
        fail("upload_page", str(e))

    try:
        r = requests.get(f"{BASE}/settings", timeout=5)
        assert r.status_code == 200
        ok("settings_page")
    except Exception as e:
        fail("settings_page", str(e))

    # 3. Settings API
    section("Settings API")
    try:
        r = requests.get(f"{BASE}/api/settings", timeout=5)
        assert r.status_code == 200
        data = r.json()
        ok("settings_get", f"keys={len(data)}")
    except Exception as e:
        fail("settings_get", str(e))

    # 4. Jobs API
    section("Jobs API")
    try:
        r = requests.get(f"{BASE}/api/jobs", timeout=5)
        assert r.status_code == 200
        data = r.json()
        ok("jobs_list", f"count={len(data.get('jobs', data))}")
    except Exception as e:
        fail("jobs_list", str(e))

    # 5. Configure LLM provider
    # 密钥只从环境取（PBC_E2E_LLM_KEY；旧名 PBC_E2E_DEEPSEEK_KEY 仍兼容），
    # 绝不写进仓库。未提供时如实登记为"未配置"，不伪造通过。
    #
    # ⚠️ 提供方**不得写死**：字段名必须由提供方名派生（f"{prov}_api_key"）。
    # 反例（2026-09-17 实测，本段曾在真实产物上 401）：原先固定写
    #   {"llm_provider": "deepseek", "deepseek_api_key": key}
    # 而注入的是**硅基流动**的 key ⇒ 请求打到 api.deepseek.com 得
    # `401 Authentication Fails, Your api key: ****ucgz is invalid`。
    # 这一错配长期"绿"着，因为样例 PDF 曾是**空白页**：Stage 2 无内容可分析
    # ⇒ 从不调用 LLM ⇒ 401 从未发生。夹具改为含真实文字后**当场暴露** ——
    # 这正是"断言必须能真的失败"为什么必须成立。
    section("Configure LLM")
    _key = llm_key()
    _prov = llm_provider()
    # 模型与提供方**配对**注入（空 = 沿用产品默认值）。不注入模型会落到产品默认
    # 的**收费**档，拿到 `402 balance insufficient` —— 那是**档位选择**问题，
    # 不是产品缺陷（详见 e2e_proc.LLM_MODEL_ENV 的实测记录）。
    _model = llm_model()
    _base_url = ""   # 由应用上报后回填（归因探测必须打**应用真正会用的那个端点**）
    #: 应用**实际上报**的模型 —— 归因探测第二段（计费请求）必须用**同一个**模型。
    #: 用 `_model`（我们请求的档位）会验错对象：应用可能没接受它。
    #: 预置空串而非留未绑定：上面的 try 若早退，下面第 8 段仍会读它。
    _model_used = ""
    _llm_ready = False   # 覆盖清单口径：凭据被应用**接受**才算就绪（不是"存在即可"）
    if not _key:
        print(f"    [WARN] 未设置 {llm_key_env_display()} —— 跳过 LLM 配置，"
              f"下游流水线将走降级路径（不是缺陷，但会记入覆盖清单）")
    try:
        _payload = {"llm_provider": _prov, f"{_prov}_api_key": _key}
        if _model:
            _payload[f"{_prov}_model"] = _model
        r = requests.post(f"{BASE}/api/settings", json=_payload, timeout=5)
        print(f"    POST settings status={r.status_code} body={r.text[:300]}")
        # Verify GET returns the key for THAT provider
        r2 = requests.get(f"{BASE}/api/settings", timeout=5)
        settings = r2.json()
        llm = settings.get("llm", {})
        provider = llm.get("provider") or llm.get("active_provider")
        providers_list = llm.get("providers", [])
        p = next((x for x in providers_list if x.get("name") == _prov), {})
        p_configured = p.get("configured", False)
        p_key_masked = p.get("api_key", "")
        _base_url = p.get("base_url") or ""
        _model_used = p.get("model") or ""
        ok("settings_configure_llm",
           f"provider={provider} {_prov}_configured={p_configured} key={p_key_masked}")
        # 判别性前置：密钥非空却"没配上"或"活动提供方不是它" ⇒ 后续任何
        # 结论都无意义（会被误报成产品缺陷），故当场 FAIL。
        if _key:
            if provider and provider != _prov:
                fail("settings_llm_provider_matches_key",
                     f"活动提供方={provider}，密钥却是给 {_prov} 的 —— 配置未生效")
            elif not p_configured:
                fail("settings_llm_provider_matches_key",
                     f"{_prov} 收到密钥后仍 configured=False（密钥被拒写？）")
            else:
                ok("settings_llm_provider_matches_key", _prov)
                _llm_ready = True
                # 判别性前置（B11-11）：**要求了模型却没生效** ⇒ 本轮验的不是
                # 我们选定的那个档位 ⇒ 结论无意义（且会被误归因到产品头上）。
                if _model and _model_used != _model:
                    fail("settings_llm_model_matches",
                         f"请求模型={_model} 但应用上报 model={_model_used!r} ⇒ 模型未生效")
                else:
                    ok("settings_llm_model_matches",
                       f"model={_model_used or '(产品默认)'}")
    except Exception as e:
        fail("settings_configure_llm", str(e))

    # LLM 配置这条覆盖项：就绪记 covered，未提供凭据记 skipped（**不得**因为
    # "其它断言全绿"而被读成已验）。凭据缺失是环境事实，不是缺陷。
    # ⚠️ 措辞收窄：covered 的含义只是"凭据被**写入**并生效于 settings 通路"，
    # **不**声称凭据有效 —— 2026-09-21 实测：塞一把必然无效的 key，应用照样
    # 报 configured=True（配置期不做上游校验）。凭据是否有效由 LLM 链路覆盖项
    # （流水线终态）回答，两者不可互相顶替。
    if _llm_ready:
        COV.record(ENTRY_LLM_CONFIG, STATUS_COVERED,
                   f"provider={_prov} 凭据被应用写入（仅证明配置通路，不证明凭据有效）",
                   _prov)
    elif not _key:
        COV.record(ENTRY_LLM_CONFIG, STATUS_SKIPPED,
                   f"未提供 {LLM_KEY_ENV} ⇒ LLM 链路本轮未验", "")
    else:
        COV.record(ENTRY_LLM_CONFIG, STATUS_FAILED,
                   f"提供了 {LLM_KEY_ENV} 但配置未生效（见 settings_llm_provider_matches_key）",
                   _prov)

    # 5b. Configure OCR backend —— 只配 LLM 不配 OCR 是**假的绿**：
    # 实测（2026-09-16）Paddle 的 api_url 为空时提交即失败
    # （`Invalid URL '': No scheme supplied`），pipeline 一路走到 error，
    # 而冒烟此前把 error 也判 PASS。凭据来自环境（PBC_E2E_*），绝不入库。
    section("Configure OCR")
    _paddle = os.environ.get("PBC_E2E_PADDLE_TOKEN", "")
    _mineru = os.environ.get("PBC_E2E_MINERU_TOKEN", "")
    _env_injected = bool(_paddle or _mineru)
    #: 本轮**实际配置的**后端名 —— 后面用它判 `ocr_backend_used` 是否被
    #: failover 掩盖（"跑了 OCR" 与 "跑了我配的那个后端" 是两件事）。
    _ocr_expected = ""
    if _env_injected:
        payload = {"ocr_backend": "paddle" if _paddle else "mineru"}
        _ocr_expected = payload["ocr_backend"]
        if _paddle:
            payload["paddle_ocr_api_url"] = os.environ.get(
                "PBC_E2E_PADDLE_URL",
                "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs")
            payload["paddle_ocr_token"] = _paddle
            payload["paddle_ocr_model"] = os.environ.get(
                "PBC_E2E_PADDLE_MODEL", "PaddleOCR-VL-1.6")
        if _mineru:
            payload["mineru_token"] = _mineru
        try:
            r = requests.post(f"{BASE}/api/settings", json=payload, timeout=5)
            assert r.status_code == 200, r.text[:200]
            ok("settings_configure_ocr", f"backend={payload['ocr_backend']}")
            COV.record(ENTRY_OCR_CONFIG, STATUS_COVERED,
                       f"backend={payload['ocr_backend']} 配置被接受",
                       payload["ocr_backend"])
        except Exception as e:
            fail("settings_configure_ocr", str(e))
            COV.record(ENTRY_OCR_CONFIG, STATUS_FAILED, f"配置 OCR 失败：{e}", "")
    else:
        print("    [WARN] 未提供 PBC_E2E_PADDLE_TOKEN / PBC_E2E_MINERU_TOKEN —— "
              "本轮不注入 OCR 凭据；就绪与否改由**应用自报**判定"
              "（e2e appdata 固定复用，可能带着上轮写入的凭据）")

    # 就绪真值 = **应用自报**（单一真值），不是"本轮 env 有没有给"。
    # 由来见 `tests/e2e_proc.ocr_ready_from_settings` 的注释：只按 env 判会把
    # "其实跑了 OCR"记成 skipped（低报），且会把真实 error 吞成"预期降级"（fail-open）。
    _ocr_self_ok = True
    try:
        _settings_now = requests.get(f"{BASE}/api/settings", timeout=5).json()
    except Exception as e:
        _settings_now = {}
        _ocr_self_ok = False
        fail("ocr_ready_self_report", f"读取应用 OCR 就绪状态失败：{e}")
    OCR_CONFIGURED = (ocr_ready_from_settings(_settings_now)
                      if _ocr_self_ok else _env_injected)
    if _ocr_self_ok:
        ok("ocr_ready_self_report",
           f"应用自报 configured={OCR_CONFIGURED}（env_injected={_env_injected}）")
    if not _env_injected:
        # 该**配置步骤**本轮确实没执行（skipped 语义不变）；但**不得**再说
        # "流水线成功路径本轮未验" —— 应用若已带凭据，OCR 照跑（2026-10-08 实测）。
        COV.record(
            ENTRY_OCR_CONFIG, STATUS_SKIPPED,
            "本轮未注入 OCR 凭据（该配置步骤未执行）；"
            f"应用自报已就绪={OCR_CONFIGURED}"
            + (" ⇒ OCR 链路仍会真实运行" if OCR_CONFIGURED else
               " ⇒ 流水线成功路径本轮未验"), "")

    # 6. PDF upload —— 样例必须**含真实文字**，不能是空白页。
    # 为什么（2026-09-17 实测，两处盲区同根）：
    #   (a) 空白页 ⇒ 每页被判为"空/稀疏" ⇒ 触发**旋转自愈** ⇒ 冒烟的时长与结果
    #       被**上游 Paddle 状况**支配。实测同一样例：paddle 健康时 2m54s 通过；
    #       拥塞时 >5min 仍停在 `Rotation probe upstream congestion … backing off`
    #       （#120 的退避阶梯本身工作正常，但它让冒烟变得**不可重复**）。
    #   (b) 空白页 ⇒ Stage 2 **无内容可分析** ⇒ 下面 `error and OCR_CONFIGURED`
    #       分支**永不可达** —— "LLM 凭据失效"在冒烟里**报不出来**（实测：用已失效的
    #       key 跑，仍得 status=review）。这违反项目自己的规矩"断言必须能真的失败"。
    # 用 PyMuPDF 现生成一页带文字的小 PDF（项目已依赖 fitz），且**每次都重写** ——
    # 此前是 `if not os.path.exists`，陈旧的空白件会被一直沿用。
    section("Upload")
    test_pdf = os.path.join(os.environ["TEMP"], "e2e-test-text.pdf")
    try:
        import fitz  # PyMuPDF（项目依赖，随包分发）
        _doc = fitz.open()
        _pg = _doc.new_page()                       # A4
        _pg.insert_text((72, 100),
                        "Batch Production Record   batch no 72408119", fontsize=12)
        _pg.insert_text((72, 130),
                        "Step 1 Charge API  spec 10.0 mg  actual 9.8 mg", fontsize=12)
        _pg.insert_text((72, 160),
                        "Operator ZHANG   Reviewed by LI   2026-09-17", fontsize=12)
        _doc.save(test_pdf)
        _doc.close()
        print(f"    smoke fixture = {test_pdf} "
              f"({os.path.getsize(test_pdf)} B, 含真实文字)")
    except Exception as e:
        fail("smoke_pdf_fixture", f"生成含文字样例失败：{e}（下游上传将一并失败）")
    try:
        with open(test_pdf, "rb") as f:
            r = requests.post(f"{BASE}/api/jobs?force=1", files={"file": ("test.pdf", f, "application/pdf")}, timeout=10)
        assert r.status_code == 200, f"status={r.status_code} body={r.text[:200]}"
        data = r.json()
        job_id = data.get("job_id") or data.get("id")
        assert job_id, f"no job_id in response: {data}"
        ok("upload", f"job_id={job_id}")
    except Exception as e:
        fail("upload", str(e))
        job_id = None

    # 7. Job status
    if job_id:
        section("Job Status")
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            assert r.status_code == 200
            data = r.json()
            ok("job_status", f"status={data.get('status')}")
        except Exception as e:
            fail("job_status", str(e))

        # 8. Wait for pipeline and check review page
        # 断言强度取决于**环境是否具备跑通条件**（这是本用例的核心纪律）：
        #   - 已配 OCR 凭据 → 必须走成功路径（review/partial_review）；
        #     出现 error 即真实缺陷 → FAIL 并带出 error_message。
        #   - 未配凭据 → 如实标注"降级"，**绝不冒充 PASS**。
        #     历史缺陷：此前 `status in (..., "error", ...)` 直接 ok() ——
        #     "pipeline 完全跑不起来"在冒烟里也是绿的（与 importorskip 同源的
        #     "静默成功"）。
        section("Pipeline -> Review")
        terminal, err_msg = "", ""
        # 终态等待预算**派生**而非写死（`docs/RUNTIME_WATCHDOG.md` §8.4 同源教训：
        # 写死单值会因上游排队把真实长跑误判为失败）。组成 = 一次 1 页轮询封顶
        # （`poll_timeout_for_pages(1)`，当前 630s）+ 分析阶段基线；env 可覆盖。
        # 2026-09-17 实测：上游 Paddle 排队时，单页 2 分钟仍在 `ocr_running`
        # —— 旧的写死 60s 当场把一次**完全正常**的作业判成 FAIL。
        from core.ocr_client import poll_timeout_for_pages
        _budget_s = float(os.environ.get(
            "E2E_FROZEN_TERMINAL_TIMEOUT", poll_timeout_for_pages(1) + 300))
        _deadline = time.time() + _budget_s
        while time.time() < _deadline:
            time.sleep(2)
            try:
                r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
                data = r.json()
                terminal = data.get("status", "")
                err_msg = data.get("error_message") or ""
                if terminal in ("review", "partial_review", "error", "cancelled"):
                    break
            except Exception:
                pass
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5)
            data = r.json()
            terminal = data.get("status", terminal)
            err_msg = data.get("error_message") or err_msg
            if terminal in ("review", "partial_review"):
                ok("pipeline_terminal", f"status={terminal}")
            elif terminal == "error" and OCR_CONFIGURED:
                # 判据强度不变（仍是 FAIL，fail-closed）；这里只把**归因**说准。
                # 原先把任何"已配 OCR 的 error"一律写成"真实缺陷"，实测会把一份
                # 已失效的 key 报成产品缺陷，把排查引向代码（Round 54）。
                # ⚠️ 2026-09-30 第二次同类事故：只探**免费**端点 ⇒ 账户欠费(402)
                # 被报成"产品缺陷"。现改为两段式探测（免费 + 计费），归因文案的
                # **唯一实现**在 `tests/e2e_proc.llm_failure_attribution`。
                # ⚠️ 覆盖状态**仍记 failed**，不因"归因是环境"而改记 skipped ——
                # 流水线**确实执行了**且没到成功终态，符合 failed 的定义
                # （"执行了但结果不符预期"）；skipped 的定义是"因环境缺项**未执行**"，
                # 用在这里是**事实错误**。归因与状态是两个正交维度，不可互相顶替。
                verdict, why = probe_llm_credential(_base_url, _key, _model_used)
                attribution = llm_failure_attribution(verdict, why)
                fail("pipeline_terminal",
                     f"status=error（已配 OCR 仍失败）"
                     f"error_message={err_msg[:200]} ｜ {attribution}")
            elif terminal in ("error", "cancelled"):
                print(f"    [SKIP] pipeline_terminal status={terminal} —— 环境未配 OCR "
                      f"凭据，属预期的降级路径（error_message={err_msg[:160]}）")
            else:
                fail("pipeline_terminal",
                     f"未在 {_budget_s:.0f}s 内到达终态: status={terminal!r}")
        except Exception as e:
            fail("pipeline_terminal", str(e))

        # 8b. #132 —— failed_pages 的**运行时类型**必须与前端预期一致。
        #     db 列是 TEXT，接口曾原样透传 ⇒ 响应里是 `"[2, 1]"`（字符串），
        #     前端 `Array.isArray()` 拿到即静默退化成 `[]` ⇒ 失败页永不显示。
        #     本条跑在**打包产物自己起的服务**上，故它证明的是"修复已随产物
        #     分发"，而非"源码树里写过这句话"。
        #     ⚠️ **能力边界（诚实标注）**：本冒烟的成功路径不会产生失败页 ⇒
        #     `failed_pages` 为 `None`，**修复前后都是 `None`** ⇒ 这条在此
        #     场景下**不具判别力**。真正有判别力的是 #127 验收（用失效凭据
        #     造出 `failed_pages=[2,1]` 并断言它是 list），二者互补，不可互替。
        try:
            fp = requests.get(
                f"{BASE}/api/jobs/{job_id}", timeout=5
            ).json().get("failed_pages")
            assert not isinstance(fp, str), (
                f"failed_pages 是字符串 {fp!r} —— 前端 Array.isArray 会静默丢弃（#132）"
            )
            assert fp is None or isinstance(fp, list), f"failed_pages 类型非法: {fp!r}"
            ok("failed_pages_type", f"failed_pages={fp!r}")
        except Exception as e:
            fail("failed_pages_type", str(e))

        # 9. Review page
        try:
            r = requests.get(f"{BASE}/jobs/{job_id}/review", timeout=5)
            assert r.status_code == 200
            ok("review_page")
        except Exception as e:
            fail("review_page", str(e))

        # 10. Findings API
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/findings", timeout=5)
            assert r.status_code == 200
            data = r.json()
            count = len(data) if isinstance(data, list) else len(data.get("findings", []))
            ok("findings_api", f"count={count}")
        except Exception as e:
            fail("findings_api", str(e))

        # 11. Page image
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/page/1", timeout=10)
            assert r.status_code == 200
            assert r.headers.get("content-type", "").startswith("image/")
            ok("page_image", f"size={len(r.content)}")
        except Exception as e:
            fail("page_image", str(e))

        # 12. Report
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/report.md", timeout=5)
            assert r.status_code == 200
            ok("report_md", f"len={len(r.text)}")
        except Exception as e:
            fail("report_md", str(e))

        # 12b. 链路**权威证据** —— 覆盖清单里的 covered 只能建立在此之上。
        #
        # 为什么必须新增（2026-09-23 实测的真实假绿）：一把 key 的账户**余额耗尽**时，
        # 逐页分析先成功、跨页 LLM 得 `402 code=30001 account balance is insufficient`；
        # 产品**按设计降级**（把"LLM 调用失败"写成 finding）并照常走到 `review`。
        # 旧的覆盖判据只看 `terminal == review` ⇒ 记 `covered` ⇒ 在
        # `PBC_E2E_REQUIRE_LLM=1` 下**退出码 0**：一条本该拦住发版的门禁**放了行**。
        # 这就是本项目自己的纪律「跳过 ≠ 已覆盖」在"流水线"这一项上从未落地。
        #
        # 证据一律取**产品自己的记录**，不另起一套真值：
        #   LLM —— `GET /api/jobs/{id}/llm_audit` 的 entries 里有没有 `success=1`
        #   OCR —— `GET /api/jobs/{id}` 的 `ocr_backend_used`（产品记录的真实后端）
        # 取不到 ⇒ 事实为 None ⇒ 判据侧 fail-closed（判不了 ≠ 已验）。
        section("Link evidence (authoritative)")
        _llm_ok = None
        _ocr_backend = None
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/llm_audit", timeout=10)
            assert r.status_code == 200, f"status={r.status_code}"
            _entries = r.json().get("entries") or []
            _llm_ok = any(e.get("success") for e in _entries)
            _bad = [e for e in _entries if not e.get("success")]
            _okc = sum(1 for e in _entries if e.get("success"))
            if _llm_ok:
                ok("llm_audit_success",
                   f"success={_okc}/{len(_entries)}（失败 {len(_bad)} 次）")
            else:
                _why = str((_bad[0].get("error") if _bad else "") or
                           "审计表里没有任何 LLM 调用")
                fail("llm_audit_success",
                     f"**没有任何成功的 LLM 调用**（共 {len(_entries)} 条）"
                     f"⇒ 终态是靠降级达成的，LLM 链路未验；首条错误：{_why[:220]}")
        except Exception as e:  # noqa: BLE001 — 判不了 ⇒ fail-closed
            fail("llm_audit_success", f"取不到 LLM 审计 ⇒ 判不了（fail-closed）：{e}")
        try:
            _job = requests.get(f"{BASE}/api/jobs/{job_id}", timeout=5).json()
            _ocr_backend = _job.get("ocr_backend_used")
            assert _ocr_backend, "产品未记录 ocr_backend_used"
            if _ocr_expected and str(_ocr_backend).strip().lower() != _ocr_expected:
                # 判据强度**不变**（仍是 FAIL）：本轮要验的那个后端没跑成。
                # 但**保留真实值**，让覆盖清单能写出"真实后端是 X 而不是 Y"。
                # 旧写法在此把 `_ocr_backend` 置 None ⇒ reason 写成
                # "**未记录** ocr_backend_used"，与事实**相反**（2026-10-08 实测：
                # PaddleOCR 上游 `state=failed, errorMsg=系统错误-单页` ⇒
                # 产品按设计 failover 到 MinerU ⇒ 字段有值 'mineru'）。
                # 那条 reason 会把排查引向"字段为什么是空的"，而真问题是
                # "配置的后端为什么没跑成" —— 归因错向比没有归因更贵。
                fail("ocr_backend_used",
                     f"ocr_backend_used={_ocr_backend!r}，期望本轮配置的 "
                     f"{_ocr_expected!r} ⇒ 配置的后端未完成解析、由 failover 兜底")
            else:
                ok("ocr_backend_used",
                   f"{_ocr_backend}（期望 {_ocr_expected or '未配'}）")
        except Exception as e:  # noqa: BLE001 — 判不了 ⇒ fail-closed
            _ocr_backend = None
            fail("ocr_backend_used", str(e))

        # ── 覆盖清单（B9-7）：流水线两条链路的状态一律由**事实**推导
        # （终态字符串 + 凭据是否被应用接受 + **各链路自己的权威证据**），
        # 规则只此一处（`tests/e2e_coverage.classify_pipeline`）。
        COV.record_many(classify_pipeline(
            terminal, llm_ready=_llm_ready, ocr_ready=OCR_CONFIGURED,
            llm_call_succeeded=_llm_ok, ocr_backend_used=_ocr_backend,
            ocr_backend_expected=_ocr_expected))
    else:
        # 上传就没成功 ⇒ 流水线根本没跑（产品在未配置 LLM 时会直接 400 拒绝上传，
        # 见 api/jobs/upload.py）。**传空终态 + upload_failed** 让判据记 failed，
        # 且归因写成"作业从未建立" —— 绝不能因为"作业都没建起来"就记成 skipped：
        # 把"没跑"混进"环境跳过"里，正是本轮要消除的盲区。
        COV.record_many(classify_pipeline(
            "", llm_ready=_llm_ready, ocr_ready=OCR_CONFIGURED, upload_failed=True))

    # 13. Static assets
    section("Static Assets")
    try:
        r = requests.get(f"{BASE}/static/app.css", timeout=5)
        assert r.status_code == 200
        ok("app_css", f"len={len(r.text)}")
    except Exception as e:
        fail("app_css", str(e))

    try:
        r = requests.get(f"{BASE}/static/review.js", timeout=5)
        assert r.status_code == 200
        ok("review_js", f"len={len(r.text)}")
    except Exception as e:
        fail("review_js", str(e))

    # 13b. #127 的可见性修复必须**在产物里**。
    #     这才是"验产物而非验源码"的实质：修复有没有到达用户手上，是一个
    #     **分发事实**，源码树干净并不蕴含它。静态资源由冻结包直接提供，
    #     故这几条断言证明的正是"要分发的那份东西带着修复"。
    #
    #     ⚠️ 判据必须**与文件位置无关**（2026-09-30 修）。原实现只在
    #     `upload.js` / `review.js` 里找标记，但 #127 的消费逻辑随后被拆进
    #     `upload-jobs.js`（`job.failed_pages` 在 518 行、`["error",
    #     "partial_review"].includes(st)` 在 583 行）与
    #     `review-pageinfo.js`（`structured._error` 在 88 行）⇒ 断言**假红**。
    #     要证明的是上面那条**分发事实**，不是"修复住在哪个文件里"；
    #     把位置写进判据，等于让一次纯模块化重构把发版门禁变成噪声（"狼来了"）。
    #     护栏：tests/unit/test_e2e_status_js.py::TestE2e127VisibilityJudgeIsLocationIndependent
    def _served_js(paths):
        out = {}
        for _p in paths:
            _r = requests.get(f"{BASE}{_p}", timeout=5)
            _r.raise_for_status()
            out[_p] = _r.text
        # 防空转：必须真的取到非空内容；否则"没找到标记"其实只是"什么都没取到"
        _empty = [_p for _p, _t in out.items() if not _t.strip()]
        assert not _empty, f"静态资源取到空内容，标记断言无意义: {_empty}"
        return "\n".join(out.values())

    section("Frontend #127 Visibility (shipped bundle)")
    try:
        up = _served_js(["/static/upload.js", "/static/upload-jobs.js"])
        # upload 页必须真的消费失败页与失败原因（文本级，成立即说明控制流消费了它们）
        checks = {
            "failed_pages_rendered": "job.failed_pages" in up,
            "reason_shown_for_partial_review": (
                '(st === "error" || st === "partial_review")' in up
                or '["error", "partial_review"].includes(st)' in up
            ),
        }
        missing = [k for k, v in checks.items() if not v]
        assert not missing, f"产物内缺 #127 修复标记: {missing}"
        ok("upload_js_127", "显失败页 / 显原因")
    except Exception as e:
        fail("upload_js_127", str(e))

    # 13c. 档位配色 —— **按行为**验证，不按字面量（Round 54 修：原判据两半都失效）。
    section("Frontend #127 dot semantics (shipped bundle)")
    try:
        sj = requests.get(f"{BASE}/static/status.js", timeout=5).text
        m = status_dot_classes(sj)
        problems = dot_semantics_problems(m)
        assert not problems, "；".join(problems)
        ok("status_dot_semantics",
           f"partial_review={m['partial_review']} != review={m['review']}")
    except Exception as e:
        fail("status_dot_semantics", str(e))

    try:
        # 同 13b：`structured._error` 现由 `review-pageinfo.js` 消费（88 行），
        # 而它原先住在 `review.js` ⇒ 判据同样必须与文件位置无关。
        rj = _served_js(["/static/review.js", "/static/review-pageinfo.js"])
        assert "structured._error" in rj, (
            "复核页未消费 structured._error（页内横幅显不出真实原因）"
        )
        ok("review_js_127", "页内横幅显真实原因")
    except Exception as e:
        fail("review_js_127", str(e))

    # 13d. settings 页拆分模块 —— 必须**全部**能从产物里取到。
    #      拆分后入口只做接线，任何模块漏进包 ⇒ 页面静默失去对应职责
    #      （无任何报错，与"测了 A 发了 B"同宗）。见
    #      docs/ADVERSARIAL_REVIEW_2026-09-28.md §维度 4。
    section("Frontend settings modules (shipped bundle)")
    try:
        mods = ["settings-state.js", "settings-llm.js", "settings-ocr.js",
                "settings-feishu.js", "settings-rules.js", "settings.js"]
        _served_js([f"/static/{m}" for m in mods])  # 取到空内容即抛（防空转）
        ok("settings_modules_served", f"{len(mods)} 个模块均可取且非空")
    except Exception as e:
        fail("settings_modules_served", str(e))

    try:
        # 入口必须真的启动初始渲染；拆分后是 `S.load()`（load 定义在 settings-ocr.js）。
        # ⚠️ 不能只查 `load()` —— 入口文件头注释里也有该词，那是**空断言**。
        entry = requests.get(f"{BASE}/static/settings.js", timeout=5).text
        assert "S.load();" in entry, "入口未调用 S.load() —— 设置页永不渲染"
        ok("settings_entry_calls_load", "S.load()")
    except Exception as e:
        fail("settings_entry_calls_load", str(e))

    # 14. API docs
    section("API Docs")
    try:
        r = requests.get(f"{BASE}/docs", timeout=5)
        assert r.status_code == 200
        ok("swagger_ui")
    except Exception as e:
        fail("swagger_ui", str(e))

finally:
    stop_server(proc, _logf, timeout=5)

# ── 覆盖清单收尾（B9-7）────────────────────────────────────────────
# 先落盘、再判定：即使下面因硬要求未满足而 FAIL，清单本身也必须在磁盘上 ——
# 事后审计要回答的是"这轮到底验到了什么"，不该依赖还留在终端里的 stdout。
COVERAGE_FILE = COV.write()
section("Coverage")
for _line in COV.report_lines():
    print(_line)
print(f"  coverage file = {COVERAGE_FILE}")

# 硬要求**默认不开启**：日常冒烟可在弱环境跑；发版/验收前置
# PBC_E2E_REQUIRE_LLM=1，未真实覆盖即整体 FAIL（见 DEPLOYMENT.md）。
GAPS = required_gaps(COV)
for _gap in GAPS:
    fail("coverage_requirement", _gap)

section("Results")
passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
print(f"\n{'='*50}")
print(f"target: {EXE}")
print(f"Total: {passed} passed, {failed} failed")
for s, name, detail in RESULTS:
    marker = "OK" if s == "PASS" else "XX"
    print(f"  {marker} {name}: {detail}")
_counts = COV.counts()
print(f"Coverage: covered={_counts[STATUS_COVERED]} "
      f"skipped={_counts[STATUS_SKIPPED]} failed={_counts[STATUS_FAILED]}"
      f"  (file: {COVERAGE_FILE})")
if GAPS:
    print(f"UNMET HARD REQUIREMENTS: {'; '.join(GAPS)}")
print(f"{'='*50}")
sys.exit(exit_code(failed, GAPS))
