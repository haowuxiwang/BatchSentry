# BatchSentry 部署与运维指南

## 便携版分发（推荐）

BatchSentry 以**文件夹便携版**形式分发，用户无需安装，解压即用。

### 构建便携版

```powershell
# 在真实 PowerShell 终端执行（非 IDE Sandbox）
cd d:\learn\claudecode\pharma-batch-checker

# 方式 1：完整构建（CSS + PyInstaller + Electron 文件夹便携版）
.\build.ps1

# 方式 2：跳过 CSS（app.css 已最新）
.\build.ps1 -SkipCSS

# 方式 3：清理后重建
.\build.ps1 -Clean
```

### 构建产物

| 产物 | 路径 | 说明 |
|------|------|------|
| Electron 应用文件夹 | `dist-electron/win-unpacked/` | 双击 `BatchSentry.exe` 运行，无需安装 |
| Python 后端 | `dist/pbc-server/pbc-server.exe` | PyInstaller 打包的后端，嵌入 win-unpacked/resources/ |
| Tailwind CSS | `static/app.css` | 压缩后的样式（~14KB） |

### 分发方式

1. 将 `dist-electron/win-unpacked/` 整个文件夹压缩成 zip
2. 用户解压后双击 `BatchSentry.exe` 即可运行

### 用户首次使用

1. 解压便携版到任意目录（如 `D:\BatchSentry\`）
2. 双击 `win-unpacked\BatchSentry.exe` 启动
3. 首次运行会显示 splash 窗口（"正在启动后端服务…"）
4. 主窗口打开后，进入**设置页面**配置 LLM API key
5. 配置完成后即可上传 PDF 开始审核

## 数据存储位置

### 便携版（开发/测试模式）

| 数据 | 位置 |
|------|------|
| 数据库 | `{项目根目录}/data/pharma.db` |
| 日志 | `{项目根目录}/logs/` |
| 上传文件 | `{项目根目录}/output/{job_id}/`（OCR 完成后自动清理） |

### 安装版（Frozen 模式）

| 数据 | 位置 |
|------|------|
| 配置 | `%APPDATA%/PBC/config.json` |
| 数据库 | `%APPDATA%/PBC/data/pharma.db` |
| 日志 | `%APPDATA%/PBC/logs/` |
| 上传文件 | `%APPDATA%/PBC/output/{job_id}/` |

## 运维指南

### 日志系统

BatchSentry 有 4 个日志文件（位于 `logs/` 目录）：

| 日志文件 | 内容 | 轮转策略 |
|----------|------|----------|
| `pharma.log` | 全部日志（INFO+） | 10MB × 5 份 |
| `pipeline.log` | 流水线相关（OCR/LLM/分析） | 10MB × 5 份 |
| `error.log` | 仅 ERROR 级别 | 5MB × 3 份 |
| 控制台 | 实时输出 | — |

日志格式：
```
2026-08-03 10:23:26 [INFO] [req=abc123 job=84f17f8f] core.pipeline: Stage 1: OCR complete
```

每个日志条目包含 `request_id` 和 `job_id`，便于关联同一请求的完整链路。

### 健康检查

```bash
# 基础健康检查（开发模式端口 8000，frozen 模式端口 58765）
curl http://127.0.0.1:58765/health
# → {"status":"ok","version":"1.0.0"}

# 下游服务连通性检查（LLM + OCR）
curl http://127.0.0.1:58765/api/health/downstream
# → {"ocr":{"ok":true,...},"llm":{"ok":true,...},"all_ok":true}
```

### 卡死任务恢复

应用重启时自动执行 `recover_stuck_jobs()`，将非终态任务（pending/ocr_running/analyzing 等）标记为 error，允许用户重试。

日志中会显示：
```
Startup recovery: 3 stuck jobs marked as error (ids: [...])
```

### 优雅关闭

Electron 退出前调用 `/api/shutdown` 端点：
1. 取消所有活跃的 pipeline task
2. 等待 2 秒让正在进行的 OCR/LLM 调用完成
3. 返回关闭确认

### 数据库维护

```bash
# 查看活跃任务数
sqlite3 data/pharma.db "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('review','error','cancelled','archived')"

# 清理已归档任务的数据
sqlite3 data/pharma.db "DELETE FROM page_cache WHERE job_id IN (SELECT id FROM jobs WHERE status='archived')"

# WAL checkpoint（压缩 WAL 文件）
sqlite3 data/pharma.db "PRAGMA wal_checkpoint(TRUNCATE)"
```

### LLM Provider 配置

配置通过设置页面管理，持久化到 `config.json`（开发模式在项目根，frozen 模式在 `%APPDATA%/PBC/config.json`）。支持运行时切换，无需重启：

```bash
# 切换 LLM provider
curl -X POST http://127.0.0.1:58765/api/settings \
  -H "Content-Type: application/json" \
  -d '{"llm_provider":"siliconflow"}'

# 切换 OCR 后端
curl -X POST http://127.0.0.1:58765/api/settings \
  -H "Content-Type: application/json" \
  -d '{"ocr_backend":"mineru","mineru_token":"sk-xxx"}'
```

### Secret 轮换流程

1. 在 LLM 服务商平台生成新 API key
2. 在设置页面更新对应的 API key 字段并保存（写入 `config.json`）
3. 调用 `/api/health/downstream` 验证新 key 连通性
4. 检查 git 历史确保旧 key 未提交：`git log -p -- config.json`（应无记录）
5. 旧版 `.env` 已弃用，若仍有残留可直接删除

## 故障排查

### 后端无法启动

```bash
# 检查端口是否被占用
netstat -ano | findstr :58765

# 查看启动日志
type logs\pharma.log | findstr "ERROR"
```

### OCR 失败

1. 检查 token 是否有效：`/api/health/downstream`
2. 检查网络连通性
3. 查看日志中的 OCR 错误详情

### LLM 调用失败

1. 检查 API key 是否有效
2. 检查 base_url 是否正确
3. 查看 `llm_call_audit` 表了解调用详情
4. LLM 有 3 次重试 + 指数退避，瞬时故障会自动恢复

### 数据库锁定

SQLite 在 WAL 模式下支持并发读写，但极端情况可能锁定：

```bash
# 检查锁状态
sqlite3 data/pharma.db "PRAGMA journal_mode"

# 强制 checkpoint
sqlite3 data/pharma.db "PRAGMA wal_checkpoint(TRUNCATE)"
```

## 安全注意事项

1. **`config.json` 文件包含 API key**，切勿提交到 git（已在 `.gitignore` 中）
2. **CSP 头**：`default-src 'self'`，禁止加载外部资源
3. **CORS**：仅允许 `127.0.0.1:8000/58765`，禁止远程访问
4. **PDF 上传**：magic bytes 校验 + 200MB 大小限制 + 路径遍历防护
5. **本地访问限制**：`/api/shutdown` 等敏感端点仅允许本地访问
6. **审计日志**：所有状态转换和人工操作均记录，不可篡改
