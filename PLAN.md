# 方案 v2：先验证基线，再基于真实数据定规则

## 顶层定位（user 明确）

**这是半自动辅助人工复核系统，不是全自动审核。**

- 系统价值 = 减轻人工审核负担，不是取代人工
- 人工兜底复核始终在，所以**不追求零漏报**——漏了人工能补
- 真正的负担是**误报**：误报越少，人工越省
- 不要低估复杂度：真实批记录的表格矩阵/LaTeX 残片/签名粘连/OCR 串扰都是硬骨头
- "**做得不全也没关系，能帮上一部分就有价值**"——允许降级落地，不做完美主义

这个定位如何影响设计：
- LLM 提取丢几行 cell 不致命，规则只对成功提取的部分判，剩余由人工复核界面补
- 优先级："低误报" > "高覆盖率"
- baseline 跑不通不阻塞：最小修复就继续，不追求 baseline 合格与否的二元判断

## 为什么 v1 作废

v1 基于代码阅读**推断** baseline 可跑通 + 推断 OCR 输出格式。实际读了 spike/page2.html 和 page9.html（真实 OCR 产物）后发现：

- **数据模型 hold 不住**：page9 工序参数是「4 列设备 × 11 个时间点 × 2 指标」的表格矩阵，`step.parameters=[{name,spec,value}]` 单值模型完全承接不住
- **年份规则会误报**：page2 同页有 2022(起草)/2015(生产)/2025(审核)/2027(发放)，是不同事件的合法年份，现有"全页众数年份"规则会乱报 year_contradiction
- **参数规格真实写法**：`0.5-1.0 $ m^{{3}}/h $`（LaTeX 残片）、`<0.3MPa`（HTML 转义）、`≤12h`、`不超过100次`——v1 的 parse 规则全没覆盖
- **签名字段分散**：签名+日期粘连成一段文本（`庞明女署2027.01.17`），不是独立字段，抽不出来
- **基准从未真跑过**：user 明确说"过去的构建我不确定是否满足"

所以必须先用真实凭证 + 真实样本端到端跑一次，拿到 ground truth 再定规则。

## 已确认决策

1. 先跑 baseline，用事实定规则
2. 参数模型用时间序列 `measurements: [{time, values: {column: {spec, actual, unit, in_spec}}}]`，与单值 `parameters` 并行
3. 应用形态维持 PyInstaller exe + 本地服务（不动壳）
4. 时间检查范围：工序间时序 + 工序内时序 + 签名时间 + 异常日期
5. 参数判定：规则优先 + LLM 兜底

## 凭证与样本（user 提供）

- OCR: `PADDLE_OCR_API_URL` / `PADDLE_OCR_TOKEN` / `PADDLE_OCR_MODEL` 配置在 `.env`（参考 `.env.example`）
- LLM: SiliconFlow + Qwen3-8B（.env 已配）
- 样本：`samples/` 目录（丝裂霉素提取批记录.pdf），spike/page2.html、spike/page9.html 是历史 OCR 产物（可信参考）

## 实施阶段

### Phase 0 — 端到端 baseline 验证（不开新代码，只跑+观察）

目标：确认现有 pipeline 在真实凭证+真实样本下能跑通，并产出 ground truth。

1. 启动 dev server：`uvicorn main:app --reload --host 127.0.0.1 --port 8000`
2. 通过 `POST /api/jobs` 上传 `丝裂霉素提取批记录.pdf`（或直接用已有 output/ 下样本）
3. 轮询 `GET /api/jobs/{id}` 直到 status=review 或 error
4. 若 error：记录失败点（OCR? LLM parse? pipeline?），**最小修复后重试**（小 bug 顺手修，如 token/超时类；大改造停下问 user）。断点续跑可复用已写入的 page_cache，不必从头。
5. 若成功：
   - 导出每页 `structured_json`（查 `page_cache` 表 或 `GET /api/jobs/{id}/pages/{n}`）
   - 导出 findings（`GET /api/jobs/{id}/findings`）
   - 人工对照 spike/page2、page9 真实 OCR，标注：LLM 提取丢了什么？规则抓到了什么？误报了什么？
6. 产出物：`spike/baseline_report.md`——记录每页 LLM 提取的真实结构、finding 清单、与 spike HTML 的差异。**这份报告是后续所有规则的输入**

**Phase 0 完成标准**：拿到一份真实的 structured_json 样本 + findings 清单 + 差异标注，写进 baseline_report.md。半自动定位下不追求 baseline 完美，拿到够定规则的数据就过关。

### Phase 1 — 数据模型扩展（基于 baseline 报告定）

只在 Phase 0 拿到真实数据后开始。改 3 个文件：

- `core/page_analyzer.py`：prompt + JSON schema 加 `measurements` 时间序列结构 + 签名字段（`operator_sign_time`/`reviewer_sign_time`，可能要从粘连文本 regex 抽，具体看 baseline 数据）+ `handwritten` 结构化
- `models/schemas.py`：同步 `Measurement`/`MeasurementValue` 模型和 `FindingType` 枚举（对齐真实 type 值）
- 不改 DB schema：`structured_json` 是 TEXT 列，模型变了表结构不变

**完成标准**：用真实 page9 HTML 喂改后的 page_analyzer，能提取出 measurements 矩阵（11 行时间 × 4 设备 × 流速/压力），单元格 spec/actual 配对正确。

### Phase 2 — 规则层（`core/cross_page_analyzer.py`）

**时间检查**（4 项，确定性）：
- 工序内 start>end → time_reversal
- 工序间 start[i]<end[i-1] → time_reversal
- 签名时间 < 操作时间 → signature_time_anomaly（签名日期在操作之前）
- 异常日期（年<2000 或 >current+1）→ suspicious_year
- 重写年份检查：按事件类型分组（起草/生产/审核/发放各有自己的年份，不跨类型比），不再全页众数

时间格式 parse（按 baseline 数据调整）：支持 `2022.05.07` / `2022-05-07` / `2022/4/202205.07`(OCR 串扰，需清洗) / `07-17 14:30` / `14:30`（推断日）

**参数范围检查**（规则优先 + LLM 兜底）：
- `_parse_spec`：支持 `0.5-1.0` / `0.5~1.0` / `<0.3`(decode `<`与`<`) / `≤0.3` / `≥30` / `NMT 0.5` / `不超过100`，**strip LaTeX `$...$` 残片和 `{{}}`**
- 对 measurements 矩阵每个 cell + 表格级 parameters 单值都判
- parse 不动的（如"应澄清""应符合要求"）→ 加入 LLM 兜底队列
- LLM 兜底：summary 带 spec，prompt 明确"规则无法判定的才让你判"

**不要误报**（page9 的关键验证点）：流速 0.964~0.988（spec 0.5-1.0）应全 in_spec，压力 0.15~0.17（spec <0.3）应全 in_spec——规则跑完 page9 应**零 param finding**。这是不误报的关键测试。

### Phase 3 — 测试与集成验证

- `tests/test_rules.py`：用 baseline 报告里的真实 case 构造 mock structured_json
  - page2 真实矛盾：开始 2015.01.27 vs 结束 2015.01.25 → time_reversal（真实 bug，必须抓）
  - page9 矩阵：全 in_spec，零 finding
  - 产量 1300~3200 vs 1550.32 → in_spec；收率 ≥30% vs 53.6% → in_spec
  - 签名时间 vs 操作时间异常 → signature_time_anomaly
- 端到端：重跑丝裂霉素 PDF，对比 baseline 的 findings 看 delta
- 不回归：现有 year_contradiction（改造后按事件分组）仍能抓真实跨事件矛盾

## 明确不做

- 不改 OCR client（本次需求无关）
- 不并发 Stage 2
- 不动 api/templates/frontend（finding 字段没变）
- 不加 DB 列
- 不强推 Pydantic 校验到 pipeline（schemas 只做模型对齐）

## 风险点

- **baseline 可能跑不通**：OCR client 缺重试（探索 agent 已发现）、`verify=cfg.api_url.startswith("https")` SSL 判断有 bug、Qwen3-8B 可能 JSON 输出不稳。Phase 0 若挂在这些点，报 user 决策是否顺手修（最小修复 vs 专门一轮）
- **LLM 提取 measurements 矩阵可能丢行/串列**：page9 有 11 行 4×2 指标，8B 模型提取复杂表格的能力有限。若 Phase 1 验证丢列严重，可能要换更大模型或加二次校验 prompt——到时报 user
- **OCR 时间格式串扰**（`2022/4/202205.07`）：靠规则清洗可能不可靠，部分得靠 LLM 在 per-page 层先清洗

## 推进规则

每个 Phase 完成后停下写小结 + 等 user 确认，不自动进下一阶段。Phase 0 若失败尤其要停下。

---

## 进度追踪

> 下方为各阶段实际完成情况，与上方规划对照。最新状态见 `CLAUDE.md`。

### Phase 0 — baseline 验证 ✅
- 丝裂霉素 PDF 端到端跑通，OCR/LLM/规则全链路产出 findings
- `spike/baseline_report.md` 记录每页 LLM 提取结构 + 与 spike HTML 的差异

### Phase 1 — 数据模型扩展 ✅
- `core/page_analyzer.py` prompt + JSON schema 增加 `measurements` 时间序列 + 签名字段
- `models/schemas.py` 同步 `Measurement` / `MeasurementValue` 模型
- DB schema 未动（structured_json 是 TEXT 列）

### Phase 2 — 规则层 ✅
- `core/cross_page_analyzer.py` 实现 4 类时间检查（time_reversal / signature_time_anomaly / suspicious_year / year_contradiction 按事件分组）
- 参数规格解析支持范围 / `<` / `≤` / `≥` / NMT / 中文描述 / LaTeX 残片
- 规则优先 + LLM 兜底混合策略

### Phase 3 — 测试与集成 ✅
- `tests/unit/test_cross_page_analyzer.py` / `test_page_analyzer.py` 覆盖真实 case
- E2E 9 阶段测试：upload → pipeline → findings → measurements → review actions → audit logs → reports → HTML rendering

### Phase 5 — 工程化（CORS / 打包 / frozen mode）✅
- CORS 显式限定 `127.0.0.1:8000` + `:58765`，移除 `file://`，headers 限定 `Content-Type, X-Request-ID`
- PyInstaller `pbc-server.spec` 含 hidden imports（`core.mineru_client`, `api.settings`）
- frozen mode 资源路径走 `sys._MEIPASS`，`.env` / DB / output 重定向至 `%APPDATA%/PBC/`
- Electron `electron/main.js`：spawn pbc-server.exe → health check → BrowserWindow → 退出清理子进程
- `build.ps1` 支持 `-SkipCss` / `-Clean` 选项
- Tailwind 本地构建（15.8KB），无 CDN 依赖

### Phase 6 — 生产硬化 ✅
- 结构化日志：`request_id` ContextVar + 中间件 + 4 个 handler（console / pharma.log / pipeline.log / error.log）
- API 路由业务日志（upload / cancel / retry / archive / delete / finding update / report）
- 文件上传：8MB 分块流式 + 文件名消毒 + 200MB 上限
- Settings API：`.env` 读写 + 内存 config live reload + 密钥脱敏
- 状态机 `VALID_TRANSITIONS` + `InvalidTransitionError` 阻断非法转换
- Job 生命周期：archive / unarchive / delete / stats overview
- 前端按钮 loading/disabled 防重复点击 + 翻页全局加载指示
- 测试覆盖率 90%+（unit + integration）

