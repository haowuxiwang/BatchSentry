# BatchSentry v1.1 执行计划（粒度到任务）

> 配套文件：`docs/ROADMAP_v1.1.md`（回答"为什么做、做什么"）。
> 本文件回答 **"怎么做、做到什么程度、怎么验收"** —— 每条任务都有：文件、做法、验收、依赖、覆盖率影响。
> 编制：2026-09-11（全部结论基于当日**实测代码**，非文档转述）。
> 执行纪律（用户定）：**先定位（拿真值）→ 再解决（最小改动）→ 再测试（含覆盖率门禁）→ 最后验打包信号**。

---

## 0. 进度总览（实测）

| 里程碑 | 状态 | 证据 |
|---|---|---|
| M1a 覆盖率 92% | ✅ | `093ebf8`；`pytest.ini --cov-fail-under=92` |
| M1b 覆盖率 95% | ✅ | `ebf1d96`；**95.12%**（377/7718 未覆盖），门禁已置 **95** |
| M1c 打包信号脚本 | ✅ | `1bbd387`（T0）；junitxml 事实源 + 原始输出落盘 + 环境专有失败 WARN 标注 |
| T0 门禁自愈 | ✅ | `1bbd387`；`OVERALL: pass`（4 PASS + 1 WARN），1567 passed |
| M2 Finding 质量可度量 + 降噪 | ✅ | `93d7896`+`d978f88`；金标 P/R/F1=1.0；真实 51 页 job 784→268（-65.8%），critical 33→33 全保留 |
| M3 OCR 尺寸/形态鲁棒性（O1/O2/O3/O6） | ✅ | 本文件 §2 M3；`test_ocr_robustness.py` 42 例 + `tests/e2e_ocr_integrity.py` 20 断言全通过 |
| M4 规则扩展 R11–R17 + 注册表 | ✅ | `f0ebdc`；金标 **11 例 P/R/F1=1.0（0 known_gap）**；真实 51 页重放 **+9 findings（390→399，+2.3%）、critical 12→13**，全部为真信号（无 deviation_link/alteration 噪音） |
| M5 知识库多源化 + 条款级溯源 | ✅ | `4270fd9`+`f9b73df`；**6 源 / 441 条**；金标 37 条命中率 **97.3%**（排除已登记缺口 100%，阈值 85%）；门禁 95.22%/1845 passed |
| M6 SSE 优化 + 对标落地 | ✅ | 本文件 §2 M6 落地结论；S1 常量统一（源码扫描锁死）、S2 稳态查询 **16→1**、S4 秒级本地计时、T6.4 三色分级（六面同源机检）、T6.5 评估判**不做**（`docs/TREND_SCREENING_EVAL.md`） |
| M7 docling 第三对照引擎 | ✅ | 本文件 §2 M7 落地结论；T7.1 能力表单一来源（源码扫描护栏）、T7.2 缺失即降级（`OcrBackendUnavailable`→回退 Paddle）、T7.3 三引擎对比脚本、T7.4 ROADMAP 许可残留改 MIT |
| M8 打包放行（v1.1.0） | ✅ | 门禁 **OVERALL: pass**（2001 passed / 1 已登记沙箱专有失败，覆盖率 **95.17%**）；真实 51 页 e2e **两轮通过**（首轮 paddle 落库 395 条 / 第二轮 mineru 落库 407 条，`ALL ROUNDS PASSED`）；**看图比对**（`docs/M8_VISUAL_VERIFICATION.md`）定位并修复 4 类规格缺陷（消除 **37 条误报 / 9 条 critical**）；tag `v1.1.0` 已打并推送（commit `233a0ff`） |

**当前工作区**：干净；**与 `origin/main` 同步**（`8cd0dc1`，v1.1.2）。推送要点：凭据在 Windows 凭据管理器，但端口默认的 `credential.helper=helper-selector` 是 GUI 选择器、**无凭据时会静默挂住** → 必须 `git -c credential.helper= -c credential.helper=wincred push`（先清空助手列表，否则 GUI 助手仍排在队首）。
⚠️ **网络结论已更新（2026-09-15 实测）**：此处原写"GitHub 直连会被 RST、必须走系统代理 `127.0.0.1:7897`"——**已不成立**。实测该端口未监听（`netstat` 无输出、经代理请求返回 000），而**直连 GitHub 返回 200 且 push 成功**。正解是**两条路都备着**：先探测代理是否在听，代理不可用就清空 `HTTP(S)_PROXY` 走直连；不要把任一条写死（写死代理会在代理未开时直接失败，掩盖可用的直连）。

**打包信号当前结论**：T0 自愈后 **OVERALL: pass**；M8 放行前复跑仍 **pass**（2001 passed / 1 已登记沙箱专有失败，覆盖率 95.17%）。每次放行前须复跑 `scripts/release_gate.py`（注意 `--python` 需传 **Windows 路径**，POSIX `/c/...` 会判"python 不可用"）。

**M8 看图比对结论（新增）**：用多模态直接读 `samples/丝裂霉素提取批记录.pdf` 原页，与落库 findings 逐条对照，定位 4 类缺陷：`±`→空格（p08 温度 21 条）、OCR 丢小数点（p08 进料压力 8 条）、LLM 自报超差无复核（p09 压力 7 条 critical）、真空度符号约定未 fail-closed（首轮 6 处 / 第二轮实测 17 条改判人工）。**精确核对共消除 37 条误报（含 9 条 critical）**。另有 2 类未决项归抽取质量：p9 使用次数手写 `17` 被读成 `417`（根因：OCR 把批次号与使用次数粘进同一 `<td>`，raw_html = `A000626-221201/417 次`）；p19 手写纯度主备后端读数分歧（paddle `49.0` 误报 / mineru `99.0`，6× 实读确认为 **99.0**，`≥98%` 合规 → 印证"需后端分歧仲裁"而非"标注值失真"）。详见 `docs/M8_VISUAL_VERIFICATION.md`。

**M8 真实 e2e 两轮**：首轮 paddle（`a5284ca3-47b`，落库 395 条）、第二轮 mineru（`55c50601-b99`，落库 407 条，`ALL ROUNDS PASSED`，`gmp_basis` 50/50，稀疏页 0）。第二轮首轮后端切换由**上游限流**触发（audit `ocr_failover …paddle→mineru… 任务提交队列已满`），非本地缺陷。⚠️ e2e 驱动打印的 "N findings" 取自 `GET /api/jobs/{id}/findings`（**`limit` 默认 50**），是首页条数而非总数。

---

## 1. 深度调研结论（逐问作答，附代码证据）

### Q1. 当前流式输出（SSE）是否合理？

**结论：架构合理，有 4 处待修，且 S1 是硬伤（文案与代码不一致）。**

实测（`api/jobs/status.py`）：
- L230 docstring 写 **"每 2 秒收到一次进度更新"**，但 L259 / L281 实际 `await asyncio.sleep(3)` —— **文档与代码不一致**（S1）。
- L243 `yield "retry: 2000\n\n"`（2 s 重连）与 3 s 推送间隔**不同源**（S1）。
- L90 注释自述"每 3 秒一次的 SSE 轮询此前 `SELECT *` 全列"——已优化列，但**每 tick 仍全量重算**，无终态缓存（S2）。
- 设计优点（保留）：幂等全量快照 + `Last-Event-ID` 断线自愈 + `retry` 提示；SSE 消费端与 DB 轮询解耦。

→ 处置：**T6.1 统一轮询常量为 2 s 并修正文案 + 单测锁定**；**T6.2 复用 `listings.py` 的终态快照缓存范式**；**T6.3 前端补"阶段内已耗时"秒级本地计时**（Stage3 单次大调用 330 s+ 期间观感像卡死）；S3（事件总线）列为 v1.2。

### Q2. 当前 OCR 能否完整解析不同尺寸的 PDF 和图片？

**结论：大页面盒已解决，小页面盒 / 极端长宽比 / 混合尺寸 / 小字号仍为空洞。**

实测（`core/pipeline/ocr_support.py`）：
- 已有：`_PDF_ABNORMAL_BOX_PT = 1600`（仅**上界**）、`_NORMALIZE_TARGET_DPI = 300`、`_NORMALIZE_MAX_SIDE_PX = 4096`、`_LOW_DPI_THRESHOLD = 150`、灰度 JPEG q85 工作副本、双后端 failover（`_run_ocr_with_failover`，缺页阈值 `max(2, 10%)`）、空页自愈、旋转自愈。
- **O1 小页面盒**：`_prepare_ocr_pdf` 只判 `max(rect) > 1600`，**无下界**→ 微型标签/连续表单不放大。
- **O2 超长连续页**：无 aspect-ratio 切分，极端长宽比正文被切碎。
- **O3 混合尺寸**：仅"逐页 >1600pt 才重渲染，否则原样拷贝"，**不按目标像素密度统一**→ 混排文件后端表现漂移。
- **O6 小字号（<6pt）**：无联合标记。
- 图片上传路径（`api/jobs/upload.py::_image_to_pdf_sync`）已按 **300 DPI 映射**（`pt = px * 72/300`），**图片侧是统一的**，问题集中在 PDF 侧。

→ 处置：M3（T3.1–T3.6）。

### Q3. 当前规则是否合理？是否还需要新增？

**结论：设计稳健，覆盖有系统空洞，需新增 R11–R17。**

实测：`core/rules/*.py` 共 8 模块、2667 行、**15 个 `_check_*` 规则函数**；规则类型实测产出 9 类（`batch_inconsistency`/`completeness`/`handwritten`/`param_out_of_spec`/`signature_time_anomaly`/`step_gap`/`suspicious_date`/`time_reversal`/`year_contradiction`，另低置信/签名不一致/勾选一致性等经 `rule_doc.py` 产出）。

- 防误报设计到位：R10 缺口阈值、R7 批号变体归并、R-M1/M2 小样本豁免、R9a 同角色豁免、R3 手写边缘降级。
- 偏保守处：R6 的 `reviewer`/`qa` 缺失统一 `info`（可按工序关键性分级）；R3 手写 ≤10% 才降级（阈值可标定）。
- **系统空洞**（GMP 高频、当前完全未检查）：物料平衡/收率、**操作人=复核人**、设备清洁状态、环境监测、文件版本一致性、超限偏差关联、涂改规范 → 即 **R11–R17**。

→ 处置：M4（含**规则注册表** T4.0）。

### Q4. 如何降低 finding 噪音、提升质量/准确性/精确性？

**结论：先"能度量"，再动刀；五条措施按证据强度排序。**

实测：
- `confidence` **未落库** —— `api/review.py:23 _confidence_for()` 仅在读接口 L249 现算（无 schema 列）。
- 降噪 N1 **过粗** —— `core/pipeline/stage3.py:122` 按 `(page, type)` 抑制 LLM finding（会误杀同页同类型的不同问题），且被抑制项只记计数（L190 审计），**无明细**。
- `LLM_JSON_MODE` 默认 **`"false"`**（`config.py:616`）→ 曾触发 330 s 的 fix-hint 重试。
- `TYPE_QUERIES`（`core/kb/retriever.py:33`）仅覆盖 11 类，**缺 `step_gap`** 等规则类型。

→ 处置：M2 全量（T2.1–T2.11），核心顺序是 **金标集 → eval 脚本 → 基线数字 → 再优化**。

**Q4 续（M8 后复盘，2026-09-14）**：上述 Q4 的两条实测已过期，且降噪在真实数据上暴露出新的一类——
**"数字本身即错"**（p9 使用次数手写 `17` 被 OCR 读成 `417`、p19 主备后端读数分歧 `49.0`/`99.0`），
**规则层与 LLM 层同时中招，任何"规格 vs 实测"复核都发现不了**。为此做了第二轮调研（主流产品 +
受监管行业实践），结论与三档决策见 **`docs/NOISE_REDUCTION_RESEARCH.md`**。要点：

- 更正：`confidence` **已落库**（`findings.confidence`，由 `core/finding_quality.py::confidence_for` 写入，
  取值 0.50–0.85 离散）；但它是**"我们的确定性"代理（source × page_flag 映射）而非校准的正确概率**，
  不可作为阈值路由依据。
- 更正：N1 抑制**仍无明细**——`spec_guard.drop_unfounded_spec_findings` 只返回剔除计数，
  受监管场景下"删除"需留理由且可回退 → 列为 P0-2。
- 新增认知：降噪只能靠**引入独立证据**（第二读一致性 / 结构先验 / 列先验），不能靠"看起来像误报"的模糊判据
  （M8 的 `_decimal_loss_factor` 回归已实证软化过宽会吃掉真实超差）。

**Q4 三次（Spike 实测，2026-09-14）**：P0-1/P0-3 的**粒度**已用真实产物定死，见
**`docs/NOISE_REDUCTION_SPIKE.md`**；分项执行清单见 **`docs/NOISE_REDUCTION_TODO.md`**。要点：

- **不能按单元格对齐**：Paddle 与 MinerU 都只给**块/区域级** bbox（Paddle
  `prunedResult.parsing_res_list[].block_bbox` + `layout_det_res.boxes[].coordinate`；
  MinerU `content_list_v2[].bbox` + `layout.json` 的 line/span 级，但整表只有 1 个 span）。
  `_model.json` 里 `cell`/`rowspan`/`colspan` 出现 **0 次**。→ 可行粒度：**表级（bbox 粗筛）
  + 字段级（标签文本匹配）**，页级兜底。
- **PaddleOCR-VL 确实回传 bbox**，但是**区域级、不是单元格级**；另有 `layout_det_res` 的
  **版面检测 score**（实测 mean 0.579 / p50 0.545），**不是**文字识别置信度。
  坐标与 score 目前 **100% 被丢弃**（`page_cache` 无坐标列）→ 区域级证据锚是白拿的能力。
- **Paddle 上游不可用是常态**：spike 期间两次实测 `code 10010 任务提交队列已满`
  （与第二轮 e2e 触发 `ocr_failover paddle→mineru` 同因）→ P0-1 必须把"第二读取不到"
  定义为 `needs_arbitration`（fail-closed），不得静默采信第一读。
- **已修一项（T-P0-4）**：`_parse_spec` 的 7 种书写变体缺口（LaTeX `\pm`、全角 `～`、
  **单位夹分隔符** `972 μg/mg ~1020 μg/mg` 等）；真实 3526 个 `(spec,value)` 对照验证：
  22 处转为可判定、**解析结果变化 0 处、新增超差 0 处**。
- **更正 M8 表述**：p9「417」的根因不是"两个相邻单元格粘连"，而是 **Paddle 把多行区块压成
  1 个 `<tr>` + 两个多行 `<td>`（标签列/值列），标签↔值只靠行序对应；分隔符形态在同一文档内
  不一致（p09 有字面 `\n`、p21 完全没有）→ 相邻两行的值融合**。属服务端非确定性行为，
  不做启发式还原。

### Q5. 当前知识库构建如何？

**结论：单源、可运行，但语料单薄；`source_id` 脚手架已在，扩源成本可控。**

实测：
- 语料：单源 `core/kb/data/gmp2010.json`，**300 条**（`release_gate` kb_corpus 实测；上限不限）。
- 检索：`core/kb/retriever.py` 字符 bigram 倒排 + BM25(k1=1.5,b=0.75)，TopK=4、excerpt 120 字、budget 1500 字；页面级注入 `build_page_kb_context`（TopK=5/excerpt 200）。
- **`db/schema` 的 `kb_entries` 已含 `source_id` 列**，`store.py::source_meta()/kb_version()` 已就绪 → **多源只需扩种子管线 + 查询维度**，无需改表结构。
- 引用：`article_label` 已随检索返回（`_Index.search` L108 `"label": e["article_label"]`），§5.3 的"条款级引用"已有落地基础。
- 缺：附录、ICH Q7/Q9/Q10、ALCOA+、Annex 11、21 CFR Part 11。

→ 处置：M5。

### Q6. 可借鉴的产品 / GitHub 项目（2026-09-11 重新检索）

**① 本次新发现（可直接引入/借鉴）**

| 项目 | 价值 | 对应里程碑 |
|---|---|---|
| **`1999XIAOZHANG/gmp-csv-validator-skill`** | **现成的 6 部法规原文语料**（中国 GMP 2010 / 计算机化系统附录 / FDA 21 CFR Part 11 / EU Annex 11 / WHO TRS996 数据完整性 / ICH Q9），零依赖、每条整改项定位到条款原文 → **可直接作为 M5 多源语料来源**（引入前核许可） | **M5** |
| **`memopena/regulated-multiagent-reference`** | "agents propose, rules decide"；**每个合成件自带答案键，eval 用 precision/recall 双 1.0 卡 CI**；含 prompt-injection 夹具 → **为 M2 金标 + 评测门禁提供范式** | **M2** |
| **`MeyerThorsten/QAtrial`**（AGPL-3.0） | 开源 QMS：需求/风险/CAPA/电子签名/审计，GAMP5、FDA GMP starter packs | 参考（M6 面板/审计） |
| **`Sukarth/ai-audit-aid`** | 审计历史留存、**跨版本对比（已解决 vs 新增）**、SHA-256 内容去重缓存、严重度追踪 | 参考（M2 评测/报告对比） |

**② 修正路线图的一处事实错误**

> `docs/ROADMAP_v1.1.md §7.2` 把 **docling 标为 AGPL-3.0 —— 有误**。
> 实测（2026-09-11）：**docling 为 MIT 许可**，已由 IBM 捐给 **Linux Foundation AI & Data**，当前版本 **2.126.0**，定位"企业级多格式解析流水线"，表格结构化（TableFormer/DocLayNet）为强项，支持 PDF/DOCX/PPTX/XLSX/HTML/图片等，输出 `DoclingDocument`（结构化 JSON）+ Markdown。
> **含义**：M7 引入 docling 作第三对照引擎**无 AGPL 分发风险**，比原评估更安全。（MinerU 仍是自定义协议/AGPL 系，Marker 为 GPL+OpenRAIL，二者风险较高。）

**③ 持续沿用**：MinerU（已在用，中文最强）、Instructor/Outlines/Guardrails（结构化输出）、GMFT/TableTransformer（表格交叉校验）、gxpeval（GxP 评测）。

---

## 2. 详细执行计划（M2–M8 + T0）

> 通用验收线：**任何代码改动后，`release_gate.py` 必须 GREEN 且覆盖率 ≥95%**。
> 每条新能力**必须自带单测**；模块化（新能力独立文件/清晰接口，不堆进既有大文件）。

### T0 —— 门禁自愈 + 信号可信化（最高优先，前置一切）

**定位（已实测）**：2026-09-11 11:24 全量门禁 `OVERALL: fail`，`tests_coverage` 报 `1551 passed, 1 failed, coverage=95.12%；pytest 报告 1 项失败但未解析出用例`。
- 该失败为**环境专有、且 flaky**：同命令 `--tb=no` 复跑为 `1551 passed, 0 failed`（沙箱 `safe-delete` 状态相关）。
- 根因：`release_gate.py::_parse_pytest_summary`（L190–216）依赖文本解析 `FAILED <nodeid>` 行；当失败行形态不符/被日志 `log_cli` 交错打散时，`failed>0` 但 `nodeids` 为空 → 落入 L278 分支判 FAIL（fail-closed，方向正确但**信号不可信**）。

**任务**
| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T0.1 | 改用 **`--junitxml`** 作为机器可读事实源（不解析文本） | `scripts/release_gate.py` | 失败用例 nodeid 从 XML 取，100% 可复现 |
| T0.2 | 门禁**落盘原始 pytest 输出**（失败时 `devlogs/gate_pytest_<ts>.log`） | 同上 | 事后可回溯，不再"未解析出用例" |
| T0.3 | 环境专有失败 allowlist 保留（`TestServePdf`），但**必须在报告中显式标注** | 同上 | WARN 语义清晰 |
| T0.4 | 单测覆盖新解析路径（正常/无 nodeid/日志交错/XML 缺失降级） | `tests/unit/test_release_gate.py` | ≥6 新例，覆盖率不降 |

**依赖**：无（最先做）。**产出**：可信的 go/no-go 信号。

---

### M2 —— Finding 质量可度量（先度量，后优化）

**目标**：把"降噪靠手感"变成"准召/F1 可证明"。

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T2.1 | 定义金标 schema（页号/type/位置指纹/期望严重度/应报与否，**不含业务原文**） | `docs/FINDING_GROUND_TRUTH.json` | schema 文档化 |
| T2.2 | 采集金标（丝裂霉素 51 页 + 合成件；借鉴 `regulated-multiagent-reference` 的"答案键"法） | 同上 | ≥1 个完整 job 的金标 |
| T2.3 | 离线评测 `scripts/eval_findings.py`：precision/recall/F1 + 按 type 分解 + 误报 Top + 漏报清单 | `scripts/eval_findings.py` | 对 Round 23 e2e 产物出 **v1.0 基线数字** |
| T2.4 | **confidence 落库**：schema v10 加 `findings.confidence REAL`，写入时计算（`_confidence_for` 逻辑下沉到 core，避免 api→core 反向依赖） | `db/`+`core/` | 可 SQL 排序/阈值；旧 job 读时兜底 |
| T2.5 | **语义去重**（LLM 判重，**无 embedding**）：规则 finding 为权威 + LLM finding 与其判重；保留真新增；失败降级回键去重 | `core/pipeline/stage3.py` | 误杀率可测下降；离线可用 |
| T2.6 | **类型白名单**：prompt 内 enum 强约束 + 未知 type 归并到最近规范类型（原文 type 存 `raw_type`） | `core/rules/llm_checks.py` | 未知 type 归零 |
| T2.7 | **finding 级证据锚定** `evidence_ref`（页内字符偏移/片段指纹）；page 级 `_grounding_warn` 下沉 finding 级 | `core/page_analyzer.py` 等 | 复核页可高亮跳转 |
| T2.8 | **抑制可解释**：被抑制的 LLM finding 写审计明细（哪条被谁覆盖） | `core/pipeline/stage3.py` | 审计可回看 |
| T2.9 | 提示校准：**按 type 分桶**注入 few-shot；误报样例显式标"禁止同类输出" | `core/rules/llm_checks.py` | 分桶命中 |
| T2.10 | `LLM_JSON_MODE` **默认改 true**（保留网关不支持自动降级） | `config.py:616` | 默认开；降级路径有单测 |
| T2.11 | **Instructor 式验证+Reask**：补 Pydantic 语义校验（当前仅结构校验） | `llm/client.py` | `_schema_warn` 比例下降 |

**依赖**：T0 可信门禁。**产出**：质量基线数字 + 误报清单。**风险**：T2.4/v10 迁移需兼容旧库。

---

### M3 —— OCR 鲁棒性（不同尺寸/形态）

**目标**：补齐 O1/O2/O3/O6，且**不静默标记成功**。

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T3.1 | **O1** 新增 `_PDF_SMALL_BOX_PT` 下界，小页面盒放大到目标 DPI | `core/pipeline/ocr_support.py` | 微型页不再整页稀疏 |
| T3.2 | **O2** 极端长宽比：按内容带切分或提高渲染上限 | 同上 | 超长页正文不碎 |
| T3.3 | **O3** 规范化改为按**目标像素密度**统一（不再仅 >1600pt 触发） | 同上 | 混合尺寸表现一致 |
| T3.4 | **O6** 小字号 + 低 DPI **联合标记**，强制人工复核 | 同上 + `assess_ocr_page` | 不静默成功 |
| T3.5 | 合成样本生成器 `gen_*.py`（O1/O2/O3/O6 四类）+ 扩充样本表 | `devlogs/`/`docs/OCR_GOLDEN_CORPUS.md` | 每类可重复最小回归 |
| T3.6 | e2e 断言"该页不得静默标记成功" | `e2e_*.py` | 断言通过 |

**依赖**：无（可与 M2 并行）。**产出**：新增样本全过 + 无静默成功。

**实现说明（已落地）**：

- 触发条件从"仅长边 >1600pt"改为**几何带 + 长宽比**（O3 按目标像素密度统一）：
  `_box_geometry_reason` 判定顺序 极端长宽比 → 超大盒 → 微型盒 → 正常。
- 缩放策略按类别分流（`_normalize_zoom`）：**微型盒放大到目标 DPI**（300/72，
  页盒尺寸不变、密度升到可用区）；**超大盒不放大**（不伪造像素，仅受
  `_NORMALIZE_MAX_SIDE_PX` 约束 → 300dpi 输出即恢复真实物理尺寸）；
  **极端长宽比**放宽长边上限至 8192px 且抬升短边至 ≥1024px（O2 "提高渲染
  上限"路线；不做页面切分以免破坏 finding 的页号映射）。
- **微型盒仅在含嵌入栅格时重渲染**（`has_raster`）：矢量/文本微型页保留
  矢量保真，交由后端自身按文本层光栅化（避免把小尺寸矢量标签转成低质图）。
- O6 联合标记：`_pdf_page_diagnostics` 新增 `image_coverage` / `min_font_pt` /
  `small_font`；`assess_ocr_page` 在"小字号(<6pt) + 低 DPI(<150)"时输出合并
  原因并置 `integrity=incomplete`（结构化键 `low_dpi`/`small_font`/
  `extreme_aspect` 可机检）。极端长宽比与超大盒同属几何异常 → **不重复告警**。
- 可重复合成样本：`scripts/gen_ocr_samples.py`（O1/O2/O3/O6 + 两个对照/守护）
  写入 `devlogs/ocr_samples/`；离线 e2e `tests/e2e_ocr_integrity.py`（20 断言）；
  冻结包轮次 `python e2e_run.py --rounds robust`（"不得静默标记成功"）。

---

### M4 —— 规则扩展（R11–R17 + 注册表）

| ID | 任务 | 规则/type | 依据 |
|---|---|---|---|
| T4.0 | **规则注册表**（id/type/severity/basis/开关），替代散落硬编码 | —— | 使"规则是否合理"可配置可审计 |
| T4.1 | R11 物料平衡/收率 | `mass_balance` | GMP 生产管理 |
| T4.2 | R12 操作人≠复核人 | `self_review` | ALCOA+ Attributable |
| T4.3 | R13 设备/清洁状态完整性 | `equipment_state` | GMP 设备与清洁 |
| T4.4 | R14 环境监测完备性 | `env_monitor` | GMP 厂房设施 |
| T4.5 | R15 文件版本一致性 | `doc_version` | GMP 文件管理 |
| T4.6 | R16 超限偏差关联 | `deviation_link` | 偏差管理 |
| T4.7 | R17 涂改规范 | `alteration` | ALCOA+ Original |
| T4.8 | 每条规则 **≥4 单测**（正例/反例/边界/畸形） | —— | 覆盖率不降 |
| T4.9 | 前端 `type_zh` **双端同步**（`templates/review.html` + `static/review.js`） | —— | 两端一致 |
| T4.10 | `gmp_basis.py::GMP_BASIS_MAP` + `retriever.py::TYPE_QUERIES` 补齐新类型 | —— | 每条有依据+可检索 |

**依赖**：M2（评测口径就绪，才能证明新增规则**净增价值而非净增噪音**）。**产出**：每规则 ≥4 单测。

**M4 落地结论（2026-09-11，`f0ebdc`）**
- 触发键全部经**真实 51 页**（job `33495b33-761`）逐字段核查后确定，不做"理论上该查"的空规则：
  收率/物料平衡在 `parameters[]`；操作人/复核人在 `step.operator/reviewer`+`signatures[]`；
  设备/清洁与环境是"是/否"确认项（落在 `parameters[]`，**`checks[]` 实为空**）；
  `page_info.file_code` 跨页**合法地各不相同**（R20/R22/R23… 是不同表单）→ R15 改为
  "同一 file_code 多版本"，而非"全页同编号"（否则海量误报）。
- **净效果实测**（`scripts/replay_rules.py`，离线规则层重放）：**+9 findings（390→399）**，
  critical 12→13；新增 = doc_version 2（R23/R27 版本冲突，与定位预测一致）、self_review 1
  （critical，真实自检自核）、equipment_state 5、env_monitor 1；deviation_link/alteration
  真实数据零误报。金标 11 例 **P/R/F1=1.0**（R12 由 known_gap 翻为验收）。
- 顺手修复：`parsing._parse_spec` 支持 `"99%~101%"`（"%" 夹在数字与 `~` 之间原会解析失败，
  使物料平衡率规格整条降级为 LLM 兜底）——R3 与 R11 同时受益。
- 新增不变式测试：注册表结构（id 唯一/type 规范/依据齐备/R3 先于 R16）、
  类型六面同步（zh_map/前端双端/GMP 依据/检索词/白名单）、前端双端逐键一致。

---

### M5 —— 知识库升级（多源 + 条款级引用）

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T5.1 | 多源语料：GMP2010 **附录** / ICH Q7/Q9/Q10 / ALCOA+ / Annex 11 / 21 CFR Part 11 / 江苏记录规范（可复用 §1-Q6 发现的开源语料） | `core/kb/data/*.json` | ≥6 源 |
| T5.2 | `seed_kb.py` 改**多源种子管线**，每源独立 `source_id` + content hash 版本 | `scripts/seed_kb.py` | 可重复种子 |
| T5.3 | `source_id` 查询维度（表已含该列，`store.py` 已就绪） | `core/kb/store.py` | 按源过滤 |
| T5.4 | **`TYPE_QUERIES` 全类型覆盖**（补 `step_gap` 等 + M4 新类型） | `core/kb/retriever.py` | 无遗漏 |
| T5.5 | 条款级引用：`article_label` 随检索返回（**已具备**，补报告渲染） | 报告模板 | 引用可追溯 |
| T5.6 | 设置页知识库分区：多源开关/版本/命中预览；报告按源分组 | `templates/`+`static/` | UI 可用 |
| T5.7 | 金标查询集 ≥30 条（type→应命中条款） | `docs/` | **命中率 ≥85%** |
| T5.8 | e2e 断言 findings 带 `kb_refs` 比例维持 **100%** | e2e | 断言通过 |

**依赖**：M2（评测口径）；T5.1 依赖许可核查。**产出**：命中率 ≥85%。

**M5 落地结论（2026-09-14，`4270fd9`+`f9b73df`）**
- **语料（6 源 / 441 条）**：GMP2010 正文 313 + 计算机化系统附录 24 + 确认与验证附录 54
  + NMPA《药品记录与数据管理要求（试行）》30 + ALCOA+ 10 + 21 CFR Part 11 10。
  全部取自**权威源头**并在 `raw/*.md` 的 provenance 头标注 title/effective/authority/
  document_no/origin_url/license/retrieved_at；外文源为"中文摘编 + 英文原文"双文本
  （中文保证 bigram 检索可命中，英文保留逐字原文供对照）。
  **许可结论**：`1999XIAOZHANG/gmp-csv-validator-skill` **无 LICENSE**（`license: None`）
  → 不可引用，改为自行从 govinfo/eCFR/NMPA/PIC-S 等公开权威源头按条摘编并标注出处。
- **检索质量（以金标集实测选型，不靠猜测）**：三处修复，命中率 59.5% → 83.8% → **97.3%**：
  ① **idf 改标准 BM25 对数形式** —— 原实现漏了对数，df=1 的词权重高达 ≈277，
     而真正稀有的多是描述挖掘出的**噪声 bigram**（"数超"/"围但"），把权威条款挤出前列；
     取对数后 df=1 仅 ≈5.6。这是最大的一处偏差源。
  ② **长度归一化 B 0.75 → 0.4** —— 条文级短文本（avgdl≈87 字符），列举式权威条款
     往往较长，过强长度惩罚会惩罚正确条款。
  ③ **索引并入 `article_label`** —— labeled 语料把概念词写在标题里
     （"同步记录 Contemporaneous"），正文却是意译，只索引正文检索不到。
  另修正**词表缺口**：GMP 第二百五十条讲偏差用的是"**偏离**"（df≈1%）而非"偏差"（df≈6%），
  `TYPE_QUERIES` 原只收"偏差" → `param_out_of_spec` 金标因此掉榜。**词表纪律**：
  必须核对话料自身用词，不得凭常识写同义词。
- **语料完整性修复（严重）**：`seed_kb` 的中文数字字符类 `[一二三四五六七八九十百]`
  **漏了"零"**，导致编码含零的 27 条（第一百零一~一百零九 / 二百零一~二百零九 /
  三百零一~三百零九）**永远匹配不上**，其正文被静默并入前一条（第二百条正文污染）。
  对合规产品是硬伤（条款无法被引用 + 前条被污染）。修后 **286 → 313 条，条号 1..313 连续**。
- **打包/入库修复（静默失效）**：① `pbc-server.spec` 原**只带 `gmp2010.json`**
  → 冻结版其余 5 源会静默缺失；改为枚举 `core/kb/data/*.json`。
  ② `.gitignore` 的 `data/` 匹配了 `core/kb/data/` —— 而 CLAUDE.md 与 .gitignore 自身
  都记载"派生 JSON **ships**/入 git"，即**本应入库却被误吞**（克隆后语料消失）
  → 锚定为 `/data/`。两者均已加机检护栏。
- **新增门禁项 `kb_packaging`**：每个 `core/kb/data/*.json` 都必须被 spec 的 datas 覆盖
  （glob 或显式列举），防"新增源忘记入包"再次静默漂移。
- **验收实证**：`scripts/eval_kb_queries.py` 金标 37 条 **hit 36/37 = 97.3%**
  （排除已登记缺口 `q37 user_rule` → **100%**，阈值 85%）；`release_gate`
  **OVERALL: pass**（5 PASS + 1 WARN 沙箱 allowlist），coverage **95.22%**，1845 passed，
  `kb_corpus` 477 条 / `kb_packaging` 6 源自动入包。

---

### M6 —— SSE 优化 + 对标能力落地

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T6.1 | **S1** 统一轮询常量（2 s）+ 修正 docstring/`retry` + 单测锁定 | `api/jobs/status.py` | 文案与代码一致 |
| T6.2 | **S2** 终态快照缓存（复用 `listings.py` 范式） | 同上 | 稳态 QPS 可测下降 |
| T6.3 | **S4** 前端"阶段内已耗时"秒级本地计时 | `static/*.js` | Stage3 长调用不再像卡死 |
| T6.4 | **三色复核面板**（规则=红 / LLM 辅助=蓝 / system pass=绿），借鉴 Mareana | `templates/`+`static/` | 视觉分级 |
| T6.5 | **趋势筛查评估**（借鉴宝软 EIOS）：参数未超限但偏离历史区间 → 评估是否做 | `docs/` | 出评估结论 |

**依赖**：无（T6.1–T6.3 可先做）。**产出**：S1 一致 + S2 可测。

**M6 落地结论（2026-09-14）**

- **T6.1（S1）轮询常量统一** —— 定位到三方矛盾：`retry: 2000`（2s）但我服务端
  `asyncio.sleep(3)`、docstring 又写"每 2 秒"。后果是客户端重连快于服务端推送，
  断线重连立即拿到旧帧、白跑一轮。现统一为 `api.jobs._SSE_POLL_SECONDS = 2`，
  `retry:` 帧与两处 sleep 全部由此派生；`tests/unit/test_sse_poll_constant.py`
  以**源码扫描**锁死（禁止再出现 `retry: <数字>` / `asyncio.sleep(<数字>)` 字面量，
  并禁掉"每 3 秒"这类过时文案）。
- **T6.2（S2）终态快照缓存** —— 原先只有 `listings.py` 自持一份缓存，`status.py`
  的共用查询 `_get_job_progress` 没有 → 双套易漂移。现**收敛到
  `status.py::_get_job_progress` 单一来源**（键 `(status, finished_at)`，容量 100），
  并抽出 `cached_terminal_snapshot()` 供聚合流直接用**已查出的** `status/finished_at`
  拼键，避免为拼键再查一次 jobs 行。
  实测（`test_steady_state_query_count_drops`）：3 个终态 job 的聚合流一轮查询数
  **16 → 1**（终态 job 每轮 5 条 → 0），即 S2 的"稳态 QPS 可测下降"。
  *过程中修掉一个真实缺陷*：写入缓存时存的是同一个 dict 对象，调用方改字段会
  改脏缓存（跨请求串数据）→ 改为存副本、命中返回副本。
- **T6.3（S4）阶段内已耗时** —— `static/eta.js` 增纯函数 `tickPhase/showElapsed/
  fmtElapsed`（node 单测 5 组锁定）；`review.js` 用 **1s 本地 ticker** 刷新"已用 X"
  （不等 2s 一帧；Stage 3 单次 LLM 调用数分钟不再像卡死），`upload.js` 行内按帧
  刷新并配 1s ticker 重写"已用"段。*两处隐患已消除*：计时器建在 `connect()` 内会
  因重试叠加泄漏 → 上提到订阅作用域；`labelEditable` 防止断开/错误提示被 ticker 覆写。
- **T6.4 三色复核分级** —— 按**检出来源**分层（与严重度正交）：
  红=规则命中 / 蓝=LLM 辅助 / 绿=系统校验通过。映射**只在**
  `core/finding_quality.REVIEW_TIER_BY_SOURCE` 一处，服务端
  `attach_review_tier()` 挂 `tier` 到每条 finding（SSR 与 AJAX 共用），前端只读
  `f.tier` 不持映射表 —— 由 `tests/unit/test_review_tier.py` 机检（含"产出端所有
  `source` 字面量都必须有分级"的漂移护栏 + CSS 类名逐键对齐）。绿色 = 启用规则中
  0 命中的**类型**数（`core/rules/registry.rule_coverage`），口径为**全批次**
  （红/蓝为本页），并在面板上标明。
- **T6.5 趋势筛查评估** —— 结论见 `docs/TREND_SCREENING_EVAL.md`，**判定 v1.1 不做**：
  需求与法源成立（第二百三十五/二百三十八/二百五十三/二百六十六条 + 确认与验证
  附录第二十九条），但**数据前提不满足** —— 真实库 23 个 job 中实质只有 **1 个批次**
  （51 页 job 均为同一份丝裂霉素提取批记录 112701 的反复重跑；92 个"产品标识"实际
  是同一产品的自由文本变体）；参数名 37% 只出现一次不可作主键；仅 30% 参数可机械
  解析规格。单批次**无法标定阈值 → 验收不可证伪**，故不做，仅保留阶段 0
  （测量序列结构化沉淀）作为 v1.2 起点。证据由入库脚本
  `scripts/eval_trend_basis.py`（只读库）复现。

**门禁**：见收尾报告（`devlogs/gate_report_*.json`）。

---

### M7 —— docling 第三对照引擎

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T7.1 | OCR 后端**统一接口**抽象（`run_ocr` 协议 + 能力声明） | `core/pipeline/ocr_support.py` | 双后端回归通过 |
| T7.2 | docling 接入（**可选依赖**，缺失时优雅降级不影响主链） | 新 `core/docling_client.py` | 缺失即降级 |
| T7.3 | 三引擎对比脚本 + 差异页报告 | `scripts/` | 有对比报告 |
| T7.4 | 许可标注修正（docling = **MIT**，非 AGPL） | ✅ 已在 `ROADMAP_v1.1.md` 勘误（§决策锁定行4 / §7.2 / §7.3） |

**依赖**：M3（OCR 接口抽象）。**产出**：docling 缺失不影响主链。

**M7 落地结论（2026-09-14）**

- **T7.1 OCR 后端统一接口 + 能力声明** —— 背景：后端"能力"（能否分片 /
  能否按页子集重跑 / 是否本地）历史上散落在 `engine.py` / `stage1.py` /
  `dual_compare.py`，以 `== "paddle"` / `== "mineru"` 字符串字面量判定 ——
  新增第三后端时每处都要改、极易漏改漂移。现收敛为**单一来源**：
  `core/pipeline/ocr_support.py` 的 `OcrCapabilities`（frozen dataclass：
  name/label/local/requires_token/supports_slicing/supports_page_subset）
  + `_CAPABILITIES` 表 + `describe_backend()` / `supports_slicing()` /
  `supports_page_subset()` / `is_backend_available()` / `_REMOTE_BACKENDS`。
  所有后端显式声明共享 **run_ocr 协议**
  `run_ocr(pdf_path, progress_callback, job_id, cancel_check) -> list[dict]`。
  `engine.py` 切片决策、`stage1.py` 自愈/对比守卫、`dual_compare.py` 备选
  选择均改走能力表。**漂移护栏**：`tests/unit/test_ocr_backends.py`
  源码扫描 `engine.py`/`stage1.py`，出现能力比较字面量即失败。
- **T7.2 docling 接入（可选依赖）** —— 新增 `core/docling_client.py`
  （本地后端，无需 token/网络；`run_ocr` 同签名，输出 page dict 形状对齐
  `{"markdown":{"text"}, "page_count", "_source":"docling"}`）。**缺失即降级**：
  `is_available()` 只做顶层 import 探测（不拉起 torch）；未装时
  `_get_ocr_backend()` 抛可捕获的 `OcrBackendUnavailable`，`_get_ocr_chain()`
  捕获后**回退默认 PaddleOCR**，主链不受影响。`health.probe_all` 增加
  `probe_docling` 分支，如实上报本地依赖状态。
- **T7.3 三引擎对比脚本** —— 新增 `scripts/compare_ocr_engines.py`：
  对同一 PDF 跑可用的 2~3 个后端，逐页对比复用
  `dual_compare.compare_page`（阈值单一来源），输出引擎概览 + 两两差异页
  + 可选 JSON。缺失/无凭据的后端**优雅跳过并标注**（不抛栈，退出码 1）。
- **T7.4 许可标注修正** —— 本轮补齐残留：`ROADMAP_v1.1.md` 决策锁定行 4
  与 §7.4（原仍写 AGPL-3.0）已改为 **MIT**，与 §7.2/§7.3 一致。

**端到端**：`tests/e2e_m7_docling_absent.py`（真实 uvicorn + HTTP）**6/6 过** ——
`OCR_BACKEND=docling` 且未安装时：进程正常启动、链降级为 `['paddle']`、
`/api/health/downstream` 返回 200 且 `ocr.ok=false`（reason=未安装）、
对比脚本优雅跳过。

---

### M8 —— 打包放行（v1.1.0）

| ID | 任务 | 验收 |
|---|---|---|
| T8.1 | `release_gate.py` 全绿（T0 修复后） | OVERALL: pass |
| T8.2 | `build.ps1` 重打包（**真实 PowerShell**，非沙箱） | 产物生成 |
| T8.3 | **真实 51 页全链路** frozen e2e + ui_e2e | 全过 |
| T8.4 | 打 tag `v1.1.0` | tag 存在 |
| T8.5 | 更新 `CLAUDE.md` / `README` / `CHANGELOG` | 文档同步 |

---

## 3. 依赖关系与推荐执行顺序

```
T0 (门禁自愈)
 └─> M2 (质量可度量) ──> M4 (规则扩展)   ← 无评测不动规则
                     └─> M5 (知识库)
M3 (OCR 鲁棒性) ──> M7 (docling 对照)
M2..M7 全绿 ──> M8 (打包放行 → tag v1.1.0)
M6 可与 M2/M3 并行（S1/S2/S4 独立）
```

**推荐顺序**：`T0 → M2 → M3 → M4 → M5 → M6 → M7 → M8`
（M3 提前与 M2 并行亦可；M4 必须在 M2 之后，否则无法证明"净增价值"。）

---

## 4. 门禁与交付纪律

1. **每条任务**：定位（实测真值）→ 解决（最小改动）→ 测试（含新增单测）→ 覆盖率 ≥95%。
2. **每里程碑收尾**：跑 `scripts/release_gate.py`，报告落盘 `devlogs/gate_report_<ts>.json`。
3. **沙箱注意**：取覆盖率**必须**走 `coverage run ... && coverage report --fail-under=95`（勿用 `pytest --cov`，其 `cov.combine()` 在沙箱触发 safe-delete 批量守卫 → INTERNALERROR）。
4. **提交粒度**：一事一提交；提交信息记录"定位/解决/测试"三段。

---

## 5. 风险与明确不做

**风险**
- 95% 覆盖率对 `config.py`/`settings/*`/`mineru_client.py` 补齐需大量边界测试，可能暴露真实缺陷（按"先定位"处理，**不为过门禁改产品行为**）。
- 新增规则（M4）会推高 finding 数量 → **必须同批交付 M2 的评测与阈值调优**，否则净增噪音。
- M2 去重若引入 LLM 判重会增调用成本 → 保留"键去重"离线降级。
- 门禁 flaky（T0）若不修，任何"是否可打包"的判断都不可信。

**明确不做（本轮）**
- 不做 Web/移动端适配；不接真实 MES/LIMS；不换主 OCR 后端（第三方仅对照）；不做模型微调。
- 不引入 embedding/向量库（决策 3：维持纯 BM25）。

---

## 6. 决策记录（承接 ROADMAP_v1.1 §10）

1. 覆盖率：先 92% 再 95% —— ✅ 已达成（95.12%，门禁 95）。
2. 规则扩展：R11–R17 全做（M4）。
3. 语义检索：维持纯 BM25；语义去重用 LLM 判重（离线可用）。
4. 第三方后端：引入 docling 第三对照（M7）；**许可更正为 MIT**。
5. 打包放行：v1.1 必须跑真实 51 页全链路（M8）。
6. **新增**：门禁自愈 T0 前置（信号可信化）。
