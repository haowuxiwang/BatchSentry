# 对抗性审查报告 — 2026-09-29

**范围**：`static/*.js`（17 文件 6700 行）、`core/` + `api/` 后端
**方法**：先定位 → 再修复 → 最后变异验证（每条修复都配基线金丝雀）
**口径**：本报告所有数字均为**本次实测/复跑**，不引用未复现的旧数字。
上一轮提出的 "141/141 变异" 因脚本未持久化、**不可复现，已废弃**，不计入台账。

---

## 一、上一轮遗留任务清单：逐条状态与阻塞原因

| # | 条目 | 状态 | 阻塞原因 / 处置 |
|---|------|------|----------------|
| 1 | `settings.js` 拆分为 6 文件（原 §维度 4） | **未开始** | **无技术阻塞**。1861 行（占前端 27.8%），已有 31 条测试兜底；纯体量问题，需一次性完成 + 全量回归。**本轮未动**。 |
| 2 | 后端 P0：`core/pipeline/__init__.py` god-hub + 190 处函数级 import（原 §维度 5） | ✅ **已闭环（本轮）——以"否证 + 护栏"结案** | 见 §3.2 / §五 修复 9。三条前提**全部不成立**：承重的函数级 import 只有 **25/1013**（98% 与环无关）；全仓真环**只有 1 个**且与扇出无关（已修）；改扇出要动 **217 处**锚点、仅 1 处危险 ⇒ **净负收益**。改做"把无环/分层/扇出面/可解析性变成 14 条可执行不变式"（变异 **17/17**）。 |
| 3 | 单一真值重复：状态分区（`ACTIVE_STATUSES` / `TERMINAL_STATUSES` / `_STUCK_STATUSES`） | ✅ **已闭环（本轮）** | 见 §五 修复 2。 |
| 4 | 单一真值重复：`_mask` / `_is_real_api_key` 手抄副本 | ✅ **已闭环（前轮 B4-4）** | 实测 `grep "def _mask"` 全仓仅 1 处定义（`api/settings/read.py:24`）；`_is_real_api_key` 已改为 `config._is_real_key` 的别名并留注释。 |
| 5 | `listings.py` 的 N+1 | ✅ **已结案（判定不构成缺陷）** | 代码内已带实测数据：20 job × 51 页 ≈ 1020 行 structured_json，逐 job 调用 5.3 ms vs 单条分组查询 4.5 ms，仅 **1.2×**；瓶颈是读取量而非往返次数 ⇒ 引入第二套判据的收益为负。**这是测量过的取舍，不是遗留缺陷。** |
| 6 | `analyze_cross_page` 从 `core/rules/__init__.py` 迁到 `rules/engine.py` | ✅ **已闭环（本轮）** | 见 §五 修复 7。**未**照原计划"与 #2 同批"——本轮独立闭环，并发现原计划引用了**不存在的模块** `core.pipeline.api`（见 §七 第 4 条）。 |
| 7 | 前端覆盖缺口 6 个 | ✅ **已闭环（前轮）** | `confirm-dialog` 16 / `review-pageinfo` 39 / `upload-jobs` 40 / `review-suppressions` 22 / `review-pageview` 41 / `settings` 31 条。 |
| 8 | UX 缺口 5 条（FIX-11） | ✅ **已闭环（前轮）** | 含新增 `static/pbc-fallback.js`。 |
| 9 | 跨页问题总览（前端入口） | ✅ **已闭环（本轮）** | 见 §五 修复 4。后端本就支持，本轮补齐前端视图并**用 `offset` 真正分页**。 |
| 10 | 单页 findings >limit 时条目不可达 | ✅ **已闭环（本轮）** | 见 §五 修复 3。 |
| 11 | 模态弹窗内方向键穿透底层页面 | ✅ **已闭环（本轮）** | 见 §五 修复 1。 |
| 12 | 多文件上传在 UI 上不可达 | ✅ **已闭环（本轮）** | 见 §五 修复 5。不是加 `multiple` 就完事 —— 需**重设计成功流程**（不跳转 + 串行 + 汇总）。 |
| 13 | `config.py` 去掉导入期 I/O（`config.py:951 config = load_config()`） | ✅ **已闭环（本轮）——以"否证 + 护栏"结案** | 见 §五 修复 6。实测确认副作用存在，但**惰性单例方案被证据否证**（收益≈0 / 风险 28 模块），且写盘行为是**被测试锁定的设计契约**；改做"把注释约束变成护栏"。 |

**小结**：13 条中 **11 条已闭环**（8 条本轮 + 3 条前轮），**1 条判定不成立**（#5），
**1 条未开始**（#1 `settings.js`）——属"大体量机械重构"，不难，但必须成批做 + 全量回归，
本轮选择不半途动它（半成品重构比不动更危险）。

> **#2 的处置口径**（与 #13 同型）：条目写的是"god-hub + 190 处函数级 import"，
> 听起来是个待修的后端 P0。但对抗性审查的第一步是**核验前提**。实测后结论是：
> 那 190 处里 **98% 与循环依赖无关**，全仓真环**只有 1 个**（`core.kb ⇄ retriever`，
> 与 `core.pipeline` 无关，已修），而"收敛扇出"要改 **217 处** patch 锚点。
> ⇒ **否证该方案**，改做真正有牙的部分：把**无环 / 分层方向 / 扇出面清单 /
> 产品源可无警告解析**钉成可执行契约。**否证一个方案也是推进。**

> **#13 的处置口径**：条目写的是"去掉导入期 I/O"，听起来是个待修的缺陷。
> 但对抗性审查的第一步是**核验前提**，而不是照单施工。实测后结论是：
> ① 副作用确实存在；② 但它**被 `test_config_internals.py:282` 显式断言**，
> 是有意的设计；③ 我原拟的惰性代理方案**行为收益≈0**（写盘只是从"导入时"
> 挪到"首次访问时"，启动期照写），代价却是 28 个生产模块 + 承重单例身份。
> ⇒ **否证该方案**，改做真正有牙的部分：把 `server.py` 里那条
> **只靠注释约束、零测试**的顺序不变式变成护栏。**否证一个方案也是推进。**

---

## 二、前端对抗性审查：阻碍用户体验的方面

### 优先级排序

| 优先级 | 问题 | 具体影响 | 证据（可复现） |
|--------|------|----------|----------------|
| **P0** | **单页 findings 超过 limit 时，第 51 条起在 UI 里完全不可达** | GMP 漏检：复核者看到 50 条就以为"本页就这些"。旧提示「请逐页翻页或处理后刷新」**两句都做不到** —— findings 是**页内**集合，翻页回来仍是同一批前 50 条；已裁决的 finding 只在渲染时变淡（`opacity-50`）**不会移出列表**。 | `api/review.py` 的 `order=confidence` 分支先取全页（≤2000）排序再 `[offset:offset+limit]` 切片，前端**从不传 `offset`**；仓库自带测试 `test_api_review.py::test_has_more_scoped_to_current_filter` 实测 57 条 → `count=50 / total=57 / has_more=True` |
| **P0** | **（已修）** 多文件上传在 UI 上不可达，且是静默丢弃 | 拖入 3 个 PDF，**只处理 1 个、另外 2 个无任何提示地消失**。后端 `MAX_CONCURRENT_JOBS=3` 的并发能力对用户完全不可见。 | `templates/upload.html:100` 的 `<input type="file">` 无 `multiple`；`static/upload.js:95`（change）与 `:119`（drop）都只取 `files[0]` |
| **P1** | **（已修）** 无跨页问题总览 | 300 页的批次里找 1 条 critical 要翻 300 次；无法回答"这批里最需要人看的是哪几条"。 | 后端 `/findings?order=confidence`（**不带 page**）已支持全量聚合 + 置信度排序；前端 `static/review.js` 两处 fetch **恒带 `page=`** |
| **P1** | **（已修）** 模态弹窗内方向键穿透到底层页面 | 在确认/输入弹窗里按 ←/→ 会翻走 PDF 与问题清单，用户以为只是在弹窗内移动光标。 | `static/review.js` 原 `keydown` 处理器无模态判定；两个 `document` 级监听器按**注册顺序**触发，`stopPropagation()` 对同目标无效 |
| **P2** | 上传遇 409 直接拒绝，**无排队** | 已有 3 个任务在跑时第 4 个被拒，用户必须盯着页面手动重试；`force` 路径只解决"重复文件"不解决"配额满"。 | `api/jobs/__init__.py` 两处 409（前置 best-effort + 锁内权威） |
| **P2** | 长任务进度只靠 SSE + 兜底轮询，无桌面通知 | 大 PDF 分析数十分钟，必须保持标签页前台才敢离开。 | `static/review-progress.js` 已有兜底轮询与连续失败上限（`TestFallbackPollingIsBounded`） |
| **P3** | `settings.js` 1861 行单文件 | 维护性：任何改动都要在近 2000 行里定位。已有 31 条测试兜底，**对最终用户无直接影响**。 | `wc -l` |

### 两条**经核查后撤回**的假设（不得作为缺陷上报）

1. **「tier-bar 看起来可点但点了没反应」** —— 核查 `templates/review.html` 与
   `static/review.css:59-83`：`.tier-bar` / `.tier-chip` / `.tier-dot` 无 border、
   无 hover、无背景，是**图例**（legend），**本来就不该可点**。不是缺陷。
2. **「复核页没有方向键翻页」** —— 实测存在（`static/review.js` 的 keydown 处理器）。
   真正的问题是它的**模态隔离**（已按 P1 修复），不是"没有"。

---

## 三、后端对抗性审查

### 3.1 分工是否明确 / 是否遵循 SRP / 是否模块化

**做到了的部分（不是空话，有可验证的单一真值点）**

- `core/pipeline/` 按阶段分文件：`stage1` / `stage2` / `stage3` / `ocr_support` /
  `self_heal` / `state` / `locks` / `engine`。职责边界基本清晰。
- 并发原语被收敛成两个**显式入口**：`db_lock`（逻辑原子性）与 `transition_status`
  （状态机唯一入口）。
- 中文映射**三处由机检锁定**：`core/zh_map.py`（后端）、`static/status.js`（状态）、
  `static/findings-map.js`（finding 级）—— 这是真正的单一真值实践。

**违反 SRP 的具体位置（按体量排序，均为实测行数）**

| 文件 | 行数 | 混在一起的职责 |
|------|------|----------------|
| `core/mineru_client.py` | **1301** | HTTP 客户端 + 重试 + failover + 响应解析 + 手写体清洗 + 线程池（`ThreadPoolExecutor(max_workers=min(n,4))`） |
| `core/page_analyzer.py` | **1066** | 页级编排 + 结果清洗 + LLM 交互 |
| `config.py` | **1049** | 配置模型 + **导入期文件 I/O**（`config.py:258` `_load_json_config()` + `:951` `config = load_config()`）+ 校验 —— 已核验为**有意的设计契约**，本轮以护栏固定其调用前提（§五 修复 6） |
| `core/pipeline/engine.py` | 713 | 编排 + 分片 + 队列消费 |
| `core/rules/__init__.py` | **208 → 65** | ~~规则聚合 + `analyze_cross_page` 实现（`:64`）~~ —— **已修**（§五 修复 7）：编排体迁到 `engine.py`，本文件回归"包导出面"；同时删掉 15 个**死导入**的 `_check_*` |
| `core/rules/engine.py` | **195**（新增） | 仅承担 `analyze_cross_page` 编排（原 `__init__.py:64-208` 的 145 行 + 契约说明） |
| `core/pipeline/__init__.py` | **59** | **re-export 了 38 个符号** —— 为兼容 monkeypatch 而存在的"枢纽" |

**`config.py` 的导入期 I/O —— 已核验，结论与初判不同（见 §五 修复 6）**

- `config = load_config()` 在**模块导入时**执行，实测 **28 个生产模块**顶层依赖它
  （`from config import config`）。导入耗时仅 **43–50 ms**，**不是性能问题**。
- **副作用实测**（`devlogs/_verify/probe_config_import_io.py`，每场景独立子进程）：

  | 场景 | 导入期写盘 | 改 `os.environ` |
  |---|---|---|
  | A 干净（无 `config.json` / 无 `.env`） | 否 | 否 |
  | B 仅存旧 `.env` | **创建 `config.json`**（迁移） | 是（注入 `DEEPSEEK_API_KEY`） |
  | C 有 provider 待自动切换 | **重写 `config.json`**（新增 `LLM_PROVIDER: glm`） | 是 |

- **⚠️ 更正一处前判**：此前把"导入期 I/O"列为那 190 处函数级 import 的**成因之一**，
  **实测不成立** —— `config.py` 只依赖 stdlib + `dotenv`，是**纯叶子模块**，
  不可能参与循环依赖。190 处函数级 import 的成因是
  `core/pipeline/__init__.py` 的 eager re-export（§3.2），与 config 无关。
- **写盘是设计，不是缺陷**：`tests/unit/test_config_internals.py:282` 明确断言
  `load_config()` 会调用 `_persist_env_to_config`（B4-4 系列同）。
  ⇒ 惰性化方案会同时打破这批测试与 `AUTO_ACTIVATE_NOTICE` 契约。
- **真正的风险点不在 config，而在调用方**：`AppConfig.port` 是**导入期快照**，
  而 `main.py:219 _cors_origins()` 在导入期由它生成 CORS 白名单。
  于是"`PORT` 必须先于 `import config` 进入 env"成为**硬约束**，
  却此前**只写在 `server.py:32-37` 的注释里、零测试**。本轮已补护栏。

### 3.2 模块化：原判断**已被否证** —— 函数级 import 不是"循环依赖的补偿"

> ⚠️ **本节的原始结论是错的，此处按实测重写。** 原判断："`core/` + `api/` 共 190 处
> 函数级 import，根因是 `core/pipeline/__init__.py` 的全量 eager re-export，
> 是循环依赖的补偿"。本轮按"**先核验前提、再动手**"的纪律做了可证伪核验，
> 三条前提**全部不成立**。

**核验 1：函数级 import 里有多少是"承重的"？**

判据（可证伪）：把 `A` 里的函数级 `import B` 提到顶层，**当且仅当**在顶层导入图里
`B` 能到达 `A` 时才会成环。对全仓建图后用 Tarjan SCC 逐条判定
（`devlogs/_verify/probe_pipeline_hoistability.py`）：

| 口径 | 数量 |
|---|---|
| 全仓本仓模块的函数级 import | **1013** 处 |
| 其中**承重**（提到顶层会成环） | **25** 处（24 → `core.pipeline`，1 → `core.rules`） |
| 其中**只是风格**（提到顶层不成环） | **988** 处 |

⇒ **98% 的函数级 import 与循环依赖无关**。"190 处"（原口径）本身还是**低估值**，
且把它归因为"环的补偿"属于**方向性错误**。

**核验 2：全仓到底有几个真环？**

顶层导入图 SCC 实测：**只有 1 个** —— `core.kb ⇄ core.kb.retriever`，且
**与 `core.pipeline` 扇出无关**。它一直没炸，是因为 CPython 3.7+ 对部分初始化
模块的 `IMPORT_FROM` 有 `sys.modules` 回退（"顺序恰好成立"的脆弱写法）。
✅ **本轮已修**（§五 修复 9），修完 SCC 数 = **0**。

**核验 3：改 `__init__.py` 的代价有多大？**

patch 锚点普查（`devlogs/_verify/probe_pipeline_patch_anchors.py`）：
`patch("core.pipeline.*")` 共 **217 处 / 12 个符号**；其中**只有 1 处是危险的
模块级绑定**（`tests/unit/test_pipeline.py:18`），7 个符号走**调用期**解析
（改子模块后自动生效），3 处锚在子模块全局，1 处锚的是标准库 `asyncio`。

**结论：原方案（删 34 个 re-export）是负收益** —— 它修不了任何环（核验 2），
却要动 ~217 处锚点。**否证一个方案也是推进**，故本节的可落地形态改为
**把真正有约束力的不变式变成可执行护栏**：

**✅ 替代交付物：`tests/unit/test_import_graph_contract.py`（14 条，17/17 变异被抓）**

1. **顶层导入图必须无环**（SCC 全部 size==1）。这条同时把 `core/pipeline/*.py`
   里那 24 处**调用期** import 变成"承重的"：谁把它提到顶层就立刻变红 ——
   而不是等某天导入顺序变了才炸（这类错误**平时是静默的**）。
2. **分层方向**：`core` 顶层不得依赖 `api`（实测 0 条）；`db` 是叶子（0 条）；
   反向对照 `api → core` **必须 ≥5 条**（否则"0 条"可能只是"什么都没扫到"）。
3. **`core.pipeline` 的 re-export 面是显式清单**（39 个名字 **+ 来源模块**），
   新增/改名必须显式改清单 —— 这才是本节真正要防的"**扇出悄悄变大**"。
4. **产品源必须无警告地解析**（非法转义序列 0 处）—— 因为导入图是"把每个文件
   `ast.parse` 一遍"得来的，**解析失败 = 静默缺边 = 假的无环**。

⚠️ 原计划里"约 200 处锚点必须先补行为化护栏"的告警**保留**（它是对的）：
将来若仍要收缩 re-export 面，必须按修复 7 的模式逐锚点补行为化护栏。
但**不再建议为"消除环"而做** —— 环已经没有了。

### 3.3 业务逻辑

- `transition_status` 是状态机唯一入口；**绕过它直写 status 的点都写了审计**
  （`VALID_TRANSITIONS` 直写路径）⇒ 这是**有意设计**，不构成 GMP 缺口。
- 状态分区本轮已单一真值化。⚠️ 注意一个反直觉陷阱：CPython 会**对同一 code object
  内相等的常量元组去重**，所以"另写一份字面量副本"与"别名"会是**同一个对象**，
  用 `is` 断言**永远抓不到漂移**。护栏必须断言**内容相等**（`set(a) == set(b)`）。

### 3.4 数据获取

- `listings.py` 的逐 job `_count_analyzed_pages` 是 N+1 形态，**但已实测结案**（见 §一 #5）：
  1.2× 差距，收益为负。**不要再为它开单。**
- `_live_jobs_snapshot` 有终态快照缓存（T6.2）：稳态下终态 job 每轮 5 条查询 → 0 条。
  且历史上 `status` 侧另有一份重复缓存，已删除（易漂移）。
- `list_findings` 的 `regions_json` 复用同一次 `page_cache` 查询（P0-3），
  没有为区域锚多打一次全表扫描。

### 3.5 数据库

- **模型：单一共享 aiosqlite 连接**（`db/client.py`），有 init 锁防重复建连
  （`_db is None` 检查与 `connect` 之间的竞态被注释显式点出）。
  `PRAGMA journal_mode=WAL` / `foreign_keys=ON` / `busy_timeout=5000`。
- `db_lock` 是 `asyncio.Lock`，作用是让**读-改-写**在**逻辑**上原子；
  **不是**防文件损坏（单连接 + SQLite 单写者已保证物理串行）。
- 实测覆盖：**20 个** `async with db_lock` 块；**57 条**字面量写语句
  （INSERT/UPDATE/DELETE/REPLACE），其中 **27 条在锁内、30 条在锁外**。
- ⚠️ **"不在锁内"是代理指标，会严重高报**：多数属于
  ① 被调用方（辅助函数**假设调用方已持锁**，如 `transition_status` 内部的
  `_transition_status_unlocked`）；② 启动期一次性写（如 `core/kb/store.py`）。
  **批量加锁是错的**，必须逐函数判定调用契约。本轮**未做**任何批量改动。

### 3.6 队列与异步任务

- **没有作业队列。并发上限是"配额"不是"排队"**：超过 `MAX_CONCURRENT_JOBS=3`
  直接返回 **409**。两处检查：前置 best-effort + **与 INSERT 同一把 `db_lock` 内的
  权威检查**（B2-7 修的 TOCTOU）。
- 每个 job 一个 asyncio Task；有引用计数锁 + `ChildTasks` **级联取消**。
  取消是**协作式**的（靠 `_is_cancelled` 轮询点），不是强杀。
- ⚠️ 易误读：`core/pipeline/engine.py` 里的 `asyncio.Queue` 是
  **OCR 分片结果队列**，**不是**作业队列。
- CPU 密集（GIL 受限）走 `core/procpool.py` 的 `ProcessPoolExecutor(max_workers=2)`
  （spawn，实测标定，注释带 B3-2 标定记录）。
- LLM 页级并发 `LLM_CONCURRENCY=5`（`config.py:924`），是**每 job 一个 Semaphore**。
- uvicorn **单 worker**：`server.py` 显式建 `Config`/`Server`，并**显式传
  `workers=1`**（`server.py:79`），与"进程内全局单连接 + `asyncio.Lock`"自洽。
  ⚠️ **若将来加 `workers`，`db_lock` 与单连接模型会立刻失效** —— 这是隐性耦合点。
  ✅ **本轮已闭环**：`server.py` 加了 11 行注释锁死该前提，并补 4 条护栏
  （见 §五 修复 6 的**补充**）。

---

## 四、当前实现是否支持"同时对 3 份文件进行审查"？

**结论：后端支持（且是设计值）；前端不支持；而且不能靠加一个 `multiple` 属性解决。**

### 后端：支持，且 3 就是设计上限

| 环节 | 实测值 | 位置 |
|------|--------|------|
| 同时活跃任务上限 | **3**（env `MAX_CONCURRENT_JOBS` 可覆盖） | `api/jobs/__init__.py:116` |
| 检查方式 | 前置 best-effort + **与 INSERT 同锁内的权威检查** | `api/jobs/__init__.py`（B2-7 修 TOCTOU） |
| 超出时行为 | **409 拒绝，不排队** | 同上 |
| 每 job 的 LLM 页级并发 | 5 | `config.py:924`（`LLM_CONCURRENCY`） |
| CPU 进程池 | 2（全进程共享） | `core/procpool.py` |
| uvicorn worker | 1 | `server.py`（显式 Config，未传 workers） |

限额本身有测试覆盖：`test_api_jobs_upload.py::TestUploadQuotaIsAtomic::
test_concurrent_uploads_stop_at_the_limit` 等
（`-k "concurren or quota or atomic or limit"` → **4 passed**）。

### 前端：**原为静默丢弃，本轮已修**（见 §五 修复 5）

**修复前**：`templates/upload.html` 的 `<input type="file">` 没有 `multiple`，
`static/upload.js` 的 change / drop 处理器都只取 **`files[0]`**
⇒ 用户拖入 3 个 PDF，**只有 1 个被处理，另外 2 个无任何提示地消失**。

**为什么"只加 `multiple`"是伪修复**（这条分析决定了修复方案）：

`uploadFile()` 的成功流程**硬编码为单文件语义**：

1. 成功后 `setTimeout(..., delayMs)` 后 **`window.location.href = /jobs/{id}/review` 跳转**
   —— 第 1 个文件成功就会**跳走，杀掉其余上传**；
2. 上传区被 `dropZone.style.pointerEvents = "none"` **禁用**；
3. 进度条是**单例**（`#upload-progress` / `#upload-progress-fill`），多文件会互相覆盖；
4. 状态行 `setStatus()` 是**单行**，N 次调用只剩最后一条。

⇒ 只加 `multiple` 会让"静默丢弃"变成"**竞态 + 跳转中断**"，**比现状更糟**。

**修复方案（已实施）**：多文件走独立路径 ——
**不跳转**（`noRedirect`）+ **串行**提交（共享的单例进度条才有意义，也避免一次打满
后端并发配额）+ 全部结束后给**一条汇总**，逐条进度由任务列表（已有 SSE 推送 +
`static/upload-jobs.js` 渲染）承载。单文件仍保留"上传完自动跳转复核页"的既有体验。

### 现在同时审查 3 份文件怎么做

- **一次选/拖 3 份**（已支持）：串行上传 → 3 个任务进入队列，任务列表逐条显示进度。
- 或**开 3 个标签页各传 1 份**（旧办法，仍可用）。
- 注意后端是**配额不是排队**：若已有 3 个任务在跑，第 4 个会被 **409 拒绝**。

---

## 五、本轮闭环的修复与验证证据

### 修复 1：模态弹窗内方向键穿透（前端）

- `static/confirm-dialog.js`：新增 `isDialogOpen()`，**以 DOM 为单一真值**
  （查 `[role="dialog"]`），并**无条件导出**（覆盖 fallback 的恒 false 降级版）。
- `static/pbc-fallback.js`：补 `isDialogOpen` 降级 shim（恒 `false`），
  只在 `confirm-dialog.js` **缺席**时留下。
- `static/review.js`：方向键处理器先问 `window.PBC.isDialogOpen()`，
  且**防御式取值**（该处理器每次按键都跑，一旦抛错会连带废掉全部键盘导航）。
- 为什么不靠 `stopPropagation()`：两个监听器都挂在 `document` 上，
  **同目标监听器不被 `stopPropagation` 阻断**，只能按注册顺序全跑
  ⇒ 隔离必须由**消费方**主动查询状态。
- 验证：`tests/unit/test_modal_keyboard_isolation_js.py` **14 passed**；
  变异 `devlogs/_verify/mutation_modal_keyboard.py` **6/6 被抓**（基线 14 passed）。

### 修复 2：状态分区单一真值（后端）

- `core/pipeline/state.py`：`ACTIVE_STATUSES` / `TERMINAL_STATUSES` 就地定义，
  `_STUCK_STATUSES = ACTIVE_STATUSES`（历史别名，与非终态**同集**）。
- `api/jobs/__init__.py`：删掉两份字面量副本，改为
  `from core.pipeline.state import ... as _ACTIVE_STATUSES / _TERMINAL_STATUSES`。
- `tests/unit/test_pipeline_state_machine.py`：新增
  `TestStatusPartitionIsSingleTruth`（3 条），并把原先**无效的 `is` 断言**
  换成**内容相等**断言（CPython 常量元组去重使 `is` 恒真 ⇒ 那是空断言）。
- 验证：该模块 **28 passed**；
  变异 `devlogs/_verify/mutation_status_partition.py` **4/4 被抓**（基线 OK）。
  - M1 api 退回字面量副本 → CAUGHT
  - M2 `ACTIVE_STATUSES` 混入未定义状态 → CAUGHT
  - M3 `TERMINAL_STATUSES` 漏掉 `archived` → CAUGHT
  - M4 `_STUCK_STATUSES` 漏 `cancelling`（漂移）→ CAUGHT

### 修复 3：findings 截断导致条目不可达 + 误导文案（前端）

- `static/review.js`：新增单一常量 `FINDINGS_PAGE_LIMIT = 200`（= 后端
  `min(limit, 200)` 上限），**两处 findings fetch 都显式带上 `&limit=`**；
  并把 `findingsData.total` 传给渲染函数。
- `static/review-findings.js`：`renderFindings(findings, hasMore, total)`；
  截断提示改为**报真实数字**（"本页共 57 条，按置信度仅显示前 50 条，尚有 7 条未显示"），
  **删除两句做不到的旧建议**，改指向**真含全部 findings 的出口 —— 报告导出**
  （`api/report.py` 的查询无 `LIMIT`）。`total` 缺省时降级为不报数字，但**绝不**退回无效指引。
- 验证：`tests/unit/test_findings_truncation_js.py` **12 passed**；
  变异 `devlogs/_verify/mutation_findings_truncation.py` **8/8 被抓**（基线 OK）：

```
[BASELINE OK]
M1  CAUGHT   review.js 第 1 处 fetch 丢掉 &limit=（退回后端默认 50）
M2  CAUGHT   FINDINGS_PAGE_LIMIT 从 200 降回 50（恢复原缺陷量级）
M3  CAUGHT   renderFindings 调用点漏传 total（提示静默降级为无数字）
M4  CAUGHT   missing 恒为 null（不再报真实缺口数字）
M5  CAUGHT   提示退回无效指引「请逐页翻页或处理后刷新」
M6  CAUGHT   提示删掉报告导出出口（指路消失）
M7  CAUGHT   边界判据 > 写成 >=（total==shown 时报出 0 条缺口）
M8  CAUGHT   findings-list 查不到（渲染整体失效，所有'不含'断言将假绿）
==== 8/8 被抓 ====
```

**修复 3 的残留边界（如实记录）**：`limit=200` 把单页可见上限从 50 提到 200；
若**单页**超过 200 条，仍有截断 —— 此时提示会报出真实缺口数字并指向报告导出，
**不再静默**。彻底解法是跨页总览视图（§一 #9）—— **本轮已实施，见修复 4**。

### 修复 4：跨页问题总览（前端，P1）

后端本就支持，缺的只是前端入口。本轮补齐，并**用 `offset` 真正分页**。

- `templates/review.html`：问题清单 header 加 `#all-findings-toggle`（"全部页"），
  下方新增 `#all-findings-panel`（`#all-findings-summary` / `#all-findings-more` /
  `#all-findings-list`）。
- `static/review-findings.js`：新增 `OVERVIEW_LIMIT = 200` + `overviewRow()` /
  `overviewSummaryText()` / `loadOverviewPage()` / `setOverviewOpen()` / `initOverview()`。
  - 请求 `/findings?order=confidence&limit=200&offset=N`（**不带 `page`**）；
    `加载更多` 按**已读条数**推进 offset，**追加**渲染（不是覆盖）。
  - 摘要用后端 `total`（不是已读条数）；`has_more` 为假时隐藏"加载更多"；
    **加载失败渲染错误文案 + toast**（绝不静默）；空结果有明确文案。
  - 行内 `onclick="goPage(N)"` 跳回该条所在页（页码先 `Number()` 归一）。
  - 描述/类型/来源全部经共享件 `esc` 转义；映射仍走 `findings-map.js`（与页内同源）。
- `static/review.js`：`DOMContentLoaded` 里防御式调用 `R.findings.initOverview()`。

**三个设计决策（都写进了代码注释）**

1. **独立容器，不给 `#findings-list` 加"全部"模式** —— 页内清单是 `loadPageData` /
   SSE `refreshCurrentPageFindings` 的重渲染目标，加模式就要在每个刷新点插分支；
   且 `#tier-bar` 的文案与 `title` 都写着"本页"，与全批数字并排显示会自相矛盾。
   展开总览时**隐藏**页内清单与 tier 图例，收起时原样恢复。
2. **必须用 `offset`，不是"把 limit 提到 200"** —— 后者只是把天花板从 50 挪到 200；
   排序键 `(confidence, id)` 确定，故 `offset` 续读安全且能取到**全部**条目。
3. **总览行是只读的、故意比页内行简单** —— 页内行带裁决/修正/定位按钮，那是**权威
   操作面**；在总览复制一套操作按钮就会多出一个必须同步的操作面（漂移源）。

**本轮测试反过来抓出我自己新代码里的一个真 bug**（值得记）：
`loadOverviewPage` 的 `catch` 块引用了只在 `overviewRow` 里解构的 `esc`
⇒ **错误处理本身抛错 ⇒ 失败完全静默**（正是该用例要防的形态）。
已改为 fail-closed：拿不到 `esc` 就**不渲染**动态文本，绝不注入未转义内容。

- 验证：`tests/unit/test_all_findings_overview_js.py` **22 passed**；
  变异 `devlogs/_verify/mutation_all_findings_overview.py` **12/12 被抓**（基线 OK）。

### 修复 5：多文件上传（前端，P0）

- `templates/upload.html`：`<input type="file">` 加 `multiple`；提示文案补"可一次选择/拖入多份"。
- `static/upload.js`：
  - change / drop 两个入口改为 `Array.from(...files)` → `submitFiles(files)`；
  - 新增 `rejectReason(file)` —— **校验的单一真值**（`uploadFile` 与多文件预检共用，
    避免两处扩展名/体积判据漂移）；
  - 新增 `submitFiles(fileList)`：单文件走原路径（保留自动跳转）；
    多文件走 **不跳转 + 串行 + 汇总**；`needs_setup` 只弹**一次**对话框；
    不合格文件**一次性**列出名字；全部不合格时**只报拒绝、不走批次汇总**；
  - `uploadFile(file, force, opts)` 新增 `opts`：`noRedirect` / `progress` / `onDone`；
    新增 `settleOnce` 幂等守卫（本函数有 7 条退出路径，逐条手写 `onDone`
    极易漏调 ⇒ 队列卡死，或重复调 ⇒ 队列跳跃）；
  - `releaseDropZone()` 收敛上传区状态（多文件路径下由 `submitFiles` 整批结束后恢复，
    避免中途某个失败短暂放开拖拽、用户插入新批次与串行队列交错）。
- `tests/js_harness.py`：新增可编程 **XHR / FormData / File** 桩。
  ⚠️ 上传路径走 `XMLHttpRequest` 而非 `fetch`，此前 harness 未提供
  ⇒ **该路径从未被行为测试覆盖**。桩**不自动结算**（由测试显式 `flushOne()` /
  `flush()` 推进）—— 自动结算会让"串行 vs 并行"不可观测。

**测试又抓出两处我自己的真缺口**（值得记）：

1. 混合选择时**只报了被跳过文件的"份数"、没报名字** ⇒ 用户不知道哪几份没传。
2. 把名字加进即时提示后仍然没用 —— **紧接着的逐份状态会把它覆盖掉**
   （`上传中 ok.pdf…`）。最终改为写进**批次汇总**（唯一持久可见的位置）。
   ⇒ 教训：**"报了"不等于"用户看得到"**，必须断言在**最终可见**的文本上。

- 验证：`tests/unit/test_upload_multifile_js.py` **28 passed**；
  变异 `devlogs/_verify/mutation_upload_multifile.py` **11/11 被抓**（基线 OK）。

**本次 `MISSED` 的分诊记录（M8）**：去掉"全部不合格时提前返回"这个守卫后，
其余断言**全部仍然通过** —— 因为 `skippedNote` 已保证名字出现在汇总里。
但差异是**真实**的：状态行会变成「已提交 **0** 份文件，下方任务列表逐条显示分析进度」，
把"什么都没传"说成"提交完成"。⇒ 这是**断言缺口**（不是无效变异），
故**补断言** `test_all_rejected_does_not_claim_a_batch_was_submitted`，重跑后 11/11。

### 修复 6：`import config` 的顺序不变式 —— **否证原方案 + 把注释变成护栏**（后端）

**定位（先核验前提，再动手）**

条目原文是"`config.py` 去掉导入期 I/O"。先做**实测核验**，而不是照单施工：

```
devlogs/_verify/probe_config_import_io.py    # 三场景 × 独立子进程
devlogs/_verify/probe_config_port_freeze.py  # 顺序是否有牙
```

| 场景 | 导入期写盘 | 改 `os.environ` |
|---|---|---|
| A 干净 | 否 | 否 |
| B 仅存旧 `.env` | **创建 `config.json`** | 是 |
| C 有 provider 待自动切换 | **重写 `config.json`**（新增 `LLM_PROVIDER: glm`） | 是 |

⇒ 副作用**确实存在**，且导入耗时仅 43–50 ms（非性能问题）。

**解决（= 否证 + 改做有牙的那部分）**

1. **否证"惰性单例"方案**，三条独立理由：
   - **收益≈0**：惰性代理只是把写盘从"导入时"推迟到"首次属性访问"，
     而首次访问就在启动路径上 ⇒ **启动期照写**，用户可观察行为不变。
   - **契约冲突**：`tests/unit/test_config_internals.py:282` **显式断言**
     `load_config()` 调用 `_persist_env_to_config`（B4-4 系列同）
     ⇒ 该写盘是**有意的设计**，不是意外。
   - **代价不对称**：28 个生产模块 + 承重单例身份（`update_config` 就地改
     `config["app"]`）+ `core.pipeline.config` monkeypatch 锚点。
2. **同时更正一处前判**：`config.py` 是**纯叶子模块**（仅 stdlib + `dotenv`），
   **不可能**是那 190 处函数级 import 的成因（§3.1 已改）。
3. **改做真正有牙的部分**：`server.py:32-37` 的注释写着
   "必须在 import config 之前 setdefault(PORT)"，**零测试**。而它有真实故障后果：

   ```
   probe_config_port_freeze.py:
     before → config_port = 59999 → CORS = http://127.0.0.1:59999   ✅ 一致
     after  → config_port =  8000 → CORS = http://127.0.0.1:8000    ❌ 与监听端口 59999 不一致
   ```
   顺序错了就是**监听端口与 CORS 白名单不一致** ⇒ 浏览器访问设置页的
   POST/PUT 被 preflight 拦掉。

**新增护栏**：`tests/unit/test_config_import_order_contract.py`（**16 passed**）

| 类 | 覆盖 |
|---|---|
| `TestHarnessIsNotVacuous`（4） | 探针能跑、env 真被注入、锚点存在、提取器非空 |
| `TestPortIsFrozenAtImport`（3） | **行为化**：before→59999 / after→8000 / CORS 跟着冻结值走 |
| `TestServerSyncsPortBeforeImportingConfig`（2） | **源码顺序**：`setdefault("PORT")` 必须先于 `from config import config` |
| `TestCorsDerivesFromConfigPort`（3） | CORS 白名单**派生自** `config["app"].port`，无硬编码端口字面量 |
| `TestSingleWorkerPrerequisite`（4） | 见下方**补充**：`uvicorn.Config(workers=1)` 是正确性前提 |

⚠️ 顺序类断言**锚定调用点**（`^\s*from config import config\b`），不锚标识符 ——
`server.py:32-37` 的注释里就含 "import config" 字样，锚错即假绿。
⚠️ 行为化用例必须走**独立子进程**：`config` 一旦进 `sys.modules`，
同进程内无法再观察"导入顺序"的差别。

**补充（§七 第 7 条，同文件同处理）**：`server.py` 还有一条**只写在代码里、
零测试**的前提 —— `uvicorn.Config(workers=1)`。它不是性能取舍而是**正确性前提**：

- 本进程的并发模型建立在「单进程内全局单例」上 —— `core/pipeline/locks.py:127` 的
  `db_lock`（进程内全局单锁）、`core/procpool.py:52/54` 的 `_pool` /
  `_POOL_MAX_WORKERS = 2`（进程内单池）、job 配额计数与 pipeline task 注册表。
- 多 worker ⇒ 每进程各一把锁、各一个池 ⇒ 配额**按 worker 数翻倍**
  （"与 `INSERT` 同一把 `db_lock` 内的权威检查"不再权威）、CPU 池上限变 2×N、
  跨 worker 的取消与级联取消全部失效。

做法：`server.py:68` 加 11 行注释说明上述失效链，并补 4 条护栏
（`uvicorn.Config` 实参文本必须含 `workers=1` / 全文件 `workers=` 只许取 1 /
被保护的两个单例确实是模块级定义 / 提取器防空转）。
⚠️ 锚点仍是**实参文本**而非裸 `workers` —— 新加的注释里就出现了 "worker" 字样。
**顺带更正报告自身的一处错**：§3.6 原写"`server.py` 显式建 `Config`/`Server`
（**不传 `workers`**）"，实测代码**显式传了 `workers=1`** —— 已改。

- 验证：**16 passed**；回归 `tests/unit/test_config*.py`（6 个现存文件）**167 passed**；
  变异 `devlogs/_verify/mutation_config_import_order.py` **10/10 被抓**（基线 OK，
  M7–M10 为本轮新增：调大 worker 数 / `db_lock` 降级 / 池上限改动 / 调用形状变化）。

**本次 `MISSED` 的分诊记录（harness bug，非断言缺口）**：首跑报 0/6，日志显示
**每条变异都产生了预期的失败**。根因是我的结果解析正则
`^FAILED [^\s:]+::(\w+)` 抓到了 `FAILED path::TestClass::test_name` 里的
**类名**而非用例名。⇒ 属**无效 MISSED**（harness 缺陷），修正则为
`^FAILED .*::(\w+)\s*$` 后 **6/6**。
**教训：变异脚本的"结果解析"本身也要做防空转 —— 否则 0/6 会被误读成"护栏全无牙"。**

### 修复 7：`analyze_cross_page` 迁出包 `__init__` + 7 个 patch 契约护栏（后端）

**定位**

`core/rules/__init__.py` 208 行里，**145 行**是编排体（`:64-208`），只有 63 行是导出面。
一个"包的 `__init__`"同时承担**导出面**与**Stage-3 编排入口**两件事 ⇒ SRP 违规。

另外机检发现（`devlogs/_verify/probe_rules_init_dead_imports.py`）：该文件顶层导入了
**15 个 `_check_*` 规则函数**，而编排体走的是**注册表**（`spec.check(pages, ctx)`），
**一个都没调用** ⇒ 纯死导入，还让 `from core.rules import _check_time_reversal_in_page`
**意外可用**（误导性 API）。

**风险定位（这一步是关键，不能省）**

编排体原先住在 `core.rules` 里，因此它解析的 **7 个协作者都是包命名空间的全局名**。
多处脚本/测试正是**按包路径**打桩：

| patch 目标 | 调用方 |
|---|---|
| `core.rules._llm_fallback_check` | `scripts/replay_rules.py`、`scripts/eval_findings.py`、`test_cross_page_entry_coverage.py`、`test_rule_registry.py` |
| `core.rules._llm_based_check` | 同上 |
| `patch.object(core.rules, "enabled_rule_specs", …)` | `scripts/replay_rules.py:79-82` |
| `core.rules._normalize_pages` / `_collect_per_page_findings` / `_build_summary` / `RuleContext` | 经 `from core.rules import …`（devlogs 等） |

⇒ 若在 `engine.py` 里改成**模块级** from-import，这些桩会**静默失效**：
测试转而**调用真 LLM**、跑**全部规则** —— **不报错**，只是变慢变贵或失败。
这就是"机械替换会让一批测试静默失效"的**具体机制**。

**解决**

1. 新建 `core/rules/engine.py`（195 行）：只放编排体；7 个协作者在**函数内**
   `from core.rules import ... as _run_*` **调用期**解析（本仓库既有约定，
   见 `core/pipeline/__init__.py` 模块 docstring）。
2. `core/rules/__init__.py` 收敛为**导出面**：公开 API（`analyze_cross_page`、`SpecBounds`）
   + 一段**带注释的"调用期解析契约"块**（那 7 个名字，勿删勿下沉）。
3. 删除 15 个死导入 `_check_*`（依据上述机检脚本）。
4. 垫片 `core/cross_page_analyzer.py` 与 `core/pipeline/__init__.py` **不动** ——
   `core.pipeline.analyze_cross_page` 的锚点身份因此天然保持。

**验证**

- 新增 `tests/unit/test_rules_engine_patchability.py`（**14 passed**）：
  不满足于"属性存在"这种空断言，而是对 7 个名字**逐一**断言
  **"打桩后 `analyze_cross_page` 的可观察行为确实改变"** —— 用**哨兵值**做信号
  （只有真走了包命名空间，哨兵才会出现在结果里）；
  另有防空转：未打桩时四个哨兵**一个都不许出现**，且注册表**非空**（否则替身断言失去对照）。
- 回归：cross-page / rules / pipeline 两批共 **869 passed**（426 + 443）。
- 下游实测：`scripts/replay_rules.run_rule_layer([], m4_on=True/False)` 两个分支均正常返回
  ⇒ 脚本里的三个包级 patch 目标**仍然有效**。
- 变异 `devlogs/_verify/mutation_rules_engine_patchability.py`：**9/9 被抓**（基线 14 passed OK）。
  核心变异形态 = "把某个协作者从调用期解析改成模块级绑定"（桩静默失效的真实形态）。

**本次 `MISSED` 的分诊记录（M8 —— 断言缺口）**

M8（把 `_check_*` 加回包 `__init__`）首轮**漏网**。分诊：**不是无效变异**，
是我的检测器写错了 —— 它按"行首是否以 `_check_` 开头"判定，只能抓到原来的
**多行括号式**导入，抓不到**单行式** `from m import _check_x`。两种写法都能让符号
泄漏进包命名空间 ⇒ **判定为断言缺口**，检测器改为 **AST 解析**（与格式无关），重跑 **9/9**。
**教训：源码类护栏若用"行首前缀/字符串"匹配，就会被**排版**骗过；符号级的约束要用符号级工具（AST）。**

### 修复 9：全仓唯一真环 `core.kb ⇄ core.kb.retriever` + 把导入图变成可执行契约（后端）

> 编号说明：**修复 8**（并发额度预检）属于第二轮并发专项，正文在 **§八**。

**定位**

§3.2 的核验顺带做了一次全仓顶层导入图 SCC 普查，结果是：**真环只有 1 个** ——

```
core/kb/__init__.py:13    from core.kb.retriever import (...)   →  core.kb.retriever
core/kb/retriever.py:19   from core.kb import store             →  core.kb
```

它一直没炸，只是因为 CPython 3.7+ 对**部分初始化模块**的 `IMPORT_FROM` 有
`sys.modules` 回退：`core.kb` 先执行到 `:13` 时把 `retriever` 拉起来，此时
`core.kb` 已在 `sys.modules` 里（半初始化），`from core.kb import store` 于是"恰好"
拿到后来才绑上的 `store`。这是**"顺序恰好成立"的脆弱写法**，不是设计 ——
任何一次 `__init__.py` 的行序调整都可能把它变成 `ImportError`。

**解决**

- `core/kb/retriever.py` 改为**直连子模块**：
  `from core.kb.store import entries, source_ids, sources`
  （`core/kb/store.py` 本身是**刻意的零依赖叶子**，直连无环），
  3 处调用点 `store.entries()` / `store.source_ids()` / `store.sources()` → 直接调用。
  无默认参数别名，故导入名与原属性访问**功能等价**。
- 顺手修掉一个**同源缺陷**：`core/md_render.py:13` 的 docstring 写了
  `` ``\`行内代码\`` `` —— `\`` 是**非法转义序列**（3.11 只报 DeprecationWarning，
  **3.12+ 变 SyntaxWarning**），而且 Markdown 也是错的（代码段内反斜杠是字面量）。
  正确写法是 `` `` `行内代码` `` ``，已改。

**验证**

- 新增 `tests/unit/test_import_graph_contract.py`（**14 passed**）—— 见 §3.2 的四条不变式。
  其中"无环"一条现在**专门守着** `core/pipeline/*.py` 那 24 处调用期 import：
  谁把它们提到顶层，立刻变红。
- 变异 `devlogs/_verify/mutation_import_graph.py`：**17/17 被抓**，基线 14 passed OK，
  且 sha256 自证真文件未被改动。
- 修完 SCC 数 **1 → 0**。

**口径说明（容易踩）**

本仓 `core/ api/ db/ scripts/` 里**一处相对导入都没有**（实测，全为绝对导入）。
所以契约文件里的"相对导入解析"分支是**纯防御性**的 —— 没有任何真实代码会把它
暴露出来，它**只由合成用例钉住**。故该分支必须区分两种写法（子模块 vs 仅 re-export
的符号），并用两条用例分别锁死（变异 M10 / M11 验证）。

### 回归

| 套件 | 结果 |
|------|------|
| `test_pipeline_state_machine.py` + `test_modal_keyboard_isolation_js.py` | **42 passed** |
| 本轮 4 个新护栏（overview / truncation / modal / state） | **76 passed** |
| 上传 + 复核相关护栏（10 个文件，含 harness 变更回归） | **183 passed** |
| 全部 review 相关 JS 护栏（14 个文件） | **258 passed** |
| 复核页相关集成（`test_frontend_field_contract` / `test_api_review(_coverage)`） | **61 passed** |
| 并发限额相关（`-k "concurren or quota or atomic or limit"`） | **4 passed** |
| config 相关 7 个文件（含本轮新护栏） | **170 passed** |
| cross-page / rules / pipeline 两批（含本轮新护栏） | **869 passed**（426 + 443） |
| EOL / BOM 完整性（字节级，本轮全部改动文件） | **无混合换行、无 BOM** ✅（口径更正见 §六 教训 11） |
| 并发额度三件套（`quota_js` / `jobs_js` / `quota_contract`） | **79 passed**（§八 修复 8） |
| 上传 + JS 模块族（`multifile` / `resilience` / `review_js_modules` / 上述三件套） | **127 passed** |
| EOL / BOM 完整性（§八 全部改动文件，字节级） | **无混合换行、无 BOM** ✅ |
| 导入图契约 + `md_render` + `kb_retriever`（修复 9） | **60 passed** |
| 导入图变异验证（`mutation_import_graph.py`） | **17/17 被抓**，基线 14 passed OK |
| 全仓 260 个 `.py` 非法转义序列普查 | **0 处**（修复 9 前为 1 处） |
| `test_config_import_order_contract.py`（修复 6 补充：单 worker 前提） | **16 passed**（原 12） |
| 配置导入顺序变异（`mutation_config_import_order.py`） | **10/10 被抓**（原 6/6） |

---

## 六、变异验证台账（本轮新增）

| 脚本 | 条数 | 结果 | 基线 |
|------|------|------|------|
| `devlogs/_verify/mutation_modal_keyboard.py` | 6 | **6/6 被抓** | OK |
| `devlogs/_verify/mutation_status_partition.py` | 4 | **4/4 被抓** | OK |
| `devlogs/_verify/mutation_findings_truncation.py` | 8 | **8/8 被抓** | OK |
| `devlogs/_verify/mutation_all_findings_overview.py` | 12 | **12/12 被抓** | OK |
| `devlogs/_verify/mutation_upload_multifile.py` | 11 | **11/11 被抓** | OK |
| `devlogs/_verify/mutation_config_import_order.py` | 10 | **10/10 被抓** | OK |
| `devlogs/_verify/mutation_rules_engine_patchability.py` | 9 | **9/9 被抓** | OK |
| `devlogs/_verify/mutation_concurrency_quota.py` | 14 | **14/14 被抓** | OK（79 passed） |

**本轮踩到并已固化的教训**

1. **`MISSED` 要分诊**：`_STUCK_STATUSES = tuple(ACTIVE_STATUSES)` 是**无效变异**
   （CPython 里 `tuple(t) is t` 为真，行为与原代码完全一致），**不是断言缺口**。
   正确处理是**改变异**（改成真实漂移）并**同时**把空断言换成内容相等断言。
2. **变异锚点必须与实际文件的 EOL 一致**（⚠️ 本条的**理由**已更正，见教训 13）：
   工作区 EOL **不是**统一的 —— 实测 `*.py` 是 396 LF / 57 CRLF。多行锚点写错 EOL
   会 `count == 0`，变异**静默不生效** ⇒ 假绿。
   **正确做法**：用**字节锚点** + `assert count == 1`（让"锚点没命中"**大声失败**），
   并用 `_eol_of(raw)` 从文件自身探测换行符，不要假设。
3. **源码断言要锚"调用点/URL 模板"，不能锚裸标识符**：`order=confidence` 裸串
   会命中**函数内注释**（本轮的提示注释里就出现了这个词），
   锚成 `findings\?page=\$\{[A-Za-z]+\}&order=confidence` 才唯一。
4. **防空转断言是前提**：M8 专门验证"若 `#findings-list` 查不到（渲染整体失效），
   所有『不含某字符串』的断言都会假绿" —— 这类断言必须先证明提取器真的在工作。
5. **转义类断言不能锚"子串不存在"**：`onerror=alert` 在**已转义**的输出里依然存在
   （转义只处理尖括号）⇒ 断言"该子串不存在"是**假失败**。正确信号是"载荷的尖括号
   变成了实体"（`&lt;img ... &gt;`）。**本轮初版即写错，被自己抓出。**
6. **错误处理路径本身要能被测试**：`catch` 块里引用未定义变量会让"失败处理"抛错
   ⇒ 失败**完全静默**。这类 bug 只在**失败用例**里暴露 —— 必须有专门的
   "失败必须可见"用例（修复 4 的 `test_failure_is_visible_never_silent`）。
7. **"报了"不等于"用户看得到"**：把信息写进会被**下一次 `setStatus` 覆盖**的位置
   等于没报。断言必须锚在**最终可见**的文本上（修复 5 的
   `test_all_rejected_names_appear_in_one_message` —— 首版只写进即时提示，被逐份状态覆盖）。
8. **串行/并行必须有"即时副作用"可观测**：测试桩若自动结算，两者不可区分。
   `__xhr` 桩**不自动结算**，靠 `__xhr.sent.length` 在 `change` 之后**立刻**读出
   "只发出 1 次请求"来证明串行。
9. **多退出路径的异步函数要用幂等结算守卫**：`uploadFile` 有 7 条退出路径，
   逐条手写 `onDone` 必然漏调（队列卡死）或重复调（队列跳跃）
   ⇒ `settleOnce` 一次性收口；递归重试（409 force）必须**透传 `opts`**。

10. **变异脚本的结果解析本身要做防空转**：本轮 `mutation_config_import_order.py`
   首跑报 **0/6**，而日志显示每条变异**都**产生了预期的失败 —— 根因是解析正则
   `^FAILED [^\s:]+::(\w+)` 抓到了 `FAILED path::TestClass::test_name` 里的
   **类名**。⇒ "0/6" 与"护栏全无牙"是两个完全不同的结论，
   必须先确认**解析器**能正确提取用例名（正则 `^FAILED .*::(\w+)\s*$`），
   再谈变异结果。**这是 harness 缺陷，不是断言缺口。**
11. **一个"待修条目"可能本身就建立在错误前提上**：本轮 #13 的实测既确认了副作用，
   又**否证了原拟方案**（惰性代理的启动期行为不变）与**前判**（`config.py` 是叶子模块，
   不可能是 190 处函数级 import 的成因）。⇒ **否证一个方案也是推进**；
   把"照单施工"换成"先核验前提"，否则会做出一堆无收益且高风险的改动。
   同族：本轮 #6 的"计划"引用了**不存在的模块** `core.pipeline.api`
   （实测 `ls core/pipeline/` 无此文件）—— 计划文本本身也会腐坏。
12. **源码类护栏用"行首前缀/字符串"匹配会被**排版**骗过** —— 修复 7 的 M8 首轮漏网：
   检测器按"行首以 `_check_` 开头"判定，只抓得到**多行括号式**导入，
   抓不到**单行式** `from m import _check_x`。符号级约束要用**符号级工具**（AST），
   不要用文本模式。**判定为断言缺口，不是无效变异。**
13. **EOL 口径更正（字节级实测，推翻此前两次判断）** —— 全仓 git 跟踪的文本文件：
   **LF 239 / CRLF 72 / MIXED 0 / BOM 5**。即：
   ① 仓库**两种 EOL 并存**（LF 才是多数），并非"工作区一律 CRLF"；
   ② 唯一真不变式是 **"单个文件内不混合"**（MIXED=0），
   **不是**"必须 CRLF"，也**不是**"CRLF 行数 == 总行数"这种依赖工具实现的代理指标；
   ③ 带 **BOM 的有 5 个文件**（`api/jobs/actions.py`、`api/settings/probe.py`、
   `api/settings/provider.py`、`static/settings.js`、`tests/unit/test_page_analyzer.py`），
   **不止 `settings.js` 一个**。
   ⇒ 正确做法：**字节级**统计 `CRLF / lone_LF / lone_CR` 三个量，只要求"不混合、无意外 BOM"；
   新文件随大流（LF）即可，**不要为 EOL 做 churn**。

14. **"用例全过"与"一条用例都没跑"在失败集上完全一样**（都是空集）——
    必须显式区分。`mutation_concurrency_quota.py` 首版对每条变异只打印
    `实际失败 （无）`，无法分辨这两种情形，于是 M13/M14 的"变异没生效"
    被读成了"护栏没牙"。⇒ 现在脚本在 `len(res) == 0` 时**大声中止**
    （`[HARNESS DEFECT]`）。**同族：Trap 48 的"0/N 通常先是采集器坏了"。**

15. **`sys.path` 前置**不是**重定向模块导入的可靠手段**：被测项目的
    `tests/conftest.py` 自己会 `sys.path.insert(0, PROJECT_ROOT)`，
    把临时工作目录顶到后面 ⇒ 变异副本**静默不生效**（假的 MISSED）。
    可靠做法是**直接注册进 `sys.modules`**（`spec_from_file_location` +
    `module_from_spec` + `sys.modules[name] = mod` + `exec_module`），
    命中缓存后完全绕过 `sys.path` 的先后之争；并**自证**
    `sys.modules[name].__file__` 指向变异副本。

16. **假时钟会让"时间类"断言变成恒真断言**：假 DOM harness 的时钟只在测试
    显式 `__advanceClock()` 时才走 ⇒ "每份各等 30min（= N×30min）"这种
    **耗时**断言**看不出来**（`waitedMs == 31min` 恒真）。能区分的信号是
    **"有没有新排定时器"**（`__timers` 增量）或"有没有新发起副作用"。
    ⇒ 断言"某事没发生"时，必须选一个**若发生了就立刻可见**的信号（本仓库
    的既有约定），"耗时没变长"不满足这个条件。

17. **源码子串断言会因"同文件别处也有同一子串"而假绿**：
    `assert "ACTIVE_STATUSES.includes(st)" in src` 在本文件另有 2 处调用点
    ⇒ 删掉目标那处后断言**照样成立**（M10 首轮 MISSED）。
    ⇒ 源码断言只能用于**结构/顺序**（"调用点 A 在调用点 B 之前"），
    **口径/取值类**约束必须落到**可观测输出**上（这里是
    `window.PbcJobs.quota().active`）。

18. **一个"不可观测的变异"往往是"重复实现"的味道**：M7（删掉
    `waitForQuotaSlot` 里的熔断短路）首轮 MISSED，根因是同一语义在
    `uploadFile` 预检里**也写了一遍** ⇒ 短路**永远不可达**。
    ⇒ 分诊时要问一句"这段代码真的可达吗"：**不可观测的变异**有两种处理，
    改变异（换成可达的形态）**或**删掉冗余实现（让唯一那份可观测）。
    本轮选了后者，并因此把熔断判定收敛到**唯一判定点**（§8.3.1）。

19. **基线金丝雀会抓到 harness 自己的缺陷**（修复 9）：变异脚本作为文件运行时
    `sys.path[0]` 是脚本目录，`import core.pipeline` 直接 `ModuleNotFoundError`
    ⇒ 基线就红。**这正是"先跑基线"的价值**：否则 16 条变异里那条"恒红"的用例会
    被当成"这条守卫很强"，而其余 15 条的结论也一并不可信。

20. **失败集比较必须用"用例名"，不能用"用例名 + 异常后缀"**（修复 9 的 M14）：
    我把异常类型拼进失败名（`… [SyntaxError]`），再用它做集合包含判断，
    结果一条**全红**的变异被判成 `MISSED`。⇒ 分诊结论是**比较口径缺陷**，
    不是护栏缺口。**"格式化的展示串"不要复用到"逻辑比较"上。**

21. **关掉一个分支不等于制造了那个 bug —— 先看它是否可达**（修复 9 的 M12）：
    我把 `visit()` 里"跳过函数/类体"的 `continue` 关掉，断言"函数体内的 import
    会被扫到" —— 没有。因为 `visit()` **只对 `If`/`Try` 递归，从不进入函数体**，
    那个 `continue` 是**无副作用的双保险**。⇒ 分诊为**无效变异**，改正后
    （**显式加** `visit(node.body)`）立刻被抓。同时把代码注释改成描述**真实机制**，
    免得下次有人以为"有那个 continue 就安全"。

22. **期望集要按"守卫的适用范围"写，不能想当然**（修复 9 的 M3）：
    我往 `core/page_analyzer.py` 注入 `import api`，却期望**两条**用例变红 ——
    其中一条只检查 `core.pipeline.*`，**正确地没有触发**。⇒ 这是**期望集写错**
    （不是守卫漏），拆成 M3a/M3b 后两条守卫各自被独立验证。
    **教训：期望集是"断言"，也要能被否证；写宽了会掩盖"守卫其实不覆盖那条路径"。**

---

## 七、下一步建议（按收益/风险排序）

1. ~~跨页问题总览~~ ✅ **已完成**（§五 修复 4）。
2. ~~多文件上传流程重设计~~ ✅ **已完成**（§五 修复 5）。
3. ~~`config.py` 去掉导入期 I/O~~ ✅ **已结案 —— 方案被证据否证**（§五 修复 6）：
   副作用实测存在，但①写盘是**被测试锁定的设计契约**（`test_config_internals.py:282`）；
   ②惰性代理的**启动期可观察行为不变**（收益≈0）；③代价是 28 个生产模块 + 承重单例。
   ⇒ 改做"把 `server.py` 的注释约束变成 12 条护栏"（已落地，变异 6/6）。
   **若将来真要动**，正确形态是"把 `_persist_env_to_config` 从 `load_config()` 里
   移到启动检查点（lifespan）"，并同步改写 B4-4 那批断言 —— 这是一次**独立批次**，
   需先与"启动期自动切换必须持久化"的产品意图确认，不宜顺手做。
4. ~~`analyze_cross_page` 迁移~~ ✅ **已完成**（§五 修复 7）——
   **未**照原计划"与 #2 同批"：它可独立闭环，且本轮实测发现原计划引用的
   `core.pipeline.api` **不存在**（`ls core/pipeline/` 无此文件）。
   ⇒ 已改为"7 个协作者调用期从 `core.rules` 解析"，并用 14 条护栏 + 9 条变异锁定该契约。
5. ~~`core/pipeline/__init__.py` 扇出收敛~~ ✅ **已结案 —— 方案被证据否证**（§3.2）：
   前提核验三条**全部不成立** —— ①函数级 import 只有 **25/1013** 处承重（98% 与环无关）；
   ②全仓真环**只有 1 个**且与扇出无关（已修，§五 修复 9）；③改扇出要动 **217 处**
   锚点、其中只有 1 处危险。⇒ **修不了任何环，却要改 200+ 处锚点，净负收益。**
   替代交付物 = `tests/unit/test_import_graph_contract.py`（14 条 + 17/17 变异）：
   把"**无环**""**分层方向**""**扇出不得静默变大**""**产品源必须可无警告解析**"
   变成可执行不变式。**这才是原条目真正要防的东西。**
   ⚠️ 原告警**保留**：将来若仍要收缩 re-export 面，必须先按修复 7 的方法逐锚点补
   行为化护栏 —— 但**不再以"消除环"为理由**（环已经没有了）。
6. **`settings.js` 拆分**（大）：见 §一 #1。1861 行、31 条测试兜底，需一次做完 + 全量回归。
7. ~~在 `server.py` 注释锁死"单 worker"前提~~ ✅ **已完成**（§五 修复 6 的**补充**）：
   加了 11 行注释说明失效链（`db_lock` / 进程内单例池 / 配额计数 / 级联取消），
   并在同一契约文件里补 4 条护栏（变异 M7–M10 验证，10/10 被抓）。
   **顺带更正报告自身**：§3.6 原写"不传 `workers`"，实测**显式传了 `workers=1`**。
8. **跨页总览的后续增强**（小，可选）：`#all-findings-panel` 目前无 `severity`/`status`
   过滤与关键词搜索 —— 后端 `status` 过滤参数已支持，接上即可。
9. **上传批次的并发提交**（小，可选）：目前串行（1 份传完再传下一份）。后端配额是 3，
   可改成"最多 3 路并发 + 每路独立进度"，代价是单例进度条要拆成每文件一条。
   当前串行的好处是进度可读、不撞配额，**不建议在没有实测收益前改**。
   ⚠️ 但串行 + 配额**会撞**（选 >3 份时第 4 份必 409）—— 这一点本轮已定位并修复
   （**见 §八**），与本条不是同一件事：修的是"撞上之后白传 + 报红"，不是"改成并发上传"。

---

## 八、并发专项（第二轮）：上限调研 → 前端体验 → 修复 → 验证

> 起因：需要回答"系统最多支持多少并发"，并评估前端在并发场景下的体验。
> 全程遵循"先定位 → 再解决 → 最后验证"，全部结论**从源码/运行期实测取得**，
> 不引用任何记忆中的数字（本轮实测就推翻了仓库注释里的一个关键归因）。

### 8.1 系统最多支持多少并发？（可复现结论）

**结论：应用层硬上限 = 3 个 job**（`MAX_CONCURRENT_JOBS`，默认 3，env 可覆盖）。
**第 4 个起直接 409 拒绝，不排队、不降级**。配额之下真正**先**成为瓶颈的，
是 Stage 2 的 **15 路并发 LLM 请求**（每 job 5 × 3 job，**无全局闸门**），
**不是内存**。

取证脚本：`devlogs/_verify/probe_concurrency_ceiling.py`（P1 静态普查 + P2–P5 实测）

| 限制器 | 值 | 作用域 | 超限表现 | 取证 |
|---|---|---|---|---|
| job 配额 `_MAX_CONCURRENT_JOBS` | **3** | 全局（DB 行计数） | **409 硬拒绝** | P2 |
| LLM 并发 `llm_concurrency` | **5** | **per-job**（函数内建 `Semaphore`） | 页分析排队；**全局无闸门** | P3 |
| CPU 进程池 `_POOL_MAX_WORKERS` | **2** | **进程全局**（模块级单例池） | Stage 0 排队 | P4 |
| MinerU 分片池 | 4 | per-job（`min(n,4)`） | 上游限流 | P1 |
| `db_lock` | 1 | 进程全局单锁 | **串行排队**（瓶颈，不拒绝） | P5 |
| uvicorn `workers` | **1** | 进程 | —— （`db_lock` 模型以单进程为前提） | P1 |
| 复核页渲染 | 无全局上限 | per-job `threading.Lock` | 线程池排队 | 代码审读 |

**实测数据**

- **P2（job 配额是硬拒绝）**：6 个内容互不相同的 PDF **并发**上传 →
  **3 成功 / 3 × 409**，采样到的**峰值活跃数 = 3**，最终库里 3 行。
  409 文案：`已有 3 个任务在处理中，上限为 3。请等待完成或取消后再试。`
- **P3（LLM 信号量是 per-job）**：跑**真实** `_run_stage2_analysis`（只 stub 网络调用）
  - 单 job 12 页 → 在途峰值 **5**（per-job 上限成立）
  - 3 job × 5 页并发 → 全局在途峰值 **15**，墙钟 0.16s
  ⇒ **3 × 5 = 15**，证实"信号量在函数内新建、无全局闸门"。这是配额之下
  **最先打爆的上游资源**（LLM 服务商的速率/并发限制）。
- **P4（进程池是全局 2 worker）**：6 个并发 `run_cpu`（每任务 0.4s）→ 池
  `max_workers=2`，父进程侧 6 个同时在等。⇒ 3 个 job 同时进 Stage 0 时，
  **第 3 个在池上排队**（与 `bench_procpool.py` 的标定一致：2×3 相对 1×3 加速 1.64x）。
- **P5（`db_lock` 是串行而非限流）**：8 个协程争抢 → 峰值持锁数 **1**。
  它是**吞吐瓶颈**，不是并发上限。

**内存维度：默认 3 **不是**内存推导出来的（仓库注释的归因有误）**

`api/jobs/__init__.py:111-113` 的注释是默认值 3 的**唯一**理由：

> "Each pipeline holds the OCR result + LLM JSON in memory; 3 concurrent 200MB
>  PDFs with multi-page OCR results can hit ~2GB."

实测（`devlogs/_verify/probe_concurrency_memory.py`，样本 = 仓库里**真实存在**的
138 MB / 51 页扫描件 `output/2878c84d-999/丝裂霉素提取批记录.pdf`，与真实 DB）：

| 项 | 实测 | 性质 |
|---|---|---|
| M1 `fitz.open` + **逐页触碰**（51 页） | **1.10 MB** | 常驻 |
| M2 规范化重渲染（`_normalize_zoom` + `csGRAY`） | **48.4 MB** | **瞬时**，在 procpool 子进程内 |
| M3 复核页渲染（`_pdf_render_zoom` + `csRGB`） | **51.7 MB** | **瞬时**，受 2000px 硬上限约束 |
| M4 文本层（OCR 结果 + LLM JSON 全量反序列化） | **1.02 MB** | 常驻 |

⇒ 单 job ≈ **102 MB**（其中瞬时占 100 MB），3 并发 ≈ **221 MB**（含 procpool 2×59MB）。
⇒ 注释的 `~2GB` **量级成立但归因错误**：
  1. **文件体积几乎不进内存**（138 MB 文件逐页触碰后仅 +1.1 MB —— fitz 惰性加载）；
  2. 注释点名的 "OCR result + LLM JSON" 实测 **1.02 MB**，可忽略；
  3. 真正的内存项是**瞬时 pixmap**，而它由 **procpool 的 2 worker + 渲染像素上限**
     封顶，**与 `MAX_CONCURRENT_JOBS` 无关**。
⇒ 把 `MAX_CONCURRENT_JOBS` 调大不会等比例涨内存，调到 1 也省不下多少。
   **默认 3 的取值有内存依据，但不是精确推导**；真正的内存护栏在别处。

> ⚠️ **本探针第一版给出的是错数（632 MB），差点被用来"证明"一个不存在的内存危机。**
> 第一版自己编了渲染参数（`Matrix(300/72, 300/72)` + `csRGB`），而生产规范化路径用的是
> `_normalize_zoom()`（受 `_NORMALIZE_MAX_SIDE_PX=4096` / `_NORMALIZE_EXTREME_MAX_SIDE_PX=8192`
> 约束）+ `csGRAY`（**1** 字节/像素，不是 3）。⇒ **测量必须调用生产函数取参数，
> 不能自己复述一遍参数** —— 否则误差可以是 10 倍，且方向恰好迎合"内存紧张"的直觉。

### 8.2 并发场景下的前端体验：定位到的问题

| # | 问题 | 优先级 | 具体影响 | 定位 |
|---|---|---|---|---|
| F1 | **多文件 >3 份时第 4 份起必然失败，且失败前已完整上传** | **P1** | 用户选 5 份 ⇒ 前 3 份成功、后 2 份各**白传整个文件**（大扫描件分钟级）后报错，必须重选重传 | `upload.js:submitFiles` 串行队列在**上传结算**时推进（不是 job 结束时）；后端配额 3 ⇒ 必撞 |
| F2 | **配额 409 被当成"上传失败"（红色错误），且无额度可见性** | **P1** | 语义错（容量暂时不足 ≠ 失败）；用户不知道 3 份上限的存在，无法预判；没有任何"等空位"路径 | `upload.js` 409 分支只识别**去重** 409（`/已上传过/`），配额 409 落到通用 `setStatus("上传失败: HTTP 409: …", "err")` |
| F3 | **409 时文件选择被丢弃** | P2 | `settleOnce(false)` + `releaseDropZone()` ⇒ 用户必须重新选、重新读盘 | 同上分支 |
| F4 | ~~3 个 job 的 SSE/轮询会撞 HTTP/1.1 每域 6 连接上限~~ | **不成立** | 上传页用**单条聚合 SSE** `/api/jobs/live` 推送全部活跃任务；复核页每页 1 条，3 页 + 1 上传页 = 4 条 < 6；降级轮询是 10s/页且有 6 次失败上限 | 经代码审读 + 既有注释确认，**设计已规避**，不作为缺陷 |

**F1 为什么是 P1（而不是"用户操作不当"）**：产品的卖点就是"批量审查"，
`<input multiple>` 是明确支持的入口，而 UI 上**没有任何**关于 3 份上限的提示。
⇒ 任何 >3 份的合法操作都**确定性地**部分失败，且代价是已经付过的传输时间。

### 8.3 修复 8：并发额度预检 + 额度 409 不再当失败

**设计（三件事，单一真值贯穿）**

1. **额度下发前端**（后端 → `window.__PBC__.concurrency.limit`）
   - `main.py` 新增 `_max_concurrent_jobs()`，**调用期**从 `api.jobs._MAX_CONCURRENT_JOBS` 取值。
   - ⚠️ 不能写成模块级 `from api.jobs import _MAX_CONCURRENT_JOBS`：那会把值**冻结在
     import 时刻**，`MAX_CONCURRENT_JOBS` env 覆盖与测试 patch 全部失效 ⇒
     "界面显示 3、后端实际 5"的静默漂移（护栏：`test_injected_limit_is_resolved_at_call_time`）。
2. **活跃数登记**（`upload-jobs.js`，单一写点）
   - 它已经是 `/api/jobs/live` 聚合帧的唯一持有者 ⇒ 在 `onFrame` 里维护
     `liveStatuses: Map<jobId, status>`，按 `ACTIVE_STATUSES` 过滤后计数，
     经 `window.PbcJobs.quota()` 暴露。
   - ⚠️ **必须按状态过滤**：推送体还含近 10 分钟内的**终态** job（行状态切换需要），
     直接 `jobs.length` 会**高估** ⇒ 明明有空位却一直等（把可用性换成假死）。
   - ⚠️ 登记必须**早于**行渲染短路（`if (!li) return`）：当前页没有该行的活跃 job
     同样占着后端额度，挂在渲染循环里会漏计。
   - 降级轮询路径**同样**维护登记表，否则 SSE 断线后 `active` 冻结在最后一帧。
3. **发送前预检 + 额度 409 的有界重试**（`upload.js`）
   - 预检放在 `uploadFile` **内部**（而非 `submitFiles`）：单文件路径对 1 份**直接调
     `uploadFile`**，若只在 `submitFiles` 里预检，"拖 1 份"与"拖 2 份"行为不一致。
   - **fail-open 是硬约束**：共享件缺失 / `limit<=0`（未注入）/ 数值非法 / `quota()` 抛错
     ⇒ 一律**跳过预检**直接发。绝不能因为一次注入失误把上传功能锁死。
   - **常规路径保持同步**：只有"真的满额"才切异步等待，既有"pick/drop 后立刻发请求"
     的时序不变（避免给每次上传都加一次微任务跳变）。
   - 等待参数：轮询 **2s**（与后端 SSE 帧同频，更快只是空转）、上限 **30min**
     （与 procpool 单任务超时 1800s 同量级）。
   - **超时熔断**：额度是全局的，一轮超时后同批剩余每一份都会撞同一堵墙 ⇒
     置位后立即失败，否则整批 N 份 = **N×30min**。解除点**只有一个**
     （`submitFiles` 的批次边界）—— 刻意不在 `quotaSnapshot()` 里"顺手自愈"，
     那会与它重复且悄悄削弱熔断。
   - **额度 409**（≠ 去重 409）：`info` 级提示 + 等空位 + **有界**自动重试
     （`QUOTA_RETRY_MAX=2`，每次重试都要重传整个文件，不能无限次），
     `opts` 透传以保证队列继续推进（与既有的去重 force 分支同一套约定）。
   - **两个 409 不合并**：去重 409 要问用户（重传会重新完整分析），额度 409 不该问。

**未做（明确记录，避免"看着像漏了"）**：没有把上传改成 3 路并发
（§七 第 9 条的理由仍成立）；没有做"服务端排队"（会把 409 语义改成等待，
是产品决策，不在本轮范围）。

#### 8.3.1 修复 8 的收敛：熔断判定**去重**（变异验证逼出来的改动）

首版把"本批已等超时过"这条熔断**写了两遍**：`uploadFile` 的预检里一份
（提前失败 + 专属文案），`waitForQuotaSlot` 里一份（短路返回 `false`）。
变异验证（M7）暴露了后果：**删掉 `waitForQuotaSlot` 里那份，测试全绿** ——
因为预检那份先拦住了，第二份**永远不可达**。

按本仓库的"单一真值 / 单一实现"约定（同 §8.3 里刻意不做的
`quotaSnapshot()` 自愈），已收敛为**唯一判定点放在 `waitForQuotaSlot`**：

- 它既是**置位者**（超时时 `quotaWaitTimedOut = true`）也是**判定者** ⇒ 对
  **所有**调用路径成立（预检路径 + 额度 409 的重试路径），比在单个调用点判定
  更强（首版只护住了预检路径）；
- 预检里只保留"满额 ⇒ 提示 + 调 `waitForQuotaSlot()`"，不再重复判定。

⇒ 副作用：被熔断的文件现在显示的是 `…等待超时 — …`（此前是预检那份专属文案）。
这是**有意的**：批粒度上"等待超时"是事实，且它让 M7 变成**可观测**变异
（见 §8.4）。

### 8.4 验证（#20）

**新增护栏 39 条**

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/unit/test_upload_quota_js.py` | **31** | 防空转（4）/ fail-open（5）/ 同步快路径（2）/ 满额不发请求（6）/ 超时与熔断（4）/ 额度 409 语义与有界重试（7）/ **识别器排他性（1）** / 发布侧顺序与发布名（3） |
| `tests/unit/test_upload_quota_contract.py` | **6** | 注入存在性、**调用期解析**、与 409 文案同源、既有 `limits` 注入未被挤掉、防空转 |
| `tests/unit/test_upload_jobs_js.py::TestQuotaPublisher` | **2** | 活跃数**行为**口径（只数 `ACTIVE_STATUSES`）、终态后 `active` 回落 |

**每个测试文件都带防空转用例**：
`kind()` 能区分 err/info（否则"不是 err"类断言无判别力）、`PbcJobs.quota()` 确实被读到、
基线有空位时确实发请求（否则"满额没发"也可能是"根本不会发"）、
注入点 `window.__PBC__` 存在、`limit` 确实从注入读到（为 0 则预检恒放行）。

**变异验证 14/14 全部被抓**（脚本 `devlogs/_verify/mutation_concurrency_quota.py`，
基线 79 passed，真文件哈希自证未被改动）。台账与教训见 §六。

首轮跑出 **8/13，5 条 MISSED**。**5 条全部做了分诊**（"MISSED 必须分诊"是本仓库的
硬纪律）—— 结论是 **1 条 harness 缺陷 + 2 条无效变异 + 2 条断言缺口**，
**没有一条是"护栏没牙"**：

| 变异 | 首轮 | 分诊结论 | 处理 |
|---|---|---|---|
| M13 `main.py` 冻结字面量 | MISSED | **harness 缺陷**：`tests/conftest.py:26` 自己 `sys.path.insert(0, PROJECT_ROOT)`，把临时工作目录顶掉 ⇒ 测试拿到的仍是**真** `main.py`（变异根本没生效）。复现脚本：`devlogs/_verify/diag_m13_import.py`（它证明"变异副本可导入"但"pytest 里的 `main` 仍是真仓库的"） | 改为**直接注册进 `sys.modules`**（`_install_mutant_main`），并加"加载到的确实是变异副本"自证 |
| M14 模板不注入 | MISSED | 同上（同一个 `main` 缓存导致模板目录仍指向真仓库） | 同上 |
| M5 合并两个 409 识别器 | MISSED | **无效变异**：去重分支在额度分支**之前**且带 `!force` 守卫 ⇒ force=false 时合并识别器**在行为上不可观测**（那是结构性防线） | 把判别用例换成**直接对生产识别器求值**的新用例（`test_quota_recognizer_does_not_swallow_a_dedup_409`），并另加 M15 覆盖"合并"的另一方向 |
| M7 去掉等待超时熔断 | MISSED | **断言缺口（判别信号选错）**：假时钟只在测试显式 `__advanceClock` 时才走 ⇒ "N×30min"在时钟上**看不出来**，`waitedMs == 31min` 是**恒真**断言 | 换成**新排定时器数**（`__timers` 增量）作判别信号；同时按 §8.3.1 去掉重复的熔断判定，使该变异可观测 |
| M10 `active` 口径退化 | MISSED | **断言缺口（源码子串假绿）**：`ACTIVE_STATUSES.includes(...)` 在本文件另有 2 处调用点，删掉 `recountQuota` 里那处后子串断言**照样成立** | 改为在 `test_upload_jobs_js.py` 里做**行为**断言（`window.PbcJobs.quota().active`），源码断言只保留"顺序"这一条 |

**回归**

| 套件 | 结果 |
|------|------|
| 并发额度三件套（quota_js / jobs_js / quota_contract） | **79 passed** |
| 上传 + JS 模块族（`multifile` / `resilience` / `review_js_modules` / 上述三件套） | **127 passed** |
| 变异验证基线（同上三件套） | **79 passed** |
