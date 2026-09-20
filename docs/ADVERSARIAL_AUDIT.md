# 对抗性审查报告（2026-09-16）

> 触发：用户提出「构建物是否可分发 / 日志与状态机是否完善 / 流式输出是否合范 /
> 设置能否正常设置 / pipeline 是否无阻塞 / 鲁棒性与泛化与抗挫折如何 / 知识库是否完善 /
> 是否要引入"进化机制" / 视觉交叉对比能否提精度」。
>
> **纪律**：本报告只写有证据的结论，每条带 `file:line` 或实测命令。凡"没有 X"都
> 说明搜索范围。凡"测不到"就写"测不到"。**不引用厂商基准当作自己的结论。**
>
> 方法：四路只读审查（状态机与日志 / 设置与配置 / 管线阻塞 / 知识库）+ 本机实测
> （真实库 2 份、规则层离线重放、降噪量测脚本）。本机 Python 3.11.9。
>
> **2026-09-16 续（本报告的结论已被自己的修复追过时，逐处同步）**：
> - §5 的"超时清单"漏了**本地 CPU 重活** —— `run_cpu` 此前**没有任何超时**，是全链路唯一
>   能造成"永久非终态"的入口，且 `max_workers=1` 会把单 job 卡死放大成整个应用停摆。
>   已修（`core/procpool.py` + 回归用例），报告 §0/§2/§3/§5/§6/§11 相应更新。
> - 新增调研：`docs/RUNTIME_WATCHDOG.md`（看门狗三问判据、超时覆盖面矩阵、分层方案）。
> - 新增实测：`docs/VISUAL_CROSSCHECK.md`（视觉交叉对比的**前置条件与结论**）。
> - 新增实测缺陷：`tests/e2e_frozen.py` 存在**假绿**（pipeline 走到 `error` 也判 PASS），已修。
> - **看门狗 P1「周期巡检」已实施（2026-09-16，v1.1.4）**：§2 的 P0「无运行时看门狗」
>   （`recover_stuck_jobs` 只在启动时调用）**已关闭** —— 心跳绑定前向进展（非定时器）、
>   分层阈值、后台扫描、`GET /api/health/watchdog` 自述端点。实施后回审又修 5 项
>   （3 项严重），见 `docs/RUNTIME_WATCHDOG.md` §8。故 §2 表格与本文"速览"里
>   "无看门狗 / SSE 无兜底"均为**审查时点**结论，现状以 §11 与看门狗文档为准。

---

## 0. 一页结论

| 问题 | 结论 | 证据强度 |
|---|---|---|
| 构建物能否分发 | **技术上可以**（便携版 + 分发一致性机检在），但**从未在干净机器上验证过**，且打包链在本机被安全软件驱动级阻塞 | 中（有产物与机检，无他机验证） |
| 日志与状态机是否完善 | **状态机是显式的、有审计、有崩溃恢复**；**周期巡检仍缺**（`recover_stuck_jobs` 只在启动跑）—— 但"永久非终态"的**唯一无上限入口已封**（`run_cpu` 超时 + 池回收，见 `docs/RUNTIME_WATCHDOG.md`） | 高（实测代码路径 + 回归用例） |
| 流式输出是否合范 | **架构合范**（单一常量、终态快照、三色面板）；**但 SSE 的终止性没有兜底**（同上无看门狗） | 高 |
| 设置能否正常设置 | **能设、且热生效**（config.json 原子写 + LLM 客户端重置）；**但有 1 个 P1 会让用户"设了不生效"**（provider 被启动逻辑静默改写） | 高 |
| pipeline 是否无阻塞 | **无 P0**（本轮补上遗漏的最后一项）：外部调用**与本地 CPU 重活**都有超时，慢 job 不饿死他者；仍有 3 处 P1/P2（`self_heal` 的 GIL 阻塞、Stage3 同步 CPU、procpool 全局串行） | 高 |
| 鲁棒性 / 泛化 / 抗挫折 | 尺寸/方向/坐标/清晰度/形态 5 个维度**都有阈值与降级**，且原则一致（宁缺勿错）；进程级抗挫折的**兜底已补**（`run_cpu` 超时 + 池回收），剩余缺口是"自动巡检"而非"等待上限" | 中高 |
| 知识库是否完善 | 语料 6 源 441 条、金标 36/37=97.3% 全绿、已入包并有门禁；**空洞**：2 个死键 + 2 个规范类型无词表、gmp2010 无 license/无可复现 raw | 高 |
| 是否引入"进化机制" | **不需要新机制**；需要的是**评测基准 + 回归门禁**（见 §7） | 高（依据为本项目的实测） |
| 视觉交叉对比能否提精度 | **能，但先决条件是 R4 标注集** —— 否则无法度量"提升"。已有 VLM 第四读的可行性结论（引用 §1） | 高（已有实测） |
| R1–R3 降噪 | **本轮已实现并实测**：真实轮次 442 → 272（−38.5%），critical 与高价值类型零损失 | 高（可复现脚本） |

---

## 1. 构建物能否分发？

**有产物、有机检、有 e2e 驱动；缺"他机验证"与"打包链可用性"。**

已具备：
- 分发一致性机检（`tests/unit/test_distribution_parity.py` 一类）与产物 e2e
  （`tests/e2e_frozen.py`，版本断言从 `main.APP_VERSION` 派生）。
- `e2e_proc.resolve_exe()` + `PBC_E2E_EXE` 覆盖 —— 修掉了历史缺陷"测了 A、发了 B"。
- 便携交付物 `win-unpacked/BatchSentry.exe`（~180 MB），不产 NSIS。

真实障碍（**本机实测，非推断**）：
1. **外部进程持有** `resources/app.asar`：`tasklist` 查不到该进程（**排除法在此失效**）、
   重启不释放 →
   🔁 **归因更正（2026-09-17 实测）**：持有者是**宿主进程 WorkBuddy**（Restart Manager
   具名，pid 16220 / 14048），**不是安全软件**，且**只有 `*.asar` 被占**
   （同目录 `pbc-server.exe` / `BatchSentry.exe` 全部空闲）。详见 `PROJECT_PITFALLS.md` §二十二。
   electron-builder 只能不断换**全新输出目录**（自愈目录是一次性的）。
2. **PyInstaller 被 safe-delete 钩子直接打死** → 构建必须显式关掉钩子。
3. **从 agent shell 起 `BatchSentry.exe` 必然 ~1s rc=0 退出**（非交互式会话，
   对照实验证明旧包同样如此）→ GUI 子系统只能**人工双击**验证。

**未验证的部分（必须说清）**：没有一份"干净 Windows 机器"上的安装-启动-跑一份
PDF"记录。所以"能分发"目前的证据是**构建产物的一致性**，不是**跨机可运行性**。

---

## 2. 日志与状态机是否完善？

### 已具备（这是本项目做得最扎实的一块）
- **显式状态机 + 合法转移表**：`core/pipeline/state.py:35-46`；非法转移被阻断并写审计
  `state.py:74-97`。
- **终态兜底**：`CancelledError → error`、`InvalidTransitionError` 按当前态归位
  `core/pipeline/engine.py:300-364`。
- **崩溃重启恢复**：`state.py:138-226`，启动调用 `main.py:80`，用 `created_at` cutoff
  防止把新任务误判为陈旧（`main.py:76`）。
- **结构化日志 + 轮转**：格式含 request/job（`logging_config.py:107`）；10MB×5 三文件轮转
  （`logging_config.py:135/147/159`）；`request_id` 中间件 `main.py:184-214`。
- **job_id 贯穿 OCR 链路**：`core/ocr_client.py:24`、`core/mineru_client.py:46` 挂 JobIdFilter。
- **终态快照缓存键 =(status, finished_at)**，可失效且唯一一份 `api/jobs/status.py:170-198`。

### 真实缺陷

| 级别 | 现象 | 证据 | 影响 |
|---|---|---|---|
| **P0** | **无运行时看门狗**：`recover_stuck_jobs` **只在启动时调用** | `main.py:80`；搜 `watchdog/heartbeat/periodic/recover_stuck` 无其他命中 | 单进程长跑时 pipeline 异常逃逸或上游挂死 → job 永久停留非终态 → **SSE 无限等待**（`status.py:310-346`）。这是"日志与状态机"里唯一的 P0 |
| ↳ **已修一半（2026-09-16）** | **"等待无上限"已消除**：`run_cpu` 补超时 + 超时回收进程池（`core/procpool.py`，默认 1800s / `PBC_CPU_TASK_TIMEOUT_S`）。此前的实测结论是——所有外部调用都有上限，**唯独本地 CPU 重活（Stage 0 规范化）没有**，且进程池 `max_workers=1`，一个挂死 worker 会把"单 job 卡死"放大成"整个应用不再接活" | 回归用例 `test_procpool.py::TestCpuTaskTimeout` / `::TestProcessPoolTimeoutRecycle`；调研 `docs/RUNTIME_WATCHDOG.md` | 剩余缺口**不是"等待上限"而是"自动巡检"**：卡死时用户仍只看到进度不动，需**重启应用**。补齐须加 `jobs.last_activity_at`（schema v11→v12）+ 状态机写入点 + 后台周期扫描 + 阈值分级，**触及关键路径，待用户拍板** |
| P1 | **状态串 4 份独立硬编码** | `VALID_TRANSITIONS` 键 `state.py:35`、`_ACTIVE_STATUSES` `api/jobs/__init__.py:71`、`_STUCK_STATUSES` `state.py:135`（与前者同值却独立定义）、`JOB_STATUS_ZH` `core/zh_map.py:28`；SQL 又写死 `'pending' upload.py:371`、`'error' state.py:195 / engine.py:340`、`'archived' listings.py:40` | 加一个状态要改 7 处，漏一处即"状态机接受但前端没有中文名 / 巡检不覆盖" |
| P2 | 并发上限 **TOCTOU**：COUNT 检查与 INSERT 分两次取 `db_lock`，中间隔最长 120s 的写盘/图片转换 | `upload.py:99-112` vs `upload.py:349-373`；`upload.py:114`、`__init__.py:52`；`upload.py:96` 注释自认软限制 | 并发上传可齐过检查后突破 `MAX_CONCURRENT_JOBS` |
| P2 | **静默吞异常**（4 处 `except Exception: pass` 且无日志） | `stage1.py:179-180`、`stage1.py:188-189`、`stage2.py:365-366`、`self_heal.py:454-455` | 出错时"日志里什么都没有"，与"可追溯"目标冲突 |

---

## 3. 流式输出是否符合成熟范式？

**符合。** 本项目已把三件事收敛成单一真值（这正是成熟做法）：
- SSE 轮询间隔单一常量 `api/jobs/_SSE_POLL_SECONDS = 2`（机检
  `test_sse_poll_constant.py`）；
- 终态快照缓存（存/返副本，conftest autouse 防串测）；
- 三色复核面板 + `attach_review_tier()` 让 SSR 与 AJAX 共用同一分级。

行业对标（项目已是这个形态）：**轮询式 SSE + 终态快照**优于"长连接推增量"，因为
它天然幂等、可重放、断线重连成本低 —— 对"复核员看着进度等 30 分钟"这个场景是对的。

**唯一缺口**：SSE 的**终止性**依赖状态机进入终态。§2 的"等待无上限"入口已在 2026-09-16
封堵（`run_cpu` 超时），所以**新发生的卡死不会再无声延续**；但**已经卡住的 job 仍不会自愈**
（无周期巡检），且前端没有"超过 N 分钟仍是 running → 明示可能已卡住"的提示 ——
后者是 `docs/RUNTIME_WATCHDOG.md` 里的 **P2 可选项**（纯可见性，不动状态机）。

---

## 4. 设置部分能否正常设置？

**能。** 逐项核实（全部热生效，因为写 `config.json` 后重置 LLM 客户端 / 每次请求读文件）：

| 设置项 | UI | 存储 | 热生效 | 证据 |
|---|---|---|---|---|
| LLM provider + 协议/密钥/base_url/模型 + 增删 provider | settings.html:118-179 | config.json（原子写） | 是 | `write.py:448-481`、`config.py:801-851` |
| OCR 后端 + paddle token/url/model + mineru 全项 | settings.html:182-332 | config.json | 是 | `write.py:37-64`、`stage1.py:35`、`ocr_client.py:59`、`mineru_client.py:73` |
| ocr_slices | settings.html:299-311 | config.json | 是 | `engine.py:201`、`config.py:805` |
| user_rules | settings.html:335-385 | config.json | 是 | `rules.py:108-159`、`llm_checks.py:211` |
| feishu_*（9 项） | settings.html:388-514 | config.json 顶层 | 是 | `write.py:54-63`、`notify.py:443` |
| kb.disabled_sources | settings.html:517-562 | config.json `kb` | 是 | `kb.py:71-97`、`config.py:278` |

测试覆盖：`test_api_settings.py:95/110/122` 覆盖 POST→GET 生效（**不是只测 GET**）。

### 真实缺陷

| 级别 | 现象 | 证据 | 修法方向 |
|---|---|---|---|
| **P1** | **"设了却不生效"**：active provider 的 key 为空或命中 `TEST_KEY_PATTERNS`（含 `placeholder`/`xxxx`/`changeme`）时，启动或进设置页会**自动切走并改写 config.json** | `config.py:714-726`、`config.py:645-656`、`settings.js:898-912` | 只在"未配置"时回退；测试模式判定不得用于生产切换；覆盖前显式提示 |
| **P1** | `kb_prompt_inject` **完全不可达**：后端支持但 `_STATIC_FIELDS` 无此键 → POST 静默丢弃；UI 也无控件。同类：`llm_json_mode` / `ocr_dual_compare` 仅 API 可设 | `config.py:814`、`write.py:37-64` | 补 `_STATIC_FIELDS` 与 UI 开关；未知字段应**报错而非静默丢弃** |
| P2 | 密钥**明文落盘** `config.json`（仅 gitignore 排除提交；GET 侧脱敏 `read.py:23-27`） | `config.json:2-12`、`.gitignore:54` | OS 凭据库 / 加密 |
| P2 | `ocr_backend` **无白名单** → 任意串落盘，非法值静默回退 paddle | `write.py`（未特判）、`ocr_support.py:222-240` | 校验 ∈ {paddle, mineru, docling} |
| P2 | `ocr_slices` 上限**不一致**：后端仅校验 ≥1；底部保存未 clamp，独立保存 clamp 20 | `write.py:195-206`、`settings.js:1736` vs `settings.js:823` | 上限收敛到 `config.UPLOAD_LIMITS` 同源的单一真值 |
| P2 | **死字段**：`DATABASE_PATH` frozen 下被 APPDATA 覆盖、`APP_PORT` 被 `PORT` 环境变量压过，且都无 UI；DB 连接首次后缓存，改了也不生效 | `config.py:684-697`、`config.py:752`、`server.py:31`、`db/client.py:27-42` | 要么接 UI 并即时重连，要么从 config 里删掉（消失的开关比假开关好） |

测试缺口：**无重启持久化测试**；`llm_json_mode` / `kb_prompt_inject` 全仓库无测试。

---

## 5. pipeline 是否正常且无阻塞？

**无 P0。** 这是本轮审查最"干净"的一块。

- **阶段与入口**：`api/jobs/upload.py:400` → `core/pipeline/engine.py:29/75/150`；
  Stage0 规范化 `stage1.py:51` → Stage1 OCR（带 failover）`ocr_support.py:646` →
  Stage2 逐页 LLM `stage2.py:14` → Stage3 跨页规则 + LLM `stage3.py:11`。有进度上报。
- **超时清单（无一项缺失）**：

| 外部调用 | 超时 | 值 | 位置 |
|---|---|---|---|
| OCR submit POST | 有 | `max(120, 120+3MB)` | `ocr_client.py:79/92` |
| OCR poll GET | 有 | 30/次，总 `min(600+30×页, 3600)` | `ocr_client.py:147,54` |
| OCR download | 有 | 180 | `ocr_client.py:309` |
| MinerU 申请/上传 | 有 | 60 / 300 | `mineru_client.py:124,151` |
| MinerU poll | 有 | 30/次，总 1800 | `mineru_client.py:184,49` |
| MinerU 下载 | 有 | 300 | `mineru_client.py:304` |
| LLM chat | 有 | 180/页，480 | `client.py:121`、`page_analyzer.py:600` |
| 飞书 POST | 有 | 5 | `notify.py:236/276/323/392` |
| **本地 CPU 重活（Stage 0 规范化）** | **原缺 → 已补** | 1800（`PBC_CPU_TASK_TIMEOUT_S`） | `procpool.py`（`run_cpu`）；**超时须回收进程池**，只停止等待不会释放 `max_workers=1` 的唯一槽位 |

⚠️ 上表最后一行是本轮**唯一的修补项**：初审时把"超时覆盖面"限定在**外部调用**上，
于是漏掉了本地执行路径。教训：**"超时清单"要按"所有可能永久等待的点"枚举，
而不是按"网络调用"枚举** —— 本地进程池同样可以永久不返回。

- **并发**：`MAX_CONCURRENT_JOBS=3` + 每 job 锁（`engine.py:102`）+ `llm_concurrency=5`
  （`stage2.py:54`），**无全局 pipeline 锁** → 慢 job 不饿死他者。
- **降级**：Stage3 规则层先跑；LLM 失败被 catch 转人工复核 finding（`llm_checks.py:134/326`）
  = fail-closed。Stage2 LLM 全挂 → 页集为空 → `rules/__init__.py:80` 返回 `[]`，**不产生假通过**。

### 真实缺陷

| 级别 | 现象 | 证据 | 影响 |
|---|---|---|---|
| P1 | `self_heal` 用 `to_thread(fitz)` **未走 procpool**；而项目**自己已经证明** fitz C 持有 GIL 数十秒会饿死事件循环 | `self_heal.py:99/121/550`；`stage1.py:40-42`（Stage0 已因此迁 procpool） | SSE / status 秒级卡顿（非整服务无响应） |
| P1 | Stage3 规则层 / 降噪 / KB 检索 / summary / `load_user_rules` **全为事件循环内同步 CPU** | `rules/__init__.py:132-140,164`、`stage3.py:82/98/110` | 同量级卡顿 |
| P2 | procpool `max_workers=1` → **全局串行 Stage0** | `procpool.py:96` | 吞吐瓶颈（不阻塞 loop） |
| P2 | `db_lock` 全局单锁 + 单连接，存在队头等待 | `locks.py:20` | 量级小 |

**真实耗时构成（库内 `stage1_ms/stage2_ms/stage3_ms` 实测）**：
`550d2fda` OCR 644s / LLM 825s / Cross 122s；`33495b33` OCR 153s / LLM 1209s / Cross 124s。
→ **Stage2（逐页 LLM）占 52–68%，是唯一值得优化的瓶颈。** 其余优化都是噪声级别。

---

## 6. 鲁棒性 / 泛化 / 抗挫折

`docs/ARCHITECTURE_AND_QUALITY_REVIEW.md` §3 已有五维能力矩阵（尺寸/DPI、方向、坐标空间、
清晰度、形态），**评价是"有阈值 + 有降级"，且贯穿一条原则：宁缺勿错**
（`rotation` 未知返回 `None` 不猜；`space_aspect` 与渲染图宽高比不符 → 明示"无法定位"
而不是画错框）。

本轮补充的判断：
- **鲁棒性：好。** 输入形态拒绝是显式的（多页 TIFF / 动画 WEBP → 400；加密 PDF /
  0 页拒绝）；OCR 后端 failover 有判据（异常/0 页/缺页 > max(2,10%)）。
- **泛化能力：有边界且边界是"已知的"。** 最大的泛化风险不是算法而是**样本单一** ——
  全部实测都来自同一份丝裂霉素提取批记录。**没有第二份不同工艺/不同版式的批记录**
  被验证过。所以"泛化能力"目前只能说"有降级路径"，不能说"已验证"。
- **抗挫折能力：分层看，进程层最弱。**
  - 单页失败 → 有降级（稀疏页 / 空页 / 截断 / `partial_review`）。
  - 单阶段失败 → fail-closed，不产生假通过。
  - **上游持续不可用（Paddle `10010 队列已满`）→ 静默 failover 到 MinerU**，且
    **只有 `jobs.ocr_backend_used` 能说明谁跑的** —— 跨后端逐条对比在数据上是无效的。
    已有 `tests/e2e_*` 的 `expect_backend` 断言来防"以为跑了 A 其实跑了 B"。
  - **进程层：等待上限已补（2026-09-16）**：`run_cpu` 加超时 + 超时回收进程池（§2）。
    此前的缺口是"本地 CPU 重活可以永远不返回"；现在**卡死会转成一次显式的 job error**，
    而不是静默永久非终态。**仍未做的**是周期巡检（自动自愈 + 前端明示），
    属 `docs/RUNTIME_WATCHDOG.md` 的 P1/P2，需用户拍板。
  - **上游瞬时不可达也一样会失败**：本轮 e2e 实测到一次
    `ProxyError('Unable to connect to proxy')`（系统代理瞬时不可用，重试 3 次后整 job error）。
    这是**如实失败**而非静默成功，符合 fail-closed；但**轮次级没有自动重跑**，
    运维上表现为"偶发一整轮红"（`e2e_run.py` 每轮只提交一次）。

---

## 7. 知识库是否完善？ + 是否要引入"进化机制"？

### 7.1 知识库现状（实测）

- **语料**：6 源 / **441 条** / 59,419 字符（`core/kb/data/*.json`，`store.py:41`）。
  gmp2010 313 / annex_vv 54 / annex_cs 24 / nmpa_di_2020 30 / alcoa_plus 10 /
  fda_21cfr_part11 10。来源与许可多数齐全（`raw/*.md` 头部含
  authority / document_no / origin_url / license / retrieved_at）。
- **检索**：自研**字符 bigram BM25**（无库、无向量）`core/kb/retriever.py`。
  引用是**条目级**（entry_id / article_label / chapter / excerpt，`retriever.py:147`）。
- **金标**：`docs/KB_GOLDEN_QUERIES.json` 37 条，`test_kb_multisource.py:346` 真跑 →
  **排除 1 条已登记的 known_gap 后 36/36 = 100%，总 36/37 = 97.3%**（阈值 85%，PASS）。
- **打包**：`pbc-server.spec:41` 以 `glob("*.json")` 收 `core/kb/data`；
  门禁 `release_gate.py:224`（kb_corpus）+ `:241`（kb_packaging）双重校验，CI 执行。

### 空洞（真实）

| 级别 | 空洞 | 证据 |
|---|---|---|
| P1 | `TYPE_QUERIES` 有 **2 个死键**（`batch_logic` / `low_confidence` 不是规范类型）+ **2 个规范类型无词表**（`user_rule` / `uncategorized` → 退化为「批记录/记录」） | `retriever.py:44`；金标只覆盖 20 类，`uncategorized` 无金标用例 |
| P1 | **gmp2010 是唯一主源（313/441）却缺 `license`、无可复现 raw** → 正文只能从 `docs/2010版GMP.doc` 经 Word COM 重建 | `test_kb_multisource.py:388` |
| P2 | 无 **OCR 混淆字符表**（搜 `core/` 无 confusable/CONFUSION 表） | 本轮 R2 的 `_OCR_DIGIT_MAP` 是硬编码的 5 个映射，不在 KB 里 |
| P2 | GMP 附录只有 2 个（缺无菌/原料药等）；无**检查缺陷项库** | 语料来源清单 |
| P2 | 纯 BM25 无同义扩展（"效价"vs"含量"、"批号"vs"批代码" 全库搜索无映射） | 设计取舍，非 bug |
| P2 | `retriever.py:5` 注释写 "29K chars" 与实际 59.4K 不符（陈旧注释） | 同上 |

**另一条要记的事实**：本轮的 442 条 finding 里 `kb_refs` **442/442 全空**。
但那轮（2026-08-26）早于 KB 后置富集（v8）落地，**不能据此说 KB 未被使用**；
需要一轮真实轮次来确认 `kb_refs` 实际命中率。**这是一个待验证项，不是结论。**

### 7.2 是否引入"进化机制"？

**结论：不需要引入新机制。本项目真正的缺口是"评测基准"，不是"自我改进"。**

论证（全部基于本项目自己的实测）：

1. **模型不是瓶颈。** 项目实测（`docs/ARCHITECTURE_AND_QUALITY_REVIEW.md` §1）：
   VLM 直出 vs 现链在真实第 9 页**逐格 72/72 一致**。
   → **"换个更强的模型能提精度"这个假设在本项目上不成立。**
2. **噪声来自"判得滥"和"呈现得糊"，不是"读不对"。** 本轮实测真实轮次 442 条，
   噪声占比 **75.8%**（completeness + handwritten），其中自指元噪声 72 条。
   → 换模型对这部分**零影响**。
3. **模型可替换性已经是既成事实**：`llm/client.py` + provider 配置、OCR 后端抽象
   `ocr_support.OcrCapabilities`、第三引擎 docling（可选依赖 + 优雅降级）。
   所以"模型升级后应用能跟上"这件事**结构上已经具备**。

**因此"进化机制"应该被翻译成三个具体的、可交付的东西：**

| 应有的东西 | 本项目现状 | 缺口 |
|---|---|---|
| **可重复的评测基准** | `eval_findings.py`（合成金标 P/R/F1）、`eval_kb_queries.py`（37 条金标）、`replay_rules.py`（真实轮次规则重放）、本轮新增 `eval_noise_reduction.py` | **真实文档的标注集（R4）缺失** → 换模型后**无法判断好坏**。这是唯一的卡点 |
| **回归门禁** | `scripts/release_gate.py` 6 项 + CI（Windows runner）已成立 | 评测脚本尚未全部进门禁（`eval_findings.py` 有 `--min-f1` 可作门禁但未接入） |
| **可替换性契约** | 有抽象层与配置 | OCR 后端/LLM provider 的**能力差异没有被机检**（如"某后端不返回朝向角"→ 已有护栏 `test_ocr_backends.py`，但只是能力表，不是"换后端后指标不掉"） |

**反对"自动自我改进"式进化机制的合规理由**（项目已登记，见
`docs/NOISE_REDUCTION_RESEARCH.md` / `NOISE_REDUCTION_TODO.md` 引用的 EU GMP Annex 11 §16
与中国《计算机化系统》附录第 15/16 条）：关键数据的处理逻辑变更需经批准并留痕。
一个会自己改判据的系统，其**验证状态不可复现** —— 这与本项目的核心卖点（每条 finding
可追到 OCR 原文 + 区域坐标 + prompt 版本 + KB 命中 + 抑制台账）直接冲突。

> 一句话：**把"进化"做成"评测基准 + 门禁 + 可替换抽象"三件确定的事，而不是做成一个会自己变的行为。**

---

## 8. 视觉交叉对比能否提精度？

**能，但今天的先决条件是 R4 标注集，不是模型。**

已有的事实（项目实测，非引述）：
- VLM 与现链在抽取层面**没有可测得的差距**（p9 逐格 72/72 一致）。
- VLM 侧实测出四类风险：丢 bbox → **P0-3 区域级证据锚失效**；若成为唯一读取方 →
  **失去"独立第二读"**这个降噪抓手；单页 156–163s vs 现链 ≈37s/页（**4.2×**）；
  合规审计面变小。
- **推荐形态已确定：VLM 作为"第四读"**，走已有 `dual_compare` 框架，仅对低置信度页触发
  （见 §1.5 与 R5 条目）。工具已存在：`scripts/compare_vlm_reader.py`（可复现，需 provider key）。

**"提精度"的可证实路径**（项目的验收口径已写死，我照抄，不自创）：
只有当 ① 在**有人工标注**的 ≥20 页样本上 VLM 直出的字段级准确率**显著高于**现链，
且 ② 有了 VLM 的坐标（或等效定位）方案，才值得接入。
→ **两条都依赖 R4。** 所以顺序是：**R4 → 视觉第四读试点**，不能倒过来。

**本轮可以立刻做的**（不需要 R4）：把 `compare_vlm_reader.py` 的 A/B 结果沉淀为
**字段级一致率矩阵**（现链 vs VLM vs 人眼），作为 R4 之后的对照基线。

---

## 9. 本轮已改（R1–R3 降噪开工）

### 9.1 三项改动

| 项 | 做法 | 不变量 |
|---|---|---|
| **R1 自指元噪声** | `core/finding_noise.py::reduce_self_referential_noise` —— `handwritten` 与「整体识别置信度较低」是**工具可读性**陈述而非记录缺陷，整份文档聚合成 1 条（携带条数与完整页清单）。接入 `stage3.py` | 摘要必须携带 `page_list`（不丢信息）；<2 条不聚合；幂等；输入不被修改；文案契约与 `rule_doc.py:370` 双向绑定 |
| **R2 跨页批号投票归一** | `rule_doc._check_batch_consistency` —— 归一化先剥离**全部非字母数字符号**（`^*` / `/`），再由**投票**决定：多数核心串覆盖 ≥60% 时把**编辑距离 ≤1** 的组吸收为变体；吸收数量**明示**在描述里。新增 `parsing._edit_distance_le1` | 投票不决定性 / 距离 >1 → 照旧全报；真实混批（`B202201` vs `B202202`）仍报 critical |
| **R3 年份投票归一** | 新增 `core/rules/year_vote.py`（纯函数、无状态）：全文档年份页支持度 + 批号 `YYMMDD` 独立佐证；**五道闸**同时成立才归一（有佐证 / 不相邻 / 单字符 / 有基数 / 投票决定性）。接入 **4 个发射点**：R2 `_check_year_contradiction`、R1-b `_check_time_reversal_cross_page`、R5 `_check_signature_time_anomaly`、R9a `_check_signature_order` —— 一条误读不再派生第二条 finding | **相邻年保护**（2024/2025 跨年记录永不被归一）；差 ≥2 位不归一；批号佐证的年份永不被吞；不改写任何原始文本 |

### 9.2 实测（可复现，非推断）

**R2/R3 —— 规则层离线重放**（`scripts/replay_rules.py`，同一真实轮次 `0c5cfb00-897`）：

| | 改动前 | 改动后 |
|---|---|---|
| 规则层总数 | 432 | **426**（−6） |
| `year_contradiction` | 9 | **3**（−6） |
| R7 批号 | **5 组不同批号**（19 个原始变体） | **2 组**（19 变体，2 个被投票吸收） |
| 其余 11 个类型 | — | **零变化** |

**R1 / M2 —— 真实轮次 findings 上的降噪量测**（本轮新增 `scripts/eval_noise_reduction.py`）：

| 阶段 | 总数 | 噪声占比 | critical |
|---|---|---|---|
| 原始 | 442 | 75.8% | 29 |
| R1 自指元噪声聚合后 | **372**（−70；72 条 → 2 条摘要） | 71.2% | 29 |
| 再叠加 M2 completeness 降噪 | **272**（再 −100；聚合 102 条、降级 36 条） | **60.7%** | 29 |
| **净效果** | **442 → 272（−38.5%）** | | **±0** |

- 高价值类型（`param_out_of_spec` / `time_reversal` / `batch_inconsistency` /
  `signature_time_anomaly` / `self_review`）**一条未减**；
- `critical` **29 → 29，一条未减**；
- **页级证据闭合**：原始 51 页 → 降噪后 51 页，无页丢失。
- **漏检对照**：`scripts/eval_findings.py` → `precision=1.0 recall=1.0 F1=1.0, TP=10 FP=0 FN=0`
  （**降噪不是靠丢真命中换来的** —— 这是项目自定的行业禁则）。

### 9.3 新增/修改的文件

- 新增 `core/rules/year_vote.py`、`scripts/eval_noise_reduction.py`、`tests/unit/test_year_vote.py`
- 修改 `core/rules/rule_time.py`（4 个发射点接投票）、`core/rules/rule_doc.py`（R2 投票）、
  `core/rules/parsing.py`（符号剥离 + `_edit_distance_le1`）、`core/finding_noise.py`（R1）、
  `core/pipeline/stage3.py`（接入 R1）、`tests/unit/test_cross_page_analyzer.py`、
  `tests/unit/test_finding_noise.py`

---

## 10. 本轮审查发现的"度量与证据"问题（必须记）

1. **`docs/ARCHITECTURE_AND_QUALITY_REVIEW.md` 的关键证据轮 `95a27d88-52b`（347 findings /
   51 页 / Paddle / 1894s）在两份库中都不存在**（`data/pharma.db` 23 个 job、
   `%APPDATA%/PBC/data.db` 4 个 job，均无；`%APPDATA%/PBC/output/` 也无该目录）。
   该轮当时跑在 `%TEMP%/pbc_e2e_appdata` 隔离库上，事后被清理。
   → **文档里的 347 / 71 / 58 / 1894 这些数字今天无法复算。**
   缓解：同类结论在**仍保留**的 `0c5cfb00-897`（51 页 / Paddle / 442 条）上可复现 ——
   本轮所有量测都用它。
   → **纪律（已生效）**：以后任何"决定性证据轮"必须落到**常驻库或入库的产物**上，
   不能只活在 `%TEMP%`。
2. **`core/finding_noise.py`（2026-09-11 引入，"实测 −65.8%"）从未在任何真实轮次上验证过**
   —— 库中最新真实轮次是 **2026-08-26**，早于该模块。本轮首次在真实轮次上量到它的效果
   （372 → 272）。**"实测 −65.8%" 是离线重放数字，不是端到端数字**，文档措辞需区分。
3. **M2 降噪在那轮真实数据上确实没有生效**（结构性 completeness 仍以逐条形式存在：
   `time` 74 / `qa` 28 / `reviewer` 16 / `operator` 5）→ 与 2 一致。
4. **`finding_suppressions` 台账在两份库中均为 0 行** → T-P0-2（抑制留痕）**从未被真实轮次
   触发过**，其 10 项集成测试覆盖的是合成路径。不是缺陷，但"已完成"的成色要标注。

---

## 11. 待决（需用户决定，我不擅自开工）

| # | 事项 | 为什么需要你决定 |
|---|---|---|
| 1 | **R4 人工标注集（≥20 页真实批记录，逐页真值字段 + 期望 findings）** | 它是**所有** P/R、**所有**"精度提升"、**视觉第四读**的先决条件。没有它，后面所有"更准"都不可度量 |
| 2 | **运行时看门狗**（§2 的 P0） | ✅ **已决并已实施（2026-09-16，v1.1.4）**。兜底一半：`run_cpu` 超时 + 池回收（随 `b1c48bc`）。周期巡检一半：`jobs.last_activity_at`（schema v11→v12）+ 后台扫描 + 阈值分级（用户决策"做 P1 周期巡检"）。实施后回审又修 5 项（3 项严重：孤儿 task 未终止、OCR/cancelling 阈值低于上游封顶），并新增 `GET /api/health/watchdog` 自述端点 —— 全部带实测，见 `docs/RUNTIME_WATCHDOG.md` §8。**仍不做**：P2（SSE 停滞可见性），因阈值抬高已升级为"应当做" |
| 3 | **第二份不同工艺/版式的真实批记录** | 当前"泛化能力"只能说"有降级路径"，不能说"已验证"；没有第二份样本就无法验证 |
| 4 | 设置 P1（provider 静默改写 / `kb_prompt_inject` 不可达） | 是否改"启动时自动回退"的行为，涉及用户体验取舍 |
| 5 | 密钥轮换 | 你说会删除；删除后**必须同时在厂商侧轮换**（删除无效，见 `scripts/check_leaked_keys.py`） |
| 6 | R4 之前的降噪继续做哪一块 | 当前 `completeness` 仍是最大桶（降噪后 164/272）。下一刀应是"LLM 生成的非结构 completeness"，但它需要 R4 才能判定哪些是真缺失 |

---

## 12. 复现命令

```bash
PY="C:/Users/WuSiTan/AppData/Local/Programs/Python/Python311/python.exe"

# 规则层离线重放（R2/R3 净效果）
"$PY" scripts/replay_rules.py --job 0c5cfb00-897 --db <data.db>

# 降噪量测（R1 + M2）
"$PY" scripts/eval_noise_reduction.py --job 0c5cfb00-897 --db <data.db>

# 漏检对照（P/R/F1 不得下降）
"$PY" scripts/eval_findings.py

# 知识库金标
"$PY" scripts/eval_kb_queries.py

# 权威门禁（6 项）
"$PY" scripts/release_gate.py --python "$PY" --fail-under 95
```

> 真实轮次的库在 `%APPDATA%/PBC/data.db`（运行库，含 WAL）；`data/pharma.db` 是开发库。
> **量测前先复制到临时目录**，避免对运行库做写操作。

---

## 13. CI 与干净检出的用例差异（精确清单，含一处未能钉死的残差）

### 13.1 设计使然的 skip：artifact-gated 用例（run #2 实测 17 条；run #6 = **18 条**）

> 这些是**预期**行为：本机有未入库的构建产物所以能跑，CI 干净检出无产物所以被 skip。
> **数目随用例增删浮动**（不是固定 17），且它们**不是**"44 个用例"的主因 ——
> 真正的问题在 §13.2b。

| 组 | 文件 | 条数 | 依赖 | 能否在 CI 上跑 |
|---|---|---|---|---|
| **A** | `tests/integration/test_frozen_smoke.py` | **8** | `dist/pbc-server.exe`（冻结产物） | ✅ **能** —— 前提是 CI 里先跑一次 PyInstaller 构建 |
| **B** | `tests/unit/test_distribution_parity.py` | 3 | `dist-electron*` + 已构建输出 | ⚠️ 需要 Electron 打包（node_modules + electron-builder），CI 代价高 |
| **C** | `test_regions_anchor.py`(4) + `test_region_anchor_wiring.py`(2) | 6 | 真实 Paddle/MinerU 产物 | ❌ **设计上不跑**（体积 + 数据敏感性，不入库）；上一轮已用**等形态合成数据**补了等价契约 |

逐条（A/B/C）：

```
tests/integration/test_frozen_smoke.py
  test_api_docs_served / test_db_created_in_appdata / test_health_ok
  test_jobs_api_returns_paginated / test_review_page_route
  test_settings_page_served / test_static_css_served / test_upload_page_served
tests/unit/test_distribution_parity.py
  test_latest_artifact_embeds_the_build_output_byte_for_byte
  test_latest_build_is_complete / test_packaged_app_version_matches_app_version
tests/unit/test_regions_anchor.py
  test_real_artifact_reports_orientation_for_every_page
  test_real_artifact_rotation_parity_matches_space_orientation
  test_real_paddle_artifact_replays
  test_real_rotated_page_landmarks_land_where_they_should
tests/unit/test_region_anchor_wiring.py
  test_real_mineru_artifact_replays / test_real_mineru_regions_anchor_a_real_finding
```

### 13.2 **未能钉死的残差（诚实声明）**（→ 已于 §13.2b 钉死）

CI run #3 报 `2250 passed`，同期本地 `2362 collected` → 差 **112**（含 17 skip）。
要把差额逐条归因，唯一依据是**逐条 pytest 日志**。但手上那份 artifact
（`gate_pytest_20260915_094323.log`，来自 run #2，当时 CI 为 2237 通过）**是残缺的**：
只解析出 **1416** 条结果行（2237 应有多少条就有多少条），所以 per-file 对比不可信。

→ **我不知道那 26（run #2 口径）具体是哪些用例。** 此前把它写成"属预期、不是缺陷"
是**未经证实的**，本节据此更正。

**补救（两步；第 2 步才是真正止血的那一步）**：

1. `.github/workflows/ci.yml` 上传步骤由 `if: failure()` 改为 **`if: always()`**
   —— 绿了也上传。不这么做，**成功时连 artifact 都没有**（run #3 实测：该步骤
   结论是 `skipped`，"上传门禁报告"这一栏根本不存在）。
2. **但只改 ① 不够** —— 传出来的仍是**残缺的 stdout 文本日志**（不含逐条 nodeid）。
   真正的根因是：门禁跑 pytest 带 `-o addopts=-q`，stdout 上**只有点和汇总**；逐条
   事实源是 junit XML，而它落在 `tempfile.gettempdir()`（CI 上是 `D:\a\_temp\`）
   —— 随 runner 一起消失，也不在 artifact 上传列表里。
   → 修法：门禁把 junit XML **另存一份到 `devlogs/gate_junit_<stamp>.xml`**
   （字节级复制，路径写进报告 `detail`），CI 的 artifact 列表补上该 glob。
   护栏 `tests/unit/test_release_gate.py::TestJunitAuditTrail`（4 例），其中一条是
   **跨文件契约**：`ci.yml` 必须同时含 `devlogs/gate_junit_*.xml` 与 `if: always()`
   —— 少了任一半，门禁写出来的副本都传不出 CI，盲区原样保留。

**于是"44 个用例"从此可核对**：下一次 CI 的 artifact 里就有逐条用例清单，把 CI 的
junit 与本地 `--collect-only` 做**集合差**即可逐条归因，不再依赖残缺日志的行数猜测。

```bash
# ① CI 侧：从 artifact（gate-report）里取 gate_junit_<stamp>.xml
# ② 本地侧：现场 collect，交给工具做集合差
PY="C:/Users/WuSiTan/AppData/Local/Programs/Python/Python311/python.exe"
"$PY" -m pytest tests --collect-only -q -o addopts="" -p no:cacheprovider \
    > /tmp/local_ids.txt
"$PY" scripts/compare_test_matrix.py \
    --ci-junit <下载的 gate_junit_xxx.xml> --local-collect /tmp/local_ids.txt
```

`scripts/compare_test_matrix.py` 把这个对比固化下来，**复用** `release_gate._junit_nodeid`
做 nodeid 还原（不复制第二套口径 —— 同一事实只能有一处真值）：

- 输出"只被本地收集"与"只被 CI 收集"两组，按文件聚合（"整文件缺失"一眼可见）；
- 单独给出 CI 侧 `skipped` 计数并说明**为什么它不等于"没跑"**：junit 里 skip 的用例
  **仍在**（`<testcase><skipped/></testcase>`），所以"只被本地收集"才是真正的
  "CI 上不存在"，skip 数只用于对账；
- 退出码 1 = 两侧有差异，便于把"差异必须被解释"接进流程。

护栏 `tests/unit/test_compare_test_matrix.py`（18 例），其中三条锁"口径唯一"
（必须加载 release_gate、不得在本脚本里重现 `_junit_nodeid`），并专门测了
**CRLF 输入**（Windows 的 `--collect-only` 输出是 CRLF，裸 `grep`/`comm` 会失真）。

### 13.2b **残差已钉死（2026-09-16，用 CI 的 junit 审计副本实测）**

CI run #6（`4416de8`）之后，artifact 里首次有了 `gate_junit_*.xml`，残差可以**逐条**算了：

| | 本地 | CI |
|---|---|---|
| collect / junit testcase | **2384** | **2358**（passed=2340, skipped=**18**, failed=0） |
| 差 | — | **26** |

逐条归因（`scripts/compare_test_matrix.py`）：

- **27 条** —— `tests/unit/test_anchor_orientation_tool.py` 的**全部**用例在 CI 上
  **不存在**；CI 的 junit 里该文件只剩一条 `classname=""` 的条目：

  ```
  <skipped message="collection skipped">
    ("D:\a\BatchSentry\BatchSentry\tests\unit\test_anchor_orientation_tool.py", 22,
     "Skipped: could not import 'numpy': No module named 'numpy'")
  ```

- **18 条用例级 skip**（artifact-gated：`test_frozen_smoke` 8 / `test_distribution_parity` 3 /
  `regions_anchor` + `region_anchor_wiring` 6 / 其他 1）—— 这些才是**设计使然**：
  本机有产物所以跑，CI 干净检出无产物所以跳。

**26 = 27（整文件消失）− 1（那条畸形条目占位）**，与 18 条 skip **无关**。

→ **此前"属预期、不是缺陷"的判断错了一半**：18 条确实预期，但那 **27 条是真实缺陷** ——
一整文件在 CI 上**从未运行过**，而门禁六项全绿、覆盖率也没反映（该文件的覆盖被本机
"顺手"补上了）。这是"只被可选用例覆盖"隐患的**升级版：整个文件消失**。

**修法（三层，缺一不可）**：

1. **声明依赖**：`requirements-dev.txt` 加 `numpy==2.4.5`（本机实测通过的那一版）→
   CI 装上后 27 条回归。
2. **护栏（预防）**：`test_declared_dependencies.py::TestImportOrSkipIsDeclared` ——
   `pytest.importorskip("X")` 的 X 必须在清单里声明。**用 AST 而非正则**：正则版把本文件
   docstring 里的示例误判成"未声明依赖 x"（假阳性会让人干脆把护栏关掉），实测踩到。
3. **检测（兜底）**：门禁新增 `_container_skips()` —— junit 里 `classname=""` 的 skip 条目
   （= 整文件在**收集阶段**被跳）**直接判 FAIL**。这是唯一能看见这类失败的检查：
   它既不进 `failed`、也不进覆盖率。护栏 `test_release_gate.py::TestContainerSkips`（4 例）
   + `test_container_skip_makes_the_check_fail` / `test_case_level_skip_still_passes`
   （正反对照，防"用例级 skip 被误判"）。

**最终验证（CI run #7，`98448bc`）**：

```
本地 collect : 2395
CI junit     : 2395  (passed=2378 skipped=17 failed=0)
── 只被本地收集（CI 上不存在）：0 条
── 只被 CI 收集（本地没有）：0 条
```

→ 两侧用例矩阵**完全一致**。27 条已回归 CI；剩余 **17 条全部是用例级 skip**
（artifact-gated，设计使然）。**"CI 上 44 个用例"到此终结**：
`44 = 27（真实缺陷，已修）+ 17（设计使然）`，且此后**每一次 CI 都可用一条命令核对**
（`compare_test_matrix.py`），不再需要任何猜测。

### 13.3 让 A 组（8 条）真正在 CI 上跑的路径

需要给 CI 加一个 job：装 PyInstaller → 跑 `build.ps1`（或直接 `pyinstaller pbc-server.spec`）
→ 再跑 `tests/integration/test_frozen_smoke.py`。

**本轮没有做，理由**：新增一个 CI job 属于"新增验证路径"，按本项目纪律必须由**一次真实
CI 运行**证明它可用；而它的代价（PyInstaller 构建 + 依赖）与失败模式（构建锁、路径）
都需要单独一轮调试。**先把它挂成待办，而不是提交一个未经运行验证的 workflow。**
（这也正是本轮 CI 的教训：`ci.yml` 第一次跑就抓到了 cp1252 与干净检出覆盖率两个缺陷
—— 未跑过的 CI 配置不能当作"已完成"。）

---

# 第二轮对抗性审查（Round 40，基线 = v1.1.9，2026-09-18）

> 触发：代码推进了 4 个小版本（1.1.5 → 1.1.9）、修了 11 条缺陷并**已重建产物**，
> 需要判定「当前产物能否作为分发基线」。
> 纪律：**先定位 → 再解决 → 最后测试**；结论必须挂证据；**已修的不得当缺陷报**。

## 14.0 一页结论：可以作为基线，且建议就这么定

| 提问 | 结论 | 证据强度 |
|---|---|---|
| 构建物能否分发 | **可以**。版本四处一致、asar/后端 exe 均为一源、Electron 内嵌 exe **实跑通过**、目录唯一、门禁 8 项全绿（覆盖 95.24%）、一致性机检 13 passed | 高（**本机实测**）；仍缺"他机验证"，见 §14.4 |
| 前端是否正常 | **正常**。EventSource 聚合单连接 + retry 帧 + 断线计数 + 卸页清理 + 终态快照缓存；明确避开 `event: error` 保留字 | 中高（读码 + 既有 e2e） |
| 后端日志/状态机/pipeline 无阻塞且有提醒 | **有运行时看门狗，非终态不再无上限**（旧 P0 已修）；仍余 **P1 状态串 4 份硬编码 / TOCTOU / self_heal GIL / Stage3 同步 CPU / procpool 串行**；静默吞异常**净增**但**多数为合理防御** | 高（本轮抽验，纠正了"净增即缺陷"的定性） |
| OCR/LLM 能否正常配置 | **能**。配置为扁平键、env 可覆盖；protocol 是一等配置项 | 高 |
| 设置能否兼容 openai / anthropic | **架构上兼容**（`{prefix}_PROTOCOL` → `get_adapter()` 路由两套 adapter，各自报文形态有 7 例协议桩覆盖）。**厂商真机未验** | 中（协议桩，非真机） |
| 流式输出是否合主流 | **合**（单一常量驱动 `retry:` 与 sleep、聚合连接、终态缓存）。**差距**：未见 `id:`/`Last-Event-ID` 断点续传与心跳注释帧 | 中高 |
| 规则是否合理 | **合理且有针对性**：本轮实测确认「OCR 丢小数点」已有一等对策（见 §14.2）。**残余**：同一根因的多点未聚合 | 高（直接调用复验） |
| 如何提升精/准 | **不需要新机制**，需要 ①「同根因聚合」②「R4 真实标注集」（否则无可信 P/R）③ 把已落地的 `vision_crosscheck` 接入 pipeline | 中（①有实测依据） |
| 文档是否及时 | **及时**。CHANGELOG/CLAUDE/TODO/README/PORTABLE_README 均为 9-17/9-18 | 高 |
| 知识库是否完善 | 477 条 / 6 源 / 金标全绿 / 已入包有门禁；**空洞**：2 死键 + 2 类型无词表（沿用上轮，本轮未复验） | 中 |

**判定**：**没有 P0 阻塞分发**。建议**以 v1.1.9 为基线**，把 §14.5 的清单作为下一版迭代。

## 14.1 旧指控复核（逐条实测，含"已修"的正结论）

| 旧指控（Round 36–38） | 当前状态 | 证据 |
|---|---|---|
| **P0** 无运行时看门狗（`recover_stuck_jobs` 只在启动跑） | **已修** | `core/watchdog.py` 全模块；`main.py` 启动 `_watchdog_bg()`；默认 60s；覆盖非终态；**含孤儿 task 终止**；`GET /api/health/watchdog` 自述阈值 |
| **P1** provider 被启动逻辑静默改写 | **本轮未复核** | 见 §14.4 |
| **P1** 状态串 4 份独立硬编码（加状态要改 ≥4 处） | **仍在** | `VALID_TRANSITIONS` `state.py:35`、`_STUCK_STATUSES` `state.py:139`、`_ACTIVE_STATUSES` `api/jobs/__init__.py:111`、`JOB_STATUS_ZH` `zh_map.py:28`；另有 SQL 写死 `'pending'`/`'error'`/`'archived'` |
| **P2** 并发上限 TOCTOU | **仍在（部分加固）** | COUNT 已进 `db_lock`（`upload.py:99-104`），但 INSERT 在另一段锁（`upload.py:349`），中间隔写盘/转 PDF；注释自认软限制 |
| **P2** 静默吞异常 4 处 | **仍在，且全仓共 28 处** | 但**逐处甄别后绝大多数是合理防御**：`rules/__init__.py`×4 与 `docling_client.py:177` 包围 `progress_cb()`；`probe.py`×2 包围审计落库；`upload.py:396` 包围 `rollback()`；`docling_client.py:61` 是 API 形态探测；`procpool.py:225`/`watchdog.py:437` 有注释说明。**真正有效者见 §14.3** |
| **P1** `self_heal` 用 `to_thread(fitz)` 持 GIL 阻塞事件循环 | **仍在** | `self_heal.py` 多处 `asyncio.to_thread(fitz...)`，未走 procpool |
| **P1** Stage3 同步 CPU | **仍在** | `rules/__init__.py:133` 同步 `spec.check(...)`；`stage3.py` 多处同步 |
| **P2** procpool 全局串行（`max_workers=1`） | **仍在** | `procpool.py:96-98` |
| **P2** `page_image.py` 三处静默 | **定性需修正** | 实测为 **PDF 句柄缓存淘汰**（`doc_cache.pop` 前后），非渲染主路径；渲染主路径 `:145`/`:178` **有** `logger.error` 并抛 500。**低危** |

## 14.2 本轮最有价值的实测：视觉交叉比对（回答"能否提精度"）

**做法**：用本模型的多模态能力**直接读** `devlogs/m8_pdf_pages/p08.jpg`（51 页真实批记录的第 8 页渲染图），
与库中系统抽取结果逐格比对。

**结果**：
- 图上「进料 压力(bar)」列 5 个时间点的真实手写值**全部是 `4.6`**；系统 OCR 输出为 **`46`**（丢小数点）。
- **同页其他值小数点均在**（`3.6`/`2.6`/`2.7`/`5.8`/`0.90`/`5.7`/`1.00`）⇒ 是该列的**系统性误读**，非随机噪声。
- **反例（重要）**：并非"视觉与 OCR 处处不一致"——大多数格两侧一致，不一致集中在「进料 压力」列。

**但必须说清版本**：这份抽取来自 **2026-08-26** 的 job（`0c5cfb00`，库在 `%APPDATA%/PBC/data.db`），
属**降噪 R1–R3 之前**的产物，不能直接代表 v1.1.9。

**于是做了当前版本的直接复验**（`_severity_for_out_of_spec` + `_decimal_loss_factor`，无需网络）：

| 输入 | 当前版本输出 | 评价 |
|---|---|---|
| `46` vs `3.0-5.0`，raw=`"46"` | **info** +「实测值与规格相差约 10 倍（疑似 OCR 丢失小数点，如 4.6 读成 46），请对照 PDF 原页人工核对」 | ✅ 已软化，且提示直指根因 |
| `46` 但 raw 含小数点 | **warning** | ✅ 严谨：带点就不是丢点 |
| `25` vs `3.0-5.0`（仅 5 倍） | **warning** | ✅ 不软化（倍数不符） |
| `printed 46`（`value_source="printed"`） | **warning** | ✅ 印版值不软化 |
| `45.6` vs `<=5.0`（真实失控） | **warning** | ✅ 真超差不软化 |

**结论**：**「OCR 丢小数点」在当前版本已是一等对策**，代码注释明确标注"**M8 真实 p08 实测**"
（即上一轮实测后所修），护栏在 `test_cross_page_analyzer.py`（8 处）+ `test_spec_guard.py`（2 处），
且 LLM 侧的重复条目会被 `drop_unfounded_spec_findings` **剔除并留痕**（已接入 `stage2.py:372`）。
⇒ **这是"已修"项，不得再当缺陷报。**

**同时它也验证了 `core/vision_crosscheck.py` 的价值**：若已接入，`视觉 4.6 / OCR 46`
会被直接标成「不一致 → 待人工核对」，**不需要用户先被假告警吓一跳**。

## 14.3 本轮新发现（真实有效，需下一版处理）

| 级别 | 现象 | 证据 | 影响 |
|---|---|---|---|
| **P2** | **同根因未聚合**：`4.6→46` 会在 **7 个时间点各报 1 条** info（旧版是 7 条 warning + 2 条 critical）。severity 已正确降级，但**条数不变** ⇒ 用户仍要逐条读同一件事 | 旧 job 实测 9 条；`rule_spec.py:228` 逐值判定，无聚合层 | 噪声总量下降但**信息密度未升**；51 页实测该 job 共 **442 条** finding |
| **P2** | `_get_analyzed_pages` 的 **fail-open**：`structured_json` 解析失败时 `except: pass` ⇒ 该页被计入"已分析" ⇒ **retry 不会重跑它，且日志无痕** | `stage2.py:488-497`（预筛要求同时含 `"_parse_error"` 与 `true`，损坏 JSON 走 `pass` 后仍 `analyzed.add`） | 窄条件（需 JSON 损坏），但属**静默漏检**；应改 fail-safe（解析不出即视为未分析）并留 warning |
| **P2** | 诊断类静默失败**无日志**：`stage1.py:179`（`_pdf_page_diagnostics` 失败）、`self_heal.py:727`（`ocr_diagnostics` 解析失败） | 同上两处 `except Exception: pass` | 注释承诺"原始证据链保留，可回答'此件为何被规范化'"，但诊断缺失时**事后无从查询** |
| **P2** | SSE 未见 `id:` / `Last-Event-ID` **断点续传**，也未见心跳注释帧 | `api/jobs/status.py`、`listings.py` 仅 `retry:` + `data:` | 中间代理超时或短暂断线时需整段重放；长 job（51 页）体验受影响 |
| **P2** | 状态串仍 4 份硬编码 + SQL 字面量 | 见 §14.1 | 加/改状态易漏改（非正确性 bug，属可维护性） |
| **P3** | `feishu_app_id` / `feishu_app_secret` **明文存于 `%APPDATA%/PBC/config.json`** | 本机实读 | 本机文件、不入库；但属凭据卫生项，建议至少提示或支持引用环境变量 |

## 14.4 未复核 / 诚实留白（不得当成"已通过"）

1. **provider 静默改写**（旧 P1）—— 本轮未复验，**仍是未决项**。
2. **知识库空洞**（2 死键 + 2 规范类型无词表 + `gmp2010` 无 license/raw）—— 沿用上轮结论，本轮未重测。
3. **他机验证**：所有产物结论都是**本机**证据；GUI 只能人工双击（agent shell 起 GUI 会 ~1s 退出）。
4. **anthropic 厂商真机**：仅本地协议桩，**未与 `api.anthropic.com` 实际连通**。
5. **无真实标注集**：因此**没有任何可信 P/R 数字**；`docs/FINDING_GROUND_TRUTH.json` 是合成件。
6. **本轮的 `_severity_for_out_of_spec` 复验是"直接调用"级**，不是端到端跑一遍 v1.1.9（未重跑 51 页）。

## 14.5 下一版迭代清单 → 详见 `docs/TODO.md` 的「v1.2.0 迭代清单」

---

# 15. 第三轮对抗性审查（2026-09-20，Round 43）

> **触发**：用户要求复核「精度 / 错误可见性 / 前端 / 后端日志与状态机与 pipeline /
> OCR 与 LLM 配置 / openai 与 anthropic 兼容 / 流式输出合主流 / 规则合理性 /
> 文档 / 知识库」，并明确要求**用模型自身的多模态能力直读 PDF 原页做交叉比对**。

## 15.0 一页结论

| 提问 | 本轮结论 | 证据强度 |
|---|---|---|
| **能否分发** | **程序面可以**：版本四处一致 = `1.1.9`、`asar` 内版本 = `1.1.9`（子进程读）、内嵌 exe 与 `dist/` **sha256 相同**、目录唯一、7 轮产物级 e2e 全过。**但精度不足以"免人工复核"**：51 页产出 **55 条 critical**，本轮**只核了 5 页就坐实 4 条假 critical** | 高（实测） |
| 前端是否正常 | **正常**，但发现 4 处过渡期/口径问题（`type:error` 两类帧不分、终态文案漏中文映射、error 转态期不显示原因、状态中文映射第 3 份副本） | 高（两端读码确证） |
| 后端日志/状态机/pipeline | **无 P0**；新发现 1 条**日志口径**缺陷（分片路径把累计值当"本片新增"）+ 3 条 P3（看门狗自述无停滞清单 / 审计写失败静默 / Stage3 CPU 期间看门狗盲窗） | 高（读码确证） |
| OCR / LLM 能否正常配置 | **能**，OCR 后端与分片运行时读取、热生效；**但发现 P1**：provider 会被启动逻辑**自动改写并持久化**（见 15.2） | 高（读码确证） |
| 设置能否兼容 openai / anthropic | **报文层兼容**（鉴权头、system 顶层、content 数组、`usage`、错误分级各自正确，无"只在一家能跑"的硬编码）；`response_format` 仅 openai 生效而 anthropic 正确忽略（已知不对称）。**厂商真机仍未连通** | 中高（读码 + 协议桩） |
| 流式输出是否合主流 | **合**。⚠️ 上一轮登记的「未见 `id:` / 缺心跳帧」**已过时**（见 15.4） | 高 |
| 规则是否合理 | **合理**：规则层**未发现**判据方向反转（逐条核 R1a/R1b/R5/R9a/R4）。覆盖度实为 **17/21**（非 16/21）。**新发现 P2**：R4 用**墙钟**当基准 ⇒ 结论不可复现 | 高（读码确证） |
| 如何提升精/准 | 见 15.7；核心 = 把"判据"从 LLM 自由裁量**收回到规则层**（15.3 的三类新形态都指向同一个根因） | 高 |
| 文档是否及时 | **及时**，但发现 **KB 条目数两套口径并存（441 vs 477）**、两处登记已过时 | 高 |
| 知识库是否完善 | **P2 新发现**：门禁用的 `count_kb_entries` 把**章节标题元数据**计入语料 ⇒ 数字虚高 36（真值可检索 **441**） | 高（实测） |

**判定**：**无 P0 阻塞分发**；可作为基线，精度问题整体下沉到 v1.2.0。

## 15.1 方法（可复现）

1. **五路只读审查**（前端 / 后端 / 配置与协议 / 流式与规则 / 文档与知识库），
   各自先读 §14 上一轮结论，再深挖代码；**结论必须挂 `file:line`**。
2. **视觉交叉比对**（本轮最强新证据）：用模型多模态能力**直读** `devlogs/m8_pdf_pages/pNN.jpg`
   （51 页真实批记录渲染图），与**直查隔离库**的 findings 逐条对质。
   隔离库来源 = Round 42 的 e2e 产物，**7 个 job / 355 findings**，其中 real 轮
   （job `6f80145a…`，51 页）**293 条**（critical 55 = rule 6 + `llm_page` 25 + `llm_cross` 24）。
3. **产物实测**：sha256 / `asar_version()`（**子进程读**）/ 目录唯一性 / 门禁口径。
4. **不采信上一轮结论与 subagent 结论**：凡高价值断言**逐条亲自复核**（本轮据此**修正了 3 条**
   转述偏差，见 15.4）。

## 15.2 本轮新发现

### P1

1. **provider 被启动逻辑自动改写并持久化（界面无提示）** —— 这正是 §14.4 列为
   「**未复核**」的那条，本轮**确证**。
   - 证据：`config.py:714-726` —— 若 active provider 的 `api_key` 未通过 `_is_real_key`
     （`config.py:659-669`，子串匹配 `TEST_KEY_PATTERNS`，`config.py:645-656`），
     则**遍历注册表挑第一个"有真 key"的 provider**，改写 `os.environ["LLM_PROVIDER"]`
     **并 `_persist_env_to_config()` 写回配置文件**。
   - 精确表述（**比 subagent 的转述更收敛**）：它是**有日志的**（`logger.warning("Auto-activating…")`），
     所以**不是"完全静默"**；真正的问题是 ① 该决策**只落日志、界面零提示**，
     ② **会持久化覆盖用户在设置页的显式选择**，③ 判据是**子串匹配**（真实 key 若含
     `placeholder`/`xxxxx`/`test-key` 等子串会被判"非真"）。
   - 影响：GMP 场景"**你以为的模型 ≠ 实际跑的模型**"，且事后难以追溯。
   - 建议：仅当"**一个 key 都没配**"时回退；**不得用测试/占位判定做生产切换**；
     覆盖前在设置页显式提示；子串判定改精确格式校验。

### P2

2. **KB 条目数"真值"口径把章节元数据计入 ⇒ 规模虚高 36**
   - 实测：`scripts/release_gate.count_kb_entries` = **477**，而检索器 `core.kb.store.entries()` = **441**；
     差值 36 = 4 个 JSON 的 `chapters`（纯章节标题，无正文、无 `entry_id`，**检索器不索引**）。
   - 证据：`scripts/release_gate.py` 对 `entries`/`items`/`chapters` 三表求和。
   - 建议：`count_kb_entries` 分列"条目数 / 章节数"，文档权威口径统一为 **441**。

3. **规则层 R4 用墙钟 `datetime.now()` 当基准 ⇒ 结论不可复现**
   - 证据：`core/rules/rule_time.py:271` `current_year = datetime.now().year`；`:283` `if year < 2000 or year > max_year`。
   - 影响：同一份 PDF **在不同年份重分析会得到不同 finding** —— 对"可复现"要求高的
     受监管审计工具是硬伤。（注意：**判据方向本身是对的**，问题在**基准随运行时刻漂移**。）
   - 建议：把"分析基准年"固化为配置项（或取记录自身年份），与运行时刻解耦。

4. **`review.js` 把两类 `type:error` 帧一律当"任务已删除"**
   - 证据：`static/review.js:259-268` 只判 `d.type === "error"`；而后端 `api/jobs/status.py:402`
     发 `'进度查询失败'` 后 **`continue`（设计上应重试）**，`status.py:411` 才是真终态。
   - 影响：一次**瞬态 DB 抖动**会被前端误判为"任务被删除"，进度流永久断开 + 文案说谎。

5. **`review.js` SSE 进度文案对终态漏中文映射**（`review.js:372-379` 只处理
   `cancelling/cancelled/error`，其余落 `else` 显示原始英文 token），而同页徽章走
   `statusZh` 是中文 ⇒ **同一状态同页两处不一致**。

6. **分片路径逐片日志把"累计新增"当"本片新增"**
   - 证据：`core/pipeline/engine.py:488` `new_pages = 0`（片循环**外**）；`:548` `new_pages += 1`；
     `:556-559` **片循环内**打印 `({new_pages} new)` ⇒ 第 2 片起日志显示的是累计值。
   - 影响：属"日志说谎"，按日志核对"本片落几页"会被误导。（`:611` 的最终汇总用累计值**是对的**。）

7. **文档对 KB 条目数自相矛盾（441 vs 477）** —— `README.md:39` 等写 441，
   `ADVERSARIAL_AUDIT.md:586`、`PLAN_v1.1_EXECUTION.md:322` 写 477（根因见第 2 条）。

### P3（择要）

8. 看门狗自述不含"当前停滞 job 清单"（`core/watchdog.py:501-518` 仅计数；
   `find_stalled_jobs()` 有能力但无端点暴露）⇒ 外部监控无法定位具体 job。
9. **审计写失败被静默丢弃**（`core/pipeline/state.py:31-32` 仅 `logger.warning`）
   —— GMP 追溯链可能空洞且无信号。
10. **看门狗与 pipeline 同事件循环** ⇒ Stage3 同步 CPU（已知 B2-4）期间看门狗**盲窗**。
11. `/live` 聚合流在 DB 异常期**不 yield 任何帧**（`api/jobs/listings.py:187-192`
    只有 `logger.error` + `continue`），对照单任务流异常时仍每 2s 发错误帧。
12. `review.js` 在 error 转态期间不消费 `error_message`（仅 1.5s 窗口，SSR 后可见）。
13. 状态中文映射出现**第 3 份前端副本**（`review.js:385-390`；同页颜色却走共享 `PbcStatus`）
    ⇒ 文字与颜色不同源，是最危险的分裂形态。
14. 规则覆盖度实测为 **17/21**（`registry.py` 22 条规则；无确定性规则的仅
    `signature_mismatch`、`ocr_noise`）—— TODO 里写的"16/21"应更正。
15. 少数阈值**无实测依据**：`rule_spec.py` 的 `_EDGE_MARGIN=0.10`、R10 的
    `len(missing)*2 > len(nums)`；`rule_time.py:283` 的 `year < 2000` 会误报**合法旧年份**。
16. `clean_dist.asar_version()` 传错参数（目录而非 asar 文件）时**静默返回 `None`**，
    与"文件损坏"不可区分 ⇒ 调用方容易得出"asar 坏了"的错误结论（本轮亲自踩到）。

## 15.3 视觉交叉比对（本轮最强新证据）

**方法**：直读 `devlogs/m8_pdf_pages/pNN.jpg`（原始记录渲染图），与隔离库 findings 逐条对质。
**核了 5 页，坐实 4 条假 critical + 1 条确认**：

| 页 | 系统给出的 finding（severity=critical） | 读原图核对的结果 | 判定 |
|---|---|---|---|
| **p2** | 「车间负责人审核日期 2025.01.30 **晚于**当前日期 **2026.09.18**」 | 该处手写确为 **2025.09.30**，年份 2025 **早于** 2026 | **假**（方向反） |
| **p6** | 「接收结束时间 **13:6** 早于接收开始时间 09:00」 | 该行三格是：开始 `09 时 00 分` / 结束 **`09 时 52 分`** / 体积 **`13.6 m³`** ⇒ 顺序**完全正常**；LLM 把**体积**串成了**结束时间** | **假**（跨字段串位） |
| **p6** | 「操作者签名日期 **18222** 无法识别」 | 该页签名为「郑雅」「李伟胜」，**图上不存在 `18222`** | **假**（凭空数字） |
| **p17** | 「13:18 时 P3 (MPa) 的实际值为 **`A`**」「F3 累计流量 (L) 的实际值为 **`2025.01.21`**」 | 附表 6 的 **P3 列四行全是 `0`**；F3@13:18 = **`2578`**；`2025.01.21` 是**表格外的手写签名日期** | **假**（列串位，与 B1-5 一致） |
| **p39** | 「步骤 6/7 中…参数值为 **否**，不符合规格范围」 | 序号 6/7 的**是/否框全部勾的是「是」** | **假**（勾选读反） |

**由此新增三类形态**（都可复现，均已登记）：

- **形态 D：跨字段串位**（p6 把同行的「体积」值配到「结束时间」上）。
- **形态 E：布尔项勾选读反 + 类型错配**（p39 原件勾「是」判「否」，且塞进
  `param_out_of_spec`——那是**数值超差**类型，不适用于是/否项）。
- **形态 F：凭空数字**（p6 的 `18222` 在图上无源）。

**共同的根因（本轮最重要的判断）**：这三类与 B1-4/B1-5 **同源** ——
**LLM 在"自由裁量 + 自由取值"**：既自己从 OCR 文本里挑值，又自己下判定。
⇒ **提升精度的主路径不是"再调提示词"，而是把"取值"与"判据"从 LLM 手里收回到规则层**
（LLM 只负责抽取结构化值，判定交给确定性代码）。这条与 15.2 第 3 条（规则层墙钟基准）
是一体两面：**基准要固化、取值要可追溯、判据要可复现**。

**同时得到的反面证据（同样重要）**：p2 上清晰可读的「开始生产日期 2025.01.20」
在 p38 的 finding 里被引用为**生产日期 2025年01月20日**（正确），
却在 p28 的 finding 里变成了**生产日期 2026-09-18**（= 当前日期）
⇒ **同一次运行内"生产日期"这个基准被各页各解一次**，B1-4 确认无误。

## 15.4 旧登记项复核（含 2 条"登记已过时"）

| 旧登记 | 本轮复核 | 证据 |
|---|---|---|
| **B4-1**「SSE 未见 `id:` / 无心跳注释帧」 | ❌ **登记过时**：`id:` 帧**早已存在**（`api/jobs/status.py:401/410/418/422`、`listings.py:194`）；且循环**每 2s 必发一个真实数据帧**（`_SSE_POLL_SECONDS=2`）⇒ **本身即保活**，不需要额外注释帧。`Last-Event-ID` 未解析是**有意的设计**（每帧都是幂等全量快照，重连即自愈，见 `status.py:417` 注释） | 读码确证 |
| F5 **#155**「upload.js 只 `onmessage` 会漏 `done`」 | ❌ 对复核页**不适用**：`static/review.js:437` 已有 `es.addEventListener("done", …)` | 读码确证 |
| 「OCR 丢小数点 `4.6`→`46`」 | ✅ **已修**，仍成立（`rules/parsing.py:_decimal_loss_factor`）；**不得再当缺陷报** | 沿用 §14.2 |
| 「无运行时看门狗」 | ✅ 已修 | `core/watchdog.py` |
| 「#127 绿点假成功」 | ✅ 机制已落地（v1.1.6，`4513395`）；残留 = 分片路径（#144）+ 验收未跑（B1） | 读码确证 |
| 「provider 静默改写」（§14.4 **未复核**） | ⚠️ **本轮已确证**（见 15.2 第 1 条），并**修正**了"完全静默"的转述 | 读码确证 |
| 「规则层判据方向」 | ✅ **逐条核过，未见反转**（R1a/R1b/R5/R9a/R4 语义与注释一致）—— 阴性结果，同样有价值 | 读码确证 |

## 15.5 分发判定

**可以分发，但必须把"精度"写进交付说明。**

- 程序面：版本四处一致 `1.1.9`、`asar` 内版本 `1.1.9`（**子进程读**，避免锁死）、
  `win-unpacked` 内嵌 exe 与 `dist/pbc-server/pbc-server.exe` **sha256 相同**
  （`cfe28a30…`，20,340,967 B）、`BatchSentry.exe` 188.8 MB、
  `clean_dist.py` dry-run「待清理: (无)」⇒ **不存在"测 A 发 B"**。
- 功能面：Round 42 的 7 轮产物级 e2e 全过（含 **51 页真实文档** 293 findings / 14 类）。
- 启动体验：Electron 层健全 —— splash 即时反馈 + 预检端口 + spawn 失败**立即失败**
  （非空等 30s）+ `dialog.showErrorBox` + 端口冲突给用户选择 + `backend-boot.log` 落盘。
- **但**：51 页产出 **55 条 critical**，本轮抽样即坐实多条**假 critical**
  ⇒ 交付时须写明「**结论需人工复核，不可作为唯一放行依据**」（`DEPLOYMENT.md` 已有该口径）。

## 15.6 未复核 / 诚实留白

1. **他机验证**：全部结论仍为**本机**证据；GUI 只能人工双击。
2. **anthropic 厂商真机**未连通（仅本地协议桩）。
3. **无真实标注集** ⇒ 仍**没有任何可信的 P/R 数字**；本轮"假阳性率"只是**抽样**（5 页 4 例），
   **不可外推为整体比例**。
4. 本轮**未重跑** 51 页全链路（复用 Round 42 的隔离库与产物；产物 sha256 已核一致，
   故结论对当前产物有效）。
5. 五路审查中的 P2/P3 均为**静态读码**结论，除 15.2 第 2/3/6 条外**未做运行时触发验证**。

## 15.7 v1.2.0 迭代清单 → 已写入 `docs/TODO.md`


