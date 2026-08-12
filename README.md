# BatchSentry

> GMP 批生产记录半自动合规检查系统

BatchSentry 是面向制药企业的批生产记录（BPR）审核工具，通过 OCR + LLM 半自动提取结构化数据，结合规则引擎与 LLM 语义判定，辅助 QA 人员完成 GMP 合规审查。

## 核心能力

- **多格式 PDF 解析**：PaddleOCR-VL / MinerU 双后端（主备 failover：主后端异常/0 页/缺页>20% 自动切换，`ocr_backend_used` 留痕），支持扫描件、电子件、混合件
- **结构化提取**：LLM 提取工序步骤、参数矩阵、签名、时间、事件年份分组
- **实时进度**：SSE 流式推送任务状态（上传页行内 OCR/分析计数 + 复核页按页热更 findings）
- **跨页合规分析**：规则引擎（R1-R8）+ LLM fallback + LLM 语义检查三层判定
  - R1 时间倒序（time_reversal，页内 + 跨页，critical）
  - R2 年份矛盾（year_contradiction）
  - R3 参数越界（param_out_of_spec，规则无法判定时进 LLM fallback 队列）
  - R4 可疑日期（suspicious_date，如 2000 年前 / 未来年份）
  - R5 签名异常（signature_time_anomaly）
  - R6 完整性检查（completeness，缺操作/复核签名）
  - R7 批号一致性（batch_consistency，跨页批号漂移）
  - R8 低置信度参数（low_confidence，标记人工复核）
- **多 LLM 服务商**：DeepSeek / SiliconFlow（内置，可通过 config.json 动态注册更多），Anthropic 协议适配
- **GMP 审计追踪**：所有状态转换、LLM 调用、人工复核操作均写入审计日志
- **Electron 桌面应用**：Windows 便携版（解压即用），splash 启动、优雅关闭、卡死任务恢复

## 技术架构

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
# 1. 安装 Python 依赖
pip install -r requirements.txt

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
.\build.ps1 -SkipCss    # 跳过 CSS 重建
.\build.ps1 -Clean      # 清理后重建
```

构建产物：
- `static/app.css` — 压缩后的 Tailwind CSS（~14KB）
- `dist/pbc-server/pbc-server.exe` — PyInstaller 打包的后端
- `dist-electron/win-unpacked/` — Electron 文件夹便携版（双击 `BatchSentry.exe` 运行，无需安装）

详细部署与运维见 [DEPLOYMENT.md](./DEPLOYMENT.md)，开发规范见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 配置说明

配置通过设置页面管理，持久化到 `config.json`（开发模式在项目根，frozen 模式在 `%APPDATA%/PBC/config.json`）。关键项：

| 配置项 | 说明 | 默认值 |
|------|------|--------|
| `llm_provider` | 默认 LLM 服务商 | `deepseek` |
| `ocr_backend` | OCR 后端 (`paddle`/`mineru`) | `paddle` |
| `max_concurrent_jobs` | 最大并发任务数 | `3` |
| `app_host` / `app_port` | 监听地址/端口 | `127.0.0.1` / `58765`（开发模式 8000） |
| `deepseek.api_key` | DeepSeek API key | — |
| `siliconflow.api_key` | SiliconFlow API key | — |
| `paddle_ocr.token` | PaddleOCR-VL token | — |
| `mineru.token` | MinerU token | — |

> `.env` 已弃用，仅作为旧版本迁移源（首次启动且 `config.json` 不存在时自动迁移）。新增 LLM 服务商通过设置页面或直接编辑 `config.json` 的 `providers` 字段添加，无需改代码。

## 测试

```bash
# 全量测试（需设置环境变量避免日志文件冲突）
$env:PBC_NO_FILE_LOG='1'
python -m pytest tests/ --cov=. --cov-report=term --timeout=30

# 当前状态：710 passed, 94.30% coverage（目标 ≥90%）
```

## 安全设计

- **CSP**：`default-src 'self'`，禁止外部资源加载
- **CORS**：仅允许 `127.0.0.1:8000/58765`，移除 `file://`
- **XSS 防御**：Jinja2 autoescape + DOMParser 解析 OCR 文本 + JS esc 转义
- **路径遍历**：`Path(filename).name` 清洗 + `relative_to` 校验
- **PDF 校验**：magic bytes 检查 `%PDF-` 文件头
- **本地限制**：`/api/shutdown` 等敏感端点仅允许本地访问
- **审计日志**：状态转换、LLM 调用、人工复核全部记录

## 项目结构

```
├── api/                    # FastAPI 路由层
│   ├── jobs.py             # 任务管理（上传、取消、重试、归档）
│   ├── review.py           # 复核操作（确认、驳回、纠正）
│   ├── report.py           # 报告导出
│   └── settings.py         # LLM/OCR 配置管理
├── core/                    # 业务核心
│   ├── pipeline.py         # 三阶段编排 + 状态机
│   ├── page_analyzer.py    # 单页 LLM 分析（v3 prompt）
│   ├── cross_page_analyzer.py  # 跨页规则 + LLM fallback
│   ├── ocr_client.py       # PaddleOCR 客户端
│   ├── mineru_client.py    # MinerU 客户端
│   ├── security.py         # 本地访问校验
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
