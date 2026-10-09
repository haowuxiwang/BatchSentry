# 企业级就绪度评估（2026-10-09 · R78 第十八批）

> **触发**：用户问「当前应用是否足够健壮？是否足够作为企业级的应用？」
> **方法**：三个**只读**审计代理分头取证（安全面 / 鲁棒性 / 泛化与企业级），
> 关键断言由我**逐条复核**（不采信代理的"未确认"项当结论）；外部基线用
> **Electron 官方安全清单**（20 项）与 **21 CFR 11.10(a)–(k)** 原文。
> **本文件只写有证据的**；未能确认的一律另列（§5），**不得**读成"已通过"。
>
> 相关：`docs/RELEASE_READINESS_PLAN.md`（分发就绪，§1 安全）、
> `docs/ADVERSARIAL_AUDIT.md`（§6 鲁棒性/泛化）、`docs/B6-1_GENERALIZATION_REPORT.md`（泛化实测）、
> `docs/CVE_REACHABILITY.md` / `docs/DEPENDENCY_AUDIT.json`（依赖公告）。

## §0 一页结论

**对它的既定定位 —— 本机、单用户、桌面工具 —— 工程水准明显高于同类。**
已经做实的（**有代码与机检证据**）：本机请求守卫在**读 body 之前**判定
（`main.py` 的 `LocalGuardMiddleware`，且注册次序有 **AST 护栏**）；请求体**流式**硬上限
（`Content-Length` 非法值/重复头/负数一律拒 —— 对应 `CVE-2026-53540` 与走私形态）；
CSP + `nosniff` + `X-Frame-Options` + `base-uri`；上传**三重**校验（扩展名 + **magic bytes**
+ 尺寸/像素/页数上限）；SQL **全部参数化**；`GET /api/settings` **掩码**回显密钥；
Electron `contextIsolation: true` / `nodeIntegration: false`；门禁 **11 项**含
依赖漏洞与运行时 EOL 的 **fail-closed** 机检。

**但作为"企业级应用"（多用户 / 受监管 / 要审计追踪与判定质量证据）—— 不足**，
且缺口是**结构性**的。最硬的三条：

| # | 缺口 | 证据（可复核） | 为什么是阻断项 |
|---|---|---|---|
| **G1** | **无身份 / 权限 / 电子签名** | 全仓无任何鉴权（唯一闸门是本机守卫）；`core/security.py` 自述 "local single-user app" | 11.10(d)(g) 不满足。在共享机 / RDP / 多账户上，**任何本地进程**都能读全部批记录、改结论、读或覆写 API 密钥、删库、触发关机 |
| **G2** | **审计追踪可被删除** | `api/jobs/actions.py:277-278`：删 job 时 `DELETE FROM audit_log` 与 `DELETE FROM llm_call_audit`；只留一条 `_system job_deleted` **摘要**行 | 11.10(e)：「record changes shall not obscure previously recorded information」且「audit trail shall be **retained** at least as long as the subject records」。当前设计把**被删记录自己的轨迹**一起销毁 |
| **G3** | **判定质量无可信数字** | 无人工标注集；`FINDING_GROUND_TRUTH.json` 的唯一 pass/fail 判据是**合成语料**的规则层 F1（零 LLM/OCR）；真实 job 同输入两次跑 **41 vs 51**（+24%） | 11.10(a) 的系统验证与"辨别被改动记录"**无从量化**。目前**回答不了**"漏报率/误报率是多少" |

## §1 对照 21 CFR 11.10（a）–（k）

| 条 | 要求（要点） | 现状 | 判定 |
|---|---|---|---|
| (a) | 系统验证：准确性、可靠性、能辨别被改动的记录 | 门禁 11 项 + 3900+ 单测 + 变异验证；**但判定质量无标注集** | **部分** |
| (b) | 生成**人可读 + 电子**形式的完整副本 | 有报告导出（md/json）；**无数据库级完整导出** | **部分** |
| (c) | 记录保护：保留期内准确、可随时检索 | SQLite + 日志轮转 5；**无备份/保留策略**，删除即销毁 | **部分** |
| (d) | 访问限于**授权个人** | **无身份体系** | **✗** |
| (e) | 安全、计算机生成、带时间戳的审计追踪；**改动不得遮蔽先前记录**；保留期 ≥ 记录 | 有 `audit_log`/`llm_call_audit`；**但删 job 会 DELETE 它们**（见 G2） | **✗** |
| (f) | 操作顺序检查 | 状态机 + `transition_status`（有护栏） | **✓** |
| (g) | 权限检查（谁能用/签名/改记录） | **无** | **✗** |
| (h) | 设备检查（数据来源有效性） | 本机守卫（Host/Origin）+ 端口绑定 | **部分** |
| (i) | 人员资质 | 软件不适用 | **N/A** |
| (j) | 书面政策与问责 | 软件不适用 | **N/A** |
| (k) | 系统文档控制（分发/访问/变更控制） | `CHANGELOG` + `PROVENANCE.txt` + 门禁 + 产物指纹 | **✓** |

## §2 对照 Electron 官方安全清单（20 项，节选有判别力的）

| 官方项 | 现状 |
|---|---|
| 3. Enable context isolation | ✅ `contextIsolation: true`（**两处窗口都有**，已加护栏） |
| 4. Enable process sandboxing | ✅ **本轮显式写出** `sandbox: true`（Electron ≥20 本就默认 true ⇒ **原先不是活漏洞**，但隐式默认可被 `nodeIntegration: true` 连带关掉）；已加机检 |
| 2. Do not enable Node.js integration for remote content | ✅ `nodeIntegration: false` |
| 6. Do not disable `webSecurity` | ✅ 未关闭（护栏禁止回流） |
| 7. Define a restrictive CSP | ⚠️ **有**（`default-src 'self'` + `base-uri 'self'`），但 `script-src` 含 `'unsafe-inline'`（代码已自述成因，待 nonce 重构） |
| 13/14. Disable/limit navigation & new windows | ✅ `setWindowOpenHandler` 仅放行 http(s) 转 `shell.openExternal`；`will-navigate` 拦非本机 |
| 16. Use a current Electron version | ✅ Electron **43.7.4**（门禁 `runtime_eol` 机检在支持线 41/42/43 内） |
| 17. Validate the `sender` of IPC messages | N/A（无 IPC / 无 preload） |
| 19/20. Fuses / 不向不可信内容暴露 API | ⚠️ **未核**（见 §5） |

## §3 本轮**已修**（4 项，全部带护栏 + 变异验证）

| 修复 | 内容 | 证据 |
|---|---|---|
| **F1** Electron 沙箱**显式化** | 两处 `webPreferences` 显式 `sandbox: true`（防隐式默认被连带关闭） | `tests/unit/test_electron_security_flags.py`（5 条）；变异 E1–E4 |
| **F2** 全局异常处理器 | 未捕获异常由**纯文本 500** 改为与 `HTTPException` **同形**的 JSON，**只**回显请求号（`str(exc)` 可能含路径/上游 URL/密钥片段）；完整栈只进服务端日志 | `tests/unit/test_unhandled_exception_handler.py`（3 条）；变异 X1–X4 |
| **F3** 上传**磁盘余量**守卫 | 入口预检（507），不足即拒；覆盖 chunked/撒谎头（流式写完后按真实字节数复核） | `tests/unit/test_upload_disk_headroom.py`（5 条）；变异 D1–D4 |
| **F4** §SEC-6 **文档漂移** | `RELEASE_READINESS_PLAN.md` 原写「`\|safe`/`Markup(`/`innerHTML` **零命中** → 已复核、无需动作」—— **与实测相反**（`innerHTML` **46 处**、`Markup(` **4 处**；前端 R63/R65 **拆模块后**才出现）。已更正并加**事实绑定**护栏 | `tests/unit/test_doc_release_plan_sec6_claim.py`（3 条）；变异 S1–S3 |

> 变异合计 **15/15 CAUGHT + 基线绿**（`devlogs/_verify/r78_hardening_mutation.py`）。
> ⚠️ 首版 harness 曾把"临时树无 `.git` ⇒ 护栏自身报错"读成 CAUGHT ⇒ **基线红**暴露了
> 这个**假 CAUGHT**；已把该护栏改为**不依赖 VCS** 的文件系统扫描。

## §4 本轮**登记未修**（需产品/设计决策，不擅自改语义）

| # | 事项 | 建议 |
|---|---|---|
| **0-18** | **审计追踪不可删**（G2） | 二选一：① 软删除（job 行保留 + `deleted_at`）；② 删除前把该 job 的 `audit_log`/`llm_call_audit` **归档**到独立表。**属产品语义变更，须你拍板** |
| **0-19** | `innerHTML` **46 处**逐个依赖 `esc()` 纪律，**无**机检 | 加护栏：`static/**/*.js` 里每个 `innerHTML = <expr>` 必须是空串 / 常量模板 / 含已知转义器调用 |
| — | 无身份/RBAC/电子签名（G1） | 企业部署前必须有；至少先做"多账户下的数据隔离"评估 |
| — | 无人工标注集（G3） | 需要领域数据；在此之前**不得**对外宣称任何精度/召回 |
| — | `script-src 'unsafe-inline'` | 用 CSP nonce 重构（代码内已注明） |
| — | 密钥明文落盘 | 可评估 Windows DPAPI；注意会改变配置格式与迁移路径 |
| — | 无 DB 备份/导出端点、无 `LICENSE` 文件 | 分发前补 |
| — | 磁盘满的**中途**行为、`-wal` 断电重放 | 无端到端证据（见 §5） |

## §5 未验证边界（**不得**读成"已通过"）

- 本评估基于**源码与文档**；三个代理**均未**运行应用做动态验证（除本轮 e2e）。
- Electron `sandbox` 的**运行时**生效值未实测（本 agent 环境下 Electron 层 e2e 不可复现）。
- `%APPDATA%/PBC/config.json` 的**实际 ACL** 未测（明文已知）。
- `innerHTML` 的 46 处**未逐处**判定转义是否充分（只抽查了关键渲染器）。
- 磁盘写满在 pipeline **中途**的确切行为无端到端证据。
- Stage2 单页**最坏时长**可能超过 `analyzing` 看门狗阈值（代码推导：`(3+2)×480s = 2400s > 1800s`）
  ⇒ 存在**误判停滞**风险；本轮 e2e 实测到单页 `latency=313.3s` + fix-hint 重试，
  但**尚未**观察到越过阈值（登记于 `docs/TODO.md` 的 **0-10**）。
- `Qwen/Qwen3.5-35B-A3B` **从未**产品级验证通过（仅有 >930s 无日志挂起的历史记录）。
