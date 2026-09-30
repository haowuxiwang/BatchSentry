/* ============================================================
   Review 页 — 抑制留痕面板（拆分自 review.js，R63 P1-A）

   P0-2 抑制留痕：被降噪规则抑制的候选条目（可查原因 / 可回退）。
   抑制 ≠ 删除。后端把每条被抑制的候选条目连同**理由 + 命中证据**落
   finding_suppressions 台账（EU GMP Annex 11 §16 / 中国附录《计算机化系统》
   第 15/16 条：关键数据修改须记录理由）。本面板是它在复核页的出口：
   可查、可抽检、可一键回退为正式问题。
   渲染只此一处（SSR 只放空容器），避免 SSR/AJAX 两套标记漂移。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;
  const esc = global.PbcFindingsMap.esc;

  function renderSuppressions(data, page) {
    const panel = document.getElementById("suppression-panel");
    const list = document.getElementById("suppression-list");
    const brief = document.getElementById("suppression-brief");
    if (!panel || !list) return;
    const entries = (data && data.entries) || [];
    const total = (data && data.total) || 0;
    if (brief) {
      brief.textContent = total
        ? `（第 ${page} 页 ${entries.length} 条 · 全批 ${total} 条）`
        : "";
    }
    if (!entries.length) {
      panel.classList.add("hidden");
      list.replaceChildren();
      return;
    }
    panel.classList.remove("hidden");
    list.innerHTML = entries
      .map((e) => {
        const matched = (e.evidence && e.evidence.matched) || [];
        const evText = matched
          .map(
            (m) =>
              `${m.name}：实测 ${m.actual || "—"} / 规格 ${m.spec || "—"} → ${m.state || "?"}`,
          )
          .join("；");
        const action = e.reverted
          ? `<span class="text-[11px] text-muted-foreground">· 已回退为正式问题</span>`
          : `<button onclick="revertSuppression(event, ${e.id})"
                     class="btn-press focus-ring text-[11px] font-medium text-foreground hover:text-muted-foreground">
                 回退为正式问题
               </button>`;
        return `<div class="border-b border-border last:border-b-0 py-2 px-1" id="suppression-${e.id}">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="text-[12px] font-medium text-foreground">${esc(e.type_zh || e.type || "")}</span>
            <span class="text-[11px] text-muted-foreground uppercase tracking-wider ml-auto">${esc(e.severity || "")}</span>
          </div>
          <p class="text-[12px] text-muted-foreground leading-relaxed break-words">${esc(e.description || "")}</p>
          <p class="text-[11px] text-muted-foreground mt-1 leading-relaxed">抑制理由：${esc(e.reason || "")}</p>
          ${evText ? `<p class="text-[11px] text-muted-foreground mt-0.5 font-mono break-words">命中证据：${esc(evText)}</p>` : ""}
          <div class="action-btns mt-1 flex items-center gap-2">${action}</div>
        </div>`;
      })
      .join("");
  }

  async function loadSuppressions(page) {
    const jobId = R.state.jobId;
    try {
      const r = await fetch(`/api/jobs/${jobId}/suppressions?page=${page}`);
      if (!r.ok) {
        log.warn("loadSuppressions — HTTP " + r.status);
        return;
      }
      const data = await r.json();
      if (page !== R.state.currentPage) return; // 代际守卫：期间已翻页则丢弃
      renderSuppressions(data, page);
    } catch (err) {
      log.warn("loadSuppressions failed", err);
    }
  }

  function revertSuppression(e, suppressionId) {
    const jobId = R.state.jobId;
    const btn = e && e.currentTarget ? e.currentTarget : null;
    R.setButtonLoading(btn, true);
    log("revertSuppression() called", { suppressionId });
    fetch(`/api/jobs/${jobId}/suppressions/${suppressionId}/revert`, {
      method: "POST",
    })
      .then((r) => {
        if (!r.ok) return r.text().then((t) => {
          log.err("revertSuppression — HTTP error body", t);
          throw new Error("HTTP " + r.status);
        });
        return r.json();
      })
      .then(() => {
        window.PBC.showToast("已回退为正式问题，可在问题清单中裁决", "ok");
        // 回退后该 finding 才会出现在问题清单 → 双面板一起刷新
        return R.refreshCurrentPageFindings().then(() =>
          loadSuppressions(R.state.currentPage));
      })
      .catch((err) => {
        log.err("revertSuppression failed", err);
        window.PBC.showToast("回退失败: " + err.message, "err");
        R.setButtonLoading(btn, false, "回退为正式问题");
      });
  }

  const sup = (global.PbcReview = global.PbcReview || {});
  sup.suppressions = {
    renderSuppressions: renderSuppressions,
    loadSuppressions: loadSuppressions,
    revertSuppression: revertSuppression,
  };

  // 暴露到全局（onclick 处理器需要）
  global.revertSuppression = revertSuppression;
})(typeof window !== "undefined" ? window : globalThis);
