# 对抗性代码审查报告 — BatchSentry

**日期**：2026-09-28（收尾与复跑 2026-09-29）
**范围**：全仓库（前端 `static/*.js` 17 文件 6622 行；后端 `core/ api/ llm/ db/` 23649 行；测试 52142 行）
**方法**：逐文件通读 + 定点验证。每项问题按 **定位 → 修复 → 测试** 处理；新增测试一律
用 node **实跑真实函数**（非字符串匹配），并对关键断言做**变异验证**（把 bug 改回去，
确认护栏变红）。
**环境**：Python 3.11 / pytest 8.3.5 / node v22.22.2
**说明**：本报告所有计数均为**实跑取数**；临时脚本已被清理的批次一律**重跑复核**，
不沿用手写数字（见 §2 维度 1 末尾）。

---

## 1. 已修复问题（11 项，均附测试）

### FIX-1 · P0 · 字段契约护栏整体失效（`NameError`，静默零收集）

**定位**：`tests/integration/test_frontend_field_contract.py:95`

**问题**：R63 拆分把 `UPLOAD_JS = REPO/"static"/"upload.js"` 常量删掉、改为
`UPLOAD_JS_FILES` 文件集，却漏改第 95 行 `_JS = UPLOAD_JS.read_text(...)`。
模块在 **import 期** 即抛 `NameError` ⇒ **整个文件 0 条收集**：

```
ERROR tests/integration/test_frontend_field_contract.py - NameError: name 'UPLOAD_JS' is not defined
no tests collected, 1 error
```

丢失 15 条用例，包括 **#171 的 `window.__PBC__` 桥接契约**（消费集 ⊆ 注入集）
与 `upload-jobs.js` 全部字段/类型契约。这正是该文件 docstring 亲口警告的
"**空护栏比没有护栏更危险**"——它让人以为这里查过了。

**修复**：`_JS = _upload_js_sources()`（走 `upload*.js` 聚合源码，与派生逻辑一致）。

**测试**：`15 passed`（修复前 0 收集）。契约本身仍然成立，未暴露新的字段漂移。

---

### FIX-2 · P0 · 翻页空清单文案使用"上一页"的标记（GMP 假阴性）

**定位**：`static/review.js` → `loadPageData()` / `refreshCurrentPageFindings()`

**问题**：`renderFindings([])` 经 `emptyFindingsNote()` **读** `state.currentPageFlags`
区分三种"清单为空"（分析失败 / OCR 空页 / 确实无问题）；该标记的**唯一写点**是
`review-pageinfo.updatePageLevelUI`。原顺序是**先渲染、后写标记**：

```
renderFindings(...)          // ← 读 currentPageFlags（此时还是上一页的）
await loadSuppressions(...)
updatePageLevelUI(...)       // ← 才写 currentPageFlags
```

后果（翻页时）：

| 上一页 | 本页 | 界面显示 | 性质 |
|---|---|---|---|
| 无异常 | `_parse_error` 且 0 条 finding | **"本页无问题"** | **GMP 假阴性**（最危险） |
| `_parse_error` | 无异常且 0 条 finding | "未能分析…" | 假阳性（干扰复核） |

即 #136 要消灭的形态在**翻页路径**上重新出现（首屏路径由 `applyInitialPageLevelUI`
兜住，AJAX 路径漏了）。

**修复**：两个函数内把 `R.pageinfo.updatePageLevelUI(...)` 的**调用点前移**到
`R.findings.renderFindings(...)` 之前；`refreshCurrentPageFindings` 内把
measurements 请求一并提前。两处均加注释说明"写点必须先于读点"的不变式。

**测试**：新增 `tests/unit/test_review_empty_note_js.py`（11 条）

* 行为：`emptyFindingsNote()` 四种标记组合的 node 实跑断言；clean 分支**必须**恰为
  "本页无问题"且**不得**含失败/空页措辞；两标记同真取解析失败；`id` 稳定。
* 顺序：花括号配平提取 `loadPageData` / `refreshCurrentPageFindings` 函数体，
  断言 `R.pageinfo.updatePageLevelUI(` 的**调用点**先于 `R.findings.renderFindings(`。
  ⚠️ 断言锚定**调用点**而非标识符——函数内注释也会出现这两个名字，锚错即假绿
  （本文件第一版即踩此坑）。
* 变异验证：把两个调用块换回旧顺序后，断言 `write < render` 变为 `False` ⇒ 护栏变红。

---

### FIX-3 · P1 · `htmlToText` HTML 实体二次解码

**定位**：`static/review-locate.js` → `htmlToText()`

**问题**：实体解码顺序为 `&nbsp; → &amp; → &lt; → &gt; → &quot;`。
`&amp;` 先解 ⇒ `&amp;lt;`（字面文本 `"&lt;"`）被**二次解码**成 `"<"`：

```
"A &amp;lt; B"  old ⇒ "A < B"     ← 错（单遍解码器不会这样）
               new ⇒ "A &lt; B"   ← 对
```

OCR 原文里的 `&amp;lt;` 会被显示成尖括号，误导复核者以为原文如此。

**修复**：`&amp;` 的 `replace` 移到**最后**；顺带补齐 `&#39;`。

**测试**：新增 `tests/unit/test_review_locate_js.py`（22 条）——空/null 输入、
结构标签分隔、字面 `\n`、`<script>` 剥离、实体解码表，以及
`&amp;lt;` / `&amp;gt;` 的**定点顺序回归** + "真正的实体仍须解码"的正向对照。

---

### FIX-4 · P1 · SSE 降级轮询无限重试且无终态（UX 恢复路径缺失）

**定位**：`static/review-progress.js` → `PbcSse.create({ fallback })`

**问题**：SSE 重试耗尽后降级为 10s 轮询 `/api/jobs/{id}`。旧实现
`if (!r.ok) return;` 与 `catch { log.warn }` 都是**静默**的 ⇒ 后端持续不可用时
页面每 10s 无反馈地重试**到永远**：既无终态、也不提示用户，定时器与连接永不释放。

**修复**：加连续失败计数（成功即清零）；连续 6 次（≈60s）失败后停止轮询、
把进度文案换成可操作提示（"…已停止自动刷新 — 请检查后端服务后手动刷新页面"）
并置灰进度条。

**测试**：新增 `tests/unit/test_review_progress_js.py`（6 条）——用 stub 驱动
**真实回调**（DOM/定时器/fetch/EventSource 全 stub，`window` 自引用）：
达阈值必停 + 有提示；未达阈值不停；`fetch` 抛异常同样计数；**成功一次即清零**
（5 失败 + 1 成功 + 5 失败 不停）；终态停轮询 + 触发刷新（正向对照）。
变异验证：删掉 `pollFailures = 0;` 后"成功清零"用例变红。

---

### FIX-5 · P2 · 共享状态死镜像 `pageFindingCounts`

**定位**：`static/review-state.js:54`（原 `pageFindingCounts: ctx.page_finding_counts || {}`）

**问题**：该字段**无任何读取方**——页码圆点的真值始终来自 SSE 帧
`d.page_finding_counts`（`review-progress.js → updatePageNavDots`）。这份 SSR 镜像
只会与实时帧**漂移**（"同一数据两处存"是状态不一致的温床），且误导维护者以为它是
圆点数据源。

**修复**：删除该字段，就地注释说明"SSR 首屏圆点由 Jinja 变量直接渲染，不经本对象"。

**测试**：新增 `tests/unit/test_review_state_js.py`（10 条）——`bool()` 逐值归一
（`"true"`/1 为真，`"yes"`/2 为假）、状态键面**快照**（防同类死状态被无声加回）、
`pageFindingCounts` 定点回归、`setButtonLoading` 加载/复位/`null` 按钮。

---

### FIX-6 · P2 · 状态机注释与实现不符 + 缺不变量护栏

**定位**：`core/pipeline/state.py` → `recover_stuck_jobs()`

**问题**：注释称"状态机不允许 …ocr_done→error **不在** VALID_TRANSITIONS 中"，
据此论证崩溃恢复"必须"绕过状态机。**实测该论断错误**：五个非终态
（`pending/ocr_running/ocr_done/analyzing/cancelling`）**全部**允许 `→ error`。
真正的绕过理由是"需一并写 `error_message`/`finished_at`，且需要
`status IN (...)` 的**条件更新**来避免与并发 retry 抢写"。

**修复**：改写注释为事实（两条真实理由），避免后续维护者基于错误前提继续绕过状态机。

**测试**：`tests/unit/test_pipeline_state_machine.py` +4 条：
每个非终态必须可达 `error`（否则任务永久卡死）、`_STUCK_STATUSES` 必须都是真实
状态、必须都是非终态（终态不得被启动恢复误标）、恢复路径必须写 `stuck_recovery`
审计且 UPDATE 带 `status IN (...)` 条件。

---

### FIX-7 · P1 · 实时状态下状态点颜色**冻结在首帧**（文案与颜色互相矛盾）

**定位**：`static/upload-jobs.js` → `updateJobRowLive()`

**发现方式**：为 `upload-jobs.js` 补行为护栏时，用假 DOM 连打两帧，发现
第二帧 `li.querySelector(".status-dot")` 返回 `null`。

**问题**：`.status-dot` 是**产出方**（`renderJobRow`）与**消费方**
（`updateJobRowLive`）之间的定位契约。产出方写
`` `w-1.5 h-1.5 rounded-full ${dotCls} status-dot` ``，消费方却写
`` `w-1.5 h-1.5 rounded-full ${statusDotClass(st)}` `` —— **整体重写 className
时漏掉了 `status-dot`**。后果：

* 第一帧之后圆点再也找不到 ⇒ **颜色永久冻结在首帧状态**；
* 而 `.status-text` 仍在正常更新 ⇒ 同一行内**文案与颜色矛盾**
  （例如红点旁边写着"可复核"），对 GMP 复核者是主动误导。

终态会整行重建（重新带上标记），所以**最终**状态是对的 —— 缺陷只在
用户**正在盯着的中间过程**里出现，恰是最需要可信的时候。

**修复**：把状态点 class 收敛为单一函数 `statusDotClassName(st)`，
`renderJobRow` 与 `updateJobRowLive` **共用**。这与本文件对 `buildMetaLine`
的既有理由完全一致（"两条路径共用后，这种漂移在结构上不可能再发生"）。

**测试**：`tests/unit/test_upload_jobs_js.py::TestStatusDotSurvivesLiveUpdates`
—— 连打两帧，断言第二帧仍能定位圆点、class 等于由**真实** `PbcStatus`
推出的期望值、且不残留上一帧的破坏色。

---

### FIX-8 · P2 · `created_at` 派生有两份实现，且分隔符不一致

**定位**：`static/upload-jobs.js` → `updateJobRowLive()` / `buildRowFromSnapshot()`

**问题**：两处都从摘要行取回 `created_at`（SSE 快照不带该字段），但
**分隔符不同**：`split(" · ")` vs `split("· ")`。后者会留下**尾随空格** ⇒
终态重建出的行出现 `10:00  · 失败页`（双空格），且每次重建再累积一个。

**修复**：收敛为 `createdAtFromMeta(li)`，内部 `trim()`。这是同一类
"同一派生两处实现"的缺陷 —— 与 FIX-5（死镜像）、`buildMetaLine` 的由来同源。

**测试**：`test_row_is_replaced_and_keeps_filename_and_failed_pages`
以**精确相等**断言摘要行，双空格会被立即抓到。

---

### FIX-9 · P2 · 页码圆点的签名漏掉 `total` ⇒ 数字永久冻结在首帧

**定位**：`static/review-pageview.js` → `updatePageNavDots()`

**发现方式**：为 `review-pageview.js` 补行为护栏（此前只有源码字符串断言）时，
构造"total 变了但三个分档计数没变"的 counts，发现圆点上的数字不更新。

**问题**：跳过重建的缓存签名是 `` `${c.critical}-${c.warning}-${c.info}` ``，
但 info 分支渲染的是 **`c.total`** 而非 `c.info`：

```js
if (c.info > 0 && c.critical === 0 && c.warning === 0) {
  n.textContent = String(c.total);      // ← 输出依赖 total
}
const sig = `${c.critical}-${c.warning}-${c.info}`;   // ← 签名不含 total
```

`total ≠ critical+warning+info` 是**可达**的：后端
`api/jobs/page_image.py::_page_finding_counts` 对未知 severity 只累加
`total`、不增任何分档计数：

```python
sev = r["severity"]
if sev in entry:            # 未知 severity 落不进任何档
    entry[sev] += r["cnt"]
entry["total"] += r["cnt"]  # 但 total 照样涨
```

而 `findings.severity` 列是裸 `TEXT NOT NULL`（无 CHECK 约束），
`core/pipeline/stage3.py` 直接写 `f["severity"]` 原值，LLM 输出不做白名单归一
（`core/rules/llm_finding_guard.py` 只做 `str(f.get("severity") or "info")`）。
前端 `findings-map.js::severityZh → zhOrUnknown` 返回 `未知(x)` 也印证
"未知 severity"是既定现实。

**后果**：某页新增一条未知 severity 的 finding 时，`total` 从 1 变 2、签名不变
⇒ 圆点数字**永远停在 1**。GMP 场景下这是"该页问题数少报"。

**修复**：签名覆盖**渲染输出依赖的全部输入** —— 补上 `total`：

```js
const sig = `${c.critical}-${c.warning}-${c.info}-${c.total}`;
```

**测试**：`tests/unit/test_review_pageview_js.py::TestUpdatePageNavDots::
test_total_is_part_of_the_signature` —— 同一 `info` 计数、只改 `total`，
断言圆点文字由 `1` 变 `2`。

---

### FIX-10 · P2 · SSR 与 AJAX 的圆点容器标记不一致 ⇒ 陈旧的严重度圆点

**定位**：`templates/review.html`（生产者）↔ `static/review-pageview.js`（消费者）

**问题**：圆点由**两条渲染路径**产出，但标记不同：

| 路径 | 容器标记 | 子圆点标记 |
| --- | --- | --- |
| SSR（首屏，Jinja） | **无** | `data-dot="critical\|warning"` |
| AJAX（实时帧，JS） | `data-dots="1"` | 无 |

而 JS 的"删旧建新"只认 `[data-dots]`：

```js
el.querySelectorAll("[data-dots]").forEach((n) => n.remove());
```

选择器对不上 SSR 容器 ⇒ **服务端渲染的圆点永远删不掉**，每次实时重建都在旁边
**再挂一个**容器。后果不是"多两个小点"，而是**指向错误的严重度**：SSR 时刻该页
有 critical（红点），随后降级为 info；旧容器留着红点、新容器显示数字 ⇒ 复核者
看到红点会以为该页仍有严重问题。

终态任务不订阅 SSE，所以只影响**进行中**的任务 —— 恰是复核者实时盯着的时刻。
这与 FIX-7（`.status-dot` 标记被整体重写 className 丢掉）是**同一类**缺陷：
产出方与消费方对"定位标记"的约定不一致。

**修复**：统一为同一套标记 ——

* `templates/review.html` 的容器补上 `data-dots="1"`；
* `static/review-pageview.js` 生成的子圆点补上 `data-dot="critical|warning"`。

两条路径此后产出**完全一致**的圆点标记，下一个人再加消费者（CSS / e2e / 查询）
不会再次分叉。

**测试**：
* `TestUpdatePageNavDots::test_ssr_rendered_dots_are_replaced_not_duplicated`
  —— SSR 形态节点刷新后**只有一个**圆点容器；
* `TestUpdatePageNavDots::test_stale_ssr_severity_dot_disappears`
  —— SSR 的红点在该页降级后必须消失；
* `TestSsrAjaxDotMarkerParity`（3 条）—— 模板侧源码锁，标记名一改就红。

---

### FIX-11 · P1+P2 · 前端 UX 的 5 项韧性缺口（降级 / 滚动锁 / aria / 单位 / 空容器）

维度 6 列出的 5 条残留缺口一次性闭环。**共 42 条新用例**（3 个新文件）+
`upload-jobs.js` 1 条，**15/15 变异全部被抓**（见 §2 维度 1）。

#### 11a · P1 · `window.PBC` 缺失时调用点抛 TypeError，无降级

**定位**：`static/confirm-dialog.js`（提供者）↔ **10 个消费模块**

**问题**：`window.PBC.{showToast, confirmDialog, promptDialog}` 被 **10 个**模块
直接消费（`review*.js` 8 个 + `settings.js` + `upload.js` + `upload-jobs.js`，
**不是**报告初稿写的 2 个）。该文件一旦未加载（网络 / 缓存 / CSP 异常 / 扩展拦截），
调用点抛 TypeError；而这些调用多在 async 回调与事件处理器里，异常逃出 Promise
边界后**无人 catch** ⇒ 表现为"点了删除 / 取消 / 重试 / 归档没反应"：
没有请求、没有报错、没有提示。用户无法区分"操作被拒"与"页面坏了"。

**修复**：新增 `static/pbc-fallback.js`（**单一真值**，30 余行，零依赖 IIFE），
**只补缺失的方法、绝不覆盖已有实现**。由此得到两条关键性质：

1. **与加载顺序无关** —— `confirm-dialog.js` 结尾是无条件赋值，故谁先执行，
   最终生效的都是真实实现；只有它缺席时降级版才留下。
2. **降级语义只写一处**，不必在 10 个消费模块里各抄一份（那样必然漂移）。

降级的两条**不可退让的不变量**：**不可逆操作降级后仍必须被确认**（连原生
`confirm` 都问不到时返回 `false` —— 降级绝不能放行删除）；**失败必须可见**
（`err` 类走 `alert`，成功类降到 `console`）。

三处模板（`review.html` / `upload.html` / `settings.html`）在 `confirm-dialog.js`
**之前**引入该 shim。

**测试**：`tests/unit/test_pbc_fallback_js.py`（**23 条**）—— 含两条**加载顺序
置换**用例（shim 先 / confirm-dialog 先）、"缺原生 confirm 必须否决"、
"err 必须 alert / 成功不得 alert"、"prompt 取消返回 null 而非空串"、
以及端到端经**真实入口**（`deleteJob` 事件委托、`window.cancelJob`）验证
"只有 shim 时删除 / 取消仍可用且不抛错"。残余风险（shim 与 confirm-dialog.js
**同时**失败）已在 shim 头部诚实标注。

#### 11b · P2 · 嵌套弹窗的滚动锁被"先关闭者"提前解开

**定位**：`static/confirm-dialog.js` `lockScroll` / `unlockScroll`

**问题**：旧实现每处关闭都直接 `document.body.style.overflow = ""`。弹窗可叠加
（设置页"删除 provider"确认框之上又弹"未保存"提示），于是**先关闭的那个**把锁
解掉，而另一个仍在显示 —— 用户看到弹窗还开着、背后页面却能滚。

**修复**：改为**计数器**（`scrollLocks`），只有归零才恢复滚动。

**测试**：`test_confirm_dialog_scroll_aria_js.py` 的 `TestNestedDialogsKeepTheScrollLock`
（3 条）—— 两层（关下层后仍 locked）、混合 confirm+prompt、三层（前两次关闭都须保持锁定）。

#### 11c · P2 · 错误 toast 的播报被容器 politeness 盖过

**定位**：`static/confirm-dialog.js` `showToast`

**问题**：live region 在**容器**上（`role="status"` + `aria-live="polite"`），
"删除失败"这类必须尽快播报的消息也要排队等读屏用户停下手上的操作。

**修复**：容器改为普通 `role="region"` + `aria-label="通知"`；**逐条 toast** 定 role：
`err → role="alert"`（隐式 assertive），其余 `role="status"`（隐式 polite）。

**测试**：`TestToastLiveRegionSemantics`（3 条）—— 容器不再是 live region、
逐条 role 为 `["status","alert","status"]`、缺省 `type` 不得误判成错误。

#### 11d · P2 · 亚 MB 限额渲染成"文件超过 0MB 上限"

**定位**：`static/upload.js` `limitMB` → `formatBytes`

**问题**：旧写法 `Math.floor(max_bytes / 1024 / 1024)` 在 `max_bytes < 1MB` 时
得到 **0** ⇒ "文件超过 **0MB** 上限"，荒谬且让用户以为限额被配坏了。

**修复**：按量级选单位（B / KB / MB）的纯函数 `formatBytes`。

**测试**：`test_upload_resilience_js.py` 的 `TestUploadLimitMessageUnits`（6 条）
—— 512000→`500 KB`、512→`512 B`、10 MiB→`10 MB`（整数不带小数）、
1.5 MiB→`1.5 MB`、**恰好等于上限必须放行**（`>` 而非 `>=`）、低于上限放行。

#### 11e · P2 · 容器缺失时 `loadHistory` 整段脚本中断

**定位**：`static/upload-jobs.js` `loadHistory`

**问题**：`loadHistory(1)` 在**模块加载期**被调用，旧代码紧接着
`listEl.innerHTML = ""`。容器一缺就抛 TypeError，异常中断整个 IIFE ⇒ 后续所有
事件绑定（删除 / 归档 / 翻页按钮）全部失效，页面"加载完了但点什么都没反应"。

**修复**：`if (!listEl || !pagEl) { log.warn(...); return; }` 提前返回；
`history-count` / 归档计数等次要 UI 一律先判存在（`if (countEl)`）；
`renderPagination` / `loadArchivedList` 同样加守卫。

**测试**：`TestLoadHistoryContainerGuards`（4 条）—— 缺列表 / 缺分页容器均不得
中断 IIFE（`goToPage`/`refreshHistory` 仍须在）；**缺计数容器仍须正常渲染**
（锚定空态文案 `暂无历史记录`，且 `加载失败` 不得出现 —— 只断言 `children > 0`
是空断言，`catch` 也会写一条错误 `<li>`）。

> **⚠️ 顺序说明**：本轮把 11a 的降级**从"10 处薄封装"改为"一个 shim"**。
> 薄封装方案曾短暂落地（`upload.js` / `upload-jobs.js` / `review.js`），
> 后发现 10 个消费点各写一份守卫必然漂移，遂改为单一真值并回退了薄封装。
> 该决策过程与理由见 §维度 6。

---

## 2. 六维度评估结论

### 维度 1 · 前端测试覆盖

**现状**：仓库测试文化很强（node 实跑 + 变异验证 + "空护栏"意识），但覆盖面**严重不均**：
`status.js / sse.js / eta.js / findings-map.js` 有行为覆盖；
`review*.js` 八个模块几乎**只有静态字符串断言**。

**本次累计补充 279 条用例**（13 个新文件）覆盖：`emptyFindingsNote`、渲染顺序不变式、
`htmlToText`、降级轮询、`bool`/`setButtonLoading`/状态键面、**`confirmDialog` 全部
关闭路径与回车语义**、**`updatePageLevelUI` 全部横幅 + 参数矩阵 + OCR 豁免区闭环**、
**抑制台账**、**PDF 左栏分页/圆点/缩放**、**设置页 provider/规则/飞书**、
**`window.PBC` 降级 shim（加载顺序无关 + 两条降级不变量）**、
**嵌套弹窗滚动锁与 toast 播报语义**、**上传限额单位与空容器韧性**。

**仍缺（按优先级，未在本次补）**：

| 优先级 | 目标 | 缺口 | 为何重要 |
|---|---|---|---|
| ~~P1~~ ✅ | `confirm-dialog.js` | **已补 16 条**（见下） | **误按回车确认删除 = 数据丢失** |
| ~~P1~~ ✅ | `review-pageinfo.js` | **已补 39 条**（见下） | 豁免是**审计记录**；横幅漏更即误读 |
| ~~P1~~ ✅ | `upload-jobs.js` | **已补 39 条**（见下；FIX-11 批再 +1 → **40 条**），并据此**发现并修复 FIX-7 / FIX-8** | 失败页数静默丢失是合规缺口（#134 同族） |
| ~~P2~~ ✅ | `review-suppressions.js` | **已补 22 条**（见下） | 抑制台账不可见 = 审计链断裂 |
| ~~P2~~ ✅ | `review-pageview.js` | **已补 41 条**（见下），并据此**发现并修复 FIX-9 / FIX-10** | "末页回跳后 next 永久卡死"回归门 |
| ~~P2~~ ✅ | `settings.js` | **已补 31 条**（见下）；`display`/`esc`/`feishuSelectedEvents`/`ruleDirty` 均经真实入口实跑 | 设置页是"配错就整条链路不通"的入口 |

#### 本轮追加：六个缺口已闭环

**A. `tests/js_harness.py` —— 可复用假 DOM harness（新基础设施）**

前两档护栏（"node 实跑纯函数" / "源码字符串断言"）之间的**中间档**此前完全缺失：
"DOM 交互类函数的真实行为"。本次补上，含假 `document`/`window`/元素工厂、
定时器队列（`__flushTimers`）、`fetch`/`EventSource` stub、`__keydown` 分发。
后续 `upload-jobs.js` / `review-pageview.js` 可直接复用。

harness 能力边界**写在模块 docstring 里**（避免后来者误以为它保真）：
`innerHTML` 只解析**扁平元素**序列、定时器需手动驱动、微任务用
`setImmediate` 排空。

**B. `tests/unit/test_confirm_dialog_js.py`（16 条）**

覆盖 Esc / 遮罩点击 / 取消按钮 / 确认按钮四条关闭路径、**回车语义**
（危险操作无焦点⇒取消、非危险⇒确认、焦点在按钮⇒交给浏览器）、
初始焦点落在"取消"、Tab 焦点环、keydown 监听器不泄漏、
`promptDialog` 的"空串 ≠ null"契约。

**C. `tests/unit/test_review_pageinfo_js.py`（39 条）**

`currentPageFlags` **无条件写入**（含"页面上一个元素都没有"时）、置信度徽章
（parse-error 页必须隐藏）、parse-error / grounding / 截断 / schema / OCR 五类
横幅的显示隐藏与文案、参数矩阵三态单元格、`applyInitialPageLevelUI`、
OCR 豁免区的**真实点击 → 请求体 → toast → 重载**闭环（撤销 / 授予 /
取消 / 失败四条分支）与 `reason` 转义（存储型 XSS 回归）。

**D. `tests/unit/test_review_suppressions_js.py`（22 条）**

抑制留痕面板此前**零行为覆盖**（且**无任何**源码断言）。覆盖：空 entries
**同时**隐藏面板 + 清空列表（只隐藏不清空 ⇒ 残留别页条目；只清空不隐藏 ⇒
假"空台账"）、`data` 为 null/undefined、`total=0` 时不显示 brief、
面板/列表/摘要三者缺一不抛错、已回退条目**不再给回退按钮**、
description/reason/命中证据字段**全部转义**（存储型 XSS）、
`loadSuppressions` 的 URL 与页码、**HTTP 失败不得清空已有面板**（抹掉已展示的
证据 = 审计链在界面上被静默截断）、**代际守卫**（翻页后过期响应必须丢弃）、
网络异常不外抛；`revertSuppression` 的按钮加载态、POST 请求体、
**"刷新问题清单 → 再重载台账"的顺序**（用"被调用时已发出的请求数"作证）、
成功/失败两条分支的 toast 与按钮恢复、`event.currentTarget` 缺失时的不抛错。

**E. `tests/unit/test_review_pageview_js.py`（41 条）**

左侧 PDF 面板此前只有源码字符串断言（`assert "REGION_ASPECT_TOL" in js`）。
覆盖：**"末页回跳后 next 永久卡死"回归门**（缓存命中分支仍须
`syncNavButtons()`）、换页设 src / 清区域框 / 重置 loading、`total_pages=0`
显示 `?`、缩放**夹取与百分比格式化**、缩放后区域框按新图片尺寸重算、
圆点的**签名跳过**与元素同一性、`buildPageNav` 占位态重建与点击不整页刷新、
选中态双向切换、`locateRegion` 的像素几何、再点收起、切换 finding、
**宽高比闸门**（拒绝时 toast 且不画框 / 容差内放行 / 旋转页拒绝 /
`page_aspect` 缺失不拦 / 图片未加载不误判）、可视区外的滚动定位、
`Number()` 归一（类型敏感比较）、以及 SSR ↔ AJAX 标记一致（3 条）。

**并在补测过程中抓出 FIX-9（圆点数字冻结，P2）与 FIX-10（陈旧严重度圆点，P2）**
—— 见第 1 节。两个模块此前都被认为是"已覆盖"的（有源码断言），
实际都是行为盲区。

**F. `tests/unit/test_settings_js.py`（31 条）**

`settings.js`（1861 行）此前**零前端覆盖** —— 只有后端 API 测试
（`test_api_settings_*.py`）。它是"配错就整条链路不通"的入口，故本轮补齐：

* provider 渲染（显示名回落、当前徽标与置顶、内置项不可移除、脱敏 key）；
* **不可信文本转义** —— `name`/`model`/`base_url`/`api_key` 都来自
  `config.json`（可手改），直接拼进 `innerHTML` 即存储型 XSS；
* 规则区：计数、命中徽标、"从未保存 — 规则不会生效"警告、
  **总字数上限翻红**（后端会 400 硬拒，这是提交前唯一预警）、
  `textarea` 的 `input`/`change` 事件置脏并更新字数、删除的**确认闸门**、
  规则加载失败可见；
* 飞书通知：勾选的事件**按 DOM 顺序原样提交**（勾错 = 该推的不推）；
* provider 自动改写：徽标 + 原因提示（用户唯一能看到的告知）；
* 加载失败可见（网络异常 / HTTP 状态码）；
* **`bindProviderActions` 的"克隆换节点"防重复绑定** —— 断言两次渲染后
  列表节点上的 click 监听器数量仍为 2（否则"点了没反应"/双请求）；
* **跨语言常量一致性机检** —— `settings.js` 的 `8000` / `1000` 必须等于后端
  `_USER_RULES_TOTAL_MAX` / `USER_RULES_TEXT_MAX`（本仓库对"两处实现"的既有
  做法，见 `test_status_js.py`）。

**为跑通它，harness 又补了 4 项能力**（每一项都曾是"静默失真"）：

| 能力 | 缺了会怎样 |
|---|---|
| `cloneNode(deep)`（**不**复制监听器） | `bindProviderActions` 直接 `TypeError`；复制了监听器则防重复绑定永远测不出 |
| `replaceWith` 同步 `getElementById` 注册表 | 克隆替换后 `getElementById` 拿到已脱离文档的死节点，断言测的不是页面上的那个节点 |
| `cloneNode` 回灌 `dataset` | 克隆体的 `dataset.xxx` 全变 `undefined`（选择器还能命中）⇒ 按 `dataset` 写的断言静默失真 |
| 伪类 `:checked` | `feishuSelectedEvents()` 静默返回空集 ⇒ "没勾任何事件"的**空断言**（与 `:not` 不同，忽略 `:checked` 是往**假绿**方向失真） |

**G. FIX-11 批（3 个新文件，42 条）—— 收的是维度 6 的 UX 缺口，不是上面六个覆盖缺口**

`test_pbc_fallback_js.py`（23）、`test_confirm_dialog_scroll_aria_js.py`（8）、
`test_upload_resilience_js.py`（11）。三者都要求 harness 之外的**新观测面**：
Promise 解析值（降级 confirm 的 true/false）、`body.style.overflow` 的**逐层快照**、
逐条 toast 的 `role` 属性、`status.textContent` 的单位文案、容器缺失时
`window.goToPage` 是否仍存在。详见 FIX-11 与维度 6。

**为跑通它们，harness 又补 1 项能力**：`getElementById` 改为**"注册表 + 树内查找"
双路** —— `createElement` 不登记 id，运行期建出的 `#toast-container` 只查注册表
会返回 null。这与上表 4 项同属"能力缺失伪装成功能正常"。

#### 顺带发现四条**假绿**护栏（比新增用例更有价值）

1. **`test_enter_while_cancel_focused_does_not_settle` 原本是空断言。**
   `settle()` 先改样式、再把 `resolve` 包进 **150ms 定时器**；原用例不 flush
   定时器就断言"Promise 仍未 settle"，于是"没结算"与"结算了但还没 resolve"
   **无法区分** —— 删掉守卫它照样绿。改用同步副作用（新增定时器数 +
   `overlay.style.opacity`）作判据后，变异 M1 才被抓到。
   ⇒ **断言"某事没发生"时，必须选一个"若发生了就立刻可见"的信号。**
2. **新用例自己也踩了同一坑**：用 `let early = SENTINEL; p.then(v => early = v)`
   想快照"点击前是否已结算"，但 `early` 是**活绑定**，回调稍后 resolve 时把它
   改写 ⇒ 读到的是**最终值**。快照必须"布尔 + 立即读出"。
3. **本轮新写的 `test_string_finding_id_is_coerced` 也是空断言。**
   我断言"字符串 id 也要能锚上"，但 `regionRefs` 是普通对象、属性名本就是
   字符串 —— `regionRefs["7"]` 与 `regionRefs[7]` 等价，所以删掉
   `Number(findingId)` 它照样绿（变异 M8 **没抓到**）。真正被 `Number()`
   影响的是 `activeRegionFid === fid` 这个**类型敏感**的比较：数字/字符串混用
   时第二次点击收不起来。改成混用类型后 M8 立刻变红。
4. **`test_zoom_rounds_and_clamps` 的"取整"部分同样是空断言。**
   `zoomPdf` 里的 `Math.round((pdfZoom + delta) * 100) / 100` **不可观测** ——
   `applyZoom` 用 `Math.round(pdfZoom * 100)` 又取整一次，内部浮点毛刺
   （`1.3000000000000003`）永远到不了 DOM。该处取整是卫生措施，不是可测行为。
   已把用例改名为 `test_zoom_formats_percent_and_clamps`、在 docstring 写明
   "诚实边界"，变异改为验证**夹取**（去 `PDF_ZOOM_MAX` / 去 `PDF_ZOOM_MIN`），
   两条都被抓到。

> 这 4 条里有 3 条是**我自己写的**假绿。结论：新增用例必须**先跑变异**再宣称
> 覆盖 —— 用例的存在不等于断言的有效。

#### FIX-11 批次又抓出四条（同样是我自己写的）

5. **`test_upload_delete_still_gates_and_proceeds` 的确认文案断言是空断言。**
   我断言"确认框里提到文件名"，但文件名本来就在 `title` 里 —— 去掉 `message`
   它照样绿（变异 M11 实测没抓到）。改为锚定**只有 `message` 才含**的
   `不可恢复` / `审计日志` 后立刻变红。
6. **FIX-8 的 `.trim()` 在单帧下不可观测。** 原用例只驱动**一帧**实时帧，
   有没有 `.trim()` 结果一样。改为驱动**两帧**（第二次的 `created_at` 会拼在
   第一次之后，空格才暴露）后，`M12` 才被抓到。
7. **`children > 0` 也是空断言。** 容器缺失用例断言"列表非空"，但 `loadHistory`
   的 `catch` 同样会写一条"加载失败"的 `<li>` —— 长度也是 1。改为断言**正常空态
   文案**（`暂无历史记录` 在、`加载失败` 不在）才能区分"渲染成功"与"异常被兜住"。
8. **M19 是一条*无效变异*（不是护栏漏网）。** 我只改了 MB 分支，而亚 MB 输入
   **根本走不到**那个分支 ⇒ 它不可能被抓。这类 `MISSED` 必须先分诊
   "断言缺口" vs "无效变异"，否则会误以为护栏有问题而白写一堆用例。
   改回"整个 `formatBytes` 退化成 `Math.floor(bytes/1MB)`"（忠实复现原始 bug）
   后即被抓。

> 教训升级：**`MISSED` 要分诊**。断言缺口要补断言；无效变异要重写变异。
> 把两者混为一谈，会在错误的层反复返工。

#### 变异验证（73/73 全部被抓）

`confirm-dialog.js`：去焦点早退守卫、兜底无条件确认。
`review-pageinfo.js`：去 integrity 抑制、去 `reason` 转义、不写 `currentPageFlags`、
未判定单元格当合格、允许空原因豁免、撤销不确认、所有 finding 都算 critical、
parse-error 页仍显置信度。
`upload-jobs.js`：重新漏掉 `.status-dot` 标记（FIX-7 回归）、不 trim `created_at`
（FIX-8 回归）、不刷新实时摘要行（#134）、不透传 `failed_pages`、运行中也允许删除、
删除不确认、去掉翻页竞态守卫、把非数组 `failed_pages` 当数组。
`review-pageview.js`（本轮 11 条）：`syncNavButtons` 放回缓存分支之后（历史事故形态）、
签名漏掉 `total`（FIX-9 回归）、去掉签名跳过、不删旧圆点容器（FIX-10 回归）、
JS 圆点丢掉 `data-dot` 标记、`applyZoom` 不再重算区域框、宽高比闸门放行、
`findingId` 不做 `Number` 归一、去掉滚动到区域、去掉 `PDF_ZOOM_MAX` /
去掉 `PDF_ZOOM_MIN`。
`templates/review.html`（1 条）：SSR 容器丢掉 `data-dots` 标记（FIX-10 回归）。
`review-suppressions.js`（7 条）：空 entries 不隐藏面板、去掉代际守卫、
去掉 HTTP 失败守卫、description 不转义、命中证据不转义、失败后不恢复按钮、
回退成功后不刷新问题清单。
`settings.js`（本轮 21 条）：`esc` 退化为恒等、显示名不查映射表、状态徽标恒真、
总字数上限 / 单条上限常量漂移、超限不翻红、飞书事件不看勾选（提交全量）、
`ruleDirty` 不置脏 / 不更新字数、删除不弹确认、空规则无提示、不呈现自动改写、
无通知却写徽标、网络失败不提示、HTTP 失败不提示、"从未保存"不警告、
内置 provider 可被移除、去掉"克隆换节点"、已选事件不回填、
规则加载失败静默吞掉、当前 provider 不排首位。

> 其中 M8 / 原 M10 两条**第一轮没抓到** —— 原因是我自己的用例是空断言（见上）。
> 修正用例后重跑，`review-pageview.js` + `templates/review.html` +
> `review-suppressions.js` 这一批 **19/19 全部变红**；`settings.js` 那批
> **21/21 全部变红**。至此（FIX-11 之前）累计 **58/58**。

**FIX-11 批次（本轮 15 条，`pbc-fallback.js` / `confirm-dialog.js` /
`upload.js` / `upload-jobs.js`）**：

* `pbc-fallback.js`（5 条）：缺原生 confirm 时**放行**（fail-open）、
  err 类 toast 不再 alert、prompt 取消返回空串而非 null、
  shim **无条件覆盖**（破坏顺序无关）、不转发 `defaultValue`。
* `confirm-dialog.js`（5 条）：`unlockScroll` 退化为布尔复位（**原始 bug 形态**）、
  `unlockScroll` 永不释放、`lockScroll` 不锁、错误 toast 也用 polite、
  容器仍是 live region。
* `upload.js`（3 条）：`formatBytes` 退回 `Math.floor(bytes/1MB) MB`、
  整数 MB 也带一位小数、边界 `>` 写成 `>=`。
* `upload-jobs.js`（2 条）：去掉容器守卫（加载期 TypeError 中断 IIFE）、
  `history-count` 不做判空。

> **15/15 全部变红**，累计 **73/73**。
>
> ⚠️ **本批次是"重跑"得来的，不是沿用手写数字。** 上一批的临时变异脚本
> （`pbc_mutation_*.py`）落在系统临时目录、已被清理；而本项目铁律是
> **报告里的计数必须可复现**。故本轮把 15 条变异**重新合并成一个脚本**
> （临时副本 + `[BASELINE OK]` 金丝雀）实跑取数，而非从记忆里抄一个数。
> 同理，报告初稿里一度出现的"141/141"因**无脚本可复现**而弃用。
>
> **脚本已持久化**：`devlogs/_verify/mutation_fix11.py`（`devlogs/` 已 gitignore
> ⇒ 本地可复跑、不进 git）。路径自适应（`Path(__file__).resolve().parents[2]`），
> 跑法见其 docstring。**要改这批护栏，先跑它。**

> ⚠️ 变异脚本**不得改真文件**：Bash 工具 120s 超时 SIGTERM 时，Python 默认
> SIGTERM 处理器**不展开 `finally`**，变异会留在文件里；而未跟踪文件
> （`static/review-pageinfo.js`、`static/upload-jobs.js` 等 R63 拆分产物）
> **git 救不回来**。本次已实际踩到（残留 `(f) => true,`，靠逐字节核对 8 个锚点
> 才发现）。正确做法：把 `js_harness.STATIC` 指向临时副本目录，`pytest.main()`
> 进程内跑。

#### `upload-jobs.js`（39 条，FIX-11 批再 +1）—— 补测过程**当场抓出两个真实缺陷**

本模块是**无导出**的 IIFE（仅挂 `window.refreshHistory` / `window.goToPage`），
故全部经由**真实入口**驱动，**未新增任何生产代码**：
`fetch` 桩 → `loadHistory`；捕获 `PbcSse.create` 的 `onFrame` → `updateJobRowLive`；
在 `#history-list` 上派发 click（假 DOM **不冒泡**，故必须指定 `target`）。

覆盖：摘要行四种形态（含 >12 页省略号、非数组守卫）、空/失败态、计数、分页显隐、
**翻页竞态丢弃**、实时帧刷新摘要行（#134）、`error`/`partial_review` 原因可见、
OCR 后端标签、终态重建行（保留文件名 + 失败页 + 按钮解禁）、
按钮门控（运行中禁删）、文件名 XSS、归档/删除的**确认闸门与请求体**、
降级轮询的启停与泄漏。

**并在补测过程中抓出 FIX-7（状态点颜色冻结，P1）与 FIX-8（`created_at` 双空格，
P2）** —— 见第 1 节。这正是行为护栏的价值：两条缺陷都**不会**被源码字符串断言发现。

**另发现（测试基础设施）— 已定位根因：不是仓库缺陷，是沙箱工具的产物**

现象：全量跑（`tests/unit` + `tests/integration`）时会有 **1 条**用例失败，且**受害者在不同
run 之间会变**（本次见过 `test_pdf_non_local_host_returns_403` 与
`test_upload_image_creates_job[webp]`）；单跑、只跑 `tests/integration` 都通过。

**根因（已用 traceback 逐帧验证）**：失败**不在断言上**。以
`test_main_routes.py::TestServePdf::test_pdf_non_local_host_returns_403` 为例，
traceback 的 `>` 标记落在 **`finally:` 块**：

```
            r = await test_client.get("/api/jobs/guarded-pdf/pdf", headers={"Host": "evil.com:80"})
            assert r.status_code == 403          # ← 这一行**通过了**
        finally:
>           pdf_path.unlink(missing_ok=True)     # ← 失败在这里
tests\integration\test_main_routes.py:204:
  → sitecustomize.py:_safe_path_unlink → _try_trash → _check_bulk_delete_guard
E   SystemExit: 1
```

抛出 `SystemExit(1)` 的是 WorkBuddy 沙箱的**批量删除守卫**：

```
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
{"count":715,"threshold":50,"scope":"turn","targets":["...\output\guard_probe.pdf"],"targetCount":1}
```

它 monkeypatch 了 `Path.unlink`，按 **turn 累计**删除次数；累计值（715）越过阈值（50）后，
下一次 `unlink()` 直接 `SystemExit(1)`。于是**哪个用例恰好在越线后删文件，哪个用例就"失败"**
—— 这解释了受害者的不确定性。日志同时证明守卫本身工作正常：

```
WARNING main: [guard] 拒绝非本机请求 GET /api/jobs/guarded-pdf/pdf (host='evil.com:80' origin=None)
```

**结论**：仓库代码**没有问题** —— 403 守卫正确触发；`is_local_request`
（`core/security.py`）是纯 header 逻辑、无状态、不受测试顺序影响。这条失败**只在本沙箱
环境出现**，CI 与普通开发机上不会复现。

**排除过程**（供后来者省时间）：

* 把 110 个 unit 文件对半切，**两半都复现**，且报出**同一个** `count: 715`
  ⇒ 计数是跨 run 的持久状态，不是某个测试文件的"污染"。
* 早先"93 / 155 / 76 passed"的排除结论**方向就错了**：当时计数尚未越线，
  自然全绿 —— 那组实验无法证伪任何东西。
* 早先怀疑的"进程级 `config` 单例未还原"与本例**无关**。

**对使用者的建议**：全量跑时若看到 1 条此类失败，先看 traceback 的 `>` 落在哪一行。
落在 `finally` 的删除语句上即可忽略（或把该断言单独重跑确认）。


---

### 维度 2 · 前端状态更新与渲染链路

**链路（可追溯）**：

```
SSR  __PBC__ ──► review-state.js（唯一共享状态宿主）
                     │  ctx → jobId/currentPage/totalPages/currentPageFlags/regionRefs
                     ├─► review-pageview   （页图/缩放/页码导航/区域锚）
                     ├─► review-pageinfo   （横幅/置信度/参数矩阵/豁免）  ← currentPageFlags 唯一写点
                     ├─► review-findings   （findings 渲染与裁决）        ← 读 currentPageFlags
                     ├─► review-locate     （OCR 面板定位）
                     └─► review-suppressions（抑制台账）

SSE /api/jobs/{id}/stream ──► review-progress ──► 状态徽章/状态点/进度条/ETA/
                                                  stall 横幅/取消按钮/错误横幅
```

**评估结论**：

* **代际守卫完备**：`pageLoadToken` 覆盖 `loadPageData`（2 处校验）与
  `refreshCurrentPageFindings`（2 处校验）；`loadSuppressions` 另按
  `page !== state.currentPage` 自守。快速翻页的过期响应会被丢弃。
* **已消除的不一致源**：FIX-2（写点先于读点）、FIX-5（删除死镜像）。
* **冗余渲染**：`loadPageData` 末尾 `finally` 再次调用 `updatePdfDisplay` 属**刻意**
  （恢复翻页按钮禁用态）；该调用命中"src 已是目标页"缓存分支提前返回，
  开销可忽略。**保留**（有注释说明）。
* **剩余风险（低）**：
  * `state.regionRefs` 跨页只增不减——长会话（数百页）会有轻微内存增长，
    量级为数千个小对象，**非泄漏**，可接受；若追求严谨可在翻页时按页清理。
  * 共享状态无统一 setter，靠"约定 + 键面快照护栏"维持（FIX-5 已加护栏）。
  * 更新 `state` 的调用点分散（`review-progress` 改 `totalPages`、`review.js` 改
    `currentPage`），但每字段写点唯一，未见漂移。

---

### 维度 3 · 状态机与 pipeline 完整性

**状态机**：`VALID_TRANSITIONS` 覆盖 10 个状态，无孤立状态。

```
pending ─► ocr_running ─► ocr_done ─► analyzing ─┬─► review ──────┐
   │            │            │            │       └─► partial_review│
   └────────────┴────────────┴────────────┴─► error ──────────────┐│
                                              cancelling ─► cancelled┤│
                                     review/partial_review/error/   ││
                                     cancelled ─► pending（retry）──┘│
                                     review/partial_review/error/    │
                                     cancelled ─► archived ─► review ┘（unarchive）
```

* 五个非终态**全部可达 `error`** ⇒ 任何任务都能被崩溃恢复或看门狗收敛（新护栏锁定）。
* 两处"绕过状态机"（`recover_stuck_jobs`、`watchdog`）**均写审计**
  （`stuck_recovery`），且恢复 UPDATE 带 `status IN (...)` 条件防并发抢写。
* `cancelling → cancelled` 只在 `_is_cancelled()` 收口，审计 + 飞书通知同点触发。

**阶段职责**：`stage1`（OCR + failover + 空页自愈）/ `stage2`（单页 LLM + 质量分级）/
`stage3`（跨页规则 + LLM 语义 + final_status 判定）边界清晰；
`engine.py` 负责编排（含 `OCR_SLICES>1` 分片路径）；`self_heal.py` 承载几何自愈。
**阶段间耦合**主要通过 `state.py` 的状态/进度写入函数，方向单一（stage → state），
无反向依赖。

**评估结论**：状态机与 pipeline 完整性**良好**；本次修正了 1 处**误导性注释**
（FIX-6）并补上 4 条不变量护栏。

---

### 维度 4 · 前端大文件拆分（>2000 行）

**实测**：**没有任何 JS 文件超过 2000 行**。最大为 `settings.js` **1861 行**。
`review.js` 已从 2037 行拆为 8 个模块（R63），拆分质量高（单一职责、脚本依赖序有护栏）。

**`settings.js` 拆分方案**（按单一职责，建议 5 个文件）：

| 目标文件 | 行范围 | 职责 | 优先级 |
|---|---|---|---|
| `settings-state.js` | 1–76 | logger / `current` 状态 / `display`/`ocrDisplay`/`statusBadge` 等纯工具 | P1（先行，被其余模块依赖） |
| `settings-llm.js` | 76–521 | provider 渲染 + 动作绑定（激活/保存/清 Key/测试/删除）+ `initSectionNav` | P1 |
| `settings-ocr.js` | 521–972 | KB 面板 + OCR 配置保存 + `load()` + 自动激活提示 + `fillOcrForm`/`setSeg` | P1 |
| `settings-feishu.js` | 972–1224 | 飞书表单/保存/清除/测试 | P2 |
| `settings-rules.js` | 1224–1539 | 规则编辑器 + 模板面板 | P2 |
| `settings-wiring.js`（入口） | 1539–1861 | 各 `addEventListener` 接线 + 底部"保存全部"/"测试连接" + `showMsg` + init | P1（最后加载） |

**收益**：单文件降到 ~500 行以内；provider/OCR/飞书/规则四块可独立测试；
接线集中在入口，`document.getElementById(...)` 的空指针风险集中可查。
**风险**：`settings.js` 目前是**单个 IIFE 闭包**，`current`、`activeProvider` 等
是闭包变量——拆分必须先抽到 `settings-state.js` 的命名空间（与 `review-state.js`
同款模式），否则会重演 review.js 的"同名遮蔽"类缺陷。
**建议**：先拆 `settings-llm.js`（最大、最独立），并同步在 `templates/settings.html`
按依赖序引入 + 加脚本顺序护栏（复用 `test_review_js_modules.py` 的 `TestScriptOrder` 模式）。

**`upload-jobs.js`（933 行）**：职责（列表渲染/SSE 聚合/归档删除）相对内聚，
**建议暂不拆**；若后续增长，按"行渲染（`renderJobRow`/`buildMetaLine`/
`buildRowFromSnapshot`）"与"实时订阅（`startLiveTracking`/`startFallbackPolling`）"切开。

---

### 维度 5 · 后端模块化评估

**总体**：包结构按**技术层**（`core/pipeline`、`core/rules`、`api/jobs`、`api/settings`）
而非按 feature 划分；`__init__.py` 承担了过重的 re-export 职责。

**主要问题**：

| 文件 | 行数 | 责任判定 | 建议动作 | 优先级 |
|---|---|---|---|---|
| `core/mineru_client.py` | 1301 | **混杂 4 类**：HTTP 客户端 + markdown 组装 + 页切分 + OCR 编排 | 抽 `mineru_markdown.py`（~788–962）、`mineru_pages.py`（~391–1014） | P1 |
| `core/page_analyzer.py` | 1066 | **混杂**：LLM 编排 + 校验 + grounding + HTML 截断 | 抽 `core/grounding.py`、`core/html_budget.py` | P1 |
| `config.py` | 1049 | **混杂**：文件/env 迁移 + 用户规则 + 上传限额 + provider 解析 + 运行时改写；**import 期做 I/O** 且是 21 个模块依赖的单例 | 抽 `config_rules.py`/`config_limits.py`；`config` 改为惰性构建 + 可注入 | P1 |
| `core/rules/llm_finding_guard.py` | 871 | 自带日期代数（与 `rules/parsing.py` 重复） | 日期工具下沉到 `parsing.py` | P2 |
| `api/review.py` | 817 | **读写混杂**：12 个 handler 横跨 GET 统计/列表/审计 + 变更 | 拆 `review_read.py` / `review_write.py`（对齐 `jobs/`） | P2 |
| `core/pipeline/self_heal.py` | 996 | 视觉几何 + 切片 I/O + DB 进度 | 抽 `self_heal_rotation.py` | P2 |
| `llm/client.py` | 739 | 重试/退避 + JSON 修复 + 掩码 + 单例 | 抽 `llm/json_repair.py`；掩码与 `settings/read._mask` 去重 | P2 |

**单一真值违反（重复实现）**：
* 密钥掩码两套：`llm/client._mask_secrets` vs `api/settings/read._mask`。
* 日期解析两套：`llm_finding_guard` 自实现 vs `rules/parsing._parse_time`。
* 任务状态集合三处：`api/jobs/__init__._ACTIVE/_TERMINAL_STATUSES`、
  `core/pipeline/state._STUCK_STATUSES`、`VALID_TRANSITIONS`。

**P0 建议（结构性收益最大）**：消除 `core/pipeline/__init__.py` 这个
**god 循环枢纽**（re-export ~40 个符号）。目前为绕开循环，全仓库有 **~110 处
函数内惰性 import**（`core/` ~60、`api/` ~50），是最大的可测试性与耦合税。
做法：把 `analyze_cross_page` 从 `core/rules/__init__.py` 移到 `rules/engine.py`，
再把"为让测试 monkeypatch 而做的惰性 re-export"改为对稳定门面
`core.pipeline.api` 的顶层 import。**单点改动即可去掉大部分惰性 import**，
无需重写业务逻辑。

---

### 维度 6 · 前端用户体验（边界 / 优雅退出 / 错误恢复）

**已具备**：
* 错误提示：`showToast`（替代 alert）+ SSR 错误横幅 + SSE 转态期即时横幅
  （`showJobErrorBanner`）+ 停滞横幅（`stall-banner`）。
* 优雅退出：`beforeunload`/`pagehide` 清理 SSE 连接、轮询定时器、阶段计时器
  （`sse.js` 注册连接清理，`review-progress`/`upload-jobs` 额外清 poll/stage ticker）。
* 错误恢复：AJAX 翻页失败 → 整页刷新降级；SSE 重试耗尽 → 轮询降级；
  `safeAutoReload` 检测用户正在输入/有对话框打开时**不打断**输入，仅 toast 提示。
* 安全边界：所有不可信文本经 `esc`/`textContent` 注入（无 innerHTML 注入面）；
  前端限额只认服务端注入（无兜底副本漂移）。

**本次修复**：FIX-4（降级轮询无限重试 → 有界 + 有终态提示）、
**FIX-11（下列 5 条残留缺口一次性闭环）**。

**残留缺口 —— 5 条已全部闭环 ✅**：

| 优先级 | 问题 | 位置 | 处理 |
|---|---|---|---|
| ~~P1~~ ✅ | `window.PBC` 未加载时 `cancelJob`/`retryJob`/`deleteJob` 直接 `TypeError`，无降级 | `review.js`、`upload-jobs.js` | **FIX-11a**：新增 `static/pbc-fallback.js` 单一真值 shim（顺序无关、只补缺失）；三处模板引入 |
| ~~P2~~ ✅ | 两个对话框叠加时，先关闭者会把 `body.overflow` 复位，后者仍在显示但页面可滚动 | `confirm-dialog.js` `settle()` | **FIX-11b**：改为计数器 `scrollLocks`，归零才恢复 |
| ~~P2~~ ✅ | 错误 toast 用 `aria-live="polite"`，读屏用户可能延迟获知失败 | `confirm-dialog.js` `showToast` | **FIX-11c**：容器降为 `region`，逐条 toast `err→alert` / 其余 `→status` |
| ~~P2~~ ✅ | `max_bytes < 1MB` 时提示"文件超过 0MB 上限" | `upload.js` `limitMB` | **FIX-11d**：`formatBytes` 按量级选单位（B / KB / MB） |
| ~~P2~~ ✅ | `loadHistory` 对 `#history-list`/`#history-pagination` 无空指针保护，元素缺失即整页脚本中断 | `upload-jobs.js` | **FIX-11e**：提前返回 + 次要 UI 判空；`renderPagination`/`loadArchivedList` 同加守卫 |

> **一个设计决策值得记下**：11a 最初按"10 处薄封装"落地（在 `upload.js` /
> `upload-jobs.js` / `review.js` 各加一层守卫）。写完后发现 `window.PBC` 的
> 消费者是 **10 个模块**，各抄一份 14 行守卫**必然漂移**（这正是本项目
> "单一真值"约定要防的）。遂改为**一个 shim**，并**回退了那三处薄封装**。
> 教训：先数清消费者数量，再决定"封装"还是"兜底"。

---

## 3. 测试运行记录

| 项 | 结果 |
|---|---|
| 新增测试文件 | 13 个 = **279 条**（10 个见下 + FIX-11 批 3 个：`test_pbc_fallback_js.py` 23、`test_confirm_dialog_scroll_aria_js.py` 8、`test_upload_resilience_js.py` 11 = 42） |
| 既有文件新增用例 | `test_pipeline_state_machine.py` +4；`test_upload_jobs_js.py` +1（FIX-8 双帧） |
| 护栏复活 | `test_frontend_field_contract.py` 由 **0 收集 → 15 passed** |
| 前端 JS 行为套件（**18 文件**，node 实跑） | **358 passed** |
| 契约 / 卫生 / 共享件（4 文件） | **74 passed** |
| 变异验证 | `confirm-dialog` 2/2、`review-pageinfo` 8/8、`upload-jobs` 8/8、`review-pageview`+`review.html`+`suppressions` 19/19、`settings` 21/21、**FIX-11 批 15/15** —— **73/73 全部变红** |
| `tests/unit/` 全量（本轮实跑） | **3113 passed / 4 skipped**（517.33s） |
| `tests/integration/` 全量（本轮实跑） | **515 passed / 1 failed**（117.65s，见下） |

> 那 1 条失败是 `test_main_routes.py::TestServePdf::test_pdf_non_local_host_returns_403`
> —— **已定位为沙箱工具的产物，非仓库缺陷**（断言实际通过，失败发生在 `finally`
> 的 `unlink()`，被沙箱批量删除守卫 `SystemExit`）。
>
> 另一条 `test_repo_hygiene.py::TestNoShadowDirectories` 的失败在**本轮前半段**
> 出现（`.workbuddy-ai/` 触发"影子目录"护栏），已由 `.gitignore` 补 `.workbuddy-ai/`
> 修复（该护栏的 docstring 恰好预言了"agent 会往里写"）；上表 3113 passed 是
> **修复后**的数字，故已不含此失败。

---

## 4. 变更文件清单

**修改**
* `static/review.js` — FIX-2（两函数内写点前移）
* `static/review-locate.js` — FIX-3（实体解码顺序）
* `static/review-state.js` — FIX-5（删除死镜像）
* `static/review-progress.js` — FIX-4（降级轮询有界化）
* `static/upload-jobs.js` — FIX-7（状态点 class 收敛为单一函数，修复颜色冻结）、
  FIX-8（`created_at` 派生收敛 + trim）、**FIX-11e**（`loadHistory` /
  `renderPagination` / `loadArchivedList` 容器守卫 + 次要 UI 判空）
* `static/review-pageview.js` — FIX-9（圆点签名补 `total`）、
  FIX-10（JS 圆点补 `data-dot` 标记，与 SSR 统一）
* `templates/review.html` — FIX-10（SSR 圆点容器补 `data-dots="1"` 标记）、
  **FIX-11a**（引入 `pbc-fallback.js`，置于 `confirm-dialog.js` 之前）
* `templates/upload.html`、`templates/settings.html` — **FIX-11a**（同上）
* `static/confirm-dialog.js` — **FIX-11b**（滚动锁改计数器）、
  **FIX-11c**（容器降为 `role="region"`，逐条 toast 定 role）
* `static/upload.js` — **FIX-11d**（删 `limitMB`，新增 `formatBytes` 按量级选单位）
* `static/review.js`、`static/upload.js`、`static/upload-jobs.js` —
  FIX-11a 期间一度加入的 `window.PBC` 薄封装**已回退**（改为单一 shim，见 §维度 6）
* `static/settings.js` — 顺手修掉 1842 行一处既有的缩进错位（**仅空白**，
  1 行 diff；`size=77559 / lines=1861 / crlfLines=1861 / BOM 保留`，
  `node --check` 通过）
* `core/pipeline/state.py` — FIX-6（注释订正）
* `tests/integration/test_frontend_field_contract.py` — FIX-1（`NameError`）
* `tests/unit/test_pipeline_state_machine.py` — +4 条不变量
* `tests/unit/test_upload_jobs_js.py` — +1 条（FIX-8 的 `.trim()` 双帧可观测）
* `.gitignore` — 补 `.workbuddy-ai/`（影子目录护栏）

**新增测试**
* `tests/unit/test_review_empty_note_js.py`
* `tests/unit/test_review_locate_js.py`
* `tests/unit/test_review_state_js.py`
* `tests/unit/test_review_progress_js.py`
* `tests/unit/test_confirm_dialog_js.py` — 16 条
* `tests/unit/test_review_pageinfo_js.py` — 39 条
* `tests/unit/test_upload_jobs_js.py` — 40 条
* `tests/unit/test_review_suppressions_js.py` — 22 条
* `tests/unit/test_review_pageview_js.py` — 41 条
* `tests/unit/test_settings_js.py` — 31 条
* `tests/unit/test_pbc_fallback_js.py` — **23 条**（FIX-11a）
* `tests/unit/test_confirm_dialog_scroll_aria_js.py` — **8 条**（FIX-11b/11c）
* `tests/unit/test_upload_resilience_js.py` — **11 条**（FIX-11d/11e）

**新增生产代码**
* `static/pbc-fallback.js` — **FIX-11a**：`window.PBC` 降级 shim（IIFE，零依赖，
  只补缺失方法）。CRLF / 无 BOM，与同目录其余 16 个 `.js` 一致。

**新增/增强测试基础设施**
* `tests/js_harness.py` — 可复用假 DOM/定时器/网络 harness（`run_js` / `run_js_async`）。
  本轮为其补上：`style.setProperty`、`innerHTML` **扁平解析**、属性选择器
  `^=`/`$=`/`*=`、**后代组合选择器**（`#list li[data-id]`）、
  `dataset` **反射**为 `data-*` 属性 —— 缺任何一项，`findJobRow()` 与事件委托
  都会静默查不到元素，护栏退化成空断言。为 `settings.js` 又补 4 项：
  `cloneNode(deep)`（**不**复制监听器）、`replaceWith`（同步 `getElementById`
  注册表）、`cloneNode` 回灌 `dataset`、伪类 `:checked`（见维度 1 表格）。
  为 `pbc-fallback` 再补 1 项：`getElementById` 改为 **"注册表 + 树内查找"双路**
  —— `createElement` 不登记 id，运行期建出的 `#toast-container` 只查注册表会
  返回 null，护栏随即变成假失败或空断言。
