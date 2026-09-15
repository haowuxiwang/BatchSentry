# Changelog

本文件记录 BatchSentry 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.2]

> 起点：`v1.1.1`。把 P0-3 遗留的唯一验收项（"抽 5 页人工核对高亮框与页面坐标系
> 一致"）**闭环**，并修掉它暴露的一个真实缺陷。

### 修复

- **服务端转正的页面现在能正确定位（P0-3 补完）**：OCR 坐标系被上游旋转过时
  （实测 51 页中第 8 页上报 `doc_preprocessor_res.angle = 270`，坐标系因此是
  `1920×1440` 横向，而页面是竖向），此前呈现层只做宽高比闸门 —— 该页的定位入口
  会**明示"无法定位"**。现按上报角度做**确定性逆映射**把框换回页面空间后再呈现：
  - `core/pipeline/regions.py` 新增纯函数 `map_bbox_to_page(bbox, rotation)`，
    四分支精确映射（`0→(u,v)`、`90→(1-v,u)`、`180→(1-u,1-v)`、`270→(v,1-u)`），
    由不变量推导并经真实产物逐点核对；载荷新增 `space_rotation`。
  - 锚定结果新增 `page_bbox`（页面空间，呈现层画这个）与 `page_aspect`
    （页面**应当**具有的宽高比；旋转 90/270 时为 `space_aspect` 的倒数）；
    `bbox` 保留为 OCR 空间原样框，供审计回查。
  - **角度未知时不得猜测方向**：`-1`（上游未启用朝向分类）或任何非规范值一律归
    `None`，退回宽高比闸门兜底（宁缺勿错）。
  - 前端 `static/review.js` 改用 `page_bbox` 定位、`page_aspect` 过闸。

### 新增（验收工具与证据）

- **`scripts/verify_anchor_orientation.py`**：用 Paddle 回传的 `inputImage`
  （未旋转原图）与 `outputImages.layout_det_res`（它在"旋转后"图上画的检测可视化，
  授权串无过期）**配准实测**服务端施加的旋转，并与上报角对表。实测第 8 页
  `270°CCW`（轴优势 3.15×）与上报角一致、第 7 页 `0°` 对照成立、`inputImage` 与
  渲染页在 0° 下墨迹 100% 重合。判定**分两级**：轴判错 = FAIL（框会横竖颠倒），
  同轴内方向不可分辨 = `PASS-AXIS`（如密排表格页在 180° 下近乎自对称 —— 如实认怂，
  不伪造 PASS）。
- **`scripts/verify_region_anchor.py`**：把"抽 5 页目视"变成可复现证据 —— 用生产
  函数算框、画到真实渲染页、输出图与清单；旋转页额外输出"不做逆映射"的对照图。
- **`tests/unit/test_anchor_orientation_tool.py`**（27 项）：用合成图案确定性检验
  **核验工具本身** —— 验证器判错等于给出虚假的"已验证"。
- 真实产物侧新增两条机检：旋转奇偶 ⟺ 坐标系横竖的自洽护栏、朝向分类必须开着且
  逐页有角的**能力退化**护栏（关掉它不会报错，只会静默退回"无法定位"）。
- **`docs/REGION_ANCHOR_VISUAL_CHECK.md`**：完整核验记录，含一条**负面结论** ——
  本批记录上不存在廉价的像素代理判据（墨迹密度 58.8%、投影空白带奇偶、框内文本行
  方差 12.0%、整页区域并集 F1 29.4% 全部不具鉴别力），故**刻意不写**"永远绿却无
  鉴别力"的伪测试。

### 工程卫生与分发校验（本次补齐）

- **清除已入库的真实 LLM 密钥**：`tests/e2e_frozen.py` / `e2e_manual.py` /
  `e2e_quick.py` 三处硬编码了同一把真实 DeepSeek key（自 `81964a3` 起在 git 历史中
  且已推送）。改为统一从 `PBC_E2E_DEEPSEEK_KEY` 读取（`tests/e2e_proc.llm_key()`）；
  未设置时 e2e 如实降级并打印提示，不伪造通过。⚠️ **删除不能抹掉历史，该 key
  必须到服务商处轮换**（流程见 DEPLOYMENT.md「Secret 轮换流程」）。
- **e2e 可指向任意产物**：`tests/e2e_frozen.py` / `e2e_manual.py` 此前把
  `pbc-server.exe` 路径写死成 `D:\...\dist\pbc-server`（含绝对盘符），导致 e2e
  **测的是 PyInstaller 直接产物，而用户运行的是 Electron 包内嵌的那一份** ——
  等于"测了 A、发了 B"。现由 `tests/e2e_proc.resolve_exe()` 解析，默认仍为
  `dist/pbc-server`（行为不变），可用 `PBC_E2E_EXE` 指向 `win-unpacked` 内嵌副本。
- **新增分发一致性机检 `tests/unit/test_distribution_parity.py`**：把
  **配置 ↔ 文档 ↔ 实物**三者钉在一起 ——
  `win.target` 声明的形态必须与 DEPLOYMENT.md 的分发指导一致（防"文档承诺安装包、
  实物只有免安装目录"）；`extraResources` 必须恒为 `dist/pbc-server`（打包链的
  唯一定义点，改错会静默装进空/旧服务端）；`npm run build` 必须串起 css→py→win；
  产物在场时校验**最新产物**（构建失败后最新目录恰是残缺的，故"最新"即最高风险）
  完整、内嵌服务端与 `dist/` 逐字节一致、`app.asar` 内版本 == `main.APP_VERSION`。
- **新增回归护栏 `tests/unit/test_e2e_proc_helper.py`**：源码扫描禁止再出现
  32+ 连串 `sk-` 字面量、禁止写死本仓库/家目录绝对路径（含阳性对照，防护栏因豁免
  而空转）；并覆盖 `resolve_exe` / `llm_key` 的行为。
- **文档同步**：DEPLOYMENT.md 显式声明"当前不产出安装包"（`nsis` 配置块是保留调参
  但未被 target 启用，属死配置，已就地说明启用法）、补"分发前检查清单"与
  SmartScreen/杀软首次运行指引、说明**产物目录名可能因安全软件占锁而变动**
  （`dist-electron` / `-locked` / 手工指定的 `-v112`），分发前须按"最新且完整"
  而非目录名判断。

### 工程卫生续补（产物目录治理 + OCR 后端可验证）

- **新增 `scripts/clean_dist.py`（dist 产物体检与安全清理）**：electron-builder 的
  输出目录名会漂移 —— 安全软件（本机为火绒）占锁 `resources/app.asar` 时
  `build.ps1` 自愈到 `dist-electron-locked`，手工重打包又可能指定
  `dist-electron-v112` —— 仓库一度积压 5 个 `dist*` 目录（约 1.0GB）。脚本把
  "该发哪一个"变成可判定：按**最新且完整**选出保留项；`dist/`（PyInstaller 产物，
  是 electron-builder `extraResources` 的输入）永不清；逐文件**认锁**并点名报出
  被外部句柄占用、从而让**整个目录**都无法重命名/删除的文件；默认 **dry-run**，
  `--apply` 才动手且**走回收站**（可恢复）；认不出类型的目录只通报不动手；
  **一个完整产物都没有时什么都不删**。
- **e2e driver 现在校验"实际用的是哪个 OCR 引擎"**：主后端提交失败会自动 failover
  到备选后端，而终态/findings/SSE 全都照常 —— 只有 `jobs.ocr_backend_used` 能揭穿。
  `e2e_run.py` 的 `run_upload(..., expect_backend=...)` 把实际后端写进结果，不一致即
  判 FAIL 并打印 `BACKEND MISMATCH`。2026-09-15 实测：Paddle 上游返回
  `10010 任务提交队列已满`，一轮"用 Paddle 跑"的报告看着全绿（`review` + 32 findings
  + SSE 正常），实际后端却是 MinerU。**这类结论不得作为 Paddle 路径的证据。**
- **e2e 密钥不再走命令行**：`--sf-key` / `--paddle-token` / `--mineru-token` 均可由
  `PBC_E2E_SILICONFLOW_KEY` / `PBC_E2E_PADDLE_TOKEN` / `PBC_E2E_MINERU_TOKEN` 注入。
  命令行参数会进 shell history、进程表（`tasklist`）与 CI 日志，等同于泄漏 ——
  本仓库已经吃过一次硬编码密钥的亏。
- **新增护栏**：`tests/unit/test_clean_dist.py`（方案判定 / 安全默认 / 认锁跳过）、
  `tests/unit/test_e2e_backend_assert.py`（判定语义 + **AST 联检**所有
  `run_upload(...)` 调用必须声明 `expect_backend`，防新轮次漏校验）。
  `tests/unit/test_e2e_proc_helper.py` 的产物目录排除表由"罗列具体名"改为
  **前缀匹配**，目录名再漂移也不必回来同步。
- **文档同步**：DEPLOYMENT.md 补产物目录治理（根因 + 体检脚本 + 认锁处置）与
  "真实文档轮次要确认实际 OCR 引擎"的告警，检查清单新增产物体检步骤并改用规范名
  `dist-electron\`；README.md 同步。

### 验证（2026-09-15 真实 Paddle 轮次）

- **51 页真实批记录在 Paddle 后端上跑通，且旋转分支被真实数据覆盖**：
  job `95a27d88-52b`，`review`，1894s，`ocr_backend_used = paddle`（新增断言
  `expect_backend=paddle` 通过，`backend_mismatch=null`）；51 页均无稀疏页
  （<40 字符），findings 总量 347。
  - 逐页 `space_rotation`：50 页 `0` + **第 8 页 `270`**（`space=[1920,1440]`、
    `space_aspect=1.3333`，而页面 `page_aspect=0.75`）—— P0-3 的旋转场景在真实
    数据上复现。
  - **逆映射逐边核对**（`270` 规则 `x'=v, y'=1-u`）：OCR 空间
    `[0.02865, 0.22153, 1.0, 0.86736]` → 页面空间
    `[0.22153, 0.0, 0.86736, 0.97135]`，四边与期望值精确吻合。
  - **方向独立核验**（`scripts/verify_anchor_orientation.py`，读本次真实
    `paddle_original.jsonl`）：第 8 页上报 270° / 实测 270°（轴优势 3.148），
    第 3/7/19 页 0° 对照成立 —— 全部 PASS。
  - 详见 `docs/REGION_ANCHOR_VISUAL_CHECK.md` §7。
- **纠正上一轮的结论**：此前那次 51 页"Paddle"轮次（`4b098630-1cb`）直查库为
  `ocr_backend_used = 'mineru'`（上游 10010 队列满 → 自动 failover）。当时的报告
  与屏幕输出都看不出这一点 —— 这正是本轮加后端断言的直接动因。

### 已知限制

- 上游去畸变（`use_doc_unwarping: true`）使块 bbox 与渲染图存在非刚性形变 —— 已
  实测证明该残差**非旋转引入**（未旋转页同样存在）；高亮框可能比文字略松，与"只做
  区域级、不宣称单元格级"的产品口径一致。

---

## [1.1.1]

> 起点：`v1.1.0`（`ca355d6`）之后的补丁级发布。含 `_parse_spec` 修复与降噪专项
> 调研文档（`e2417cd`、`4273c1f`，此前未随二进制发布），以及下面两项降噪落地。

### 新增

- **抑制留痕（P0-2，合规必需项）**：被降噪规则抑制的 LLM 误报从"只记一个计数"
  改为**落台账 + 可查 + 可回退 + 可抽检**。
  - `drop_unfounded_spec_findings()` 返回 `(保留, 明细列表)`（**第二项是列表不是
    计数**）；每条带非空 `reason`（含命中的三元组及各自判定）与结构化 `evidence`。
  - schema v11 新增 `finding_suppressions` 台账表：抑制 ≠ 删除（行只追加、不删除），
    `reason` 由 `CHECK` 强制非空（显式空白字符集，堵住 Tab/换行绕过）。
  - 新端点 `GET /api/jobs/{id}/suppressions`（含 page 过滤与全 job 计数）、
    `POST /api/jobs/{id}/suppressions/{sid}/revert`（一键回退为正式 finding；
    台账行只记 `reverted_at`/`reverted_finding_id`，重复回退 400，跨 job 404）。
  - 复核页新增"已抑制条目"面板：逐条展示理由与命中证据，支持回退。
  - 落库与正式 finding **同一把锁/同一事务**；重分析只清未回退行（已回退的是人工
    审计证据）；写 `audit_log(action=spec_guard_dropped)`。
- **区域级证据锚（P0-3）**：finding 可一键定位回原页并高亮其所在 OCR 版面区域。
  - 新增纯函数模块 `core/pipeline/regions.py`（归一化 / 标签闭集 / 提取 / 锚定）。
  - `page_cache.regions_json` 保存每页区域（Paddle 块级 bbox、MinerU 块 bbox +
    `layout.json` 的 `page_size`），bbox **一律归一化到 0..1**。
  - 复核页叠加高亮框 + finding 卡片"定位原图"按钮（SSR 首屏与 AJAX 翻页共用同一
    推导与同一呈现逻辑）。
  - **只到区域级**：两个后端都不回传单元格级 bbox；自称单元格级等于给复核员一个
    错位的框。
  - **宽高比闸门**：实测第 8 页 OCR 坐标系为横向（服务端旋转过），与页面宽高比不
    一致时**明示无法定位**，而不是画一个横竖颠倒的框。
  - 锚不上（无区域 / 无特征词命中）时不显示定位入口（宁缺勿错）。

### 变更

- 版本号 1.1.0 → **1.1.1**（`main.APP_VERSION`、`package.json`、
  `package-lock.json`；`v1.1.0` tag 保持不动）。
- `tests/e2e_frozen.py` 的 `/health` 版本断言改为从 `main.APP_VERSION` 派生，
  不再硬编码（硬编码会在升版本时静默失配，把验证变成假通过）。

### 测试

- 新增 `tests/unit/test_suppression_ledger.py`（迁移 / CHECK / 单一声明处 /
  源码扫描）、`tests/unit/test_regions_anchor.py`（60 项，含真实 51 页产物复算）、
  `tests/unit/test_region_anchor_wiring.py`（16 项接线不变式 + 真实 MinerU 复算）、
  `tests/integration/test_api_suppressions.py`（10 项真实 HTTP）、
  `tests/integration/test_api_region_anchor.py`（6 项 API + SSR 一致性）。

---

## [Unreleased]

### 修复

- **`_parse_spec` 书写变体缺口（M9-spike 真实落库定位）**：真实 3526 个
  `(spec, value)` 对中存在 7 种**语义等价但此前解析失败**的写法，全部静默降级为
  `spec_unverifiable`（送人工 = 噪音）：
  - **单位夹在数字与分隔符之间**：`972 μg/mg ~1020 μg/mg`（**20 处，全是含量/效价
    这类关键质量属性**，全部解析失败；同语义的 `0.1~0.2 MPa` 一直正常）；
  - **LaTeX `±`**：PaddleOCR-VL 把印版 `±` 读成 `\pm`（51 页真实输出 **16 处 / 12 页**），
    外层还可能裹 `$`（`温度(15 \pm 5°C)`）；LLM 目前多数会归一，但规则层是判定权威，
    一旦 LLM 原样回填就静默丢判定能力；
  - **全角/Unicode 符号**：`～`(U+FF5E)、`－`、`−`(U+2212)、`—`
    （`99%～101%` 1 处失败，而同语义半角 `99%~101%` 15 处成功）；
  - **外层括号**：`(1300~3200)`。

  修复：`core/rules/parsing.py` 新增 `_normalize_spec_notation`（只归一书写形状，
  不改判定语义），在 `≤/≥ → <=/>=` 归一**之前**调用。折叠"同单位夹分隔符"时
  **保留串尾单位** —— `_try_unit_normalize` 依赖 spec 串里的单位做实测值单位换算。

  **验证**（真实数据对照，用 `git show HEAD:` 取修复前版本）：22 处由"不可解析"转为
  "可判定"，**解析结果变化 0 处、新增超差 0 处**（纯降噪，不引入新误报）。
  仍不可解析的 31 种形态全部是**正确的 fail-closed**（`是/否`、`符合规定`、
  `蓝紫色结晶性粉末`、裸数字 `1.4`/`14 L/min`）。单测 +9 项。

### 变更

- **调研与执行清单入档**：新增 `docs/NOISE_REDUCTION_SPIKE.md`（表格对齐粒度与
  PaddleOCR-VL bbox 资格的实测结论）与 `docs/NOISE_REDUCTION_TODO.md`（P0–P2 分项清单）。
  两条硬结论：① 两个后端**都不提供单元格级 bbox**（Paddle 整表一个 block、MinerU 整表
  一个 span，`_model.json` 中 `cell`/`rowspan`/`colspan` 出现 0 次）→ 对齐只能用
  **表级 bbox 粗筛 + 字段级标签匹配**；② Paddle 的 `layout_det_res.boxes[].score` 是
  **版面检测分**（实测 mean 0.579 / p50 0.545），**不是**抽取置信度，不可当数值正确性信号。

### 已知缺陷（已定位，未修）

- **Paddle 多行单元格分隔符不确定 → 值融合**（更正 v1.1.0 CHANGELOG 中"两个相邻单元格
  粘连"的表述）：Paddle 把**整个多行区块压成 1 个 `<tr>` + 两个多行 `<td colspan=N>`**
  （标签列 / 值列），标签↔值**只靠行序对应**；且分隔符形态**在同一份文档内不一致** ——
  p09 有字面 `\n`（23 处）正确成行，p21 **完全没有分隔符** →
  `A000626-221201/4` + `17 次` 融合成 `A000626-221201/417 次`，规则层与 LLM 层同时读到
  `417`（规格 ≤100 → 双双误报）。属**服务端非确定性行为**（8/26 轮次同页显示
  `A000626-2212017 ☐次`），**不做启发式还原**（无法可靠还原切分点）；
  方向见 `docs/NOISE_REDUCTION_TODO.md` 的 T-P1-1/T-P1-2（标签行数 vs 值行数一致性校验 →
  降级 `structure_warning` 交人工）。

---

## [1.1.0] — 2026-09-14

v1.1 聚焦**质量可度量、鲁棒性、知识库多源、对标能力**四方面。所有结论均基于实测
（真实 51 页手写批记录全链路 + 真实上游服务），不保留未经证实的声明。

### 新增

- **Finding 质量可度量 + 降噪（M2）**：金标集 P/R/F1 评测管线（`scripts/eval_findings.py`）；
  真实 51 页 job finding 数 **784 → 268（-65.8%）**，critical 全保留（33 → 33）。
- **规则扩展 R11–R17（M4）**：物料平衡（mass_balance）、自检自核（self_review，critical）、
  设备/清洁状态（equipment_state）、环境监测（env_monitor）、文件版本（doc_version）、
  偏差关联（deviation_link）、涂改规范（alteration）。规则以 `core/rules/registry.py::RULE_REGISTRY`
  为唯一入口，按 id 可经 `config.json` 的 `rules.disabled` 关闭。真实 51 页重放 **+9 findings
  （399，+2.3%）**，全部为真信号。
- **多源 GMP 知识库（M5）**：GMP（2010 修订）全文 + 附录 + NMPA 记录规范 + ALCOA+ +
  21 CFR Part 11 等 **6 源 / 441 条**；纯 BM25 检索（无向量库）；金标 37 条命中率 **97.3%**
  （排除已登记缺口 100%，阈值 85%）；设置页多源浏览与按源启停。
- **docling 第三对照引擎（M7）**：`core/docling_client.py`（MIT，本地推理，无 token）；
  `scripts/compare_ocr_engines.py` 输出两两差异页报告；**未安装即优雅降级**，主链不受影响。
- **三色复核分级（M6/T6.4）**：复核页按**检出来源**分档——规则命中（红）/ LLM 辅助（蓝）/
  系统校验通过（绿，按问题类型统计覆盖率）；映射单一来源 `core/finding_quality.REVIEW_TIER_BY_SOURCE`，
  SSR 与 AJAX 双端同源。
- **OCR 后端能力表（M7/T7.1）**：`OcrCapabilities` 显式声明各后端能力（切片/页子集/本地/凭据需求），
  engine/stage1/dual_compare 的字面假设改由能力表驱动。

### 变更

- **SSE 轮询常量统一（M6/T6.1）**：`api/jobs/_SSE_POLL_SECONDS = 2` 作唯一来源；
  修复 docstring（"每 2 秒"）与代码（`retry: 2000` / `asyncio.sleep(3)`）三方不一致。
- **终态快照缓存收敛（M6/T6.2）**：`(status, finished_at)` 为键的单一来源缓存；
  聚合并发流稳态查询 **16 → 1**。
- **阶段内已耗时（M6/T6.3）**：前端 `eta.js` 纯函数 + 1s 本地 ticker，长阶段（OCR/逐页/跨页）
  超 5s 起显示"已用 45 秒 / 3 分 07 秒"，不再依赖 SSE 帧间隔。
- **OCR 尺寸/形态鲁棒性（M3）**：不可无损修复页（小字号+低 DPI / 极端长宽比）显式携带
  非完整信号，不得静默标记成功。
- **覆盖率门禁 90% → 95%**（仅统计 `api/ core/ llm/ db/ config/ main`）。

### 修复

- **规格解析三处缺陷（M8 真实 51 页看图定位）**，均由"OCR/LLM 把印版符号读丢"引发：
  - **`±` 丢成空格**：印版 `15±5℃` → OCR `15 5°C` → 结构化 `15-5°C`，
    `_parse_spec` 解出反向区间 `between(15,5)` 恒为假 → 单页 21 条温度误报。
    修复：支持 `±` 形式与空格 `A B` 形式；反向区间按 `A±B` 重建（`A-B, A+B`）。
  - **小数点丢失**：手写 `4.6` → OCR `46`（规格 `3.0-5.0bar`）→ 7 条误报 +
    1 条 critical。修复：`_decimal_loss_factor` 软化（判据：实测值不含小数点
    且超差倍数 ≈10×/100×）→ `info` + 明确提示，**仍表面化不静默丢弃**；
    反例保护：`45.6 vs 0~5°C`、`25 vs ≤5.0` 维持 `warning` 铁口。
  - **LLM 自报超差无复核**：LLM 把 `<0.3MPa` 当下限，`0.16` 判成超差（7 条
    critical）；`40±3℃` 丢 `±` 后误判 `42.1` 超差。修复：新增
    `core/rules/spec_guard.py`，LLM 自报 `param_out_of_spec` 一律用**规则层同一
    解析器**复核，判为合规/疑似丢点者剔除，定位不到或不可判者保留（fail-closed）。
    真实页剔除 14/26 条。名称比对做分隔符归一（`T2101a_压力` ↔ `T2101a 压力`）。
- **真空度/负压符号约定未 fail-closed**：规格 `≤0.08MPa` × 负表压实测（`-0.068~-0.096`）
  数值比较恒真 → 真实偏差被静默放过。修复：`_sign_convention_uncertain` 命中即降级
  `spec_unverifiable` 交人工（真实页 6 处）。
- SSE 轮询常量三方漂移（文案 2s / retry 2000 / sleep 3）。
- 终态快照缓存写入同一 dict 对象，调用方改字段污染缓存（跨请求串数据）→ 改为存/返副本。
- 长阶段前端计时器在断线重连时叠加泄漏 → 定时器上提至 subscribe 作用域并配对停止。
- docling 取不到分页符时的页数切分回退（`total=0` 不再漏切）。
- `ROADMAP_v1.1.md` 中 docling 许可残留 AGPL-3.0 → 更正为 **MIT**。

### 明确不做（本轮）

- 不做 Web/移动端适配；不接真实 MES/LIMS；不换主 OCR 后端（第三方仅对照）；不做模型微调。
- 不引入 embedding/向量库（维持纯 BM25）。
- **趋势筛查（OOT）不在 v1.1 落地**：真实库 23 job 实质仅 1 个批次、参数名 37% 只出现一次、
  仅 30% 参数可机械解析规格 → 单批次无法标定阈值，验收不可证伪。结论与 v1.2 分阶段路径见
  `docs/TREND_SCREENING_EVAL.md`。

### 验证

- 全量测试通过；`scripts/release_gate.py --fail-under 95` → **OVERALL: pass**。
- 真实 51 页全链路 frozen e2e 两轮通过（首轮 paddle、第二轮 mineru）+ ui_e2e 通过。
  ⚠️ e2e 驱动打印的 "N findings" 取自 `GET /api/jobs/{id}/findings`，该端点
  `limit` 默认 **50** → 那是**首页条数不是总数**（首轮实际落库 395、第二轮 407）。
  汇报总量必须直接查库。

### 已知局限（v1.1.0 发布时点）

看图比对（`docs/M8_VISUAL_VERIFICATION.md`）除定位并修复上面 4 类缺陷外，还暴露出
以下**未修**问题，根因均不在规格解析器：

- **OCR 把相邻单元格粘进同一个 `<td>`**（如 p9 批次号尾部与使用次数粘成
  `A000626-221201/417 次` → 使用次数手写实值 `17` 被读成 `417`；p6 同类使 `中和后 pH`
  读成 `70 L 6.88`）。此类"数字本身即错"的条目**规则层与 LLM 层会被同时误报**，
  任何"规格 vs 实测"的交叉复核都无法发现（规格正确、数字也看似合理）。
- **OCR 表格列错位**：mineru 在 p08 把 `透出液流量`/`TMP` 取到左邻列的值 → 14 条超差。
- **主备后端读数分歧**：p19 手写纯度 paddle 读 `49.0`（→ 误报 critical）、mineru 读
  `99.0`（纸面实读亦为 `99.0`，`≥98%` 合规）；手写年份 `2015` vs 印版 `2025` 同类。
- **`spec_guard` 的 fail-closed 边界**：LLM 把 7 条 `<0.3 MPa` 误报合并为一条范围式
  名称（`T2101a~d`）时无法匹配单列 → 按 fail-closed 保留（7 条 critical → 1 条，未归零）。
- 其他：仅含"时:分"的时点会被锚定到**当天**日期，可能产出 `signature_time_anomaly`；
  `doc_version` 规则会把文件编号 `H3-MPD-10133-R22` 的 `R22` 误当版本号。

**不修的理由**：上述"数字本身即错"的条目，启发式软化一旦过宽就会**抑制真实超差**
（fail-closed 原则要求宁可多报交人工）；`_decimal_loss_factor` 的回归教训已证明
软化必须有可机检的强判据。方向留作后续专项（T-D）：抽取后校验（数字 token 与相邻文本
需有分隔符、列数与表头一致）、范围名（`T2101a~d`）展开后再交 `spec_guard`、
主备读数跨规格阈值分歧时强制人工仲裁、手写值置信度标注。


---

## [1.0.0] — 2026-08-20

首个正式版本：GMP 批生产记录半自动合规检查系统（OCR + LLM 结构化提取 + 规则/LLM 跨页
合规分析 + 人工复核 + 报告导出），本地单用户部署（PyInstaller exe + Electron 便携版）。
详见 `docs/发布说明_BatchSentry_v1.0.0.html`。
