# BatchSentry Web 版（飞书入口）范围决策文档

> 状态：**已决策暂缓实施（2026-08-18）**。调研已完成，D1~D5 决策点已拍板；
> 项目当前优先桌面端（Electron）功能的打磨与后续迭代，Web 版（飞书入口）延后，
> 本轮不实施。本文档作为范围决策记录留存——后续启动时按 §6 实施路线推进即可，
> 决策结论无需重新讨论。

---

## 1. 目标与范围

### 1.1 范围（本次要做）

| 项 | 说明 |
|---|---|
| **保留 Electron 桌面版** | 现有 `electron/` + `build.ps1` + `pbc-server` PyInstaller 打包链路**完全不动**，作为本地/单机使用形态继续维护 |
| **新增 Web 版（飞书网页应用）** | 前后端同一套代码，部署到公网 HTTPS；在飞书工作台添加应用入口，员工点开即用（免登录） |
| **功能一致性** | Web 版功能与桌面版一致：上传/OCR/LLM 分析/复核/报告导出/设置/审计 |
| **端覆盖** | 飞书桌面客户端 + 移动客户端（webview 内打开），以及浏览器直访 |
| **文档** | 本文档 + 部署文档更新（DEPLOYMENT.md） |

### 1.2 非目标（明确不做）

- 不做飞书小程序（见选型分析）
- 不改 Electron 桌面端行为与打包产物
- 不做账号体系自建（登录身份完全依赖飞书免登）
- 不在 Phase A 做 OCR/LLM 凭证按用户隔离（凭证是全局共享的服务配置）

---

## 2. 形态选型：飞书「网页应用」✅

飞书开放平台提供三种应用形态：**机器人 / 网页应用 / 小程序/小组件**。

| 形态 | 开发成本 | 前端复用 | 端覆盖 | 结论 |
|---|---|---|---|---|
| **网页应用（H5 嵌入工作台）** | 低 | 100% 复用现有 Jinja2+JS+CSS | 飞书桌面端 + 移动端 webview；浏览器可直访 | ✅ **采用** |
| 小程序 | 高 | 需用字节小程序框架重写（Taro 等），Tailwind/vanilla JS 全废 | 飞书内 | ❌ 违背"快速支持" |
| 机器人 | — | 只能发消息/卡片，无法承载复核 UI | — | ❌ 不适用 |

**网页应用的官方定位正是"将已有 H5 系统接入飞书工作台，员工一键打开、免登录"**
（公开文档《网页应用概述》《将已有网页应用嵌入飞书工作台》），迁移成本 = 配置 +
少量开发。且网页应用通过公网 URL 承载，**改前端/后端即动态生效，无需在飞书侧发版**。

### 2.1 免登流程（飞书侧能力，二次开发量小）

```
用户在飞书工作台点开应用
        │
        ▼
网页加载 → 引入 JSSDK（官方 CDN js-sdk）
        │
        ▼
tt.requestAccess()（可选前置 tt.requestAuthCode）拿到一次性 code
        │
        ▼
后端 POST open.feishu.cn/open-apis/authen/v1/access_token
        │  (app_access_token 作 Authorization + code)
        ▼
返回 user_access_token + 用户信息（open_id / name / avatar 等）
        │
        ▼
后端签发自家会话（httponly cookie）→ 后续 API 全部走会话
```

要点：
- code 有效期 5 分钟、一次性；user_access_token 约 2 小时，可 refresh。
- 生产配置：应用"安全设置"填重定向/安全域名；**网页应用主页 URL 必须是公网
  HTTPS 地址**。
- 无需申请额外权限范围（免登只需 `auth:user.id:read` 内置范围）即可拿到
  open_id/user_id，用于 Web 版登录态。

### 2.2 部署前置条件（外部依赖，需用户确认）

| 前置 | 说明 |
|---|---|
| 公网服务器 | 云主机（阿里云/腾讯云等），能跑 Python 3.11 + uvicorn |
| 域名 + ICP 备案 | 中国大陆服务器必须备案域名（HTTPS 证书签发也依赖域名） |
| HTTPS 证书 | 建议 Let's Encrypt 免费证书或云厂商免费证书 |
| 飞书企业管理员 | 创建企业自建应用需要企业管理员/开发者权限；发布需管理员审核 |
| Nginx | 反向代理 + client_max_body_size 200m（PDF 上传上限） |

---

## 3. 现状代码差距（Web 化改动点清单）

现有代码是"本地单用户威胁模型"，Web 化是**安全模型升级**，不是纯部署。逐项：

| # | 差距 | 现状位置 | Web 化影响 |
|---|---|---|---|
| 1 | **鉴权缺失（最大项）**：所有写操作/设置靠 `is_local_request`（Host=localhost）守卫 | `core/security.py:129`，被 `api/settings.py`（7 处）、`api/jobs.py`（5 处）、`api/review.py`、`main.py`（2 处）引用 | 公网下 Host 非 localhost → 全部 403。需引入**部署模式**（`local` / `web`）：local 模式维持现状；web 模式改为会话中间件（httponly cookie + 飞书身份），守卫语义从"本机请求"变为"已登录用户" |
| 2 | CORS 硬编码 127.0.0.1 | `main.py:93-104` | 改为按部署模式/配置读取（同源部署下 CORS 退化为恒放行同源，仍需保留配置项） |
| 3 | 飞书免登接口缺失 | 无 | 新增 `api/auth.py`：`GET /api/auth/status`（探查是否已登录）+ `POST /api/auth/feishu`（code 换 token + 签发会话）+ 登出；会话存内存/SQLite |
| 4 | 绑定地址 | `server.py`（58765，Electron 专用，不动）；dev 用 `main.py` uvicorn 8000 | Web 部署用 uvicorn `main:app --host 0.0.0.0 --port 8000` + Nginx 反代；`config["app"].host` 已支持配置 |
| 5 | 移动端响应式 | `templates/review.html` 3 列 grid（`review-grid`），upload/settings 基本可宽；viewport meta 已齐 | review 页需断点改造：小屏折叠为单列（页导航 → PDF → findings 纵向堆叠），必要交互改触屏友好（左滑/按钮）。upload/settings 窄屏检查 |
| 6 | 上传限制 | 8MB 分块、200MB 上限（应用层） | 200MB 上限在应用层已存在；Nginx 层需 `client_max_body_size 200m` 配套 |
| 7 | 多用户数据模型 | `jobs`/`page_cache`/`findings`/`audit_log` 无 owner 字段 | **决策点 D1**（见 §5） |
| 8 | Settings 页权限 | 任何本机请求可改 LLM 凭证 | **决策点 D2**（web 版下建议仅管理员可改） |
| 9 | 通知 | Phase 12 飞书 job 完成通知已实现 | Web 版直接受益（任务在服务器后台跑，用户端无需常开） |
| 10 | CSP | `connect-src 'self'`；同源部署 OK | 需要容纳飞书 JSSDK 引入（官方 CDN 域名加入 script-src 白名单）；`frame-ancestors 'self'` 保持 |

### 3.1 利好（现有架构的复用面）

- 前后端分离（Jinja2 模板 + static JS/CSS 分离），无 SPA 构建链，直接挂公网即可跑
- Tailwind 本地构建产物（无 CDN 运行时依赖）
- `config.py` 已有 JSON 配置 + live reload，新增 `auth_mode` 等键即插即用
- 状态机/审计/CSRF 守卫已成型，Web 化主要是把守卫的"身份来源"换掉
- OCR/LLM 全部走公网 API，不依赖内网设施 → 服务器部署无额外依赖

---

## 4. 目标架构

```
飞书工作台（桌面端 / 移动端 webview）           浏览器直访
        │                                          │
        └──────────── HTTPS ───────────────────────┘
                          │
                 Nginx (443, TLS 终止)
                 ├─ client_max_body_size 200m
                 └─ proxy_pass http://127.0.0.1:8000
                          │
                 uvicorn (main:app, 单 worker)
                 ├─ FeishuAuthMiddleware（web 模式）
                 ├─ 状态机 pipeline（BackgroundTask）
                 ├─ SQLite (WAL) —— 与桌面版同一 schema
                 └─ output/ + logs/ + config.json
                          │
                 ┌────────┴────────┐
           公网 OCR API    LLM API（DeepSeek 等）
```

- **同源部署**：页面与 API 同域名 → CSP 无需放开 API 域、CORS 只在跨源调试时需要
- 桌面版（Electron + 58765）独立并存，代码同源、模式切换
- SQLite 单文件仍适用（并发写由 `db_lock` 串行保护；团队规模 <10 人无压力）

---

## 5. 决策点（实施前需用户拍板）

> **已确认（2026-08-18）**：D1~D4 均选推荐项；D5 默认 A（通知复用 Phase 12 已有
> 飞书通知配置，不新增机器人形态）。实施按此结论推进，变更需重开讨论。

| # | 决策点 | 选项 | 影响 |
|---|---|---|---|
| **D1** | Web 版多用户数据如何组织 | **A. 共享队列** ✅（所有人看到同一批 job，谁都能复核/导出——适合团队共用）<br>B. 按用户隔离（jobs 加 owner，每人只见自己的） | A 无需改 schema，最快落地；B 需 schema v4 + 全链路过滤 + 复核冲突处理 |
| **D2** | Settings 页（LLM/OCR 凭证）权限 | A. 全员可改（维持现状语义）<br>**B. 仅管理员** ✅（按飞书 open_id 白名单配置） | 凭证是全局共享的，B 更稳妥但多一项配置 |
| **D3** | Web 版部署边界 | **A. 先内测** ✅（开发服务器 + ngrok/内网穿透，不开公网/免备案）<br>B. 直接公网上线（域名+备案+HTTPS） | A 无备案成本、可先行联调飞书免登；B 是最终形态 |
| **D4** | 移动端适配深度 | **A. review 页移动端单列折叠** ✅（核心）、列表/设置触屏化<br>B. 完整重排（含 PDF 预览手势、旋转） | A 满足"能用一致"，B 增加打磨工时 |
| **D5** | 飞书应用形态范围 | **A. 仅网页应用** ✅<br>B. 网页应用 + 机器人（任务完成推消息到用户） | Phase 12 已有 job 通知能力，B 顺手但需配事件订阅 |

---

## 6. 实施路线（建议，每阶段完成交付可验证）

### Phase A — 部署模式与鉴权基座
1. `config` 增加 `auth_mode`（`local` / `feishu`）+ JSSDK/应用凭证配置项
2. `core/security.py` 守卫改造：`local` 模式维持 `is_local_request`；`feishu` 模式改会话校验（cookie 签名/过期）——**保持 10+ 处调用点不动，只换守卫实现**
3. CORS allowlist 配置化；CSP 增加飞书 CDN 白名单
4. 新增 `api/auth.py`：feishu code → user_access_token → 会话；前端 `static/auth.js` 免登引导（含浏览器直访时的"登录中"占位）
5. 部署骨架：`web/`（nginx 示例 + uvicorn 启动脚本）+ DEPLOYMENT.md 更新
6. **测试**：守卫双模式单测 + auth 集成测试 + 回归 100%（Electron 模式不动）

### Phase B — 移动端适配
1. review.html 断点（`md:` 以下单列堆叠：页导航 → PDF → findings）
2. upload/settings 窄屏检查与修正
3. 触屏交互（点击放大 PDF、按钮尺寸）
4. **测试**：覆盖率维持 ≥90%；视口宽度人工验收清单

### Phase C — 上线与验收（依赖 D1/D2/D3 结论）
1. 飞书开发者后台：创建自建应用 → 网页应用能力 → 主页 URL → 安全设置 → 发布
2. 服务器部署 + HTTPS + 管理员审核
3. 端到端验收：桌面端工作台打开、手机端打开、免登、完整 pipeline 跑通
4. 文档收尾：CLAUDE.md / README / ARCHITECTURE.md 同步新形态

---

## 7. 风险

| 风险 | 级别 | 缓解 |
|---|---|---|
| `is_local_request` 改造引入桌面版回归 | 高 | 双模式分治：local 模式逻辑一字不改，新增 feishu 分支；桌面版回归测试全绿再上线 web |
| 飞书免登在**浏览器直访**场景不适用（无 webview） | 低 | 浏览器直访展示二维码登录（官方 OAuth 免登同流程）或提示"请在飞书中打开"（D3 内测阶段先取后者） |
| SQLite 并发（多用户同时操作） | 中 | 现有 db_lock 串行化已覆盖；若团队 >10 人才考虑换库，明确不做 PG 迁移 |
| 移动端 PDF 预览体验（大文件渲染） | 中 | JPEG 渲染已压缩（quality 82）；移动端限制单页渲染尺寸即可 |
| 凭证泄露面扩大（公网服务器拿到 LLM/OCR key） | 高 | config.json 权限收紧 + 部署文档明确安全基线（仅 root/服务用户可读）；key 不落日志已有 |
| 备案/审核周期 | 低 | D3-A 先用内网穿透联调，备案并行办理 |

---

## 8. 项目内文档联动

- `CLAUDE.md`：Current phase、架构段落补充 web 形态
- `DEPLOYMENT.md`：新增「Web 版部署」章节（Nginx + HTTPS + 免登配置 + 故障排查）
- `README.md`：形态说明（桌面 Electron + 飞书 Web 双形态）
- `PLAN.md`：进度追踪追加 Web 版 Phase
- 本文档：随实施推进更新决策点结论（D1~D5）