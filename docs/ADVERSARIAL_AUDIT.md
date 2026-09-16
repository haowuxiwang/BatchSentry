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

---

## 0. 一页结论

| 问题 | 结论 | 证据强度 |
|---|---|---|
| 构建物能否分发 | **技术上可以**（便携版 + 分发一致性机检在），但**从未在干净机器上验证过**，且打包链在本机被安全软件驱动级阻塞 | 中（有产物与机检，无他机验证） |
| 日志与状态机是否完善 | **状态机是显式的、有审计、有崩溃恢复**；**但缺运行时看门狗** —— 单进程长跑会留下永久非终态的 job | 高（实测代码路径） |
| 流式输出是否合范 | **架构合范**（单一常量、终态快照、三色面板）；**但 SSE 的终止性没有兜底**（同上无看门狗） | 高 |
| 设置能否正常设置 | **能设、且热生效**（config.json 原子写 + LLM 客户端重置）；**但有 1 个 P1 会让用户"设了不生效"**（provider 被启动逻辑静默改写） | 高 |
| pipeline 是否无阻塞 | **无 P0**：全部外部调用都有超时，慢 job 不饿死他者；有 3 处 P1/P2（`self_heal` 的 GIL 阻塞、Stage3 同步 CPU、procpool 全局串行） | 高 |
| 鲁棒性 / 泛化 / 抗挫折 | 尺寸/方向/坐标/清晰度/形态 5 个维度**都有阈值与降级**，且原则一致（宁缺勿错）；**抗挫折最弱在"进程级"**（无看门狗） | 中高 |
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
1. **安全软件驱动级持有** `resources/app.asar`：`tasklist` 查不到、重启不释放 →
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

**唯一缺口**：SSE 的**终止性**依赖状态机进入终态。既然 §2 的 P0（无看门狗）存在，
一个卡住的 job 会让 SSE **永远等下去**，而前端没有"超过 N 分钟仍是 running →
明示可能已卡住"的提示。这是同一根因的第二个症状，修一处即可。

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
  - **进程层：无看门狗**（§2 P0）→ 这是抗挫折唯一的真实缺口。

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
| 2 | **运行时看门狗**（§2 的 P0） | 改动面触及状态机与 SSE，属"关键路径"，想先确认要不要在本轮做 |
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
