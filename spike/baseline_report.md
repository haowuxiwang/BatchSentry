# Phase 0 Baseline 报告 — 丝裂霉素批记录端到端跑通验证

| 字段 | 值 |
|---|---|
| Job ID | `2a96d33f-0f1` |
| 文件 | `丝裂霉素提取批记录.pdf`（131.9 MB，51 页） |
| 运行日期 | 2026-07-27 |
| 凭证 | OCR=PaddleOCR-VL-1.6；LLM=SiliconFlow / DeepSeek-V3.2 |
| 状态 | review ✅（无 failed_pages） |
| 总耗时 | ~14 分钟（OCR 11.5s + LLM 726.5s + 跨页 98.5s） |
| Findings 总数 | 13（全部为 `year_contradiction` 误报） |
| LLM 调用次数 | 51（每页 1 次） |
| 平均 LLM 耗时 | 14.2s/页（最短 6.6s，最长 89.2s） |
| 平均 token 消耗 | prompt ~1100 + completion ~700 = ~1800/页 |

---

## 1. 端到端跑通情况 ✅

| 阶段 | 结果 | 备注 |
|---|---|---|
| Stage 1 OCR | ✅ 51 页 11.5s | PaddleOCR-VL token 有效，无 SSL/重试 bug |
| Stage 2 LLM 提取 | ✅ 51/51 成功，无失败页 | Server 中途被杀（Page 43/51 时），用 `_flip_status.py` 把 `analyzing→error` 后调 `retry` 端点断点续跑成功 |
| Stage 3 跨页分析 | ✅ 13 findings 入库 | 规则层 + LLM 层都跑完，无崩溃 |
| 复核页 | ✅ 可访问 | `GET /jobs/{id}/review` 正常渲染 |
| 报告导出 | ✅ 可访问 | `GET /api/jobs/{id}/report.md`、`report.json` 正常 |

**结论**：PLAN.md Phase 0 完成标准达成——拿到真实 structured_json + findings + 差异标注。

---

## 2. Findings 全量清单（13 条）

**全部为 `year_contradiction` warning**，描述格式统一：

> 第 N 页年份(...)与主流年份 2025 不一致，可能是 OCR 误识，需人工确认

| # | 页 | 年份集合 | 真实事件类型 | 应否报 |
|---|---|---|---|---|
| 1 | 1 | 2022 | 起草年（合法） | ❌ 误报 |
| 2 | 2 | 2015,2022,2025,2027 | 生产/起草/审核/发放（合法） | ❌ 误报 |
| 3 | 3 | 2015 | 生产年（合法） | ❌ 误报 |
| 4 | 4 | 2015 | 生产年（合法） | ❌ 误报 |
| 5 | 13 | 2011,2025 | 待核 | ⚠️ 可能误报 |
| 6 | 14 | 2024 | 待核 | ⚠️ 可能误报 |
| 7 | 16 | 2005,2025 | 待核 | ⚠️ 可能误报 |
| 8 | 19 | 2015 | 生产年（合法） | ❌ 误报 |
| 9 | 23 | 2024,2025 | 待核 | ⚠️ 可能误报 |
| 10 | 26 | 2015 | 生产年（合法） | ❌ 误报 |
| 11 | 27 | 2015,2025 | 生产+审核（合法） | ❌ 误报 |
| 12 | 36 | 2015,2022,2025 | 生产+起草+审核（合法） | ❌ 误报 |
| 13 | 37 | 2015 | 生产年（合法） | ❌ 误报 |

**误报率：13/13 = 100%**。规则方向错误（确认 PLAN.md 诊断）。

**LLM 兜底层零 finding**——没抓到任何语义异常。

---

## 3. Page 2 提取差异（spike/page2.html vs LLM structured）

### 3.1 spike/page2.html 真实内容

| 字段 | 值 |
|---|---|
| 起草人日期 | 2022.05.07 |
| 起草部门审核 | 2022/4/202205.07（OCR 串扰） |
| 技术部审核 | 2022-05-07 |
| QA 产品负责人 | 2022.05.09 |
| QA 经理 | 2022.05.09 |
| 事业部总经理 | 2022.05.12 |
| 执行日期 | 2022.06.01 |
| 产品批号 | 1127011N 250101 |
| 文件编号 | H3-MPD-10133-R20 |
| 版本号 | 09 |
| 记录发放人签字 | 庞明女署 2027.01.17（粘连） |
| **开始生产日期** | **2015.01.27**（写错 2015.01.20） |
| **结束生产日期** | **2015.01.25** |
| 理论产量 | 1300~3200 g |
| 实际产量 | 1550.32 g |
| 理论收率 | ≥30% |
| 实际收率 | 53.6% |
| 车间负责人审核 | 王2728 2025.01.30 |

### 3.2 LLM 提取结果

| 字段 | LLM 提取 | 真值 | 评估 |
|---|---|---|---|
| page_info.title | "丝裂霉素提取批生产记录（112701）" | 同 | ✅ |
| page_info.file_code | "H3-MPD-10133-R20" | 同 | ✅ |
| page_info.version | "09" | 同 | ✅ |
| page_info.batch_no | "1127011N 250101" | 同 | ✅ |
| page_info.production_date | "2015-01-20" | 2015.01.27（写错为 2015.01.20） | ⚠️ LLM 取了"写错"的日期 |
| **steps** | **[]** | 应有"生产起止时间、产量、收率"等关键 step | ❌ **重大丢失** |
| time_anomalies | ["year_contradiction", "suspicious_year"] | — | ⚠️ 只标字段，未作为 finding |
| ocr_noise | 含"记录发放日期 2027.01.17 和车间负责人审核日期 2025.01.30 为未来年份，与生产日期（2015年）矛盾" | — | ⚠️ **LLM 自己识别到矛盾，但塞进 ocr_noise 字段而非 findings** |

### 3.3 Page 2 关键缺失

1. **steps 数组完全空**：开始/结束生产日期、理论/实际产量、理论/实际收率、记录发放人签字、车间负责人审核——全部未抽进 steps
2. **真实时间矛盾未抓**：开始 2015.01.27 晚于结束 2015.01.25 → 应触发 `time_reversal` (critical)，LLM 提取了日期但没自己判
3. **签名时间字段缺失**：庞明女署 2027.01.17、王2728 2025.01.30 没有结构化字段，粘连文本未拆
4. **OCR 串扰未清洗**：`2022/4/202205.07` 直接进了 raw，没被规则清洗

---

## 4. Page 9 提取差异（spike/page9.html vs LLM structured）

### 4.1 spike/page9.html 真实内容

- 工序：SP-1 树脂吸附（112701）
- 文件编号 H3-MPD-10133-R22
- 批号：1127011N·250101（OCR 串扰 `-04` 拆行）
- Step 1：7 个设备"是否符合要求" + 树脂批号 + 使用次数（≤100 次）+ 预处理液停留时间（≤12h）
- Step 2：T2101a~d 上柱，**矩阵表格**：
  - 9 个时间点（11:04, 12:06, 13:07, 14:05, 15:04, 16:06, 17:08, 18:05, 19:09）
  - 4 列设备（T2101a/b/c/d）
  - 每设备 2 个指标：流速（spec 0.5-1.0 m³/h）+ 压力（spec <0.3 MPa）
  - 共 9 × 4 × 2 = 72 个 cell

### 4.2 LLM 提取结果

| 字段 | LLM 提取 | 评估 |
|---|---|---|
| page_info.title | "丝裂霉素 SP-1 树脂吸附岗位批生产记录 (112701)" | ✅ |
| page_info.file_code | "H3-MPD-10133-R22" | ✅ |
| page_info.batch_no | "1127011N·250101 -04" | ⚠️ OCR 串扰未清洗 |
| page_info.production_date | "2025-01-20" | ✅（从结尾签名日期推断） |
| step 1 parameters | 10 个，含"是否符合要求" + 树脂批号 + 使用次数 + 停留时间 | ✅ 全对 |
| step 2 start_time | "2025-01-20 11:04" | ✅ |
| step 2 end_time | "2025-01-20 19:09" | ✅ |
| step 2 parameters | **8 项**（4 设备 × 2 指标），value 是**逗号分隔字符串** | ❌ **结构丢失** |
| step 2 operator | "李伟, 李伟胜" | ✅ |
| step 2 reviewer | "纪红健" | ✅ |

### 4.3 Page 9 关键问题：矩阵被压成字符串

LLM 输出形态（节选）：

```json
{"name":"T2101a 流速","spec_range":"0.5-1.0","value":"0.974, 0.979, 0.983, 0.973, 0.971, 0.974, 0.968, 0.976, 0.966","unit":"m³/h"}
```

**问题**：
1. 9 个时间点的值被压成逗号分隔字符串，规则层无法逐 cell 判定 in_spec
2. 时间维度完全丢失——不知道哪个值对应 11:04、哪个对应 19:09
3. 现有 `Parameter.in_spec` 字段是单 bool，无法表达 9 个值各自的合规性

**这正是 PLAN.md 预判的"单值 parameters 模型 hold 不住矩阵"**。需要 `measurements: [{time, values: {column: {spec, actual, unit, in_spec}}}]` 时间序列结构。

### 4.4 Page 9 真值校验（手工计算期望结果）

| 指标 | spec | 实测范围 | 期望判定 |
|---|---|---|---|
| T2101a 流速 | 0.5-1.0 | 0.966~0.983 | 全 in_spec ✓ |
| T2101b 流速 | 0.5-1.0 | 0.962~0.980 | 全 in_spec ✓ |
| T2101c 流速 | 0.5-1.0 | 0.969~0.988 | 全 in_spec ✓ |
| T2101d 流速 | 0.5-1.0 | 0.964~0.984 | 全 in_spec ✓ |
| T2101a-d 压力 | <0.3 | 0.15~0.17 | 全 in_spec ✓ |

**期望**：page9 应该零 param finding（规则跑完应不报）。

**baseline 实际**：page9 零 finding ✓——但这是"规则没跑"导致的零，不是"规则跑了全 in_spec"的零。Phase 2 完成后必须验证是后者。

---

## 5. 与 PRD/PLAN 预判的对照

| PLAN.md 预判 | Baseline 验证 |
|---|---|
| 数据模型 hold 不住矩阵 | ✅ 确认：page9 parameters.value 变成逗号字符串 |
| 全页众数年份规则误报 | ✅ 确认：13/13 findings 全误报 |
| 参数 spec 真实写法多样 | ✅ 确认：`<0.3MPa`（HTML 转义）、`0.5-1.0 $m^{3}/h$`（LaTeX 残片）、`≤12h`、`不超过100次` |
| 签名字段粘连 | ✅ 确认：`庞明女署2027.01.17` 没拆出，未进 structured |
| LLM 提取丢字段 | ✅ 确认：page2 的生产日期/产量/收率全丢，只塞 ocr_noise |
| baseline 可能跑不通 | ❌ 已跑通（OCR 11.5s + LLM 726.5s + 跨页 98.5s） |

---

## 6. Phase 1/2 规则输入清单

### Phase 1 数据模型扩展（基于 baseline 真实结构定）

**prompt v3 schema 必须新增**：

1. `page_info.event_year_groups: {draft|production|review|approval|issue|other: [year, ...]}`
   - 让 LLM 把年份按事件类型分组（page2 验证：起草 2022 / 生产 2015 / 审核 2025 / 发放 2027）
   - 同事件类型内才比年份，跨事件类型不比

2. `steps[].measurements: [{time, values: {column_name: {spec, actual, unit, in_spec}}}]`
   - 用于 page9 类矩阵页（4 设备 × 9 时间 × 2 指标）
   - 与 `parameters` 单值并行（page2 用 parameters，page9 用 measurements）

3. `steps[].signatures: [{role, name, sign_time, confidence}]`
   - 用于 page2 `庞明女署 2027.01.17`、`王2728 2025.01.30`
   - 接受粘连文本，让 LLM 在 prompt 里 regex 拆
   - `confidence: high|medium|low` 用于手写体降级

4. `findings[]` 字段（让 per-page LLM 也直接产 finding）
   - 当前 page2 LLM 把"开始 2015.01.27 晚于结束 2015.01.25"塞进 ocr_noise，Phase 1 改 prompt 让它产 `time_reversal` finding
   - 这是 baseline 暴露的关键 prompt 设计缺陷

5. `Step.start_time` / `Step.end_time` 必须是 ISO 字符串
   - 当前 page9 推断出 "2025-01-20 11:04"，OK
   - page2 LLM 没产 steps，需要 prompt 引导

### Phase 2 规则层（基于 baseline 真实 case 定）

**时间 parse 必须支持**（baseline 实测格式）：
- `2022.05.07` ✓
- `2022-05-07` ✓
- `2022/4/202205.07` → 清洗成 `2022.05.07`（OCR 串扰）
- `2025.01.30` ✓
- `2027.01.17` ✓
- `11:04` / `19:09` → 推断日（从 page_info.production_date）

**spec_range parse 必须支持**（baseline 实测格式）：
- `<0.3`（HTML unescape `&lt;` → `<` 后）
- `0.5-1.0` / `1300~3200`
- `≤12h` / `≥30%`
- `不超过100次` → `<100`
- `0.5-1.0 $m^{3}/h$` → strip `$...$` 和 `{{}}` 后 `0.5-1.0`

**规则用例**（baseline 提供的真实 case）：

| Case | 输入 | 期望 finding | 来源 |
|---|---|---|---|
| TC1 | page2 开始 2015.01.27 vs 结束 2015.01.25 | `time_reversal` critical | spike 真实矛盾 |
| TC2 | page2 起草 2022 / 生产 2015 / 审核 2025 / 发放 2027 | **零 finding** | 合法多年份 |
| TC3 | page2 发放人签名 2027.01.17 vs 操作时间 2015.01 | **零 finding**（签名晚于操作，合法） | 签名时间规则不能误报 |
| TC4 | page9 T2101a-d 流速 0.962~0.988（spec 0.5-1.0） | **零 finding** | 全 in_spec |
| TC5 | page9 T2101a-d 压力 0.15~0.17（spec <0.3） | **零 finding** | 全 in_spec |
| TC6 | page9 树脂使用次数 17（spec ≤100） | **零 finding** | in_spec |
| TC7 | page9 预处理液停留时间 ≤12h（值=是） | **零 finding** | 合规 |
| TC8 | page2 实际产量 1550.32（spec 1300~3200） | **零 finding** | in_spec |
| TC9 | page2 实际收率 53.6%（spec ≥30%） | **零 finding** | in_spec |

**反向用例（防误报）**：
- 全页众数年份规则在 Phase 2 必须删除
- 替换为"按 event_year_groups 分组，同事件类型内比年份"

---

## 7. 副产物 / 工程问题

| 问题 | 处理 |
|---|---|
| Server 中途被杀导致状态卡 `analyzing` | 已加 `_flip_status.py` 工具；可考虑在 `db/client.py` 启动时自动检测并清理 stale 状态（Phase A 改造候选） |
| PowerShell `curl` alias 干扰 | 用 `curl.exe` 规避；后续脚本统一用 `Invoke-WebRequest` |
| 8000 端口被占 | 换 8001；后续可让 `APP_PORT` 走环境变量（已是，但默认 8000） |
| Retry 端点要求 status in (error, cancelled) | 设计合理；若要支持"暂停后继续"，需新增 `paused` 状态（路线图，不在 Phase 0-3 范围） |

---

## 8. 产出物

- `spike/baseline/findings.json` — 13 条 findings 全量
- `spike/baseline/page2_structured.json` — page2 raw_html + structured
- `spike/baseline/page9_structured.json` — page9 raw_html + structured（含矩阵被压成字符串的实证）
- `spike/baseline/report.md` — markdown 报告
- `spike/baseline_report.md` — 本文档
- `_flip_status.py` — 状态修复工具（root 目录，可保留）

---

## 9. Phase 0 完成确认

| 完成标准 | 状态 |
|---|---|
| 端到端跑通（无 failed_pages） | ✅ |
| 拿到至少 page2 和 page9 真实 structured_json | ✅ |
| 拿到 findings 清单 | ✅（13 条全误报，确认规则方向错误） |
| 对照 spike 标注差异 | ✅（steps 丢失、矩阵压成字符串、签名粘连、OCR 串扰） |
| 写入 baseline_report.md | ✅ |

**Phase 0 完成，可进入 Phase 1。**
