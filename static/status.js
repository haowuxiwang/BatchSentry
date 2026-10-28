/* Job 状态展示（状态点颜色 / 中文名）— upload.js / review.js / SSR 模板共享。
 *
 * 背景（缺陷 #133，P0）：`templates/review.html` 的状态点把 `bg-success`
 * **写死在模板里**，而 `review.js` 的 SSE 更新只改 `#status-badge` 的
 * `textContent`、**从不改这个点的 class**（`statusDotClass` 当时只存在于
 * `upload.js`）。于是 `status=error` / `partial_review` 的复核页显示的是
 * **"绿点 + 出错"** —— 与 #127 同一类回归：upload 页修了、复核页没修。
 *
 * 为什么必须抽成共享件而不是在模板里再写一份 if：
 * 状态→颜色/中文的映射一旦有第二份副本就会漂移。本项目已经因为"两处各判一次"
 * 而漏掉过复核页（本文件的由来），且此前 `esc()` 已有 6 份副本、状态→中文已有
 * 4 份副本（缺陷 #139）。**单一真值**是这里唯一可接受的形态。
 *
 * 这三个函数是纯的（无 DOM 依赖），SSR 之外的任何调用方都可直接复用；
 * `tests/unit/test_status_js.py` 用 node 锁定行为，并**机检模板里不得再出现
 * 无条件的 bg-success**（防止有人把硬编码改回来）。
 *
 * ⚠️ 增删状态枚举时必须同步三处，缺一即漂移：
 *   1. 本文件 STATUS_ZH / statusDotClass
 *   2. `core/zh_map.py`（后端 SSE/报表用）
 *   3. 本文件尾部的机检用例（tests/unit/test_status_js.py）
 */
(function (global) {
  "use strict";

  /* 状态 → 中文。未知状态兜底显示 `未知(x)` 而非裸英文（后端新增枚举
   * 而前端没跟上时，用户至少能看出"这是个我没见过的状态"，而不是以为
   * 界面坏了）。 */
  var STATUS_ZH = {
    pending: "待处理",
    confirmed: "已确认",
    rejected: "已拒绝",
    corrected: "已修正",
    queued: "排队中",
    processing: "处理中",
    review: "可复核",
    partial_review: "部分可复核",
    error: "出错",
    cancelled: "已取消",
    cancelling: "取消中",
    ocr_running: "识别中",
    ocr_done: "识别完成",
    analyzing: "分析中",
    done: "已完成",
    archived: "已归档",
  };

  function statusZh(st) {
    return STATUS_ZH[st] || (st ? "未知(" + st + ")" : "");
  }

  /* 状态 → 状态点颜色类。
   *
   * 关键判断（不可退化成"只有 review/done 是绿的"之外的其他简化）：
   * `partial_review` 的**定义**就是"存在失败页或双后端差异"（见
   * `core/pipeline/stage3.py` 的 final_status 判据）—— **它永远不是成功态**。
   * 此前与 review 同归 bg-success，使"0 条 finding 是因为压根没分析成功"
   * 与"记录确实无异常"在界面上长得一模一样，是 GMP 场景下最危险的一种
   * 假阴性（缺陷 #127 / #133）。故这里的映射按"用户会不会误读为成功"排序，
   * 而不是按"状态看起来像不像完成了"。
   */
  function statusDotClass(st) {
    if (["review", "done"].includes(st)) return "bg-success";
    if (st === "partial_review") return "bg-warning";
    if (st === "error") return "bg-destructive";
    if (["cancelled", "cancelling", "archived"].includes(st))
      return "bg-muted-foreground/40";
    return "bg-info";
  }

  /* 该状态是否属于"运行中"（与后端 `_ACTIVE_STATUSES` 对齐）。
   * 前端据此禁用删除，防孤儿 pipeline task。 */
  var ACTIVE_STATUSES = [
    "pending",
    "ocr_running",
    "ocr_done",
    "analyzing",
    "cancelling",
  ];

  function isActiveStatus(st) {
    return ACTIVE_STATUSES.indexOf(st) !== -1;
  }

  /* SSE `type: "error"` 帧的处置判定 —— 「要不要关流」的**单一真值**。
   *
   * 服务端（`api/jobs/status.py` 单任务流）会发两类语义完全不同的 error 帧：
   *   - `terminal: false`「进度查询失败」= 瞬态 DB 抖动，服务端发完 `continue`
   *     继续推帧 ⇒ 前端必须**保持**长连。关流会永久丢失实时更新；
   *   - `terminal: true`「任务不存在」= 终态，服务端发完 `return` ⇒ 前端关流，
   *     否则 EventSource 会按 SSE 语义无限重连一个已不存在的 job。
   *
   * 历史缺陷（B2-10 ①）：复核页只判 `d.type === "error"` 就把两类混为一谈，
   * 于是**一次 DB 抖动被谎报成"任务不存在或已被删除"**，同时进度流永久断开。
   *
   * ⚠️ 判据只认 `terminal` 字段，**不得**按 `message` 文本判断：文案属展示层，
   * 改文案会静默改变控制流（且会让"理由说谎"类缺陷重新出现）。
   * 字段缺失时按"瞬态"处理 —— fail-safe 方向取"宁可多留一会儿连接，也不把
   * 可恢复的抖动谎报成任务被删"。服务端一侧由集成用例锁定两类帧必带该字段，
   * 故不会长期缺失（见 tests/integration/test_api_job_status_coverage.py）。
   */
  function sseErrorAction(d) {
    if (!d || d.type !== "error") return null;
    return d.terminal === true ? "terminal" : "transient";
  }

  global.PbcStatus = {
    STATUS_ZH: STATUS_ZH,
    statusZh: statusZh,
    statusDotClass: statusDotClass,
    ACTIVE_STATUSES: ACTIVE_STATUSES,
    isActiveStatus: isActiveStatus,
    sseErrorAction: sseErrorAction,
  };
})(typeof window !== "undefined" ? window : globalThis);
