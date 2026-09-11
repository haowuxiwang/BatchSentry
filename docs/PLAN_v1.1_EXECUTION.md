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
| M1c 打包信号脚本 | ⚠️ **需自愈** | `66203b4`+`ea4ae97`；脚本可用，但 2026-09-11 11:24 实测 **OVERALL: fail**（见 T0） |
| M2–M8 | ⬜ 待执行 | 本文件 §2 |

**当前工作区**：干净；本地领先 `origin/main` **15 个提交**（未推送，缺 GitHub PAT）。

**打包信号当前结论：不满足（fail）** —— 5 项检查 4 PASS + 1 FAIL，FAIL 源自 `release_gate.py` 自身对"环境专有失败"的解析健壮性，**非产品回归**。先修 T0，再谈放行。

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

---

### M7 —— docling 第三对照引擎

| ID | 任务 | 文件 | 验收 |
|---|---|---|---|
| T7.1 | OCR 后端**统一接口**抽象（`run_ocr` 协议 + 能力声明） | `core/pipeline/ocr_support.py` | 双后端回归通过 |
| T7.2 | docling 接入（**可选依赖**，缺失时优雅降级不影响主链） | 新 `core/docling_client.py` | 缺失即降级 |
| T7.3 | 三引擎对比脚本 + 差异页报告 | `scripts/` | 有对比报告 |
| T7.4 | 许可标注修正（docling = **MIT**，非 AGPL） | ✅ 已在 `ROADMAP_v1.1.md` 勘误（§决策锁定行4 / §7.2 / §7.3） |

**依赖**：M3（OCR 接口抽象）。**产出**：docling 缺失不影响主链。

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
