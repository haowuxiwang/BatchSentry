# BatchSentry 部署与运维指南

## 便携版分发（推荐）

BatchSentry 以**文件夹便携版**形式分发，用户无需安装，解压即用。

> **当前分发形态：仅免安装目录，不产出安装包。**
> `package.json` 里的 `nsis` 配置块是**保留的调参**，但 `build.win.target` 只有
> `dir`，故 electron-builder **不会**生成 `Setup.exe`（不会下载 NSIS 工具链，也不需要
> 代码签名证书）。请勿指导用户寻找安装包。若要改产安装包，把 `win.target` 改成
> `[{"target":"nsis","arch":["x64"]}]` 即可启用那套现成配置 —— 但需一并准备
> 代码签名（否则首次运行会被 SmartScreen 拦）并补做安装包的端到端验证。
>
> 该口径由 `tests/unit/test_distribution_parity.py` 机检守住（配置 ↔ 文档 ↔ 实物）。

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

> ⚠️ **产物目录名不一定是 `dist-electron/`。** 若安全软件（火绒等）持有上一轮
> `win-unpacked/resources/app.asar` 的文件句柄，`build.ps1` 会自愈到备用目录
> `dist-electron-locked/`；手工重打包也可能显式指定别的名字（曾输出到
> `dist-electron-v112/`）。
>
> 这是仓库里积出**多个 `dist*` 目录**的根因，不是构建逻辑的缺陷：只要那个句柄
> 还在，`app.asar` 就既不能改名也不能删除，**连它所在的整个目录都无法重命名/
> 删除**（NTFS 拒绝重命名含被占用子项的目录）。2026-09-15 实测：某目录 1880 个
> 文件中恰好 4 个被占 —— 全部是 `app.asar`。
>
> 因此**不要凭目录名假定要怎么发**，用体检脚本：
>
> ```powershell
> python scripts/clean_dist.py            # dry-run：体检 + 出方案，不动手
> python scripts/clean_dist.py --apply    # 按方案清理（走回收站，可恢复）
> ```
>
> 它按"最新且完整"判定该保留哪一个（完整 = 含 `BatchSentry.exe`、
> `resources/app.asar`、`resources/pbc-server/pbc-server.exe`），并把**被外部
> 句柄占用**的文件点名报出来。若报出占用：把本仓库目录加入安全软件的信任区/
> 白名单（或临时退出安全软件），再重跑 `--apply`。清理**只走回收站**，可恢复。

### 分发前检查清单

```powershell
# 0. CSS 是否为最新（改动过 templates/ 或 static/*.js 就必须查）
#    判法：重跑编译，git diff 必须为空 —— 若输出有差异，说明 app.css 曾过期，
#    必须提交新 CSS 并**重新打包**（app.css 会随 pbc-server.exe 一起冻结进包，
#    只改源码不重打包 = 用户看到的仍是旧样式。历史上曾因此少 2 个 Tailwind 类。）
npm run build:css
git diff --exit-code -- static/app.css

# 1. 产物体检：确认只剩一个待发目录，且它是"最新且完整"的那个
python scripts/clean_dist.py

# 2. 门禁（覆盖率 + 单测 + 集成）
python scripts/release_gate.py --python "<python.exe 的 Windows 路径>" --fail-under 95

# 3. 分发一致性机检（配置↔文档↔实物：内嵌服务端逐字节一致 + 包内版本号）
python -m pytest tests/unit/test_distribution_parity.py -o addopts="" -q

# 4. 对**将要分发的那份**做端到端（把 PBC_E2E_EXE 指向 win-unpacked 内嵌副本）
$env:PBC_E2E_EXE = "dist-electron\win-unpacked\resources\pbc-server\pbc-server.exe"
python tests/e2e_frozen.py

# 5. 核对版本（服务端 /health 与包内 package.json 必须都等于 main.APP_VERSION）
Remove-Item Env:\PBC_E2E_EXE
```

第 4 步的意义：默认 `tests/e2e_frozen.py` 测的是 `dist/pbc-server/`（PyInstaller 的
直接产物），而**用户双击运行的是 Electron 包里内嵌的那一份**。不显式指过去，就等于
"测了 A、发了 B"。第 3 步的逐字节比对是这条链路的兜底。

> ⚠️ **真实文档轮次要确认"实际用的是哪个 OCR 引擎"。** 主后端提交失败会自动
> failover 到备选后端，而终态、findings、SSE 全都照常 —— 只有
> `jobs.ocr_backend_used` 能揭穿。`e2e_run.py` 的每轮都已声明期望后端，不一致直接
> 判 FAIL 并打印 `BACKEND MISMATCH`；报告里的 `ocr_backend_used` 才是真值。
> 2026-09-15 实测：Paddle 上游返回 `10010 任务提交队列已满`，51 页真实文档整轮跑的
> 其实是 MinerU，而报告里写着 paddle —— 这类结论**不得**作为 Paddle 路径的证据。

### 分发方式

1. 先跑 `python scripts/clean_dist.py` 确认只剩一个完整产物（`dist-electron/`），
   再把它的 `win-unpacked/` 整个文件夹压缩成 zip
2. 把 `PORTABLE_README.txt` 放到 zip 根目录一并交付（它是用户解压后第一份会读的文档；
   **它不在 Electron 包内**，`build.files` 只打包 `electron/main.js`，所以必须手工附带）
3. 用户解压后双击 `BatchSentry.exe` 即可运行
4. 首次运行可能被 Windows SmartScreen 或杀软拦下（**未做代码签名**，属预期）：
   提示用户选"仍要运行"，或把解压目录加入杀软白名单

### 用户首次使用

1. 解压便携版到任意目录（如 `D:\BatchSentry\`）
2. 双击 `win-unpacked\BatchSentry.exe` 启动
3. 首次运行会显示 splash 窗口（"正在启动后端服务…"）
4. 主窗口打开后，进入**设置页面**配置 LLM API key
5. 配置完成后即可上传 PDF 开始审核

### 启动失败排障（backend-boot.log）

Electron 主进程把后端的 spawn/stdout/stderr/exit 与每次健康检查的 lastError 追加写入：

```
%APPDATA%/PBC/logs/backend-boot.log
```

- 启动等待为截止时间制（180s）：只要后端进程存活就继续等（splash 每 10s 显示进度与当前原因）；进程退出立即失败
- 报错信息包含最后一次检查的真实错误（如 ECONNREFUSED / request timeout）
- 冷启动偶发 >30s 属正常（Windows Defender 对新构建 exe 的深度扫描实测可达 145s）；若频繁超时可将安装目录加入杀软白名单
- 日志中的 `[boot] server.py entering ...` 行用于切分"引导阶段"与"导入/绑定阶段"

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

### 上传限额调整（按客户文档规模）

体积与页数限额由**单一真值** `config.UPLOAD_LIMITS` 驱动，可用环境变量覆盖
（非法值会被收敛到自洽区间并告警，不会导致启动失败）：

```powershell
$env:MAX_UPLOAD_BYTES  = "524288000"   # 单文件体积上限（字节），默认 200 MB
$env:MAX_UPLOAD_PAGES  = "300"         # 页数硬上限，默认 200
$env:WARN_UPLOAD_PAGES = "120"         # 页数软阈值（放行但告知预估耗时），默认 80
```

⚠️ **不要盲目调高页数**：上限的定标依据是实测单页耗时（OCR 9.0 s/页 +
逐页 LLM 22.8 s/页）与既有 `core/ocr_client.POLL_TIMEOUT_MAX = 3600s` 的余量。
调高前请先读 [docs/UPLOAD_LIMITS.md](./docs/UPLOAD_LIMITS.md) —— 特别是
"页数数字不可跨产品照抄"这一条。另外 `MAX_UPLOAD_BYTES` 不得超过 MinerU 通道
的厂商上限（`core/mineru_client.MINERU_MAX_UPLOAD_BYTES`），否则文件会在
failover 到 MinerU 时**流程中途失败**（护栏
`test_policy_limit_does_not_exceed_mineru_vendor_limit` 会拦下）。

### Secret 轮换流程

1. 在 LLM 服务商平台生成新 API key
2. 在设置页面更新对应的 API key 字段并保存（写入 `config.json`）
3. 调用 `/api/health/downstream` 验证新 key 连通性
4. **全仓库排查旧 key 是否落入版本控制**（不只 `config.json`）：
   ```bash
   git log -p -S "<key 前 8 位>" --oneline   # 任何历史命中都必须轮换，删文件无效
   git grep -nE "sk-[A-Za-z0-9]{32,}"        # 工作树扫描（有测试固化此规则）
   ```
   注意 `config.json` 已被 `.gitignore`，但**测试脚本**一度把真实 key 写死并入库
   （2026-09-15 发现于 `tests/e2e_frozen.py` / `e2e_manual.py` / `e2e_quick.py`，
   自 `81964a3` 起在历史中）。**删除文件不能抹掉历史，只能轮换。**
5. e2e 需要真实 key 时一律走环境变量，不要写进代码：
   ```bash
   PBC_E2E_DEEPSEEK_KEY=<key> python tests/e2e_frozen.py
   ```
   未设置时 e2e 会把 LLM 步骤如实降级并打印提示，不会伪造通过。
6. 旧版 `.env` 已弃用，若仍有残留可直接删除

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
