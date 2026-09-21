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

> ⚠️ **产物目录名不一定是 `dist-electron/`。** 若外部进程持有上一轮
> `win-unpacked/resources/app.asar` 的文件句柄（**本机实测＝WorkBuddy 宿主进程，
> 不是杀毒软件**），`build.ps1` 会自愈到带时间戳的备用目录
> `dist-electron-out-<时间戳>/`；手工重打包也可能显式指定别的名字（曾输出到
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
> 句柄占用**的文件点名报出来，还会用 Restart Manager **具名持有者进程**。
> 若报出占用：**完全退出该持有者进程**后重跑 `--apply`（本机实测持有者是宿主
> WorkBuddy —— 加杀毒白名单**无效**）。清理**只走回收站**，可恢复。

### 分发前检查清单

> **分发入口唯一**：只分发 `dist-electron/win-unpacked/`。带 `PROVENANCE.txt` 的
> `dist-electron-out-<时间戳>` 变体是构建自愈的产物（标准目录被外部句柄占用时
> 的落点），验证通过后用 `scripts/clean_dist.py` 归位或清理；历史版本一律打包
> zip 存 `release-archive/`，不散放目录。详见 CLAUDE.md「仓库卫生与发布纪律」。


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

### 本分发基线（v1.1.9）的证据边界

> **只看这一节，就能知道哪些结论被验证过、哪些没有。**（生成于 2026-09-18 基线冻结）

**已在**本机**实测（可作为依据）**：

- 产物结构：`dist-electron` 唯一、`scripts/clean_dist.py` 报「待清理: (无)」、状态「完整 · 1.1.9」。
- 版本四处一致：`main.APP_VERSION` / `package.json` / `package-lock.json`（顶层 + `packages[""]`）
  / `PORTABLE_README.txt`；`app.asar` 内版本**经子进程**读出 = `1.1.9`。
- **内嵌服务端实跑过**（不只是逐字节比对）：`/health` → `{"status":"ok","version":"1.1.9"}`；
  看门狗自述阈值符合不变式；跑在**隔离**数据目录上。
- 门禁 8 项全绿、覆盖率 95.24%；分发一致性机检（`test_distribution_parity.py`）通过。
- **产物与 `app.asar` 二进制内不含密钥**：对 3 把历史 `sk-` 值逐一字节搜索 + `sk-[A-Za-z0-9]{32,}`
  全量正则扫描，**0 命中**。
- **产物级端到端测试已跑通（2026-09-18，Round 42）**：用 `tests/e2e_run.py` 驱动
  `win-unpacked/resources/pbc-server/pbc-server.exe`（**正是用户双击运行时内嵌的那一份**），
  7 个轮次全部 PASS：`pdf` / `img` / `cancel` / **`real`（51 页真实批记录）** / `rot` /
  `robust`×2，driver 报 `ALL ROUNDS PASSED` 且 `exit=0`。
  real 轮 **293 findings / 14 类 / `gmp_basis` 293/293**、`ocr_backend_used=paddle`
  （**无 failover 掩盖**）、`error_message=null`、`failed_pages=null`。
  **独立核验**（不采信 driver 自述）：findings **直查隔离库**与 driver 逐条一致、
  SSE 帧数吻合（real 507 帧）、真实 `%APPDATA%/PBC` **未被写入**（mtime 未变）；
  且**内嵌 exe 与 `dist/pbc-server/pbc-server.exe` 的 sha256 完全相同**
  ⇒ 分发件 = 构建产物（不存在「测 A 发 B」）。逐项证据见 `docs/TODO.md` B0-3。

**未验证（不得当作已通过）**：

- **无他机验证**：未在干净 Windows 机器 / 新用户账户下解压运行；GUI 仅人工双击，无自动化 UI 断言。
- **「功能跑通」≠「判定正确」**：本轮已在 v1.1.9 产物上跑通 51 页真实文档全链路（见上），
  并用**视觉直读原页**核实出多条 **LLM 层假阳性** —— 日期判据方向反（`2025.01.30 晚于
  2026.09.18`）、跨行串位（把「退出循环」的时间配到「清洗罐搅拌」上）、把非数值/日期形态
  当超差（`P3='A'`、`F3='2025.01.21'`）。详见 `docs/TODO.md` **B1-4 / B1-5**。
  ⇒ 若你的用途要求**判定准确性**，请把本轮结果看作「链路可用」，而非「结论可信」。
  **Round 43 追加抽样（2026-09-20，第三轮对抗性审查）**：51 页 job 共产出 **55 条 critical**，
  本轮**抽核 5 页即坐实 4 条假 critical**（跨字段串位：把「接收体积 13.6」当成「结束时间 13:6」；
  布尔项勾选读反：原件全勾「是」却判「否」；凭空数字 `18222`；日期方向反），并发现
  **同一基准值（生产日期）在同一次运行内被各页各解**（`2025.01.20` vs `2026-09-18`）。
  ⚠️ 这是**抽样**，**不可外推为整体假阳性率** —— 但足以说明交付说明里的
  「须人工复核」不是免责套话。详见 `docs/ADVERSARIAL_AUDIT.md` §15.3。
- **上游凭据与真机连通性未验**：本机 LLM key 可用性、Anthropic **厂商真机**（仅有本地协议桩）。
- **无真实标注集** ⇒ 精度/召回**没有可信数字**（`docs/FINDING_GROUND_TRUTH.json` 是合成件，不可外推）。
- **200 页上传上限是线性外推**，未实跑。

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

关闭窗口时 Electron 走这条链（`electron/main.js::gracefulShutdown`）：

1. `POST /api/shutdown` —— 后端取消所有活跃 pipeline task（写 error 状态 +
   audit_log），**并在响应写回后请求自己的 uvicorn 优雅关停**；
2. 按**端口释放**等待后端自行退出（上限 6s）。“优雅”的判据是后端跑完
   lifespan 收尾（即 `close_db()`，日志里会出现 `Shutdown complete.`），
   而不是“我们发过什么信号”；
3. 超时未退 ⇒ `taskkill /pid <pid> /T /F` 强杀（**本实例子进程与复用孤儿后端
   一视同仁**），再等端口释放；仍不放就明确报错。

响应体里的 `exit_requested` 表示后端是否确认拥有该通道：

```json
{"status": "shutting_down", "cancelled_tasks": 0, "exit_requested": true}
```

`exit_requested=false` 表示后端没有绑定退出通道（例如把它当库导入、或换了旧版本
后端），此时 Electron 会打一条 warn 并依赖强杀兜底 —— **“没有通道”绝不等于
“已经优雅退出了”**。

为什么要这么绕（都是实测踩出来的，`devlogs/_verify/probe_shutdown_semantics.py`）：

| 旧写法 | 实际后果 |
|---|---|
| 只发 `/api/shutdown`（端点当时不触发退出） | 进程根本不退，端口与 DB 句柄一直占着 |
| 靠 `kill('SIGTERM')` 收尾 | Windows 上等价于 TerminateProcess，**不执行任何 Python 代码** ⇒ `close_db()` 从不运行、SQLite `-wal`/`-shm` 残留（实测 494 KB） |
| 兜底条件 `!pythonProcess.killed` | Node 的 `killed` 在**信号发出时**即置真（语义是“已发送信号”）⇒ 条件恒假 ⇒ `taskkill` 兜底是**死代码**；复用孤儿路径（`pythonProcess` 为 null）更是整体跳过终止 |

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
   python scripts/check_leaked_keys.py    # ① 历史曾入库的疑似密钥 ② 当前生效的密钥
                                         # ③ 两者交集 = 必须立即轮换（退出码 2）
   git grep -nE "sk-[A-Za-z0-9]{32,}"      # 工作树扫描
   ```

   两条命令的**判定模式是同一个**（`sk-[A-Za-z0-9]{32,}`），且由
   `tests/unit/test_no_committed_secrets.py` 固化："工作树出现即测试失败"、
   该模式与本节命令**双向绑定**（`test_documented_scan_command_matches_this_guard`，
   防文档与护栏各自漂移）、以及阳性对照防"护栏空转"。覆盖范围含**仓库根目录脚本**
   —— 那正是本事故的发生地。

   `config.json` 已被 `.gitignore`，但**测试脚本**一度把真实 key 写死并入库
   （2026-09-15 发现于 `tests/e2e_frozen.py` / `e2e_manual.py` / `e2e_quick.py`，
   自 `81964a3` 起在历史中）。**删除文件不能抹掉历史，只能到服务商处作废重发。**

   **暴露面登记（2026-09-18 复验，`scripts/check_leaked_keys.py` ①）**：
   ⚠️ **本仓库是 public**（`haowuxiwang/BatchSentry`）⇒ 历史里的 key 等同于**已经公开**。
   历史中曾入库的 `sk-` 形态值共 **3** 把（只记前缀与 sha256 前 8 位，不落全文）：

   | 代号 | 历史出处 | 前缀 / sha8 | 至今仍是**生效值**？ |
   |---|---|---|---|
   | K1 | `tests/e2e_frozen.py`·`e2e_manual.py`·`e2e_quick.py`，自 `81964a3` | `sk-vpr` / `5d855143` | 🔴 **是**（开发模式的 `config.json`，见下） |
   | K2 | `tests/unit/test_config.py`（早期 5 个提交） | `sk-TuK` / `838c00a7` | 否 |
   | K3 | `spike_test.py`（`8651e3e` 初始骨架） | `sk-evn` / `2608ecd2` | 否 |

   ⚠️ **"生效"取决于运行模式**（`config.py:_config_path()`）：
   **开发模式读仓库根 `config.json`**（K1 仍在此文件，8-31 起未改）、
   **冻结/安装版读 `%APPDATA%\PBC\config.json`**（该文件里的值已不是 K1）。
   ⇒ **K1 对开发模式与任何走仓库配置的 e2e 仍是有效凭据，且已公开暴露，必须作废重发。**
   ⚠️ 顺带修正一处配置卫生问题：该文件里 `DEEPSEEK_API_KEY` 与 `SILICONFLOW_API_KEY`
   **同值**（都是 K1）—— 两家厂商共用一把 key，轮换时应分别申请。

   ⚠️ **本工具已知盲区**（对抗审查 Round 41 发现）：`scripts/check_leaked_keys.py:117`
   只读 `repo/config.json`，**不读 `%APPDATA%\PBC\config.json`** ⇒ 在只按装版使用的
   机器上它会把"生效值"判成"已不存在"，从而**漏报**。自查时两条路径都要看。
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
