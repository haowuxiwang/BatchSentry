# Changelog

本文件记录 BatchSentry 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.1]

> 起点：`v1.1.0`（`ca355d6`）之后的补丁级发布。含 `_parse_spec` 修复与降噪专项
> 调研文档（`e2417cd`、`4273c1f`，此前未随二进制发布），以及下面两项降噪落地。

### 新增

- **抑制留痕（P0-2，合规必需项）**：被降噪规则抑制的 LLM 误报从"只记一个计数"
  改为**落台账 + 可查 + 可回退 + 可抽检**。
  - `drop_unfounded_spec_findings()` 返回 `(保留, 明细列表)`（**第二项是列表不是
    计数**）；每条带非空 `reason`（含命中的三元组及各自判定）与结构化 `evidence`。
  - schema v11 新增 `finding_suppressions` 台账表：抑制 ≠ 删除（行只追加、不删除），
    `reason` 由 `CHECK` 强制非空（显式空白字符集，堵住 Tab/换行绕过）。
  - 新端点 `GET /api/jobs/{id}/suppressions`（含 page 过滤与全 job 计数）、
    `POST /api/jobs/{id}/suppressions/{sid}/revert`（一键回退为正式 finding；
    台账行只记 `reverted_at`/`reverted_finding_id`，重复回退 400，跨 job 404）。
  - 复核页新增"已抑制条目"面板：逐条展示理由与命中证据，支持回退。
  - 落库与正式 finding **同一把锁/同一事务**；重分析只清未回退行（已回退的是人工
    审计证据）；写 `audit_log(action=spec_guard_dropped)`。
- **区域级证据锚（P0-3）**：finding 可一键定位回原页并高亮其所在 OCR 版面区域。
  - 新增纯函数模块 `core/pipeline/regions.py`（归一化 / 标签闭集 / 提取 / 锚定）。
  - `page_cache.regions_json` 保存每页区域（Paddle 块级 bbox、MinerU 块 bbox +
    `layout.json` 的 `page_size`），bbox **一律归一化到 0..1**。
  - 复核页叠加高亮框 + finding 卡片"定位原图"按钮（SSR 首屏与 AJAX 翻页共用同一
    推导与同一呈现逻辑）。
  - **只到区域级**：两个后端都不回传单元格级 bbox；自称单元格级等于给复核员一个
    错位的框。
  - **宽高比闸门**：实测第 8 页 OCR 坐标系为横向（服务端旋转过），与页面宽高比不
    一致时**明示无法定位**，而不是画一个横竖颠倒的框。
  - 锚不上（无区域 / 无特征词命中）时不显示定位入口（宁缺勿错）。

### 变更

- 版本号 1.1.0 → **1.1.1**（`main.APP_VERSION`、`package.json`、
  `package-lock.json`；`v1.1.0` tag 保持不动）。
- `tests/e2e_frozen.py` 的 `/health` 版本断言改为从 `main.APP_VERSION` 派生，
  不再硬编码（硬编码会在升版本时静默失配，把验证变成假通过）。

### 测试

- 新增 `tests/unit/test_suppression_ledger.py`（迁移 / CHECK / 单一声明处 /
  源码扫描）、`tests/unit/test_regions_anchor.py`（60 项，含真实 51 页产物复算）、
  `tests/unit/test_region_anchor_wiring.py`（16 项接线不变式 + 真实 MinerU 复算）、
  `tests/integration/test_api_suppressions.py`（10 项真实 HTTP）、
  `tests/integration/test_api_region_anchor.py`（6 项 API + SSR 一致性）。

---

## [Unreleased]

### 修复

- **`_parse_spec` 书写变体缺口（M9-spike 真实落库定位）**：真实 3526 个
  `(spec, value)` 对中存在 7 种**语义等价但此前解析失败**的写法，全部静默降级为
  `spec_unverifiable`（送人工 = 噪音）：
  - **单位夹在数字与分隔符之间**：`972 μg/mg ~1020 μg/mg`（**20 处，全是含量/效价
    这类关键质量属性**，全部解析失败；同语义的 `0.1~0.2 MPa` 一直正常）；
  - **LaTeX `±`**：PaddleOCR-VL 把印版 `±` 读成 `\pm`（51 页真实输出 **16 处 / 12 页**），
    外层还可能裹 `$`（`温度(15 \pm 5°C)`）；LLM 目前多数会归一，但规则层是判定权威，
    一旦 LLM 原样回填就静默丢判定能力；
  - **全角/Unicode 符号**：`～`(U+FF5E)、`－`、`−`(U+2212)、`—`
    （`99%～101%` 1 处失败，而同语义半角 `99%~101%` 15 处成功）；
  - **外层括号**：`(1300~3200)`。

  修复：`core/rules/parsing.py` 新增 `_normalize_spec_notation`（只归一书写形状，
  不改判定语义），在 `≤/≥ → <=/>=` 归一**之前**调用。折叠"同单位夹分隔符"时
  **保留串尾单位** —— `_try_unit_normalize` 依赖 spec 串里的单位做实测值单位换算。

  **验证**（真实数据对照，用 `git show HEAD:` 取修复前版本）：22 处由"不可解析"转为
  "可判定"，**解析结果变化 0 处、新增超差 0 处**（纯降噪，不引入新误报）。
  仍不可解析的 31 种形态全部是**正确的 fail-closed**（`是/否`、`符合规定`、
  `蓝紫色结晶性粉末`、裸数字 `1.4`/`14 L/min`）。单测 +9 项。

### 变更

- **调研与执行清单入档**：新增 `docs/NOISE_REDUCTION_SPIKE.md`（表格对齐粒度与
  PaddleOCR-VL bbox 资格的实测结论）与 `docs/NOISE_REDUCTION_TODO.md`（P0–P2 分项清单）。
  两条硬结论：① 两个后端**都不提供单元格级 bbox**（Paddle 整表一个 block、MinerU 整表
  一个 span，`_model.json` 中 `cell`/`rowspan`/`colspan` 出现 0 次）→ 对齐只能用
  **表级 bbox 粗筛 + 字段级标签匹配**；② Paddle 的 `layout_det_res.boxes[].score` 是
  **版面检测分**（实测 mean 0.579 / p50 0.545），**不是**抽取置信度，不可当数值正确性信号。

### 已知缺陷（已定位，未修）

- **Paddle 多行单元格分隔符不确定 → 值融合**（更正 v1.1.0 CHANGELOG 中"两个相邻单元格
  粘连"的表述）：Paddle 把**整个多行区块压成 1 个 `<tr>` + 两个多行 `<td colspan=N>`**
  （标签列 / 值列），标签↔值**只靠行序对应**；且分隔符形态**在同一份文档内不一致** ——
  p09 有字面 `\n`（23 处）正确成行，p21 **完全没有分隔符** →
  `A000626-221201/4` + `17 次` 融合成 `A000626-221201/417 次`，规则层与 LLM 层同时读到
  `417`（规格 ≤100 → 双双误报）。属**服务端非确定性行为**（8/26 轮次同页显示
  `A000626-2212017 ☐次`），**不做启发式还原**（无法可靠还原切分点）；
  方向见 `docs/NOISE_REDUCTION_TODO.md` 的 T-P1-1/T-P1-2（标签行数 vs 值行数一致性校验 →
  降级 `structure_warning` 交人工）。

---

## [1.1.0] — 2026-09-14

v1.1 聚焦**质量可度量、鲁棒性、知识库多源、对标能力**四方面。所有结论均基于实测
（真实 51 页手写批记录全链路 + 真实上游服务），不保留未经证实的声明。

### 新增

- **Finding 质量可度量 + 降噪（M2）**：金标集 P/R/F1 评测管线（`scripts/eval_findings.py`）；
  真实 51 页 job finding 数 **784 → 268（-65.8%）**，critical 全保留（33 → 33）。
- **规则扩展 R11–R17（M4）**：物料平衡（mass_balance）、自检自核（self_review，critical）、
  设备/清洁状态（equipment_state）、环境监测（env_monitor）、文件版本（doc_version）、
  偏差关联（deviation_link）、涂改规范（alteration）。规则以 `core/rules/registry.py::RULE_REGISTRY`
  为唯一入口，按 id 可经 `config.json` 的 `rules.disabled` 关闭。真实 51 页重放 **+9 findings
  （399，+2.3%）**，全部为真信号。
- **多源 GMP 知识库（M5）**：GMP（2010 修订）全文 + 附录 + NMPA 记录规范 + ALCOA+ +
  21 CFR Part 11 等 **6 源 / 441 条**；纯 BM25 检索（无向量库）；金标 37 条命中率 **97.3%**
  （排除已登记缺口 100%，阈值 85%）；设置页多源浏览与按源启停。
- **docling 第三对照引擎（M7）**：`core/docling_client.py`（MIT，本地推理，无 token）；
  `scripts/compare_ocr_engines.py` 输出两两差异页报告；**未安装即优雅降级**，主链不受影响。
- **三色复核分级（M6/T6.4）**：复核页按**检出来源**分档——规则命中（红）/ LLM 辅助（蓝）/
  系统校验通过（绿，按问题类型统计覆盖率）；映射单一来源 `core/finding_quality.REVIEW_TIER_BY_SOURCE`，
  SSR 与 AJAX 双端同源。
- **OCR 后端能力表（M7/T7.1）**：`OcrCapabilities` 显式声明各后端能力（切片/页子集/本地/凭据需求），
  engine/stage1/dual_compare 的字面假设改由能力表驱动。

### 变更

- **SSE 轮询常量统一（M6/T6.1）**：`api/jobs/_SSE_POLL_SECONDS = 2` 作唯一来源；
  修复 docstring（"每 2 秒"）与代码（`retry: 2000` / `asyncio.sleep(3)`）三方不一致。
- **终态快照缓存收敛（M6/T6.2）**：`(status, finished_at)` 为键的单一来源缓存；
  聚合并发流稳态查询 **16 → 1**。
- **阶段内已耗时（M6/T6.3）**：前端 `eta.js` 纯函数 + 1s 本地 ticker，长阶段（OCR/逐页/跨页）
  超 5s 起显示"已用 45 秒 / 3 分 07 秒"，不再依赖 SSE 帧间隔。
- **OCR 尺寸/形态鲁棒性（M3）**：不可无损修复页（小字号+低 DPI / 极端长宽比）显式携带
  非完整信号，不得静默标记成功。
- **覆盖率门禁 90% → 95%**（仅统计 `api/ core/ llm/ db/ config/ main`）。

### 修复

- **规格解析三处缺陷（M8 真实 51 页看图定位）**，均由"OCR/LLM 把印版符号读丢"引发：
  - **`±` 丢成空格**：印版 `15±5℃` → OCR `15 5°C` → 结构化 `15-5°C`，
    `_parse_spec` 解出反向区间 `between(15,5)` 恒为假 → 单页 21 条温度误报。
    修复：支持 `±` 形式与空格 `A B` 形式；反向区间按 `A±B` 重建（`A-B, A+B`）。
  - **小数点丢失**：手写 `4.6` → OCR `46`（规格 `3.0-5.0bar`）→ 7 条误报 +
    1 条 critical。修复：`_decimal_loss_factor` 软化（判据：实测值不含小数点
    且超差倍数 ≈10×/100×）→ `info` + 明确提示，**仍表面化不静默丢弃**；
    反例保护：`45.6 vs 0~5°C`、`25 vs ≤5.0` 维持 `warning` 铁口。
  - **LLM 自报超差无复核**：LLM 把 `<0.3MPa` 当下限，`0.16` 判成超差（7 条
    critical）；`40±3℃` 丢 `±` 后误判 `42.1` 超差。修复：新增
    `core/rules/spec_guard.py`，LLM 自报 `param_out_of_spec` 一律用**规则层同一
    解析器**复核，判为合规/疑似丢点者剔除，定位不到或不可判者保留（fail-closed）。
    真实页剔除 14/26 条。名称比对做分隔符归一（`T2101a_压力` ↔ `T2101a 压力`）。
- **真空度/负压符号约定未 fail-closed**：规格 `≤0.08MPa` × 负表压实测（`-0.068~-0.096`）
  数值比较恒真 → 真实偏差被静默放过。修复：`_sign_convention_uncertain` 命中即降级
  `spec_unverifiable` 交人工（真实页 6 处）。
- SSE 轮询常量三方漂移（文案 2s / retry 2000 / sleep 3）。
- 终态快照缓存写入同一 dict 对象，调用方改字段污染缓存（跨请求串数据）→ 改为存/返副本。
- 长阶段前端计时器在断线重连时叠加泄漏 → 定时器上提至 subscribe 作用域并配对停止。
- docling 取不到分页符时的页数切分回退（`total=0` 不再漏切）。
- `ROADMAP_v1.1.md` 中 docling 许可残留 AGPL-3.0 → 更正为 **MIT**。

### 明确不做（本轮）

- 不做 Web/移动端适配；不接真实 MES/LIMS；不换主 OCR 后端（第三方仅对照）；不做模型微调。
- 不引入 embedding/向量库（维持纯 BM25）。
- **趋势筛查（OOT）不在 v1.1 落地**：真实库 23 job 实质仅 1 个批次、参数名 37% 只出现一次、
  仅 30% 参数可机械解析规格 → 单批次无法标定阈值，验收不可证伪。结论与 v1.2 分阶段路径见
  `docs/TREND_SCREENING_EVAL.md`。

### 验证

- 全量测试通过；`scripts/release_gate.py --fail-under 95` → **OVERALL: pass**。
- 真实 51 页全链路 frozen e2e 两轮通过（首轮 paddle、第二轮 mineru）+ ui_e2e 通过。
  ⚠️ e2e 驱动打印的 "N findings" 取自 `GET /api/jobs/{id}/findings`，该端点
  `limit` 默认 **50** → 那是**首页条数不是总数**（首轮实际落库 395、第二轮 407）。
  汇报总量必须直接查库。

### 已知局限（v1.1.0 发布时点）

看图比对（`docs/M8_VISUAL_VERIFICATION.md`）除定位并修复上面 4 类缺陷外，还暴露出
以下**未修**问题，根因均不在规格解析器：

- **OCR 把相邻单元格粘进同一个 `<td>`**（如 p9 批次号尾部与使用次数粘成
  `A000626-221201/417 次` → 使用次数手写实值 `17` 被读成 `417`；p6 同类使 `中和后 pH`
  读成 `70 L 6.88`）。此类"数字本身即错"的条目**规则层与 LLM 层会被同时误报**，
  任何"规格 vs 实测"的交叉复核都无法发现（规格正确、数字也看似合理）。
- **OCR 表格列错位**：mineru 在 p08 把 `透出液流量`/`TMP` 取到左邻列的值 → 14 条超差。
- **主备后端读数分歧**：p19 手写纯度 paddle 读 `49.0`（→ 误报 critical）、mineru 读
  `99.0`（纸面实读亦为 `99.0`，`≥98%` 合规）；手写年份 `2015` vs 印版 `2025` 同类。
- **`spec_guard` 的 fail-closed 边界**：LLM 把 7 条 `<0.3 MPa` 误报合并为一条范围式
  名称（`T2101a~d`）时无法匹配单列 → 按 fail-closed 保留（7 条 critical → 1 条，未归零）。
- 其他：仅含"时:分"的时点会被锚定到**当天**日期，可能产出 `signature_time_anomaly`；
  `doc_version` 规则会把文件编号 `H3-MPD-10133-R22` 的 `R22` 误当版本号。

**不修的理由**：上述"数字本身即错"的条目，启发式软化一旦过宽就会**抑制真实超差**
（fail-closed 原则要求宁可多报交人工）；`_decimal_loss_factor` 的回归教训已证明
软化必须有可机检的强判据。方向留作后续专项（T-D）：抽取后校验（数字 token 与相邻文本
需有分隔符、列数与表头一致）、范围名（`T2101a~d`）展开后再交 `spec_guard`、
主备读数跨规格阈值分歧时强制人工仲裁、手写值置信度标注。


---

## [1.0.0] — 2026-08-20

首个正式版本：GMP 批生产记录半自动合规检查系统（OCR + LLM 结构化提取 + 规则/LLM 跨页
合规分析 + 人工复核 + 报告导出），本地单用户部署（PyInstaller exe + Electron 便携版）。
详见 `docs/发布说明_BatchSentry_v1.0.0.html`。
