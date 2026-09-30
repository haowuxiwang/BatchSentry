/* ============================================================
   Upload 页 — 文件上传交互（R63 P2-1 拆分后瘦身为单一职责）

   原文件 1231 行混杂上传交互与历史列表渲染两块职责，现已拆分
   （加载顺序见 templates/upload.html）：
     upload-jobs.js   历史列表 / SSE 聚合实时状态 / 归档删除
     upload.js        本文件 — 文件选择 / 拖拽 / XHR 上传 / 进度条 /
                      409 去重 force 重传
   依赖：window.__PBC__.jobs_count（可选，仅用于日志）、
   window.__PBC__.limits（服务端限额注入，单一真值）。
   ============================================================ */
(function () {
  "use strict";

  // === PBC Upload Page Logger ===
  const log = (...args) =>
    console.log("%c[PBC]", "color:#0ea5e9;font-weight:bold", ...args);
  log.warn = (...args) =>
    console.warn("%c[PBC]", "color:#f59e0b;font-weight:bold", ...args);
  log.err = (...args) =>
    console.error("%c[PBC]", "color:#ef4444;font-weight:bold", ...args);

  const ctx = window.__PBC__ || {};
  const input = document.getElementById("file-input");
  const area = document.getElementById("upload-area");
  const status = document.getElementById("status");

  // 图片上传（Phase 13）：与后端 _IMAGE_EXTENSIONS 同步 — 客户端预检
  const IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"];

  // 上传限额：**只认服务端注入**（单一真值 config.UPLOAD_LIMITS，见
  // templates/upload.html）。这里刻意**不设兜底数值** —— 兜底副本会随服务端
  // 调整而漂移，"前端放行、后端拒绝"正是本次要根除的缺陷。注入缺失时跳过
  // 前端预检，交由服务端判定（服务端本来就权威，且其 detail 会被下方
  // HTTP 错误分支展示），只是多一次往返。
  const LIMITS = ctx.limits || null;
  if (!LIMITS) {
    log.warn(
      "window.__PBC__.limits 缺失 — 跳过前端体积预检，交由服务端判定",
    );
  }

  // === 并发额度（发送前预检）=========================================
  //
  // **要防的失效模式**：多文件是**串行**上传的（见 submitFiles），而后端配额
  // 是 `MAX_CONCURRENT_JOBS`（默认 3）。用户选 5 份 ⇒ 第 4、5 份必然吃 409，
  // 且吃 409 的那一刻文件**已经完整传完**（大文件分钟级、几百 MB）——
  // 带宽与等待全废，然后还要用户手动重选重传。
  //
  // 修法：发送**之前**问一句"还有没有空位"。额度由服务端下发
  // （`window.__PBC__.concurrency.limit`，单一真值 api.jobs._MAX_CONCURRENT_JOBS），
  // 活跃数由 upload-jobs.js 从它唯一持有的 /api/jobs/live 聚合帧登记
  // （`window.PbcJobs.quota()`，单一写点）。
  //
  // **fail-open 是硬约束**：共享件缺失 / limit<=0（服务端未注入）/ 数值非法
  // 一律返回 null ⇒ **跳过预检**。绝不因为"读不到额度"就拦住上传 ——
  // 后端才是权威，且其 409 有独立分支（见 quota 409 处理）兜底。
  // 反过来（把"读不到"当成"已满"）会让一次注入失误把整个上传功能锁死。
  function quotaSnapshot() {
    const jobs = window.PbcJobs;
    if (!jobs || typeof jobs.quota !== "function") return null;
    let q = null;
    try {
      q = jobs.quota();
    } catch (err) {
      log.warn("PbcJobs.quota() 抛错 — 跳过额度预检", err);
      return null;
    }
    if (!q) return null;
    const active = Number(q.active);
    const limit = Number(q.limit);
    if (!Number.isFinite(limit) || limit <= 0) return null;
    if (!Number.isFinite(active) || active < 0) return null;
    // 熔断**不在这里**自愈：解除点只有一个 —— `submitFiles` 的批次边界。
    // 在这里顺手清掉会与它重复（同一语义两处实现，本仓库反复踩过），
    // 且会悄悄削弱熔断（同批后续文件又能各等一轮 30min）。
    return { active: active, limit: limit, atCapacity: active >= limit };
  }

  // 预检等待的参数。
  //   · 轮询 2s —— 与后端 SSE 帧间隔（`_SSE_POLL_SECONDS`）同频：比帧更快地
  //     轮询只是在读同一个未更新的值，纯空转。
  //   · 上限 30min —— 与 procpool 单任务超时（`PBC_CPU_TASK_TIMEOUT_S`=1800s）
  //     同量级：一个 job 的合理生命周期就在这个数量级。超过它说明后端异常，
  //     此时应报错让用户介入，而不是无限期地把他的批次扣在等待里。
  const QUOTA_WAIT_POLL_MS = 2000;
  const QUOTA_WAIT_MAX_MS = 30 * 60 * 1000;

  // 等待超时**熔断**：额度是**全局**的，一轮等待超时意味着后端在 30min 内
  // 一个名额都没放出（异常）。此时同一批里后续每一份都必然同样撞墙，若各自
  // 再等 30min，用户会被逐个扣住 —— 整批 N 份 = N×30min。故置位后**立即失败**。
  //
  // 解除点**只有一个**：`submitFiles` 的批次边界（新一轮选择 = 新一轮额度博弈）。
  // 刻意不在 `quotaSnapshot()` 里"顺手自愈" —— 那会与批次边界重复，
  // 且悄悄削弱熔断（同批后续文件又能各等一轮）。见 MEMORY 的"单一真值"约定。
  let quotaWaitTimedOut = false;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** 轮询直到出现空位。返回 true=可以发；false=等待超时（或被熔断）。
   *
   *  ⚠️ 本函数是熔断的**唯一判定点**（`uploadFile` 的预检刻意不再重复判定）：
   *  它既是置位者也是唯一的判定点，故对**所有**调用路径都成立 —— 预检路径与
   *  额度 409 的重试路径都受保护。 */
  async function waitForQuotaSlot() {
    // 熔断：本轮已等超时过且仍未出现空位 ⇒ 立即失败，不再空等一轮
    // （否则整批 N 份 = N×30min 逐个扣住）。
    const start = quotaSnapshot();
    if (quotaWaitTimedOut && start && start.atCapacity) return false;
    const deadline = Date.now() + QUOTA_WAIT_MAX_MS;
    while (Date.now() < deadline) {
      await sleep(QUOTA_WAIT_POLL_MS);
      const q = quotaSnapshot();
      // 读不到额度了（共享件被卸载 / 注入丢失）⇒ 不再等，放行交给后端判定
      if (!q || !q.atCapacity) return true;
    }
    quotaWaitTimedOut = true;
    return false;
  }

  /** 后端"并发额度"409 的识别（与去重 409 区分开）。
   *
   * 文案由 api/jobs/upload.py + actions.py 单点产出：
   *   `已有 N 个任务在处理中，上限为 M。请等待完成或取消后再试。`
   * 去重 409 的文案是 `该文件已上传过（…）` —— 两者语义完全不同：
   *   · 去重 = "这份内容已在库里"，重传需用户确认（force）；
   *   · 额度 = "现在没空位"，等一会儿就能过，**不该打扰用户**。
   */
  function isQuotaRejection(detail) {
    return /已有 \d+ 个任务在处理中/.test(String(detail || ""));
  }

  // 额度 409 的有界自动重试次数（预检与后端 INSERT 之间仍有窗口 ——
  // 例如另一个标签页刚占掉最后一个名额）。上限取小：每一次重试都要重传
  // 整个文件，用户等不起无限次。
  const QUOTA_RETRY_MAX = 2;

  // 人类可读体积：按量级选单位。
  // 旧写法 `Math.floor(max_bytes / 1024 / 1024)` 在 max_bytes < 1MB 时得到 0，
  // 于是提示变成"文件超过 0MB 上限"——荒谬，且让用户以为限额被配坏了
  // （真实原因是他上传的文件确实超了，只是限额本身就小于 1MB）。
  function formatBytes(n) {
    const b = Number(n) || 0;
    if (b >= 1024 * 1024) {
      const mb = b / 1024 / 1024;
      return `${Number.isInteger(mb) ? mb : mb.toFixed(1)} MB`;
    }
    if (b >= 1024) return `${Math.round(b / 1024)} KB`;
    return `${b} B`;
  }

  log("upload.html loaded", {
    has_input: !!input,
    has_area: !!area,
    has_status: !!status,
    jobs_count: ctx.jobs_count || 0,
  });

  if (!input || !area || !status) {
    log.err("Critical DOM elements missing — interactions will fail", {
      input,
      area,
      status,
    });
  }

  function setStatus(text, kind) {
    const colors = {
      info: "text-foreground",
      ok: "text-emerald-600",
      err: "text-destructive",
    };
    log("setStatus()", {
      kind,
      text: typeof text === "string" ? text.slice(0, 100) : text,
    });
    // Security: use textContent to neutralize any HTML in the text.
    // Previously used innerHTML with file.name / err.message interpolated,
    // which allowed a malicious filename like '<img src=x onerror=alert(1)>.pdf'
    // to execute arbitrary script.
    status.innerHTML = "";
    const p = document.createElement("p");
    p.className = colors[kind] || colors.info;
    p.textContent = String(text);
    status.appendChild(p);
  }

  if (input) {
    input.addEventListener("change", (e) => {
      const files = Array.from((e.target && e.target.files) || []);
      log("file-input change event", {
        count: files.length,
        names: files.map((f) => f && f.name),
      });
      submitFiles(files);
    });
  }

  if (area) {
    area.addEventListener("dragover", (e) => {
      e.preventDefault();
      log("dragover on upload-area");
      area.classList.add("border-primary", "bg-accent/40");
      area.classList.remove("border-border");
    });
    area.addEventListener("dragleave", () => {
      log("dragleave on upload-area");
      area.classList.remove("border-primary", "bg-accent/40");
      area.classList.add("border-border");
    });
    area.addEventListener("drop", (e) => {
      e.preventDefault();
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      log("drop on upload-area", {
        count: files.length,
        names: files.map((f) => f && f.name),
      });
      area.classList.remove("border-primary", "bg-accent/40");
      area.classList.add("border-border");
      submitFiles(files);
    });
  }

  /** 文件预检 —— **校验的单一真值**（`uploadFile` 与多文件预检共用）。
   *
   * 返回 `""` 表示通过，否则返回**可直接展示**的中文原因。
   * 抽出来的理由：多文件路径要先把不合格的一次性报出来，若在 `submitFiles`
   * 里再抄一份扩展名/体积判据，两处必然漂移（本项目反复踩过"手抄副本"）。
   */
  function rejectReason(file) {
    const name = (file && file.name) || "";
    const lower = name.toLowerCase();
    if (!lower.endsWith(".pdf") && !IMAGE_EXTS.some((e) => lower.endsWith(e))) {
      return "仅支持 PDF 或图片（jpg/jpeg/png/webp/bmp/tif/tiff）";
    }
    // 体积预检（仅当服务端注入了限额时）—— 省一次往返；判定权威仍在服务端
    if (LIMITS && file.size > LIMITS.max_bytes) {
      return `文件超过 ${formatBytes(LIMITS.max_bytes)} 上限`;
    }
    return "";
  }

  /** 多文件提交（对抗审查 P0）。
   *
   * 此前 change/drop 都只取 `files[0]` ⇒ 拖入 3 份只有 1 份被处理，另 2 份
   * **无任何提示地消失**（静默数据丢失，用户以为传上去了）。
   *
   * ⚠️ **只给 input 加 `multiple` 是伪修复**：`uploadFile` 成功后 1.5s 会
   * `window.location.href = /jobs/{id}/review` 跳走、上传区被
   * `pointerEvents="none"` 禁用、进度条是单例 ⇒ 第 1 份成功就杀掉其余上传，
   * 结果是"静默丢弃"变成"竞态 + 跳转中断"，**比现状更糟**。
   *
   * 故多文件路径必须同时满足三件事：
   *   1. **不跳转**（`noRedirect`）—— 留在上传页看任务列表；
   *   2. **串行**提交 —— 共享的单例进度条才有意义，也避免一次打满后端并发配额；
   *   3. 全部结束后给**一条汇总**，逐条进度由任务列表（SSE 推送）承载。
   *
   * 单文件仍走原路径（保留"上传完自动跳转复核页"的既有体验）。
   */
  function submitFiles(fileList) {
    const list = Array.from(fileList || []);
    if (list.length === 0) return;
    // 新一轮选择 = 新一轮额度博弈：解除上一批留下的"等待超时"熔断
    // （一次超时不该永久锁死上传；额度是动态的）。见 quotaWaitTimedOut。
    quotaWaitTimedOut = false;
    if (list.length === 1) {
      uploadFile(list[0]);
      return;
    }
    // 未配置 LLM：只弹**一次**对话框。逐个 uploadFile 会让每个文件各弹一次
    // （N 个叠加对话框），且第一份之后的都排在后面 —— 用户会以为页面卡死。
    if (ctx.needs_setup) {
      log.warn("submitFiles — blocked (needs_setup)");
      confirmDialog({
        title: "未配置 LLM 服务商",
        message:
          "上传后无法进行结构化分析，请先前往「设置」完成 API Key 配置。",
        confirmText: "前往设置",
        cancelText: "取消",
      }).then((ok) => {
        if (ok) window.location.href = "/settings";
      });
      return;
    }
    // 先统一预检：不合格的**一次性**报出来。逐个报会被后来的 setStatus 覆盖，
    // 用户只看到最后一条 —— 等于没报（正是本次要消灭的"静默"形态）。
    const ok = [];
    const bad = [];
    for (const f of list) {
      const reason = rejectReason(f);
      if (reason) bad.push(`${f.name}（${reason}）`);
      else ok.push(f);
    }
    if (ok.length === 0) {
      log.warn("submitFiles — all rejected", { bad: bad.length });
      setStatus(`已跳过 ${bad.length} 份不合格文件：${bad.join("；")}`, "err");
      return;
    }
    log("submitFiles — submitting", { accepted: ok.length, rejected: bad.length });
    // ⚠️ 被跳过的文件名**必须进最终汇总**：逐份状态会立刻覆盖这里设的提示
    // （`uploadFile` 的"上传中 x.pdf…"），只在这里报等于没报 —— 用户永远看不到
    // 哪几份被跳过了（本文件首版即踩此坑，被
    // `test_all_rejected_names_appear_in_one_message` 抓住）。
    const skippedNote = bad.length
      ? `（已跳过 ${bad.length} 份不合格：${bad.join("；")}）`
      : "";
    setStatus(
      bad.length
        ? `已跳过 ${bad.length} 份不合格文件；正在上传其余 ${ok.length} 份…`
        : `正在上传 ${ok.length} 份文件…`,
      bad.length ? "err" : "info",
    );
    let done = 0;
    let failed = 0;
    const next = () => {
      if (done >= ok.length) {
        // 汇总。逐条进度由下方任务列表（SSE 推送）承载 —— 单例状态行说不了 N 条。
        setStatus(
          (failed
            ? `已提交 ${ok.length - failed}/${ok.length} 份，${failed} 份失败（详见上方任务列表）`
            : `已提交 ${ok.length} 份文件，下方任务列表逐条显示分析进度`) +
            skippedNote,
          failed || bad.length ? "err" : "ok",
        );
        // 上传区在 uploadFile 里被禁用（防重复提交），全部结束后必须恢复
        if (area) area.style.pointerEvents = "";
        return;
      }
      const f = ok[done];
      uploadFile(f, false, {
        noRedirect: true,
        progress: { index: done + 1, total: ok.length },
        onDone: (success) => {
          if (!success) failed += 1;
          done += 1;
          next();
        },
      });
    };
    next();
  }

  /** 单文件上传。
   *
   * `opts`（可选，多文件路径用）：
   *   · `noRedirect` — 成功后**不**跳转复核页（跳走会杀掉其余上传）
   *   · `progress`   — `{index, total}`，用于"上传中 x.pdf（2/3）"这类标注
   *   · `onDone(ok)` — 上传**结算**回调（成功/失败各调一次，且**至多一次**），
   *                    供串行队列推进
   */
  function uploadFile(file, force = false, opts = {}) {
    const noRedirect = !!opts.noRedirect;
    const progress = opts.progress || null;
    const onDone = typeof opts.onDone === "function" ? opts.onDone : null;
    const label = progress ? `（${progress.index}/${progress.total}）` : "";
    // 结算**幂等**守卫：本函数有多条退出路径（校验拒绝 / 未配置 / 缺 job_id /
    // JSON 解析失败 / HTTP 错误 / 网络错误 / 409 取消），逐条手写 onDone 极易
    // 漏调（队列卡死）或重复调（队列跳跃）。统一走 settleOnce。
    let settled = false;
    const settleOnce = (success) => {
      if (settled) return;
      settled = true;
      if (onDone) onDone(success);
    };
    log("uploadFile() called", {
      name: file.name,
      size: file.size,
      sizeMB: (file.size / 1024 / 1024).toFixed(2),
      type: file.type,
      lastModified: new Date(file.lastModified).toISOString(),
      force,
      noRedirect,
    });
    // 校验走 rejectReason（单一真值，与多文件预检同源）
    const reason = rejectReason(file);
    if (reason) {
      log.warn("uploadFile — rejected", { name: file.name, reason });
      setStatus(reason, "err");
      settleOnce(false);
      return;
    }
    // 前端预检：未配置 LLM 时引导用户先去设置（与后端 400 拦截双保险）
    if (ctx.needs_setup) {
      log.warn("uploadFile — blocked (needs_setup)");
      confirmDialog({
        title: "未配置 LLM 服务商",
        message:
          "上传后无法进行结构化分析，请先前往「设置」完成 API Key 配置。",
        confirmText: "前往设置",
        cancelText: "取消",
      }).then((ok) => {
        if (ok) window.location.href = "/settings";
      });
      settleOnce(false);
      return;
    }
    const mb = (file.size / 1024 / 1024).toFixed(1);

    // 禁用上传区域，防止重复提交（**必须在额度预检之前** —— 等待期间
    // 用户不该还能再拖一批进来，否则等待队列会叠加）
    const dropZone = document.getElementById("upload-area");
    if (dropZone) dropZone.style.pointerEvents = "none";

    /** 放开上传区。多文件路径下**不**放开 —— 由 `submitFiles` 在整批结束后
     *  统一恢复；否则中途某个文件失败会短暂放开拖拽，用户可以插进新批次，
     *  与正在串行的队列交错（任务行顺序与用户预期不符）。
     *
     *  ⚠️ 必须定义在**额度预检之前**：预检的提前 return 会让后面的语句
     *  永不执行，而 `const` 的 TDZ 会让"提前调用"直接抛 ReferenceError
     *  （不是"没生效"，是整条路径崩掉）。 */
    const releaseDropZone = () => {
      if (dropZone && !noRedirect) dropZone.style.pointerEvents = "";
    };

    // ── 并发额度预检（**发送之前**）──────────────────────────────────
    // 放在 uploadFile 里而不是 submitFiles 里：单文件路径（submitFiles 对
    // 1 份**直接调 uploadFile**）与多文件路径必须同一套语义，否则"拖 1 份"
    // 与"拖 2 份"行为不一致 —— 前者仍会把整个文件白传一遍再吃 409。
    // 理由与 fail-open 约束见文件头 quotaSnapshot 注释。
    const quota = quotaSnapshot();
    if (quota && quota.atCapacity) {
      // ⚠️ 熔断（"本批已等超时过"）**只由 `waitForQuotaSlot` 判定** ——
      // 它既是置位者也是唯一的判定点，故此处**刻意不**再写一遍。
      // 同一语义两处实现有两个害处：① 两份真值，将来改一处会漂移；
      // ② 其中一处永远不可达（本仓库的变异验证会把这种冗余判为 MISSED）。
      // 见 MEMORY 的"单一真值 / 单一实现"约定。
      setStatus(
        `已达并发上限（${quota.active}/${quota.limit}），${file.name}${label}` +
          `等待空位后自动继续…`,
        "info",
      );
      log("uploadFile — at capacity, waiting for a slot", {
        active: quota.active,
        limit: quota.limit,
      });
      waitForQuotaSlot()
        .then((got) => {
          if (!got) {
            setStatus(
              `已达并发上限（${quota.active}/${quota.limit}），等待超时 — ` +
                `请等待任务完成或取消后再试。`,
              "err",
            );
            releaseDropZone();
            settleOnce(false);
            return;
          }
          // opts 必须透传：否则重跑这一次不再结算（onDone 不触发）⇒ 队列卡死
          uploadFile(file, force, opts);
        })
        .catch((err) => {
          // 等待器本身不该抛；真抛了也必须把流程推进下去，绝不永久卡住
          log.err("uploadFile — quota wait failed, sending anyway", err);
          uploadFile(file, force, opts);
        });
      return;
    }

    setStatus(`上传中 ${file.name}${label}（${mb} MB）…`, "info");
    const fd = new FormData();
    fd.append("file", file);
    log("uploadFile — XHR POST /api/jobs", {
      formDataEntries: [...fd.entries()].map(([k, v]) => [
        k,
        v instanceof File ? v.name : v,
      ]),
    });

    // 使用 XHR 替代 fetch，以获取 upload progress 事件
    const progressBar = document.getElementById("upload-progress");
    const progressText = document.getElementById("upload-progress-text");
    const progressPct = document.getElementById("upload-progress-pct");
    const progressFill = document.getElementById("upload-progress-fill");
    if (progressBar) {
      progressBar.classList.remove("hidden");
      if (progressFill) progressFill.style.width = "0%";
      if (progressPct) progressPct.textContent = "0%";
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/jobs${force ? "?force=1" : ""}`);
    // 大文件上传超时保护：限额上限 × 慢速网络 ≈ 120s 超时
    xhr.timeout = 120000;

    // 上传进度（大文件反馈关键）
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable && progressBar) {
        const pct = Math.round((e.loaded / e.total) * 100);
        if (progressFill) progressFill.style.width = pct + "%";
        if (progressPct) progressPct.textContent = pct + "%";
        if (progressText) {
          const loadedMB = (e.loaded / 1024 / 1024).toFixed(1);
          const totalMB = (e.total / 1024 / 1024).toFixed(1);
          progressText.textContent = `上传中 ${loadedMB}/${totalMB} MB`;
        }
        log("upload progress", { pct, loaded: e.loaded, total: e.total });
      }
    });

    xhr.onload = () => {
      log("uploadFile — response", {
        status: xhr.status,
        ok: xhr.status >= 200 && xhr.status < 300,
      });
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(xhr.responseText);
          log("uploadFile — success response", data);
          if (data.job_id) {
            if (progressBar) progressBar.classList.add("hidden");
            // 大文件软告警（后端 page_warning）：如实告知预估耗时。存在告警时
            // 延长跳转延迟 —— 1.5s 读不完一句耗时提示，等于没有告知。
            let warning = "";
            let delayMs = 1500;
            if (data.page_warning) {
              log.warn("uploadFile — page_warning", data.page_warning);
              warning = `（${data.page_warning}）`;
              delayMs = 5000;
            }
            if (noRedirect) {
              // 多文件：**不跳转** —— 跳走会杀掉其余上传（见 submitFiles 注释）。
              // 逐条进度由任务列表（SSE 推送）承载，这里只报"这一份已创建"。
              setStatus(`任务已创建 ${data.job_id}${warning}`, "ok");
            } else {
              setStatus(
                `任务已创建 ${data.job_id}${warning}，${delayMs / 1000}s 后跳转复核页…`,
                "ok",
              );
              const target = `/jobs/${data.job_id}/review`;
              log("uploadFile — scheduling redirect", { target, delayMs });
              setTimeout(() => {
                log("uploadFile — redirecting now", target);
                window.location.href = target;
              }, delayMs);
            }
            settleOnce(true);
          } else {
            log.err("uploadFile — response missing job_id", data);
            setStatus(`上传失败: ${JSON.stringify(data)}`, "err");
            if (progressBar) progressBar.classList.add("hidden");
            releaseDropZone();
            settleOnce(false);
          }
        } catch (err) {
          log.err("uploadFile — JSON parse failed", err);
          setStatus(`解析响应失败: ${err}`, "err");
          if (progressBar) progressBar.classList.add("hidden");
          releaseDropZone();
          settleOnce(false);
        }
      } else {
        log.err("uploadFile — HTTP error", xhr.status, xhr.responseText);
        // robustness-C7: 展示后端返回的 detail（如"未配置 LLM/OCR"等中文
        // 友好提示），此前仅显示状态码，用户无法得知具体原因。
        let detail = "";
        try {
          const body = JSON.parse(xhr.responseText || "{}");
          if (body && body.detail) detail = `: ${String(body.detail)}`;
        } catch (_) { /* 非 JSON 响应（网关/代理错误），忽略 */ }
        // 409 去重命中时（非 force 重试），提供"仍要重新上传"路径 —
        // 无需删除旧任务（删除不可恢复，连审计日志一起），直接 force 重传。
        // 旧任务保留在历史记录中，便于对比复核。
        if (xhr.status === 409 && !force && /已上传过/.test(detail)) {
          const match = detail.match(/任务 (\S+?)「(.+?)」，状态 ([^）\s]+)/);
          const jobId = match ? match[1] : "";
          const oldName = match ? match[2] : file.name;
          const oldStatus = match ? match[3] : "";
          confirmDialog({
            title: "该文件已上传过",
            message:
              `历史任务「${oldName}」（状态: ${oldStatus}）包含此文件。\n\n` +
              "重新上传会创建新任务并重新完整分析（旧任务保留，可对比）。",
            confirmText: "仍要重新上传",
            cancelText: "取消",
            statusBadge: { text: oldStatus, dotClass: statusDotClass(oldStatus) },
          }).then((ok) => {
            if (!ok) {
              setStatus("已取消 — 可在历史记录中查看原任务", "info");
              if (progressBar) progressBar.classList.add("hidden");
              releaseDropZone();
              settleOnce(false);
              return;
            }
            log("uploadFile — force re-upload confirmed", { jobId });
            // opts 必须透传：否则重传这一份不再结算（onDone 不触发）⇒ 串行队列
            // 永久卡住。settleOnce 守卫保证只结算一次（外层这次不算）。
            uploadFile(file, true, opts);
          });
          return;
        }
        // 并发额度 409 —— **不是"上传失败"**，是"此刻没有空位"。
        //
        // 旧行为把它落进下面的通用分支：`err` 级红色提示"上传失败: HTTP 409:
        // 已有 3 个任务在处理中…"，同时 `settleOnce(false)` + `releaseDropZone()`
        // ⇒ 用户的文件选择被丢弃，必须**重新选**并**重传整个大文件**。
        // 三个问题叠在一起：(1) 语义错（把容量不足说成失败）；
        // (2) 用户要重做一遍体力活；(3) 明明只要等一会儿就能过。
        //
        // 现在：info 级提示 + 等空位 + 有界自动重试。仍不结算外层 ——
        // 重试那次会带着同一个 opts 走完整流程并在结束时结算（与去重分支
        // 同一套"透传 opts + settleOnce 守卫"的约定，见上）。
        if (xhr.status === 409 && isQuotaRejection(detail)) {
          const attempt = (opts && opts.quotaAttempt) || 0;
          if (attempt >= QUOTA_RETRY_MAX) {
            setStatus(
              `并发已达上限，重试 ${attempt} 次仍未取到空位 —${detail}`,
              "err",
            );
            if (progressBar) progressBar.classList.add("hidden");
            releaseDropZone();
            settleOnce(false);
            return;
          }
          const nextAttempt = attempt + 1;
          setStatus(
            `并发已达上限，等待空位后自动重试（第 ${nextAttempt}/` +
              `${QUOTA_RETRY_MAX} 次）…`,
            "info",
          );
          if (progressBar) progressBar.classList.add("hidden");
          log("uploadFile — quota 409, waiting for a slot", {
            attempt: nextAttempt,
          });
          // opts 必须透传（否则重传这一份不再结算 ⇒ 串行队列永久卡住）
          const retryOpts = Object.assign({}, opts, {
            quotaAttempt: nextAttempt,
          });
          waitForQuotaSlot()
            .then((got) => {
              if (!got) {
                setStatus(`并发已达上限，等待超时 —${detail}`, "err");
                releaseDropZone();
                settleOnce(false);
                return;
              }
              uploadFile(file, force, retryOpts);
            })
            .catch((err) => {
              log.err("uploadFile — quota wait failed", err);
              setStatus(`并发已达上限，等待失败 —${detail}`, "err");
              releaseDropZone();
              settleOnce(false);
            });
          return;
        }
        setStatus(`上传失败: HTTP ${xhr.status}${detail}`, "err");
        if (progressBar) progressBar.classList.add("hidden");
        releaseDropZone();
        settleOnce(false);
      }
    };

    xhr.onerror = () => {
      log.err("uploadFile — XHR network error");
      setStatus(`网络错误: 上传失败`, "err");
      if (progressBar) progressBar.classList.add("hidden");
      releaseDropZone();
      settleOnce(false);
    };

    xhr.ontimeout = () => {
      log.err("uploadFile — XHR timeout (120s)");
      setStatus(`上传超时: 文件过大或网络不稳定，请重试`, "err");
      if (progressBar) progressBar.classList.add("hidden");
      releaseDropZone();
      settleOnce(false);
    };

    xhr.send(fd);
  }

  // === Toast + 确认弹窗（共享实现 confirm-dialog.js）===
  // P1-5: 薄别名（页面 API 名不变，调用点零改动）。函数声明（非 const）
  // 避免 TDZ，顶层/回调皆安全。
  //
  // P1-9: `window.PBC.*` 的**可用性**由 `pbc-fallback.js` 统一兜底（只补缺失
  // 方法、与加载顺序无关，见其头部注释），故此处不必再判存在 —— 降级语义
  // 只写一处，避免多份副本漂移。
  function showToast(msg, type) {
    return window.PBC.showToast(msg, type);
  }
  function confirmDialog(opts) {
    return window.PBC.confirmDialog(opts);
  }

  // 409 去重弹窗的状态徽章色（共享件 status.js，PbcStatus 缺失时降级）
  function statusDotClass(st) {
    return window.PbcStatus
      ? window.PbcStatus.statusDotClass(st)
      : "bg-muted-foreground/40";
  }

  // 捕获全局错误
  window.addEventListener("error", (e) => {
    log.err("window.error", {
      message: e.message,
      filename: e.filename,
      lineno: e.lineno,
      colno: e.colno,
      error: e.error,
    });
  });
  window.addEventListener("unhandledrejection", (e) => {
    log.err("unhandledrejection", e.reason);
  });
})();
