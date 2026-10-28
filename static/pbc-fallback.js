/* ============================================================
   window.PBC 降级兜底（P1-9）

   背景：`confirm-dialog.js` 提供 `window.PBC.{showToast, confirmDialog,
   promptDialog, isDialogOpen}`，全站 8 个模块（review.js / review-findings.js /
   review-locate.js / review-pageinfo.js / review-pageview.js /
   review-progress.js / review-suppressions.js / settings.js /
   upload.js / upload-jobs.js）直接消费它。若该文件未能加载
   （网络 / 缓存 / CSP 异常、被扩展拦截），调用点会抛 TypeError；而这些
   调用多发生在 async 回调与事件处理器里，异常逃出 Promise 边界后
   **无人 catch** ⇒ 表现为"点了删除 / 取消 / 重试 / 归档没反应"：
   没有请求、没有报错、没有提示。用户无法区分"操作被拒"与"页面坏了"。

   做法：本文件**只补缺失的方法**，绝不覆盖已存在的实现。由此得到两个
   关键性质：

     1. **与 confirm-dialog.js 的加载顺序无关。** confirm-dialog.js 结尾是
        无条件赋值（`window.PBC.showToast = showToast`），所以无论谁先执行，
        最终生效的都是真实实现；只有它缺席时降级版才留下。
     2. **单一真值。** 降级语义只写在这里一处，不必在 10 个消费模块里
        各抄一份（那样必然漂移）。

   降级语义（两条不可退让的不变量）：
     1. **不可逆操作降级后仍必须被确认。** 连原生 `confirm` 都问不到时返回
        `false` —— 降级绝不能放行删除。
     2. **失败必须可见。** `err` 类提示走 `alert`；成功类降到 `console`
        （不丢数据，且页面另有视觉反馈，如行淡出 / 自动刷新）。

   残余风险（诚实说明）：本文件与 confirm-dialog.js **同时**加载失败时，
   TypeError 仍会回来。本文件无任何依赖、仅 30 余行，加载失败的概率远低于
   331 行的 confirm-dialog.js；再往下就只能内联脚本了，而 CSP 只放行
   `window.__PBC__` 注入点。
   ============================================================ */
(function () {
  "use strict";

  const PBC = (window.PBC = window.PBC || {});

  if (typeof PBC.showToast !== "function") {
    PBC.showToast = function (msg, type) {
      console.warn(
        "[PBC] showToast 降级（confirm-dialog.js 未加载）:",
        type,
        msg,
      );
      if (type === "err") window.alert(msg);
    };
  }

  if (typeof PBC.confirmDialog !== "function") {
    PBC.confirmDialog = function (opts) {
      const o = opts || {};
      console.warn("[PBC] confirmDialog 降级（confirm-dialog.js 未加载）:", o);
      // 问不了就不做 —— 降级绝不能放行删除 / 取消这类不可逆操作。
      if (typeof window.confirm !== "function") return Promise.resolve(false);
      return Promise.resolve(
        window.confirm([o.title, o.message].filter(Boolean).join("\n\n")),
      );
    };
  }

  if (typeof PBC.promptDialog !== "function") {
    PBC.promptDialog = function (opts) {
      const o = opts || {};
      console.warn("[PBC] promptDialog 降级（confirm-dialog.js 未加载）:", o);
      if (typeof window.prompt !== "function") return Promise.resolve(null);
      const v = window.prompt(
        [o.title, o.message].filter(Boolean).join("\n\n"),
        o.defaultValue || "",
      );
      // 契约与真实实现一致：取消返回 null，确认返回字符串（**空串也算确认**）
      return Promise.resolve(v === null ? null : String(v));
    };
  }

  if (typeof PBC.isDialogOpen !== "function") {
    // confirm-dialog.js 缺席 ⇒ 本页不可能有弹窗 ⇒ 恒 false。
    // 语义与真实实现一致：真实实现查 DOM 里有没有 `role="dialog"`。
    PBC.isDialogOpen = function () {
      return false;
    };
  }
})();
