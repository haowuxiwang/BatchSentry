/* ============================================================
   Upload page — file upload + job archive/delete interactions
   依赖：window.__PBC__.jobs_count（可选，仅用于日志）
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
      const file = e.target.files[0];
      log("file-input change event", {
        hasFile: !!file,
        fileName: file?.name,
        fileSize: file?.size,
      });
      if (file) uploadFile(file);
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
      const file = e.dataTransfer.files[0];
      log("drop on upload-area", {
        hasFile: !!file,
        fileName: file?.name,
        fileSize: file?.size,
      });
      area.classList.remove("border-primary", "bg-accent/40");
      area.classList.add("border-border");
      if (file) uploadFile(file);
    });
  }

  function uploadFile(file) {
    log("uploadFile() called", {
      name: file.name,
      size: file.size,
      sizeMB: (file.size / 1024 / 1024).toFixed(2),
      type: file.type,
      lastModified: new Date(file.lastModified).toISOString(),
    });
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      log.warn("uploadFile — rejected (not .pdf)", file.name);
      setStatus("仅支持 PDF 文件", "err");
      return;
    }
    if (file.size > 200 * 1024 * 1024) {
      log.warn("uploadFile — rejected (too large)", {
        sizeMB: (file.size / 1024 / 1024).toFixed(1),
        limitMB: 200,
      });
      setStatus("文件超过 200MB 上限", "err");
      return;
    }
    const mb = (file.size / 1024 / 1024).toFixed(1);
    setStatus(`上传中 ${file.name} (${mb} MB)...`, "info");
    const fd = new FormData();
    fd.append("file", file);
    log("uploadFile — fetch POST /api/jobs", {
      formDataEntries: [...fd.entries()].map(([k, v]) => [
        k,
        v instanceof File ? v.name : v,
      ]),
    });
    fetch("/api/jobs", { method: "POST", body: fd })
      .then((r) => {
        log("uploadFile — response", { status: r.status, ok: r.ok });
        if (!r.ok) {
          return r.text().then((txt) => {
            log.err("uploadFile — HTTP error body", txt);
            throw new Error("HTTP " + r.status);
          });
        }
        return r.json();
      })
      .then((data) => {
        log("uploadFile — success response", data);
        if (data.job_id) {
          setStatus(`Job 已创建 ${data.job_id}，1.5s 后跳转复核页...`, "ok");
          const target = `/jobs/${data.job_id}/review`;
          log("uploadFile — scheduling redirect", { target, delayMs: 1500 });
          setTimeout(() => {
            log("uploadFile — redirecting now", target);
            window.location.href = target;
          }, 1500);
        } else {
          log.err("uploadFile — response missing job_id", data);
          setStatus(`上传失败: ${JSON.stringify(data)}`, "err");
        }
      })
      .catch((err) => {
        log.err("uploadFile — fetch failed", err);
        setStatus(`错误: ${err}`, "err");
      });
  }

  // === Job 归档/删除 ===
  async function archiveJob(jobId, filename) {
    if (
      !confirm(
        `归档 "${filename}"？\n\n归档后将从列表移除，但数据保留，可在归档列表查看。`,
      )
    )
      return;
    log("archiveJob", { jobId, filename });
    try {
      const r = await fetch(`/api/jobs/${jobId}/archive`, { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      log("archiveJob — success", data);
      setStatus(`已归档 ${filename}`, "ok");
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      log.err("archiveJob failed", err);
      setStatus(`归档失败: ${err}`, "err");
    }
  }

  async function deleteJob(jobId, filename) {
    const sure = confirm(
      `彻底删除 "${filename}"？\n\n⚠️ 此操作不可恢复，将删除：\n• PDF 原文件\n• 所有 OCR 数据\n• 所有 findings\n• 审计日志`,
    );
    if (!sure) return;
    log("deleteJob", { jobId, filename });
    try {
      const r = await fetch(`/api/jobs/${jobId}?keep_pdf=false`, {
        method: "DELETE",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      log("deleteJob — success", data);
      setStatus(`已删除 ${filename}`, "ok");
      setTimeout(() => location.reload(), 800);
    } catch (err) {
      log.err("deleteJob failed", err);
      setStatus(`删除失败: ${err}`, "err");
    }
  }

  // 暴露到全局（onclick 处理器需要）
  window.archiveJob = archiveJob;
  window.deleteJob = deleteJob;

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
