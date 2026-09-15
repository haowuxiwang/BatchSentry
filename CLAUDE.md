# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**BatchSentry** — GMP 批生产记录半自动合规检查系统（前身 Pharma Batch Checker / PBC）。

用户上传 PDF 批生产记录 → OCR 识别 → LLM 结构化提取 → 规则+LLM 跨页合规分析 → 人工复核界面 → 导出报告。

**Current phase**: **v1.1.2** — 已完成 v1.1 全部里程碑 M2–M7：Finding 质量可度量+降噪（M2）、OCR 尺寸/形态鲁棒性（M3）、规则扩展 R11–R17 + 注册表（M4）、多源 GMP 知识库（M5，6 源/441 条）、SSE 优化 + 三色复核分级（M6）、docling 第三对照引擎（M7）。此后 v1.1.1 落地**降噪 P0-2 抑制留痕**与 **P0-3 区域级证据锚**，v1.1.2 补完 P0-3 的**服务端转正页面逆映射**（第 8 页 `angle=270`，此前只做宽高比闸门 → 该页明示不可定位）并补齐**分发一致性机检**（配置↔文档↔实物）与**上传页数上限**（此前只有体积上限，低密度极长 PDF 会先撞 OCR 1 小时轮询上限；见 `docs/UPLOAD_LIMITS.md`）。覆盖率门禁 **95%**；真实 51 页全链路 frozen e2e 为放行前提。此前：Phase 12（Feishu job notifications）+ 2 轮对抗审查，v1.0.0，本地单用户部署（PyInstaller exe + Electron 便携版 win-unpacked）。

**Round 15 (真实文档 e2e + GIL 隔离, 2026-08-24)**: 138MB/51 页真实手写批记录（丝裂霉素提取）全链路 e2e 三轮——① **P0 GIL 饿死事件循环**：Stage 0 规范化（51 页 3000x4000pt → 300dpi 重渲染）经 `asyncio.to_thread` 执行时 fitz C 调用持 GIL 数十秒，aiosqlite 毫秒级操作退化 1s+/条、status GET 10s ReadTimeout、SSE 停摆（pipeline.log 时间戳实证）。修复：新模块 `core/procpool.py`（spawn 单 worker 进程池 + `run_cpu`；不可 pickle 测试替身自动回退 to_thread；worker 父进程死亡守卫——e2e 实证孤儿 pbc-server.exe 锁死 dist 文件致构建 PermissionError）。② **规范化副本 JPEG 瘦身**：灰度 PNG 无损嵌入膨胀（51 页 → 224.8MB 超 200MB 上限）；改 `pix.tobytes("jpeg", jpg_quality=85)` 嵌入（OCR 无损，实测 224.8→43.8MB，5.1×）。③ **gmp_basis 补全**：LLM 生成 `batch_logic` 等变体 type 缺映射 → 显式映射 + 关键词回退（真实 PDF 50/50 全覆盖）。④ e2e harness：`e2e_run.py`（frozen exe 多轮驱动 + 逐页稀疏页检测 <40 字符 + gmp_basis 覆盖断言 + 轮询抗挫折），`gen_e2e_pdf.py`（合成埋点 PDF）。**e2e 结果**：真实 PDF paddle 轮 review/475 findings/critical 42/sparse 0/SSE 三阶段（ocr 39/51 → analyze 8→9 → cross）实测；img 轮 Paddle 队列满（HTTP 400 code 10010）3 次退避 → 自动 failover MinerU 成功——重试+兜底链实战验证。

**Round 16 (上游超时自适应 + pytest 退出挂起根因, 2026-08-24)**: ① **Paddle 轮询超时按页数自适应**：e2e real-mineru 轮实证 51 页任务受理后 600s 固定上限内未返回（任务 85287516750168064）——`poll_timeout_for(pdf)` = 600s + 30s/页（封顶 3600s），`run_ocr` 传给 `poll_job(timeout_s=)`。② **MinerU 瞬态终态重提交**：服务端 `parsing failed, please try again later`（明示可重试的任务级终态）→ 20s 退避重新提交一次，二次失败才走 failover 链（`run_ocr` attempt 循环）。③ **P0 pytest 退出挂起根因（py-spy 实证）**：多会话堆积的"pytest 打印完摘要后挂起/僵尸进程"= `_load_review_exemplars`→`get_db()` 在 `asyncio.run()` 瞬态循环里创建全局单例 aiosqlite 连接，循环销毁后连接线程（aiosqlite 0.21 Connection 即 Thread 子类、非 daemon）无法经 API 关闭（close() 的 future 绑定死循环）→ `threading._shutdown` 永久阻塞（`test_cross_page_analyzer.py` 单文件可复现）。修复：conftest session 级 autouse fixture 在会话 teardown 用 gc 扫描存活 Connection 调 `_stop_running()`（线程安全队列哨兵，不依赖事件循环）。④ **procpool 加固**：`import pickle` 提到模块级（except 子句引用未导入名会在 worker 异常时 NameError 掩盖原错误）；worker std 流重定向 devnull（spawn worker 继承父进程管道句柄致包装 shell EOF 永不关闭）；`atexit.register(shutdown_pool)`；pytest 环境（`PYTEST_CURRENT_TEST`）一律 to_thread 回退（进程隔离是生产诉求，测试只需确定性）。验证：906 tests 全过 + 进程零残留干净退出。

**Round 19 (知识库接入 KB-1: GMP2010 条文后置富集, 2026-08-26)**: 语料勘察：docs/2010版GMP.doc 为二进制 .doc，Word COM 提取 33,587 字 / 14 章 / 286 条独立条款（全"正文"样式，按 `第X章/第X条` 正则切分）。① **种子管线**：`scripts/seed_kb.py`（COM 提取 → 中文数字转阿拉伯 → 行首标签切分防正文交叉引用误切；`--verify` 金样断言章数=14/条款∈[280,292]/升序/无空条）→ 派生 `core/kb/data/gmp2010.json` 入 git（源 .doc 不入包不入库）。② **零依赖检索**：`core/kb/retriever.py` 字符 bigram 倒排 + BM25(k1=1.5,b=0.75)，查询 = TYPE_QUERIES 按 type 的法规词表 ∪ 描述高价值 bigram（df≤60 过滤泛化字）；TopK≤4、摘录≤120 字、总预算≤1500 字。③ **Schema v8**：kb_entries 镜像表 + findings.kb_refs TEXT（JSON [{entry_id,label,excerpt,score}]），守卫式迁移镜像 v7 范式。④ **后置富集接线**（prompt 零改动、零 token）：stage2 llm_page 流式行与 stage3 批量/dual_diff 三处 INSERT 前调 `attach_kb_refs`（幂等，坏库安全退化），序列化随行落库。⑤ **展示三端**：复核卡片折叠「依据条文」（SSR kb_refs_list 解码 + AJAX JSON.parse 双端同步）、report.md「依据条文附录」去重全文、设置页新增只读「知识库」分区（搜索防抖 + GET /api/settings/kb 本地守卫）。⑥ **打包**：spec hiddenimports += core.kb.*、api.settings.kb；datas += gmp2010.json（frozen 实测入包）。**验收**：24 项 KB 测试 + 全量 1279 passed/90.10%；冻结版 e2e pdf 轮后 **35/35 findings 带 kb_refs**（sample 第一百六十X条 记录类条文 score≈243）、报告附录/设置浏览 OK；ui_e2e 43 断言回归全过。

**Round 18 (对抗审查三连修 + 分发实证五连修 + e2e cancel/dual 轮, 2026-08-25)**: ① **P0 复核翻页 findings 脱钩**（用户生产事故报告）：`list_findings` 的 `order=confidence` 分支 WHERE 只有 job_id 无 page 过滤——复核页 `loadPageData` 固定带该参数，任何页都返回全 job 前 50 条（total 恒为全局数），点导航页码清单不变；修复：by_confidence 分支补 `AND page = ?`（无 page 参数时保留全局队列语义），回归测试锁定。② **Stage1 取消语义落地**：OCR 阻塞在 to_thread 线程无法被 await 中断 → `state.is_job_stopping_sync`（独立只读 sqlite3 连接探针，WAL 并发读）注入 Paddle/MinerU 轮询循环 → 新异常 `OCRCancelled`（RuntimeError 子类，无瞬态标记故不触发 MinerU 重提交）；failover 链显式放行（取消≠后端故障，不切备选白烧配额），stage1 在事件循环完成正式 cancelling→cancelled 迁移。e2e 实证：ocr_running 后取消 **10s 终态 cancelled**（旧实现分钟级）。③ **_repair_truncated_json 数值安全化**：尾部裸数字一律丢弃（BPE 可把 25.4 切成合法前缀 token，fullmatch 通过 = 静默错值）→ null 触发 schema 校验 fix-hint 重试；true/false/null 原子关键字保留；垃圾 token 返回 None 保住重试链。④ **_truncated_warn/_schema_warn 三端接线**（此前死代码）：SSR 横幅 llm-integrity-banner + review.py page_flags 置信度扣分 + review.js AJAX 翻页同步。⑤ **空页 job 报告防"静默合规通过"**：report.md 头部页面覆盖声明（N 页 OCR 空白/M 页未分析），零 findings 且覆盖缺失时汇总改警告文案。⑥ **安全批**：`/api/jobs/{id}/pdf` 补 is_local_request（最后裸奔读端点）+ measurements 守卫前置 + probe 表单 base_url 过 validate_external_url + upload filename=None TypeError + electron will-navigate 白名单。⑦ **性能**：page_image ETag → If-None-Match 304 短路 fitz 渲染；中间件 Cache-Control 改 setdefault。⑧ **signature_order 同角色豁免**：排序后仅跨级比较，两名复核人间先后不再误报。⑨ **重分析页 pending llm_page 先清后插**（自愈改变 raw_html 后指纹变化，UNIQUE 挡不住并存两套结论）。⑩ **requirements 钉 starlette>=0.47**（旧版 GZipMiddleware 吞 SSE 帧）。⑪ **main.py 补 import json**（review 页带 ocr_diagnostics 即 NameError 500）。

**KB-2 条文注入 + 后端清账（同日续）**：① **prompt 注入（RAG grounding）**：`build_page_kb_context`（TopK≤5/摘录 200 字/预算 1500）按页面主题词+高价值 bigram 检索；块注入 user 侧 `[知识库参考]`（system 静态维持 prompt caching 不变量），置于 user_suffix 之前保持终结语义；开关 `kb_prompt_inject`（env/settings，默认开）；PROMPTS 注册 v4（模板=v3，审计可区分带条文调用）。② **RAG 审计留痕**：schema v9 `llm_call_audit.kb_used TEXT`（JSON {v: 种子 sha 前 12 位, ids:[entry_id]}），主调用与 schema-fix 重试两处 audit_ctx 均携带——满足 Annex 22 级"重建当时输入"要求。③ **后端 P2/P3 清账**：Stage0/procpool 取消检查点（整份 stage1 pre/post + 切片 engine pre/post，取消不再排队规范化/不采纳结果）；grounding 双通道加固（token 级精确+尾零容忍，根除相邻单元格拼接幻观数字与前向嵌入量级错误；tokens=None 保持旧子串语义零回归）；page_image LRU 淘汰持 per-doc 锁 close（渲染竞态 500 根除）；live 快照终态缓存（(id,status,finished_at) 键，省稳态 QPS）；notify webhook 去重 asyncio.Lock 串行化（并发终态重复推送根除）；_derive_phase 优先 cross_progress 显式信号（缺页场景文案不再卡 analyze）；report.json 加 schema_version=1；review.js 页码圆点 counts 签名 diff-skip。**验证**：1293 passed / 90.00%；ui_e2e 48 断言全过（新增 KB 引用 AJAX+SSR 渲染、设置页知识库分区列表/搜索/404）；冻结版重打包冒烟 OK。

**分发实证五连修**（双击 win-unpacked 实测驱动）：① **启动失败可诊断化**：用户报 "Server not ready after 60 checks"——backend stdout 只进 console 打包后永久丢失、健康检查静默吞错误、固定 60×500ms=30s 预算被杀软深度扫描击穿（boot log 实证 spawn 后 145s 才到 uvicorn）。修复：新增 `%APPDATA%/PBC/logs/backend-boot.log`（spawn/stdout/stderr/exit/每次检查 lastError 全落盘）；等待改截止时间制 180s——子进程存活就继续等（splash 每 10s 刷进度+原因），进程退出立即失败；server.py 入口加最早一行 `[boot]` 引导日志切分阶段。复现压测：修复前 3 轮 1 失败（138s>30s 预算），修复后 4 轮全过（14-21s）。② **splash 方框**：首帧直接渲染中文，软件渲染下 CJK 回退字体未就绪即 tofu——spinner 圆点是纯形状先呈现，文本 fonts.ready（300ms 兜底）再填充 + 字体栈补 Microsoft YaHei。③ **设置页保存按钮 hover 消失**：`.btn-press:hover{background:muted/0.6}` 特异性(0,2,0) 压过 `.bg-foreground`(0,1,0)，黑底白字按钮悬停变白底白字——btn-press 只留按压反馈，hover 交还各按钮自身工具类。④ **打包 console 收敛**：pbc-server stdout/stderr 与含绝对路径的 spawn 日志不再回显 DevTools（isPackaged 门控），完整内容始终落盘 boot.log。⑤ **e2e harness 新增 cancel/dual 轮**：cancel 断言 ≤180s 终态 cancelled；dual 开 OCR_DUAL_COMPARE 断言 audit dual_compare_done + 差异 findings（合成件 p2/p4 双引擎分歧页如期强制 partial_review）；run_upload 回传 job_id、pdf/img 轮 force=1 轮次顺序无关。**双引擎 A/B 定量**（同文 6 页，生产 dual_compare 函数实测）：页集合一致、单元格零丢失、双向覆盖率 0.78-1.00，p2/p4 低于 0.85 如期判 DIFF；已知方差——MinerU 轮 LLM 曾互换提取工序 6 起止时间致规则层漏报（llm_page+llm_cross 双层仍 critical 兜住，缺陷无漏报）。**UI 级 e2e 新增**（`ui_e2e.py`，Playwright + 系统 Edge 驱动打包产物，43 断言全过）：真实表单上传→review 跳转→逐页点击导航三断言（PDF 原图页码 / findings 仅本页且与 API 计数精确一致 / OCR 面板内容匹配服务端数据）+ 前后箭头 + 设置页四分区切换 + 保存按钮 hover 可见性与底色保持 + test-conn 连通性探测 + 全程零未捕获 JS 错误。**顺带抓出并修复真 app bug**：`updatePdfDisplay` 的图片缓存短路 return 会跳过 `syncNavButtons`，叠加 loadPageData 晚更新全局 currentPage——从末页跳回任意页后next 箭头永久卡死（setter 探针栈实证）；修复为 sync 前置无条件执行 + currentPage 先于 UI 刷新落位。**e2e 累计 9 轮全过**（pdf×3/img/mineru/cancel×2/dual/含冻结版逐页 findings 过滤断言）。

**Round 22 (持锁通知 P1 + 申报表修订, 2026-09-03)**: ① **取消通知持锁 P1（round-20 修复的自伤审查）**：`_is_cancelled` 内的 `notify_job("cancelled")` 在全局 db_lock 持锁期间执行——飞书 HTTP 重试退避可达数秒，会阻塞所有 DB 写入方（status GET/SSE 推送）。重构：锁内只做状态迁移 + 置标志，通知移至锁外且仅在本次 cancelling→cancelled 迁移时触发（重复探测由 notify 审计查重兜底）；修复过程中自伤一处（重构把 `return False` 误写为 `return True`，活跃任务会被误判取消），被新增锁范围回归测试当场抓出——`test_is_cancelled_notifies_outside_db_lock` 断言通知回调执行时 `db_lock.locked()==False` 且函数返回值语义不变。② **申报表 docx 修订**（run 级安全替换，10 处全命中）：5 处"15 条规则"→14 条（代码实测 R1-R10+R8b+R9a+R-M1/R-M2）；阶段时长 3+8+3 周对齐 git 时间线（07-15 启动→08-26 KB 完成 ≈6 周）；"数据不离开企业内网/所有数据本地存储"→"结构化结果与审计数据本地存储；OCR/LLM 推理经加密 HTTPS 调用第三方服务（可选私有化端点）"；51 页耗时 25 分钟→21 分钟（Stage1/2/3 实测 193s/922s/147s）。原件备份 devlogs/申报表_backup_20260902.docx。**验证**：1301 passed / 90.02%；重打包后 e2e pdf（gmp_basis 32/32、SSE 四阶段）+ cancel（4s cancelled）+ ui_e2e 全过。

**Round 23 (A 横置自愈 + B Stage2 ETA + C 复核反馈回流, 2026-09-04)**: ① **A 横置页旋转自愈（OCR 完整性最后盲区）**：内容横置（扫描时纸横放，非 /Rotate 元数据——元数据路径 Stage 0 已处理）OCR 返回稀疏/空 → 切片重试仍空时进旋转恢复通道：`self_heal._rotation_heal` 逐 90/270/180°（横放最常见在前）单页重渲染（`_write_rotated_slice`，fitz show_pdf_page）重 OCR，过 `_accept_heal_text` 验收即采纳——与切片自愈同款落库（清 structured_json 触发补跑、prior_diagnostics 保留、内存回写），诊断加 `rotation_deg`，审计 `stage1_rotation_recovered`（GMP 可追溯"此页为何多了旋转字段"）；探测未果页落 `rotation_probed` 诊断（复核页提示"已尝试 90/270/180° 旋转恢复未果，请人工核对原图"——恢复尝试本身即是完整性证据）。job 目录 mkdir 防御（rot 切片写入前）。复核页 OCR 横幅展示"已自愈（横置页按 N° 重渲染后识别）"。② **B Stage2 进度 ETA（纯前端）**：`static/eta.js` 纯函数（无 DOM/nowMs 注入，node 单测锁定）——5 分钟时间窗口 (pages_analyzed, t) 采样算速率；窗口基线取窗口内首个样本的前一个（稀疏采样兜底，两帧间隔 >5min 仍可算）；MIN_SPAN 8s 防同秒抖动；n 回退（job 重跑）清池重采；fmtEta 分级文案（<60s ≈1 分钟内 / <90min 约 N 分钟 / 更长 约 H 小时 M 分 / 99+ 封顶）。upload.js（任务行）与 review.js（进度条）双端接入"· 剩余约 N 分钟"后缀，终态清采样池防 Map 泄漏。③ **C 复核反馈回流（confirm/reject 数据再利用）**：`api/review._review_stats_from_rows` 纯函数聚合（对 SELECT 行聚合不自查 DB，端点/报告共用）——确认/驳回率、高频驳回类型 Top5（含份额）、按来源驳回率（rule 层 >50% 即阈值过紧信号）；`GET /api/jobs/{id}/review-stats`（404 守卫 + 本地守卫）；复核页折叠面板（brief 一行摘要 + 明细）；report.md 新增「复核反馈统计」章节（高频驳回类型 + 按来源驳回率 + ⚠️ 偏高标记）、report.json 加 `review_stats` 字段。④ **e2e 扩展**：`gen_e2e_rot_pdf.py` 合成横置样本（p2 内容横置 90°、p3 横置 270°，show_pdf_page 旋转嵌入非 /Rotate 元数据；p1 批号基准 B2025001 对照页防旋转链误伤正常页）+ `e2e_run.py` rot 轮（双路径合法：VL 直识横置文本 / 旋转自愈；硬断言标记文本可见不丢失、rotation_deg 落库必有审计、p1 对照可见）+ `ui_e2e.py` B/C 断言（Phase2 进度文案采样 + review-stats 面板/端点形状）。⑤ **自伤修复**：review.js 插入 loadReviewStats 时误加 `});` 提前闭合 DOMContentLoaded → 文件尾 `})();` 悬空语法错误（node --check 由 test_js_files_pass_node_check 用例当场抓出）——函数移至 IIFE 层修复。⑥ **A3 嫌疑横置页升级（e2e 二轮实证）**：VL 对横排文本有旋转容忍度——横置页切片重跑可能"部分恢复"（表格可读、标题/细字乱码，如 e2e p3「横置二百七十度参数表」→「横直一口」），>100 字过验收后旋转探测从未运行。修复：横向几何（aspect_ratio>1）+ 初判稀疏的切片恢复页列为嫌疑横置，补跑旋转探测；旋转读取须内容量明显更优（> `_ROTATION_UPGRADE_FACTOR` 1.1× 现有长度）才替换——正常横版宽表页旋转后读取必然更差，长度门槛天然拒绝误替换；未升级成功保留切片结果（诊断不动）。⑦ **A4 几何预筛 + 瞬态重试（e2e 三轮根因）**：上游「系统错误-拆页」随机杀死 90°/270° 探测时，仅剩的 180° 乱序读取曾因长度门槛被误采纳（p2 被 180° 乱序文本污染）。双修复：`_prescreen_rotation_angles` 投影方差预筛——原生页单次 48dpi 灰度渲染算行/列强度投影方差（横向文本行间明暗交替→行方差大；纵向同理反转），90°/270° 旋转交换两轴、180° 保持 → 数学推出各候选角度朝向，横置页只探测 90/270（180° 从几何上不可能正确，永不探测）；灰区/空白页返回 None 回退全角度（预筛永不阻塞恢复）。e2e_rot.pdf 实测判别力：横向 ratio 6.1-13.1 vs 纵向 0.08-0.16（阈值 1.15，双峰完美分隔）。`_probe_slice_text` 瞬态错误重试——单角度探测异常时退避 2s 重试一次（上游「系统错误-拆页」为随机瞬态失败），正确角度存活率翻倍。**验证**（2026-09-10 收尾复跑，实测为准）：1326 passed / 1 环境阻塞（`test_main_routes.TestServePdf::test_pdf_non_local_host_returns_403`——被测行为正确 403，仅其清理步骤删除项目 `output/` 探针被沙箱拦截；隔离单跑通过，属 order-dependent 环境产物，非代码回归）/ 覆盖率 90.26% ≥ 90% 门禁（新增 rotation 7 用例：升级正/反例 + 预筛拦截 180° 误采纳 + 瞬态重试恢复 + 预筛纯函数三态 + 直识路径；ETA node 10 用例 + review-stats 7 用例）；ruff F 类全部为 HEAD 存量基线（stash 对比零新增）；build.ps1 全流程（app.css 19.2KB / pbc-server 106.7MB smoke 1s / win-unpacked 381.1MB）+ 冻结版 e2e rot（p2/p3 via rotation@90°、rot_lost=[]、audit_rotation_events=1）、pdf+real（51 页真实件 2186s、gmp_basis 50/50、sparse_pages=0）、ui_e2e（含 round-23 B ETA 后缀 / C review-stats total=34）全部 ALL PASSED。

**Round 21 (对抗审查三连修 + 仓库卫生 + 拥堵自适应 e2e, 2026-09-02)**: ① **分片路径取消竞态（整份路径 P0 的对称修复）**：最后一片检查点之后、`ocr_done` 转换之前收到取消（ocr_running→cancelling）→ 转换非法抛 InvalidTransitionError → 片内循环后的 `analysis_tasks` 清理被跳过，孤儿 LLM 协程继续跑并写入已取消 job。修复：转换前补取消检查点（取消即 drain + 返回）+ 转换包 try/except InvalidTransitionError（drain 后重抛，引擎恢复分支完成 cancelling→cancelled）；`_drain_analysis_tasks` 提取公共清理函数。两个新回归测试（尾部检查点命中 / 转换瞬间竞态，断言终态 cancelled + 无 status_forced_error）。② **cancelled 通知缺口**：全链路无任何 `notify_job("cancelled")` 调用点（README 承诺取消推送飞书）——收口在 `_is_cancelled` 确认点（所有取消路径唯一必经），审计查重 + 锁串行化防重复。③ **仓库卫生**：根目录 22 个开发期日志/SSE 帧/杂散 PDF/JPG 归集 `devlogs/`（gitignore）；e2e_run SSE 证据改写 devlogs/；申报表 docx（含联系人 PII）显式 gitignore；本会话临时日志删除。④ **e2e 拥堵自适应**：上游 LLM 拥堵日单页排队数分钟（2026-09-02 实测 img 轮 1 页 482s、ui_e2e Phase2 6 页 >5min）——常规轮预算 600s 固定值 → `E2E_PDF_TIMEOUT` env 覆盖（e2e_run）+ `UI_E2E_TIMEOUT`（ui_e2e Phase2 deadline 制），与 REAL_TIMEOUT_S 同策略。**验证**：1300 passed / 90.01%（新增分片取消测试补回覆盖门禁）；冻结版最终产物 e2e pdf（112s，gmp_basis 33/33，SSE 四阶段）/img（482s 拥堵日）/mineru（108s）/cancel（77s，cancelled 终态）ALL PASSED + ui_e2e ALL CHECKS PASSED。

**Round 20 (降噪 N1/N2 + 打包信号复核 + cancel 终态覆盖 P0, 2026-08-31/09-01)**: ① **降噪 N1（LLM 语义重复抑制）**：复核 UI 同一问题出现 rule+LLM 两三份是用户可见噪声主源——`stage3._run_stage3_cross_analysis` 对 `llm_cross`/`llm_fallback` 中与规则层已覆盖 (page,type) 重复的 finding 直接抑制（rule 为权威版本；`user_rule` 豁免——用户显式规则与规则层共存属正常）；抑制计数写日志 + 审计 `findings_overlap_suppressed`（GMP 可追溯"为什么少了一条"）。真实 e2e 实证：6 页合成件抑制 1 条（28 new + 6 llm_page + 1 suppressed）。② **降噪 N2（completeness 提示收紧，PARSE 精确化）**：v4 user_suffix 在 schema 占位符锚定 `completeness_notes` 输出位置 + 追加 `[完整性检查规范]`——completeness 仅允许 4 类可从原文确证的情形（签名栏空/必填留白/勾选矛盾/整栏缺失），明令禁止「无法准确识别/可能缺失」等推测性表述（识别不清归 overall_confidence=low 与 handwritten 职责）；v3 模板零改动。③ **回归修复**：v4 改动使 `test_user_prompt_contains_html_and_prefix` 的 `endswith(v3.user_suffix)` 断言过期（v4 schema 内部已插行，非纯追加）→ 改为断言 `endswith(PROMPTS[CURRENT_PROMPT_VERSION]["user_suffix"])`。④ **P0 cancel 终态覆盖 bug（打包产物 e2e cancel 轮抓出）**：OCR 完成后的窗口（空页自愈/双后端对比期间）取消被确认（cancelling→cancelled 终态落库），stage1 尾部无条件 `transition ocr_done` 抛 InvalidTransitionError，引擎恢复分支只认 `cancelling`、对已是 `cancelled` 的终态落入 else 强制 UPDATE error——取消审计链被破坏、用户看到"处理失败"而非"已取消"。修复两层：stage1 尾部 ocr_done 前补取消检查点（与 Stage 0 前后检查点同模式，取消即整体退出）；引擎 InvalidTransitionError 恢复分支对已是终态（cancelled/review/partial_review/error/archived）的 job 保持现状不覆盖。回归测试两个（fake_is_cancelled 第 4 检查点确认取消断言终态 cancelled + audit 无 status_forced_error；fake_stage3 终态后抛 InvalidTransitionError 断言不被覆盖），`test_stage2_cancel_kills_inflight_page_tasks` 调用序阈值 6→7 同步更新。⑤ **ui_e2e harness 自包含**：临时 APPDATA 无 config.json 时前端 needs_setup 拦截一切上传（8/26 通过是残留配置侥幸）——启动前从仓库根 config.json 种子到 appdata；浏览器通道 msedge 失败回退 chrome。⑥ **配置勘误**：config.json 的 DEEPSEEK_API_KEY 实为 SiliconFlow key（LLM_PROVIDER=deepseek 必 401）→ 切 `LLM_PROVIDER=siliconflow`；MINERU_TOKEN 更新为当前有效值。**验证**：单测 1298 passed / 90.02%（门禁 90%）；KB 模块覆盖 store 100% / retriever 98% / settings.kb 100% / stage3 98%。dev 真实 e2e（mineru+siliconflow）：6 页 review 终态，API 级逐页断言 6/6，UI 级 goPage/navPage 三断言 6/6，KB 链路 34/34 findings 带 kb_refs、llm_call_audit 全部携带 kb_used。冻结版最终产物（cancel 修复重打包后）：ui_e2e 全过（含逐页三断言×6 页、KB 引用 AJAX+SSR、页码圆点导航平滑 avg 615ms、设置页/规则保存/连通性探测/知识库分区）；e2e_run pdf/img/cancel 三轮 ALL PASSED（cancel 4s 内 cancelled 终态，SSE idle:cancelling → done:cancelled 证据链完整）。

**Round 17 (real-mineru 双轮闭环 + 覆盖率门禁修复 + SSE 三阶段证据, 2026-08-24)**: ① **real-mineru e2e 双轮通过**：`3800a78a`（review/528 findings/gmp_basis 528⁄528=100%/Stage1-3=193s·922s·147s）+ `a8ef9a53`（review/522 findings/2123s/0 失败页/逐页可见字符 min=194·median=720·sparse(<40)=0）——MinerU 对 51 页真实手写文档完整解析（非仅页眉页脚），类型分布健康（year_contradiction 11/completeness 23/suspicious_date 9/handwritten 1）。② **P0 gmp_basis 关键词兜底死代码接线**：覆盖率核查发现 `_lookup`（Round 15 设计的 `_KEYWORD_FALLBACK`）从未被 `attach_gmp_basis` 调用——LLM 变体 type（batch_number_mismatch 等）此前拿不到法规依据；接线 `attach_gmp_basis`→`_lookup`（精确 type → 关键词兜底 → None），7 组新测试锁定（中文变体/垃圾 type 不误配/_UNMAPPED 短路优先于关键词）。③ **覆盖率门禁修复**：Round 16 procpool 在 pytest 下全程线程回退 → 46% 覆盖拖垮 90% 门禁（全量 89.70% FAIL）；新增 `test_procpool.py` 三层验证（pytest 回退语义/patch `_in_pytest` 真实 spawn 池创建·缓存·executor·异常冒泡·幂等关闭/subprocess 干净环境隔离验证 ISOLATED=True，注意 spawn 子进程重跑 main 脚本需 `__main__` 守卫），worker 体内代码标 pragma（仅子进程执行）。**1241 tests 全过 + 90.30% 门禁恢复**。④ **e2e harness 强化**：real 轮预算 2400s→`REAL_TIMEOUT_S`（默认 5400s，env 可覆盖）——硅基流动拥堵日单页排队 500-1000s 实测，旧预算在 40/51 页处误杀整轮（driver finally 终止 exe）；每轮内嵌 SSE 记录线程（EventSource 语义：read=60s + 断流重连 + 终态即停），产出流式输出证据。⑤ **SSE 三阶段证据闭环**：ocr 阶段 50 帧（内置 recorder）+ analyze→cross→done 490 帧（补采）终帧 review/51 页/516 findings——`/api/jobs/{id}/stream` 全程 3s 推送至终态干净关闭；单次连接 + 短读超时会误杀采集（Stage1→2 大事务提交间隙 >10s 无字节），前端 EventSource retry 自动重连不受影响。⑥ **卡死恢复实战**：被 driver 超时终止的 `3e3e53fd`（analyzing 中被杀）在下次启动被 `recover_stuck_jobs` 正确标记 error + 审计。

**Round 9 (P0/P1 批次: 门禁3 + 分片保护 + WCAG + 结构化输出, 2026-08-21)**: 十维调研后落地 8 项——① **P0 ocr_client.py 缺 `import os`**：`_persist_paddle_original` 的 `os.replace` 必抛 NameError 被吞 → Paddle 原始产物落盘（门禁 1c）从未成功过；修复 + 3 测试。② **门禁 3 双后端对比**（OCR_GOLDEN_CORPUS gate 3）：新模块 `core/pipeline/dual_compare.py`，`OCR_DUAL_COMPARE` 开关（默认关，成本翻倍 opt-in）；主后端成功后备选复跑同一规范化副本，逐页对比 = 单侧空页 + **双向覆盖率**（difflib matching-blocks，0.85 阈值——ratio 会惩罚"一侧多识别"故弃用）+ 表格单元格丢失率 >30%（按内容存在性判断，兼容 Paddle 纯文本 vs MinerU HTML 标记风格差异）；差异页写 source=rule/type=completeness findings（复用去重/复核 UI/报告链路）+ 强制 partial_review；audit `dual_compare_start/skipped/error/done`；分片路径跳过并记审计。③ **分片路径保护补齐**：`_run_sliced_stage1_2` 接收 pdf_diags（与整份路径同款页级 PDF 结构诊断）；自愈在 gather 后执行，`skip_pages` 只排除"已成功分析"页（`_ocr_empty`/`_parse_error`/未分析页保持可自愈——空页会被 _analyze_one 短路存 structured_json，按"已分析就跳过"会漏掉最需要自愈的页）；恢复页从 DB 重取 raw_html 补跑 `_analyze_one`。**顺带修复预存 bug**：MinerU 自愈第二轮重跑第一轮已恢复页（still_empty 只增不减，日志 p[5,10,5,10] 重复即证据）→ next_pending 每轮重建，省一倍上游配额。④ **page_analyzer 二次空页短路**（调试中发现的生产 bug）：raw_html 唯一内容是 `[OCR 警告:]` 前缀时（空页+警告组合），前缀骗过非空检查、剥离后空数据区直达 LLM（幻觉风险）→ 前缀剥离后补短路检查，保留 `_ocr_warning` 供横幅。⑤ **Stage 3 子进度**：`analyze_cross_page(progress_cb=)` 4 里程碑（规则校验/LLM 兜底判定/LLM 语义分析/完成）→ state.py `_update_cross_progress` 写 ocr_progress.cross 子键 → SSE 快照 `cross_progress` → review.js/upload.js 显示"跨页分析 x/y · 标签"。⑥ **结构化输出**（P1-7）：`LLM_JSON_MODE` 开关（默认关），openai 协议 chat_json 传 `response_format={"type":"json_object"}`；网关 400 拒绝（`_looks_like_rf_unsupported` 宽松匹配）→ 降级重试一次 + 会话级 `_json_mode_disabled` 禁用；anthropic 无等价参数跳过；JSON 修复链保留为兜底。⑦ **grounding 归一化**：全角数字→半角 + 千分位逗号剥离（`_normalize_grounding_text` 双侧应用），消除全角 OCR 原文 × 半角 LLM 输出的假阴性。⑧ **WCAG 整改**：全部 `text-muted-foreground/{50,60,70}` 文本变体（≈2.0-2.75:1 不达 AA）→ 实色（4.8:1）；装饰性分隔符 `/30 /40` 与禁用态保留（豁免）；`text-[10px]` 徽章 →11px；settings.css `.field-hint`/provider 按钮/小按钮 11px→12px（按钮高度已 24px 达标 WCAG 2.5.8）；toast 容器 + review 进度文本加 `role="status" aria-live="polite"`；Tailwind CSS 重建（19.0KB）。验证：1185 tests 全过（+31）。

**Module refactor (2026-08, post round-4)**: large modules split into packages with zero behavior change (verified: 1015 tests, route table identical 37/37, perf regression-free): `api/jobs.py` (1236 lines) → `api/jobs/{upload,listings,page_image,status,actions}.py`, `api/settings.py` (1128 lines) → `api/settings/{read,write,rules,provider,probe}.py`, `core/pipeline.py` (1692 lines) → `core/pipeline/{locks,state,ocr_support,self_heal,stage1,stage2,stage3,engine}.py`, `core/cross_page_analyzer.py` (1700 lines) → `core/rules/{base,parsing,rule_time,rule_spec,rule_doc,llm_checks}.py` (old module kept as a noqa'd re-export shim). Pattern: `__init__` owns the router/constants and re-exports names tests monkeypatch (`api.jobs.{launch_pipeline,Path,open,db_lock,transition_status,...}`, `api.settings._config_path`); consumers resolve those at **call time** via `from api.jobs import X` inside the function body so monkeypatching keeps working. `api/settings` router has NO prefix (decorators carry full paths). `core/cross_page_analyzer.py` is a shim (noqa F401); new code imports from `core.rules`. PyInstaller hiddenimports in `pbc-server.spec` list every leaf module (packages don't recurse) + `core.notify` (function-body dynamic import). **PIL removed from PyInstaller excludes** (Round 5: image upload→PDF conversion needs Pillow at runtime, previously broken in frozen exe).

**Round 5 (中文化收尾 + 流式补全, 2026-08)**: page markers made **visible** to LLM (`<!-- 第 N 页 -->` → `## 第 N 页` in mineru_client, page_analyzer already used that format — empty pages keep `（此页无文本内容）` placeholders); OCR truncation surfaced as `_ocr_truncated` flag (`_clean_html` returns `(cleaned, truncated)` tuple) → review banner "此页 OCR 内容过长已截断"; Paddle plain-text fallback (no `<table>/<tr>/<div>`) gets an adaptive prompt (`提取以下 纯文本内容 中的结构化数据` + `text` fence + explicit "表格结构已丢失" system warning) instead of being falsely labeled HTML; SSE snapshots gained `phase` (`ocr`/`analyze`/`cross`/`done`, derived: analyzing + pages_analyzed≥total_pages ⇒ cross-page analysis) and `self_heal_progress` (`{done,total,pages}` merged into jobs.ocr_progress as a `self_heal` sub-key by `_update_self_heal_progress`, cleared at end — upload/review pages show "空页自愈 x/y" instead of looking stuck); `ocr_backend_display` (zh_map `OCR_BACKEND_ZH`, incl. `cached`) in status/review/uploads; status-machine + HTTP error details localized (transition details like "开始 OCR 识别", "任务不存在", "问题记录不存在", Paddle/OcrClient poll messages, SSE "任务不存在" error frame); feishu_mode default unified to `webhook` (app_bot leftovers cleaned); user_rules total length cap 8000 chars with live counter; settings test-provider probes unsaved form values via `dataclasses.replace`; **frozen exe smoke-tested with image upload** (PORT=58766 smoke job: PNG → total_pages=1 → ocr_running, PIL path verified).

**Round 3 (P1-4~P1-8 + P2-1~P2-6)**: image upload (jpg/png/webp/bmp/tif/tiff → backend converts to PDF), MinerU structural completeness check, table-first truncation, review→pending full re-analysis state machine, signed-URL redaction, stuck-job recovery notifications + provider-test audit, unified GET/DELETE endpoint guards, recover_stuck_jobs process-start cutoff, CORS port constants.

**Round 3 audit round 3 (A1/B1/B2/C1/C2/C3/D3/A3)**: empty-page self-heal extended to Paddle (fitz single-page re-submit), `[OCR 警告]` prefix moved OUT of the fenced OCR data zone into the system-warning zone (`_OCR_WARNING_RE` in page_analyzer + `_ocr_warning` result key → review banner), schema-validation-failure fix-hint retry (1 retry with error echo, `_schema_warn` marker if still invalid), `run_ocr_pages` returns `(page, text, discarded_count)` so self-healed pages re-attach the OCR warning prefix, severity counts moved after dedup (log matches real DB writes), archive-nonexistent test corrected to 404 (unified guard behavior).

**Round 3 localization wrap-up (竞价收尾 / adversarial #2)**: `core/zh_map.py` single source for Chinese enums (severity/finding-status/job-status/finding-type) consumed by report.md / Feishu notify / InvalidTransitionError messages; `api/jobs.py` upload error messages + image edge cases (multi-page TIFF / animated WEBP → 400 explicit reject, transparent PNG composited on white background, pixel check on HEADER size before decode to avoid full-decode DoS); MinerU `full.md` separator split keeps empty pages as `（此页无文本内容）` placeholders (page numbers no longer shift); table truncation regex widened to `<table[\s>]` (attribute tables) + open table gets synthetic `</table>` in plain-truncation fallback (closed-table count uses anchored regex — `str.count("<table")` double-counts `</table>` substrings); OCR token masked write-back protection in settings (paddle_ocr_token/mineru_token, was feishu + api_key only); `recover_stuck_jobs` UPDATE now guarded by `status IN (...)` (stale snapshot can't clobber a job a concurrent path already advanced); SSE `_get_job_progress` projects columns instead of `SELECT *`; LLM timeout log path masked with `_mask_secrets`; protocol dropdown labels Chinese.

**Round 4 (UX + robustness)**: cancel checkpoint injection (`AnalysisCancelled` + `cancel_check` three checkpoints in `page_analyzer.analyze_page`, cancelled pages not counted as failed — single HTTP call remains uninterruptible), prompt page-number injection + `findings[].page` backend enforcement (`_validate_page_result` + force-overwrite after sanitize), LLM hallucination grounding check (`_grounding_check`/`_value_grounded`: ≥4-digit substring match, shorter numbers boundary-checked; `_grounding_warn` → review banner), UX P1 set (no `target="_blank"` in Electron, corrected/note info in AJAX finding cards, `safeAutoReload` skips reload while typing or in dialog, PDF zoom 0.5-2.0x buttons), finding locate v1 (click card → OCR panel mark + scroll), P2 quick wins (empty report.md explicit "no findings" text + total_pages from `jobs.total_pages` instead of page_cache COUNT which undercounts failed-OCR pages, image→PDF conversion 120s timeout via `asyncio.wait_for` → 408, review.js `has_more` notice when >50 findings truncated).

**Round 6 (规则第三轮 + 页 9 截断修复, 2026-08)**: 挂载两条新规则 —— R9a `signature_order`（同一 operator 跨步骤签名时间必须递增，`core/rules/rule_time.py._check_signature_order` + `_ROLE_RANK`；type 合入 `signature_time_anomaly`）和 R8b `check_consistency`（同一复核项前后勾选状态切换必须落在规定的 pendingPage/allowable_transitions 内，`core/rules/rule_doc.py._check_check_consistency`；type 合入 `completeness`）；**value_source 三层方案**（印刷体信任/手写体存疑）：① v3 prompt 的「列级可信度（value_source 必填）」项（标注每列 printed/handwritten），② base.py `_infer_value_source`（列名关键词启发式：实际/实测/记录/填写/手写/结果/偏差 → handwritten；规格/标准/范围/指导/要点/要求/检查项目/项目 → printed；LLM 50% 概率不输出，backfill 兜底，内存级不写回 DB）+ `_backfill_value_source`，③ rule_spec.py `_severity_for_out_of_spec(bounds, actual, spec, value_source)`：printed → warning 不降噪；handwritten/unknown → ≤10% `_EDGE_MARGIN` 边缘偏离降 info。`analyze_cross_page` 输出 value_source 覆盖率统计日志（parameters n/m、cells n/m）；**页 9 大矩阵页截断/超时修复**：页 9（9 时间点 × 8 列大表 + 12MB OCR HTML）LLM 输出 426-485s 且被 max_tokens=6000 截断在字符串中间 → JSON 不可恢复 → fix-hint 重试每次 240s 超时 → 12 分钟整页失败。修复：$`_PAGE_MAX_TOKENS=8000`/$`_PAGE_TIMEOUT=480.0`/$`_PAGE_RETRIES=2`（page_analyzer 两处 chat_json）；llm/client.py 新增 `_repair_truncated_json`（字符串中间截断 → 回退到开引号 + 补 null；尾部完整数字保留；legacy 补括号兜底）+ `_parse_json` block 提取加条件（text 以 `{`/`[` 开头时跳过 block 提取，防误抓内嵌 `[]` 返回空 list）+ 恢复时注入 `_truncated_recovered: True`（analyze_page 打 `_truncated_warn` 日志）。修复后同页 258s 成功、payload 12191 bytes；**server.py CORS 一致性修复**：`python server.py`（无 PORT env）监听 58765 但 config.app.port 默认 8000 → CORS allowlist 只放行 8000 → 浏览器设置页 POST/PUT 被 CORS 拦截。修复：`os.environ.setdefault("PORT", str(port))` 必须在 `from config import config` 之前（否则 config 模块已按 8000 初始化；Electron main.js 传 PORT=58765 天然一致）。

**Round 8 (对抗审查 #3: 冻结版 e2e + git 泄露扫描, 2026-08-20)**: 冻结版注入路径发现并修复 3 个前端运行时缺陷 + 2 个后端 P0/P1——① **P0 `/api/jobs/live` 路由被遮蔽**：模块拆分后 `listings.py` 顶层 `from api.jobs.status import ...` 令 `/{job_id}` 先注册（FastAPI 按注册顺序匹配），`GET /api/jobs/live` 命中动态路径 → 404 "Job not found" → upload 页 EventSource 404、实时进度退化 10s 轮询。修复：listings.py status 符号改函数体内延迟解析（与 monkeypatch 约定一致），`api/jobs/__init__.py` 注明"listings 必须先于 status 导入"硬约束；回归测试 `TestLiveRouteNotShadowed` 直接断言路由注册顺序。② **P1 `POST /api/settings` 带 `mineru_model_version`/`mineru_language` 必 500**：`config.MINERU_MODEL_VERSIONS` 属性访问 dict → AttributeError（测试走 update_config 内存路径未暴露）；修复：引用 `config.py` 模块级常量 `MINERU_MODEL_VERSIONS`/`MINERU_LANGUAGES`（Settings 页 MinerU 下拉保存此前必炸）。③ **P0 upload.js `log.info` 不存在**：logger 仅 log/log.warn/log.err（upload.js:9-14），SSE onmessage 回调中 `log.info` 抛 TypeError 使整帧状态更新失效（与①叠加：路由修复后此 bug 才触达）→ 改 `log(...)`。④ review.js:283 `未知()` 未定义函数（ReferenceError 被外层 catch 吞掉）→ 改 `statusZh[d.status] || d.status`。⑤ review.js updatePageLevelUI 补 three OCR banners（empty/sparse/warning) AJAX 翻页同步（此前上一页横幅残留误导复核）。**git 历史泄露扫描结论**：1 项真实泄露——初始 commit `8651e3e` `spike_test.py:8` 硬编码 SiliconFlow key `sk-evnpxdew…`（已 push 至公共 GitHub 远端，需轮换）；PaddleOCR token 从未进 git（历史中的 26f37846 系掩码测试演示值）；`.env`/`config.json` gitignore 保护完好。验证：1120 tests/90.51%；冻结版 e2e ×3（PDF×2+图片×1）状态机/去重/降级全通，全部 ERROR 源于 LLM key 401（已失效，待轮换）；SSE 修复冻结版实测 `HTTP 200 text/event-stream` + 首帧 `retry: 2000`；设置持久化（POST→重启→GET）验证通过。

**Round 7 (value_source OCR 结构化信号优先 + 上下文按模型适配, 2026-08)**: `value_source` 从「LLM 猜」升级为「OCR 结构化信号优先」——调研结论：MinerU content_list_v2 无行级 score（score 在 middle.json 需额外参数）、PaddleOCR-VL 官方确认 VLM 不给 confidence、**MinerU `###` 低置信度占位符（已清洗为 `[手写内容未识别]`）是唯一可用单元格级机器信号**。新模块 `core/hw_signal.py`：`_extract_low_conf_tokens(text)` 把标记转为确定性 token —— ①列信号：管道表/HTML 表内标记单元格 → 表头名（colspan/rowspan 破坏列对齐则禁用列映射，退化为标签信号）；②标签信号：`审核意见:[标记]` 与 `签名[标记]`（无冒号，真实记录页 39/40/41/47 形态；CJK 标点截断防吞描述句）两种形态都提取标签。规则层 `_backfill_value_source` 优先级变为 **0. OCR 信号（`_ocr_low_conf_cols`，含 `_matches_low_conf` 双向包含匹配）> 1. 单元格标记 > 2. LLM 标注 > 3. 关键词兜底**（机器事实 > 模型猜测）。analyze_page 在清洗后文本上提取并注入 `_ocr_low_conf_cols` 内部键（与 `_ocr_warning` 同机制）；rules 覆盖率日志新增「OCR handwriting signal: n/m pages」；**上下文预算按模型适配**：`config.app.llm_context_window`（`LLM_CONTEXT_WINDOW` env / config.json 顶层键，默认 128000）→ `llm_checks._summary_max_chars(window)` = max(8000, window×0.35×1.6 字符)（≤35% 窗口预算，防 200 页 job/32K 小窗模型溢出；无窗口参数时回退 `_SUMMARY_MAX_CHARS=100_000` 保持旧行为）。验证：1118 tests/90.44%；定向 e2e 5 标记页（真实 OCR→真实 LLM）信号强制 64/64 值 handwritten（页 36 封面仅得 5 批准类 token、无参数可强制，符合预期）；图片 e2e ×2 + 51 页全量 e2e：value_source 356/356+450/450 覆盖率 100%、summary 24.2K tokens（<71K 预算不截断）、0 失败页；打包 3 轮成功（含 build.ps1 58765 占用即 FAIL 的坑——打包前须停 dev server）+ frozen e2e（APPDATA 隔离、图片上传全链路 review、12.4KB payload 无截断）。pbc-server.spec hiddenimports 新增 `core.hw_signal`。

**Round 14 (规则审查 + 分发验证 + GMP 依据引用, 2026-08-21)**: ① **修复 Round 13 遗留生产 bug**：新增的表格标题 `#### 表格` 被 `_sanitize_unrecognized_handwriting` 误替换为 `[手写内容未识别]# 表格`（旧豁免 `startswith("### ")` 只覆盖 H3，`####` 前缀部分命中 `###` 正则）→ 修复：豁免规则升级为 `_MD_HEADING_LINE_RE = ^#{1,6}\s`（全部合法 markdown 标题行豁免，行内裸 `###` 占位符仍正常替换；`core/mineru_client.py`），同步 2 处测试断言（`"###" not in md` 对合法标题误判）。② **新增 R10 `step_gap` 工序缺号规则**（`core/rules/rule_time.py._check_step_number_gaps`，内置规则 14→15 条）：step_no 序列缺口 → 提示缺页/漏页/OCR 漏识别（warning）。防误报四重设计：子工序 3.1/3.2 归并为整数（`_STEP_NO_RE` 首段数字）、同工序号跨页续表去重（setdefault）、数值工序号 <3 个不检查、缺口数 > 已知数一半降级 info（附表/设备编号体系混入嫌疑）。连续缺口段合并显示（3,4,5 → "3-5"）。前端中文名映射双端同步（review.html `type_zh` + review.js `typeZh`，`.get(f.type, f.type)` 兜底故无 SSR 也不炸）。③ **空页不再强制 partial_review**（`core/pipeline/stage3.py` 门禁 3）：空页（`_ocr_empty`）如封面/目录不是失败，review UI 已有横幅供人工确认；partial_review 条件收窄为 `failed_pages or dual_diff`。④ **SSE 聚合端点补测 5 例**（`tests/integration/test_api_jobs_coverage.py::TestStreamAllLiveJobs`）：`/api/jobs/live` 403 守卫 ×3（含 /archived/list、/stats/overview）、生成器快照输出（retry 头 + 自增 id + jobs 载荷）、DB 异常不中断流（_live_jobs_snapshot 首轮抛错 → 跳过 → 次轮恢复）。无限流无法走 httpx ASGITransport（等 app 完成才交付 Response）→ 直接调用路由函数手动迭代 `body_iterator` + FakeRequest.is_disconnected 计数退出。⑤ **设置页小字 WCAG 整改**：7 处用户必读文本 11px→12px（四个 section 副标题/飞书接入步骤/Webhook 说明/底部保存行为说明；`templates/settings.html` + `static/settings.css`），徽章类元数据保持 11px。⑥ **分发全链路实证**：`.\build.ps1` 三段构建全过（Tailwind 18.6KB / PyInstaller 106.5MB 冒烟 ~1s / Electron win-unpacked 380.9MB），frozen 冒烟 8/8（含 %APPDATA%/PBC 重定向验证）。⑦ **GMP 法规依据引用（借鉴参考产品"7 类知识库检索"的最小可行版）**：新模块 `core/rules/gmp_basis.py` — `GMP_BASIS_MAP` 按 finding type 映射法规依据（GMP 2010 批记录管理 / ALCOA+ 数据可靠性 / EU GMP Ch.4 / 偏差管理 / 江苏省记录填写规范；**引用规范名+原则名，不硬编码条款号** — 错误条款号在 GMP 审计比无依据更糟）；`attach_gmp_basis` 幂等附加（非 dict 防御；ocr_noise/user_rule 不映射，user_rule 依据是其自身规则文本）。schema v7：`findings.gmp_basis TEXT`（`_migrate_v7` 守卫式 ALTER，PRAGMA user_version 6→7）。落库三处：stage3 主 INSERT（rule/llm_cross/llm_fallback）+ dual_diff completeness 行 + stage2 llm_page 流式行。展示三端：report.md"法规依据:"行 + review SSR 模板 + review.js AJAX 卡片（border-l 引用样式）。select * 查询自动带出新列无需改 review/report API。验证：1219 tests/90.25%（+9 GMP 测试：映射覆盖面/规范名 sanity/幂等/防御/v6→v7 迁移/幂等重迁移）。

**Test status**: 1255 passed, 90.01% coverage (target ≥90%).

**Web 版（飞书入口）决策（2026-08-18）**: 调研已完成（WEB.md，D1~D5 已拍板），但 **Web 版暂缓实施** — 当前优先桌面端迭代，不主动开展 Web 化（auth_mode/守卫改造/移动端适配等）。后续若启动，按 WEB.md §6 实施路线推进，决策结论无需重开讨论。

---

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest, coverage, httpx

# Run dev server (from project root)
uvicorn main:app --reload --host 127.0.0.1 --port 8000

# Or via server.py (matches bundled entry point)
python server.py            # listens on 127.0.0.1:58765

# Run tests
pytest
pytest --cov=. --cov-report=term --cov-report=html

# Release gate (packaging signal) — offline; runs structure checks + tests/coverage
python scripts/release_gate.py                 # full; writes devlogs/gate_report_<ts>.json
python scripts/release_gate.py --skip-tests    # structure checks only (seconds)

# Runtime per-job data-quality gate (needs a running server + a finished job)
python scripts/golden_gate.py --job-id <id> --expect-pages 51

# Build local Tailwind CSS (15.8KB, no CDN)
npx tailwindcss -i ./static/input.css -o ./static/app.css --minify

# Build Windows installer (must run in real PowerShell, NOT IDE Sandbox)
.\build.ps1                # full build: css + pyinstaller + electron
.\build.ps1 -SkipCss       # skip tailwind rebuild
.\build.ps1 -Clean         # clean rebuild from scratch

# API docs (Swagger): http://127.0.0.1:8000/docs
```

Test coverage target: **≥95%** (enforced by `pytest.ini --cov-fail-under=95`). Current: 95.12% (1550 passed, see `tests/` with unit + integration suites).

> Sandbox note: to read coverage, prefer `python scripts/release_gate.py` (which uses
> `coverage run` + `coverage report`). Avoid `pytest --cov` under the IDE sandbox — its
> `pytest_cov.finish()` → `cov.combine()` deletes its own parallel data file, which trips the
> sandbox bulk-delete guard and aborts with `INTERNALERROR` before the coverage table prints.

---

## Environment Setup

Copy `.env.example` → `.env` and fill in:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Active provider name (must be one of the registered providers below) |
| `DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` | Built-in LLM providers (OpenAI-compatible) |
| `LLM_PROVIDERS` | Comma-separated list of additional providers to register (e.g. `glm,kimi,qwen,mimo,anthropic`) |
| `<NAME>_PROTOCOL` | `openai` (default) or `anthropic` — wire format for custom providers |
| `<NAME>_API_KEY` / `<NAME>_BASE_URL` / `<NAME>_MODEL` | Per-provider config (prefix = UPPER(name)) |
| `PADDLE_OCR_TOKEN` / `PADDLE_OCR_API_URL` | PaddleOCR-VL async API |
| `MINERU_TOKEN` | MinerU OCR backend (optional, set `OCR_BACKEND=mineru`) |
| `DATABASE_PATH` | SQLite file (default: `data/pharma.db`) |
| `OUTPUT_DIR` | PDF storage + job artifacts (default: `output/`) |
| `MAX_CONCURRENT_JOBS` | Max simultaneous active pipelines (default 3) |

**Adding a new LLM provider** (no code changes needed):
1. Add the provider name to `LLM_PROVIDERS` (e.g. `LLM_PROVIDERS=glm,kimi`)
2. Set its 4 env vars: `GLM_PROTOCOL=openai`, `GLM_API_KEY=...`, `GLM_BASE_URL=...`, `GLM_MODEL=...`
3. Or use the in-app Settings page → "添加提供商" dropdown (writes to `.env` + live reload)
4. Set `LLM_PROVIDER=glm` to activate it

Both `openai` (DeepSeek/SiliconFlow/GLM/Kimi/Qwen/MiMo) and `anthropic` (Claude) protocols are supported via the adapter layer in `llm/adapters/`.

**Frozen mode (PyInstaller bundle)**: config is read from `%APPDATA%/PBC/config.json` (Windows), `~/Library/Application Support/PBC/config.json` (macOS), `~/.local/share/PBC/config.json` (Linux). Database and output files redirect to `%APPDATA%/PBC/` as well. Use the in-app Settings page to edit credentials at runtime — saves are applied live without restart. **Gotcha**: `config.json` must be UTF-8 **without BOM** — PowerShell 5.1 `Set-Content -Encoding UTF8` writes a BOM that makes the frozen server exit with code 1 at startup (config parse fails before logging initializes). Use the Settings page or write with `encoding="utf-8"` from Python.

**Runtime config source is `config.json` (Phase 9)**, not `.env`. On first run, a legacy `.env` is auto-migrated into `config.json` (loaded once; thereafter `config.json` wins). `config.py` exposes `update_config()` to mutate the in-memory config for live reload when the Settings page saves.

---

## Architecture

### Frontend (Jinja2 + Tailwind + vanilla JS)

Templates, styles, and scripts are **strictly separated** — no inline CSS/JS except a single `window.__PBC__` bridge per page.

- `templates/upload.html` + `static/upload.js` + `static/upload.css` — upload + job history list
- `templates/review.html` + `static/review.js` + `static/review.css` — 3-column review (page nav | PDF | findings)
- `templates/settings.html` + `static/settings.js` + `static/settings.css` — LLM/OCR credential editor
- `static/confirm-dialog.js` — shared Notion-style confirm/prompt dialogs + toast (`window.PBC.confirmDialog / promptDialog / showToast`), included by all three pages
- `static/app.css` — locally built Tailwind output (15.8KB, do not edit directly)
- `static/design-tokens.css` — shadcn HSL variables

Design system: minimalist, white background, black primary, flat lists (no cards), `border-b` hairline separators, pill-shaped nav buttons. No dark mode. Round 4 P2/P3 additions: page headers keep in-content breadcrumb + page title (BatchSentry / page name) with action button on the right — no global app bar, review page keeps its native 48px top bar; settings provider rows flattened to `border-b` list items (no nested cards) with left indicator for active; `tabular-nums` on all numeric data (times/pages/measurements); skip-link (`.skip-link` in input.css, all pages); page-shaped skeleton screens for upload history list + PDF loading (no spinner). Critical banners use static red glow — no infinite pulse (E2E probe in review.js checks the static rule).

Frontend logs use `[PBC]` prefix with color coding (blue=info, orange=warn, red=error).

### Backend (FastAPI + aiosqlite)

Entry point: `main.py` (dev) or `server.py` (bundled, port 58765).

**Three-stage pipeline** (`core/pipeline.py`) runs as FastAPI `BackgroundTask`:

1. **Stage 1 — OCR** (`core/ocr_client.py` or `core/mineru_client.py`): submit PDF → poll → download JSON. 10-minute poll timeout, 5s interval. Blocking `requests` wrapped via `asyncio.to_thread`.
   **Dual-OCR failover**: `_get_ocr_chain()` builds a primary+secondary chain (primary = `OCR_BACKEND`, secondary = the other backend if its token/api_url is configured). `_run_ocr_with_failover()` retries the whole job on the secondary when the primary raises, returns 0 pages, or loses >20%/5 pages vs the PDF physical page count (`_pdf_page_count`). The actual backend is stored in `jobs.ocr_backend_used` and surfaced in `/api/jobs/{id}` + SSE snapshots (GMP traceability). Sliced mode (`OCR_SLICES>1`, MinerU only) keeps its own path without failover.
   **MinerU structural completeness (Round 3 P1-4b)**: `_split_pages_by_content_list` returns `(pages, n_tables, n_paragraphs)`; MinerU pages whose `n_tables == 0` while the PDF physical page count ≥2 are treated as incomplete → whole-job failover to the secondary OCR backend.
   **Empty-page self-healing** (Phase 11): MinerU drops pages on >100MB PDFs (server-side defect; a page OCR'd standalone returns 1111-1702 chars). After page_cache write, pages with `<100` chars (tag-stripped text length) are re-OCR'd as small slices via `run_ocr_pages()` (two rounds: batch_size 3 then 1; mineru + any file size). Recovered pages UPDATE page_cache; audit_log records `stage1_empty_pages` / `stage1_empty_recovered`. **Round 3 audit A1**: extended to Paddle (fitz single-page slice re-submitted once, no slice API); **D3**: `run_ocr_pages` returns `(page, text, discarded_count)` and self-healed pages re-attach the `[OCR 警告]` prefix when the slice still dropped low-confidence blocks (previously silently treated as complete). Truly empty pages stay as-is and the review UI shows an `_ocr_empty` banner (manual review path).
2. **Stage 2 — Per-page LLM** (`core/page_analyzer.py`): each page's HTML table → LLM extraction prompt → structured JSON with `steps[].measurements[]` time series. Uses string concatenation (NOT `.format()`) to avoid brace collision with HTML. 240s timeout (fix-hint JSON-recovery retries inherit it), 3 retries with exponential backoff. **Round 3 audit B1**: the `[OCR 警告:...]` prefix injected by the pipeline is stripped out of the `<PBC_UNTRUSTED_OCR>` fenced data zone (`_OCR_WARNING_RE`) and re-injected as a `[系统警告]` in the system zone — plus an `_ocr_warning` result key surfaced in the review banner (C3) — so LLM treats it as an instruction-level signal instead of ignorable OCR data. **C1**: schema-validation failures trigger 1 fix-hint retry echoing the errors (`_schema_warn` marker persists the page as analysed-but-flagged if still invalid).
3. **Stage 3 — Cross-page analysis** (`core/cross_page_analyzer.py`): rule-based time reversal + LLM-based semantic anomalies + user-defined compliance rules injected into the LLM prompt (`source=user_rule` findings; `findings.user_rule_id` carries the matched rule id; `prompt_version` carries a rules content hash for GMP traceability). All write to the same `findings` table with `source` field (`rule` / `llm_page` / `llm_cross` / `llm_fallback` / `user_rule`).

**Job completion notifications (Phase 12, `core/notify.py`)**: on terminal state (review / partial_review / error / cancelled), `notify_job()` pushes a Feishu summary. Two channels: webhook group bot or app_bot DM (event subscription). 90-min dedup cache; notification failure never blocks the pipeline. Config in `config.json` under the `feishu` keys, editable from the Settings page ("飞书通知" section, includes a "测试连接" button hitting `POST /api/settings/test_feishu`).

**Live progress (SSE, Phase 10)**: `GET /api/jobs/{id}/stream` pushes a progress snapshot every 3s (default `message` event, `done` event + close on terminal state, `error` event when the job is missing). Review page subscribes and hot-refreshes the current page's findings as `pages_analyzed` grows (page-level streaming — no need to wait for the whole job). Upload page tracks active job rows the same way: inline `OCR 12/51` counts, per-page analysis counts, and auto re-enabling of archive/delete buttons at terminal state. Frontend logs page-level events via `[PBC]` logger.

**State machine** (`pipeline.VALID_TRANSITIONS`): `pending → ocr_running → ocr_done → analyzing → review | partial_review | error | cancelled`; cancel is a two-step `... → cancelling → cancelled` (pipeline checks `_is_cancelled` between stages/rounds, keeping partial results). Terminal states can `archived`; `error`/`cancelled` → `pending` for retry. **Round 3 P1-6**: `review → pending` allowed (full re-analysis; `partial_review → pending` also allowed — only missing pages). Invalid transitions raise `InvalidTransitionError`.

### Data Flow

```
PDF upload → output/{job_id}/filename.pdf
                ↓
         page_cache (raw_html per page)        ← Stage 1
                ↓
         page_cache (structured_json per page) ← Stage 2
                ↓
         findings (severity, source, status)  ← Stage 3
                ↓
         audit_log (every state change + user action)
```

### Database Schema (`db/schema.sql`)

- **`jobs`**: id, filename, status, pdf_path, total_pages, md5 (duplicate-upload detection), failed_pages, stage1_ms/stage2_ms/stage3_ms, ocr_progress (JSON), ocr_backend_used (dual-OCR audit), error_message, created_at, finished_at
- **`page_cache`**: (job_id, page) → raw_html + structured_json + analyzed_at
- **`findings`**: id, job_id, page, type, severity, source, description, ocr_text, operator, status (`pending → confirmed | rejected | corrected`), reviewer_note, corrected_text, reviewed_at (+ `user_rule_id` when `source='user_rule'`)
- **`audit_log`**: id, job_id, finding_id, action, detail, created_at

SQLite via `aiosqlite` with WAL mode. Singleton connection in `db/client.py`.

### API Layer (`api/`)

| Router | Prefix | Purpose |
|---|---|---|
| `jobs/` (package) | `/api/jobs` | Upload (8MB chunked, 200MB max; PDF or image), status, cancel, retry, archive, unarchive, delete, page data, findings |
| `review.py` | `/api/jobs/{id}/findings` | List/get/update findings (confirm/reject/correct) + audit log + page measurements |
| `report.py` | `/api/jobs/{id}/report.{md,json}` | Export Markdown + JSON reports |
| `settings/` (package, no prefix) | `/api/settings` | Read (masked) / update `config.json` with live reload |

Server-rendered HTML pages:
- `GET /` → upload + job list
- `GET /jobs/{id}/review?page=N` → review UI (3-column)
- `GET /settings` → credential editor
- `GET /health` → health check

### Logging (`logging_config.py`)

Structured logging with `request_id` ContextVar. Middleware logs `[req_id] METHOD path → STATUS (duration_ms)` for every request (skips `/static/` and `/health`).

Handlers:
- Console (stdout)
- `logs/pharma.log` — all levels
- `logs/pipeline.log` — pipeline stage events only
- `logs/error.log` — ERROR+ only

API routes emit business logs (upload/cancel/retry/archive/delete/finding update/report generation) with `[job_id]` prefix.

### Security Posture

- **CORS**: allowlist generated from `config["app"].port` (Round 3 B8) — 127.0.0.1 + localhost on the actual serving port (dev 8000 / Electron 58765 via `PORT` env). Port constant lives in `config.py` (`port=_env_int("PORT", _env_int("APP_PORT", 8000))`); changing `electron/main.js` `SERVER_PORT` no longer breaks CORS. `file://` removed to prevent XSS via Electron renderer.
- **CORS headers**: restricted to `Content-Type, X-Request-ID` (not `*`).
- **Endpoint guards (unified)**: all state-changing endpoints (upload/cancel/retry/archive/unarchive/delete/settings/shutdown/health-probe) AND all GET read endpoints (jobs list/status/page image/SSE/findings/audit/reports, Round 3 P2-1) run `is_local_request()` — non-local `Host` → 403. GET read endpoints were previously unguarded (side-channel probing via `<img>/<script>` from hostile pages).
- **Upload**: 8MB chunked streaming, `Path(file.filename).name` sanitization, 200MB hard limit, empty-file rejection, magic bytes check (`%PDF-` for PDF; per-format prefixes for jpg/png/webp/bmp/tif/tiff), MD5 content-hash duplicate rejection (409, `force=1` bypass). Empty filename falls back to `{job_id}.pdf` (still magic-checked).
- **SQL**: all queries parameterized (`?` placeholders).
- **Secrets**: `.env` never committed; Settings API masks keys (`sk-abcd...wxyz`).
- **PDF preview**: pages are rendered by PyMuPDF to JPEG (quality 82) via `GET /api/jobs/{id}/page/{n}` and shown as `<img>` (zoom capped at 2000px, cached 6 docs / 30min TTL, render in thread pool). `content_disposition_type="inline"` for the raw PDF endpoint.
- **XSS**: `render_page_links` filter escapes HTML before inserting links; `review.js` `renderFindings` escapes all LLM-sourced text via `esc()` helper; `upload.js` `setStatus` uses `textContent` not `innerHTML`.
- **Path traversal**: `delete_job` validates `job_dir` is inside `output_root` before `rmtree`.
- **Concurrency**: `MAX_CONCURRENT_JOBS` env var (default 3) caps active pipelines to prevent memory exhaustion.
- **Downstream probes**: `GET /api/health/downstream` checks OCR + LLM reachability; Settings page has "测试连接" button.

### Secret Rotation Procedure

If a key was committed to git history (e.g. the original `PADDLE_OCR_TOKEN` leak in PLAN.md):

1. **Rotate at provider** — log into PaddleOCR / DeepSeek / SiliconFlow console, revoke the old key, issue a new one. Just deleting from the repo is NOT enough — git history is immutable.
2. **Update local `.env`** (dev) or `%APPDATA%/PBC/.env` (frozen) via Settings page.
3. **Verify** with the "测试连接" button on Settings page.
4. **Audit**: `git log --all -p | grep <old-key-prefix>` to confirm no other leaks exist.

### Downstream Service Health

Before submitting real work, use `GET /api/health/downstream` to verify:
- OCR service URL is reachable + token is valid (PaddleOCR) or configured (MinerU)
- LLM service accepts auth + responds to a 1-token ping

The probe does NOT submit real OCR/LLM work — it just verifies auth + connectivity in <8 seconds.

### Packaging

- **Backend**: PyInstaller via `pbc-server.spec` → `dist/pbc-server/pbc-server.exe`. Hidden imports include `core.mineru_client`, `api.settings`. Resource paths resolve via `sys._MEIPASS` in frozen mode.
- **Frontend portable**: electron-builder via `build.ps1` → `dist-electron/win-unpacked/` (folder portable, double-click `BatchSentry.exe`, no installer — zip the whole folder for distribution). Electron main (`electron/main.js`) spawns `pbc-server.exe`, health-checks, creates `BrowserWindow`, cleans up child processes on exit. Icon `icon.ico` loaded conditionally. App version is `1.0.0` (single source in `main.py` `APP_VERSION`).
- **Run build in real PowerShell** (not IDE Sandbox) — AppData write restrictions in sandbox break packaging.

### Key Design Decisions

- **LLM provider architecture (Phase 7)**: providers are NO LONGER hardcoded. A dynamic registry in `config.py` (`_load_all_providers`) loads built-in providers (deepseek, siliconflow) + any declared via `LLM_PROVIDERS` env var. Each provider specifies a `protocol` (`openai` or `anthropic`) that selects the right adapter from `llm/adapters/`. The `LLMClient` (`llm/client.py`) owns retry/backoff + JSON parsing + audit logging; the adapter owns wire-format translation. Adding a provider requires only an env var entry — zero code changes.
- **Electron splash rendering (2026-08)**: in VM/remote-desktop/no-GPU environments Chromium falls back fully to software rendering (`gpu_compositing=disabled_software`, verifiable via `app.getGPUFeatureStatus()`); the software compositor throttles frame rate to ~30fps (16-21fps during the first second), which makes CSS linear-rotation spinners visibly stutter. Fix: win32 `disable-frame-rate-limit` command-line switch lifts the compositor frame cap (measured 30 → 36-42fps; software rasterization itself caps ~40fps) + the splash spinner uses `steps(8)` discrete rotation so perceived motion is framerate-independent. Verified with a temporary `beginFrameSubscription` FPS harness.
- **Protocol adapters** (`llm/adapters/`): `OpenAIAdapter` wraps `openai.AsyncOpenAI` (handles DeepSeek, SiliconFlow, GLM, Kimi, Qwen, MiMo, OpenAI). `AnthropicAdapter` wraps `anthropic.AsyncAnthropic` (handles Claude) — loaded lazily so the `anthropic` package is optional. Both return a uniform `ChatResult` (content + token usage + model).
- **Settings API auth**: POST `/api/settings` is guarded by `is_local_request()` (core/security.py) — only requests with `Host: localhost:*` / `127.0.0.1:*` and an allow-listed `Origin` are accepted, blocking CSRF from arbitrary web origins.
- **SSRF protection**: `validate_external_url()` blocks base_url / OCR API URLs pointing to link-local (169.254/16), loopback (127/8), private (10/8, 192.168/16, 172.16/12), or unspecified (0.0.0.0) addresses.
- **.env atomic write**: Settings POST uses a PID + UUID-suffixed tmp file + `os.replace` for atomic rename, preventing concurrent-write corruption.
- **GMP audit trail**: every LLM call (per-page + cross-page + fallback) is recorded in `llm_call_audit` table with provider, protocol, model, prompt_version, token usage, latency, success/error — for traceability.
- **JSON parsing resilience** (`llm/client.py:_parse_json`): handles markdown fences, leading text, both `{...}` and `[...]`, truncated JSON recovery. Parse failures trigger a fix-hint retry (`chat_json`, up to 2 extra single-shot calls, no API-level backoff) — found by the 51-page real-file regression (page 19 returned ```json-fenced output that survived the API call but failed parsing).
- **Upload dedup**: MD5 computed during chunked streaming (schema v3, `jobs.md5`); identical content → 409 with existing job hint. Dedup check + INSERT are inside `db_lock` (no TOCTOU race).
- **Image upload (Round 3 P1-4)**: jpg/png/webp/bmp/tif/tiff accepted; backend converts to PDF (Pillow `exif_transpose` for camera orientation + PyMuPDF at 300 DPI) so pipeline/OCR/LLM/review/report stay untouched. Original image archived in job_dir; `pdf_path` points at the converted `<job_id>.pdf`; MD5 is computed on the ORIGINAL image bytes (dedup works); audit_log records `source=image`. Magic bytes checked per format independently of extension.
- **HTML cleaning** (`page_analyzer.py`): strips `style=`/`width=`, simplifies img src, truncates to 12000 chars **table-first** (Round 3 P1-5: keep table content over body text; single oversized table falls back to plain truncation with explicit marker; multiple tables keep the fitting prefix). LLM knows info is incomplete. Prevents token overflow.
- **Rule + LLM hybrid**: rule-based checks (deterministic, no token cost) + LLM-based semantic anomalies. Both feed `findings` table with `source` field.
- **Resume**: pipeline skips pages that already have `structured_json` in `page_cache`.
- **Full re-analysis (Round 3 P1-6)**: `retry` on a `review`-state job = full re-analysis (clears findings + NULLs `structured_json`, keeps `raw_html` OCR cache, audit `analysis_reset`); `partial_review` still retries only missing pages.
- **Fault tolerance**: single page LLM failure sets `_parse_error` flag, cross-page analysis skips it, job continues to `partial_review`.
- **Crash recovery guard (Round 3 P2-x)**: `recover_stuck_jobs(process_started_at)` only marks jobs with `created_at` EARLIER than the process start as error — new uploads racing the async recovery task are never mis-marked.

---

## Conventions

- **Language**: Code comments and commit messages in English. UI strings and LLM prompts in Chinese.
- **Docstrings**: every module has a module-level docstring explaining its role.
- **Error handling**: stage exceptions set job status to `error` with truncated message. No partial success — failed pages are marked but pipeline continues.
- **Finding severity**: `critical | warning | info`. Finding status: `pending | confirmed | rejected | corrected`.
- **OCR client**: blocking `requests` calls. Pipeline wraps with `asyncio.to_thread`. Don't call its functions directly from async context without threading.
- **Dialogs**: never use native `alert()/confirm()/prompt()` — use `PBC.confirmDialog / promptDialog` (async Promise). Confirm dialogs for destructive actions focus the cancel button by default (Enter never misfires delete); Esc/overlay cancel; Enter follows focused button; Tab is trapped inside the dialog. All dialogs carry `role=dialog` + `aria-modal` + `aria-labelledby` (APG pattern).
- **Frontend logging**: `[PBC]` prefix with color coding. Critical DOM elements probed on `DOMContentLoaded` for E2E test visibility.

---

## Subdirectories

- `api/` — FastAPI routers (jobs/ package, review, report, settings/ package)
- `core/` — pipeline/ package, OCR/LLM clients, page analyzer, rules/ package
- `db/` — schema + aiosqlite client
- `llm/` — LLM client with retry + JSON recovery + GMP audit (`client.py`), protocol adapters (`adapters/`: `openai_adapter.py`, `anthropic_adapter.py`, `base.py`)
- `models/` — Pydantic schemas
- `templates/` — Jinja2 HTML
- `static/` — CSS, JS, design tokens (separated, no inline)
- `tests/` — unit + integration suites (pytest)
- `electron/` — Electron main process
- `samples/` — sample PDFs (gitignored binaries)
- `spike/` — experimental ad-hoc test inputs and reports (not part of app)

---

## v1.1 变更（M2–M8, 2026-09）

> 执行计划与逐任务验收见 `docs/PLAN_v1.1_EXECUTION.md`；变更摘要见 `CHANGELOG.md`。
> 纪律：先定位（实测真值）→ 再解决（最小改动）→ 再测试（含覆盖率门禁）→ 最后验打包信号。

**M2 Finding 质量可度量 + 降噪**：金标评测管线（`scripts/eval_findings.py`，`docs/FINDING_GROUND_TRUTH.json`），
P/R/F1=1.0；真实 51 页 784→268 findings（-65.8%），critical 全保留。降噪核心是
`core/finding_quality.py`（`CANONICAL_TYPES` 21 类为唯一来源）/ `core/finding_noise.py`。

**M3 OCR 尺寸/形态鲁棒性**：`tests/unit/test_ocr_robustness.py`（42 例）+ `tests/e2e_ocr_integrity.py`（20 断言）——
不可无损修复页必须显式携带非完整信号（不得静默标记成功）；样本生成 `scripts/gen_ocr_samples.py`（正常对照页须
嵌 300dpi 栅格，否则被正确判 `low_dpi`）。

**M4 规则扩展 R11–R17**：`core/rules/registry.py::RULE_REGISTRY` 成唯一入口（RuleSpec: id/type/severity/
description/check，basis 取 `GMP_BASIS_MAP`）；新增规则只追加注册表 + 写 `_check_*`，不再手改调用序列。
真实 51 页重放 +9 findings（399，+2.3%），全为真信号。离线重放：`scripts/replay_rules.py`。

**M5 多源 GMP 知识库**：`core/kb/data/raw/*.md`（手工策展 + provenance 头，**是源，必须入 git**）→
`scripts/seed_kb.py` 派生 `core/kb/data/<source_id>.json`（运行时载荷，也入 git）。检索 `core/kb/retriever.py`
（字符 bigram 倒排 + 标准 BM25 对数 idf `_K1=1.5,_B=0.4,_TOPK=4`，改动须重跑金标）；`TYPE_QUERIES` 必须用
**语料自身用词**（GMP 用"偏离"非"偏差"）；金标 `scripts/eval_kb_queries.py`（37 条，97.3%）。打包不变式：
`pbc-server.spec` 用 `core/kb/data/*.json` glob 枚举，门禁 `kb_packaging` 校验覆盖（新增源忘记入包会 FAIL）。

**M6 SSE 优化 + 对标落地**：T6.1 轮询常量 `api/jobs/_SSE_POLL_SECONDS=2` 单一来源（`test_sse_poll_constant.py`
源码扫描锁死，禁 `retry:<数字>` / `asyncio.sleep(<数字>)` 字面量）；T6.2 终态快照缓存 `(status,finished_at)`
为键（`test_api_jobs_listings_coverage.py` 锁稳态查询 16→1）；T6.3 `static/eta.js` 纯函数
`tickPhase/showElapsed/fmtElapsed` + 1s 本地 ticker（node 单测）；T6.4 三色分级
`REVIEW_TIER_BY_SOURCE`（rule/user_rule→rule，llm_*→llm）+ `rule_coverage()` 按 **type** 统计系统通过
（最小可陈述单元是 type 非 rule id），SSR/AJAX 双端同源，`test_review_tier.py` 跨文件机检；T6.5 趋势筛查
经真实库实测判 **v1.1 不做**（`docs/TREND_SCREENING_EVAL.md`）。

**M7 docling 第三对照引擎**：`core/pipeline/ocr_support.py` 新增 `OcrCapabilities` 能力表（`describe_backend/
supports_slicing/supports_page_subset/is_backend_available/remote_backend_configured`）为单一来源，
engine/stage1/dual_compare 的字面能力假设全部改由能力表驱动；`core/docling_client.py`（MIT，本地）——
`is_available()` 仅顶层探测（不拉 torch），未装 → `_get_ocr_backend` 抛 `OcrBackendUnavailable` →
`_get_ocr_chain` 回退默认 PaddleOCR（主链不受影响）；`scripts/compare_ocr_engines.py` 三引擎逐页对比
（复用 `dual_compare.compare_page` 阈值单一来源）；`core/health.py` 增 `probe_docling`。

**M8 打包放行（v1.1.0）**：版本号单一来源 `main.APP_VERSION`（与 `package.json` 一致性由
`tests/unit/test_version_consistency.py` 机检）；`build.ps1` 真实 PowerShell 重打包（非沙箱）；
真实 51 页 full-chain frozen e2e（`e2e_run.py --rounds real`）+ `ui_e2e.py`（Playwright 逐页三断言）；
tag `v1.1.0`。

**降噪落地 P0-2 / P0-3（v1.1.1）**：清单与依据见 `docs/NOISE_REDUCTION_TODO.md` /
`docs/NOISE_REDUCTION_RESEARCH.md` / `docs/NOISE_REDUCTION_SPIKE.md`。
- **抑制留痕（P0-2）**：抑制 ≠ 删除。`drop_unfounded_spec_findings()` 返回
  `(保留, 明细列表)`（**第二项是列表不是计数**），每条带非空 `reason` + 结构化 `evidence`，
  落 `finding_suppressions` 台账（schema v11；建表语句**只在 `db/schema.sql` 声明**）；
  复核页"已抑制条目"面板可查可回退；`GET/POST /api/jobs/{id}/suppressions[...]`。
- **区域级证据锚（P0-3）**：`core/pipeline/regions.py`（**纯函数、唯一定义点**）
  把 finding 锚回 OCR 版面区域；`page_cache.regions_json` 存归一化 bbox（0..1）。
  **只到区域级**（两后端都无单元格级 bbox）；**宽高比闸门**：`space_aspect` 与渲染图
  不一致（如服务端旋转过的页）时明示无法定位，不画错位的框。锚定是**读时推导**，
  SSR 与 AJAX 共用 `region_anchor`。

**上传限额：页数上限 + 单一真值（v1.1.2 续）**：依据与市面 10 家产品做法调研见
`docs/UPLOAD_LIMITS.md`。
- **单一真值 `config.UPLOAD_LIMITS`** 同时驱动后端强制 / 前端预检 / 页面文案。
  此前同一个 200MB 被**四处各自写死**（后端常量、后端消息、`static/upload.js`、
  `templates/upload.html`）—— 任一处改动即静默漂移成"前端放行、后端拒绝"。
  前端**刻意不设兜底数值**（兜底副本正是漂移来源）：注入缺失即跳过预检交服务端判定。
- **判定是纯函数** `config.check_upload_page_limits(page_count, limits)`，返回
  `(reject_detail, warn_text)`。硬上限 **200 页**（`MAX_UPLOAD_PAGES`）拒绝并给出
  真实页数 + 上限 + "按批次拆分"；软阈值 **80 页**（`WARN_UPLOAD_PAGES`）放行但
  响应带 `page_warning` + `audit_log` 留痕 + 前端跳转延迟延至 5s。
  **页数读取失败（=0）一律放行**（部分损坏 PDF 云端 OCR 仍有容错，此条由测试钉死）。
- **定标依据是实测而非照抄**：真实 51 页 Stage1 OCR 460.5s（**9.0 s/页**）、
  Stage2 逐页 LLM 1163.7s（**22.8 s/页**）。200 页 → OCR 预估 1800s，对既有
  `ocr_client.POLL_TIMEOUT_MAX=3600s`（**100 页即封顶**）留 2× 余量。
  ⚠️ 云厂商的 1,000~3,000 页**不可移植** —— 其单页成本 1–2s，**可移植的量是墙钟时间，不是页数**。
- **与厂商上限的关系**：`core/mineru_client.MINERU_MAX_UPLOAD_BYTES` 是 MinerU
  **厂商**上限（与本产品策略同值纯属巧合）；MinerU 是 failover 备选，准入上限超过它
  会导致"放行后流程中途失败"。护栏机检 `UPLOAD_LIMITS["max_bytes"] <= 厂商上限`。
- 护栏：`tests/unit/test_upload_limits.py`（21）+ `tests/integration/test_api_upload_page_limit.py`（5）。
  ⚠️ 200 页是**基于 51 页实测的线性外推**，未以 200 页文件实跑。

**规格可判性（M8 看图比对后固化，唯一来源均在 `core/rules/parsing.py`）**：
- `_parse_spec` 支持的写法：`A-B` / `A~B` / `A±B` / **空格 `A B`**（仅 `|B|<|A|` 才按 `A±B`，
  防 `10 20` 被误读为 `10±20`）/ **反向区间按 `A±B` 重建**（OCR 把 `±` 读成 `-`）。
- `_violated_bound`（被越过的界，供超差倍数评估）、`_sign_convention_uncertain`
  （负表压 × 印版正限值 → `spec_unverifiable` 交人工，勿静默判合规/超差）、
  `_decimal_loss_factor`（OCR 丢小数点软化，判据：**实测值不含小数点** 且 **超差倍数 ≈10×/100×**；
  反例 `45.6 vs 0~5°C`、`25 vs ≤5.0` 必须维持 `warning` 铁口）。
- **LLM 自报超差必须复核**：`core/rules/spec_guard.py::drop_unfounded_spec_findings` 用**规则层同一
  解析器**复核 `source=llm_page` 的 `param_out_of_spec`，命中状态 `in`/`soft` 才剔除；定位不到、
  不可判、符号存疑一律保留（fail-closed）。名称比对先过 `_norm`（去空白/下划线/连字符/括号），
  因结构化列名 `T2101a_压力` 与 LLM 文案 `T2101a 压力` 分隔符常不一致。
- **看图比对是定位规格缺陷的首选手段**：`docs/M8_VISUAL_VERIFICATION.md` 记录了方法与逐条实读结论。
  要点：**页面方向逐页不同**（p08 需 90°，p19 另一角度），最保真的读法是直接抽嵌入栅格
  （`doc.extract_image(page.get_images(full=True)[0][0])` → 3000×4000 原图），再按像素裁切放大 3~5×。
- **降噪的方向与禁区**（调研见 `docs/NOISE_REDUCTION_RESEARCH.md`）：降噪只能靠**引入独立证据**
  （第二读一致性 / 结构先验 / 列先验），**不得**靠"看起来像误报"的模糊判据——
  `_decimal_loss_factor` 的回归已实证软化过宽会吃掉真实超差。
  对齐粒度与 bbox 资格的**实测结论**见 `docs/NOISE_REDUCTION_SPIKE.md`，
  分项执行清单见 `docs/NOISE_REDUCTION_TODO.md`。两条硬结论：
  - **两个后端都不提供单元格级 bbox**（Paddle 整表一个 `block`、MinerU 整表一个 `span`），
    只有块/区域级坐标 → 对齐只能用**表级 bbox 粗筛 + 字段级标签匹配**，高亮只能到区域级。
  - **Paddle 的 `layout_det_res.boxes[].score` 是版面检测分，不是抽取置信度**
    （实测 mean 0.579 / p50 0.545，小文本块天然低分）——不可当数值正确性信号。
  ⚠ **已知缺口（待 P0-2）**：`spec_guard` 的抑制目前是**直接剔除、只留计数**，无 `suppress_reason` /
  无明细 / 不可回退；受监管场景（EU GMP Annex 11 第 16 条、中国附录《计算机化系统》第 15/16 条）
  要求"修改关键数据需批准并记录理由"。改为**留痕抑制**（`status='suppressed'` + 理由 + 可恢复 + 可抽检）
  是合规必需项，不是优化项。

**关键路径陷阱**：`release_gate.py` 的 `worktree_clean` 项要求**先提交再跑**，否则必然 FAIL；
`--python` 必须传 **Windows 路径**（POSIX `/c/...` 会判"python 不可用"）。
