# 降噪与兜底：市面成熟实践调研 + 决策建议

> M8 后续 / 降噪专项前置调研。回答两个问题：
> ①**怎么降噪才不牺牲检出**（用主流产品与受监管行业的成熟做法定方向）；
> ②**噪音已经产生了怎么兜底**（发现 → 降级 → 仲裁 → 留痕 → 可回退 → 闭环）。
>
> 证据来源 8 组主题 / 40+ 来源，分层标注：**法规原文 & 官方文档** > 厂商产品文档 > 论文 > 工程博客。
> 本文件只做决策，不改代码。

---

## 0. 结论摘要

| 判断 | 内容 |
|---|---|
| **行业已收敛到同一范式** | 不用"更聪明的单点判断"降噪，而用**独立多读的一致性 + 结构可验证性 + 弃权路由 + 可回退留痕**。四条腿缺一不可 |
| **我们的架构方向是对的** | fail-closed、双后端对比、finding 金标评测、离线重放——这四件正好是上述范式的骨架，**不需要推翻重做** |
| **真正的差距只有四点** | ① 双读分歧没进判定链（只在离线脚本里）② finding 没有字段级证据锚 ③ "抑制"是**删除**而非留痕状态 ④ 置信度是**确定性代理**而非校准量 |
| **最划算的一招** | P0-1：**选择性双读仲裁**——只在"要下超差结论"时才触发第二读，用引擎分歧兜住"数字本身错"。全量双读太贵，选择性双读直击痛点 |
| **最重要的合规约束** | EU GMP 拟新增 **Annex 22**：关键 GxP 决策**只允许静态确定性模型**，生成式/自适应 AI 不得用于关键决策 → LLM 层必须定位为"辅助与提示"，判定权威留在确定性规则层 + 人工 |
| **明确的禁区** | 不做宽启发式软化。M8 那次 2 条回归（`25 vs ≤5.0℃`）已证明：软化过宽会**吃掉真实超差**；合规行业把这种行为定性为"为让队列好看而调掉真风险"＝控制失效 |

---

## 1. 根因：我们遇到的 6 类，行业归为 3 类失效模式

| 实例 | 现象 | 失效模式 | 现有对策 | 是否够 |
|---|---|---|---|---|
| p9 使用次数 `17→417` | OCR 把批次号尾部与使用次数粘进同一 `<td>` | **M1 结构失效** | 无 | ✗ 规则层与 LLM 层同时中招 |
| mineru p8 整列错位 | 取到左邻列 | **M1 结构失效** | 无 | ✗ |
| p08 `温度 15±5` → `15-5` | `±` 被读成空格/减号 | **M2 字符失效** | `_parse_spec` 三写法 + `_decimal_loss_factor` | ✓ |
| p08 进料压力 `4.6→46` | 丢小数点 | **M2 字符失效** | 同上（量级判据 ≈10×） | ✓ |
| 真空度负号 | 负实测值 + 正限值 | **M3 语义/约定失效** | `_sign_convention_uncertain` → fail-closed | ✓ |
| p09 `<0.3 MPa` 方向 | LLM 判反 | **M3 语义/约定失效** | `spec_guard` 复核 / 抑制 | ⚠ 部分 |

**关键洞察**：M1 与 M2 是**同一个根**——抽取链路里"字符 ↔ 单元格 ↔ 列"的对应关系没有独立校验。任何一层的猜测（OCR 的、LLM 的、我们解析器的）都无法**自证**。所以降噪的正确着力点不是"让某一层更准"，而是**给这个对应关系引入独立的第二证据源**。

---

## 2. 行业证据

### 2.1 结构失效：官方承认的已知限制，只能靠后处理 + 列先验

- **Azure Document Intelligence**：微软在官方 Q&A 中**明确承认**相邻单元格合并是**已知限制**（2024-11-30 GA 未解决，无公开路线图）；给出的建议是"**后处理校验预期列数**，发现合并后**按内容规则切分**（日期模式、状态关键字）"。同一条答复还给出更重要的方向：v4.0 起提供 table/row/**cell 级置信度** → **"只把低置信单元格送去人工，而不是做宽泛的后处理"**。
  - https://learn.microsoft.com/en-us/answers/questions/2258179/winrar
- **AWS Textract**：`TABLE` → `CELL` → `WORD` 三层 block + `Merged Cells` 关系 + `RowSpan`/`ColumnSpan`，每个 block 带 confidence。工程侧的共识是**置信度必须作为一等数据流过管线**——"routing decisions, auto accept, validate, or send to a human, **all hang off them**"。前置条件 ≥300 DPI。
  - https://docs.aws.amazon.com/en_us/textract/latest/dg/how-it-works-tables.html
- **表格结构模型的定位能力实测**（同一基准横向对比）：`SLANet` AP50 **3.67** / AP75 **0.40**，`SLANetV2` 3.35 / 0.32，`TableMaster` 0.01 / 0.00；而分割式方案（TableStructureFormer）AP75 **86.32**。
  - 结论：**序列表格模型"字读对了、格放错了"是原理性缺陷**，不是参数调优问题。我们观察到的"列错位"属于这一类。
  - https://link.springer.com/article/10.1007/s40747-025-01975-w/tables/7
- **上游同类缺陷有官方记录**：PaddleX PR #4756——多表格场景下 NMS 后排序变化导致**单元格被分组到别的表格**（"单元格混合"），修复方式是改用 `batch_inds` 分组而非顺序切片。说明"串列"是上游常态，**不能假设调用方拿到的结构一定自洽**。
  - https://github.com/PaddlePaddle/PaddleX/pull/4756
- **无训练规则法的自定位**：SPARTAN（OpenCV 启发式，20k+ 页实测）在有框线时走线检测成格、无框线时走"词对齐推断伪边界"，并**主动接受漏检**——"prefers to miss doubtful tables rather than **flood the output with false hits**"。与我们的 fail-closed 同源。
  - https://www.clearskyscience.com/en/10.1038/s41598-026-44325-7
- **表 OCR 工程铁律**：**"Anchor to the grid, not to text presence"**——空白单元格必须显式产出空值，否则"读取时跳过空格 → 后续每个值左移一位"。这正是我们 p9 的机理。
  - https://digitalrelics.uk/posts/ocr-htr-pipelines/ocr-tables-registers

### 2.2 单一读者会"安静地错"：多读一致性是唯一已知的免费信号

- **ExtractConf**（arXiv 2606.24420，DocILE 55 字段，前沿 LLM 失败率 26%）：
  - 现成的置信代理**全部不能用作路由信号**：logprob 均值 AUC **0.705**、语言化自评 **0.692**、5 次自一致性 **0.744**（且成本 ×5）；在实用阈值上"退化成全通过分类器"。
  - 多信号融合（模型内部 + **文档质量** + 提取位置 + OCR 一致性 + **双异构调用分歧**）→ **AUC 0.928，选择性预测风险降 70%**。
  - 原文直接点破了我们的病：**"extraction errors are frequently caused by failures the model cannot observe… A frontier LLM confidently transcribing OCR noise produces high log-probabilities for a wrong answer."**
  - 附带发现：**"document quality alone outperforms LLM-internal uncertainty alone"**——我们的 `PAGE_FLAG_KEYS`（low_dpi/sparse/truncated）比 LLM 自评更值钱。
  - https://ar5iv.labs.arxiv.org/html/2606.24420
- **OCR by Consensus**（生产系统，非原型）：5 个引擎（含 Azure DI + 2 个前沿视觉模型 + 自托管 PaddleOCR/Tesseract）**独立读同一页 → 逐字段加权投票 → 每个字段带一致度分级（unanimous … split）→ 低于阈值进人工队列，并**并列展示各引擎原始输出**。核心原则：**"Consensus turns disagreement into a signal instead of a silent error."**
  - 这与我方 p19 的情形完全对应：paddle 读 `49.0`、mineru 读 `99.0`——**分歧本身就是最该被人工看见的地方**。
  - https://roderickc.com/products/ocr
- 更轻的双读范式：Tesseract + Mistral 双读 → 交给 LLM 择优（"correcting errors from one engine using the other"）。我们已具备同等原料（paddle/mineru 双后端）。

### 2.3 置信度不能直接信

- 校准方法：isotonic / Platt / temperature scaling；**ECE** 度量；生产分级阈值（auto-accept ≥0.85 / review 0.70–0.85 / reject <0.70），且**按文档类型分别标定曲线**（新类型先用通用曲线，累积 ~500 篇标注后切专属）。
  - https://www.runpulse.com/blog/confidence-scoring-for-downstream-automation-when-to-trust-when-to-review
- **未校准置信度的致命陷阱**（同一来源与专业实践一致）："if your parser has **systematic failure modes** (e.g., always misreads handwritten dates), it will return **high confidence on those wrong answers**"——正是我们 p9/p19 的形态。
- **ConfBench**（arXiv 2608.01792）：20 条受控退化管线 × FCC 票据 → 1346 变体、**70K 实体级评测**，首次把"标定"做成基准。
  - 跨模型校准质量差异巨大（Claude Opus ECE **0.05** vs Haiku **0.17**）；**OCR 文本 + 图像双输入**在准确率与校准质量上同时最优；新增 **ECARB** 指标：置信引导复核相对随机抽样多抓多少错（最佳模型在 30% 复核预算下 **2.43×**）。
- **我们的硬约束**：`core/hw_signal.py` 已登记——MinerU 的 `content_list_v2` 无行级分数、**PaddleOCR-VL 按设计不提供置信度**。所以**不能走"读 OCR 自带分数"这条路**，只能走"多读一致性 + 结构证据 + 文档质量"。
  - 反过来说，`hw_signal.py` 现有做法（把 MinerU 的手写标记变成确定性 `value_source='handwritten'`，并在规则层**覆盖**模型的 `printed` 自报——"machine fact beats model guess"）**已经就是正确范式**，值得推广到更多信号。

### 2.4 选择性预测 / 弃权：标准范式，且能做到有统计保证

- 静态阈值（0.95 accept / 0.70 review）只是起点，生产化升级路径：分类型校准曲线 → 成本矩阵阈值优化 → 阈值边界带主动学习 → conformal 区间。
- **Chow's rule**：拒识最优策略 = 比较最大后验概率与**代价比**（"accept error costs 100x more than review error"）。
- **多阶段置信聚合用几何平均**（保守，捕捉最弱环节）：OCR 0.92 / 抽取 0.88 / LLM 校验 0.80 → **0.87** < 0.90 → 判"需复核"。
- **Conformal + FDR 控制**（医学实体抽取）：span 置信 = token 概率几何平均 → logit 非一致性分数 → 用标注集标定阈值 τ，使**被接受的抽取中错误比例期望 ≤ α**。这是唯一能对监管/质量部门说出"**我们自动放过的条目，错误率上限是 α**"的机制。
  - https://www.themoonlight.io/es/review/conformal-prediction-for-risk-controlled-medical-entity-extraction-across-clinical-domains

### 2.5 受监管行业的答案：Review by Exception（有明确法规落点）

- **ISPE 定义**：RBE = "manufacturing and quality data are screened to **present or report only critical process exceptions** as required by approvers"。150 页批记录 → 3 页异常报告。
- **法源（这是关键，RBE 不是我们的取巧）**：
  - **PIC/S PI 041-1**：常规计算机化系统数据"可人工复核，**或由经验证的异常报告复核**"，并与基于风险的日志/审计追踪抽样挂钩。
  - **EMA**：RBE 在**科学论证**前提下允许，且**明确要求对自动化系统与其输出的异常报告做测试与验证**。
  - **21 CFR 211.192**：放行前 QC 须复核全部生产记录 → RBE 不是"少看"，而是把逐条核对托给**经校验**的系统，人工转向异常。
  - https://www.aiforqa.org/articles/ai-data-integrity-alcoa-plus.html
- **制药批记录 AI-OCR 商用产品的做法**（Mareana）：
  - 置信分高 **且** 在范围内 → 自动接受（绿，复核人可跳过）；
  - 超范围 → 交 QA 立即处置；**"系统对自己读数没把握"也触发标记** → "every uncertain extraction receives human verification"；
  - **每个数字化数据点保持与原始纸质记录高清片段的可追溯链接**（source-to-data traceability）——"inspectors 可即时把数字与原始物理来源核对"。
  - https://mareana.com/video/ai-exception-based-review
- **ISPE 自动放行的边界（原文）**："**AI should never independently release a batch**"；要求 traceable citations back to source records、**version-controlled prompts**、**controlled retrieval sources**、documented intended use、validation testing、change control、lifecycle monitoring、audit trails；**复核人应能接受 / 编辑 / 拒绝 AI 输出，且这些动作要留痕**。落地节奏 crawl → walk → run。
  - https://www.ispe.org/pharmaceutical-engineering/ispeak/autonomous-batch-disposition-transforming-pharmaceutical
- 反向价值论证（值得写进产品说明）：人工复核的固有短板恰恰是 AI 的强项——"人眼擅长发现**错的**值，不擅长发现**缺的**值（漏签名、空字段）"，且"200 页里找真偏差"本身就会漏。**降噪不等于降低检出**：把人工从"逐条核对"转向"异常判定"，反而提升真偏差的检出。
  - https://mareana.com?p=7278/

### 2.6 红线：EU GMP 拟新增 Annex 22

- 2025-07-07 由欧盟委员会与 PIC/S 检查员联合发布征求意见（至 10 月），**定稿预期 2026 年年中**；Annex 11 同批修订（5 页 → 19 页，首次把网络安全、云与数据平台确认、身份与访问管理列为核心要求）。
- **最硬的一条**：对**关键 GxP 决策**，Annex 22 **只允许静态、确定性模型**（参数固定、同输入同输出、部署后不再学习）；**自适应模型与生成式 AI 不得用于关键决策**。
- 其余是数据治理清单：intended use 与输入/输出/性能准则文档化；训练数据代表性、无系统偏差、完全可文档化；独立验证集；**与现有流程平行部署**；持续监控性能与输入漂移。
- **中国侧现状**：NMPA 尚无 GMP 层面 AI 专项指南（在酝酿）；中国正加速加入 PIC/S。现行《药品生产质量管理规范》附录《计算机化系统》已给出对应落点：
  - **第 13 条**："当计算机化系统替代某一人工系统时，**可采用两个系统（人工和计算机化）平行运行**的方式作为测试和验证内容的一部分。" ← 与 Annex 22 的 parallel deployment 同义
  - **第 15 条**："当人工输入关键数据时，应当**复核输入记录以确保其准确性**。这个复核可以由另外的操作人员完成，**或采用经验证的电子方式**。" ← 我们的自动检查在法律定位上就是"经验证的电子复核"
  - **第 16 条**："每次修改已输入的关键数据均应当经过批准，并应当**记录更改数据的理由**……考虑建立**数据审计跟踪系统**"
  - https://db2.ouryao.com/gmp/?s=fl10
- **对我们的直接启示**：产品必须把**层**讲清楚——确定性规则层 + 人工 = 判定权威；LLM 层 = 提示与辅助。反向说，**规则层是我们最该加固、最该做证据锚的地方**，因为它才是能承担"关键决策"的那一层。

### 2.7 降噪的度量纪律（AML/合规行业范式，可直接搬）

- **必须配对度量**：假阳率 **与** 真阳保留率。原文（金融犯罪合规）："**A change that halves your false positives but drops a single known true hit is not an improvement — it is a control failure with a better-looking dashboard.**"
- **核心禁则**："**never tune away real risk to make a queue look clean**"——"用抑制真命中换来的安静队列，是伪装成效率的最大合规风险"。
- **治理三件套**：**四眼审批**（任何放宽控制的变更不得单人决定）+ **回测**（先对历史数据重放，看会改判哪些 TP/FP，"before it ever touches live screening"）+ **不可变审计**（谁提、谁批、回测证据、生效时间）。"当检查官问 18 个月前某条为何没报，答案是**可举证的配置历史**，而不是耸肩。"
  - https://www.creodata.com/blog/reduce-false-positives-aml-screening
- **方法**：按类型**分层**找噪音 Pareto（20% 规则产生 80% 噪音）；**影子模式 30 天 / 金丝雀 5–10% 流量**；目标设**相对下降**（3–6 个月降 20–40%）而非一步到位；用**期望成本最小化**选阈值；配对监控次要信号防"SAR 质量下降"。
- **复核队列运营**：按严重度**分级 SLA** + 按能力路由 + **每条必须有 disposition**（确证 / 误报 / 无法判）——disposition 既是调优燃料，也是监管要求。

### 2.8 证据溯源：最便宜的信任机制

- 引用感知架构的对照数据：验证延迟 **10–20 分钟 → 亚秒**；合规团队接受度 85%；含"自动剔除无支撑断言"的防线。
  - https://digitalelliptical.com/blog/designing-citation-aware-ai-applications
- **实现教训（很有价值）**：Flow Specialty 先试"让 LLM 给 rationale + 上下文"——**显著提升信任，但没有降低核对耗时**（复杂表单/表格/图片下结果缺上下文）。真正的解法是**在管线里保留 bbox 元数据**（markdown 里包一层含 file/page/bbox 的元数据块），让模型回引 → 事后生成精确溯源。
  - https://engineering.zooz.com/flow-specialty/how-citations-adds-trust-in-ai-document-processing-bbcb7259a5b6
- 面向弱证据的 UI 模式：**证据强度指示**（强/混/弱/无支撑，**必须文字化、不能只靠颜色**）、来源过滤、"**引用墓地**"（显式标注无法溯源的断言，而不是让它混在已溯源断言里）。
  - https://www.aydesign.ai/blog/ai-citation-source-ui-patterns-2026

### 2.9 LLM 侧的回归门禁（eval harness）

- "**Evals are the new tests**"：不分层就只能知道"坏了"、不知道"哪坏了"——**检索层 / 生成层 / 端到端各自独立数据集与指标**，"分层就是 stack trace"。
- **金标集纪律**：**从真实流量取样，不是想象用户会问什么**；每次生产事故**变成一条用例**；**版本化**（静默变更会作废全部历史分数）；50–200 条起步，生产级 500–2000；覆盖 happy path / 长尾 / 对抗 / 敏感。
- 每次改 prompt、换模型、换索引都要跑全量，并**看分项 diff**——"总体 2% 下降可能掩盖某子类 100% 的回归"。
- LLM-as-judge 必须与人工标注**校准**（kappa / 一致率），判官模型变更后**重校**；未校准的判官 = "语法很好的随机数生成器"。
- 把 eval 分数当单测：**回归超阈值即阻断合并**；CI 跑小子集（分钟级），夜间跑全量。
  - https://zorost.com/evals-are-the-new-tests · https://www.databuzzltd.com/insights/evaluation-harnesses-for-llms

---

## 3. 映射到本仓库（现状盘点）

**已有的（比预期强，都是上述范式的骨架）**

| 能力 | 位置 | 对应行业范式 |
|---|---|---|
| 确定性规则层 + 纯函数可离线重放 | `core/rules/registry.py`、`scripts/replay_rules.py` | Annex 22 允许的"静态确定性模型"；**兜底的基石** |
| fail-closed 弃权（不可判 → `spec_unverifiable`） | `parsing.py` / `rule_spec.py` | selective prediction 的 abstention；SPARTAN 的"宁漏不滥" |
| 双后端对比 | `core/pipeline/dual_compare.py`（M7） | OCR by Consensus 的原料 |
| **机器事实覆盖模型自报** | `core/hw_signal.py`（`value_source='handwritten'` 覆盖 LLM 的 `printed`） | ExtractConf 的"多信号融合"；范式已存在，只是信号源少 |
| 页级文档质量告警 | `core/finding_quality.py::PAGE_FLAG_KEYS`（low_dpi/sparse/truncated/…） | ExtractConf：**文档质量比 LLM 自评更有效** |
| finding 级金标 + TP/FN/FP/**noise** KPI + CI 阈值 | `docs/FINDING_GROUND_TRUTH.json`、`scripts/eval_findings.py`（`--min-f1`） | eval harness；**已把 noise 单列为 KPI**，口径正确 |
| OCR 金标回归集（页级，含 low-dpi 必须"不得静默标记成功"） | `docs/OCR_GOLDEN_CORPUS.md` | 从真实样本建金标 + 不进人工不算成功 |
| 三色复核分级（规则/LLM/系统通过） | `REVIEW_TIER_BY_SOURCE` + `attach_review_tier` | Mareana 的"绿=可跳过" |
| 审计三表 | `audit_log`、`llm_call_audit`、`kb_entries` | 21 CFR Part 11 / Annex 11 的审计追踪 |

**缺的（按影响排序）**

| # | 缺口 | 后果 | 行业对应要求 |
|---|---|---|---|
| G1 | **双读分歧只在离线脚本，未进判定链** | p19 那种"两后端读在阈值两侧"会静默选边 | OCR by Consensus 的 split tier |
| G2 | **"抑制"是直接 drop，只留计数** | 受监管场景下"删除"需要理由与留痕；不可复核、不可回退 | Annex 11 第 16 条 + "never tune away real risk"的治理三件套 |
| G3 | **finding 无字段级证据锚**（无 bbox / 无原文 span） | 复核人无法"一键看到原页那格"，信任成本高；无法举证 | Mareana 的 source-to-data traceability；ISPE 的 traceable citations |
| G4 | **`confidence_for` 是"我们的确定性"代理**（source×page_flag 的离散映射 0.5–0.85），非"正确概率" | 阈值不可解释、不能对外承诺错误率 | 校准/ECE；"a 95% confidence might be correct 70% of the time" |
| G5 | **金标以合成语料为主**（`synthetic_corpus.py` 构造，`real_baselines` 仅基线） | 合成样本"比真实容易"，会漏掉真实分布的坑 | "seed from real traffic, not from what the team imagines" |
| G6 | **prompt / KB 语料 / LLM 模型版本未写入 finding 血缘** | 无法回答"这条结论是哪版提示词、哪版语料产出的" | ISPE：version-controlled prompts + controlled retrieval sources |
| G7 | 降噪/软化类变更**无四眼、无回测、无审计** | M8 那次回归靠人工发现；下次可能砸到真实超差 | 四眼 + 回测 + 不可变审计 |

---

## 4. 决策建议

### P0 —— 本迭代做，改动小、可直接机检

**P0-1 选择性双读仲裁（最高性价比）**
- **做法**：不做全量双读（成本与延迟不可接受，paddle 队列满已是实证）。只在**"即将下超差/critical 结论"**时对该字段触发第二后端读取：
  - 两读一致 → 结论成立，`evidence_tier = "concurred"`
  - 两读分歧 → **不下结论**，降级为 `needs_arbitration`，在复核页**并列展示两侧原始读数**
  - 第二读不可用（token/队列） → 降级为 `unverifiable`（fail-closed，不静默采信单侧）
- **改动面**：扩展 `dual_compare` 到字段级（复用 `spec_guard` 的三元组索引 `index_specs`）；`page_cache` 需留存备侧结果（新增列或 `page_cache_alt`）；`findings` 增 `evidence_tier`。
- **机检**：新增不变式"**任何超差结论必须带 `evidence_tier`，且 `conflict` 不得为 critical 终态**"（源码扫描 + 单测）。
- ⚠ **未验证假设**：需先做一次 spike 确认两端点可否按**单元格**粒度对齐（当前 `dual_compare` 是页级）。若只能页级对齐，则退化为"页级分歧 → 该页所有超差结论一律标待仲裁"，仍有效但更粗。

**P0-2 抑制改为"留痕抑制"（合规必需）**
- **做法**：`drop_unfounded_spec_findings` 的 `dropped` 不再只返回计数，而是**落库为 `status='suppressed'`** + `suppress_reason`（如 `llm_false_positive` / `spec_in_range`）+ `suppress_evidence`（命中的三元组与三态）。复核页给"已抑制"折叠区 + **恢复按钮**（恢复动作入 `audit_log`）。
- **理由**：① Annex 11 第 16 条要求变更留理由；② 行业禁则是"抑制必须可举证、可回退"；③ 抑制本身要能**被抽检**（否则软化吃掉真阳性无人知晓）。
- **机检**：不变式"**任何被抑制的 finding 必须在库中存在且 `suppress_reason` 非空**"；新增"抑制抽检"报表（每批抽 N 条待人工确认）。

**P0-3 字段级证据锚（信任与举证）**
- **做法**：finding 落库带 `page` + `cell_ref`（列名/参数名）+ `source_text`（OCR 原文串）+（若可得）`bbox`；复核页点击 finding → 跳原页并高亮。
- ⚠ **未验证假设**：paddle-vl 当前接口是否回传单元格 bbox 未确认（`hw_signal.py` 已登记其"按设计不提供置信度"）。**先 spike**：能拿到 → 做完整高亮；拿不到 → 退化为"页 + 原文串 + 列名"锚（弱证据，但仍可点击回页，已比现状好）。
- **机检**：不变式"**每条 critical finding 必须有 `cell_ref` 与 `source_text`**"。

### P1 —— 近期做（治理与度量的补齐）

- **P1-1 真实页金标集**：在合成金标之外，从真实 51 页批记录取 50–200 条**字段级**标注（值 + 期望判定）。三类 KPI 一起报：**字段级精确率**、**TP 保留率**、**噪音率**（扩展现有 `noise_types` 口径）。并入 CI（`eval_findings.py --min-f1` 的同类门禁）。
- **P1-2 降噪变更治理**：任何软化/抑制/阈值变更走 **① 回测**（`replay_rules.py` 对历史 job 全量重放，输出"会改判哪些 TP/FP"的新旧对比表）+ **② 四眼**（配置文件变更需双人签）+ **③ 审计**（`audit_log` 记 谁提/谁批/回测证据/生效时间）。
- **P1-3 血缘**：`findings` 增 `prompt_version` / `kb_version` / `llm_model`；KB 语料与提示词入版本管理。

### P2 —— 中期（能力升级）

- **P2-1 置信度校准**：用积累的标注算 **ECE**，出**分页类型**的校准曲线；若标注量足够，优先做 **conformal FDR 控制**（能对外承诺"自动放过条目的错误率上限 ≤ α"，这是质控/监管唯一买账的形式）。
- **P2-2 路由四态化**：把现有三色升级为 **自动通过 / 需确认 / 需仲裁 / 无法判读**（`needs_arbitration` 是 P0-1 引入的新态）。
- **P2-3 数值一致性校验**（补 M1 的结构防线）：列先验（值应落在其列的水平区间）、量级先验（比对该参数的历史典型区间）、批内交叉算术（收率、总量守恒）。**只降级不下结论**。

---

## 5. 兜底设计：噪音已经产生之后

### 5.1 四道防线 + 一个闭环

```
输入质量门 ──► 独立第二读 ──► 结构/量级校验 ──► 判定与弃权 ──► 呈现与留痕
（已有 low_dpi）  （P0-1 新增）    （P2-3 新增）    （已有 fail-closed）  （P0-2/P0-3 新增）
                                                        │
                                          人工反馈 ◄─────┘
                                             │
                                    金标集 + CI 回归门禁（P1-1）
                                             │
                                    规则/词表/阈值更新（P1-2 四眼+回测+审计）
```

### 5.2 每层的兜底语义

| 层 | 触发条件 | 兜底动作 | 不许做的事 |
|---|---|---|---|
| 输入 | dpi 低 / 页稀疏 / 截断 | 标 `PAGE_FLAG`，整页 findings 降置信 + 提示重扫 | 静默标记成功（`OCR_GOLDEN_CORPUS.md` 已立此规） |
| 第二读 | 将要下超差结论 | 一致→结论成立；分歧→**待仲裁**；读不到→`unverifiable` | 单侧静默采信 |
| 结构/量级 | 值疑似串列/丢点/符号存疑 | 降级为**人工核对**（`info`/`unverifiable`），附"疑似原因" | 直接判合规**或**直接判超差 |
| 判定 | 规格不可解析 / 界不可用 / 符号约定存疑 | `spec_unverifiable` 交人工（已有） | 静默下结论 |
| 呈现 | 任何 finding | 带页 + 列名 + 原文串（+bbox）；被抑制的进折叠区并可恢复 | 丢弃抑制痕迹 |
| 闭环 | 人工判定为误报/漏报 | **变成一条金标用例** → CI 回归 → 修规则/词表 | 只修当次数据、不进回归 |

### 5.3 三条不可动摇的原则

1. **永不静默选边**。分歧是信号，不是错误——把它呈现给人（OCR by Consensus 的原话："that split is exactly the place a person should look"）。
2. **抑制 ≠ 删除**。一切降噪动作必须留痕、可回退、可抽检（合规禁则："never tune away real risk to make a queue look clean"）。
3. **降噪与检出同时度量**。任何降噪变更必须同时报"噪音降了多少"和"TP 保留了多少"；**只看前者的一律不予合入**。

### 5.4 为什么明确不做"宽启发式软化"

M8 的实测已经给出证据：`_decimal_loss_factor` 初版把 `25 vs ≤5.0℃` 也误判为"丢小数点"，触发 2 条既有测试回归（`test_large_deviation_warning`、`test_handwritten_large_stays_warning`）。修正方式是**加量级判据**（比值须 ≈10×/100×，25/5=5 不命中），而非放宽或收紧阈值。

行业侧的定性更重：宽软化等于"为让队列好看而调掉真风险"＝**控制失效**。在我们这个场景，代价是一条真实超差被静默放过——这正是 AI 辅助 GMP 最不能出的错。

**所以方向定为**：降噪靠**引入独立证据**（第二读、结构先验、列先验）**并与真实超差可区分**，绝不靠"看起来像误报"的模糊判据。

---

## 6. 明确不做（Negative list）

| 不做 | 理由 |
|---|---|
| 用 LLM 自报置信度当路由依据 | 实测 AUC 0.692（ExtractConf）；系统性失效下**高置信地错** |
| 全量双读 | 成本/延迟不可接受（paddle 队列满为实证），且收益集中在"要下结论"的字段 |
| 让生成式 AI 参与关键判定/放行 | Annex 22 红线；ISPE："AI should never independently release a batch" |
| 为降噪而放宽规格判定 | 合规禁则 + M8 回归实证 |
| 用合成语料充当唯一金标 | "合成比真实容易"，会系统性高估精度 |
| 无版本、无回测地改 prompt/语料 | Annex 11 / ISPE 要求 version-controlled prompts + controlled retrieval |

---

## 7. 验收口径（KPI）

| 指标 | 定义 | 现状 | 目标 |
|---|---|---|---|
| 噪音率 | 噪声类产出 / 总产出（沿用 `noise_types` 口径） | 已有 | 相对下降，**分母必须同时报总产出** |
| TP 保留率 | 已知真缺陷仍被报出比例（金标集） | 已有 `eval_findings` tp | **不得下降**（硬约束） |
| 字段级精确率 | 字段级 TP / (TP+FP)（P1-1 新金标） | 无 | 建立并追踪趋势 |
| 待仲裁率 | `evidence_tier=conflict` 占比 | 无（P0-1） | 低且稳定；突增=上游劣化告警 |
| 抑制抽检通过率 | 抽检的 suppressed 中被确认为真误报的比例 | 无（P0-2） | 接近 1；下降=软化过宽 |
| 自动通过错误率上限 | conformal 给出的 FDR 界 α | 无（P2-1） | 可对外声明 |

---

## 8. 参考来源

**法规与官方文档**
1. EU GMP Annex 22（AI，征求意见稿，2025-07）解读与红线条款 — https://www.dawiso.com/blog-post/data-integrity-audit-ready-lineage-gxp-ai
2. Annex 11 修订（5→19 页，2026 年中定稿） — https://idisl.info/en/el-anexo-11-de-las-gmp-entra-en-consulta-publica-cambios-clave-para-adaptarse-a-la-era-digital/
3. 中国《药品生产质量管理规范》附录《计算机化系统》（2015）全文 — https://db2.ouryao.com/gmp/?s=fl10
4. 2025 全球药品监管法规概览（含中国 AI/GMP 现状、PIC/S 加入进程） — https://www.gempexchina.com/gmp-knowledge-info/140
5. ALCOA+ / PIC-S / EMA 对"review by exception"的认可与要求 — https://www.aiforqa.org/articles/ai-data-integrity-alcoa-plus.html
6. 21 CFR Part 11 §11.10(e) 与 ALCOA+ 映射 — https://aldenwangexis.github.io/posts/gxp-audit-trails-for-ai-21-cfr-part-11-annex-11-rules.zh-CN/
7. EMA AI 反思文件（2024-09 定稿）要点 — https://www.biosliceblog.com/2024/10/ema-adopts-reflection-paper-on-the-use-of-artificial-intelligence-ai/
8. AI/ML 在 GMP 的监管视角综述（MDPI） — https://www.mdpi.com/1424-8247/18/6/901

**产品与厂商文档**
9. Azure DI：相邻单元格合并为已知限制 + 建议（cell 级置信度、只送低置信单元格） — https://learn.microsoft.com/en-us/answers/questions/2258179/winrar
10. Azure DI：列边界依赖视觉结构、语义不参与 — https://learn.microsoft.com.office.o365rp2.betagro.myshn.net/en-us/answers/questions/5936965/can-the-azure-ai-document-intelligence-prebuilt-la
11. AWS Textract：TABLE/CELL/WORD + Merged Cells + 逐 block 置信度 — https://docs.aws.amazon.com/en_us/textract/latest/dg/how-it-works-tables.html
12. Textract 置信度作为一等数据流过管线 — https://go-cloud.io/?p=14364/
13. Mareana：制药批记录 AI-OCR、置信分 + 范围内自动接受、source-to-data 溯源 — https://mareana.com/video/ai-exception-based-review
14. Mareana：人工复核的固有短板（找不到"缺的"值） — https://mareana.com?p=7278/
15. ISPE：Autonomous Batch Disposition（HITL 必要要素、version-controlled prompts） — https://www.ispe.org/pharmaceutical-engineering/ispeak/autonomous-batch-disposition-transforming-pharmaceutical
16. ISPE / RBE 定义与 150 页 → 3 页异常报告 — https://railes.com/blog/review-by-exception-mes-pharma-batch-release
17. Pfizer（ISPE 2026）AI 在 GMP 的 pilot→routine 路径与陷阱 — https://www.bioprocessonline.com/doc/implementing-ai-solutions-in-gmp-while-managing-risk-0001
18. IDP 产品横评（字段级 HITL 路由、STP 率） — https://www.floowed.com/insights/abbyy-vantage-alternatives
19. ABBYY Vantage：HITL 达 100% 准确率、持续学习 — https://appsource.microsoft.com/nl-nl/product/web-apps/abbyyusasoftwarehouseinc1599582065565.abbyy-vantage-intelligent-document-processing?tab=Overview

**多读一致性 / 仲裁**
20. OCR by Consensus：5 引擎逐字段投票 + split tier + 分歧并列展示 — https://roderickc.com/products/ocr
21. ExtractConf：多信号置信引擎（0.928 AUC，logprob 0.705 失效） — https://ar5iv.labs.arxiv.org/html/2606.24420
22. OCR ensemble：Levenshtein 离群剔除 + 视觉相似度多序列比对 — https://github.com/rafelafrance/ocr_ensemble
23. 双 OCR（Tesseract + Mistral）→ LLM 择优 — https://deepwiki.com/gashawdemlew/audited_financial_statement_extractor/5.1-pdf-processing-pipeline

**置信度 / 选择性预测**
24. ConfBench：校准基准、ECARB 指标、OCR+Image 最优 — https://arxiv.org/html/2608.01792v1
25. 置信度校准方法与生产阈值 — https://www.runpulse.com/blog/confidence-scoring-for-downstream-automation-when-to-trust-when-to-review
26. 文档解析中的不确定性量化（校准陷阱、几何平均聚合） — https://theneuralbase.com/document-ai/learn/advanced/uncertainty-quantification
27. Selective prediction / Chow's rule / 校准方法总览 — https://inferensys.com/glossary/preemptive-algorithmic-cybersecurity/ai-guardrail-architectures/selective-prediction
28. Conformal + FDR 控制的医学实体抽取（可承诺错误率上限） — https://www.themoonlight.io/es/review/conformal-prediction-for-risk-controlled-medical-entity-extraction-across-clinical-domains

**表格结构**
29. 表格结构模型定位能力实测对比（SLANet AP50 3.67） — https://link.springer.com/article/10.1007/s40747-025-01975-w/tables/7
30. TableStructureRec 开源评测榜（TEDS） — http://github.chaintip.org/jnnycn007/TableStructureRec
31. PaddleX PR #4756：多表单元格分组错位修复 — https://github.com/PaddlePaddle/PaddleX/pull/4756
32. SPARTAN：规则法表格抽取（宁漏不滥） — https://www.clearskyscience.com/en/10.1038/s41598-026-44325-7
33. "Anchor to the grid, not to text presence"（空值必须显式） — https://digitalrelics.uk/posts/ocr-htr-pipelines/ocr-tables-registers
34. 腾讯表格图像识别：深度分割 + 框线几何 + 字符归属 — https://blog.csdn.net/tencent_teg/article/details/94080906

**降噪治理与度量**
35. 合规假阳性削减（四眼 + 回测 + 不可变审计 + "never tune away real risk"） — https://www.creodata.com/blog/reduce-false-positives-aml-screening
36. AML 假阳性度量与调优（TP 保留率配对、影子模式、分层） — https://beefed.ai/en/reduce-aml-false-positives-metrics-tuning
37. 合规降噪 8 周实验（阈值/校准/上下文富化/HITL 再训练） — https://www.upscend.com/blogs/how-can-teams-reduce-compliance-false-positives-in-8-weeks
38. 告警系统阈值与队列运营（SLA、disposition 必录） — https://intelligentfraud.com/2026/06/16/building-fraud-alert-systems-a-2026-technical-guide

**溯源 UI / 引用**
39. 引用感知架构（亚秒级定位、幻觉引用剔除） — https://digitalelliptical.com/blog/designing-citation-aware-ai-applications
40. Flow Specialty：prompt 给 rationale 不足以降本，必须在管线保留 bbox — https://engineering.zooz.com/flow-specialty/how-citations-adds-trust-in-ai-document-processing-bbcb7259a5b6
41. 引用与证据强度的 UI 模式（文字化、引用墓地） — https://www.aydesign.ai/blog/ai-citation-source-ui-patterns-2026

**LLM 回归门禁**
42. Evals are the new tests（分层金标 + CI 阻断） — https://zorost.com/evals-are-the-new-tests
43. Eval harness 三组件与 cadence — https://www.databuzzltd.com/insights/evaluation-harnesses-for-llms
44. 生产评估（真实流量取样、分项 diff） — https://llmbestpractices.com/ops/llm-evaluation-in-production

**校验机制**
45. Check digit / Luhn / ISO 7064 的覆盖范围与"算术胜过模型置信度" — https://multigrid.ai/learn/checksum-validated-identifier-field
46. 交叉一致性、双重录入、控制总数等数据校验技术 — https://www.truegeometry.com/api/exploreHTML?query=Data%20verification%20techniques
