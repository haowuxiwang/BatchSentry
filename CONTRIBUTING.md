# BatchSentry 开发指南

本文档面向 BatchSentry 的开发者与维护者，描述开发约定、测试要求、构建流程与代码审查标准。部署与运维请参考 [DEPLOYMENT.md](./DEPLOYMENT.md)，架构总览请参考 [CLAUDE.md](./CLAUDE.md)。

## 开发环境

### 必备工具

| 工具 | 版本 | 用途 |
|------|------|------|
| Python | 3.11+ | 后端运行时 |
| Node.js | 18+ | 前端 CSS 构建 + Electron |
| PowerShell | 5+ | Windows 构建脚本（不要在 IDE Sandbox 中运行） |
| SQLite | 3.35+ | 数据库 CLI 维护（可选） |

### 首次配置

```powershell
# 1. Python 依赖
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, pytest-cov, httpx, pytest-timeout

# 2. Node 依赖（仅前端构建需要）
npm install

# 3. 构建前端 CSS（首次必须）
npx tailwindcss -i ./static/input.css -o ./static/app.css --minify
```

启动后通过设置页面配置 LLM + OCR，配置持久化到 `config.json`（项目根目录）。`.env` 已弃用，仅作为旧版本迁移源。

### 启动开发服务器

```powershell
# 方式 1：直接 uvicorn（带热重载，端口 8000）
uvicorn main:app --reload --host 127.0.0.1 --port 8000

# 方式 2：通过 server.py（与打包入口一致，端口 8000）
python server.py

# 方式 3：Electron 桌面壳（需先启动后端或让 Electron 自动拉起）
npm run start:dev
```

访问 http://127.0.0.1:8000 查看应用，http://127.0.0.1:8000/docs 查看 Swagger 文档。

## 代码约定

### 文件组织

- **模板/样式/脚本严格分离**：`templates/*.html`、`static/*.css`、`static/*.js`，禁止内联 CSS/JS（每个页面仅允许一个 `window.__PBC__` 数据桥接对象）
- **`static/app.css` 是 Tailwind 构建产物**，禁止手动编辑；修改 `static/input.css` 后运行 `npm run build:css` 重新构建
- **模块级 docstring 必填**：每个 `.py` 文件首行描述模块职责
- **函数 docstring 推荐填写**：复杂逻辑、对外 API、状态转换函数必须填写

### 语言约定

| 内容 | 语言 |
|------|------|
| 代码注释 | 英文 |
| Commit message | 英文 |
| UI 字符串 | 中文 |
| LLM prompt | 中文 |
| 日志消息 | 英文（带 `[job_id]` / `[req_id]` 前缀） |
| 文档 | 中文 |

### 状态机

所有 job 状态转换必须通过 `core.pipeline.transition_status()`，禁止直接 `UPDATE jobs SET status=...`。唯一例外是 `recover_stuck_jobs()` 的崩溃恢复场景。合法转换见 `VALID_TRANSITIONS`。

### LLM 调用

- **必须通过 `llm.client.get_llm_client()`**，禁止直接调用 OpenAI/Anthropic SDK
- **必须传入 `audit_ctx`**（包含 `job_id`、`page`、`stage`、`prompt_version`），写入 `llm_call_audit` 表
- **prompt 版本控制**：所有 prompt 存放在 `PROMPTS` 字典中，修改 prompt 必须升版本号（v3 → v4），保留旧版本用于回归对比
- **OCR 内容视为不可信输入**：使用 `<PBC_UNTRUSTED_OCR>` 标签隔离，防止 prompt injection

### 前端设计系统

遵循 [user_profile](./user_profile) 中的极简设计原则：

- 主色：黑/白/灰，禁止彩色装饰块
- 字体层级：h1 20px / h2 14px / body 13px / meta 11px
- 卡片圆角：`rounded-md`（8px）
- 严重性指示：右上角标签，不使用左侧边框
- 状态点：使用 `bg-destructive` / `bg-warning` 设计 token，不使用 Tailwind 默认色
- 历史记录与问题列表：扁平列表 + `border-b` 分割线，不使用卡片容器
- 严重 findings：3 秒脉冲动画 + sticky 顶部定位

## 测试要求

### 覆盖率门槛

**生产代码覆盖率必须 ≥ 90%**，由 `pytest.ini` 强制。低于此值 CI 应失败。

```powershell
# 全量测试（需禁用文件日志避免冲突）
$env:PBC_NO_FILE_LOG='1'
python -m pytest tests/ --cov=. --cov-report=term --timeout=30
```

### 测试分层

| 层级 | 目录 | 范围 | 依赖 |
|------|------|------|------|
| 单元测试 | `tests/unit/` | 纯函数、解析器、状态机 | 无外部服务 |
| 集成测试 | `tests/integration/` | API 端点、端到端流程 | Mock LLM/OCR |

**所有 LLM/OCR 调用必须 mock**，禁止在测试中调用真实服务。Mock fixtures 在 `tests/conftest.py` 中定义。

### E2E 测试要求

端到端测试覆盖 9 个阶段，每个阶段必须暴露特定 CSS 类供 Playwright/Selenium 探测：

| 阶段 | 必需 CSS 类 |
|------|------------|
| 上传 | — |
| 流水线 | `critical-banner` |
| 发现 | `findings-list-critical`、`source-badge`、`page-link` |
| 测量 | `severity-summary` |
| 复核操作 | `critical-pulse` |
| 审计日志 | — |
| 报告 | — |
| HTML 渲染 | — |

修改前端时必须保证这些 class 仍然存在。

### 新增功能时的测试清单

- [ ] 单元测试覆盖核心逻辑分支（成功/失败/边界）
- [ ] 集成测试覆盖 API 端点（含错误响应）
- [ ] 异常路径有显式测试（不能只测 happy path）
- [ ] 覆盖率不下降（运行 `--cov-fail-under=90`）
- [ ] 新增 CSS class 同步更新 E2E 探测清单

## 构建与打包

### 构建脚本

`build.ps1` 是唯一的构建入口，支持以下参数：

```powershell
.\build.ps1              # 完整构建：CSS + PyInstaller + Electron portable
.\build.ps1 -SkipCSS     # 跳过 Tailwind 重建（app.css 已最新时）
.\build.ps1 -Clean       # 清理 dist/ + dist-electron/ + build/ 后重建
```

**必须在真实 PowerShell 终端执行**（非 IDE Sandbox）——IDE Sandbox 的 AppData 写入限制会破坏 PyInstaller 与 electron-builder。

### 构建产物

| 产物 | 路径 | 说明 |
|------|------|------|
| Electron 应用文件夹 | `dist-electron/win-unpacked/` | 双击 `BatchSentry.exe` 运行，无需安装 |
| Python 后端 | `dist/pbc-server/pbc-server.exe` | PyInstaller 打包，嵌入 win-unpacked/resources/ |
| Tailwind CSS | `static/app.css` | 压缩后约 14KB |

### 分发流程

1. 在真实 PowerShell 中运行 `.\build.ps1`
2. 验证 `dist-electron/BatchSentry-Portable-1.0.0.exe` 存在且可启动
3. 将该 exe 重命名为 `.zip` 或直接分发（portable 格式本质是自解压）
4. 用户解压后双击 `BatchSentry.exe` 即可运行

### PyInstaller spec

`pbc-server.spec` 必须包含以下 hidden imports（动态导入，PyInstaller 静态分析无法发现）：

- `core.mineru_client`
- `api.settings`

新增动态导入的模块时，必须同步更新 `pbc-server.spec` 的 `hiddenimports`。

## 提交规范

### Commit Message 格式

```
<type>(<scope>): <subject>

<body>
```

| type | 含义 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `refactor` | 重构（无行为变化） |
| `test` | 测试相关 |
| `docs` | 文档 |
| `chore` | 构建/工具/依赖 |
| `security` | 安全修复 |

示例：`feat(pipeline): add per-job lock to prevent cancel+retry race`

### 提交前检查清单

- [ ] 全量测试通过：`python -m pytest tests/ --timeout=30`
- [ ] 覆盖率 ≥ 90%：`--cov-fail-under=90`
- [ ] 无 lint 错误（推荐 `ruff check .`）
- [ ] `.env` 未被提交（`.gitignore` 已忽略）
- [ ] 无调试日志残留（`print()` / `breakpoint()`）
- [ ] 新增依赖已更新到 `requirements.txt` 或 `package.json`

### 分支策略

- `main` — 生产分支，始终可发布
- `dev` — 集成分支（如有多人协作）
- `feat/*` — 功能分支
- `fix/*` — 修复分支

## 安全审查

修改以下文件时必须进行额外安全审查：

| 文件/区域 | 风险点 |
|-----------|--------|
| `api/jobs.py` 上传逻辑 | 路径遍历、文件大小、magic bytes |
| `api/settings.py` | `.env` 原子写、CSRF 防护 |
| `core/security.py` | 本地访问校验、Origin 白名单 |
| `templates/*.html` | XSS、Jinja2 autoescape |
| `static/*.js` | DOM XSS、`innerHTML` 使用 |
| `llm/client.py` | prompt injection、密钥泄漏到日志 |
| CORS 配置（`main.py`） | 远程访问、`file://` |

### 密钥管理

- **`.env` 永不提交**（已在 `.gitignore`）
- **Settings API 返回掩码 key**（`sk-abcd...wxyz`）
- **密钥轮换流程**：见 [DEPLOYMENT.md](./DEPLOYMENT.md#secret-轮换流程)
- **历史泄漏处理**：`git log -p -- .env` 确认无记录；若已泄漏，必须在服务商处吊销并轮换，仅删除文件不够（git 历史不可变）

## 故障排查

开发中遇到问题，先查看以下日志：

```powershell
# 完整日志
Get-Content logs\pharma.log -Tail 50 -Wait

# 仅流水线
Get-Content logs\pipeline.log -Tail 50 -Wait

# 仅错误
Get-Content logs\error.log -Tail 20
```

常见问题见 [DEPLOYMENT.md#故障排查](./DEPLOYMENT.md#故障排查)。
