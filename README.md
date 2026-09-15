# BatchSentry

> GMP 批生产记录半自动合规检查系统

BatchSentry 是面向制药企业的批生产记录（BPR）审核工具，通过 OCR + LLM 半自动提取结构化数据，结合规则引擎与 LLM 语义判定，辅助 QA 人员完成 GMP 合规审查。

## 核心能力

- **多格式 PDF 解析**：PaddleOCR-VL / MinerU 双后端（主备 failover：主后端异常/0 页/缺页 >10% 且 >2 页自动切换，`ocr_backend_used` 留痕），支持扫描件、电子件、混合件；横置内容页（扫描时纸横放）自动旋转探测恢复——切片重试仍空时经投影方差几何预筛（行/列强度分布判文本朝向，横置页只探测 90/270°）+ 瞬态上游错误重试后重渲染重 OCR，采纳角度记 `ocr_diagnostics.rotation_deg` + 审计 `stage1_rotation_recovered`，探测未果页标记 `rotation_probed` 提示人工核对原图
- **第三对照引擎（可选）**：可接入 docling（MIT，本地推理，无 token）作第三 OCR 后端，用于离线对照与差异页报告（`scripts/compare_ocr_engines.py`）；**未安装即优雅降级**，不影响主链运行
- **OCR 后端能力表**：`core/pipeline/ocr_support.py` 声明各后端能力（切片/页子集/本地/凭据需求），engine/stage1/dual_compare 的字面假设全部改由能力表驱动（单一来源，防漂移）
- **上传内容去重**：流式上传时计算 MD5，相同文件二次上传返回 409 并提示已有任务（`force=1` 可绕过，用于规则变更后的合法重分析）
- **结构化提取**：LLM 提取工序步骤、参数矩阵、签名、时间、事件年份分组
- **实时进度**：SSE 流式推送任务状态（上传页行内 OCR/分析计数 + 复核页按页热更 findings）；Stage 2 逐页分析带速率与预计剩余时间（5 分钟时间窗口自适应，拥堵日跟随近期实际速率）；长阶段（OCR/逐页分析/跨页）另有**前端本地秒级计时**——不依赖 SSE 帧间隔即可看到"已用 45 秒 / 3 分 07 秒"，避免上游长调用时误判为卡死
- **三色复核分级**：复核页顶栏按**检出来源**分三档——规则命中（红，权威）/ LLM 辅助（蓝）/ 系统校验通过（绿，按问题类型统计覆盖率并可展开明细），一眼分清"机器判定"与"人工必看"
- **跨页合规分析**：规则引擎（R1-R17 及衍生规则）+ LLM fallback + LLM 语义检查三层判定；规则层已覆盖的 (page, type) 不再重复接受 LLM 语义重复报告（降噪，抑制量写审计）
  - R1 时间倒序（time_reversal，页内 + 跨页）
  - R2 年份矛盾（year_contradiction）
  - R3 参数越界（param_out_of_spec，规则无法判定时进 LLM fallback 队列）
  - R4 可疑日期（suspicious_date，如 2000 年前 / 未来年份）
  - R5 签名异常（signature_time_anomaly）
  - R6 完整性检查（completeness，缺操作/复核签名）
  - R7 批号一致性（batch_inconsistency，跨页批号漂移，critical）
  - R8 低置信度参数（completeness，标记人工复核；R8b 勾选矛盾）
  - R9 手写内容标记（handwritten，人工确认；R9a 跨角色签名顺序）
  - R10 工序缺号（step_gap，缺页/漏页检测；R-M1/R-M2 测量矩阵规则）
  - R11 物料平衡/收率可否核定（mass_balance）
  - R12 操作人=复核人（self_review，自检自核，critical）
  - R13 设备/清洁状态确认（equipment_state）
  - R14 环境监测完备性（env_monitor）
  - R15 文件版本一致性（doc_version）
  - R16 超限偏差关联（deviation_link）
  - R17 涂改规范（alteration，划改留痕）
  - 规则以注册表 `core/rules/registry.py::RULE_REGISTRY` 为唯一入口（新增只追加注册表，支持按 id 经 `config.json` 的 `rules.disabled` 关闭）
- **用户自定义合规规则**：设置页填写工厂/产品专属约束（如「XX 产品中间体储存温度必须 15-25°C」），跨页分析时注入 LLM 逐条核对，生成 `user_rule` 类型问题；变更写入审计日志，`prompt_version` 携带规则内容 hash（GMP 可追溯）
- **多 LLM 服务商**：DeepSeek / SiliconFlow（内置，可通过 config.json 动态注册更多），Anthropic 协议适配
- **GMP 审计追踪**：所有状态转换、LLM 调用、人工复核操作均写入审计日志
- **复核反馈统计**：确认/驳回率、高频驳回类型 Top5、按来源驳回率（规则阈值调优信号）——复核页折叠面板实时查看，随 md/json 报告导出（驳回率偏高的来源层即阈值过紧/提示词需收紧的量化信号）
- **多源 GMP 知识库**：GMP（2010 年修订）全文 + 附录 + NMPA 记录规范 + ALCOA+ + 21 CFR Part 11 等 **6 个来源 / 441 条**条款化语料，纯 BM25 检索（无向量库），问题自动引用具体条文依据（复核页可展开原文、导出报告附依据附录）；设置页多源浏览与检索、可按源启停
- **飞书通知**：任务完成/部分完成/失败/取消时推送飞书（webhook 群机器人或自建应用 app_bot 私聊 DM，支持事件订阅 + 90 分钟去重缓存，通知失败不阻塞流水线）
- **Electron 桌面应用**：Windows 便携版（解压即用），splash 启动、优雅关闭、卡死任务恢复

## 技术架构

> 深度架构说明（为什么这样构建、成败案例、改动指南）见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

```
┌─────────────────────────────────────────────────────┐
│                  Electron Shell                     │
│  (splash + main window + graceful shutdown)         │
└────────────────────┬────────────────────────────────┘
                     │ http://127.0.0.1:8000 (dev) / 58765 (frozen)
┌────────────────────▼────────────────────────────────┐
│              FastAPI Backend (PyInstaller)           │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │  CORS    │  │  Gzip    │  │  CSP + Security  │   │
│  │  (loop)  │  │  (≥1KB)  │  │  Headers         │   │
│  └──────────┘  └──────────┘  └──────────────────┘   │
│  ┌──────────────────────────────────────────────┐   │
│  │              Pipeline Orchestrator           │   │
│  │  Stage 1: OCR → Stage 2: LLM → Stage 3: X-Page│  │
│  │  (per-job lock + state machine + resume)     │   │
│  └──────────────────────────────────────────────┘   │
└──────┬──────────────┬──────────────┬────────────────┘
       │              │              │
┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
│  SQLite    │ │  OCR SaaS  │ │  LLM API  │
│  (WAL)     │ │  (Paddle/  │ │  (多 provider)│
│  + audit   │ │   MinerU)  │ │            │
└────────────┘ └────────────┘ └────────────┘
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+（仅构建前端 CSS 时需要）
- Windows 10+（Electron 桌面应用）

### 开发模式

```bash
# 1. 安装 Python 依赖（运行时 + 开发/测试；两个清单都是**精确锁定**版本）
pip install -r requirements.txt -r requirements-dev.txt

# 2. 构建前端 CSS（首次必须）
npx tailwindcss -i ./static/input.css -o ./static/app.css --minify

# 3. 启动后端
python server.py
# 访问 http://127.0.0.1:8000，进入设置页面配置 LLM + OCR

# 4. 启动 Electron（可选，桌面应用）
npm install
npm run dev
```

> 配置通过设置页面管理，持久化到 `config.json`（开发模式在项目根，frozen 模式在 `%APPDATA%/PBC/config.json`）。`.env` 已弃用，仅作为旧版本迁移源。

### 生产打包（便携版）

```powershell
# 必须在真实 PowerShell 终端执行（非 IDE Sandbox）
.\build.ps1              # 完整构建：CSS + PyInstaller + Electron portable
.\build.ps1 -SkipCSS     # 跳过 CSS 重建
.\build.ps1 -Clean       # 清理后重建
```

构建产物：
- `static/app.css` — 压缩后的 Tailwind CSS（~14KB）
- `dist/pbc-server/pbc-server.exe` — PyInstaller 打包的后端
- `dist-electron/win-unpacked/` — Electron 文件夹便携版（双击 `BatchSentry.exe` 运行，无需安装）

> 产物目录名不一定是 `dist-electron/`：安全软件（火绒等）占锁 `resources/app.asar`
> 时 `build.ps1` 会自愈到 `dist-electron-locked/`，手工重打包时也可能指定别的名字。
> **不要凭目录名假定要发哪一个** —— 跑体检脚本，它按"最新且完整"给出方案，并点名
> 报出被外部句柄占用、导致整个目录删不掉的文件：
>
> ```powershell
> python scripts/clean_dist.py            # dry-run，不动手
> python scripts/clean_dist.py --apply    # 按方案清理（走回收站，可恢复）
> ```
>
> 清单见 [DEPLOYMENT.md](./DEPLOYMENT.md)。
> 本版**不产出安装包**（`win.target` 仅 `dir`），交付物就是该目录压成的 zip。

详细部署与运维见 [DEPLOYMENT.md](./DEPLOYMENT.md)，开发规范见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 配置说明

配置通过设置页面管理，持久化到 `config.json`（开发模式在项目根，frozen 模式在 `%APPDATA%/PBC/config.json`）。关键项：

| 配置项 | 说明 | 默认值 |
|------|------|--------|
| `llm_provider` | 默认 LLM 服务商 | `deepseek` |
| `ocr_backend` | OCR 后端 (`paddle`/`mineru`) | `paddle` |
| `max_concurrent_jobs` | 最大并发任务数 | `3` |
| `MAX_UPLOAD_BYTES`（env） | 单文件体积上限 | `200 MB` |
| `MAX_UPLOAD_PAGES`（env） | 单文件**页数**上限（超出即拒绝，提示含真实页数与拆分建议） | `200` |
| `WARN_UPLOAD_PAGES`（env） | 页数软阈值（超出则放行但告知预估耗时） | `80` |
| `app_host` / `app_port` | 监听地址/端口 | `127.0.0.1` / `58765`（开发模式 8000） |
| `deepseek.api_key` | DeepSeek API key | — |
| `siliconflow.api_key` | SiliconFlow API key | — |
| `paddle_ocr.token` | PaddleOCR-VL token | — |
| `mineru.token` | MinerU token | — |

> `.env` 已弃用，仅作为旧版本迁移源（首次启动且 `config.json` 不存在时自动迁移）。新增 LLM 服务商通过设置页面或直接编辑 `config.json` 的 `providers` 字段添加，无需改代码。

> 上传限额（体积 / 页数）的**定标依据**（实测单页耗时、与既有 OCR 轮询上限的
> 余量、与 MinerU 厂商上限的关系）与市面 10 家产品的做法调研，见
> [docs/UPLOAD_LIMITS.md](./docs/UPLOAD_LIMITS.md)。三个限额由单一真值
> `config.UPLOAD_LIMITS` 同时驱动后端强制、前端预检与页面文案 —— 改动只应发生
> 在该处（源码扫描护栏会拦下重复写死）。

## 测试

```bash
# 全量测试（需设置环境变量避免日志文件冲突）
$env:PBC_NO_FILE_LOG='1'
python -m pytest tests/

# 打包信号门禁 —— **权威口径**：junitxml 事实源 + 覆盖率门禁
# （--python 仅在解释器不在 PATH 时才需要，且必须传 Windows 路径）
python scripts/release_gate.py --fail-under 95
```

> **为什么这里不再写 `--cov=.`**：覆盖率口径只在 `pytest.ini` 与
> `scripts/release_gate.py` 里各有一份，且**两者一致**（只统计
> `api/ core/ llm/ db/ config/ main`，门禁 95%）。命令行再叠一个 `--cov=.` 会算出
> 一个**更低的、与门禁不同的数字** —— 两套口径比没有口径更糟。
>
> 沙箱环境下 `test_main_routes.TestServePdf::test_pdf_non_local_host_returns_403`
> 因删除探针被拦截而失败，属环境产物（已登记 allowlist，非回归）。

### CI

`.github/workflows/ci.yml` 在 push / PR 到 `main` 时跑**同一个** `scripts/release_gate.py`。
runner 选 **Windows** —— 与本产品的目标平台一致（平台分支只在 Windows 上执行，
覆盖率数字才有可比性）；CI 不重复实现任何一条检查。门禁失败时会把
`devlogs/gate_report_*.json` 与原始 pytest 输出作为 artifact 带出来。

## 安全设计

- **CSP**：`default-src 'self'`，禁止外部资源加载
- **CORS**：仅允许 `127.0.0.1:8000/58765` 与 `localhost:8000/58765`（与本地访问守卫口径一致），移除 `file://`
- **XSS 防御**：Jinja2 autoescape + DOMParser 解析 OCR 文本 + JS esc 转义
- **路径遍历**：`Path(filename).name` 清洗 + `relative_to` 校验
- **PDF 校验**：magic bytes 检查 `%PDF-` 文件头
- **本地限制**：`/api/shutdown` 等敏感端点仅允许本地访问
- **审计日志**：状态转换、LLM 调用、人工复核全部记录

## 项目结构

```
├── api/                    # FastAPI 路由层
│   ├── jobs/               # 任务管理（上传、列表、状态、页面渲染、动作）
│   │   ├── upload.py       #   上传（8MB 分块、体积/页数限额、图片转 PDF）
│   │   ├── listings.py     #   任务历史列表 + 活跃快照
│   │   ├── page_image.py   #   PDF 页码 PNG 渲染
│   │   ├── status.py       #   任务状态 + SSE 进度流
│   │   └── actions.py      #   取消、重试、归档、删除
│   ├── review.py           # 复核操作（确认、驳回、纠正）
│   ├── report.py           # 报告导出
│   └── settings/           # LLM/OCR 配置管理
│       ├── read.py         #   GET（脱敏）
│       ├── write.py        #   POST 更新 + 热加载
│       ├── rules.py        #   用户合规规则
│       ├── provider.py     #   激活提供商切换
│       └── probe.py        #   下游连通性探测
├── core/                    # 业务核心
│   ├── pipeline/           # 三阶段编排 + 状态机
│   │   ├── engine.py       #   主流程引擎（launch/run_pipeline）
│   │   ├── stage1.py       #   OCR 阶段
│   │   ├── stage2.py       #   逐页 LLM 分析
│   │   ├── stage3.py       #   跨页分析落库
│   │   ├── state.py        #   状态机 + 卡死恢复
│   │   ├── locks.py        #   并发/任务注册表
│   │   ├── ocr_support.py  #   双 OCR 后端 failover
│   │   └── self_heal.py    #   空页自愈
│   ├── page_analyzer.py    # 单页 LLM 分析（v3 prompt）
│   ├── rules/              # 跨页规则引擎（原 cross_page_analyzer）
│   │   ├── base.py         #   编排 + 页面归一化
│   │   ├── registry.py     #   规则注册表（唯一入口，R1-R17 + 衍生）
│   │   ├── parsing.py      #   数值/规格/时间解析
│   │   ├── rule_time.py    #   时间倒挂、签名时间异常
│   │   ├── rule_spec.py    #   参数越限判定
│   │   ├── rule_doc.py     #   批次一致性、完整性
│   │   └── llm_checks.py   #   LLM 语义异常 + fallback
│   ├── kb/                 # GMP 知识库（多源、纯 BM25）
│   │   ├── retriever.py    #   检索（字符 bigram 倒排 + BM25）
│   │   ├── store.py        #   语料加载
│   │   └── data/           #   6 源条款化 JSON + raw/*.md 溯源
│   ├── ocr_client.py       # PaddleOCR 客户端
│   ├── mineru_client.py    # MinerU 客户端
│   ├── notify.py           # 飞书任务完成通知
│   ├── security.py         # 本地访问校验
│   ├── zh_map.py           # 中文枚举单一来源
│   └── health.py           # 健康检查（含下游探测）
├── llm/                     # LLM 适配层
│   ├── client.py           # 统一客户端（重试 + JSON 容错）
│   └── adapters/           # 协议适配器（OpenAI / Anthropic）
├── db/                      # 数据库层
│   ├── client.py           # aiosqlite + WAL + 迁移
│   └── schema.sql          # 表结构
├── templates/               # Jinja2 模板（SSR）
├── static/                  # 前端资源
│   ├── upload.js           # 上传 + 历史列表
│   ├── review.js           # 复核页（PDF 预览 + findings）
│   ├── settings.js         # 设置页
│   ├── app.css             # Tailwind 构建产物（压缩）
│   └── design-tokens.css   # 设计系统 token
├── electron/                # Electron 主进程
│   └── main.js             # splash + 健康检查 + 优雅关闭
├── tests/                   # 测试套件
│   ├── unit/               # 单元测试
│   └── integration/        # 集成测试
├── config.py                # 配置加载
├── main.py                  # FastAPI app
├── server.py               # 入口
├── pbc-server.spec          # PyInstaller spec
├── build.ps1                # 构建脚本
└── package.json             # Electron 配置
```

## License

Proprietary — Internal Use Only
