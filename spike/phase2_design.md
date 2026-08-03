# Phase 2 详细设计 — cross_page_analyzer 重写

| 字段 | 值 |
|---|---|
| 版本 | v1（待 user 确认） |
| 日期 | 2026-07-27 |
| 输入 | [spike/baseline_report.md](./baseline_report.md) + Phase 1 验证结果 |
| 目标文件 | [core/cross_page_analyzer.py](file:///d:/learn/claudecode/pharma-batch-checker/core/cross_page_analyzer.py) |
| 不动文件 | `core/page_analyzer.py`、`models/schemas.py`、`db/schema.sql` |

---

## 1. 职责边界（per-page LLM vs cross-page 规则层）

Phase 1 后 per-page LLM（v3 prompt）已能产 findings。需明确两层职责，避免重复或漏报。

| 层 | 输入 | 产出 | 兜底关系 |
|---|---|---|---|
| per-page LLM (Stage 2) | 单页 raw_html | structured_json + 页内 findings | LLM 不稳定，可能漏抓 |
| cross-page 规则层 (Stage 3) | 全部页 structured_json | 跨页 findings + 规则复判 findings | **规则层兜底**：即使 LLM 漏抓，规则跑结构化数据必抓 |

**去重策略**：相同 `(page, type, description_hash)` 的 finding 合并，规则层 finding 标注 `source: "rule"`，LLM finding 标注 `source: "llm_page"` / `"llm_cross"`。当前阶段不实现自动合并（DB findings 表保留所有），由人工复核页去重。**理由**：避免合并逻辑 bug 漏报关键问题，宁可多报让人工拒。

---

## 2. 规则层架构（4 个确定性规则 + 1 个 LLM 兜底）

```
analyze_cross_page(page_structures)
  ├── R1: _check_time_reversal         # 时间检查（4 项子规则）
  ├── R2: _check_year_contradiction     # 替换全页众数 → 按事件类型分组
  ├── R3: _check_param_out_of_spec      # 参数范围检查（含 spec parse）
  ├── R4: _check_suspicious_dates       # 异常日期
  ├── R5: _check_signature_time_anomaly # 签名时间 vs 操作时间
  └── LLM: _llm_fallback_check          # 仅处理规则无法判定的参数
```

**执行顺序**：R1 → R2 → R4 → R5 → R3（规则可判定的）→ LLM 兜底（R3 漏网的）→ 合并去重 → 返回。

---

## 3. R1 时间检查（4 项子规则）—— 详细规则

### 3.1 数据来源
- `step.start_time` / `step.end_time`（ISO 字符串或 `YYYY.MM.DD` / `HH:MM`）
- `page_info.production_date`（用于推断仅有 `HH:MM` 的时间）
- `event_year_groups.production`（用于补全年份缺省）

### 3.2 时间 parse 函数 `_parse_time(s: str, fallback_date: str) -> datetime | None`

支持格式（按优先级匹配）：

| 输入格式 | 示例 | parse 行为 |
|---|---|---|
| ISO | `2025-01-20 11:04` | 直接 `datetime.fromisoformat` |
| `YYYY.MM.DD HH:MM` | `2015.01.27 14:30` | 替换 `.` → `-` 后 parse |
| `YYYY.MM.DD` | `2015.01.27` | 同上，时间设 00:00 |
| `YYYY-MM-DD` | `2015-01-27` | 直接 parse |
| `YYYY/MM/DD` | `2022/4/202205.07` ⚠️ OCR 串扰 | **清洗**：检测含 2 个年份时取后者 + 月日 → `2022.05.07` |
| `HH:MM`（仅时间） | `11:04` | 用 `fallback_date` 补全 |
| `YYYY.MM.DD HH:MM` 含粘连 | `庞明女署2027.01.17` | **正则提取** `\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})` |

**parse 失败返回 None**，规则跳过该时间（不报异常）。

### 3.3 R1-a 工序内时间倒序

```
对每页每个 step:
  if step.start_time and step.end_time:
    t_start = _parse_time(step.start_time, page.production_date)
    t_end = _parse_time(step.end_time, page.production_date)
    if t_start and t_end and t_start > t_end:
      emit finding(
        page=page_num,
        type="time_reversal",
        severity="critical",
        description=f"第{page_num}页 工序{step.step_no} 开始时间({step.start_time}) 晚于结束时间({step.end_time})",
        ocr_text=f"{step.start_time} → {step.end_time}"
      )
```

**真值验证**（baseline TC1）：
- page2 开始 2015.01.27 vs 结束 2015.01.25 → time_reversal(critical) ✅

### 3.4 R1-b 工序间时间倒序（跨页关键）

```
# 按 page_number, step_no 排序所有 step
ordered_steps = sorted([(page, step) for page in pages for step in page.steps],
                       key=lambda x: (x[0].page_number, x[1].step_no or 0))

for i in range(1, len(ordered_steps)):
  prev_page, prev_step = ordered_steps[i-1]
  curr_page, curr_step = ordered_steps[i]
  if not prev_step.end_time or not curr_step.start_time:
    continue
  t_prev_end = _parse_time(prev_step.end_time, prev_page.production_date)
  t_curr_start = _parse_time(curr_step.start_time, curr_page.production_date)
  if t_prev_end and t_curr_start and t_curr_start < t_prev_end:
    emit finding(
      page=curr_page.page_number,
      type="time_reversal",
      severity="critical",
      description=f"第{curr_page.page_number}页 工序{curr_step.step_no} 开始({curr_step.start_time}) 早于第{prev_page.page_number}页 工序{prev_step.step_no} 结束({prev_step.end_time})",
      ocr_text=f"{curr_step.start_time} < {prev_step.end_time}"
    )
```

**注意**：当前 baseline 数据没有跨页时间矛盾 case，此规则是"防御性"实现（PLAN.md 要求），跑丝裂霉素应该零 finding。

### 3.5 R1-c 签名时间异常

见 R5（独立规则，与 R1 解耦）。

### 3.6 R1-d 异常日期（年份 < 2000 或 > 当前+1）

见 R4。

---

## 4. R2 年份矛盾检查 —— 替换"全页众数"规则

### 4.1 删除逻辑

```python
# ❌ 删除（baseline 100% 误报根因）
mode_year = Counter(all_years).most_common(1)[0][0]
for page, years in page_years.items():
    if years != {mode_year}:
        emit year_contradiction(...)
```

### 4.2 新逻辑 —— 按 `event_year_groups` 分组比较

```
对每页:
  eyg = page.event_year_groups
  if eyg is None:
    continue  # 老数据无此字段，跳过
  for event_type in ["draft", "production", "review", "approval", "issue"]:
    years = eyg[event_type]
    if len(years) <= 1:
      continue  # 单一年份，无矛盾
    # 同事件类型内多年份 → 矛盾
    emit finding(
      page=page_num,
      type="year_contradiction",
      severity="warning",
      description=f"第{page_num}页 {event_type} 事件内年份不一致: {years}",
      ocr_text=str(years)
    )
```

**关键差异**：旧规则跨事件类型比（page2 起草 2022 vs 生产 2015 误报），新规则只同事件类型内比。

### 4.3 真值验证

| Case | 旧规则 | 新规则 | 期望 |
|---|---|---|---|
| page2 draft=[2022] | 误报 | 不报（单一年份） | ✅ 零 finding |
| page2 production=[2015] | 误报 | 不报（单一年份） | ✅ 零 finding |
| page2 review=[2022, 2025] | 误报 | **报** warning（同事件类型内矛盾） | ⚠️ 待确认是否合理 |
| page2 issue=[2027] | 误报 | 不报（单一年份） | ✅ 零 finding |
| page1 draft=[2022] | 误报 | 不报 | ✅ 零 finding |

**待确认**：page2 review=[2022, 2025] 是否合理？spike 真值：起草 2022 / 车间审核 2025。这其实是两个不同的事件（起草审核 vs 车间审核），但 v3 prompt 都塞进 review 组。**两条路**：
- A：保留告警，由人工判（半自动定位精神）
- B：改 v3 prompt 把 review 拆成 `draft_review` / `workshop_review` 两组

**推荐 A**（不改 prompt，保留告警让人工判）。需要 user 确认。

---

## 5. R3 参数范围检查 —— spec_range parse + 规则可判 + LLM 兜底

### 5.1 数据来源
- `step.parameters[]`（单值参数）
- `step.measurements[].values{column: {spec, actual, unit, in_spec}}`（矩阵 cell）

### 5.2 spec_range parse 函数 `_parse_spec(spec: str) -> SpecBounds | None`

```python
@dataclass
class SpecBounds:
    op: str  # "between" | "lt" | "le" | "gt" | "ge"
    low: float | None
    high: float | None
```

**预处理**：
```python
s = spec.strip()
s = html.unescape(s)              # &lt; → <, &le; → ≤, &gt; → >
s = re.sub(r"\$+[^$]*\$+", "", s)  # strip $...$ LaTeX 残片
s = re.sub(r"\{\{.*?\}\}", "", s) # strip {{...}}
s = s.replace("NMT", "<").replace("NLT", ">=")  # 药典简写
s = s.replace("≤", "<=").replace("≥", ">=")
s = s.strip(" .,;")
```

**模式匹配**（按优先级）：

| 模式 | 示例 | op | low | high |
|---|---|---|---|---|
| `^-?(\d+\.?\d*)\s*[-~]\s*(-?\d+\.?\d*)$` | `0.5-1.0` / `1300~3200` | between | 0.5 | 1.0 |
| `^<?=?\s*(\d+\.?\d*)$` 或 `^<=?\s*(\d+\.?\d*)$` | `<0.3` / `<=0.3` / `≤100` | lt / le | None | 0.3 |
| `^>?(=)?\s*(\d+\.?\d*)$` | `≥30` / `>5` | ge / gt | 30 | None |
| `^不超过\s*(\d+)$` | `不超过100` | lt | None | 100 |
| `^不少于\s*(\d+)$` | `不少于30` | ge | 30 | None |
| 其他 | `应澄清` / `应符合要求` | None | None | None |

**返回 None 的进入 LLM 兜底队列**。

### 5.3 单值参数检查 `_check_param(param, step, page)`

```
if param.value is None or param.value == "":
  return None  # 空值不报（completeness 由 LLM 抓）
spec_bounds = _parse_spec(param.spec_range)
if spec_bounds is None:
  return param  # 加入 LLM 兜底队列（return 给上层收集）
actual = _parse_number(param.value)
if actual is None:
  return param  # value 非数字（如"是"/"合格"），加入 LLM 兜底队列
in_spec = _judge(spec_bounds, actual)
if not in_spec:
  emit finding(
    page=page_num,
    type="param_out_of_spec",
    severity="warning",
    description=f"第{page_num}页 参数 {param.name}={actual}{unit} 不在规格 {param.spec_range} 内",
    ocr_text=f"{param.name}: spec={param.spec_range} value={param.value}"
  )
# 同步回写 param.in_spec（用于复核页显示）
param.in_spec = in_spec
```

### 5.4 矩阵参数检查（cell 级）

```
for measurement in step.measurements:
  t = measurement.time
  for col_name, val in measurement.values.items():
    if val.actual is None or val.actual == "":
      continue  # 空 cell 不报
    spec_bounds = _parse_spec(val.spec)
    if spec_bounds is None:
      # 把 (time, col, val) 加入 LLM 兜底队列
      llm_queue.append({"page": p, "step": step_no, "time": t, "col": col_name, "spec": val.spec, "actual": val.actual})
      continue
    actual = _parse_number(val.actual)
    if actual is None:
      llm_queue.append(...)
      continue
    in_spec = _judge(spec_bounds, actual)
    val.in_spec = in_spec  # 回写
    if not in_spec:
      emit finding(
        page=p,
        type="param_out_of_spec",
        severity="warning",
        description=f"第{p}页 {col_name} 在 {t} 时实测 {actual}{unit} 不在规格 {val.spec} 内",
        ocr_text=f"{t} {col_name}: spec={val.spec} actual={val.actual}"
      )
```

### 5.5 真值验证

| Case | spec | actual | 期望判定 | 期望 finding |
|---|---|---|---|---|
| TC4 page9 T2101a_流速 11:04 | 0.5-1.0 | 0.974 | in_spec | 零 |
| TC5 page9 T2101a_压力 11:04 | <0.3 | 0.15 | in_spec | 零 |
| TC8 page2 实际产量 | 1300~3200 | 1550.32 | in_spec | 零 |
| TC9 page2 实际收率 | ≥30% | 53.6% | in_spec | 零 |
| TC6 page9 树脂使用次数 | ≤100 | 17 | in_spec | 零 |
| TC7 page9 预处理液停留时间 | ≤12h | "是" | LLM 兜底 | 零 |

**完成标准**：规则跑完 page9 应**零 param_out_of_spec finding**。

---

## 6. R4 异常日期

```
current_year = datetime.now().year
for page in pages:
  for date_str in _collect_all_dates(page):  # 所有 step.start/end + signature.sign_time + page_info.production_date
    year = _extract_year(date_str)
    if year is None:
      continue
    if year < 2000 or year > current_year + 1:
      emit finding(
        page=page_num,
        type="suspicious_date",
        severity="warning",
        description=f"第{page_num}页 日期 {date_str} 年份 {year} 异常（早于2000或晚于{current_year+1}）",
        ocr_text=date_str
      )
```

**真值验证**：
- page2 发放 2027.01.17 → year=2027 > 2026（当前+1）→ suspicious_date(warning) ✅
- page2 生产 2015.01.27 → 不报（合法历史年份）
- page9 签名 2025.01.20 → 不报

**注意**：当前日期 2026-07-27，current_year+1=2027。2027.01.17 是否算 suspicious？
- 严格按 PLAN.md 规则：year > current+1 才报，2027 = 2027 不报
- 但 baseline_report.md 写的是"2027 晚于当前年份"误报
- **推荐**：改成 `year > current_year + 1` 严格判（即 2028+ 才报）。2027 是合法未来日期（如批次效期）。

**待 user 确认**：阈值是 `> current_year` 还是 `> current_year + 1`？

---

## 7. R5 签名时间异常

```
for page in pages:
  for step in page.steps:
    op_time = _parse_time(step.start_time or step.end_time, page.production_date)
    for sig in step.signatures:
      if not sig.sign_time:
        continue
      sig_time = _parse_time(sig.sign_time, page.production_date)
      if sig_time is None:
        continue
      if op_time and sig_time < op_time:
        emit finding(
          page=page_num,
          type="signature_time_anomaly",
          severity="warning",
          description=f"第{page_num}页 {sig.role} {sig.name} 签名时间 {sig.sign_time} 早于操作时间 {step.start_time or step.end_time}",
          ocr_text=f"{sig.name} {sig.sign_time}"
        )
```

**真值验证**：
- page2 庞明女署 2027.01.17（发放）vs 生产 2015.01.27 → **不报**（签名晚于操作，合法）
- page2 王2728 2025.01.30（车间审核）vs 生产 2015.01.27 → **不报**（审核晚于操作，合法）
- page9 纪红健 2025.01.20 vs 操作 2025.01.20 11:04 → **不报**（同日，签名晚于操作开始）

**关键**：只抓 `sig_time < op_time`（签名早于操作，逻辑不可能），不抓 `sig_time > op_time`（晚签是合法的）。

**待 user 确认**：是否需要加"签名距操作时间过长（如 > 30 天）"告警？baseline page2 车间审核距生产 10 年，per-page LLM 已产 signature_time_anomaly finding。规则层是否也抓？

**推荐**：规则层**只抓签名早于操作**（不可能事件），"签名距操作过长"由 LLM 兜底（语义判断，规则不抓）。理由：避免规则误报合理长周期复核。

---

## 8. LLM 兜底 `_llm_fallback_check`

### 8.1 输入
规则层 R3 收集的 `llm_queue`：所有 `spec_range` 无法 parse 或 `value` 无法转数字的参数。

### 8.2 Prompt（精简版，只判参数合规）

```
你是 GMP 参数合规判定助手。以下参数的规格描述无法用规则自动判定，
请基于 GMP 常识判断 actual 是否符合 spec。

参数列表：
1. 第9页 预处理液停留时间 | spec=≤12h | actual=是 | unit=h
2. 第X页 ... | spec=应澄清 | actual=澄清 | unit=

每条返回 {"index":1, "in_spec":true|false|null, "reason":"..."}
in_spec=null 表示你也无法判定。

严格输出 JSON 数组。
```

### 8.3 处理
- LLM 返回 `in_spec=true` → 不报 finding
- LLM 返回 `in_spec=false` → 产 `param_out_of_spec` finding，标注 `source: "llm_fallback"`
- LLM 返回 `in_spec=null` → 产 `completeness` finding（warning）："参数 X 规格描述模糊，需人工确认"

### 8.4 真值验证
- TC7 page9 预处理液停留时间 ≤12h vs "是" → LLM 应判 in_spec=true（"是"通常表示已确认合规）

---

## 9. _build_summary 改造

当前 `_build_summary` 输出文本给 LLM 用，Phase 2 后 LLM 兜底用专门的 prompt（参数列表），`_build_summary` 仍保留给 `_llm_based_check`（语义兜底，抓规则漏网）。

**改造点**：
- 在 summary 中加入 `measurements` 摘要（每个 step 显示前 3 行 × 列名）
- 在 summary 中加入 `event_year_groups`
- 在 summary 中加入 `signatures`
- 在 summary 中标注已抓 findings（避免 LLM 重复报）

---

## 10. Finding 字段规范

所有 finding 必须包含：

```python
{
  "page": int,
  "type": "time_reversal" | "year_contradiction" | "signature_time_anomaly" 
        | "suspicious_date" | "param_out_of_spec" | "completeness",
  "severity": "critical" | "warning" | "info",
  "description": str,
  "ocr_text": str,
  "operator": str,        # 可空
  "source": "rule" | "llm_page" | "llm_cross" | "llm_fallback"  # Phase 2 新增
}
```

**source 字段**：用于复核页区分来源，规则 finding 直接信任，LLM finding 标注低置信度。

---

## 11. 测试用例清单（基于 baseline 真实数据）

| TC | 输入 | 期望 finding | 验证方法 |
|---|---|---|---|
| TC1 | page2 step "生产" start=2015.01.27 end=2015.01.25 | time_reversal critical | R1-a |
| TC2 | page2 event_year_groups draft=[2022] production=[2015] review=[2022,2025] issue=[2027] | review 报 year_contradiction warning；其余零 | R2 |
| TC3 | page2 签名 庞明女署 2027.01.17（发放）vs 生产 2015 | 零 finding（签名晚于操作合法） | R5 |
| TC4 | page9 measurements[0].values["T2101a_流速"] spec=0.5-1.0 actual=0.974 | in_spec=true，零 finding | R3 |
| TC5 | page9 measurements[0].values["T2101a_压力"] spec=<0.3 actual=0.15 | in_spec=true，零 finding | R3 |
| TC6 | page9 step1.parameters "树脂使用次数" spec=≤100 actual=17 | in_spec=true，零 finding | R3 |
| TC7 | page9 step1.parameters "预处理液停留时间" spec=≤12h actual=是 | LLM 兜底判 in_spec=true，零 finding | LLM |
| TC8 | page2 step "实际产量" spec=1300~3200 actual=1550.32 | in_spec=true，零 finding | R3（需 page2 step 抽出来） |
| TC9 | page2 step "实际收率" spec=≥30% actual=53.6% | in_spec=true，零 finding | R3 |
| TC10 | page2 发放 2027.01.17 | suspicious_date warning（若阈值=current+1=2027，则不报；若 current=2026，则报） | R4 |
| TC11 | page9 签名 纪红健 2025.01.20 vs 操作 2025.01.20 11:04 | 零 finding（同日） | R5 |
| TC-反向 | 跑丝裂霉素全 51 页 | 规则层 findings 应 < 20 条（baseline 13 条全误报应消失） | 端到端 |

**注意**：TC8/TC9 依赖 page2 的 steps 抽出。Phase 1 验证显示 page2 v3 prompt 产 steps=0（LLM 没把生产日期/产量/收率抽进 steps，而是产了 findings）。**Phase 2 实施前需要确认**：是否改 v3 prompt 强制 page2 类页面把 step 抽出来？

**推荐**：不改 v3 prompt，接受 page2 steps=0 现状，TC8/TC9 改为"如果 step 抽出来则判"。理由：page2 的产量/收率其实是 batch-level 信息（不是工序步骤），可放 page_info.batch_yield / batch_recovery，但这是 Phase 1 数据模型范围，Phase 2 不扩。

---

## 12. 实施顺序

1. 写 `_parse_time` + 单测（TC1 parse 各种格式）
2. 写 `_parse_spec` + 单测（TC4-TC9 spec 解析）
3. 改 `_rule_based_check` → 拆成 R1-R5 五个函数
4. 写 R1-a（页内 time_reversal）→ 跑 TC1
5. 写 R2（年份按事件分组）→ 跑 TC2
6. 写 R3（参数范围）→ 跑 TC4-TC6
7. 写 R4（异常日期）→ 跑 TC10
8. 写 R5（签名时间）→ 跑 TC3/TC11
9. 写 LLM 兜底 → 跑 TC7
10. 改 `_build_summary` + `_llm_based_check`
11. 端到端跑丝裂霉素 → 验证 TC-反向

---

## 13. 待 user 确认的 5 个问题

1. **R1-b 跨页工序间时间**：当前 baseline 没有跨页时间矛盾 case。规则是否仍实现？（推荐：是，防御性）
2. **R2 page2 review=[2022, 2025]**：是否报 year_contradiction？（推荐：报，让人工判）
3. **R4 suspicious_date 阈值**：`> current_year` (2026) 还是 `> current_year + 1` (2027)？（推荐：+1，2027 合法）
4. **R5 签名距操作过长**：规则层是否抓？（推荐：不抓，LLM 兜底）
5. **page2 steps=0 现状**：TC8/TC9 改为"如果抽出来则判"？（推荐：是）

---

## 14. 不在 Phase 2 范围

- 不改 `core/page_analyzer.py` v3 prompt（Phase 1 已定）
- 不扩 `models/schemas.py`（Phase 1 已加 measurements/signatures/event_year_groups）
- 不动 DB schema（findings 表 source 字段用 description 前缀 `[rule]` / `[llm]` 区分，不加列）
- 不动 API/templates/frontend
- 不实现 findings 自动去重（人工复核页处理）
- 不加 batch_yield / batch_recovery 到 page_info（Phase 1 范围外）
