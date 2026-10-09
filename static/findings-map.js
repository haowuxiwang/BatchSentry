/* Finding 级中文映射单一真值（severity / source / type / 复核状态）。
 *
 * 由来（R62 审计 P2）：这些映射此前内建在 review.js 的 renderFindings 里
 * （约 L1335-1421 四份局部字面量），同时 templates/review.html 头部另有
 * 一份 Jinja2 set 副本（SSR 首屏用）—— **双轨各自为政**：新增一个 finding
 * type 时必须记得改两处，漏掉任何一处就会出现"首屏英文、翻页后变中文"
 * （SSR 与 AJAX 渲染不同源）。历史上 status.js 已把 job 状态收敛为单一
 * 真值并配机检（tests/unit/test_status_js.py），本文件对 finding 级映射
 * 做同样的事。
 *
 * 三方一致性由 tests/unit/test_findings_map_js.py 机检锁定：
 *   1. 本文件 ↔ templates/review.html 的 Jinja2 set **逐值一致**（措辞
 *      漂移 = 首屏与翻页后文案不一致）；
 *   2. 本文件键集 ⊇ core/zh_map.py 的 FINDING_TYPE_ZH / SEVERITY_ZH /
 *      FINDING_STATUS_ZH（后端有的前端必须能显示）——**只锁键集不锁
 *      措辞**：后端措辞供 report/notify 使用，与前端确有历史差异（如
 *      param_out_of_spec「参数越界」vs「参数超标」），统一措辞会改变
 *      用户可见文案，须单独评估，不在本护栏范围（与 test_status_js.py
 *      对 JOB_STATUS_ZH 的既有取向一致）。
 *
 * 用法：复核页各模块（review-findings.js 等）与 SSR 模板共用。
 * 纯函数、无 DOM 依赖；node 单测直接 require 即可。
 */
(function (global) {
  "use strict";

  /* 严重度（findings.severity）— 与 review.html severity_zh 逐值一致 */
  var SEVERITY_ZH = {
    critical: "严重",
    warning: "警告",
    info: "信息",
  };

  /* 发现来源（findings.source）— 与 review.html source_zh 逐值一致。
   * 后端 zh_map.py 无对应表（report 侧直接输出原始枚举），前端是唯一持有方。 */
  var SOURCE_ZH = {
    rule: "规则",
    llm_page: "LLM单页",
    llm_fallback: "LLM兜底",
    llm_cross: "LLM跨页",
    user_rule: "用户规则",
  };

  /* Finding 复核状态（findings.status）— 与 review.html status_zh 逐值一致。
   * ⚠️ 与 job 状态（status.js 的 STATUS_ZH）是**两套枚举**：finding 的
   * pending 是「待复核」，job 的 pending 是「待处理」—— 键名重叠但语义
   * 不同，不可合并（合并后 pending 只能有一个中文值）。 */
  var FINDING_STATUS_ZH = {
    pending: "待复核",
    confirmed: "已确认",
    rejected: "已拒绝",
    corrected: "已修正",
  };

  /* Finding 类型（findings.type）— 与 review.html type_zh 逐值一致。
   * 键集是 zh_map.py FINDING_TYPE_ZH 的超集：low_confidence / time_anomaly
   * 是 LLM 自由输出的枚举（后端规范类型表未收录），前端必须能兜底显示，
   * 否则会走"未知(x)"分支让用户误以为数据坏了。 */
  var TYPE_ZH = {
    time_reversal: "时间倒序",
    year_contradiction: "年份矛盾",
    suspicious_date: "日期可疑",
    signature_time_anomaly: "签名时间异常",
    completeness: "信息缺失",
    batch_inconsistency: "批号不一致",
    param_out_of_spec: "参数超标",
    low_confidence: "低置信度",
    handwritten: "手写内容需核对",
    signature_mismatch: "签名不符",
    user_rule: "用户规则",
    ocr_noise: "OCR 噪音",
    time_anomaly: "时间异常",
    step_gap: "工序缺号",
    spec_unverifiable: "规格无法核定",
    uncategorized: "未分类",
    mass_balance: "物料平衡/收率",
    self_review: "自检自核",
    equipment_state: "设备/清洁状态",
    env_monitor: "环境监测",
    doc_version: "文件版本",
    deviation_link: "偏差关联",
    alteration: "涂改规范",
  };

  /* 页面置信度（structured.overall_confidence）— 与 review.html
   * confidence_zh 逐值一致（页面级徽章用）。 */
  var CONFIDENCE_ZH = {
    high: "高",
    medium: "中",
    low: "低",
  };

  /* 未知枚举中文兜底（原 review.js 的 zhOrUnknown，从渲染函数中提出）：
   * 后端/LLM 新枚举不更新映射表时，显示「未知(x)」而非裸英文 —— 用户至少
   * 能看出"这是个我没见过的值"，而不是以为界面坏了。 */
  function zhOrUnknown(map, key) {
    return map[key] || (key ? "未知(" + key + ")" : "");
  }

  /* HTML 转义（原 review.js renderFindings / renderSuppressions 内的 esc）：
   * f.type / f.description / f.ocr_text / f.gmp_basis / kb_refs.label 均来自
   * LLM 或规则输出，属于不可信文本 —— 注入 HTML 前必须转义。
   * 此前在 review.js 内有两个局部副本（renderFindings / renderSuppressions
   * 各一份），收敛到这里；历史背景见缺陷 #139（esc 曾有 6 份副本）。
   * ⚠️ 这**不是全仓唯一**副本：历史缺陷 #139 从 6 份收敛到 **3 份**，剩下的
   * 两份在 `settings-state.js` / `upload-jobs.js` —— 因为 settings 页与 upload 页
   * **不**加载本文件，仓里没有公共基座脚本（**结构性**原因，非疏忽）。
   * 三份的**行为等价**由机检锁定（`tests/unit/test_esc_single_source.py`：逐项按序
   * 比对替换链 + 副本集合登记 + 解码器镜像）；跨 bundle 的真单一真值需新增公共
   * 脚本并改 3 个模板，登记于 `docs/TODO.md` 的 **0-25**。 */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function severityZh(key) {
    return zhOrUnknown(SEVERITY_ZH, key);
  }
  function sourceZh(key) {
    return zhOrUnknown(SOURCE_ZH, key);
  }
  function typeZh(key) {
    return zhOrUnknown(TYPE_ZH, key);
  }
  function findingStatusZh(key) {
    return zhOrUnknown(FINDING_STATUS_ZH, key);
  }
  function confidenceZh(key) {
    return zhOrUnknown(CONFIDENCE_ZH, key);
  }

  global.PbcFindingsMap = {
    SEVERITY_ZH: SEVERITY_ZH,
    SOURCE_ZH: SOURCE_ZH,
    TYPE_ZH: TYPE_ZH,
    FINDING_STATUS_ZH: FINDING_STATUS_ZH,
    CONFIDENCE_ZH: CONFIDENCE_ZH,
    zhOrUnknown: zhOrUnknown,
    esc: esc,
    severityZh: severityZh,
    sourceZh: sourceZh,
    typeZh: typeZh,
    findingStatusZh: findingStatusZh,
    confidenceZh: confidenceZh,
  };
})(typeof window !== "undefined" ? window : globalThis);
