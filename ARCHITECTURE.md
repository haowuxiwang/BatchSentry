# BatchSentry Architecture

> 为什么这个系统这么构建 — 给新接手 agent/工程师的背景说明。

## 1. 这个系统解决什么问题

制药企业 QA 需要逐页审核 GMP 批生产记录（BPR）：核对工序参数是否在
标准范围内、时间是否倒序、签名是否齐全、批号是否一致。纯人工对 50-200
页 PDF 逐页核对既慢又漏。

BatchSentry 用 OCR + LLM 半自动化：OCR 还原页面文本 → LLM 提取结构化
工序/参数 → 规则引擎（快、免费、确定性）+ LLM（语义判定）跨页检查 →
人工在复核页确认每条问题 → 导出报告。

**它是工具，不是黑箱**：所有发现问题必须人工确认；LLM 只是提取与
建议，用户确认/驳回/纠正后才进入报告。

## 2. 为什么这样构建（关键决策的来源）

### 2.1 面向中国地区环境
- OCR/LLM 全部用中国大陆可直连的 SaaS（PaddleOCR-VL、MinerU、DeepSeek、
  SiliconFlow、GLM、Kimi、Qwen、MiMo），不是 OpenAI/Google Cloud。
- GCC 审计要求 SQLite 本地存储 + 完整审计日志，数据不出机器。
- 国内网络环境不定：mineru.net 偶发超时 → 10 分钟轮询超时 + 双 OCR
  主备 failover + 空页自愈（MinerU 对 >100MB PDF 有丢页缺陷，用单页
  切片重跑恢复）。

### 2.2 单用户本地工具 → 打包成桌面应用
- 用户是非开发 QA 人员，不会开命令行/pip。所以：
  - PyInstaller 打包后端为 `pbc-server.exe`（端口 58765，config 重定向
    到 `%APPDATA%/PBC/`）。
  - Electron 壳只是"启动后端 + 打开内置浏览器窗口"，无任何 Node 业务。
  - 单文件便携版（electron-builder portable），免安装。
- PowerShell 5.1 写 UTF-8 会带 BOM → config.json 解析崩溃 → 设置页必须
  用 Python 侧 `encoding='utf-8'` 原子写（PID+UUID 临时文件 + os.replace）。

### 2.3 为什么 OCR 和 LLM 都是"链 + 降级"
- OCR：`_run_ocr_with_failover` 主备链 — 主后端异常/0 页/缺页
  >max(2, 10%) 时整体重跑备后端；缺页 ≤阈值 时继续并显式标记
  `failed_pages`（partial_review）。空页自愈：MinerU 大文件丢页缺陷，
  对 `<100 字符`（去 HTML 标签后）的页做小切片重跑（3 页批 → 单页批）。
  实际后端记录在 `jobs.ocr_backend_used`（GMP 追溯）。
- LLM：协议适配层（OpenAI/Anthropic 两协议，动态注册 provider 零代码）；
  240s 超时 + 3 次退避重试 + JSON 容错（fence/截断/前缀文本）→
  解析失败带 fix-hint 重试；单页失败标记 `_parse_error`，跨页跳过该页，
  job 仍到 partial_review（不整体失败）。

### 2.4 三阶段流水线的流式设计
- Stage 1 OCR、Stage 2 单页 LLM、Stage 3 跨页分析。
- SSE 进度：上传页行内计数 + 复核页按页热更 findings（`pages_analyzed`
  增长即刷新当前页结果），整页路径与分片路径（OCR_SLICES>1）并存。
- resume：`page_cache.structured_json` 已存在则跳过该页（重试不重复付费）。
- 状态机 `VALID_TRANSITIONS` 强制合法迁移；崩溃恢复 `recover_stuck_jobs`
  将非终态标记 error（绕过状态机但写 `stuck_recovery` 审计）。

### 2.5 安全模型（本地工具 ≠ 无安全）
- 无登录（单用户本机），但防的是"浏览器里的恶意网页"：
  `is_local_request`（Host + Origin）守卫敏感端点（settings 读写、
  上传 → CSRF 防护）、CORS 白名单与守卫口径一致、SSRF 校验（禁止
  base_url 指向回环/私网/链路本地）、上传路径清洗 + magic bytes +
  MD5 去重、LLM 文本一律前端 esc 转义（XSS）。
- CSP：只允许同源资源（`<img>` 例外放开同源），无内联脚本注入面。

## 3. 目录速览（改代码前必读）

| 路径 | 职责 | 注意事项 |
|------|------|----------|
| `config.py` | 配置加载 | **config.json > os.environ > 默认值**；设置页热更新走 `update_config` + `reset_llm_client` |
| `core/pipeline.py` | 三阶段编排 + 状态机 + failover + 空页自愈 | aiosqlite 单连接不支持并发写 — 所有 DB 写必须走 `db_lock` |
| `core/mineru_client.py` | MinerU 客户端 + content_list 解析 | `_compose_page_markdown` 页首标记是 HTML 注释；页脚"纯数字丢弃、含文字保留" |
| `core/ocr_client.py` | Paddle 客户端（阻塞 requests，pipeline 用 `asyncio.to_thread` 包裹） | 不要直接在 async 上下文调用 |
| `llm/` | 适配层 + 重试/JSON 容错/审计 | 需新 provider 时只改 `config.py` 注册，零代码 |
| `api/` | FastAPI 路由 | 敏感端点守卫 + 业务日志 `[job_id]` 前缀 |
| `db/` | aiosqlite + WAL | schema v3（jobs.md5 / findings.user_rule_id 等） |
| `templates/ static/` | Jinja2 SSR + 分离 JS/CSS | 无内联 JS（除 `window.__PBC__` 桥）；对话框用 `PBC.confirmDialog`（禁原生 alert） |
| `electron/` | 仅启停后端 + 开窗 | 不承载业务逻辑 |
| `tests/` | 单元 + 集成 | **config 是进程级单例** — 测试修改后必须还原（test_config/test_health 已互相锁定顺序无关） |

## 4. 常见坑

- **不要新增内联 `<script>`**：CSP 只放行 `__PBC__` 注入点。
- **不要在 async 里直接调 OCR 客户端**：requests 阻塞事件循环。
- **不要绕过 `transition_status`**：绕过点（如崩溃恢复）必须写审计。
- **LLM prompt 用字符串拼接**（非 `.format()`）：HTML 表格含 `{}`。
- **新增 provider 字段**：设置页保存走 `_build_env_updates` 白名单，
  需同步 `api/settings.py` 的字段映射。
- **JSON 里的敏感值**：GET /api/settings 掩码（`_mask_secrets`），
  添加新密钥字段时确认掩码规则覆盖（`cli_`/`sec-`/hex 等格式）。
- **构建产物**：`build.ps1` 必须在真实 PowerShell 运行（IDE Sandbox
  的 AppData 限制会破坏 PyInstaller）。

## 5. 测试策略

- 900+ 测试，目标覆盖率 ≥90%；`pytest.ini` 内建 `--cov=fail-under=90`。
- 外部依赖（OCR/LLM）全 mock；`tests/unit/test_ocr_clients_mock.py`
  用真实 zip 结构测 MinerU 解析降级路径。
- 改动后至少跑受影响模块 + 全量（约 2 分钟）确认覆盖率不降。