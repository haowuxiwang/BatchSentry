# BatchSentry v1.1 质量与闭环路线图

> 定位 → 解决 → 测试 → 打包。本文件是 v1.1 的唯一规划来源。
> 基线：Round 23 收尾（4 个原子提交 `ea5249a`/`ba4a4f9`/`2c5d6e9`/`da15539`，本地未推送）。
> 编写日期：2026-09-10。状态：**已评审通过，执行中**（2026-09-11 锁定决策）。

## 决策锁定（2026-09-11，用户拍板）

| # | 决策点 | 决议 |
|---|---|---|
| 1 | 覆盖率节奏 | **先 92%，再 95%**（分两步，各自为一个提交 + 一次门禁提升） |
| 2 | 规则扩展范围 | **R11–R17 全做**（含规则注册表） |
| 3 | 语义检索 | **维持纯 BM25**（本轮不引入 embedding，降级路径天然满足） |
| 4 | 第三方后端 | **引入 docling 作为第三对照引擎**（**MIT**，本地对照用途；文档标注许可） |
| 5 | 打包放行范围 | **v1.1 必须跑真实 51 页丝裂霉素全链路**（含 ui_e2e + 冻结版 smoke） |

工程纪律：模块化（新增能力以独立模块/清晰接口落地，不堆在既有大文件里）；每个改动遵循 定位→解决→测试；新规则/新能力必须自带单测并维持覆盖率门禁。

---

## 0. 定位：当前真实基线（实测，非文档声称）

| 维度 | 实测值 | 证据 |
|---|---|---|
| 单测 | 1327 collected / **1326 passed + 1 环境阻塞** | `devlogs/_round23_closeout_tests.log` |
| 覆盖率 | **90.26%**（7718 语句 / 752 未覆盖） | 同上，门禁 90% 刚好过 |
| 达 95% 缺口 | **需再覆盖 367 行**（阈值 5% = 386 行） | `coverage.xml` 逐文件解析 |
| e2e | rot / pdf+real / ui_e2e **ALL PASSED** | `devlogs/e2e_round23_closeout_*.log` |
| 规则数 | **14 条**（R1a/R1b/R2/R3/R4/R5/R6/R7/R8/R8b/R9/R9a/R10/R-M1/R-M2） | `core/rules/__init__.py` |
| finding 类型 | 规则层 **9 种** + LLM 可自造变体（如 `batch_logic`） | `core/rules/*.py` |
| 知识库 | 单源 GMP2010：**286 条 / 29,458 字 / 14 章** | `core/kb/data/gmp2010.json` |
| 检索 | 字符 bigram 倒排 + BM25(k1=1.5,b=0.75)，TopK≤4/5，摘录 120/200 字 | `core/kb/retriever.py` |

**结论**：项目工程成熟度很高（状态机、审计、failover、幂等、降噪都做了），但**四类根因性缺口**尚未闭环——它们是本路线图的主体：

1. **质量不可度量**：没有 ground truth / 准召指标，"降噪"靠个案经验，无法证明改进。
2. **知识库单薄**：只有 GMP 正文，无附录、无 ICH/ALCOA+/Part 11，无条款级引用，无语义检索。
3. **规则覆盖有系统空洞**：见 §4，缺"物料平衡 / 设备清洁状态 / 环境监测 / 文件版本一致性 / 操作人=复核人 / 偏差关联"等 GMP 高频项。
4. **门禁未升到 95%，打包信号未脚本化**：门禁靠人跑命令，无单一可执行 go/no-go。

---

## 1. P0 — 门禁与闭环（最高优先级，先立规矩）

### 1.1 覆盖率门禁 90% → 95%
- `pytest.ini` / `setup.cfg` 的 `--cov-fail-under=90` → **95**。
- 缺口 367 行，按"性价比"排序补齐（低覆盖文件优先，收益最大）：

| 文件 | 当前覆盖 | 未覆盖行 | 目标 |
|---|---|---|---|
| `api/settings/probe.py` | 69.1% | 47 | 95% |
| `config.py` | 76.2% | 77 | 92% |
| `api/jobs/page_image.py` | 79.0% | 26 | 95% |
| `api/jobs/status.py` | 79.7% | 27 | 95% |
| `core/pipeline/state.py` | 81.8% | 29 | 95% |
| `api/settings/{rules,provider,write}.py` | 82–83% | 93 | 95% |
| `core/rules/__init__.py` | 84.8% | 17 | 95% |
| `core/ocr_client.py` | 85.5% | 35 | 95% |
| `core/page_analyzer.py` | 86.1% | 56 | 93% |
| `api/report.py` | 86.9% | 29 | 95% |
| `core/mineru_client.py` | 87.3% | 74 | 93% |
| `api/jobs/upload.py` | 87.9% | 24 | 95% |

- **纪律**：新增测试只测真实分支（异常/回退/边界），禁止为凑数写空断言；`# pragma: no cover` 仅限"仅子进程执行"等既有豁免。

### 1.2 打包信号可执行化（单一 go/no-go 脚本）
现状：`docs/OCR_GOLDEN_CORPUS.md` 定义了 4 条发布门禁，但判定分散在人工记忆里。
- 新增 `scripts/gate.py`（或 `gate.ps1`），一条命令串起并**打印可归档的信号报告**：
  `ruff F 类零新增` → `pytest --cov-fail-under=95` → `e2e_run --rounds pdf,img,rot` → `e2e_run --rounds real`（受控样本）→ `ui_e2e` → `build.ps1` 三段 → frozen smoke（含镜像上传）。
- 输出 `devlogs/gate_report_<date>.json`（含 commit hash、覆盖率、各轮判定、产物大小与 mtime），作为打包放行凭证。
- **验收**：门禁脚本从干净工作区一次跑通，报告落盘，任一环节失败即非零退出。

---

## 2. P1 — Finding 质量：从"降噪靠手感"到"准召可度量"

> 这是提升 finding 质量的**唯一正确起点**：先能度量，再谈优化。

### 2.1 建金标集（ground truth）
- 用现有受控样本（丝裂霉素 51 页 + 合成件）人工标注一份 **finding 金标**：每页应有的 finding（type/位置/严重度）+ 明确"不应报"的项。
- 格式沿用 `docs/OCR_GOLDEN_CORPUS.md` 的标注范式，新增 `docs/FINDING_GROUND_TRUTH.json`（不含业务原文，仅指纹与类型）。
- 新增离线评测 `scripts/eval_findings.py`：对任一 job 的 findings 与金标比对，输出 **precision / recall / F1 / 按 type 分解 / 误报 Top 清单**。
- **验收**：能对 Round 23 的 e2e 产物出基线数字（记录为 v1.0 质量基线）。

### 2.2 五条具体降噪/提准措施（按证据强度排序）
1. **confidence 持久化**：目前 `api/review.py::_confidence_for` 只在读时算，**未落库**。新增 `findings.confidence REAL`（schema v10），在 stage2/stage3 写入时算好；好处：SQL 可排序/可阈值/可进报告、可做"低置信度不展示"开关。
2. **语义去重替代键去重**：现有 N1 按 `(page,type)` 抑制——**过粗**（会误杀同页同类型的不同问题）。改为"规则 finding 为权威 + LLM finding 与其做 embedding/LLM 判重"，保留真正新增项。
3. **类型白名单 + 未知类型归并**：LLM 自造 type（`batch_logic` 等）→ gmp_basis 只能靠关键词兜底。改为 prompt 内 enum 强约束 + 后端把未知 type 归并到最近规范类型（保留原文 type 于 `ocr_text`）。
4. **finding 级证据锚定**：`ocr_text` 是自由文本，无位置。为每条 finding 增加 `evidence_ref`（页内字符偏移/片段指纹），复核页可高亮跳转，也让"事实核查"可自动化（把 page 级 `_grounding_warn` 下沉到 finding 级）。
5. **规则层覆盖抑制改为可解释**：被抑制的 LLM finding 现在只记计数。改为写审计明细（哪条被谁覆盖），便于回看误杀。

### 2.3 LLM 提示与校准
- 现有 `<HUMAN_REVIEW_FEEDBACK>` few-shot 已回流人工裁决（好设计）。补充：**按 type 分桶**注入样例（避免签名类样例挤掉参数类），并把"误报"样例显式标注为"禁止同类输出"。
- `LLM_JSON_MODE` 默认**开**（当前默认关且实测触发过 330s 的 fix-hint 重试，见 Round 23 观察项）。
- 引入 **Instructor 式"验证+Reask"**：本地已有 fix-hint 重试，补齐 Pydantic 语义校验（当前只有结构校验），把 `_schema_warn` 比例降下来。

---

## 3. P2 — OCR：不同尺寸/形态 PDF 与图片的完整解析

现状能力（已做）：Stage 0 规范化长边 >1600pt → 300dpi 重渲染（cap 4096px，灰度 JPEG q85）；双后端 failover；空页切片自愈；横置页 90/270/180° 旋转自愈（几何预筛 + 升级门槛）；低 DPI(<150) 标记不静默；图片 → 300dpi PDF。

**仍存在的解析空洞（逐条给验证方法）**：

| # | 空洞 | 现象 | 处置方向 |
|---|---|---|---|
| O1 | 极小页面盒（长边 <~300pt，如微型标签/连续表单） | Stage 0 只处理"过大"，小人脸不处理；可能整页稀疏 | 增加 `_PDF_SMALL_BOX_PT` 下界，放大到目标 DPI |
| O2 | 超长连续页（aspect_ratio 极端，如 200×3000pt） | 正文被切碎/表头丢失 | 在规范化的同时按内容带切分或提高渲染上限 |
| O3 | 混合页面尺寸（A4+A3+自定义同文件） | 未统一，后端表现漂移 | 规范化按"目标像素密度"统一，而非仅按 >1600pt |
| O4 | 超多页（>200 页） | Stage3 summary 触顶走显式截断；OCR 单次提交上限 3600s | 分片策略（`OCR_SLICES`）默认阈值下调 + 分片摘要合并 |
| O5 | 图片 DPI 未标注 / 相机拍摄透视畸变 | Paddle 有 unwarp/orientation 选项，**MinerU 无** | 上传侧统一做透视校正/方向校正归一化 |
| O6 | 矢量文字但字号极小（<6pt，如页脚表格） | Surya/PaddleOCR 会"编造文本"（业界已知失败模式） | 低 DPI + 小字号联合标记，强制人工复核 |
| O7 | 彩色/低对比扫描件 | 未做对比度增强 | 规范化时加自适应二值化/对比拉伸选项 |

- **验收**：扩充 `docs/OCR_GOLDEN_CORPUS.md` 样本表（新增 O1/O2/O3/O6 三类合成样本 + `gen_*.py` 生成器），每类有一个可重复的最小回归用例；e2e 断言"该页不得静默标记成功"。

---

## 4. P3 — 规则体系：合理性评估与扩展

### 4.1 现有规则合理性（结论：设计稳健，个别阈值偏保守）
- 优点：防误报设计到位（R10 缺口数阈值、R7 批号变体归并、R-M1/M2 小样本豁免、R9a 同角色豁免、R3 手写边缘降级）。
- 偏保守处：R6 的 `reviewer`/`qa` 缺失统一给 `info`（可考虑按工序关键性分级）；R3 手写 ≤10% 才降级，可结合实际 OCR 误差分布标定。

### 4.2 建议新增规则（GMP 高频、当前空白）
| 规则 | GMP 依据 | 实现要点 |
|---|---|---|
| **R11 物料平衡/收率**（`mass_balance`） | GMP 生产管理：物料平衡检查 | 抽取投入/产出/损耗与理论值，偏差超阈值 → critical |
| **R12 操作人≠复核人**（`self_review`） | ALCOA+ Attributable；GMP 复核独立 | 同 step 的 operator 与 reviewer 姓名归一后相同 → critical（**当前完全未检查**） |
| **R13 设备/清洁状态完整性**（`equipment_state`） | GMP 设备与清洁 | 缺设备编号/清洁状态/清洁有效期 → warning |
| **R14 环境监测完备性**（`env_monitor`） | GMP 厂房设施 | 温湿度/压差/洁净度必填项缺失或超限无说明 |
| **R15 文件版本一致性**（`doc_version`） | GMP 文件管理 | 跨页文件编号/版本号不一致 → critical |
| **R16 超限偏差关联**（`deviation_link`） | 偏差管理 | 存在超差 finding 但同页无偏差编号/说明 → warning |
| **R17 涂改规范**（`alteration`） | ALCOA+ Original | 检测涂改/杠改标记但缺签名与日期 |

- 每条规则必须：① 有 `GMP_BASIS_MAP` 与 `TYPE_QUERIES` 条目；② 有 ≥4 个单测（正例/反例/边界/畸形输入）；③ 前端 `type_zh` 双端同步（`review.html` + `review.js`）。

### 4.3 规则工程化
- 抽一层 **规则注册表**（id / type / severity / 依据 / 开关），替代散落硬编码，使"规则是否合理"可被配置与审计（也为 KB 条款级引用铺路）。

---

## 5. P4 — 知识库升级（当前最大短板）

现状：单源 GMP2010 正文 286 条，bigram BM25，TopK≤5 注入 user 侧，`kb_used` 审计留痕。

### 5.1 语料扩充（多源、可版本化）
- 加 **GMP2010 附录**（批记录相关条款主要在这里）、ICH Q7/Q9/Q10、ALCOA+ 数据可靠性指南、EU GMP Annex 11、FDA 21 CFR Part 11、江苏省记录填写规范。
- 扩展 `scripts/seed_kb.py` 为多源种子管线，每个源独立 `source_id` + 内容 hash 版本；`kb_entries` 加 `source_id` 维度查询。

### 5.2 检索升级
- 保留 BM25 作基线，**新增 embedding 语义检索**（混合检索 + 重排）；对"同义/改写"命中率提升明显（当前 bigram 对同义表述基本失配）。
- 补 `TYPE_QUERIES` 全类型覆盖（当前缺 `step_gap`/`batch_logic`/`dual_compare` 等）。

### 5.3 引用精细化
- 现状刻意"不硬编码条款号"（合理，避免错引）。升级路径：**由 KB 检索直接返回 `article_label`（第一条/第一百六十X条）**，而非人工硬编码——引用来自语料本身，可校验、可追溯。

### 5.4 落地形态
- 设置页知识库分区加"多源开关/版本/命中预览"；报告附录按源分组。
- **验收**：金标查询集（≥30 条 type→应命中的条款）检索命中率 ≥85%；e2e 断言 findings 带 `kb_refs` 比例维持 100%。

---

## 6. P5 — 流式输出（SSE）合理性

**结论：设计合理（幂等全量快照 + 断线自愈 + retry 提示），但有 4 处可优化**：

| # | 问题 | 位置 | 处置 |
|---|---|---|---|
| S1 | **文档/代码不一致**：docstring 写"每 2 秒"，实际 `asyncio.sleep(3)`；`retry: 2000` 与推送间隔不同源 | `api/jobs/status.py:230,243,281` | 统一为一个常量（如 2s）或修正文案 |
| S2 | 每 tick 全量重算（5 条 COUNT + page_finding_counts），无终态缓存 | `_get_job_progress` | 复用 `listings.py` 的终态快照缓存范式 |
| S3 | 轮询式 SSE：DB 轮询 → 推送，客户端数与 job 数线性放大 DB 负载 | `event_generator` | 中期改为进程内 `asyncio.Queue` 事件总线（stage 变更时 publish），降 DB 压力与延迟 |
| S4 | 3s 粒度下 Stage3 单次大调用（cross 330s+）期间无心跳，UI 看着像卡住 | 前端 | 已用 ETA 缓解；补"阶段内已耗时"秒级本地计时 |

- **验收**：S1 文案一致 + 单测锁定；S2 稳态 QPS 下降可测；S3 作为 v1.2 事项。

---

## 7. 外部对标：可借鉴的产品与开源项目

### 7.1 商业产品（借鉴其"产品能力面"）
| 产品 | 可借鉴点 |
|---|---|
| **Acodis**（瑞士，批记录） | 复核耗时降 60-80%；"异常清单 + 文档内高亮批注 + 一键生成偏差草稿"；结构化 XML 数字孪生供 PQR 分析 |
| **Mareana**（AI 批记录复核） | "系统验证通过=绿 / 失败=红 / AI 辅助规则=蓝"三色复核面板；human-in-the-loop 分级 |
| **耀点 GxPAI**（国内） | 支持 BPR/BIR/SOP 三类文件；**在原文件中高亮并注明违反的具体法规条款**；"审核-整改-复核"闭环跟踪 |
| **宝软 EIOS**（国内） | 法规条款 → 结构化检查项（1847 条）；**跨批次趋势筛查**（参数未超限但偏离历史正常区间也标记） |
| **QAtrial / gxp-ai-validation**（开源） | 验证证据自动化、审计轨迹、Annex 22/GAMP 5 对齐 |

**对 BatchSentry 的直接启示**：① 法规条款级引用（§5.3）；② 参数**趋势筛查**（同品种/设备历史区间，超越"是否超限"）；③ 偏差联动闭环（§4.2 R16）；④ 三色复核面板（rule=红/LLM 辅助=蓝/system pass=绿）。

### 7.2 GitHub 可引入项目（按用途）
| 用途 | 项目 | 引入方式 |
|---|---|---|
| PDF/表格解析（备选后端/对照） | **MinerU**（opendatalab）、**docling**（IBM→Linux Foundation, **MIT**）、**marker/Surya**、**olmOCR**（AI2）、**HunyuanOCR**（腾讯 1B） | MinerU 已在用；docling 可作第三对照后端（门禁 3 双引擎 → 三引擎） |
| 结构化输出可靠性 | **Instructor**、**Outlines**、**Guardrails** | Instructor 式验证+Reask 补进 `llm/client.py` |
| 表格结构识别 | **GMFT**、**TableTransformer**、**RapidTable**（MinerU 内） | 对矩阵页做独立结构校验，交叉验证 OCR |
| 规则引擎/合规 | **regulated-multiagent-reference**（agents propose, rules decide）、**gxpeval** | "确定性规则裁决 + 证据轨迹"，与本项目"规则为权威版本"一致，可借鉴评测框架 |
| 评测 | **gxpeval**（GxP 专用 LLM 评测，ALCOA+ 审计） | 对应 §2.1 金标评测 |

**License 提醒（2026-09-11 勘误）**：**docling 为 MIT 许可**（已由 IBM 捐给 Linux Foundation AI & Data，当前 v2.126.0）——商业分发无 AGPL 义务，M7 引入风险低于原评估；MinerU 为自定义协议/AGPL 系、Marker 为 GPL+OpenRAIL-M（二者分发需评估）。引入前逐项确认。

### 7.3 2026-09-11 复检新增（详见 `docs/PLAN_v1.1_EXECUTION.md` §1-Q6）
| 项目 | 价值 | 归属 |
|---|---|---|
| `1999XIAOZHANG/gmp-csv-validator-skill` | 现成 6 部法规原文语料（GMP2010/计算机化系统附录/21 CFR Part 11/Annex 11/WHO TRS996/ICH Q9），每条整改定位到条款 | M5 语料 |
| `memopena/regulated-multiagent-reference` | "agents propose, rules decide" + 答案键 + precision/recall 门禁 | M2 范式 |
| `MeyerThorsten/QAtrial`（AGPL-3.0） | 开源 QMS：风险/CAPA/电子签名/审计 | M6 参考 |
| `Sukarth/ai-audit-aid` | 审计历史 + 跨版本对比 + SHA-256 内容去重 | M2/报告参考 |

---

## 8. 执行顺序与里程碑

> 每阶段严格遵守：**先定位（拿真值）→ 再解决（最小改动）→ 再测试（含覆盖率）→ 最后验打包信号**。

- **M1a（门禁 92%）** ✅：覆盖率 90.26% → **92.29%**，`--cov-fail-under=92` 落地（093ebf8）。
- **M1b（门禁 95%）** ✅：覆盖率 92% → **95.12%**（377/7718 未覆盖），`--cov-fail-under=95` 落地（ebf1d96）。
  新增 5 个模块化补测文件共 55 例：`test_ocr_client_coverage` / `test_page_analyzer_coverage` /
  `test_health_security_coverage` / `test_api_jobs_listings_coverage`（另含 M1b 首批 4 文件）。
  过程中定位并修复真实缺陷：`api/jobs/listings.py:105` 对 `sqlite3.Row` 调 `.get()` → 终态快照缓存
  崩溃、`/api/jobs/live` 静默降级（610f8c5）。
- **M1c（打包信号）** ✅ `scripts/release_gate.py`（66203b4 + 修正 ea4ae97）：离线门禁五项
  ——`worktree_clean` / `packaging_files` / `rules_wired`(15) / `kb_corpus`(300) / `tests_coverage`；
  实测 **OVERALL: pass**（4 PASS + 1 WARN，WARN = 沙箱专有的 `TestServePdf` 失败，覆盖率 95.12%），
  报告落盘 `devlogs/gate_report_<YYYYMMDD_HHMMSS>.json`。
  > 沙箱陷阱：`pytest --cov` 收尾时 `pytest_cov.finish()` → `cov.combine()` 会**删除**自身
  > 的并行数据文件（`<COVERAGE_FILE>.*.pid*`），在 WorkBuddy 沙箱下会触发 safe-delete
  > 批量守卫 → `SystemExit(1)` → `INTERNALERROR`，覆盖表与 `--cov-fail-under` 均不产出。
  > **release_gate.py 取覆盖率必须走 `coverage run --source=... -m pytest … && coverage report
  > --fail-under=95`**（无 combine、无删除），不要依赖 pytest-cov 收尾。COVERAGE_FILE 应落在
  > 真实系统临时目录（`%LOCALAPPDATA%/Temp`），不要用 `D:\tmp`。
- **M2（质量可度量）**：金标集 + `eval_findings.py` + confidence 落库 + 语义去重 + 类型白名单。*产出：质量基线数字 + 误报清单。*
- **M3（OCR 鲁棒性）**：O1/O2/O3/O6 规范化与样本。*产出：新增样本全过 + 无静默成功。*
- **M4（规则扩展）**：R11–R17 全做 + 规则注册表。*产出：每规则 ≥4 单测。*
- **M5（知识库升级）**：多源语料 + 纯 BM25 检索升级 + 条款级引用。*产出：金标查询命中率 ≥85%。*
- **M6（SSE 优化 + 对标落地）**：S1/S2/S4 + 三色面板 + 趋势筛查评估。
- **M7（docling 第三对照）**：OCR 后端统一接口 + docling 接入 + 三引擎对比 + 优雅降级。
- **M8（打包放行）**：`release_gate.py` 全绿 → 重打包 → **真实 51 页全链路** frozen e2e + ui_e2e → 打 tag `v1.1.0`。

**依赖**：M1b 依赖 M1a；M2 依赖 M1 的门禁；M5 依赖 M2 的评测口径；M7 依赖 M3 的 OCR 接口抽象；M8 依赖 M1–M7 全绿。

---

## 9. 风险与明确不做

**风险**
- 95% 覆盖率对 `config.py`/`settings/*`/`mineru_client.py` 补齐需大量边界测试，可能暴露真实缺陷（要按"先定位"处理，不为过门禁而改产品行为）。
- 语义去重引入 embedding 依赖 → 需提供"无网络降级到键去重"的路径（保持离线可用）。
- 新增规则会推高 finding 数量 → 必须同批交付 §2 的评测与阈值调优，否则净增噪音。

**明确不做（本轮）**
- 不做 Web 版 / 移动端适配（沿用 2026-08-18 决策）。
- 不引入真实 MES/LIMS 对接（无数据源，且涉及客户环境）。
- 不更换主 OCR 后端（MinerU/Paddle 双后端已足够，第三方仅作对照）。
- 不做模型微调（成本高，先用 prompt + 规则 + KB 提升）。

---

## 10. 决策记录（2026-09-11 已拍板）

1. **覆盖率节奏**：先 92% 再 95%（M1a / M1b）。
2. **规则扩展范围**：R11–R17 全做（M4）。
3. **语义检索**：维持纯 BM25，本轮不引入 embedding；§2.2 的"语义去重"改用 LLM 判重（离线可用），不使用向量库。
4. **第三方后端**：引入 docling 作第三对照引擎（M7）；**MIT 许可**（2026-09-11 勘误，已由 IBM 捐给 Linux Foundation AI & Data），仅作本地对照/验证用途，不改变产品分发形态，许可风险已在 §7.2 标注。
5. **打包放行范围**：v1.1 必须跑真实 51 页全链路（M8）。
