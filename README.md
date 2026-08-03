# BatchSentry

> GMP 批生产记录半自动合规检查系统

BatchSentry 是面向制药企业的批生产记录（BPR）审核工具，通过 OCR + LLM 半自动提取结构化数据，结合规则引擎与 LLM 语义判定，辅助 QA 人员完成 GMP 合规审查。

## 核心能力

- **多格式 PDF 解析**：PaddleOCR-VL / MinerU 双后端，支持扫描件、电子件、混合件
- **结构化提取**：LLM 提取工序步骤、参数矩阵、签名、时间、事件年份分组
- **跨页合规分析**：规则引擎（R1-R5）+ LLM fallback 双层判定
  - R1 时间倒序（time_reversal，critical）
  - R2 年份矛盾（year_contradiction）
  - R3 参数越界（param_out_of_spec）
  - R4 签名异常（signature_time_anomaly）
  - R5 完整性检查（completeness）
- **多 LLM 服务商**：DeepSeek / SiliconFlow / GLM / Kimi / Qwen / Anthropic，动态注册，无硬编码
- **GMP 审计追踪**：所有状态转换、LLM 调用、人工复核操作均写入审计日志
- **Electron 桌面应用**：Windows 便携版（解压即用），splash 启动、优雅关闭、卡死任务恢复

## 技术架构

```
┌─────────────────────────────────────────────────────┐
│                  Electron Shell                     │
│  (splash + main window + graceful shutdown)         │
└────────────────────┬────────────────────────────────┘
                     │ http://127.0.0.1:58765
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

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入至少一个 LLM API key

# 3. 构建前端 CSS（可选，首次需要）
npx tailwindcss -i ./static/input.css -o ./static/app.css --minify

# 4. 启动后端
python server.py
# 访问 http://127.0.0.1:58765

# 5. 启动 Electron（可选，桌面应用）
npm install
npm run dev
```

### 生产打包（便携版）

```powershell
# 必须在真实 PowerShell 终端执行（非 IDE Sandbox）
.\build.ps1              # 完整构建：CSS + PyInstaller + Electron portable
.\build.ps1 -SkipCss    # 跳过 CSS 重建
.\build.ps1 -Clean      # 清理后重建
```

构建产物：
- `static/app.css` — 压缩后的 Tailwind CSS（~15KB）
- `dist/pbc-server/pbc-server.exe` — PyInstaller 打包的后端
- `dist-electron/BatchSentry-Portable-1.0.0.exe` — Electron 便携版（单文件，解压即用）

详细部署与运维见 [DEPLOYMENT.md](./DEPLOYMENT.md)，开发规范见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 配置说明

所有配置通过环境变量或 `.env` 文件管理，关键项：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | 默认 LLM 服务商 | `deepseek` |
| `OCR_BACKEND` | OCR 后端 (`paddle`/`mineru`) | `paddle` |
| `MAX_CONCURRENT_JOBS` | 最大并发任务数 | `3` |
| `APP_HOST` | 监听地址 | `127.0.0.1` |
| `APP_PORT` | 监听端口 | `58765` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | — |
| `GLM_API_KEY` | 智谱 GLM API key | — |
| `KIMI_API_KEY` | Moonshot Kimi API key | — |
| `QWEN_API_KEY` | 通义千问 API key | — |
| `PADDLE_OCR_TOKEN` | PaddleOCR-VL token | — |
| `MINERU_TOKEN` | MinerU token | — |

新增 LLM 服务商：在 `.env` 中添加 `<NAME>_API_KEY` + `<NAME>_BASE_URL` + `<NAME>_MODEL`，无需改代码。

## 测试

```bash
# 全量测试（需设置环境变量避免日志文件冲突）
$env:PBC_NO_FILE_LOG='1'
python -m pytest tests/ --cov=. --cov-report=term --timeout=30

# 当前状态：458 passed, 95% coverage
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
