/* ============================================================
   Review 页 — OCR 面板文本定位（拆分自 review.js，R63 P1-A）

   finding 定位（用户核心诉求第一步：减少复核查找时间）：
   点击 finding 卡片 → OCR 面板中高亮对应的原文片段并滚到可视区。
   零后端改动：f.ocr_text（LLM 摘录）在 htmlToText 后的面板文本中
   做 token 匹配。LLM 摘录可能改写个别字 → 按 token 逐个降级查找。
   高亮用 TextRange + <mark> 包装（不 innerHTML 注入，无 XSS 面）。

   ⚠️ 命名史（R63 拆分时修复的缺陷）：本模块的定位函数原与区域锚定
   函数同名（review.js 里两个 `function locateFinding`，参数为
   (card) 与 (e, findingId)）。函数声明提升使**后定义的区域版遮蔽了
   本函数**，而 findings-list 的点击事件委托仍按"card 版"调用 ——
   card 被传入区域版后 `regionRefs[NaN]` 命中 undefined 直接 return。
   净效果：点击卡片高亮 OCR 原文的功能自引入以来**静默失效**。
   拆分时改名 locateInOcrPanel（区域版改名 locateRegion，见
   review-pageview.js），彻底消除同名遮蔽。
   ============================================================ */
(function (global) {
  "use strict";

  const R = global.PbcReview;
  const log = R.log;

  // OCRraw HTML → 可读文本：保留表格结构（行/单元格分隔），剥离标签与
  // MinerU 样式噪音（style= 属性、字面 "\n" 转义、img 长路径）。
  // 纯字符串处理 + textContent 赋值，无 innerHTML，无 XSS 面。
  function htmlToText(html) {
    if (!html) return "";
    return String(html)
      .replace(/\\n/g, "\n") // MinerU 表格单元格分隔的字面 \n
      .replace(/\\t/g, "\t")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<img[^>]*>/gi, "[图]")
      .replace(/<\/tr>/gi, "\n")
      .replace(/<\/t[dh]>/gi, " | ")
      .replace(/<\/p>/gi, "\n")
      .replace(/<div[^>]*>/gi, "\n")
      .replace(/<[^>]+>/g, "") // 剩余标签
      .replace(/&nbsp;/gi, " ")
      // ⚠️ `&amp;` 必须**最后**解码：若先于 `&lt;`/`&gt;`/`&quot;`/`&#39;`，
      // 则 `&amp;lt;`（字面文本 "&lt;"）会被二次解码成 "<" —— 单遍解码器
      // 不会这样，属顺序缺陷。实测：`&amp;lt;` 曾渲染为 `<`。
      .replace(/&lt;/gi, "<")
      .replace(/&gt;/gi, ">")
      .replace(/&quot;/gi, '"')
      .replace(/&#39;/gi, "'")
      .replace(/&amp;/gi, "&")
      .replace(/[ \t]+/g, " ") // 折叠行内空白
      .replace(/ *\| */g, " | ") // 统一单元格分隔符
      .replace(/[ \t]+\n/g, "\n") // 行尾空白
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function clearLocateMarks() {
    document.querySelectorAll("mark.finding-locate-mark").forEach((m) => {
      const parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
    });
  }

  function locateInOcrPanel(card) {
    const ocrText = card.dataset.ocr || "";
    const ocrEl = document.getElementById("ocr-text");
    if (!ocrEl || !ocrText) {
      log.warn("locateInOcrPanel — no target", { hasOcrText: !!ocrText });
      return;
    }
    clearLocateMarks();

    const text = ocrEl.textContent || "";
    if (!text) {
      window.PBC.showToast("OCR 面板无文本可定位", "info");
      return;
    }
    // 摘录 token（≥4 字符，避免 "的/是" 之类噪音 token 误定位）
    const tokens = String(ocrText)
      .split(/[\s|；;，,]+/)
      .filter((t) => t.length >= 4);
    let needle = null;
    let idx = -1;
    for (const t of tokens) {
      const i = text.indexOf(t);
      if (i >= 0) {
        needle = t;
        idx = i;
        break;
      }
    }
    if (!needle) {
      window.PBC.showToast(
        "OCR 文本中未找到对应内容（原文可能被折叠/改写），请直接核对 PDF 原图",
        "info",
      );
      return;
    }

    // 文本节点内定位（textContent 通常来自单文本节点；跨节点时跳 style 兜底）
    const walker = document.createTreeWalker(ocrEl, NodeFilter.SHOW_TEXT);
    let acc = 0;
    let node;
    while ((node = walker.nextNode())) {
      const nl = (node.textContent || "").length;
      if (idx < acc + nl) {
        try {
          const range = document.createRange();
          range.setStart(node, idx - acc);
          range.setEnd(node, idx - acc + needle.length);
          const mark = document.createElement("mark");
          mark.className = "finding-locate-mark";
          range.surroundContents(mark);
          mark.scrollIntoView({ behavior: "smooth", block: "center" });
        } catch (err) {
          // 跨文本节点边界等异常 → 无高亮滚动兜底
          log.warn("locateInOcrPanel — surroundContents failed", err);
          node.parentElement.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        break;
      }
      acc += nl;
    }
    log("locateInOcrPanel", { needle: needle.slice(0, 30), at: idx });
  }

  const locate = (global.PbcReview = global.PbcReview || {});
  locate.locate = {
    htmlToText: htmlToText,
    clearLocateMarks: clearLocateMarks,
    locateInOcrPanel: locateInOcrPanel,
  };
})(typeof window !== "undefined" ? window : globalThis);
