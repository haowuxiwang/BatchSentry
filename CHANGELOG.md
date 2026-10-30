# Changelog

本文件记录 BatchSentry 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [Unreleased]
### 真实语料两轮复跑 + 对抗性审查收口（Round 134，2026-10-30）—— 0-81 / 0-82 续、0-84 / 0-85 新增

**输入**：上一轮 `1.2.2`（HEAD `6197cb8`）的 10 页 `sy_r7` 只有 **2/4** —— S1 在 p8 仍出
两张日期卡、S2 在 p9 漏报物料平衡。按「先定位 → 再解决 → 最后测试」逐条闭环。

**① S1（0-81 续）：规则层聚合卡与 LLM 跨类型卡收敛**
规则层「文档级聚合 + 日期类判定不可信」的行固定是 `suspicious_date`，LLM 对同一处
OCR 年份误读常标成 `signature_time_anomaly` ⇒ R133 的「同页**同类型**」判据压不住。
改为解析聚合行 `ocr_text` 的 `pages=[…]`，对这些页的 LLM 日期行统一收敛（规则行保留）；
非聚合的规则日期行仍只按精确类型覆盖 —— 关系类（`year_contradiction`/`time_reversal`）
不受影响。单测 +2（跨类型收敛、按 `pages` 覆盖；均带"规则行必须保留"断言）。

**② S2（0-82 续）：物料平衡表读 OCR 原文兜底**
`sy_r7` p9 原文清晰含 `领用20/使用30/损耗0`，但 `structured_json` **一个参数都没有**
（LLM 结构化漏抽）⇒ R133 只读结构化页的确定性判据整体失效。新增
`_balance_triples_from_text`：
- 只认表头 `领用数/领用数量` 之后 600 字符内的纯数字三元组；
- 三元组后必须跟行号（排除 `72502 0309 20` 这类批号/代码连续数字串）；
- 读到**工序句子**即截断 —— p9 表格后紧接步骤 10 的压差/温湿度表，
  `20.6 51.6 11.2` 不截断会被误读成第三行（实测已复现并加回归钉）；
- 原文读数无法区分印刷/手写、且未经独立复核 ⇒ 只报 **info** + 「不作为偏差结论」；
- 结构化已有读数时不跑兜底（避免同表两张卡）。单测 +4。

**③ 对抗性审查的其余发现（已修）**
- `release_gate.no_pii_in_tracked_files` 命中 2 处注释里的**真实员工姓名** ⇒ 改为占位名
  （落实"真实企业名/姓名不得入库"铁律）；复扫 **0 命中**。
- `ruff F841`：R133 收紧签名置信度后残留未使用的 `high` 集合 ⇒ 删除。

**④ 产物级复跑（冻结产物 `1.2.2` / `git_head 7b0b22f`，两份 exe sha256 逐字节一致）**
- 10 页 `sy_r8`：39 findings，评分器 **4/4 PASS**（`recall_proxy=1.0`；S1/S2 各 1 条）。
- 51 页 `slm_r4`：201 findings、0 页失败。**M1 FAIL**、M2 PASS(2)、**M3 FAIL**（2 条，
  上轮 4 条）：
  - **M1 根因（新登记 0-85）**：p6 原文里 `▲` **只在页脚图例**出现，
    `▲pH 为 6.8~7.0` 的标记被 OCR 丢失 ⇒ `_key_param_marked` 无法命中
    （`slm_r2` 曾保留、`slm_r3/r4` 均丢失）⇒ 0-72 的护栏依赖上游 OCR 保真度。
  - M3 是已在标注集里标为 `criterion:false` 的**已知开放项**，本轮只量化为 2 条。

**⑤ 未做 / 新门槛**
- **0-84**：反向代理会把 `Host` 改写，`is_local_request` 会让写接口 403 ⇒ 企业多用户
  部署照 `DEPLOYMENT.md` 走会"读得到、改不了"；长期方案（信任代理 / CSRF token）待定，
  **不得**简单放宽守卫。
- GitHub Release：本机无 `gh`、无 `GITHUB_TOKEN`，只能产出本地 zip + `SHA256SUMS`；
  资产上传需用户授权或手工上传。

### 真实语料标注集 + 已落库噪声清理 + 企业内网接入（Round 133，2026-10-30）—— 0-76 / 0-81 / 0-82 / 0-83

**这一轮把"准确/精确"从人眼对照变成可复跑的分数，并修掉两份真实记录上**同一处证据被报两遍**、
**物料平衡漏报**与**企业内网模型无法配置**三类具体缺陷。**先定位、再解决、最后测试**。

**① 真实语料的人工标注集 + 评分器（新增，与合成语料评测并存）**
- `scripts/score_real_run.py`：吃 `e2e_*.json`（三种外壳）与标注集，输出逐条 PASS/FAIL 与 `recall_proxy`。
  判据：`expect.min/max`（漏报/重复）、`severity_max`（过度定性）、`text_all`/`text_none`（口径）、
  顶层 `forbid_text`（全局禁用措辞）；**空输入 / 空 labels ⇒ exit 2（fail-closed）**。
- `tests/unit/test_score_real_run.py`（9 条契约）。
- 标注集（gitignored，`devlogs/_verify/gold_*.json`）：10 页 5 条（S1–S5）、51 页 3 条（M1–M3）；
  `criterion: true` 才计入召回，回归钉标 `criterion: false`。

**② 0-81：已落库 llm 行的同源噪声（`_suppress_persisted_llm_noise`）**
`llm_page` 在 Stage 2 每页完成即落库，Stage 3 的内存护栏看不见这些行 —— 与 0-64 同病根。新增两类清理：
同页**同类型**规则判据已覆盖（`suspicious_date`/`signature_time_anomaly`，精确类型、不套族）⇒ 删 LLM 行；
规则层已按「领用/使用/损耗」表判定（`ocr_text` 前缀 `balance_row=`）⇒ 删同页 LLM 同表描述。
规则层永远是保留方；删行落 `audit_log action=persisted_llm_noise_removed`。

**③ 0-82：物料平衡表算术的确定性判据（R11 扩展）**
`_check_mass_balance` 按「<品名> 领用数/使用数/损耗数」三列分组：`使用数 > 领用数` 或
`使用+损耗 > 领用` ⇒ `mass_balance`；手写读数（`value_source=handwritten`）只报 **info** +
「不作为偏差结论」附注，印刷值才 warning。批号粘连的 `0309 20` 取尾数。
**动机是实测的召回不可复现**：同输入两次跑，`sy_r5` 漏报 p9、`sy_r6` 由 LLM 报出。

**④ 0-76：删掉 R8 的极性推断**「（否定式提问的"否"通常即正常答案）」—— 题面极性无可靠判据，
实测 p2「上一次清场**是否在有效期内**」答"否"恰是偏差方向。改为只陈述事实 + 请人工核对。

**⑤ 0-83：企业内网模型/OCR 的显式 opt-in（`PBC_ALLOW_PRIVATE_ENDPOINTS=1`，默认关）**
默认仍拒私网/环回/`localhost`（SSRF 纵深，单机行为逐字不变）；显式开启后放行私网/环回，
**link-local（169.254/16、fe80::/10）任何模式都拒**（云元数据端点与企业内网模型无关）。
HTTPS 强制与协议白名单不变。`tests/unit/test_security.py` 47 passed（含 6 条新增）。

**⑥ 承 Round 133 前段（已提交，本文件此前未记）**：签名槽**显式 `confidence: high`** 才允许
`self_review critical`（缺失/medium ⇒ `signature_unreadable/info`）；手写数值结论补
「不作为偏差结论」附注；LLM 的「缺少签名人姓名」补 OCR 漏识口径。

**⑦ 测试与产物**：定向单测 `test_rule_gmp.py` / `test_r121_doc_level_collapse.py` /
`test_llm_finding_guard.py` / `test_security.py` 全绿；10 页产物级 e2e `sy_r5` **2/4**、
`sy_r6` **4/4**（评分器有判别力，且暴露了 S2 的 LLM 随机性 ⇒ 由 ③ 的规则兜底）。
构建物 `1.2.2` 重建后按 `PROVENANCE.txt` 校验两份 exe sha256 一致（见 §⑧）。
⚠️ **未做**：51 页 `slm_r3` 的最终评分与 GitHub Release 资产上传需在产物重建后复跑/办理。

### 手写读数派生结论降级 + 同源误读去重（Round 132，2026-10-29）—— 0-74 / 0-75

**这一轮回答的是"两份批记录上，BatchSentry 的结论比人眼多说了什么"。** 做法是先把两份真实
记录（10 页 / 51 页）的 findings 与原页逐条对照（人工看页图 + 机器核对 OCR 字面量），
再只修**已经定位到具体页与具体格**的两类缺陷 —— 先定位、再解决、最后测试。

**① 51 页交叉核对（取证脚本 `devlogs/_verify/xcheck_slm_r2.py`，产物 gitignored）**：
183 条 findings 对照全文档 raw OCR + 结构化生产日期，**同源重复 0 条**；"字面量在原文里
找不到"2 条，逐条看过都能解释 —— p21「实际值」是引号内的**参数名**被脚本当成姓名，
p36 的 `2026-10-29` 是系统注入的"今天"（不是页内读数）。⇒ 本语料上**未发现幻觉证据**。
⚠️ 边界：该轮 `llm_cross` 全 0（p0 #145 自身报 `LLM 调用失败: RuntimeError`）⇒ 51 页只验到
页级链路，跨页语义链路的证据仍来自 10 页轮。

**② 0-74：手写数字的比较结论不再直接定性（`_check_handwritten_numeric_conclusion`）**。
p9 的 PE 袋平衡表，原页 600 dpi 直读 领用 **30** / 使用 **30** / 损耗 **0**，OCR 把平顶的
"3" 读成 "2" ⇒ LLM 判「使用数大于领用数」。修法**不是**按文案去猜，而是要求
**结构化里的机器事实**：`type=mass_balance` + 文案含比较词 + 引用的数字与
`value_source=handwritten` 的数字**相交** ⇒ 降级为 `info`，`guard_reason` 写明
"结论依赖手写数值…请对照原页确认读数"。
刻意**只降级、不抑制**（记录是否真不平衡仍需人工结论），且**fail-open**：拿不到结构化
手写证据就不动。⚠️ 目前只建模 `mass_balance` 一类，"手写读数派生硬结论"的其它形态未覆盖。

**③ 0-75：同一次误读只报一条，且不准自称"篡改"**。p8 复核人日期实为 `2025.06.30`，OCR 读成
`2015.6.03`；页级报 `signature_time_anomaly`、跨页又把同一字面量报成 `suspicious_date`
并写「记录篡改嫌疑」。
- **去重**（`stage3._same_literal_covered_by`）：判据是**同页 + 同一日期字面量**已由
  `llm_page`/`llm_cross` 报过 ⇒ 后者抑制，审计 `covered_by=…_same_literal` 单独计数。
  刻意**只限** `suspicious_date` / `signature_time_anomaly`（"同一处签名日期读数异常"的
  两种说法）；**排除** `year_contradiction` / `time_reversal` —— 它们断言的是**关系**，
  实测 51 页 p2 的 #5 与 #7 共享 `2025.01.30` 却是两件事，按字面量合并会**漏报**。
  规则层条目**豁免**（不得让一条 LLM 误读去删确定性判据）。
- **措辞**（`llm_finding_guard.sanitize_accusatory_wording`）：仅 `source ∈ LLM_SOURCES`，
  把「篡改/伪造/造假/捏造」整句改写为"日期读数可能异常（不排除 OCR 误读），需人工核对"，
  原降级理由**追加**进 `guard_reason`（不覆盖）。规则层文案逐字不动。

**④ 测试**：`tests/unit/test_llm_finding_guard.py` +3（手写降级 / 印刷值不降级 /
无结构化 fail-open）与 +2（LLM 措辞被改写且留痕 / 规则层豁免）；
`tests/unit/test_r95_manual_labels.py` +5（同字面量去重 / 不同字面量保留 / 非日期类型保留 /
规则来源豁免 / 关系类不合并）。定向 **110 passed**；全量门禁见下。

**⑤ 构建物**：本批代码提交后重建，`PROVENANCE.txt` = `git_head cd497b5` /
`built_at 2026-10-29 10:29:01` / `version 1.2.1`；`dist/pbc-server` 与
`dist-electron/.../resources/pbc-server` 两份 exe **sha256 一致**（`bdcfad50…`）。

**⑥ 公开面与开源基线仍未齐（承 Round 131，0-79 保持"部分完成"）**：`CODE_OF_CONDUCT.md`、
Issue/PR 模板、`CODEOWNERS`、`.gitattributes`、`.editorconfig`、`THIRD_PARTY_LICENSES.md`、
发布 `SHA256SUMS` + tag **仍不存在**；`.github/workflows/ci.yml` 注释引用 `release.yml`，
但该文件不存在。`package.json` 无 `repository` 字段。

### 泄漏补救收尾 + 开源治理补位（Round 131，2026-10-29）—— 0-77 / 0-78 / 0-79

**这一轮回答的是"已泄露信息的补救到底做完了没有"。** 结论：**公开面干净，本地收尾了，但
公开历史里还留着一个已轮换的 key 字符串**。

**① 旧历史还活着，只是挂在 tag 上（0-77）**。删库重建后，远端 `main` 只有 2 个提交，
但本地 5 个旧 tag（`v1.0.0` / `v1.1.0` / `v1.1.1` / `v1.1.2` / `v1.1.9`）分别锚定
**82 / 158 / 164 / 176 / 250** 个旧提交。用 PII 映射表（28 条）扫描：
- `--remotes`（已发布的全部引用，11 提交）⇒ **0 命中**；
- `--tags`（250 提交）⇒ **816 处命中**（`.gitignore`、`docs/REGION_ANCHOR_VISUAL_CHECK.md`、`tests/...`）。

`git ls-remote --tags origin` 为空、GitHub `/tags` 页无内容 ⇒ **远端从未泄漏**，
但只要一次 `git push --tags` 就会把已删库的真实批号/姓名重新公开。
**处置**：`git tag -d` 5 个 → `git reflog expire --expire=now --all` → `git gc --prune=now`。
处置后 `history_pii_scan.py --all` = **13 提交 / 0 命中 / 无 unreachable 对象**，
`git cat-file -t c70a620` 已 Not a valid object（旧提交不可恢复）。
⚠️ 代价：`release-archive/` 旧包 `PROVENANCE` 里的 `git_head a225fb8` 也一并不可解析 ——
审计线索只剩归档包本身（用户选择"彻底清除"，此代价已知）。

**② 推送防护（0-77 的后半）**：`.githooks/pre-push` → `scripts/pre_push_guard.py`
（Python 实现，便于单测）**拒绝推送任何 tag、拒绝推送非 main 分支**；
确需发版时 `ALLOW_TAG_PUSH=1 git push --tags`。启用：`git config core.hooksPath .githooks`。
`tests/unit/test_pre_push_guard.py` **8 条**（含真跑进程的退出码契约，不只测纯函数）。
⚠️ 边界：钩子是**每克隆自行启用**的，不是服务端策略；服务端仍需分支保护（见 0-79 的残余）。

**③ 公开树里还留着一把真 key（0-78）**。`tests/unit/test_no_committed_secrets.py`
此前用**两段字符串拼接**保存 2026-09-15 泄漏的真 key（51 字符）—— 拼接只骗过了正则，
GitHub secret scanning 与人工审阅都看不见它。**处置**：删掉字面量，改存 **SHA-256 指纹**
（`5d855143…e4501`），护栏改为"对每个 `sk-…{32,}` 候选算哈希再比对"——既移除了真值，
又保留"这把 key 被写回任何文件就能被认出"的能力。docstring 如实声明新边界：
**无法**识别被拆得更碎（逐字符拼接）的变体。
⚠️ **残余**：已发布的 2 个提交历史里仍有那个拼接串 —— 该 key 已于 2026-09-28 轮换
（0-66），**不重写公开历史**（重写会连带 10 条 dependabot 分支与 1 个 PR）。

**④ 开源治理补位（0-79，本轮只做用户选定的两项）**：
- 新增 `SECURITY.md`：支持版本、**走私密 Advisory** 的报告通道、响应时限、数据边界、
  密钥轮换、以及"OCR 误读是已知失效模式"的边界声明。
- `README` 新增「**数据与隐私**」（批记录会发往第三方 OCR/LLM；能否出境由部署方评估）
  与「**合规边界与免责声明**」（不构成放行结论、未做 21 CFR Part 11 / GMP 附录 11 验证、
  关键参数手写读数强制转人工）。
- **仍未做**（0-79 保持部分完成）：`CODE_OF_CONDUCT` / Issue 与 PR 模板 / `CODEOWNERS` /
  `.gitattributes` / `THIRD_PARTY_LICENSES` 入库清单 / 发布附 `SHA256SUMS` 与 tag。
- 顺带记录：仓库里唯一被跟踪的二进制是 `docs/BatchSentry_发布说明.pdf`（554 KB，
  18 页，用映射表扫描 **0 命中**、无 8 位批号形态）；建议后续移到 GitHub Releases。

### 关键参数的手写读数不再"静默判合格"（Round 130，2026-10-29）—— 0-72

**为什么**：51 页真实批记录 p6「中和后 pH」原页手写 **1.88**（规格 6.8~7.0 ⇒ 越界），
而 PaddleOCR-VL 读成 **6.88** ⇒ 规则层 R3 判"在范围内"、**零 finding**。这是合规结论
方向性错误（"越界"被读成"合规"），不是噪声。

**两条本以为是解法的路，实测都走不通**（先说否证，免得后来者重走）：

- **提高分辨率**：把该表格按 2000px 宽裁切后重读，仍是 `6.88`。
- **换第二个 OCR 引擎**：MinerU 独立解析同一裁片，**仍是 `6.88`** ⇒ 两个模型对这支手写体
  的失效**同源**（起笔的一竖被读成 6）。所以"双读一致"在本语料上是**假保证**，
  二次读交叉比对**不**构成证据。
- 另：PaddleOCR-VL 的 `parsing_res_list` 块只有 `block_label / bbox / block_content`，
  **没有 score/置信度字段** ⇒ "低置信就降级"也无从实现。

**修法**（确定性信号，可离线复算）：原页把关键参数标了 `▲`（关键工艺参数）/
`★`（质量控制点），页脚有图例；这个符号**在 OCR 原文里可见**，而"值读得对不对"不可见。
因此判据改为：`value_source=handwritten` **且**规格字面量紧邻 ▲/★（`_key_param_marked`，
前 12 / 后 90 字符窗口，`6.8~7.0` 与 `6.8 - 7.0` 经连接符归一后可比）时，即便 `_judge`
判"合格"也产出 `spec_unverifiable / cause=key_param_unverified`，文案明确"请对照原页核对原值"。
判定结果本身仍是 `in_spec=True` —— 不伪造超差结论，只是**不再静默**。

- `core/rules/finding_aggregate.py`：新增 `CAUSE_KEY_PARAM_UNVERIFIED`（并入
  `UNVERIFIABLE_CAUSES` ⇒ 类型由根因派生，不再允许调用方自选）+ 文案模板
- `core/rules/rule_spec.py`：`_check_param_out_of_spec` 逐页取 `raw_html` 传入判定；
  `_judge_param` / `_judge_cell` / `_judge_value` 增加 `page_text`（默认空 ⇒ 旧调用零影响）
- `tests/unit/test_key_param_guard.py`（**10 passed**）：标记命中/页脚图例不误命中/连接符变体/
  距离越界不命中/手写无标记不产出/印刷体不受影响/矩阵单元格同护栏/根因→类型派生
- 触发面实测（51 页真实语料）：`in_spec + handwritten` 共 **228** 条，命中标记 **3** 条
  （p6「中和后 pH」、p27「封口机档位」「成品重量」）⇒ 不会淹没复核队列
- ⚠️ **边界**：这**不**证明"合格"的读数都对 —— 它只保证"关键参数的手写合格结论必须过人工"。
  未标 ▲/★ 的手写参数仍按普通参数处理（这是刻意取舍：全量降级会让 228 条全进复核队列）

### 许可证改为 AGPL-3.0-only（Round 129，2026-10-28）—— 0-67

**为什么改**：`PyMuPDF` 是 AGPL-3.0 / Artifex 商业双许可，且被冻进产物（`import fitz`
出现在 6 处入包源码）。以 Apache-2.0 分发含它的冻结 exe **不成立**；换 `pypdfium2` 要重写
6 处渲染/抽取调用点并重跑全部产物级 e2e。用户裁定「最小最快闭环、不改依赖」⇒ **整体同证**。

- `LICENSE`：Apache-2.0 官方原文 → **AGPL-3.0-only 官方原文**（sha256 `d8a6cc31…96bee`，34 020 B）
- `package.json` / `package-lock.json`：`"license"` 两处同步为 `AGPL-3.0-only`
  （顺带修掉 `package-lock` 根条目里残留的 npm 默认占位 `ISC`）
- `NOTICE`：改写为 AGPL 归属 + 授权范围排除项（第三方数据 / 商标 / 打包组件）+ **§13 网络交互**
  义务说明；仍点名 PyMuPDF 的 AGPL 双许可（换掉依赖后这段由护栏要求同步删除）
- `README`：`License` 段原文 "Proprietary — Internal Use Only" → AGPL-3.0-only 说明
- `tests/unit/test_license_consistency.py`：登记新哈希、SPDX 名改 `AGPL-3.0-only`、
  条款清单按当前许可证动态取（Apache 条款不再恒 skip）、NOTICE 标题从 LICENSE 反推 ⇒
  **14 passed / 0 skipped**，未删任何断言
- ⚠️ 后果已披露：药企采购普遍禁 AGPL ⇒ 放弃企业闭源集成市场（用户已知悉）
- ⚠️ **换证不解决数据问题**：已推送历史里的真实批号/姓名/密钥仍在（见 0-65 / 0-66）

### 真实数据泄漏处置 + 门禁补位（Round 128，2026-10-28）—— 0-65 / 0-68

**这一轮的主线是"仓库里的真实数据"，副线是"上一轮只做了一半的接线"。**

**① 上一轮的账**：B7-2 的 `check_lint_clean()` **写好了但没接进 `run_all()`** —— 单跑它
全绿，而门禁里根本没有它（与 R119/R121 同一类错误：helper 有用例 ≠ 判据被使用）。
本轮接线 + 4 条行为用例 + 编排名字契约。**接上之后立刻抓出第二处漂移**：
`CLAUDE.md` 的门禁清单停在 11 项，实际 12 —— `test_gate_inventory_doc` 报的。

**② 泄漏处置（0-65）**：`scripts/scrub_pii.py` + **保结构替换**（映射表在 gitignored 的
`devlogs/_verify/pii_map.json`，因为它的左列就是真值）。批号**只换前 3 位**、后 5 位
保留 ⇒「OCR 把一位读错」这类证据链仍然成立（`50008219` vs `50808219` 仍只差 1 位）；
姓名**只换姓** ⇒ 近形对（`王小雨`/`王涛雨`）仍成立。实测 **27 文件 / 356 处**，
改后 `--check` 残留 0。`.gitignore` 里三个**真实文件名**改为根锚通配 `/*.pdf` `/*.png`
—— 忽略清单本身被跟踪，**写出真名等于再泄一次**（已验证 `git ls-files` 根级 pdf/png = 0）。

**③ 门禁新增 `no_pii_in_tracked_files`（0-68）**，刻意分两段：
**结构段**（`.gitignore` 不得出现"具体 + 非 ASCII"文件名）任何机器都可判；
**字面量段**（`scrub_pii --check`）在**没有映射表**时返回 **WARN 而不是 PASS** ——
"没复核"和"复核通过"是两件事，合并会让这条检查在 fresh clone 上永久空转。
⚠️ **上线当天就抓到第 4 个漏网的真名**：`.gitignore` 里还写着一份**含联系人 PII 的申报材料
文档名**（该行注释自述"含联系人 PII"），**手工脱敏清单漏了它** ——
这是"手工清单不可靠、必须机检"的实证，不是补充说明。变异 `r128_gate_pii_mutation.py`
⇒ **8/8 CAUGHT**，含两条误扫方向（不跳过通配符 / 取消非 ASCII 收窄）与两条空转方向
（缺映射表返 PASS / 有命中返 PASS），阴性对照（通配写法改等价形式）**保持绿**。

**④ 对抗性审查修掉自己引入的 4 处红**（全量 4335 passed / 4 failed 的那 4 条）：
- `test_regions_anchor` 写死品牌名，而它读的是**仓库外**的真实 OCR 产物
  （`%APPDATA%/PBC/output/…`，不该也无法脱敏）⇒ 改为按**结构**取地标 + 先断言它
  真的在左上角。教训：**断言依赖外部真实数据的字面量**，换个产物就会红。
- `test_driver_reuses_coverage_module_statuses` 要求"三个 `STATUS_*` 都得导入"，
  而 B7-2 按 ruff F401 删掉了**从未使用**的 `STATUS_COVERED` ⇒ 判据改成
  "**用到的** ⊆ 导入的 + 不得自造"，并补反空转。教训：**为了满足护栏而导入一个
  从不使用的常量，本身就是负债**（R126）。

**⑤ 产物**：`artifact_freshness` **FAIL** —— 两份产物各有 **11 个入包源文件**与清单
不符。逐行看过 diff：改动**全在注释与文档串**，运行时行为等价，但字节不同 ⇒ 必须重建
（"行为等价"不是放行理由，门禁判的是字节）。

重建过程踩到四个坑，都记下来了：
- **不许产生 `dist-electron-out-*` 变体**。文档里"输出到时间戳目录以避开 `app.asar` 驱动锁"的做法会让仓库堆积
  变体 —— 本轮实测生成过一个 360 MB 残壳。**正确做法：直接构建进标准路径 `dist-electron/`**（本轮实测该目录
  **没有**被锁）；真被锁时用 `scripts/clean_dist.py --apply` 收敛，而不是新开目录。
- **`PATHEXT` 只有 `.CPL`** ⇒ electron-builder 起不到 `npm.cmd`，报 `No JSON content found in output`
  （伪装成依赖树问题）。构建前必须显式设 `PATHEXT`。
- **网络取不到 Electron 发行包**（600 s 超时 ×2）⇒ 用 `-c.electronDist=<本地解包目录>` 指向
  `%LOCALAPPDATA%/electron/Cache` 里已有的 `electron-v43.7.4-win32-x64.zip` 解包结果。
- **`--dir` 构建不写 `PROVENANCE.txt`** ⇒ 产物被判残壳（`count_complete_artifacts` 三件套之一）、
  `runtime_eol` 恒 SKIP。已补写。

结果：`dist_variants` = **1 份完整产物（dist-electron）**、`artifact_freshness` PASS、
内嵌 `pbc-server.exe` 与 `dist/pbc-server` 的 sha256 **一致**（`6022ad93…`，20 594 735 B）。

**⑥ 用户的硬令在门禁里**没有执行点**（本轮对抗性审查最实的一条）**：用户明令
「**仓库只能有 1 个 `dist-electron`**」。查 `check_dist_variants()` 才发现：两份**完整**
产物并存时它只返回 **WARN**，而 `main()` 的退出码只看 FAIL（模块 docstring 自己写着
"WARN/SKIP 不影响退出码"）⇒ **那条硬令等于没有执行点**，全靠人记 —— 而人（我）上一轮
刚违反过。更刺眼的是这个函数**自己的 docstring** 早就写着"完整产物数 > 1 才是**可能发错
版本的实质风险**"：既然承认是实质风险，就与"只提醒"自相矛盾。
⇒ 把"**≥2 份完整产物**"从 WARN 升为 **FAIL**；**残壳堆积仍保持 WARN**（理由未变：宿主
持有 `app.asar` 时残壳**清不掉**，判 FAIL 会恒红、反而让人忽略整项 —— 那是**可见性**
问题，不是"两份都能跑"）。变异 `r128_dist_variants_mutation.py` ⇒ **4/4 CAUGHT**：
M1 退回 WARN / M2 阈值放宽到 `>2` / M3 删掉"只能有 1 个"的报错文案 / M4 把残壳 WARN
**误升** FAIL（越权方向，也必须红）；阴性对照（改残壳披露措辞）保持绿。
⚠️ 变异脚本自己踩了两个坑，都修了：① 首版把 `PATH` 写成 `/usr/bin:/bin` 的"最小环境"⇒
`test_repo_hygiene` **在收集阶段**调 `git` 就 `WinError 2`，基线直接 rc=2（**必须继承
`os.environ`**，只覆盖旋钮）；② 阴性对照的锚点在**两个分支**里都出现 ⇒ preflight 的
"锚点恰好 1 次"断言当场拦下（**未写文件**，这正是它存在的意义）。

**⑦ 仍未处置（边界）**：git **历史**无法脱敏 —— 删文件抹不掉历史。本轮把数字**测准**
（此前记的"3 个未推送提交"是错的，那是 `-S` 的计数，会把**删除该串的提交**也算进去）：
- 公开快照 `c70a620` **本身**就有真批号（**3 个文件**）⇒ 泄漏**已经发生**，改写历史救不回；
- `c70a620..HEAD` 共 **64** 个提交，其中 **62** 个的快照含该串，直到 `d696581`（本轮脱敏）
  才消失 ⇒ **这 62 个提交从未推送过，但下一次 `push` 会把它们一起公开** ⇒ 必须在
  push 前**改写或压缩历史**；
- 处置顺序（**由用户裁定**）：改写历史 / 转私有 / 删除重建 / 接受已公开。仓库当前仍
  `private: false`；GitHub API 实测 `forks_count=0`、`stargazers=0`、`watchers=0`、
  最后 `pushed_at` = **2026-10-14**（14 天无推送）⇒ 无 fork 需要追，改写/删除的代价比通常小。
另：`config.py:561` 的默认模型与用户裁定冲突，见 TODO **0-69**；`release-archive/` 里的
旧包与当日产物**版本号一字不差**，见 TODO **0-70**。

**⑧ 同一病根的第二、三处（顺着"判不了怎么办"继续审）**：既然 `dist_variants` 的病是
"**WARN 不改退出码 ⇒ 规则没有执行点**"，就把门禁里**所有**返回 WARN 的路径逐条拉出来问
"这条该不该红"。**4 条是刻意设计且有用例钉着的**，保留：`worktree_clean`（无 git）、
`no_pii_in_tracked_files`（缺映射表 —— "没复核"≠"复核通过"）、`artifact_freshness`
（第三态：产物被锁、换不掉，判红会恒红）、`tests_coverage`（已登记的环境专有失败）。
另外 **2 条没有用例、且与紧邻分支自相矛盾**：
- `no_build_outputs`：`git ls-files` 空 ⇒ 旧判 WARN ⇒ **一个文件都没扫过，却报告
  "没有产物入库"**；
- `tests_coverage`：`total is None`（读不出覆盖率）⇒ 旧判 WARN，而**紧邻的上一条分支**
  写着"失败计数与用例解析不一致 → **事实源不可信，fail-closed**" 并判 FAIL ——
  同一件事两种严重度，于是门禁可以一边宣称"覆盖率门禁 95%"、一边**根本没读到那个数**。
⇒ 两条都升 **FAIL**，与既有 fail-closed 先例同源（`artifact_freshness`「清单不在 ⇒ FAIL」、
`dependency_vulns`「快照缺失 ⇒ FAIL」）。补 **2 条用例**（此前**一条都没有** —— 这正是
它们能长期 fail-open 的原因），变异 `r128_failclosed_mutation.py` ⇒ **2/2 CAUGHT**，
阴性对照（改提示措辞）保持绿。

**⑨ #175 的验收条件终于成立（同族的第三处）**：`test_todo_freshness` 的不变式 B
（"TODO 复核戳不得落后于 CHANGELOG 最新轮次"）自 R103 起**静默失效** —— 标题形态漂移成
`—— RNN`，而 `_changelog_max_round()` 只认 `Round NN`、不变式 H 又只查含「第M批」的标题
⇒ 那批标题**对两条检查都隐形**。本轮实测确认**仍未修**：`max` 现在是 128（R128 标题回到了
规范形态），但注入一条 `—— R999` 后 `max` **仍是 128** ⇒ #175 写的验收标准（"注入 `—— R999`
⇒ 必须红"）**当时仍不成立**。
⚠️ **顺带纠正本条自己记的数字**：不是 20 条、也不是 22 条，是 **24 条**、**两种形态** ——
① 22 条完全没有 `Round`；② **2 条两种形态互相矛盾**（`（Round 90 第四十五批）—— R102 首批`），
后者才是"`max` 停在 90"的直接成因，而且更隐蔽（它看起来"有 Round"）。
⇒ 修法取"**只减不增的棘轮**"：24 条进白名单 `_LEGACY_SHORT_FORM_ROUNDS`，
**新增一条短形态立刻红**；回改历史标题属**编号体系决策**（`R<NN>` 与 `Round NN` 是否同一套
计数，MEMORY 记着"两套编号不得换算"），不由 agent 单方面做。白名单会随回改自动缩小。
变异 `r128_175_ratchet_mutation.py` ⇒ **3/3 CAUGHT**（注入 `—— R999` / 检测器恒返回空 /
删掉规范形态例外），两条阴性对照保持绿 —— 其中**关键一条**是"把一条历史标题改规范 ⇒ 必须
仍绿"：棘轮不得**惩罚改进**，否则没人会去清这 24 条。

**⑩ 归档包：把"该发哪一个"从人眼比对变成可机读（0-70 方案 ②）**：`release-archive/`
的便携包**不在 `dist*` 体检范围内**（它不是目录），而它同样是可分发产物。实测
`BatchSentry-Portable-1.2.1-20261013.zip` 包内 `PROVENANCE` = `git_head a225fb8`，
当日重建产物 = `git_head 188609a` —— **版本号一字不差、代码差 70 个提交**（这个数随时间
增长，收尾实跑已是 74）。同一危险 **1.1.9 时已记过一次，这是复发**。
⇒ `clean_dist.py` 新增**只读**体检：`read_archive_provenance`（从 zip 里直接读
`win-unpacked/PROVENANCE.txt`，**不解包**）+ `archive_inventory`（给出 `git_head` /
`built_at` / **落后 HEAD 几个提交** / 是否**同版本不同码**）。文本与 `--json` 两个出口都带上，
并显式声明"**本脚本从不删除归档**"（那是用户资产；方案 ① 递增版本号属发布流程决定，待用户裁定）。
⚠️ **变异首版有一条 MISSED，原因值得记**：M4 是"让 `--apply` 连归档一起丢回收站"，
而用例把 `to_recycle_bin` **mock 成了空桩** ⇒ 断言盯着的副作用被测试自己拆掉了
（与 R120「渲染层用例往 `f` 里塞字段」同类：**mock 掉被断言的副作用 = 用例不再证明任何事**）。
改成走真函数后 **4/4 CAUGHT**。

**⑪ 许可合规终于有事实来源：`license_scan.py`（0-67 的前置）**：0-67（PyMuPDF 是 AGPL）
一直卡在"要用户三选一"，但**连该选什么的事实都没人量过**。本轮补上，并且**三次改掉
自己的错误**，每次都留下证据：

- **扫描域错了**：首版扫整个 `site-packages`（**514** 个包）⇒ 混进 PyQt6 / PySide6 /
  EbookLib / phonemizer-fork 等**根本没入包**的东西，其中 4 个被判 AGPL。真正的判据是
  "这个模块是否被冻进产物"，唯一可信来源是 PyInstaller 的 `PYZ-00.toc`。
  改成 TOC 闭包后：**90 个发行版**（`requirements.txt` 14 个直接依赖、pip-audit 43 个条目
  ⇒ 三条口径差 2–6 倍）。**"入包件"比"依赖清单"大得多**。
- **分类器漏掉最该抓的那一个**：只读 `License-Expression` + `License ::` classifier 时，
  **PyMuPDF 被判成 `unknown`** —— 它两者都没有，只有自由文本 `Dual Licensed - GNU AFFERO
  GPL 3.0 or Artifex Commercial Lic`。90 个里只有 7 个没有结构化字段，其中 6 个是
  `MIT`/`BSD-3-Clause`，**唯一的 copyleft 就是 PyMuPDF** ⇒ 自由文本通道不是兜底装饰，
  它是这条结论的**唯一来源**（同 R115「锚点写窄 = 功能等于没上线」）。
- **认不出的许可证被算成宽松**：第二版把"有 expression 但没命中 copyleft 词表"一律算
  `permissive` = **词表之外一律放行**。实测立刻抓到 `certifi`（**MPL-2.0**，弱 copyleft）
  会被静默放行 ⇒ 改为三档：copyleft 族 / 宽松白名单 / **`other`（声明了但对不上词表，
  原文照抄等人看）**。
- **`unknown` 桶把"要人工看"藏进"不用管"**：实跑输出 `unknown=118`，且表里全打印成
  `(无映射)` —— **模块名根本没显示**。更糟的是里面混着 `win32con`/`win32evtlogutil`/
  `winerror`（= **pywin32**，`License: PSF`）：`packages_distributions()` 对它**没有
  top-level 映射** ⇒ 一个真第三方依赖与 stdlib 同桶。改为 `stdlib` / `first-party` /
  **`unresolved`** 三分，未解析项**逐条点名**。本工具**不猜**（不给 pywin32 硬编码映射）：
  猜出来的结论与"没查过"在报告里长得一样。
- ⚠️ **顺手纠正一条我自己写错的记录**：docstring 原称"`ast.literal_eval` 在 `PYZ-00.toc`
  上报 `unmatched ')'`（第 13756 行）"—— **实测不成立**，它能跑通。正则是"不必建 AST"的
  选择，不是"literal_eval 会炸"。改成**双解析器交叉比对**用例（正则 vs `literal_eval`
  结果必须一致），比钉一条假 bug 有用得多。

**⑫ 产物里冻进了 agent harness 的 `sitecustomize.py`（新单 0-71）**：上面那条"未解析"
桶点名 `sitecustomize`，其 TOC 路径是 `…\WorkBuddyAI\resources\app.asar.unpacked\cli\
vendor\shim\sitecustomize.py` —— 一个 safe-delete shim（把 `os.remove`/`shutil.rmtree`
重定向到回收站）。**先做实验再定性**：临时打一个 `--onefile` 探针，冻结后
`"sitecustomize" in sys.modules` = **False**、`os.remove.__module__` = `nt`
⇒ **冻结应用不执行它**。所以它是**死重量**（外来代码进产物、跨机器构建不可比），
**不是**"生产的删除被重定向"—— 这两种结论的处置完全不同，不能靠猜。


**验证**（两段，分开记，**不合并成一句"全绿"**）：
- **改动前全量门禁**（提交 `470442b`）：**13/13 PASS**（`pass=13 fail=0 warn=0 skip=0`；
  **4904 passed / 0 failed**，覆盖率 **95.32%**，门禁线 95%）。
- **`dist_variants` 升 FAIL 之后**：结构项 `--skip-tests` 除 `worktree_clean`（改动未提交，
  预期）外**全绿**；受影响的 4 个用例文件 **170 passed**；变异 **4/4 CAUGHT**。
  全量复跑结论见下一次提交的 `devlogs/gate_report_*.json`（**不以"结构项绿"代替全量**）。
- **产物级 e2e 两轮对照**：09:13（旧产物）**4/4 covered** —— `llm_pipeline` `success=1`、
  `terminal=review`；10:56（重建后）**2/4**，两条管线都 `terminal=error`，实测原因
  `402 / code=30001 余额不足`（免费端点仍 200）⇒ **重建产物的管线路径本轮未验**，
  属**环境**（充值后复跑），**不得读成已通过**。

### 严重度只留一个真值：删掉 `RuleSpec.severity`（2026-10-27）—— R126 / TODO B3-3

注册表声明 R1a/R1b `severity="warning"`，而实现会发 `critical`。这**不是"文档过时"**：
它是**第二处真值**，而且**没有任何消费点**（全仓唯一的引用是一条"断言它是三个合法值
之一"的用例）⇒ 不一致可以长期无人发现，而任何"以注册表为准"的工具（文档生成、
规则目录页、前端分级）都会拿到错的值。

更根本的是 B1-9 之后**同一规则、同一 type 可发两种严重度**：R1b 真倒序发 `critical`、
仅重叠发 `warning`。**单一字段本身已不足以表达** —— 所以"把声明改对"只是把第二处
真值暂时调齐，下次照样漂。

- `core/rules/registry.py`：删掉 `severity` 字段与 **23 条**构造实参；模块 docstring
  与 `RuleSpec` docstring 就地写明"严重度是**发射结果**的属性，不是注册表的声明"，
  并给出漂移史与"同一 type 两种严重度"这个更根本的理由。
- `core/rules/__init__.py`：元数据清单去掉 `severity`。

护栏 `tests/unit/test_rule_severity_single_truth.py` **9 条**，四组不变式：
字段集合固定（防"加回一个没人校验的声明"）/ 注册表不得传 severity 且位置参数形态固定
（防"字段删了参数还在"）/ 发射字面量必须 ∈ 三值词表 / **R1b 对两种输入发两种严重度**
这个行为实证（删除的理由本身）。变异 `devlogs/_verify/r126_mutate.py` ⇒ **6/6 CAUGHT**。
全量 `tests` **4879 passed / 2 skipped**。

⚠️ **M3 第一版 MISSED**：词表护栏只认 `ast.Constant` 值，于是
`"severity": "warn" if notes else "critical"`（`IfExp`）**整条被跳过** —— "变异"根本没被
检查。改成遍历值的**整棵子树**后才 CAUGHT。顺带发现反向的坑：整棵子树会把
`{"severity": r["severity"]}` 的**键名**（读路径）当成发射值，故下钻时**跳过 `Subscript`**。
⇒ 静态护栏的两个失败方向都得各挡一次：**漏扫**（表达式形态）与**误扫**（读路径）。

### 审计写失败不再静默 + 自述能回答「卡的是哪个 job」（2026-10-27）—— R125 / TODO B2-9

两个洞，都属于**可观测性本身不可信**这一类 —— 比"某个功能坏了"更难发现，
因为它们让"坏了"看起来像"没发生"。

**① 审计写失败只留一条 warning。** `_audit_log` 与"被拒转换"的审计都是
`except Exception: logger.warning(...)`。后果不是"少一条日志"：GMP 追溯链出现
**空洞且无信号**时，事后无法分辨"这件事没发生"与"这件事发生了但没记上"。
三条处置口径：

- 升为 `logger.error`（与"某页重试了一次"不是同一量级，warning 会被日常噪声淹没）；
- 进程内计数 `state.audit_write_failures()`，由自述端点暴露 ⇒ 外部监控**不读日志**
  也能发现"审计在丢"（`uvicorn` 强制 `workers=1` ⇒ 进程内即全局）；
- **不重试** —— `execute`+`commit` 失败时无法判断 INSERT 是否已进入事务，重试
  可能**双写**，反而污染追溯链。条目原文给的"或轻量重试"因此**不采用**。

两个写入点（`_audit_log` / 被拒转换）**共用一个计数** —— 否则自述只覆盖一半路径。

**② 自述只有聚合计数。** `status_snapshot()` 能回答"有几个 job 卡了"，不能回答
"**是哪个**"，而 `find_stalled_jobs()` 本来就能给出，只是没人用。新增
`watchdog.runtime_report()`（端点改调它）：`currently_stalled`（**同一判据**，
不另写查询）+ `currently_stalled_total` / `_truncated`（limit 20，**显式**截断）。

顺带一个只有动手才想到的：这个端点**原本是纯内存**的，加了清单就依赖一次 DB 读
⇒ 库忙 / 库坏会让探针 500，而那正是最需要它的时候。故降级：
`currently_stalled = null` + `currently_stalled_error` 有值 = **没扫成**；
`[]` = **扫了，没有卡单**。两者**绝不合并**（同 R123 `failed_pages` 的 `NULL` vs `[]`：
`[]` 是结论，`null` 是没有结论）。存活字段照常返回。

护栏 `tests/unit/test_r125_audit_and_stall_visibility.py` **12 条**（含"自述只读"
与"清单与 `find_stalled_jobs` 同判据"）；变异 `devlogs/_verify/r125_mutate.py`
⇒ **10/10 CAUGHT**。全量 `tests` **4870 passed / 2 skipped**。

⚠️ 改动让一条**既有**用例红了：`test_config_db_pipeline_coverage.py` 的
`test_audit_log_handles_exception` 断言的是 `Logger.warning`（旧行为）。已改为
断言 `Logger.error` **且**计数上涨 —— 它本来要锁的就是"失败被留痕"，级别是这条
契约的一部分，所以**改断言**而不是改实现。

顺带登记 **#175**（本轮收尾核对时发现，未修）：CHANGELOG 的批号标题自 **R103** 起
改写成 `—— RNN`、不含 `Round` 记号 ⇒ `test_todo_freshness` 的"戳不得落后"
**静默失效 22 轮**（`docs/TODO.md` 的复核戳停在 `Round 90`，机检全绿）。属编号
体系决策，需先定案。

### 每页 KB 检索只跑一次，且审计记录的就是注入的内容（2026-10-27）—— R124 / TODO #168

`analyze_page` 里曾有两段**逐字重复**的 KB 检索：第一段（门槛 `len(cleaned) >= 40`）
构造 prompt，第二段（门槛 `>= 64`）**只**为了给 `llm_call_audit.kb_used` 留痕，
还顺手覆盖了 `kb_block` / `kb_refs_used`。

这不只是"多跑一次 BM25"：两处**门槛不一致** ⇒ 40–63 字的页（短页 + 表格标签
很容易落进这一档）**prompt 注入了条文块，审计却写 `kb_used = None`** ——
审计与"实际发给模型的东西"不符，GMP 追溯里最怕的那类。

- `core/page_analyzer.py`：删掉第二段检索，`kb_refs_used` / `_kb_version` 只有
  **一个来源**（即 prompt 那次）；`_kb_version` 显式初始化为 `""`，不再依赖
  `{...} if kb_refs_used else None` 的求值顺序。
- 门槛仍是 `>= 40` —— 本轮**不改** prompt 内容（改门槛是另一件事），只让审计
  与注入对齐。

护栏 `tests/unit/test_page_analyzer.py::TestKbRetrievalRunsExactlyOnce` **4 条**：
调用次数 / 40–63 档"注入了就必须留痕"（含版本号非空）/ 门槛以下不注入 /
**AST 单调用点**（防有人再抄一份）。变异 `devlogs/_verify/r124_mutate.py`
⇒ **4/4 CAUGHT**。

### 重试清空上一轮失败状态 + 报告携带失败信息（2026-10-27）—— R123 / TODO #147 + #148

两个条目是**一条链**，所以一起修：

- `retry` 只清 `error_message` / `finished_at`，而 `failed_pages` 与三阶段耗时
  **只由 Stage 3 的终态 UPDATE 覆盖** ⇒ 用户点重试之后、Stage 3 跑完之前
  （OCR + 逐页分析，可能几十分钟），复核页 / 任务列表 / 报告读到的都是
  **上一轮**的失败页与耗时。用户刚点重试就看到"2 页失败"，无法分辨是旧结果
  还是本次新失败。
- 报告本身又不携带任何失败信息：失败页的 `structured_json` 是带 `_parse_error`
  的占位对象 —— 既不算"空页"（`_ocr_empty`）也不算"未完成分析"（NULL），
  两个计数器都不覆盖 ⇒ 一份"0 条 finding + 全部页失败"的报告会渲染成
  **"✅ 未发现问题，无需人工复核"**。

**只修报告会更糟**：报告开始展示 `failed_pages`，而它可能还是上一轮的 ——
把"陈旧数据"升级成"看起来可信的错误陈述"。

- `core/pipeline/engine.py`：新增 `reset_run_state()`（**单点**清空
  `failed_pages` / `stage1_ms` / `stage2_ms` / `stage3_ms`，外加原有的
  `error_message` / `finished_at` / `ocr_progress`），返回被清掉的旧值供审计；
  `_run_pipeline_impl` 启动时调用并写 `pipeline_reset`。**不自己 commit** ——
  事务边界归调用方。
- `api/jobs/actions.py::retry_job`：改调同一个 helper（列集合单点），并在
  `db_lock` 内用**裸 INSERT** 写 `retry_reset` 审计（`_audit_log` 自取
  db_lock，非重入）。`total_pages` 不动 —— 那是物理页数。
- `api/report.py`：
  - `report.json` 的 `job` 块新增 `pages_analyzed`（口径复用
    `status._count_analyzed_pages`，排除 `_parse_error` 占位页）/ `failed_pages`
    （NULL **不**伪造成 `[]` —— 两者在 GMP 审阅下含义不同）/ `error_message`；
  - `report.md` 头部新增「任务状态 / 分析失败页 / 失败原因」，「页面覆盖」行
    并入失败页计数；零 findings 分支在有失败页时**不再**输出"无需人工复核"；
    有 findings 时汇总声明"本报告不覆盖第 N 页"；
  - 报告缓存摘要补 `error_message` + **解析后**的 `failed_pages`（同一输入
    不进摘要两次 —— 否则"删掉其中一处"的变异看起来被抓住、实际只是冗余项）。
- `tests/unit/db_write_lock_baseline.json`：`_run_pipeline_impl` 里那条裸写随
  实现搬到 `reset_run_state`（**同一处债务换宿主，总数不变**）。

护栏 `tests/integration/test_r123_failure_state_reset.py` **19 条**（含一条
管道型：`_run_pipeline_impl` 必须真的调 helper —— 只测 helper 证明不了接线）；
变异 `devlogs/_verify/r123_mutate.py` ⇒ **15/15 CAUGHT**。

### 看门狗的心跳改成"每次 LLM 调用一次"（2026-10-27）—— R122 / TODO 0-10

`analyzing` 的停滞阈值原先按"单次调用最坏 480s × 3 次 SDK 尝试 = 1440s"
设基准 1800s。但**心跳点只在整页结束时写**，而一页内部最多 6 次 `chat_json`
（主调用 + 2 次 JSON 修复 + schema 修复 + 其修复链）—— 6 段静默被累加成
一段，合法上界变成 **8640s**，任何阈值都覆盖不住：

- 阈值低于它 ⇒ 把**还在正常重试**的任务判成停滞，而收敛动作是"标记 error +
  取消 task"，等于杀掉一个正在工作的任务；
- 抬到 8640 以上 ⇒ 真正挂死的 job 要等 **2.4 小时**才被发现。

**修法是加心跳，不是抬阈值**：`chat_json` 每次**发起调用前**打点
（`on_call_start`），合法静默退回单次调用的上界 1440s，基准 = 1440 + 600
余量 = **2040s**。

- `llm/client.py`：`chat_json` / `_chat_result` 新增 `on_call_start`，在**每次**
  HTTP 尝试前 await（客户端重试、fix-hint 修复、schema 修复全覆盖）；回调抛
  异常被吞掉 —— 心跳只是可观测性，不能把正常调用打红。
- `core/activity_clock.py`（新）：进程内打点。**刻意不写库** —— Stage 2 多页
  并发共用一条 aiosqlite 连接，在那条连接上随时 `commit()` 会把别的页写到
  一半的事务一并提交（`stage2._mark_page_failed` 已把这条列成已知事故形态）。
- `core/watchdog.py`：判定取 `min(DB 静默, 进程内静默)` ⇒ 进程内心跳**只能让
  job 显得更新**，不会掩盖真挂死；重启后表为空即退化回纯 DB 判据。
- `core/page_analyzer.py`：`page_analysis_silence_bound_s()` 从"整页叠加"改为
  "单次调用"，`_SDK_ATTEMPTS_PER_CALL` 由 adapter 源码推导（`AsyncOpenAI`
  未传 `max_retries` ⇒ SDK 默认 2 ⇒ 3 次尝试）。
- `api/jobs/status.py`：界面"无进展，可取消或重试"提示走**同一口径** —— 否则
  会在 LLM 正常重试时劝用户取消一个正在工作的任务。

**实测依据**（真实库 `llm_call_audit`，186 次 `page_analysis` 调用）：
单次调用 max **760.3s**、p95 266.3s、median 77.2s。max > 480s 这一条本身
就证明 SDK 内部重试**真的发生过**，不是推演。

**护栏** `test_r122_llm_silence_heartbeat.py` **15 条**，其中两条是管道型
（stage2 真把心跳传下去、`analyze_page` 真转发给 `chat_json`）；另加产物侧
`e2e_frozen.py` 不变式④（`analyzing` 基准 ≥ 单次调用上界，**在冻结包上**复核）。
变异 **11/11 CAUGHT**（上界退回整页叠加 / SDK 次数写小 / 修复次数脱钩 / 基准退回
字面量 / 只在首次尝试打点 / fix-hint 不打点 / 打点异常不吞 / 看门狗忽略心跳 /
界面忽略心跳 / stage2 接线断 / analyze_page 不转发）。

⚠️ **只在运行期生效**：这是阈值与心跳口径的变化，不重算任何已有结论。

### 文档级缺陷不再逐页重复报告（2026-10-27）—— R121 / TODO 0-64

实测（`4cffb358`，10 页）复核页上有两张一模一样的卡片：

    第 5 页   completeness  「批生产记录缺少生产日期信息。」
    第 10 页  completeness  「批生产记录缺少生产日期信息。」

缺陷为真（10 页 `raw_html` 里没有任何一页含「生产日期」字段），但**报告了两遍**。

**根因不是"没有聚合逻辑"**——`reduce_document_level_facts`（CR-10）早就在做这件事，
而是**它看不见这些行**：该函数只跑在 Stage 3 的**内存** findings 上，而 `llm_page`
在 Stage 2 每页完成时就已落库、不在那个列表里。于是同一个文档级事实被页级 LLM
逐页报了几次，就有几条。

- 判据**复用** `document_fact_kind`（CR-10/CR-11 已在用的分类器），不新造词表
  —— 两套词表必然漂移；
- 新增 `core/finding_noise.plan_document_level_collapse`（纯函数，产出"改哪行/删哪行"）
  + `stage3._collapse_persisted_document_facts`（只执行计划 + 落审计）；
- 保留行改写成聚合摘要（"全份记录的记录页页眉/页面信息栏无生产日期字段…涉及 2 页"），
  并在 `guard_reason` 里**追加**合并说明 —— R120 起这条说明在界面上可见。

**三道闸门刻意保守**（各自都有用例 + 变异）：

| 闸门 | 理由 |
|---|---|
| 只合并**跨页** | 同页重复是**另一个缺陷**（A9 之前的历史数据，实测 17 组 `llm_page`+`llm_cross` 全在 2026-08/09），合并会把它的症状静默掩盖 |
| 只动 `pending` | 已裁决（confirmed/rejected/corrected）的行是**复核结论**，不能因后来做聚合就消失 |
| 只动 LLM 来源 | 规则层的行是确定性生成，已有 `reduce_completeness_noise` 的阈值治理 |

`guard_reason` 是**追加**不是覆盖：保留行可能已有 R97 的降级理由（实测 p1/p8 就带着
"文案为推测性表述 ⇒ 降级为待人工核对"），覆盖会让复核员看到 info 却不知道为什么。

**护栏** `test_r121_doc_level_collapse.py` **13 条**，其中一条是**管道层**：真跑
`_run_stage3_cross_analysis` 再查 findings 表。这条是补出来的 —— 变异 M5（删掉调用点）
最初**没有被抓住**，因为其余用例直接调 helper，证明不了"线上会执行"。
变异 **6/6 CAUGHT**（跨页门槛 / 已裁决保护 / 来源白名单 / 留痕 / 理由追加 / 接线）。

**真实数据**（库**副本**，不碰用户库）：`4cffb358` 8 条 → 7 条，p5/p10 收敛为 1 条，
其它 job 行数全部不变。

⚠️ **未覆盖**：`所有签名均无日期，无法进行签名时间逻辑检查。`（51 页 job 的 p40/p48）
不含"生产日期"字样 ⇒ 分类器不认，仍是 2 条。要覆盖它得引入"逐页同一结论"这一更一般的
机制并重新定义合并后的文案口径，而实测只有 1 例 ⇒ 不据此扩词表。
⚠️ **只对新任务生效**：已有 job 的行不会自动重算。

### 复核页显示「系统判定说明」（2026-10-27）—— R120 / CR-1 ④

R97 把"为什么降级/收权"的理由从 `description` 里**拆出来**存进 `guard_reason`
（这一步是对的：理由全是内部语，「L2 溯源失败」「LLM 独断 critical」）。但拆完之后
**没有任何界面读它** —— `grep guard_reason static/ templates/` **零命中**。
于是复核员看到一张 `info` 卡片，**看不到为什么是 info**；GMP 要求结论变更可解释，
而"可解释"只存在于数据库里，对使用者等于不存在。

- **页内卡片**：`<details>` 折叠区「系统判定说明」，默认收起 —— R97 刚把这些内部语
  从正文拿掉，不能再摆回去。**SSR 与 JS 两处都加**（`templates/review.html` +
  `static/review-findings.js`）：SSR 是 JS 接管前的首屏，也是 JS 加载失败时唯一的
  可读面，只加 JS 那条路径的话这两种情形仍然看不到理由。
- **跨页总览**：只挂 `title` 悬停提示。那是导航列表，塞一个可展开块会把行高撑乱；
  完整说明在页内卡片里。
- 三处都转义：`guard_reason` 是 LLM/规则拼出来的自由文本，直接进 HTML 就是注入面。

**护栏** `test_r120_guard_reason_ui.py` **10 条**：5 条渲染层（XSS 转义、
"无理由时不得渲染空壳"的反向对照、防空转前提）+ **1 条 API 层** + 4 条 SSR 层
（真渲染模板里那段 Jinja，不是 grep 源码）。

两条是补漏时发现的真缺口：

1. **API 层**：前 5 条**直接往 `f` 里塞** `guard_reason`，所以即使有人把
   `list_findings` 的 `SELECT *` 改成显式列清单而漏掉该字段，**5 条仍然全绿**，
   界面上却永远看不到说明 —— 与 R119 同一形态（判据层绿、管道层丢字段）。
   新用例落一条真 finding 进临时库、调真 `list_findings`、查 payload。
2. **SSR 层**：只改 JS 时 SSR 卡片没有理由。SSR 用例把模板里
   `{% if f.guard_reason %}…{% endif %}` 整块切出来交给 Jinja（`autoescape=True`，
   与线上一致）真渲染，另有一条静态断言禁 `| safe`（那是唯一能让转义失效的写法）。

变异 **7/7 CAUGHT**（`devlogs/_verify/r120_mutate.py`，sha256 逐字节还原 + preflight
校验 7 个变异点各恰好 1 处 + SIGTERM 还原兜底）：显式列清单漏字段 / 卡片不插值 /
理由不转义 / 无理由也渲染空壳 / 总览不挂 title / SSR 没加 / SSR 用 `| safe`。
其中 M1 专门验证"只有 API 那条红、5 条渲染用例仍绿"这一形态。

`test_innerhtml_escaping.py` 等 5 个前端护栏 **58 条仍绿**（新增插值站点被既有
转义机检认作安全）。真实库实测：`findings` 946 条中 **124 条**有 `guard_reason`、
`description` 带 `｜` **0 条**（CR-1 ①③ 在真实数据上成立）。

### 签名栏「姓名 + 日期」缺哪一半，现在由原文栏名判定（2026-10-27）—— R119 / TODO 0-48

用户 2026-10-26 第 12 条裁定的**后半句**（前半句「表头只写签名 ⇒ 不要求日期」
已由 `core/signature_date_need.py` 落地）：「表头写"签名+日期"⇒ 两者必须都有」。
此前只答了"要不要日期"（用于抑制无据断言），**没答"这一格到底缺了哪一半"**。

新增 `core/signature_pairing.py`，只看**行内「栏名：值」**，两半分别判，
**严重度刻意不同**（证据强度不同）：

| 形态 | 结论 | 严重度 |
|---|---|---|
| `复核人：2025.02.21`（有日期无姓名） | 只识别到日期，没有签名姓名 | `info` |
| `中控QA签名/日期：赵阿珍`（栏名要求日期，只有姓名） | 只识别到姓名，没有填写日期 | `warning` |
| `复核人：付付 2025.02.21`（两半都有） | 不报 | — |
| `检验日期：2025.02.10`（日期格，不是签名位） | 不报 | — |

**为什么两半不同级**：TODO **0-63** 已登记「PaddleOCR-VL 对连笔手写姓名系统性
漏识别，日期/印刷体正常」——「有日期、没姓名」正是那个已知 OCR 缺口的形态，
按 `warning` 报等于把 OCR 缺陷记到记录员头上（本仓库反复踩的归因错误）。
反过来「有姓名、没日期」里日期半是**可靠**的，才配 `warning`。

**为什么不能在结构化层判**：`signatures[]` 里「有姓名、无日期」实测 **106 条**，
绝大多数是抽取器没抓 `sign_time`（真实语料 p13 的日期写作「2025年01月20日09时05分」，
`sign_time` 全空）⇒ 按结构化层判这一半会造出上百条误报。只有**原文里看得见的
栏名**才是证据（与 `signature_date_need.py` 同一条理由）。

**管道**：`stage3` 早就在 SELECT `raw_html`，只是没往下传 ⇒ 现在带进
`page_structures`，`_normalize_pages` 保留为页面级 `raw_html`，`_check_completeness`
调用判据。**任一层漏传，整条判据静默失效**（R105 的同型陷阱），故有专门用例盯
归一化那一步 + 变异 M6。

**实测（两份真实批记录 61 页，逐条回原文核对）**：命中 **10 条**，全部能在原文里
找到对应空格（p29/p30 `QC 批准/日期：2025.02.21`、p31/p32 `复核人：2025.02.21`、
p12 `负责人：2015.07.21`、p9 两处、p4 中控QA 只签名不写日期）。
**两类刻意弃权**：① p2「车间负责人审核/日期：王2025.01.30」—— 姓名只认到姓，
单字也算"有姓名痕迹"，报"没签名"是拿 OCR 残缺当记录缺陷；
② p13 的「检验日期」「生产日期」—— 日期格不是签名位，收进来会一片误报。

**验证**：`test_r119_signature_pairing.py` **19 条** + `test_r119_stage3_plumbing.py`
**3 条** + 变异 **6/6 CAUGHT**（含"判据写好了但规则层不调用"和"归一化时丢字段"
两条管道型变异，每条后 sha256 逐字节还原）。

**真实数据端到端（`devlogs/_verify/r119_stage3_e2e.py`）**：把两个真实 job 的
`page_cache` 副本喂给**真实 `_run_stage3_cross_analysis`**（LLM 两条支路打桩），
落库 **10 条**（10 页 job 3 条、51 页 job 7 条），与规则层直接命中的 10 条**逐条一致**
⇒ 这条判据在「SQL 取 raw_html → 归一化 → 规则 → 跨源去重 → INSERT」全链路上
不会被 `has_business_text`、`(page,type)` 去重或 UNIQUE 索引静默吞掉。
这一步是必要的：`scripts/replay_rules.py` 只喂 `structured_json`，**看不到**
`raw_html`，用它验证这条判据会得到"0 命中"的假结论。

**降噪层不会吞掉它**：`finding_noise.noise_group_key` 只对 `ocr_text` 命中
`STRUCTURAL_MARKERS`（`operator=空` 等）或 `type ∈ {spec_unverifiable}` 的条目
分组聚合；本判据的 `ocr_text` 是 `label=… date_only=…`，`completeness` 也不在
可聚合集合里 ⇒ `None` ⇒ **原样保留**（已按代码路径核对，未跑端到端）。

⚠️ **与既有跨源去重的关系（已用用例钉住，不是推演）**：我先写成"规则与 LLM 会各出
一条"，核对 `stage3._overlap_covered_by` 后发现**说过头了** —— 它的 `rule_covered`
用**精确类型**（不是族），所以结论取决于 LLM 把同一格报成什么类型：

- 报成 `completeness` ⇒ **已被抑制**（`_overlap_covered_by` 返回 `"rule"`），不会各出一条；
- 报成 `signature_time_anomaly`（该类型是垃圾桶，实测常把"签名缺失"塞进来）
  ⇒ **仍会各出一条**。

**不**把 completeness 并进 signature 族来堵这个缺口：那会让一条「签名日期倒置」
静默吞掉 LLM 独立观察到的「姓名不符」—— 两个不同缺陷，属漏报，比噪声严重
（该函数注释已写明这条口径）。两条行为各有用例钉住。

### 首屏等待的归因校正：不是冻结包的锅（2026-10-27）—— R118

用户 `backend-boot.log` 里同一条 exe、同一台机，启动耗时是**双峰**的：
热 **1.1s**（10-26 11:07）、冷 **31.4s**（10-27 08:04）。冷的那次切分：
spawn → `[PBC] Starting server` **15.3s**（PyInstaller 引导）、
`Starting server` → `main: Starting BatchSentry` **15.4s**（import 应用）、
→ `/health` 0.6s —— **两段都慢约 30×**，所以不是 import 图的问题，是文件缓存/
杀软逐文件扫描。

旁证：直接量冻结包 3 次 = 1.57 / 1.57 / 1.06s，与源码 `server.py`（1.59s）
**没有差别**。上一轮"冻结包比源码慢 7–9×"的结论是拿**冷**冻结包比**热**源码
得出的，**属归因错误**，已在 `electron/main.js` 注释里更正并写明取证数字。

⇒ 这段等待在代码层消不掉，唯一能做的是别让用户对着白屏猜（R117 已改为每秒
真跳 + 明确阶段）。splash 文案同时去掉"首次启动"——缓存随时可能被清掉
（重启/杀软扫描/换版本），说"读取程序文件较慢时"才是真的。

### 检查人 ⇄ 复核人签署日期必须相同（2026-10-27）—— R111 P7/P8

用户 2026-10-27 **改判**：上一轮（R96/CR-28）把「复核人日期早于检查人日期」
判为**不是缺陷**，这一轮推翻 ——

> 「一般来说，复核人和检查人的日期是一致的（一样，早于或者是晚于都需要报异常），
>   检查人日期和复核人日期是一致的也就是相同的日期。」

三个层次同时落地，缺一层就会漏：

1. **确定性规则 R9b**（`core/rules/rule_time.py`）：逐人逐日核，**两个日期都要报**。
   人数不等时弃权（`len(checkers) != len(reviewers)`）—— 一方缺人时 `zip`
   会把 A 的日期配给 B，得出假倒置。
2. **启发式层**（`core/finding_quality.is_review_check_date_claim` + 守卫链
   `_raise_review_check_date`）：LLM 自然语言写的同一事实，`info` 抬到
   `warning`（只抬不降）。角色词必须带**排除尾巴**：裸 `检查` 会吃掉
   「检查**完成**日期」、裸 `复核` 会吃掉「复核**完成**日期」，两个都是无关表述。
3. **去重层**：同一页 R9b 已给出含姓名与两个日期的结论时，LLM 那句同义表述
   抑制（`guard_layer=R111-dedup`）。**放在来源闸门之后** —— `user_rule` 是用户
   自己写的判据，不替它去重。

P8（`8df2f568` p4 工序12 复核人栏空白未报）：根因在 `_check_completeness` 的
`continue` —— 工序**没有起止时间**时直接跳过，连签名槽一起跳过了。而真实页
恰恰是"无时间的纯签字行"。抽出 `_blank_slot_findings()`，无时间分支也要走。
标题行（无操作、无签名）仍然跳过，否则每页都会多一条。

**验证**：`test_r111_signature_pair.py` 36 条 + 变异 **15/15 CAUGHT**；
真实库回放确认 p7 只出一条（去重前是两条），p8 出「有 1 处复核人签名栏为空」。

### 失败横幅的「下一步」只留一份文案（2026-10-27）—— R113

用户原话：任务失败后显示「…可点击右上角"重试"**重新提交任务**；如反复失败，
请检查设置页面 LLM/OCR 配置或日志。」两个错：

1. **retry 是续跑，不是重新提交**：复用 `raw_html`（Stage 1 走 `stage1_skipped`）、
   Stage 2 跳过已分析页。用户以为白跑一遍，不敢点。
2. 无条件把原因归到 LLM/OCR 配置 —— 最常见的失败其实是**被中断**（构建脚本强杀、
   关机），与配置无关。

根因是**两处各写一份**（SSR 模板 + SSE 横幅），必然漂移。现在文案只在
`static/review-progress.js` 的 `JOB_ERROR_HINT` 里，模板只渲染空占位
`#job-error-hint`，JS 两条路径都填。「发生了什么 / 完成多少 / 能否续跑」由后端
`error_message` 说（它掌握真实进度），前端不再复述。

> 后端那半句 R103 已改好。用户看到的是**旧包**里的字符串 —— 重新构建即消失。

**验证**：`test_r113_error_hint.py` 8 条（单源、占位为空、两条路径都填、
不含"重新提交"、必须带"如反复失败"前提）。

### 历史列表不再显示裸任务号（2026-10-27）—— R114

文件名下面是 `8df2f568-3b3c-…`（UUID），对用户无意义。改为「2026年1月1日 10:00」，
任务号降级到 `title`（报障时仍可查）。

两个不显然的点：

1. **不用 `new Date()`**。后端给的是 `"2026-01-01 10:00"`，**不是** ISO 8601，
   部分引擎 `new Date()` 得到 `Invalid Date` ⇒ 直接正则拆。
2. **不用"今天/昨天"**。相对时间要读时钟，会让用例随时间变红 —— 这类"友好"
   换来的是不确定的测试。
3. `createdAtFromMeta()` 此前是**从可见文案里反解**（`parts[1]`）；摘要行一改版
   就静默失效。改为读 `li.dataset.createdAt`。

**验证**：`test_upload_jobs_js.py` 46 条（含 UUID 不可见 + `title` 保留）。

### 有效期按**区间**判定，不再当时间点（2026-10-27）—— R115

用户裁定：

> 「BatchSentry 一定声称有效期是**时间段**（比如 2025.11.02-2026.11.01），而不是
>   时间点；检查人所填日期会落在仪器的有效期的时间段里 —— 我在 2026.02.06 检查，
>   有效期是 2025.03.05-2026.03.04，仪器处于有效期。」

判定收敛到**唯一实现** `validity_verdict()`（四态，区别在**证据强度**）：

| status | 含义 | 能否证伪 |
|---|---|---|
| `expired` | 检查日期晚于区间终点 | 单点也能证 |
| `before_start` | 早于区间起点（方向相反，文案必须分开） | 需完整区间 |
| `inside` | 落在区间内 | **可证伪** ⇒ 抑制"已过期"断言 |
| `not_expired` | 只有到期日，只能证"没超期" | **不能**证"在有效期内" |

真实数据取证（946 条 findings）：有效期类断言只有 **5 条，且 0 条带区间**。
旧实现只认「检查日期」锚点，而真实文案写的是「**称量操作日期**」⇒
补说明的命中率是 **0/5**，功能等于没上线。锚点改为"日期角色"词族后 **1/5**
（`8df2f568` p3 补上「已超出 4 天；有效期区间起点未识别，请核对原页」），
其余 4 条**正确弃权**（无成对日期 / 签名日期不当锚点 / 复选框条目）。

锚点刻意**不含**「签名日期」：`4cffb358` p8 会把签名日期当落点，得出
"签名早于有效期 ⇒ 时间倒置"这种没有意义的比较。也**不单列**「称量操作日期」
（是「操作日期」的超串，变异实测删掉它零用例变红）—— 少一个词就少一处漂移。

**验证**：`test_r107_validity_interval.py` 42 条；变异 **16/16 CAUGHT**
（含"单点报 inside"这条用户明令禁止的方向）。

### 审计面板去工程化（2026-10-27）—— R116

用户看到的是 `status=review findings=33 failed=0 total=226855ms`、
`findings_overlap_suppressed count=3 …{"page":4}`、以及
`deepseek-ai/DeepSeek-V3.2 | 16898 ms | TOKEN 3462`。

改：动作名映射（20 个）、`detailZh()` 按动作分支翻译（10 个分支，未知动作
**原样透传**，不猜）、耗时换秒、token 加千分位、每个 tab 顶部一句"这张表是干什么的"。

**信息不丢失**是硬约束：单元格改成 `{text, title}`，人话在 `text`，原始机器串
留在 `title`（悬停可查）。`fmtMs(null)` 返回「耗时未知」而不是 `<1 秒` ——
`Number(null) === 0`，审计语境里"未知"被读成"很快"是错误结论。

**验证**：`test_review_audit_js.py` 32 条，夹具直接用真实 `audit_log` 里的字符串。

### 首屏 30s 白屏与"回主页有点慢"：先量再改（2026-10-27）—— R117

两个现象都**不是后端慢**，都量过：

- **首屏**：`backend-boot.log` 显示 `[PBC] Starting server` → `Started server
  process` 之间 **15.4s 全在模块 import**；应用自身初始化（`Starting BatchSentry`
  → `Application startup complete`）只有 **51ms**。源码 `import main` 1.65s，
  冻结包 15.4s（7–9×，665 个模块，大头是 FastAPI ~600ms + pymupdf ~115ms）。
  ⇒ 不重构打包的前提下这段等待**消不掉**；能改的是别让用户对着白屏猜：
  秒数按真实时钟**每秒**跳（旧实现每 20 轮 ≈ 10s 才刷一次，看起来像卡死），
  文案从「仍在等待后端就绪」（会让人去查 DB/配置）改成「正在加载分析引擎」。
- **回主页**：`GET /` 5ms、`GET /api/jobs` 9ms。慢的是级联入场动画 ——
  `--i = 序号 + 3`，第 20 行 = 22 × 40ms = 880ms 延迟 + 200ms 动画 ≈ 1.08s。
  延迟**封顶 6 级**（240ms）。

**验证**：`test_r117_startup_ux.py` 6 条（含"秒数必须每轮刷、日志才按 20 轮"）。

### `production_date` 只取生产语义（2026-10-26）—— R109 CR-24

`0c5cfb00` p1 整页**没有**「生产日期」栏，只有「执行日期：2022.06.01」
（SOP 执行日期）与「产品批号：1127011N 250101」。LLM 把执行日期填进了
`page_info.production_date` ⇒ 该页日期锚点比真实生产日期早 **2.5 年**。
`page_anchor_date` 直接读它，所以这不是一条误报，是**一整页的判定基准偏移**。

用户 2026-10-22 已确认「2022 年生效的空白记录用于 2025 年批次是合规的」——
不是"文件过期"，是**取错字段**。

改：新增 `core/production_date.py`（四态：原文有「（开始）生产日期」⇒ 取字面；
只有执行/生效/批准…标签 ⇒ **清空不猜**；其余保留），接在 **Stage 2 落库前**，
`source` 写进 `page_info.production_date_source`；prompt v5 → **v6**。

两个不显然的点：
1. **不修 OCR 读错的年份**。p2/p3 原文写的就是 `2015.01.20`（批号却是 `250101`）
   —— 那是识别错误，由 `suspicious_date` 报"相差 10 年"。悄悄改成 2025 就是编造。
   **分字段 ≠ 猜数值。**
2. **同一天不同写法不动格式**。第一版"取字面"不比语义，把 **80 页**的日期写法
   全改了一遍（`2025-01-20` → `2025年01月20日`）—— 语义没变、diff 铺满全库。
   改成"只有语义不同才改"后真正变化的只有 **9 页**。教训：回放时只数"改了多少条"
   不够，要看"改的是什么"。

**净效果**（176 页）：p1 清空 **3** 页（`2022.06.01` → 空，交由签名日期兜底）；
原本为空、从原文补齐 **6** 页。source：`literal` 77 / `model` 5 /
`cleared_nonproduction` 3 / `absent` 91。

**验证**：单元 15 条（6 条反向控制）；变异 **33/33 CAUGHT**（M30 判据不触发 /
M31 不比语义 / M32 没接进 Stage 2 / M33 不认生产日期标签）；影响面 439 passed。

**产物**：`dist-electron\win-unpacked`，`git_head 5de1489`，built_at 16:33:47
（`run_id 20261026-163003-4200`，rc=0）；全量门禁 **4680 passed / 0 failed /
coverage 95.18%**，结构项 **pass=11 / fail=0 / warn=0**。

⚠️ **没做**：**没回填历史库** —— 已有 job 仍是 `2022.06.01`，回填会改变已完成
job 的 findings 且规则层不会自动重算。新跑的任务自动生效。

### 签名是否要带日期，由表头决定（2026-10-26）—— R108 P1-5

用户 2026-10-26 第 12 条：表头写"签名"⇒ 不要求日期；表头写"签名+日期"⇒ 两者都要。

实测语料里含签名列的表头只有两种写法，泾渭分明：

| 写法 | 张数 | 签名格里是什么 |
|---|---|---|
| `序号 \| 岗位操作指导 \| 操作参数 \| 记录数据 \| 签名` | **104** | 「操作者郑旭 复核者吴伟胜」——没有日期格 |
| `步骤 \| 执行人签名和日期 \| 检查人签名和日期` | **8** | 「赵阿珍 2025.06.03」——日期必须写 |

表单把要求写得清清楚楚，系统却按"签名就该带日期"的常识，在 104 张表上报了
**29 条**「所有签名均无日期，无法进行时间逻辑检查」。这不是"漏了日期"，
是**要求了一个表单没提出的要求**。

改：新增 `core/signature_date_need.py`（三态判据：要日期 / 不要求 / 看不清弃权），
接在守卫链；只抑制"纯日期断言"，只抑制 LLM 来源；prompt v4 → **v5** 把这条要求
写进单页分析规范。

三条不显然的点：
1. **`None` 不是 `False` 的别名**。看不到签名列表头就下"表单不要求日期"的结论，
   等于用"我没看见"证明"它不存在"。弃权必须单独一态。
2. **日期缺失的正则必须紧邻**。第一版放宽成"日期…6 字内…缺失"，抑制 40 条，
   逐条核出 11 条错 ——「已记录操作**时间**但**缺少**操作者签名」（规则层，签名
   没签）、「签名字段…中**姓名缺失**，仅识别出**日期**」（姓名没写）。收紧成宾语
   形态后 40 → 29。抑制类判据的错误方向是"吞掉真缺陷"，比漏判严重。
3. **复合文案不能连坐**。「步骤5和步骤7签名缺日期；步骤6缺少复核者签名」里后半句
   是真缺陷。判据逐分句，每句都得是签名+日期断言且不含其他缺陷措辞。

顺手把 `test_page_analyzer.py` 的 `startswith("v4")` 改成继承性断言 —— 钉死版本号
的断言每次升版都要改，改着改着就变成"和常量相等"而失去意义。

**验证**：单元 22 条（5 条反向控制）；变异 **29/29 CAUGHT**（M24 看不清当不要求 /
M25 不认「签名和日期」/ M26 正则跨词 / M27 不查其他缺陷 / M28 没接进写路径 /
M29 不过滤来源）；真实语料 791 条 → 抑制 **29 条**，逐条核过无误伤；影响面 411 passed。

**产物**：`dist-electron\win-unpacked`，`git_head 2544479`，built_at 15:58:42
（`run_id 20261026-155406-21676`，rc=0）；全量门禁 **4665 passed / 0 failed /
coverage 95.19%**，结构项 **pass=11 / fail=0 / warn=0**。

⚠️ **没做**：`defect_target`（复合文案拆条，用户第 1 条"步骤12没有指出"的另一半）
与 P2-2（`rowspan` 向下填充）同批，仍在 TODO。

### 有效期的日期比较不是「参数超标」（2026-10-26）—— R107 P1-2

`8df2f568` p2 `#1638`（`llm_page` / `info`）：
「电子台秤(A-FW01-307-4005)的校验有效期(2025.05.30)早于检查日期(2025.06.03)，
设备可能超期未校验」—— 复核页标题渲染成**「参数超标」**。

两个独立的错：① 比的是两个日期，`param_out_of_spec` 是数值超差类型，这里不存在
任何规格区间；② 有效期是**区间**，只印 `2025.05.30` 读起来像"有效期就是这一天"。

改：
1. `finding_quality._DESC_TYPE_SIGNALS` **最前面**加一组信号（有效期/复验期/失效期 +
   早于/晚于 ⇒ `suspicious_date`，UI「日期可疑」）。走 R95 已有的"过宽类型按文案
   纠正"路径 —— `param_out_of_spec` 本来就在过宽集合里，缺的只是这条信号。
2. 新增 `validity_overdue_clause()`，守卫链写路径只**补**两件能算出来的事：到期日与
   检查日期相差几天、区间起点是否识别。不改写原文、不编造起点。

两个不显然的点：
1. **文档写的位置是错的**。TODO 原计划加在 `finding_aggregate.expected_type()`，
   但那个函数只被 `rule_spec.py` 两处调用（规则层数值 OOS），而这条 finding 的
   `source` 是 `llm_page` —— 加在那里等于没修，而且不会让任何测试变红。
2. **不照模板写区间**。原模板是「区间 2024.05.31–2025.05.30」，但原文只有到期日，
   起点没识别到 —— 照写就是编造。且"有两个日期"不等于"是区间"：两个日期之间必须
   有连接词（至/到/~/横线），否则 `2024.01.01 生产` 会被当成有效期起点。

**验证**：单元 14 条（4 条反向控制）；变异 **23/23 CAUGHT**（M19 信号指回
`param_out_of_spec` / M20 不判没超期 / M21 不校验区间连接词 / M22 补注没接进写路径 /
M23 不防重复追加）；真实语料 791 条 → 类型改判 **1 条** + 补注 **1 条**，无误伤；
影响面 298 passed。

**产物**：`dist-electron\win-unpacked`，`git_head ef79305`，built_at 15:28:29
（`run_id 20261026-152350-19112`，rc=0）；全量门禁 **4643 passed / 0 failed /
coverage 95.18%**，结构项 **pass=11 / fail=0 / warn=0**。

⚠️ **没做**：区间起点没补进 `structured_json` —— 抽取层没识别到就是没有，写进去是
假数据；真正的修法在抽取层，与 P2-2 同批。p2 那条 `completeness`（勾选了但不在
有效期）用户点名表扬，是真命中，未动。

### 整栏全空 = 抽取不可判，不再当"记录缺失"（2026-10-26）—— R106 P1-3 之一

「丝裂霉素提取关键设备清单」的**确认**栏，`raw_html` 里每一行都是空 `<td></td>`
（13 个），LLM 据此发「所有设备的'确认'栏均为空白，缺少确认签名或标记」。
用户看原页：**那一列是打了钩的** —— OCR 没取到手写勾选。

一整列所有行都空是**抽取失败**的形态（人手填一列极少一行都不填），但纯文本分不出
"没填"与"没识别" ⇒ 唯一诚实的结论是**不可判**。与 CR-4（`signature_unreadable`）
同一条原则：证据不可靠时不下缺陷结论。

改：新增 `core/blank_column.py`，接在守卫链写路径（`apply_review`）——
把"某列整列空"的断言从 `completeness` 改判为 `ocr_noise`（技术噪音类，
不映射 GMP 依据），文案换成「第N页「确认」栏整列未识别到任何内容（13 行全空），
无法判断是未填写还是 OCR 未识别，请核对原页」，原文进 `guard_reason`。

三个不显然的点：
1. **位置在守卫链不在规则层**：规则层吃结构化数据，"整列空"这个信息在结构化时
   已经丢了；只有守卫链同时握着 `raw_by_page`。
2. **必须展开 rowspan**：实测那张表第一列带 `rowspan="2"/"4"`，后续行少一个 `<td>`，
   按"列数相等"筛选会把整表筛掉 ⇒ 检测器**静默失效**（看起来"没命中"）。
3. **保守优先**：四条同时满足才算整列空（表头像表头、≥2 行、表格本身有内容、
   该列全空），文案侧还要求系统性措辞与列名**同句**出现 —— 错改会把真缺陷
   洗成"识别问题"，比漏判严重。

**验证**：单元 20 条（4 条反向控制）；变异 **18/18 CAUGHT**（M14 列名匹配恒假 /
M15 去掉系统性措辞会误改单格缺失 / M16 不展开 rowspan / M17 空表被误判 /
M18 判据没接进写路径）；真实语料 791 条 → 改判 **3 条**（3 个 job 的同型确认栏），
无误伤；影响面 243 passed（含 `test_type_sync` / 导入图契约 / 单实现护栏）。

⚠️ **没做**：`rowspan` 向下填充（步骤 6/7 合并签名只落第一行）属抽取层，P2-2 仍开着；
真正"整页空白"的表格仍报 `completeness`（反向控制钉住）。

**产物**：`dist-electron\win-unpacked`，`git_head 85f91d2`，built_at 14:41:26
（`run_id 20261026-143655-9620`，rc=0）；全量门禁 **4629 passed / 0 failed /
coverage 95.17%**，结构项 **pass=11 / fail=0 / warn=0**。

### LLM 的"同一人复核"断言不得建立在规则层已判不可靠的读数上（2026-10-26）—— R105 P1-4

p4 同一页两条互相拆台的结论：规则层 #1654 说「签名辨认**不可靠**…**无法排除**
同一人复核，请核对原页笔迹」（info），`llm_cross` #1655 却说
「操作人'王小雨'与复核人'王小雨'**为同一人**，违反职责分离」（warning）。
用户看原页：执行人=王小雨、检查人=**沈雅晨** ⇒ 后者是误报。

根因：`structured_json` 的 `checker` 字段 **20/20 全空**、`reviewer` 只有两条，
**从来没有一条是"王小雨"** —— LLM 读不到复核人，就把同一行的 operator 名字
复制进 reviewer 槽，再据此断言"同一人"。CR-4（R98）已经把**规则层**收敛成
"只发 info + 请核对原页"，但 LLM 没跟着收敛。

改：`stage3` 跨源去重处新增「前提不可判」判据 —— 规则层对某页发过
`signature_unreadable`，该页的 LLM `self_review`（`llm_cross` / `llm_fallback`）
抑制，明细进审计 `covered_by=rule_unreadable`。

判据是**页级**的：CR-4 的 `signature_unreadable` 只在"该页所有工序对都至少
一侧 `confidence: low`"时才发，本身就是"整页没有一对可靠签名"的结论。
规则层若真有一对可靠签名，它会直接发 `self_review`，那时 `rule_covered` 的
精确类型去重已经拦住 LLM 的重复。

⚠️ **实现与原计划的偏差**：TODO 里写的是"降级为 `signature_unreadable`"，
但规则层对同一页已经发了一条同类 info —— 降级会得到两条说同一件事的卡片，
所以改为抑制（保留规则层那条）。信息没丢：规则文案已点明工序号与原始识别串，
抑制明细可审计回看。

**验证**：单元 19 条 + 管道级 2 条（真跑 stage3，含反向控制）；
变异 **13/13 CAUGHT**（M10 集合恒空 = 判据写了但不触发；M11 不限类型会连带
抑制同页真缺陷；M12 不限来源会让 LLM 自己说"读不清"就放行；M13 会把用户显式
规则也吞掉）；真实语料 6 job / **791 条 → 抑制 1 条**，无连带误伤。

⚠️ **已知边界**：`llm_page` 版不在范围（Stage 2 落库时规则层还没跑，
要覆盖得新增一条改已落库行的写路径）。781 条里没有一例。

### 数据目录锁（启动恢复/看门狗不再抢别人的活任务）+ 合规声明不入库（2026-10-26）—— R104

R103 只隔离了冒烟用的**端口与 APPDATA**，治的是那条触发路径。本轮修根。

**P0-3 · 启动恢复没有 liveness 判据**

`recover_stuck_jobs` 的判据只有
`status IN (非终态) AND created_at < 进程启动时刻`。第二个进程（冒烟实例 /
dev uvicorn / 用户再点一次 exe）连同一个 `%APPDATA%\PBC\data.db` 启动，
就会把第一个进程**正在跑**的 job 判成 error —— 端口不冲突也照样发生。
运行时看门狗按 `last_activity_at` 判停滞，是同一个坑的另一种触发方式，
一并纳入。

改：新增 `core/instance_lock.py`（`msvcrt.locking` / `fcntl.flock`，无新依赖）。
**只有拿到 `<db_dir>/server.lock` 独占锁的进程**才执行启动恢复**和**看门狗，
关停时释放。

不用 `owner_pid` + 心跳的理由：崩溃恢复与"另一个进程还活着"在时间上不可区分。
进程刚被杀时心跳必然"很新"，按心跳判活会**漏掉真正的崩溃** ⇒ job 永远停在
非终态（无终态黑洞，比误判更糟）。锁没有这个矛盾 —— 持锁者一死，内核立刻释放。

锁跟着**库**走（`database_path` 的父目录），不跟 APPDATA 走：测试把库指到
`tmp_path` 时锁自动隔离，否则跑测试会去抢用户真实 APPDATA 里的锁。

**P1-6 · 自证"符合要求"的条目仍被入库**

`8df2f568` p9 有一条 `self_review`：「……操作人与复核人不同，**符合要求**。
但需注意……」—— 前半句自己在说没问题，却作为缺陷入库。复核员会因此认为
系统把合规项也当缺陷。

改：`core/finding_quality.is_compliant_claim()`（纯文案，叶子模块）——
剥掉引号内容 → 通篇无缺陷词 → 出现结论性合规表述。接在
`review_llm_findings` 的来源过滤**之前**（与 CR-28/CR-6 同位置，`user_rule` 一并覆盖）。

两个反向控制都是真实反例换来的：
1. `不符合要求` 含 `符合要求` —— 少了缺陷词闸门会把"不合规"读成"合规"（方向反了）；
2. `0c5cfb00` p15 的**检查项名字**里带"符合要求"（「S2103是否符合要求」勾选为"否"）
   —— 不剥引号会吞掉一条真缺陷。**这条是回放 781 条真实 finding 才发现的**：
   第一版命中 2 条，其中 1 条是误抑制。

净效果：5 份语料 **781 条 → 抑制 1 条**（就是 p9 那条）；用户表扬的
p2「勾选为"否"」那条不受影响（没有结论性合规表述）。

**验证**：`test_r104_instance_lock.py` 15 条 + `test_r104_compliant_claim.py` 13 条；
**跨进程探针** `r104_lock_probe.py`（父持锁 → 子 `BLOCKED`，放锁 → `ALLOWED`）；
**应用级 e2e** `r104_e2e_lock.py` **3/3**（真起两个 `server.py`：第二个日志"跳过启动恢复"
且 `/health` 正常，持锁者退出后第三个不再跳过）；变异 **9/9 CAUGHT**
（M2 = 只开文件不加锁，最像"已修好"的假修复；M7 = 不剥引号，会吞真缺陷）；
全量门禁 **4588 passed / 0 failed / coverage 95.15%**。

产物已重打：`dist-electron\win-unpacked`，`git_head a5da310`，built_at 13:41:58
（`run_id 20261026-133221-24016`，rc=0）；入包清单里 `core/instance_lock.py` 等
4 个改动文件的 sha256 与磁盘一致；冒烟 `[OK] /health OK (port 58799)`；
门禁结构项复跑 **pass=10 / fail=0 / warn=0**。

⚠️ e2e 第一版 FAIL 过一条，原因记在这里免得后人重踩：用 `DATABASE_PATH` 环境变量
把库指到临时目录**不生效** —— `config._load_json_config()` 会用 `config.json` 的
扁平键（`DATABASE_PATH=data/pharma.db`）无条件覆盖 `os.environ`。改成
"临时目录当 CWD + 改写 `config.json`"才隔离干净。

⚠️ Windows 实测：持锁期间**其他句柄连读都被拒**（`PermissionError`），
所以锁文件里的 pid 只能当**事后**排障线索，不能当判据 —— "谁活着"必须问内核。

### 构建脚本不再强杀用户任务 + 中断文案诚实化（2026-10-26）—— R103 P0

**事故（实测，非推测）**：用户在 `dist-electron\win-unpacked` 上跑一份 51 页 /
138 MB 的批记录，界面显示"跑到 35 页任务处理失败"。

`audit_log` + `llm_call_audit` 对齐后：OCR 在 10:50:32 就 51/51 全部完成
（`page_cache` 51 页俱在），Stage 2 从 10:51:14 跑到 11:07:07 分析了 33 页
（`MAX(page)=35`、33 条 `llm_call_audit`），**11:07:10 被 `stuck_recovery`
判成 error**。判成 error 的原因是 11:07:07 后端进程被强杀 ——
而强杀它的是**我同一时段跑的 `build.ps1`**：

```powershell
# 旧代码（build.ps1 2.5 步）：把占用 58765 的 pbc-server 当"旧产物"杀掉
if ($holderName -eq "pbc-server") { Stop-Process -Id $holderPid -Force }
```

它分不清"上次构建留下的死进程"与"用户正在跑批的后端"。

**改：**

1. **冒烟测试改用专用端口**（`-SmokePort`，默认 58799），删掉对 58765 的
   `Stop-Process`；端口被占就顺延，**不终止占用者**；
2. **冒烟子进程用独立 APPDATA**（临时目录，只复制 `config.json`）——
   端口不冲突也**不够**：第二个实例启动时的 `recover_stuck_jobs` 只按
   `created_at < 进程启动时刻` 判定，会把另一个进程的活任务判成 error。
   构建脚本从此不碰用户 `data.db`；
3. **中断文案诚实化**：`应用重启恢复：原状态 analyzing 非终态，标记为 error
   供重试` → `任务被中断（已完成 33/51 页分析）。OCR 结果与已分析页已保留，
   点「重试」会从中断处继续，不会重复消耗配额。`
   完成度**取 `page_cache.analyzed_at`**，不取 `ocr_progress` —— 后者在
   `analyzing` 阶段早已是 100%，会把 33/51 说成 51/51。

**续跑本来就有**：`retry` 从 `error` 转 `pending`，Stage 1 复用 `raw_html`
（`stage1_skipped`），Stage 2 用 `_get_analyzed_pages` 跳过已分析页
⇒ 那 33 页不会重跑。旧文案只是没说。

**验证**：`tests/unit/test_r103_interruption.py`（6 条）+ `test_build_script.py`
新增 3 条静态不变式；变异 **5/5 CAUGHT**（`devlogs/_verify/r103_mutate.py`，
含"复原事故代码"的忠实变异）；`build.ps1` 语法解析 0 error / 2568 tokens。
影响面 **191 passed**。

**产物**：`dist-electron\win-unpacked`（`run_id 20261026-122335-30864`，rc=0），
`PROVENANCE.git_head: e1d64b3`，built_at `2026-10-26 12:29:00`。
构建日志里 `[OK] /health OK (startup ~2s, port 58799)` —— 冒烟走专用端口，
全程没有 `Stop-Process`（即"构建不再杀任务"本身被实测证明）。

### 垃圾桶类型按文案归一（Round 90 第四十五批，2026-10-23）—— R102 首批：CR-29

> R102 计划是 CR-29 + CR-14，**拆两片**：CR-29 改「一条 finding 叫什么」，
> CR-14 改「几条算一条」。混在一批，"类型变少"与"条目变少"无法归因。

**`signature_time_anomaly` 被 LLM 当成"和签名有关的一切"。**

库里 14 条实测（10 页 4 + 51 页 10；文档记的 28 条是 CR-28 落地**前**的口径，
被抑制的不进库）—— 同一类型下混着 6 种缺陷：

| 实际缺陷 | 改前类型 | 改后类型 |
|---|---|---|
| 检查人姓名不一致（王小雨 vs 王涛雨） | `signature_time_anomaly` | `signature_mismatch` |
| 操作人与复核人同一人且时间相同 | 同上 | `self_review` |
| 操作者签名时间缺失 / 全部签名无日期 | 同上 | `completeness` |
| 未来日期 / 相差 10 年 / 超过 10 年 | 同上 | `suspicious_date` |
| 记录发放日早于文件起草/批准日 | 同上 | `doc_version` |
| 两处签名日期的先后 | 同上 | **保持**（交给 CR-28 判） |

改：`core/finding_quality` 新增 `_TRASH_TYPES` + `_TRASH_RECLASS_SIGNALS`
+ `_reclassify_trash_type`，接进 `normalize_finding`（顺序：垃圾桶重分类 → 过宽纠正
→ 白名单归一）。只动 `signature_time_anomaly`。

**判据上移**：`is_signature_order_claim` 从 `core/rules/llm_finding_guard.py` 移到
`core/finding_quality.py`（叶子模块）—— CR-29 也要用它，而 `llm_checks.py:513`
已用函数内导入绕过同一个环，说明环是真的。guard 保留别名，调用点不变。

**先后断言优先**：p18「签名日期（2025.04.21）晚于复核者签名日期（**无日期**）」
若先匹配缺失语会被改成 `completeness`，而 `completeness` 不在 CR-28 白名单里
⇒ 本该抑制的条目变成新噪声。

**验证**：新增 `tests/unit/test_r102_trash_type.py` **17 条**（含 6 组反向控制）；
`devlogs/_verify/r102_mutate.py` **6/6 CAUGHT**；影响面 **390 passed**；
`test_pipeline.py` **103 passed**；重放 14 条 → 8 条改到具体类型。

⚠️ **验收判据「该类型可见数 = 0」不成立，本批不凑这个数**：重放后仍有 **3 条**
可见（p2 审核晚于生产日期 / p32 签名晚于检验日期 / p42 复核签名晚于操作日期）。
它们是"签署晚于**业务日期**"，而 CR-28 自己写明了反向控制「另一侧是生产日期/
清洗日/设备校验有效期时**不**计入（那才是真缺陷）」⇒ 这 3 条正是 CR-28 有意保留的。
"只剩先后类 ⇒ 全被抑制"这一步推不出来。

**用户已裁定（2026-10-23）：保持现状** —— 签署/审核晚于其对应**业务日期**继续
作为可疑项报出（事后补签是 GMP 关注点）。验收口径改为「可见数 ≤ 3 且全为
业务日期型」，原文的 "= 0" 作废。

### 文档级事实收敛（Round 90 第四十四批，2026-10-23）—— R101 首批：CR-10 / CR-11

> 本批只做 R101 里的「文档级事实」这一片（CR-10、CR-11）。CR-3 / CR-9 / CR-24 /
> CR-25 / CR-33 留在 R101b —— 它们要么需要改抽取（prompt 契约），要么需要先定
> 「页型」这一层，不能与文案收敛混在一批（理由见 `CROSS_REVIEW_TODO.md`）。

**这两条治的不是"噪声多"，而是同一件文档级事实被逐页复述，且复述时把系统时钟写进了结论。**

**① CR-10：生产日期字段缺失是模板级事实，被当成逐页缺陷说了两遍**

10 页语料 p5、p10 各一条「批生产记录缺少生产日期信息。」—— 缺的是**页眉字段本身**
（模板里就没有这一栏），不是"这两页漏填"。51 页 p10 还有第三种写法
「页面信息缺失：版本号、批号、生产日期。」。

改：`core/finding_noise.reduce_document_level_facts` 把 `completeness` 里"生产日期缺失"
类条目收敛为**一条**文档级陈述（「全份记录的记录页页眉/页面信息栏无生产日期字段…
日期只出现在签名栏，属模板级事实」），逐页清单进 `page_list`（信息不丢）。
门槛 2 条 —— 单条本身已经是文档级口吻，不必强聚合。

**② CR-11：`suspicious_date` 拿"今天"当尺子，结论随运行日期漂移**

51 页语料实测：「生产日期（2015.01.20）**距今（2026-09-24）**已超过10年」。
2026-09-24 是**跑批那天的系统时间**，不是记录里的任何事实；而同一份文档里 2015 是
2025 的 **OCR 误读**，该说的是"年份读数不可信"，不是"距今十年"。

改：以「距今 / 当前年份 / 当前日期」为基准的 `suspicious_date` 收敛为一条
「本文档的日期年份出现 N 种读法（…）⇒ 日期类判定不可信」；
**系统时钟年份一律从结论里剔除**（`current_year` 显式注入，便于测试与复现）。
门槛**刻意设为 1** —— 哪怕只有一条也必须重写，因为它含系统时钟。

> ⚠️ 两条闸门是变异钉住的：① `aggregated` 条目不得二次聚合（否则年份清单被洗掉，
> M3）；② 该步骤必须**先于** completeness 降噪 —— 否则那两条先被通用结构性缺失桶
> 吞掉，"模板级"语义丢失（M6）。

**真实数据重放**（同一份库内 findings，只跑新增这一步；脚本
`devlogs/_verify/r101_replay.py`）：

| 语料 | 旧 | 新 | 明细 |
|---|---|---|---|
| 10 页 | 47 条 | **46** | 「缺少生产日期」2 → **1**（p5+p10 合并为模板级陈述） |
| 51 页 | 206 条 | **203** | 年份类 4 → **1**；生产日期 1 条**原样保留**（门槛 2，不新增） |

⚠️ 审计文档记的「51 页 12 条」是**修复前口径**；库里现存 4 条，其余 8 条已被更早的
守卫（CR-28/CR-29 那一批）抑制 —— 所以这里量到的是 4 → 1，不是 12 → 1。

**验证**：新增 `tests/unit/test_r101_document_facts.py` 15 条；
`devlogs/_verify/r101_mutate.py` **6/6 CAUGHT**（`preflight()` 校验 5 处预期实现，
SIGTERM 还原兜底）；`test_finding_noise.py` + `test_finding_aggregate.py` **61 passed**；
`tests/unit/test_pipeline.py` **103 passed**；全量回归 **4534 passed / 2 skipped /
0 failed**（分片 6 片全 `EXIT=0`，较 R100 的 4519 恰 +15 = 本批新增文件）。

### 参数类：规格解析、值槽绑定、越界可信度（Round 90 第四十三批，2026-10-23）

> 背景：交叉复核逐页核图后确认，参数/规格类告警里混着三种「系统已经知道该断言
> 不成立、却照样发出去」的条目（`docs/CROSS_REVIEW_2026-10-22.md` §3.2 B2/B6，
> 清单 CR-12 / CR-13 / CR-23）。三者的共同点仍然是**判据没错、输入不成立** ——
> 规格栏写的不是限值、值槽里不是数值、或者两边量纲根本不可比。

**① CR-12：把「不可判」与「超差」分开，并让类型由根因派生**

`_judge_param` / `_judge_cell` 原本是两份各自长出来的实现，`_parse_spec` 失败时
**默认**落到 `param_out_of_spec`。合并成**唯一**实现 `_judge_value`，按三级可判链
判定（**顺序是承重的**）：

1. **规格级** —— `_parse_spec` 失败时问 `spec_shape_reason`：规格栏**恰好一个**
   数字、且无任何比较符（`50L`）⇒ 那是**目标值**，不是允许范围 ⇒
   `spec_unverifiable`（`spec_not_a_limit`）。有比较符、或像
   `收集前 0.1~0.2，收集后 1.0~1.5` 这样的分段规格 ⇒ 不可判，**保留 LLM 兜底**
   （fail-closed，不替模型下结论）。
2. **形状级** —— `value_shape_reason`：记录值是时刻字面量（`18 时 30 分`）⇒
   **列串位** ⇒ `spec_unverifiable`（`time_literal`）。
   ⚠️ **必须放在规格级之后**：`接收开始时间` / `搅拌结束时间` 这类参数**本来就是**
   时刻值、规格栏为空，先判形状会把 40 余条正常时间参数全标成「列串位」
   （实测 `spec_unverifiable` 从 16 涨到 54；M10 钉住这个顺序）。
3. **越界可信度** —— `bound_plausibility_reason`：这个「越界」本身不可信 ⇒
   `spec_unverifiable`。四种形态：
   `spec_zero_bound`（越过的界恰为 0，如 p28「其他单个杂质≤0」，同页同类项均
   `≤0.5%` ⇒ 实为截断；**只对单侧限值成立**，`0~5°C` 的下界 0 是正常端）、
   `limit_literal_beyond`（实测值是检测限写法 `<99ppm` 且已越界 —— 无法证明合规，
   但同样不能断言超差）、`ratio_implausible`（无小数点的整数 + 与界限差 ≥5 倍 +
   不是十进制位移 ⇒ p39 的 `800~1000L` 记 `7800L`、`400~500L` 记 `7400L`，
   两处都**多一个前导 7**）、`decimal_loss`（十进制位移后落回规格内，p8 的 4.6 读成 46）。

**类型由根因派生**：新增 `expected_type(cause)`，`make_oos_finding` 在构造时校验
`type_` 与 cause 一致，不一致直接 `ValueError`。CR-12④（文案里写着「疑似 OCR」
的条目**不得**挂 `param_out_of_spec`）由此从"文档约定"变成**结构不变式** ——
这条校验上线时当场抓出 4 条既有用例，它们断言的正是 CR-12 禁止的自相矛盾形态。

**② CR-13：规则层已判「规格无法核定」时，LLM 的同一断言不得独立成立**

`drop_unfounded_spec_findings` 原本只挂在 stage2 ⇒ **`llm_cross`（跨页层）完全
绕过它**，这就是 p14 `1560`（`37.6°C 超出 40±3°C`，而 37.6 ∈ [37,43]）与
p24 `1424` 一直活着的原因。三处修正：

- `_triple_state` 改为复用**同一组**三个判据（不再各自维护一套），状态收敛为
  `in` / `unverifiable` / `out` / `unknown`（`soft` 退休）；`_sign_convention_uncertain`
  由 `unknown` 改为 `unverifiable` —— 只有在规则层**真的会说话**（另发一条
  `spec_unverifiable`）之后，这个 fail-closed 才是安全的。
- `_DROP_SAFE = {"in", "unverifiable"}`：规则层已判不可核定时，LLM 的超差断言
  没有支撑。
- **来源闸门**：`source == "rule"` 的条目一律不参与判定（stage3 是整表送进来的，
  没有这道闸门会把规则层的真超差也删掉）。
- `stage3` 接线：按页分组调用，**按原序**过滤（不按页重组 —— 顺序是下游聚合与
  展示的输入），抑制明细并入台账（抑制不得变成无痕删除）。

**③ CR-23：证据行要说清「拿什么在比什么」**

`ocr_text` 此前只拼 `值 / 规格`，**没有单位** ⇒ 复核员无法判断差异是量纲还是
数值。新增 `_with_unit`（幂等：`-0.094 MPa` + `MPa` 不会变成 `MPaMPa`），
前端证据行由 `OCR：` 改成 `证据：` 并**去掉 100 字截断**（截断会正好切掉单位），
改为 `title` 悬浮显示全文。

**④ CR-35（本轮发现）：两种带括号的真实规格写法，此前从未被判过**

`(18-26)°C`（温度）与 `(方位) ≥5Pa`（压差，括号内是方位说明）两种写法
`_parse_spec` 都返回 `None` ⇒ 这些格**一条判定都没有**（10 页语料 8 格）。
在 `_parse_spec` 末尾追加两条**只在该函数原本返回 `None` 的位置**才生效的分支，
既有行为逐字节不变：括号只包区间本体 ⇒ 递归解析括号内；括号内是方位说明 ⇒
解析括号后的限值。

**真实数据重放**（同一份 `page_cache.structured_json`，只换实现；脚本
`devlogs/_verify/r100_replay.py`，含落库前的 `apply_review`）：

| 语料 | 指标 | 旧 | 新 |
|---|---|---|---|
| 51 页 | 规则层 `param_out_of_spec` | 10 | **3**（p9 `417`、p19 `49.0%`、p26 `45.6°C` —— 真超差） |
| 51 页 | 规则层 `spec_unverifiable` | 5 | **16** |
| 51 页 | LLM 页级 `param_out_of_spec` | 11 条全发 | **保留 4 / 抑制 7** |
| 51 页 | LLM 跨页层 `param_out_of_spec` | 2 条全发 | **保留 1 / 抑制 1** |
| 10 页 | 规则层 `param_out_of_spec` | 3（全 `llm_cross`） | **0** |
| 10 页 | LLM 跨页层 `param_out_of_spec` | 3 条全发 | **保留 1 / 抑制 2** |

LLM 侧抑制的 7 条：p8 `46`、p14 `37.6°C`、p24 `-0.094 MPa`、p31 `<99ppm`、
p38 `18 时 30 分`、p39 `7800L`、p39 `7400L`。保留的 4 条是真超差
（p9 `417`、p19 `49.0%`、p26 `45.6°C`、p40 `水洗量 3.1`）。

CR-35 的直接收益在 10 页语料上可见：p8 的 LLM 断言「`21.8` 超出 `(18-26)°C`」
此前**规则层无从复核**（规格解析不出来），现在被同一解析器判为合规而抑制。

**CR-23**：规则层参数类 finding 缺 `ocr_text` = **0 条**（两份语料）。


**验证**：`devlogs/_verify/r100_mutate.py` **18/18 CAUGHT**（逐字节还原）；
新增 `tests/unit/test_r100_param_verdict.py` 63 条 + `test_spec_guard.py` 4 条；
受影响 10 个测试文件 **570 passed**；`tests/unit/test_pipeline.py` 103 passed；
**全量 4519 passed / 2 skipped / 0 failed**（分片 6 片，`EXIT=0` ×6，
台账 `devlogs/_verify/_r100_chunks.tsv`）。

> ⚠️ **口径变更，不是回归**：`spec_guard` 的 `soft` 状态取消、真空表压的
> `unknown` 改为 `unverifiable`，两条既有用例随之改名并补了 docstring 说明
> 「为什么现在可以 fail-closed」；`test_finding_aggregate.py` 里 4 条固定字符串
> 也一并更新 —— 它们原先断言的正是「文案说疑似 OCR、类型却断言超差」。

### 时序锚点 / 跨零点 / 准备类工序（Round 90 第四十二批，2026-10-23）

> 背景：交叉复核逐页核图后确认三类**结构性误报**（`docs/CROSS_REVIEW_2026-10-22.md`
> §3.2 B4，清单 CR-5 / CR-6 / CR-26）。三条的共同点是：判据本身不错，但**输入
> 不成立** —— 没有日期锚点却在比时刻、跨了零点却被当成倒序、把工序号顺序当成
> 时间顺序。

**① CR-5：`production_date` 为空时，时序检查不是"少报"而是"静默全灭 + 说假话"**

10 页语料 `production_date` **10/10 页全空** ⇒ `_parse_time("12:00", "")` 返回
`None` ⇒ R1-a/R1-b 的时序比较**一条都进不去**；同时把格式完全正常的
`12:00`/`14:00` 报成「**时间格式无法解析**」（`#1602`–`#1605`）。复核员会去查
OCR，而那里什么都没有问题 —— 这是**说谎**，比不报更糟。

改法：新增 `parsing.page_anchor_date(page)` —— 优先 `production_date`，缺失时
回落到该页**签名日期众数**（平票取先出现者，可复现）。同时用
`_clock_needs_anchor` 把两种失败分开：字面量里**确实有可读时刻**却解析不出 ⇒
缺的是"这一天是哪天"，文案如实写「**未找到可用于定位日期的生产日期/签名日期，
无法核对时间顺序**」；字面量真的是坏的（`无效时间`）才写「格式无法解析」。

> ⚠️ `_looks_like_clock` 不能写成 `"时" in raw` —— `无效时间` 也含"时"，
> 会把"格式坏了"说成"缺锚点"，正好是本条要消除的那种谎（R99 自测发现）。

**② CR-6：跨零点被当成倒序**

`#1482`（p24 `14时12分 → 08时18分`）、`#1511`（p11 `23:04 → 00:04`）、
`#1510`（p33 `20:21 → 00:17`）全是**跨日生产**的正常写法；`#1426` 是 LLM 对同一
现象的复述。缺日期时，"同一天倒序"与"跨零点"在结构化数据上**无法区分**。

改法：`parsing.crosses_midnight(prev_raw, curr_raw, prev_dt, curr_dt)` ——
两侧都**不带日期**、时刻往回、且**隐含时长 ≤ 20 小时**时才按跨零点处理。
20 小时是实测标定：p24 的 18.1h（浓缩过夜，真跨零点）要放过，而同日参数矩阵
`09:10 → 08:50`（隐含 23.7h，实为行序错误）必须仍报。

适用范围**刻意收窄**（两处都是变异验证钉住的）：
- 生效于**同一工序内部**（起止、测量行序列）；**跨工序边界不生效** ——
  "工序3 14:30 早于工序2 结束 16:00"要绕 22.5 小时，跨零点解释不成立，
  故保留 B1-9 的检测强度。
- LLM 侧新增 `llm_finding_guard._is_cross_midnight_clock_claim`：文案里
  **恰好两个裸时刻**、无日期/日号、无两个不同页码、且有先后关系词 ⇒ 该断言
  不可证伪，抑制并记账（`user_rule` 一并覆盖，同 CR-28 的教训）。

**③ CR-26：`step_no` 递增 ≠ 时刻必须递增**

`#1483`（p10 工序3「25%氯化钠溶液**配制**」10:15 vs p9 工序2 的 11:04）——
溶液是提前配好的。跨页时序比较里，本步属"准备类工序"（配制/配置/清洗/清洁/
领用）时不报；**年份差 >2 的提取错误仍照报**（豁免不覆盖它）。

**真实数据重放**（同一份 `page_cache.structured_json`，只换实现；脚本
`devlogs/_verify/r99_replay.py`，含落库前的 `apply_review`）：

| 语料 | 旧 | 新 | 明细 |
|---|---|---|---|
| 10 页 | 35 条（time_reversal/completeness） | **31** | 消失 4（`#1602`–`#1605` 的"格式无法解析"），新增 0 |
| 51 页 | 234 条 | **226** | 消失 17，新增 9（8 条文案改为"无法核对" + p33 一条补"签名日期"） |

`time_reversal` 在 51 页由 **12 → 7**，消失的正是 `#1482` / `#1426` / `#1511` /
`#1510` / `#1483` / `#1512`；`#1484`（p33 vs p25，CR-26 的保留项）**仍在**。

**两个由变异测试逼出来的实现缺口**（都已修）：
- `crosses_midnight` 初版的 `(curr−prev)+24 ≤ 20` 在**差值超过 24 小时**时变成
  负数 ⇒ 恒真 ⇒ 任何跨页倒退都被当成跨零点（M11 未命中暴露）；
- 跨页锚点不同（p22 工序3 洗脱 / p38 工序3 陶瓷膜清洗）时，两行本就不属同一天，
  逐时比较不成立 ⇒ 新增锚点闸门，`#1512` 因此消失（M11 补测钉住）。

**验证**：`devlogs/_verify/r99_mutate.py` **13/13 CAUGHT**（逐字节还原）；
新增 `tests/unit/test_r99_time_anchor.py` 44 条；受影响 4 个测试文件 **371 passed**；
**全量回归 4452 passed / 2 skipped / 0 failed**（3561.38 s，EXIT=0，
`devlogs/_verify/_r99_full2.log`）。

> ⚠️ 一条**既有**用例随之改写：`test_r95_manual_labels.py::
> TestSignatureOrderIsNotADefect::test_time_reversal_still_survives` 原来用
> 「开始 14:12 晚于结束 08:18」（两侧都无日期）当反例 —— 那正是 CR-6 判为
> **正常跨日生产**的形态。改用带日期的形态，用例意图（time_reversal 不得被
> 签名先后规则吞掉）不变。首轮全量因此红 1 条，改完复跑才绿。

### 低置信签名不得驱动 critical + 签名先后抑制的两处实现缺口（Round 90 第四十一批，2026-10-23）

> 背景：交叉复核逐页核图后确认，10 页语料的 `p4` 与 51 页语料的 `p26/p40/p46`
> 四条 `self_review critical` **全部是手写签名被 OCR 读成同一个人**（p4 执行人
> 王小雨 / 检查人是另一位草书签名；p26 页面上根本没有「周雅」）。判读见
> `docs/CROSS_REVIEW_2026-10-22.md` §2.1/§2.3，清单见 `CROSS_REVIEW_TODO.md`（CR-4）。

**① CR-4：低置信证据只能出 info，且不得断言"同一人"**

`core/rules/rule_gmp.py::_check_self_review` 早就算出了 `slot["low"]`，却**只用它
追加一句免责注**、`severity` 硬编码 `critical` —— 注被正文淹没，复核员看到的就是
一条"独立复核失效"的严重缺陷。改为：参与比对的两侧签名**都不**低置信才判
`self_review critical`；任一侧低置信 ⇒ 出 `signature_unreadable`（info），文案
**并列印出两侧的原始识别串**（只引用、不断言），`operator` 留空。

新类型按"三处单一真值 + 依据 + 检索"全部登记（漏一处就会静默落到 `uncategorized`）：
`finding_quality.CANONICAL_TYPES`、`zh_map.FINDING_TYPE_ZH`、`templates/review.html`、
`static/findings-map.js`（双端逐键一致）、`gmp_basis.GMP_BASIS_MAP`、`kb.retriever.TYPE_QUERIES`。

**真实数据重放**（同一份 `page_cache.structured_json`，只换实现）：

| 页 | 旧 | 新 |
|---|---|---|
| p4（10 页语料） | `self_review critical`「同一人『王小雨』」 | `signature_unreadable info`，`operator=""`，印出两侧原串 |
| p26 / p40 / p46（51 页语料） | `self_review critical` ×3（`1529/1530/1531`） | `signature_unreadable info` ×3，`operator=""` |

**② CR-28：抑制条件的两处缺口，实际生效数只有文档的一半**

原实现要求"先后词左右各 24 字**都**含签名语"，且类型白名单只有
`signature_time_anomaly`。两处都不成立：

- **规则层永远匹配不到**：R9a 的文案是「reviewer 杨月萍 签名时间(2025.06.04) 早于
  operator 韩思萍(2025.06.21),复核/审批顺序异常」—— 只有**左侧**带"签名"，右侧是
  裸的角色+姓名+时间。改成**非对称判定**：一侧有签名语、另一侧是签署主体（角色词）
  即计入。反向控制：另一侧是「生产日期 / 清洗日 / 设备校验有效期」时**不**计入。
- **白名单漏了 `user_rule` 与 `signature_mismatch`**：用户规则的判定同样是 LLM 产出，
  `type` 直接沿用模型给的 `user_rule`（`source` 由 type 反推）。实测 `1619/1620`
  就是 `user_rule critical`；51 页 `#1562`（p18）与 `#1413` **文案逐字相同**，只有
  type 不同（`signature_mismatch`）⇒ 不纳入就"同一条结论漏一条、留一条"。

⇒ **按真实落库数据重放整条守卫链**（不是单测谓词）：修正后 10 页抑制 **4** 条
（`1594/1598/1619/1620`）、51 页 **2** 条（`1413/1562`），与本文档原记一致；
**修正前实际只有 2 条 / 1 条** —— 文档里的数字此前从未由流水线达成。

⚠️ **"6 条 vs 4 条"的疑问到此关闭**：`1614/1615`（p1/p2）确实命中了谓词，但它们在
更早的 `_check_same_day_contradiction`（L3）就被收掉（文案自己写着"三者日期相同"）
⇒ 不计入 CR-28。上一轮审计脚本**直接调谓词**、绕过了这道前置判据，故多算 2 条。

**验证**

- `tests/unit/test_r95_manual_labels.py`：`TestSelfReviewAggregation` 由 5 条扩到 9 条
  （低置信降级 / 印双原串 / `operator` 留空 / 单侧低即降级 / 高置信仍 critical）。
- `tests/unit/test_llm_finding_guard.py`：新增 `TestCR28ImplementationGaps`（7 条，
  含 4 条反向控制：签名缺失 / 签名 vs 清洗日 / 发放日 vs 起草日 / 真姓名不符）。
- 变异：`devlogs/_verify/r98_mutate.py` **6/6 CAUGHT**；回归 `r96_mutate.py` **9/9 CAUGHT**
  （其 M4–M9 锚点已随实现更新 —— 旧锚点会 `count()=0` 而静默 SKIP，让证据变空）。
- 全量回归：**4408 passed, 2 skipped, 0 failed**（1314.99 s，EXIT=0，
  `devlogs/_verify/_r98_full.log`）。相对今日基线 4396 为 **+12** = 新增用例 11 条
  （`TestSelfReviewAggregation` 净 +4、`TestCR28ImplementationGaps` +7）
  + `test_finding_quality` 按 `CANONICAL_TYPES` 参数化的 1 条（新类型自带）。
- 受影响面 328 passed；打包/前端一致性 95 passed / 2 skipped。
- 打包 `build.ps1` rc=0（台账 `20261023-122952-10028`，`git_head=32f9c57`，4m57s）；
  两份产物内嵌副本均含 `signature_unreadable`。
- 发布门禁 **pass=11 fail=0 warn=0 skip=0**，coverage 95.14%（`gate_report_20261023_132658.json`）。
  ⚠️ **首次跑是 10/11**：`test_settings_js::TestProviderEscaping::test_provider_fields_are_escaped`
  报 `PermissionError: [WinError 5]` 写宿主临时目录（`probe.js`），**不是断言失败**；
  单独复跑通过、重跑门禁 11/11 通过 ⇒ 记为**环境抖动**，不记为产品缺陷，也不据此
  放宽 `ENV_ONLY_FAILURE_PREFIXES`。

### 护栏理由不再写进用户可见文案（Round 90 第四十批，2026-10-22）

> 背景：交叉复核发现 `apply_review` 把**降级理由**以 `{正文}｜{理由}` 追加到
> `description`，复核页正文里出现「LLM 独断 critical，未经规则层确定性判据复核」
> 「v4 prompt 已禁止此类表述」这类**内部语**。最恶性形态是 p22 的 `1563`：
> 业务正文本身为空，追加后**整条只剩理由** —— 用户看到的就是一句后端日志。
> 判读与清单见 `docs/CROSS_REVIEW_2026-10-22.md` / `CROSS_REVIEW_TODO.md`（CR-1）。

**四层改动（缺一层就是"看起来修好了"）**

1. **纯函数**：`apply_review` 把理由写进新键 `guard_reason`，**不再动 `description`**。
2. **schema**：`findings` / `findings_archive` 各加 `guard_reason TEXT`；
   `SCHEMA_VERSION 14 → 15` + `_migrate_v15`（表不存在则跳过，不靠异常兜底）。
3. **写路径**：stage2（`llm_page`）、stage3（跨页）、归档 INSERT 全部带上该列
   —— 本仓踩过"加了列没人写"的静默失效（`raw_type`）。
4. **空正文**：新增 `core/finding_quality.has_business_text`，两个写入口共用；
   空文案的 finding **不写行**，但**计数进日志**（`empty-description dropped`），
   不做静默丢弃。

**历史数据**：库里已有 56 条被污染的行（55 条可拆 + 1 条 `1563` 整条只剩理由）。
**没有放进迁移** —— "正文到哪里结束"是业务判断，迁移里猜分隔符等于静默改写业务记录
（与 `PROJECT_PITFALLS` 六十八① 同一条纪律）。改为 `scripts/split_guard_reason.py`：

- 默认 **dry-run**，只报告；`--apply` 才写，且**写前备份**；
- 右侧必须命中护栏理由的**专属措辞**才拆（业务文案里自带的 `｜` **不动**）；
- 正文为空的行**不自动处理**（拆完会变空正文，得人来定）。

副本库实测：可拆 55 / 正文为空 1；`--apply` 后 `description` 只剩业务语、
`guard_reason` 有值、重复执行幂等。

**验证**

- 新增 `tests/unit/test_migration_v15.py`（7）、`test_split_guard_reason_script.py`（13）、
  `test_r97_empty_description.py`（11）；`test_llm_finding_guard.py` 里
  "降级理由必须出现在 description" 改为"必须落在 guard_reason 且 description 不变"。
- 变异 `devlogs/_verify/r97_mutate.py`：**10 个变异点 10/10 CAUGHT**（含"退回拼进
  description""stage2/stage3 丢列""迁移没接上""去掉专属措辞闸门""判定恒真""关掉闸门"）。
- 相关子集：API review / 抑制台账 / 归档 / 单一实现 / 裸写棘轮 共 152 条全绿。
- 裸写棘轮基线新增 3 行（`_migrate_v15` 的 `commit` / `ALTER TABLE` / `PRAGMA`），
  与 `_migrate_v1..v14` 同类：迁移发生在 `get_db()` 返回之前，无并发写者。
- 全量回归：**4396 passed, 2 skipped, 0 failed, 0 error**（1853.98 s，EXIT=0，
  `devlogs/_verify/_r97_full3.log`）。比 R96 的 4364 多 **+32**，恰为新增用例数
  （v15 迁移 7 + 拆分脚本 13 + 空正文 11 + guard 改写 +1）。
  ⚠️ 前两轮作废：第一轮**沙箱拦 `socketpair()`**（`WinError 10013`）⇒ 639 errors
  全在 setup；第二轮跑到 94% 被中断（但已暴露 `test_pipeline` 那条旧契约，已修）。
  **全量回归必须关沙箱**。

**未做（明确留给后续批次）**：CR-1 初稿的 ④「前端折叠区渲染系统判定说明」——
本批目标是"别把内部语当业务文案"，再加一块展示内部语的 UI 属设计判断，
且前端在 R103 才动。`guard_reason` 已在 `/findings` 响应里返回，R103 可直接用。

### 两份真实批记录 100% 逐页核完 → 停止"签了说没签"与"签名先后算缺陷"（Round 89 第三十九批，2026-10-22）

> 背景：用户要求**直接逐页看**仓库内两份 PDF（`试用批记录.pdf` 10 页、
> `丝裂霉素提取批记录.pdf` 51 页），与 BatchSentry 的产出逐条交叉比对。
> 完整判读见 `docs/CROSS_REVIEW_2026-10-22.md`，可执行清单 `docs/CROSS_REVIEW_TODO.md`。
>
> **结论先说**：两份记录 253 条 findings **全部核完**（10 页 47 条 + 51 页 206 条），
> **不成立 + 不应报 ≈ 77%**（剩余 25 页单段 61.5%）。这是**误报基线，不是准确率** ——
> 仓库里仍无真实标注集。本批只落地其中两条根因最明确、影响面最大的。

**CR-2 —— 「签了说没签」：签名角色词表只有 5 个词，实测语料有 22 种**

- 定位：`core/rules/rule_doc.py` 三张角色表只认 `operator/操作人/操作员`、
  `reviewer/复核人/复核员/workshop_reviewer`、`qa/qa_reviewer/质量保证/qa审核/批准人/放行人`。
- 实测语料里 `qa_approver`（p1 赵阿巧、p6 孙文莉）、`qc_approval`、`qc_approver`、
  `approval`、`review`（19 次）、`checker`（6 次）、`inspector`、`acceptor`、`检查人`
  **全部不认** ⇒ 签名明明在，却判"缺少 QA/复核人签名"。
- 修法：三张表扩到 22 种。**故意不收** `负责人`/`supervisor`/`主管` —— 那是「工序负责人
  检查」签批位，不是 QA 放行；收进来会吞掉"模板无 QA 栏"这一真缺陷（由 CR-3 单独处理）。
- 净效果（同进程双跑、只换词表）：10 页规则层 49 → 47，消失的 2 条正是 p1/p6 的
  「未识别到 QA 签名」；51 页 279 → 279（该份语料用词都在旧表内，**如实记录无变化**）。

**CR-28 —— 「签名先后算缺陷」：用户裁定 p7/p8 为正确行为**

- 用户原话：「复核人时间早于检查人，这也是正确的行为」。同一表单不同栏目本来就
  不同步填写，复核员会按这条去追一个不存在的偏差。
- 修法：`core/rules/llm_finding_guard.py` 新增 `_is_signature_order_claim()` ——
  先后关系词左右各 24 字**都**含签名语（签名/签字/签署/签批/签认）才抑制；
  位置在来源过滤**之前**（`user_rule` 一并覆盖，实测 1620 就是 `user_rule critical`）。
- **两版更"宽松/更保守"的实现都被实测否掉**：
  - 按 `type=signature_time_anomaly` 整体抑制 ⇒ 回放 28 条里混着 6 类**真缺陷**
    （姓名不一致 / 签名缺失 / 记录发放日早于文件起草日 / 年份写错 / 自我复核 / 重复）
    ⇒ 静默漏识别，撤销。
  - 「跨年的一对不抑制」（想保住 2015/2025 的年份误读线索）⇒ **原页复核**：p8 的复核人
    日期原页是 `2025.06.30`、p27 的包装/取样都是 `2025`，两页**都没有 2015** ⇒
    跨年纯属 OCR 误读，撤销。
- 净效果：10 页抑制 4 条（p7 ×2、p8 ×2，**含 1 条 critical**），51 页抑制 2 条（p18 ×2）；
  边界项（发放日 vs 起草日 / 十年差 / 签名缺失 / 姓名不一致 / 自我复核）**全部保留**。

**核完剩余 25 页后新增的待办（CR-29 ~ CR-34，未在本批实现）**

- `B9` 跨页列一致性没限「同一 `file_code`」⇒ 7 条同族误报（不同岗位文件的同号工序互比）。
- `B10` 版本号从文件编号 `R##` 的尾号误取（`1535`/`1536`）。
- `B11` 否定式检查项答「否」被当异常（p51 ×3，文案自己都写着"否通常即正常答案"）。
- 其余：类型归一、文档生效年 ≠ 事件年、表格列内竖写签名不被识别。

**验证**

- `tests/unit/test_r96_role_vocab.py` 新增 41 条（22 种 role 族归属 + 4 条"签了说没签"
  回归 + 故意排除项 + 空姓名仍须报）。
- `tests/unit/test_r95_manual_labels.py`：`TestSameDayIsNotReversal` →
  `TestSignatureOrderIsNotADefect`，并新增 `TestSignatureOrderDoesNotSwallowRealDefects`（6 条边界）。
- `tests/unit/test_llm_finding_guard.py::test_p27_...` 期望由"不抑制"改为"抑制"
  （原期望被用户裁定推翻，docstring 记录理由与替代载体）。
- 变异 `devlogs/_verify/r96_mutate.py`：**9 个变异点 9/9 CAUGHT**，逐字节 sha256 还原。
- 离线回放 `devlogs/_verify/r96_replay.py`（不调 LLM，落库当固定输入）：摘要
  `devlogs/_verify/_r96_replay_out.json`。
- 全量回归：**4364 passed, 2 skipped, 0 failed**（1877.83 s，`devlogs/_verify/_r96_full.log`）。

**顺带修掉的工具缺陷**：变异脚本被外部 SIGTERM 杀掉时 `finally` 不执行，被测文件**留在
变异态**，之后所有"变异 ⇒ 看红"都建立在错误基线上（本次实测连带红 2 条无关用例）。
已加 `signal.SIGTERM/SIGINT` 还原兜底 + 启动 preflight 校验预期实现。

### 手工标注 10 条 → 5 组修复 + 首次打开 35.9s 定位到秒（Round 88 第三十八批，2026-10-21）

> 用户拿 `dist-electron/win-unpacked` 对 `试用批记录.pdf` 逐页人工标注 10 处（真实 job
> `4cffb358-3b3`：10 页、`paddle`、47 条 findings）。**每条都先定位到具体 finding id /
> 结构化字段，再改代码** —— 详表见 `docs/CLOSEOUT_PLAN_2026-10-21.md` §13。
> 另：用户报"首次打开等了 30S"，本批把它拆成四段量到秒（§14）。

**A1 / A3 —— 同日不构成"先后倒置"（误报）**

- 定位：`1614` / `1615` 两条 `user_rule/critical`，文案一边断言"早于"、一边自述"三者日期相同"。
- 根因两处：① `llm_finding_guard._check_declared_order` 在 `a == b` 时**直接放过**
  （"方向无法判断"）；② 另一形态（关系词右边没写日期、相同性由"均为"给出）**无人处理**。
- 修法：① 同日改为**抑制**；② 新增 `_check_same_day_contradiction`，且**放在来源过滤之前**
  —— 否则 `user_rule` 到不了 LLM 复核链（A1/A3 正是 `user_rule`）。

**A2 / A6 —— 标题与内容不符（类型错配）**

- 定位：`1624` `param_out_of_spec/info` 说"第 1 页批号与第 2 页批号不一致"；
  `1617` 同类型说"湿度 35%RH%RH，单位重复"。**A6 尤其误导**：单元格
  `{spec: "24%RH-65%RH", actual: "35", in_spec: true}` ⇒ R3 **从未触发**，
  真缺陷只是"单位重复"，却被标成"参数超标"。
- 根因：`param_out_of_spec` 在 LLM 输出里长期充当**万能兜底类型**（系统提示词的枚举
  只有 4 个值）。
- 修法：① `core/finding_quality.py` 新增 `normalize_finding` / `type_from_description`
  —— 按**文案**把过宽类型纠正到具体类型（只动 `param_out_of_spec` / `user_rule`，
  不动 LLM 已经答对的类型）；② `llm_checks` 的跨页枚举由 **4 → 15 种**，并在提示词里
  明写"不得为省事复用 `param_out_of_spec`"；③ 补 `is_spec_verdict`（判定词 + **数值区间**
  同时出现才算真超差）—— 否则"30°C 超出用户规则 15-25°C"会被反向洗成 `completeness`。

**A4 / A9 —— 同一人 OCR 变体 + 一缺陷四份（噪声）**

- 定位：`1593`（`llm_page`）与 `1623`（`llm_cross`）都在说「王小雨 vs 王涛雨」；
  p3/p4/p8/p9 的同一缺陷在 `rule` / `llm_page` / `llm_cross` / `user_rule` 各出一份。
- 修法：① 名字变体（等长、仅 1 字之差）当"不同人"断言 ⇒ **降级**不抑制（可能是 OCR，
  也可能是真换人）；② `stage3` 跨源去重抽成纯函数 `_overlap_covered_by`，**两侧口径
  刻意不对称**：`rule` 只按**精确类型**覆盖（规则是权威版本，扩大覆盖面等于静默吞掉
  LLM 的独立观察），`llm_page` 才套"签名族"。实测抑制 10 条，**没有一条
  `signature_mismatch` 被规则层的 `signature_time_anomaly` 吞掉**。

**A5 —— 自核自检不聚合（用户读成"问题有点多"）**

- 定位：p4 工序 8/9/10 的 `operator == reviewer == 王小雨`（**真自核自检**），
  旧实现逐行出 3 条。
- 修法：`_check_self_review` 按人聚合为 1 条（`工序8、9、10`），签名置信度为 `low` 时
  追加"若原页为两人签署，属识别误读，请核对笔迹"。
  ⚠️ p3 那对（王小雨 / 沈雅晨）**不是**自核自检 —— 那是 A4 的姓名误读，不能混。

**A7 / A8 / A10 —— 签名槽位的"有/无"判错（归因错误 + 漏报）**

- 定位：① p6 执行人/检查人**有手写签名**，PaddleOCR-VL 整列漏识别 ⇒ 规则报"缺少签名"
  （`1592/1629/1633/1635`）—— 把 OCR 问题说成记录缺陷，复核员会去改一份合规记录；
  ② p8 的 `signatures` 里有 `{"role": "reviewer", "name": ""}`，旧代码**只看 role**
  就判"有复核人" ⇒ 真的空白栏**漏报**。
- 修法：① `_has_signed` 要求**姓名非空**；② 新增 `_blank_slots` —— 同类另有已签署人时，
  追加「有 N 处复核人签名栏为空（同页另有已签署的复核人，属逐行核对）」；③ 文案改为
  「未识别到…签名（如原页已签，属 OCR 漏识别）」；④ `finding_noise._KIND_LABEL` 的聚合
  摘要同步改口（明细说"未识别到"、摘要说"缺少"，等于把归因错误留在摘要里）。
  ⚠️ `ocr_text` 标记（`operator=空` 等）**一字未动** —— 降噪靠它匹配，改标记会**静默
  关掉聚合**。

**首次打开 35.9s —— 分段到秒，修掉 19.4s 那一段**

- 用**用户自己那次启动**的台账分段（`backend-boot.log`）：`[spawn] 00:07:21` →
  `[boot] 00:07:35`（**14.1s** 进程创建 + 加载器 + 杀软）→ `[PBC] Starting 00:07:37`
  （1.9s uvicorn）→ `Started server process 00:07:56`（**19.4s** `import main`）→
  `[ready] 00:07:56`（0.5s lifespan）= **35.9s**。同产物**缓存热 2.7s**
  （`devlogs/_verify/r95_startup_probe.py`）⇒ 不是逻辑缺陷，是**冷缓存下按文件计费**。
- 根因量化：`import openai` 独占 **1007 个模块**，`import main` 全链 **1340 个**
  —— 一个 SDK 占 **75%**，而全树只用到 `AsyncOpenAI` 一个符号。
- 修法：`llm/adapters/openai_adapter.py` 改为**函数内导入**（首次建客户端才付）。
  启动期导入事件 **1340 → 665（‑50%）**、累计 **2.91s → 1.13s（‑61%）**。
- ⚠️ **探针第一版测错了端口**：设 `APP_PORT=8099`，而 `server.py` **只读 `PORT`**
  ⇒ 两次"超时"是**测量错误**，不是产物起不来（已记 `PROJECT_PITFALLS` §六十七）。
- ⚠️ **未验**：**冻结产物**的冷启动没重测（冷缓存要重启或清 standby list，本沙箱做不到）
  ⇒ "35.9s → ?"**没有数字**；只有"启动期少碰 675 个模块文件"这个结构性事实。

**判据（本批实测）**

| 项 | 值 |
|---|---|
| 新增用例 | `tests/unit/test_r95_manual_labels.py` **37 条**（A1–A10 逐条映射 + 跨源去重 5 条 + 提示词枚举 + 无指称 `user_rule` 降级）、`tests/unit/test_startup_import_cost.py` **6 条** |
| 变异 | R95 五组修复 **7/7 CAUGHT**（`devlogs/_verify/r95_mutate_labels.py`）；启动预算 **4/4 CAUGHT**（`r95_mutate_startup_budget.py`）。每条还原后 **sha256 逐字节一致** |
| 变异暴露的真问题 | **M4 首轮 SILENT**：A1 那条被 `_check_declared_order` 与 `_check_same_day_contradiction` **双重覆盖** ⇒ 关掉任一条都不会变红。已补隔离用例（塞一个无关日期，打破后者的"全文唯一日期"前提）；顺带修了变异脚本把**类名**当用例名的解析缺陷 |
| 离线重放 | `devlogs/_verify/r95_replay_sy.py`（LLM 两分支打桩 + 注入 DB 真实 LLM 行）：**47 → 34 条可见**，`{info 19, warning 13, critical 2}`，自指降噪 17 / completeness 降噪 13 / 跨源去重抑制 10 / 同日抑制 2 |
| 旧断言的处理 | `test_cross_page_analyzer.py::test_user_rule_findings_marked_with_user_rule_source` **原先钉的是错行为**（`user_rules=[]` 时仍要求 `source='user_rule'`）⇒ 改为同时钉正/负两个分支（规则存在才保留 `user_rule`，无指称降级 + 按"判定词+区间"回填 `param_out_of_spec`） |
| 全量 | **4313 passed / 0 failed / 2 skipped**（516.87 s，`-o addopts=""` 绕开覆盖率门槛） |
| 文档机检 | `test_todo_freshness` / `test_attribution_single_source` / `test_doc_release_plan_sec6_claim` / `test_deployment_doc_claims` / `test_gate_inventory_doc` **37 passed** |

**边界（未验 / 不可宣称）**

- 离线重放**不是**产物级证据：LLM 打桩、注入历史行；流水线本身**非确定**（0-15：
  同一输入两次 41 vs 51）⇒ **单次计数不作回归判据**。
- 10 条标注里 **A7/A10 的"OCR 漏识别"已取证**（不是用户一面之词）：p6 的 OCR 原文里
  `执行人/日期：`、`检查人/日期：` **两格是空的**，而页图 `_sy_pages/p06_sign.png` 上两格
  都写着 `…2025.05.20`；p8 原文是 `转移人/日期：___ 2025.06.03`（**日期读到了、名字成了三个
  下划线**），页图 `p08_bottom.png` 上名字清晰可见。⇒ **日期/印刷体识别正常、连笔姓名系统性
  漏识别**。代码只做**归因修正**，没有、也无法在本批修 OCR 本身（登记 `0-63`）。
- 14.1s 的进程创建段**未修**（载荷 117 MB / ~1900 文件 + 实时防护，不是代码问题）⇒
  已登记 `0-62`（P3）。
- **新发现（未修）**：`0-64` —— 全 10 页 OCR 原文里**没有任何一页有"生产日期"字段**，
  所以"缺少生产日期"是**真缺口**；但它被 p5 / p10 各报一条（同一条文档级缺陷重复两次），
  而 `reduce_completeness_noise` 的聚合阈值是 `min_group=8` ⇒ 2 条打不到阈值，原样留在
  复核页。属 A9 同族（重复）但在**文档级**，不是页级。
- 本批**改入包源**（`core/`、`llm/`）⇒ **产物必须重建**，否则手工测试拿到的还是旧行为。

### 收尾阶段：0-53 / 0-54 审计可视化与归档读入口（Round 88 第三十七批，2026-10-21）

> 承第三十六批。本轮**改入包源**（`api/`、`static/`、`templates/`、`tests/`）⇒ 产物已重建。
> 两条同属一类缺陷：**后端早就写好了，界面上根本没有出口**。

**0-53（P2）—— `findings_archive` 只有写入点，没有读接口**

- 定位：`grep -rn findings_archive api/ core/ db/` 只出 INSERT 与 schema —— 归档只在
  `api/jobs/actions.py`（review→retry 全量重分析）与 `core/pipeline/stage3.py`
  （LLM 层重算）发生，**没有任何读端点**。GMP 要求"记录可追溯"，只写不读等于台账不可查。
- 修法：新增 `GET /api/jobs/{id}/archived_findings?limit=&offset=`，返回
  `{entries, count, total}`。`total` 与 `count` **刻意分开**：前者全量、后者本页，
  混成一个 ⇒ 前端「共 N 条」永远是本页数字。
- ⚠️ 路径名**不能**叫 `/findings_archive`：同文件先注册的
  `GET /jobs/{job_id}/findings/{finding_id}`（`finding_id: int`）会把它当路径参数解析
  ⇒ **422**（既不 404 也不落到新函数）。用例 `test_route_is_not_shadowed_by_findings_id`
  专门钉住这一点。

**0-54（P2）—— 两个审计端点活着，前端零调用**

- 定位：`GET /api/jobs/{id}/audit` 与 `/llm_audit` 都存在并返回 `{entries, count}`，
  而 `grep -rn audit static/ templates/` = **0 命中** ⇒ 端点全黑、无人调用。
- 修法：`static/review-audit.js` —— 复核页头部「审计记录」按钮打开三 tab 面板
  （变更历史 / 模型调用 / 已归档），空态各有一句**解释为什么会空**的文案。
- 三条纪律：① 只用 DOM API（`createElement` + `textContent`）渲染 —— 审计内容含
  OCR 原文与上游 `error`，拼 `innerHTML` 就是 XSS 面；② `action` **不翻译**
  （GMP 追溯按 code 对表，`detail` 本来就是中文；再建一张表就是第四份中文真值）；
  ③ 取不到数据时**显示状态码**，不静默成空表 —— 空表会被读成"没有变更"，是**错误的合规结论**。

**判据（本批实测）**

| 项 | 值 |
|---|---|
| 新增用例 | `tests/unit/test_review_audit_js.py` **15 条**（7 条接线 + 8 条行渲染）、`tests/integration/test_api_review_archive_read.py` **8 条** |
| 变异 | **6/6 CAUGHT**（`devlogs/_verify/r94_mutate_c9c10.py`；每条还原后 sha256 逐字节一致） |
| 浏览器 e2e | 新增 **B8**（5 条判据）：面板打开 / 变更历史有数据且 3 列 / 「已归档」空态文案可见 / 种一条归档行后表格 6 列且类型已中文化 / 关闭真的移除面板 |
| 全量 | **4270 passed / 0 failed / 2 skipped**（1474.35 s）。首跑是 4269 + 1 红：`test_doc_release_plan_sec6_claim` 按 token 计数，本文件注释里 1 处 `innerHTML` **文字提及**让 46 → 47 ⇒ 已同步文档并复跑该文件（9 passed） |
| CSS | `static/app.css` 20 016 → 20 046 B（新增 `align-top` / `border-collapse` 等，改了 `static/*.js` 必须重建） |

**交付前 e2e（本批实测，产物 = `0371511`）**

| 层 | 结果 |
|---|---|
| 真实浏览器 `tests/e2e_browser.py` | **19/19 OK，RC=0**（6 分 11 秒，含 B8 五条）—— `devlogs/_verify/browser_e2e/run_r94b.log` |
| 产物级 `tests/e2e_frozen.py` | **RC=1**：`covered=2 skipped=0 failed=2` —— 根因**上游欠费**（`HTTP 402 code=30001 account balance insufficient`；同一凭据的**免费**端点返 200 ⇒ 凭据有效、账户没钱）。红的正是 `pipeline_terminal` 与 `llm_audit_success`；其余 20 余条（OCR / 页图 / 报告 / CSS / JS / 状态点 / 设置模块）全 OK ⇒ **不是产品缺陷，但 LLM 链路本轮未验** |
| 产物新鲜度 | `bundle_manifest --check` **两份均 OK**；`dist/pbc-server`：`version=1.2.1 files=117 head=0371511 dirty=False`；`PROVENANCE.txt git_head=0371511` |
| 发布门禁 | **10/11**：唯一未过项 `tests_coverage`（4269 passed / 1 ERROR，coverage **95.17%**）。ERROR 在 `test_watchdog.py::test_fresh_job_not_reported` 的**夹具装配期**，junit 里是 `PermissionError: [WinError 10013]`（**沙箱禁止套接字绑定**），该用例单独跑 **3/3 passed** ⇒ 环境限制，不是回归 |

**边界（未验 / 不可宣称）**

- 「已归档」tab 的非空路径靠**夹具行**（`_seed_archived`）：浏览器层两个 job 都没走过
  `review → retry`，造不出真归档 ⇒ 验的是"表格按 6 列渲染且类型中文化"，
  **不是**"retry 真的写了归档"（后者由 `test_retry_archives_findings.py` 覆盖）。
- `llm_audit` tab 的**有数据**形态未在浏览器层出现（样本 OCR 成空页）；行渲染由 node 单测覆盖。
- 面板一次最多 200 条，**没有**"加载更多" UI —— 超出的历史要看库或直接打端点。

### 收尾阶段：0-42 / 0-41 / 0-55 / 0-51 / 0-50（Round 88 第三十六批，2026-10-21）

> 承第三十五批。本轮**改入包源**（`core/`、`api/`、`tests/`）⇒ **已重建产物**并重跑门禁。
> 目标：交付前的收尾 —— 能修的先修，修不了的把边界写清；**所有数字都来自本批实测**。

**0-42（P1）—— findings 描述编码 `time`/`step_no`，停止 UNIQUE 静默合并**

- 缺陷：去重键是 UNIQUE `idx_findings_dedup(job_id, source, page, type, description)`，
  而 `core/rules/llm_checks.py` 的三个产出分支用**不含时间点**的模板 ⇒ 同一格的多个时间点
  条目描述**逐字相同** ⇒ `INSERT OR IGNORE` **静默丢行**（实测 356/422，丢 66 = 15.75%）。
- 修法：描述里编码 `time`（有 `step_no` 时一并编码）⇒ 不同时间点不再撞键。
- 证据：2 条 `xfail(strict=True)` 由红转绿 + 2 条兼容性用例；变异 **4/4 CAUGHT**。
- ⚠️ **刻意不动索引**：放宽 UNIQUE 会让**存量库**在迁移后出现「同一条 finding 两份」，
  属数据迁移决策，本轮不碰。

**0-41（P2）—— 大记录不再整份降级：LLM fallback 分批判定**

- 缺陷：`_LLM_FALLBACK_BATCH_MAX=50` 的原始理由是「449 个参数塞进一个 prompt 会让上游重试、
  Stage 3 卡住数分钟」，但它**约束的是单次调用**；实现却变成「整份记录超过 50 个待判定参数
  ⇒ 一次 LLM 都不发，整体降级成一条『参数数量过多』」⇒ **51 页 / 400 参数的真实记录 0 覆盖**。
- 修法：按 50 切批**逐批判定**，保留每个参数的判定粒度；再加两道闸门
  `_LLM_FALLBACK_MAX_BATCHES=8`（批数）与 `_LLM_FALLBACK_BUDGET_S=600`（墙钟），
  超限的批**逐参数**降级为待复核 finding ⇒ **不重不漏**（每个入参恰好落在「已判定」
  或「已降级」之一）。
- 代价（写明）：真实最大样本从「0 次调用」变成 **8 次**，最坏 +约 10 分钟。
- 护栏 `tests/unit/test_llm_fallback_batching.py`（11 条）+ 变异 **4/4 CAUGHT**。
  ⚠️ 其中 M4 首轮 **MISSED**：所有用例的队列长度都跟随常量 ⇒ 把 50 改成 1000 用例也跟着变长；
  补两条**绝对数字**断言（`<= 50`；60 个参数 ⇒ ≥2 次调用）后才抓住 —— 又一次「用例存在 ≠ 断言有效」。

**0-55（P2）—— `retry_job` 的 6 处裸 DB 写整体进 `db_lock`**

- 缺陷：`api/jobs/actions.py::retry_job` 从「清错误信息」到 `commit` 之间**没有锁**，
  而 aiosqlite 是**单连接**：一次 `commit()` 只保证「这一次提交原子」，
  **不**保证「中间没有别人的提交插进来」。
- 修法：6 处写放进**同一把锁、同一次 commit**。⚠️ 两条硬约束写进了代码注释：
  `transition_status()` 与 `_audit_log()` **都自取 `db_lock`**，而 `db_lock` **非重入**
  ⇒ 前者必须在进锁**之前**调用，后者在锁内只能换成裸 `INSERT`（否则**永久挂住**，不是超时）。
- 证据：扫描器 **122 → 116**、棘轮基线 **106 → 100 键**、`retry_job` **6 → 0**；
  变异 **1/1 CAUGHT**（摘锁 ⇒ 扫描器回到 122、棘轮变红）；
  产物级 retry e2e **RC=0**（归档行带 `reviewer_note` 原文 + 活表清空 + 审计含计数与状态分布）。

**0-51（P1，一半）—— `.github/dependabot.yml`**

- `ci.yml` 早已引用但文件不存在。三处**刻意偏离**（写在文件头）：`electron`/`electron-builder`
  进 `ignore`（钉死 43.7.4；43.7.5 在 npmmirror/华为/腾讯/清华镜像上均 404 ⇒ 自动升级会直接
  打断构建）；`github-actions` 保留（全部 action 已钉 SHA）；`pytest`/`pytest-cov` 的 major
  进 `ignore`（95% 门禁按这套版本校准）。
- ⚠️ **CD（`release.yml`）未做**：需要一台能真跑的 Windows runner 与一次真实构建。
  写一份**没跑过**的 YAML 等于制造「已交付 CI」的假象 ⇒ 留到能真跑时再做。

**0-50（P1）—— 真实浏览器 e2e（Playwright）**

- 仓库原有 **23 个 JS 护栏全是源码级 / 假 DOM**，证明不了「上传后真的跳复核页」
  「点确认真的落库」。新增 `tests/e2e_browser.py`：**7 条用例 / 14 个判据**，
  跑**冻结产物** + 真 chromium（上传 → 翻页缩放 → 回首页再上传 → 等终态 →
  确认 finding → 报告页 + 设置页保存 → JS/HTTP 健康）。
- 六条被实测逼出来的设计（每一条都对应一次真实的红）：
  ① 翻页必须用**运行时生成的 3 页**文档 —— `ocr_samples` **全是单页** ⇒ 旧写法走
  「按钮禁用（符合预期）」的**跳过分支**，绿了但什么都没验；
  ② 报告页对**非终态**任务 `303 → 复核页` ⇒ 必须先等终态，否则 `goto` 30s 超时；
  ③ 确认按钮用 `onclick` 里的 fid **精确**定位 —— findings 条数 0-15 非确定，
  取「第一个确认按钮」会点到别人的 ⇒ 随机红；
  ④ 终态可能是 `error`（合成样本 OCR 出空页）⇒ 报告「渲染内容」改由**夹具 job** 承载，
  真实 job 只断言**路由契约**（`review`/`partial_review` ⇒ 200；其他 ⇒ 303 → 复核页）；
  ⑤ 设置页是**分区切换**的（`data-section` + `hidden`）⇒ 必须深链 `#ocr`；且断言载体
  不能用 `#ocr_slices`（它在 `data-backend-form="mineru"` 子表单里，后端为 paddle 时永不显示）；
  ⑥ **首屏 findings 是 SSR 渲染**（`templates/review.html`），`static/review-findings.js` 只管
  **第 2 页起**（`loadPageData()` → `/findings?page=N`）⇒ 只验首屏时，把 AJAX 侧的 onclick
  摘掉**照样全绿**（变异 M1 首轮 MISSED 就是这么来的）⇒ 两条渲染路径各验一次。
- 健康判据取**结构化真值**：`pageerror`（未捕获异常）+ `response` 状态码（4xx/5xx），
  不解析 console 文案 —— Chromium 的「Failed to load resource」**不带 URL**，靠文案判不了。
  唯一放行的 4xx 是 `/api/jobs/{id}/pages/N` 的 404（该页尚无分析产物，接口按设计如此）。
- 变异 **3/3 CAUGHT**（`devlogs/_verify/mutate_browser_e2e.py`，只改**产物副本**，交付物事后 `bundle_manifest --check` 仍 OK）：摘掉**首屏（SSR）**「确认」onclick ⇒ `B5_confirm_ssr` 红；摘掉**第 2 页（AJAX）**「确认」onclick ⇒ `B5_confirm_ajax` 红；摘掉「下一页」onclick ⇒ `B2_page_nav` 红。⚠️ 前两条互不干扰（各自只红自己那条），所以不是同一个断言被数了两次。

**实测（本批全部数字均来自本次运行）**

- 全量 `pytest tests`：**4244 passed / 0 failed / 2 skipped**（933.77s 与 1097.91s 各一次），
  junit 交叉核对 **4246 / 0 / 0 / 2**，覆盖率 **95%**。
- 门禁 `scripts/release_gate.py`：**11/11 PASS** 两次（`gate_report_20261021_104302.json` /
  `gate_report_20261021_121906.json`），`artifact_freshness` 两次均绿。
- 产物级 `tests/e2e_frozen.py`：**covered=4 / skipped=0 / failed=0**，`llm_audit 3/3`。
- 多轮 e2e：**2/2 PASS**（RUN1 1434.3s / RUN2 1040.0s，各 8 轮 `-> OK`、0 `-> FAIL`）。
- 浏览器 e2e：**12/12 OK，RC=0**（`devlogs/_verify/browser_e2e/run_20261021_125751.log`）。

**边界（未验，不得读成通过）**

- `mineru` / `dual` / `rot` 三轮**本轮未复跑** ⇒ **不得**宣称「七轮全绿」。
- Electron 双击层 `tests/e2e_unpacked.py` 在受限沙箱内**不可复现** ⇒ 须在真实终端复跑。
- `findings` 条数仍属 **0-15 非确定项** ⇒ 单次计数**不作**回归判据（本批所有断言都避开了它）。
- 无最小标注集 ⇒ **精度/召回仍无可信数字**。
- 浏览器层**未覆盖真实 happy path**（OCR/LLM 成功 ⇒ `review` 且有 finding）：合成样本 OCR 出空页；
  该路径由 `e2e_frozen` 与多轮 e2e 覆盖（**非浏览器层**）。
- 0-48（泄露的 DeepSeek key）由用户侧轮换，本批**未验**。

### OCR 后端前置门 + LLM 截断治理 + 源码 U+FFFD 清理（Round 87 第三十五批，2026-10-14）

> 承第三十四批。本轮**改入包源**（`api/`、`llm/`、`db/`、`core/`、`tests/`）⇒ **必须重建产物**。
> 三条独立事项一次做完 ⇒ 只有**一次**重建。

**0-33 —— 上传页的 OCR 缺口：从「信息性横幅」升级为「后端前置门」**

- 缺陷：`api/jobs/upload.py` 的 400 前置门只判 LLM ⇒ OCR 未配置时上传被**接受**，
  用户白传大文件（实测样本 **138MB / 51 页**）后在 **Stage 1** 失败。
- 修法：在 **LLM 门之后、并发额度之前**插入对称的 OCR 门，真值源复用既有**单一来源**
  `core/pipeline/ocr_support.ocr_chain_configured()`（= paddle/mineru 远端已配 **或**
  docling 可用）。**位置是硬不变量**：放前面会让 15 条「期望 400 且断言具体文案」的
  入口矩阵用例改报 OCR 错误而全红。
- 配套（否则测试与真实语义脱节）：
  - `tests/conftest.py::test_db` 与 LLM key **同源**注入 mock OCR 凭据（~20 个 `client`
    fixture 全派生自它 ⇒ 一处修好整个爆炸半径）；
  - `tests/e2e_proc.ocr_ready_from_settings` 补 `paddle.api_url` 判据 —— 原实现只读
    `configured`，**比门更宽松**（危险方向）；收紧后本判定成为门的**充分条件**
    （严格 ⟹ 宽松），危险方向被消除。`tests/unit/test_e2e_proc_helper.py` 的旧钉子
    随之更正，并新增「`configured=True` 但 `api_url` 空 ⇒ 未就绪」的反例；
  - `tests/e2e_rejections.py` 加与 `_llm_provider_configured` **对称**的 `_ocr_configured`
    前置 + fail-closed（判不了 ⇒ rc=2，不伪装成通过）；
  - `tests/e2e_frozen.py` 上传步按 `OCR_CONFIGURED` **分支断言**（未就绪 ⇒ 期望 400 +
    断言文案含 OCR，且**不得**伪造 job_id）。
- 护栏 `tests/integration/test_upload_ocr_guard.py`（4 条：拒绝 / 允许 / **LLM 门先于
  OCR 门** / LLM 已配时 OCR 门生效）。

**0-38 —— 截断型 JSON 解析失败的 fix-hint 重试链（行为决策：跳过重试 + fail-closed）**

- 实测现场：`e2e_test.pdf` page 2 **稳定**打满 `_PAGE_MAX_TOKENS=8000` ⇒ `_parse_json`
  失败 ⇒ fix-hint **再发一次**（仍打满 8000）⇒ 单页 **373.7s + 903.2s ≈ 21 分钟**
  （最坏 `3 × 1440s = 4320s`）。根因：`finish_reason` 由 SDK 提供但 `ChatResult` **未采集**
  ⇒ 产品无法区分「截断」与「格式错」，而 fix-hint 对截断型失败**结构性无产**。
- 修法（四层，单一真值）：
  1. `ChatResult` += `finish_reason`（归一化）+ `raw_finish_reason`（原始枚举）。两套协议
     枚举**不同**（openai `length` / anthropic `max_tokens`）⇒ 收敛到
     `llm/adapters/base.normalize_finish_reason`（**唯一**判定入口；`""` = 未报 ≠ stop；
     未知协议/枚举 ⇒ `other`，**不**猜成 length）。
  2. `llm/client.py`：`chat()` 退化为**薄包装**，重试/退避/审计/结构化降级收进新的
     `_chat_result()`（**一个循环、两个消费者**）⇒ 让 `chat_json` 拿到 `finish_reason`
     而**不**另起一份循环。
  3. `chat_json`：`finish_reason == "length"` ⇒ **不发** fix-hint（同 prompt + 同
     max_tokens 必然再次打满）；「截断**且已解析**」单独告警 `content may be incomplete`
     —— 不得与「有重试待跳过」混进同一条文案（那一支根本没有待跳过的重试）。
  4. 留痕：`llm_call_audit.finish_reason`（`db/schema.sql` 单一声明处 +
     `SCHEMA_VERSION 12→13` + `_migrate_v13`）；旧行保持 NULL =「未采集」，不参与判定。
  5. 参数变更清扫：`tests/unit/test_watchdog.py` 的 `SCHEMA_VERSION == 12` 钉子同步为 13
     （`devlogs/_verify/r92e_schema_v13_sweep.py`，含「历史陈述不改」的豁免清单）。
- 护栏 `tests/unit/test_llm_finish_reason.py`（44 条：归一化表**钉字面量** / 适配器取字段 /
  单一真值静态扫描 / 跳重试 + **反向对照**（`stop`/`other`/**未报** 仍重试 3 次）/ `chat()`
  仍返回 `str` / 审计落库 / v13 迁移）。
- ⚠️ **未验**：未在真实上游复现「截断 ⇒ 只发 1 次」（需可用凭据）；`chat()` 非 JSON 路径
  **不**检查截断（现有调用方只有健康 / 设置探测，不消费正文）。

**0-43 —— 源码里残留 23 个 U+FFFD 替换字符（仅注释 / docstring）**

- `core/ocr_client.py`（**19**）+ `core/page_analyzer.py`（**4**）。文件本身是**合法
  UTF-8** —— 损坏是**真码点**，形态恒为「U+FFFD + ASCII `?`」替掉一个汉字（早期编码
  往返的化石）。全在注释 / docstring ⇒ 不影响行为，但污染 grep 与阅读。
- 按 git 历史原文逐处还原（`devlogs/_verify/r92a_fix_fffd.py`，幂等）；
  `ocr_client.py` 23423→23417 B、`page_analyzer.py` 55602→55599 B，字节级确认
  U+FFFD 计数 **23 → 0**，`py_compile` 通过。
- ⚠️ **未做**：全库 U+FFFD 全树扫描（本轮只修这两处**已定位**的文件）。

**变异验证（本仓库硬门槛）**

- `devlogs/_verify/mutation_r92_038_finish_reason.py` ⇒ **11/11 CAUGHT**。纪律：整仓库
  复制到临时目录、变异只打**副本**、`cwd=副本`、**先跑基线金丝雀**（未变异必须全绿）、
  锚点必须恰好命中 1 次。11 条覆盖：还原重试链缺陷 / 常量值改坏 / 未报当成 stop /
  适配器硬编码判定 / 审计不落列 / 版本号回退 / 迁移空转 / `chat()` 泄漏 ChatResult /
  `chat()` 另起循环 / 告警分支合并 / Anthropic 取错字段。
- 顺带加固 `devlogs/_verify/mutation_harness.py`：变异前把原始字节**另存**
  `devlogs/_verify/_mutbak/` + manifest，被打断（SIGTERM 不展开 `finally`）也能据 manifest
  还原 —— 原实现只有 `finally`。

**测试 / 产物级复验**

- 受影响单测：全量 `pytest tests`：**4192 passed / 6 skipped / 2 xfailed**（666.08s，零 FAILED/ERROR）。6 条 skip 均有登记理由 —— 0-39 图标管线未接线 ×2、真实 Paddle 产物不在场 ×4；2 条 xfail = 0-42 显式登记
- 产物级入口拒绝矩阵 `tests/e2e_rejections.py`：**34/34**（matrix 23 / hostile 8 / dedup 3；`exe_sha256[:12]=d2d85c7a8a93`，两份产物同源）
- 冻结冒烟 `tests/e2e_frozen.py`：**E2E_RC=0**（covered=4 / skipped=0 / failed=0；`llm_audit_success=3/3`、`ocr_backend_used=paddle`、终态 `review`、`health v1.2.1`）
- 门禁 `scripts/release_gate.py`：**11/11 PASS**（`4192 passed / 0 failed`，coverage **95.11%**；报告 `devlogs/gate_report_20261014_091529.json`）
- ⚠️ **未跑通 —— 双击层 `tests/e2e_unpacked.py`**（本批**必列**，不得读成已通过）：`D1_startup` 失败，stderr `GPU process isn't usable`。**已排除沙箱假设** —— 关闭沙箱隔离重跑**同样**失败（`r92_e2e_unpacked_ns.log`）⇒ **宿主环境问题，非产品缺陷**（与 R65 结论一致；加 `--disable-gpu` 亦不能绕过）⇒ **须在真实终端复跑**。


### 422 vs 356 归因 + 描述模板有损（Round 86 第三十四批，2026-10-13）

> 承第三十三批。本轮**只改 `docs/` + `tests/`（均不入包）** ⇒ **不重建产物**。
> 目的：把上一批**自己登记为「未解释」**的计数差（0-41 的 `422 vs 356`）**查清**。

**Findings（0-41 的未解释差 → 已归因；并拆出 0-42）**

- 🔴 **机制**：写库去重键是 UNIQUE `idx_findings_dedup(job_id, source, page, type, description)`，
  而 `core/rules/llm_checks.py` 的三个产出分支都用**裸 dict + 不含 `time`/`step_no` 的
  f-string** ⇒ `kind='cell'` 的**多时间点**条目描述**逐字相同** ⇒ `INSERT OR IGNORE` **静默合并**。
- **直接观测（跨 7 个 job）**：全库凡带「参数数量过多（N）」的 job，行数**均** < N ——
  `550d2fda 420/497`、`33495b33 414/482`、`749ead79 410/477`、`58bdb529 366/432`、
  `3a42f4b7 364/428`、**`670672f3 356/422`**、`63fa17db 339/407` ⇒ 丢 **64–77 条（13–17%）**。
  ⚠️ 这 7 个 job 只有 **3 个文件名**（`51pages*.pdf` ×4 + `丝裂霉素提取批记录.pdf` ×3），
  实为**同一份 51 页文档的多次跑** ⇒ 是**可重复性**证据，**不是** 7 份独立文档。
- **离线重放**（`devlogs/_verify/r91f_reproduce_422.py`；`page_cache` 有该 job 全 51 页，
  规则层是纯函数）：队列 **400** → 按历史模板去重后 **337**（丢 **63 = 15.75%**），
  与跨 job 区间吻合。400 vs 当时 422（差 22）= **规则层自 2026-08-19 漂移**。
- **判别「模板有损」vs「纯重复」**：32 个重复组里 **28 组是模板有损**（成员只在 `time`，
  偶尔 `step_no` 上不同 = 本是**不同时间点的不同测量**），丢 58 条；仅 4 组（5 条）全字段相同。
- **对照（规则层不丢）**：规则层 cell 判定走 `make_oos_finding`，`_where_phrase` 把时间点清单
  写进描述 ⇒ 同输入产出 **2 条互异**描述（`在 10:38 时` / `在 11:34 时`）。
  ⇒ 缺陷在**产出侧**，**不是** UNIQUE 索引本身。
- **暴露面（条件）**：把队列按各分支模板重算 —— `in_spec=False ⇒ param_out_of_spec` 分支
  同样 **400 → 337（丢 63）**（149 条是 cell）；`in_spec=None` 分支更差 **400 → 270（丢 130）**，
  因为该模板连 `actual` 都不含。⚠️ **未直接观测**：全库 `source='llm_fallback'` 仅 7 条，
  本 job 的 fallback 被 0-41 的 fail-closed 拦掉 ⇒ 这是**条件暴露**，不是已发生的丢失。
- 登记为 **0-42（P1，未修）**；**0-41** 的「未解释」改为「已归因」。

**Tests（护栏 —— `xfail(strict=True)` 显式登记 + 陈旧检测）**

- `tests/unit/test_cross_page_llm.py::TestQueueFindingsAreNotCollapsedByDedupKey`：
  2 条 xfail 钉住「两个时间点必须产出可区分的 description」（cap 分支 + `param_out_of_spec` 分支）
  + **1 条规则层正对照**（现在就绿，证明修法方向有效）。`strict=True` ⇒ 修好后会
  **XPASS → FAIL**，强制显式移除登记（不是「永远绿」的放松）。

**边界（未验，不得读成通过）**

- 0-42 **未修**：改描述会改去重指纹 ⇒ 影响**存量库**与复核页展示 ⇒ 属**行为决策**。
- `llm_fallback` 源的真实 OOS 丢失**未直接观测**（63 条是条件暴露）。
- 400 vs 422 的 22 条差归因于**规则层漂移**（未逐条比对历史规则集）。

### 真实标注样本登记 + 参数自动判定降级定位（Round 86 第三十三批，2026-10-13）

> 承第三十二批。本轮**只改 `docs/` + `tests/` + `scripts/`（均不入包）** ⇒ **不重建产物**
> （`dist-electron` 仍为第三十二批产物，`artifact_freshness` 不受影响）。
> 目的：把用户长期提的「判定质量无可信数字」落成**可复现的第一步**（TODO #161）。

**Added（真实样本口径 —— 与合成集口径分开）**

- `docs/FINDING_GROUND_TRUTH.json` 新增 **`real_samples`** 块（**非判据**：`criterion:false` /
  `truth_declared:false`）：登记**首个真实标注样本** —— `用户标注.png`（仓库根）红框圈出
  规格行 `▲pH 为 6.8~7.0。` 与实测行 `中和后 pH 6.88`，定位到 job `670672f3-d0f`
  （`丝裂霉素提取批记录.pdf` / `partial_review` / 51 页 / `ocr=mineru` / 2026-08-19）的 **page 6**。
  ⚠️ **无标签**（`annotation.label: null` / `intent_confirmed: false`）：该行「应为 OOS 还是合规」
  **未与业务方确认**（6.88 落在 [6.8, 7.0] 内）⇒ 不得当已知真值用（TODO #161 原文要求）。
- `scripts/eval_findings.py`：文本报告**新增独立小节**「真实样本口径」⇒ 合成集口径（`cases` 的 F1）
  与真实样本口径**分开呈现**；并由机检保证 `evaluate()` 的返回**不含**任何真实样本内容（口径分离）。

**Findings（参数自动判定被整体吞没 —— 一手取证）**

- 🔴 **page 6 的参数判定被 fail-closed 分支整体降级**：`core/rules/llm_checks.py:109-118`
  在 `len(llm_queue) > _LLM_FALLBACK_BATCH_MAX(=50)` 时**不调 LLM**，改为 flag 人工复核。
  实测该 job 有 **356** 条带「超出自动判定上限」的 findings（分布在**页 5–47**），page 6 占 **5** 条
  （发酵批号 / 接收体积 / 残液体积 / 磷酸批号 / 磷酸用量）⇒ 该页 **0 条 `param_out_of_spec`**。
- ⚠️ **实测值 `6.88` 在全 job findings 中命中 0 次**（规格串 `6.8~7.0` 命中 1 次，
  出现在一条 `completeness` 的 description 里）⇒ 实测值**根本没进入系统**。
- ⚠️ **未解释的计数差**：reason 内嵌计数 = **422**，而带该 reason 的 findings = **356**（差 **66**）。
  成因**未查**（该 job 由**旧版本**产生，且 `partial_review`）⇒ 登记为待查，**不猜**。
- 登记为 **0-41**（未修；属行为决策）。

**Tests（护栏）**

- `tests/unit/test_eval_findings.py` 新增 `TestRealSampleContract`（**7 条**）：块必须显式
  `criterion:false` + `truth_declared:false`；每条样本 `label is None` / `intent_confirmed is False` /
  `usable_as_criterion is False` + 必须写明待确认问题；locator 必备字段 + 显式 `pixel_binding`；
  `observed_system_output` 必须自报测量环境；**AST 机检「判据路径不得引用 `real_baselines` /
  `real_samples`」** + 防空转；**口径分离**（`evaluate()` 返回不含真实样本内容）。

**Docs**

- `DEPLOYMENT.md`：由「**无真实标注集**」改为「**仍无真实标注集**（首个真实样本已登记但
  **无标签**）」⇒ 结论（精度/召回无可信数字）**不变**，但口径不再含混。
- `docs/TODO.md`：**#161** 更新为「真实样本已登记、意图待业务方确认」；新增 **0-41**。

**边界（未验，不得读成通过）**

- 真实样本**只有 1 条**且**无标签** ⇒ **仍无**真实口径的 P/R 数字（C1 未解）。
- `用户标注.png` 与 `p6` 的**像素级绑定未完成**（两份 PDF 均纯扫描件；全页模板匹配 51 页
  >11 min 未收敛，已放弃）⇒ 页码来自 R76 记录 + DB 内容复核，**不是**图像定位结论。
- `356 vs 422` 的差**未解释**。

### LLM 截断解析载荷定位修复（Round 86 第三十二批，2026-10-13）

> 承第三十一批。本轮**修一处入包源**（`llm/client.py`）⇒ **必须重建产物**
> （`dist-electron` 随本批刷新）；**不升版号**（缺陷修复、无接口/行为契约变更，且 1.2.1 尚未分发）。

**Fixed（真缺陷）**

- 🔴 **`_parse_json` 在「前导文本 + 截断」时返回内层片段 ⇒ 该页 findings 丢失**
  （消费侧实测 `r90u`：**非静默** —— 片段缺 `page_info`/`steps` ⇒ schema 校验失败 ⇒
  **多一次 LLM 调用** ⇒ 以重试结果为准，重试仍失败才 `_schema_warn`）：旧实现先用
  `if not text.startswith(("{","[")):` 跳过截断恢复，再用双定界符 `find`/`rfind` 块扫描；
  当外层对象被截断、而内部已有**闭合**的数组/对象时，`rfind` 会抓到那个**内层**片段并当作
  整页结果返回 —— **无异常、无重试、`_truncated_recovered` 不置位**，该页 findings 静默丢失。
  实测（`devlogs/_verify/r90m_038_parse_probe.py`）：4/4 前导文本截断样例返回内层片段。
  修法：载荷起点 = 第一个 `{`/`[`，用 `json.JSONDecoder().raw_decode` 取**一个**完整值
  （完整 JSON + 尾随文字的行为与旧实现等价），失败才走**唯一**恢复实现
  `_recover_truncated_payload`（只试与载荷首字符匹配的那一对定界符、**不再 `rfind`**），
  仍失败 ⇒ `_parse_error`（**fail-closed**）。`_repair_truncated_json` 本体**未动**。
- 🟠 **`_repair_truncated_json` 返回契约违例 3 例**（R3/R4 裸词、R5 冒号后 ⇒ 返回值非合法
  JSON；根因 L681 `out = out[:j+1]` 会切掉已消费的 `"`）。新路径**不依赖**它：先
  `json.loads(repaired)` 校验，违例时**丢弃**（不再被当作结果返回）。

**Docs**

- `docs/TODO.md`：**0-38** 的「推断」改为**实测事实**（原表述「前导文本 ⇒ parse_error」被
  取证**推翻**）；新增 **0-40** 登记本次定位 + 修复 + 变异。

**Build / Gate（重建证据）**

- 入包源变更 ⇒ 本批**重建**（复刻 `build.ps1` 四步）：CSS（`app.css` md5 未变）→
  PyInstaller（冒烟 `/health` 200，~2s）→ `bundle_manifest --write/--check`
  （`version=1.2.1 files=116 head=a225fb8 dirty=False`）→ electron-builder
  （`electron=43.7.4`；日志含 `default Electron icon is used` = 0-39 的**预期**现象）→
  `PROVENANCE.txt`（`git_head=a225fb8` / `standard output: dist-electron was NOT locked`）。
- 产物：`win-unpacked` = 498,334,075 B / 907 文件；内嵌 `pbc-server.exe` sha256
  `6b100b39b70355c3d3f8d7f2ab93ee458e1707a1f9382d4a0b134fe92678bf66`
  （== `dist/pbc-server` 产物）；`_internal` 内**无** `scripts`/`tests`/`docs`/`devlogs`。
- 门禁 `devlogs/gate_r90_r32.json`：**11/11 PASS**（`tests_coverage` 4135 passed /
  0 failed / coverage 95.1%；`artifact_freshness` 两份产物与源码逐字节一致）。
- 便携版 zip 重打（`--force` 覆盖同名）：205.0 MB / 908 成员，sha256
  `92fbd8e5392f2c0ebcdb7a977de26f1174ad28be8d257192d17cf41eabee75fb`。
- **zip 实物端到端**（解压 → 内嵌 exe sha256 对账 → `PBC_E2E_REQUIRE_LLM=1` 跑
  `tests/e2e_frozen.py`）：`E2E_RC=0`、**29 passed / 0 failed**、`covered=4 / skipped=0 /
  failed=0`、`llm_audit success=3/3`、`ocr_backend_used=paddle`、`pipeline_terminal=review`、
  `settings_llm_model_matches=deepseek-ai/DeepSeek-V3.2`。⚠️ `findings_api count=3`（上一轮
  同一条 e2e 为 4）—— 这正是 **0-15 已登记的非确定性**，**不得**把单次计数当回归判据。

**Tests（护栏）**

- `tests/unit/test_llm_client.py::TestParseJson` 新增 6 条：前导文本截断须恢复**外层**对象
  （不是内层片段）、数组载荷同理、**修不回来必须 fail-closed**、完整 JSON + **含方括号**的
  尾随文字仍须解出、载荷定位器**永不返回片段**。
- 变异（`devlogs/_verify/r90p_mutation_parse_payload.py`）**5 场景全部 AS EXPECTED**，
  `drift=none`：还原旧实现 ⇒ **4 条红**；`raw_decode` → 双定界符 `rfind` 扫描 ⇒ 3 条红；
  `raw_decode` → `json.loads`（去尾随文字容错）⇒ 1 条红；两个**负控**（只改注释 / 交换
  定界符对顺序）⇒ 全绿。
- ⚠️ 既有 `test_parse_truncated_json_recovers` 只断言 `isinstance(result, (dict, list))`
  ⇒ 片段与整体**都能过**（弱到不可能红）—— 这是本缺陷长期存活的原因。

**Findings（消费侧复核 —— 更正本批初稿的一处推演）**

- 初稿把旧实现后果写成「静默数据丢失」，并推测「若按 list 遍历，片段会变成无 finding 的
  成功页，比 parse_error 更糟」。**实测推翻该推演**（`devlogs/_verify/r90u_stage2_consumption.py`，
  给真实 `analyze_page` 喂片段）：消费侧 `core/page_analyzer.py` 对 list 取 `result[0]` 后走
  `_validate_page_result`，缺 `page_info`/`steps` ⇒ **必然**校验失败 ⇒ **多一次 LLM 调用**
  （实测 `calls=2`）⇒ 重试仍失败才 `_schema_warn`（review 页可见）；`[]` ⇒ `_parse_error`。
- **唯一静默**情形 = 片段**恰好**含 `page_info` + `steps`（构造性；实测该情形返回值键与
  **正常页完全无法区分**）⇒ 已加护栏
  `tests/unit/test_page_analyzer.py::TestFragmentNeverSilentlyPersisted`（片段必须被标记
  + 防空转正对照；变异：`REQUIRED_PAGE_FIELDS` 改空集 ⇒ **恰好 2 条红**）。
- 修复后**端到端**复核（`r90v`）：6 个「前导文本 + 截断」输入经新 `_parse_json` 再喂
  `analyze_page` ⇒ **零静默**（有告警或显式失败）。结构上：载荷起点 = **第一个**定界符 ⇒
  `raw_decode` 解出的必然是最外层值 ⇒ 内层片段**不可达**。

### 便携版图标管线定位 + 忽略规则锚定（Round 86 第三十一批，2026-10-13）

> 承第三十批。本轮**不重建**（改动**全部不入包**：`.gitignore` / `docs/**` / `tests/**` / `devlogs/**`）
> ⇒ 按项目纪律**不升版号**（边界 = 是否进入 PyInstaller 产物），产物与第三十批逐字节相同。

**Fixed（真缺陷）**

- 🟠 **`.gitignore` 的 `build/` 未锚定 ⇒ 连带吃掉 `electron/build/`**：该目录是**打包图标**
  的落点（`directories.buildResources` 的目标），却因这条规则**从未进过版本控制**
  （`git ls-files electron/` 只有 `main.js`；`git log -- electron/build/` 为空），且是**静默**的。
  全树 `build` 目录只有 `./build`（PyInstaller workpath）与 `./electron/build` ⇒ 已改为
  **`/build/`**（锚定），并把 `electron/build/` 改为**显式**忽略（附退出条件）。

**New（护栏）**

- `tests/unit/test_repo_hygiene.py::TestIgnoreRulesAreRootAnchored`（2 条）：忽略规则必须
  **锚定到根** —— 用见证路径 `core/build/…`（无显式规则，故只有未锚定的 `build/` 才会命中）
  做**行为化**判定（问 `git check-ignore` 的返回值，不读 `.gitignore` 文本）。
- `tests/unit/test_distribution_parity.py::TestPackagingIconIsWired`（9 条）：一旦
  `build.win.icon` 被声明，它就必须**存在 + ICO 头含 ≥256×256 条目 + 不被忽略清单吃掉 +
  与 `electron/main.js` 的运行时图标同源**。现状**未声明** ⇒ 两条条件式用例 `SKIP` 并指向
  `docs/TODO.md` 0-39（**诚实标注，非假绿**）；另 7 条**无条件**正负对照证明检测器不空转。

**变异验证**（`devlogs/_verify/r90l_mutation_icon.py`）：**7/7 AS EXPECTED**，逐文件还原
sha256 一致（drift=none）—— S0 基线 **SKIP**；S1 `win.icon`=16×16 ⇒ 红；S2 `main.js` 与
builder 不同源 ⇒ 红；S3 图标不存在 ⇒ 红；S4 `.gitignore` 退回未锚定 ⇒ 红；**S5 负控**
（图标同源、但仍 16×16）⇒ **绿**（证明 S2 红的是"不同源"而非"图标不合格"）。

**便携版 zip 的实物端到端（本轮补做）**

`devlogs/_verify/r90j_zip_e2e.py` 把 `release-archive/BatchSentry-Portable-1.2.1-20261013.zip`
解压（**908** 成员）后，用**解压出来的内嵌** `pbc-server.exe` 跑 `tests/e2e_frozen.py`
（`PBC_E2E_REQUIRE_LLM=1`，真凭据）⇒ **`E2E_RC=0`、29 passed / 0 failed**、覆盖账本
`covered=4 skipped=0 failed=0`；`llm_audit success=3/3`、`ocr_backend_used=paddle`、
`pipeline_terminal=review`、`settings_llm_model_matches=deepseek-ai/DeepSeek-V3.2`；
解压出的内嵌 exe sha256 `3b174f3f…41e` **== `dist/pbc-server/pbc-server.exe`**；
`PORTABLE_README.txt` 位于 zip 根。⇒ **"用户解压后双击的那份"已被端到端验过**。

**定位（未修，登记 `docs/TODO.md` 0-39）**

- 图标管线**两处都没接线**：`main.js` 找 `electron/icon.ico`（不存在）、electron-builder 找根
  `build/icon.ico`（不存在，且根 `build/` 是 PyInstaller workpath、会被 `--clean` 清掉
  ⇒ 默认位置**不可用**）；唯一在盘的 `electron/build/icon.ico` 是
  **190 B / 16×16 / 1-bpp DIB 占位**（< builder 的 256×256 下限）⇒ 产物 `BatchSentry.exe`
  用 **Electron 默认图标**。**未接线是有意的**：先接 16×16 会把「静默降级」变成「构建硬失败」。
  修它需**真实资产**（本项目无任何 logo/favicon 可派生 ⇒ 属产品决策）。

**更正（本轮自查）**

- 撤回一条**误记**：此前记录称「`CHANGELOG.md` 含 `421.7 MB` 与 `418.9 MB` 自相矛盾」——
  全库 grep **零命中**该两数。实测 `win-unpacked` = **498,333,600 B（475.2480 MiB）/ 907 文件**；
  正文 `475.1 MB` 属 **W2 收尾（2026-09-24）** 那次构建，与当前产物是**两次不同构建**的
  0.1 MiB 差异，**不是矛盾**。⇒ 该"缺陷"不存在，未做任何"修复"（不编造、也不"修"不存在的问题）。


### 重建产物（干净树）+ 核销 0-34：启动等待注释改为引用常量名（Round 86 第三十批，2026-10-10）

> 承 R89。本轮把 R86/R87 一直延后的 **0-34** 结掉，并按项目纪律「**先提交 → 重建 → 全量跑**」
> **在已提交的树上完整重跑 4 步构建**。

**前提破除（本轮实测）**：R86/R87 把 0-34 挂在「需宿主退出」（无法重建）上。本轮用
`os.rename` 往返探测三份产物 —— `dist-electron/win-unpacked/resources/app.asar`、
`.../resources/pbc-server/pbc-server.exe`、`dist/pbc-server/pbc-server.exe` —— **全部成功**
⇒ 宿主**未**持有它们（无 `FILE_SHARE_DELETE` 锁）⇒ **重建可行**。

**改动**：`electron/main.js:13` 的文件头注释 `(up to 30s)` → `(bounded by READY_TIMEOUT_MS)`
—— **引用常量名、不再复述数值**（`READY_TIMEOUT_MS = 180_000`）。⚠️ **只改 L13**：
L45 / L239 是**历史叙述**（引用已删除的旧行为 `not ready after 60 checks`），非活规格；
L278 是**面向用户**的报错文案且当前值与常量一致，均不动。

⚠️ **该改动必然让一条单测变红，直到 asar 重建**（`docs/PROJECT_PITFALLS.md` §六十三）：
`test_bundle_manifest.py::TestAsarReader::test_real_asar_matches_source_for_electron_main`
把 `app.asar` 里的 `electron/main.js` 与源码**逐字节**比对 ⇒ 与重建**不可分离**。
本轮据此前置确认后按顺序执行，§六十三 的「一般化约束」由此**闭环**。

**重建（4 步，干净树 `06c18a1`）**：

  1. `npm run build:css` —— `static/app.css` **无 diff**（已最新）；
  2. `PyInstaller pbc-server.spec --noconfirm --clean`；
  3. `bundle_manifest.py --write`（`git_dirty=false`、`files=116`）→ `--check` 通过；
  4. `electron-builder --win --x64` → `gen_provenance.py`（`git_head=06c18a1`、BOM+CRLF）。

**产物一致性（实测）**：asar 内 `electron/main.js` == 源码（34719 B）；两份 `pbc-server.exe`
**同 sha256** `3b174f3f1a875d9123a10fe21fc414a26f7526550dddb6702dbe5c99f42e641e`。

**门禁**（干净树，报告 `devlogs/gate_report_20261010_164104.json`）：**11/11 PASS** ——
`artifact_freshness` **由红转绿**（2 份产物逐字节一致）；`tests_coverage` **4114 passed,
0 failed, coverage=95.09%**（junit `devlogs/gate_junit_20261010_164107.xml`）。

**产物级 e2e（全部打 Electron 内嵌的那份 `pbc-server.exe`，真凭据，`PBC_E2E_REQUIRE_LLM=1`）**：

  · `tests/e2e_frozen.py` **连跑 3 次** ⇒ 每次 **29 passed / 0 failed**、`E2E_RC=0`、
    覆盖账本 `covered=4 skipped=0 failed=0`（`devlogs/e2e_coverage_20261010-164855.json` /
    `-165038.json` / `-165208.json`）；每次 `llm_audit success=3/3`、`ocr_backend_used=paddle`、
    `pipeline_terminal=review`。
    ⚠️ `findings_api` 计数 **4 / 4 / 3** —— 已知**上游非确定性**（0-15 / 0-29），**非**失败。
  · **7 轮**多轮冒烟（`pdf,img,mineru,rot,robust,cancel,dual`；`e2e_run.py`，
    `devlogs/_verify/r90_e2e_multiround.log`）⇒ **7/7 达成、`ALL ROUNDS PASSED`**：

    | 轮 | 终态 | 页 | 耗时 | findings | OCR 后端 | LLM 成功 |
    |---|---|---|---|---|---|---|
    | pdf | review | 6 | 296s | 26 | paddle | 7/7 |
    | img | review | 1 | 252s | 11 | paddle | 3/3 |
    | mineru | review | 6 | 465s | 24 | **mineru** | 8/8 |
    | rot | review | 4 | 889s | 16 | paddle | 5/5 |
    | robust（复合 o6+o1） | review | 1+1 | 84s+180s | 1+1 | paddle | 2/2 + 2/2 |
    | cancel | cancelled | — | 1s | — | — | 豁免 |
    | dual（对比） | partial_review | 6 | 1001s | 27 | mineru | 8/8 |

    · `rot`：横向页 `via=[{page2,rotation@90°},{page3,rotation@90°}]`、`lost=[]`、
      `audit_rotation_events=1` ⇒ 旋转页经 OCR 正确复原。
    · `dual`：`dual_compare` 审计事件齐（`start` + `done`），`diff-completeness findings=2`。
    · 全部轮次 `llm_failed=0`；`cancel` 按设计豁免（不经 `run_upload`）。

⚠️ **未做 / 未验证（不得读成已通过）**：

  · **Electron 层 e2e**（`tests/e2e_unpacked.py`）在受限沙箱内**不可复现**（R65 实测：
    请求关沙箱隔离后**仍崩**）⇒ 须在**真实终端**复跑；
  · 本轮 e2e 是**单次**重复（frozen ×3、多轮 ×1），**不是** N 次重复的**区间**估计 ——
    findings 计数的上游方差（0-29）依然存在，**单次计数不得当回归判据**；
  · `dual` 轮出现**上游 LLM 长延迟**（page2 单次 **424s** 且 JSON 解析失败触发 fix-hint
    重试）—— 属已知上游行为（0-10），非本轮引入。


**追加：同轮 e2e 暴露的新发现（登记为 `docs/TODO.md` 0-38，未修）**

补跑**安全性/并发**两维后暴露一条**可复现**的健壮性缺陷 —— `e2e_test.pdf` 的 **page 2**
稳定触发 LLM 输出**打满** `_PAGE_MAX_TOKENS=8000` ⇒ JSON 解析失败（截断修复未救回）⇒
`chat_json` 的 fix-hint 重试**再发一次**（**仍**打满 8000）⇒ 单页实测 **373.7s + 903.2s**
（≈21 分钟）。`finish_reason` 由 SDK 提供但 `ChatResult` **未采集** ⇒ 产品**无法**区分
「截断」与「格式错」；fix-hint 文案也不含「精简」提示 ⇒ 对截断型失败**无产**。
最坏 ~3×1440s = **4320s**（即 0-10 的算术，本轮首次拿到**实测现场**）。

  · **归因（3 条 FAIL 单一根因）**：5 路并发 e2e **6/9** —— 3 条 FAIL 全部源自**该 job 未在
    驱动 1500s 预算内到终态**：① 未终态；② `per_job_isolation_storage`（驱动对**非终态**
    job 的 API 计数取 0、DB 仍含其**部分写入** ⇒ 23 vs 21 —— **驱动口径**，非产品缺陷）；
    ③ 残留 `cancelling`。
  · **并发本身正常**：5/5 准入、峰值重叠 = **5**（真并发）、`error` 终态 = **0**、
    并行收益成立（总墙钟 1501s < 各 job 之和 2263s）。
  · **同轮补跑的安全性维度全绿**：入口拒绝矩阵 **34/34**（保留设备名 / 带前缀设备名 /
    敌意文件名安全不变式 / 去重-force；`%2f` 未被解码）；5 文件并发**准入 + UI** **7/7**。
  · 同轮**修正 0-10 的前提**：其原文写「需上游在 call1 之后**彻底失联**」，实测触发面**更宽**
    —— **输出打满 `max_tokens`** 即可触发（与上游失联无关）。

### 安全维度扫描 + 命令执行面静态护栏（Round 85 第二十九批，2026-10-10）

> 承 R88。对抗性审查走到"安全性"：**先扫已知高危形态，再把它钉住**，不停在"扫过了"。

**扫描（应用包 `api/ core/ llm/ db/ models/` + 4 个根模块）**

  · `shell=True` / `os.system` / `os.popen` / 裸 `eval` / `exec` —— **一处都没有**；
  · 3 处 f-string SQL（`db/client.py`）**均非注入面**：`SCHEMA_VERSION` 是 int 常量、
    `col_def` 来自模块内字面量元组；PRAGMA 那处已写明 "cannot be parameterized" 的理由；
  · 文件服务端点的路径穿越防御**一致**：`serve_pdf`（`main.py`）与 `page_image`（`api/jobs`）
    都是「本机守卫 → DB 查行 → `relative_to(output_dir)` 包含性检查」；`actions.py` ×2 同款；
  · `LocalGuardMiddleware` 对**每个** http 请求判 `is_local_request` ⇒ `review.py` 的 POST
    不写逐端点守卫**不是缺陷**（中间件已覆盖），`actions.py` 那些是**纵深防御**。

**顺带实测**：应用源有 **3 个文件带 BOM**（`api/jobs/actions.py`、`api/settings/probe.py`、
`api/settings/provider.py`）—— Python tokenizer 容忍（应用不受影响），但
`ast.parse(open(p, encoding="utf-8").read())` 会对残留 U+FEFF 报 SyntaxError
⇒ **AST 工具必须 `encoding="utf-8-sig"`**（写本条护栏时即踩到）。

**钉住**：`tests/unit/test_no_dangerous_exec.py` —— AST 扫描应用包，禁上述形态；
含**反空转**（扫描面塌缩到 <30 个文件即红，否则"什么都没扫到"会被读成"很干净"）；
**刻意只扫应用包**（`scripts/**` 合法地调 git/pytest/PyInstaller）。
变异 `devlogs/_verify/r88f_mutation_exec_guard.py` ⇒ **3 CAUGHT + 1 负控 OK**
（`shell=False` 的合法 subprocess 不得误伤）。

⚠️ **边界（不得读成"已安全"）**：本护栏只拦**语法上可见**的调用 —— `getattr(os, "system")(...)`、
经第三方库的间接执行、数据流可达性层面的注入都绕得过去。它是**回归网**，不是安全证明。

**门禁**：本轮只加 1 个测试文件 + 文档（**均不入包**）⇒ 失效面不变。

### 诊断报文不得低报：`artifact_freshness` 的路径清单截断改为**显式声明**（Round 84 第二十八批，2026-10-10）

> 承 R87（Round 83）。本轮修的是**上一轮我自己踩到**的缺陷：门禁报文说"**8 个**源文件与
> 清单不符"，却只列 **5** 个 ⇒ 我据此以为失效面是 5 个。

**缺陷**：`scripts/bundle_manifest.py` 有 5 处报文写成 `f"{len(x)} 个…：{x[:5]}"` ——
`len()` 是真值、`[:5]` 只显示 5 个。**数量与清单互相矛盾，而清单更显眼 ⇒ 读者信清单**。
该报文的**唯一作用**就是告诉读者"哪些文件要重建"，藏起 3 个即**无法据以行动**。
门禁侧 `release_gate.py` 的 `problems[:4]` 属同类。

**修法（判据不变，只让报文诚实）**：新增 `_fmt_paths(items)` —— ≤ 上限（20）全列；
> 上限列前 N 个并**明说**"另有 M 个未显示，共 K 个"；门禁侧补"另有 N 项未显示"。

**护栏 + 变异**

  · `tests/unit/test_bundle_manifest.py::TestProblemReportDoesNotUnderReport`（**3 条**）：
    上限内全列 / 超限**明说**余量 / 端到端 **6 个失配源**时每个路径都出现（旧 `[:5]` 会吞掉第 6 个起）。
  · `tests/unit/test_release_gate.py::TestArtifactFreshness::
    test_truncated_problem_list_declares_the_remainder`（**1 条**）：7 项时明细必须写"另有 3 项未显示"。
  · 变异 `devlogs/_verify/r88a_mutation_underreport.py` ⇒ **基线绿 + 3/3 CAUGHT**
    （M1 回退 `[:5]` / M2 去掉上限 / M3 去掉门禁标记）。

**实测**：`--skip-tests` 门禁的报文现在**列全 8 个** —— `['api/jobs/listings.py',
'core/pipeline/ocr_support.py', 'core/procpool.py', 'main.py', 'static/settings.js',
'static/upload.js', 'templates/settings.html', 'templates/upload.html']`。
⚠️ 本轮只改 `scripts/**` 与 `tests/**`（**均不入包**）⇒ **不扩大** `artifact_freshness`
失效面（仍是那 8 个）；产物过期状态**不变**，分发前仍需重建。

**全量门禁（head `f1c8879`）**：**10/11 PASS**，`tests_coverage` = **4106 passed, 0 failed, coverage 95.09%**（较 R87 的 4102 **+4** = 本轮新增护栏 3 条 + 门禁护栏 1 条）；唯一红仍是 `artifact_freshness`（同 8 个入包源）。报告 `devlogs/gate_report_20261010_154313.json`。

**清扫（同轮）**：按同一形态把 `scripts/**` 全扫一遍，又找出 **6 处**同类（`release_gate.py` 4+1、`golden_gate.py` 2、`seed_kb.py` 2、`clean_dist.py` 1）⇒ 抽出**唯一实现** `scripts/report_fmt.py::fmt_list`（`sep=None` 渲染列表字面量 / `sep="; "` 保留调用方原连接风格），各脚本按**文件位置**互导（同 `release_gate` 既有做法）。护栏 `tests/unit/test_report_fmt.py`（边界 + 反空转 + **只有一处实现**的静态扫描）+ `tests/unit/test_release_gate.py::TestOrchestration::test_check_worktree_clean_declares_truncated_remainder`；变异 `devlogs/_verify/r88d_mutation_shared_fmt.py` ⇒ **3/3 CAUGHT**。（既有好做法：`check_leaked_keys.py` 早已写 `等 N 个 blob`、`compare_ocr_engines.py` 写 `另 N 页` —— 好做法早就在仓库里，只是没被推广。）

**全量门禁（head `43f968f`）**：**10/11 PASS**，`tests_coverage` = **4113 passed, 0 failed, coverage 95.09%**（较上一提交 4106 **+7** = 本轮新增护栏 6 条 + 门禁用例 1 条）；唯一红仍是 `artifact_freshness`（同 8 个入包源）。报告 `devlogs/gate_report_20261010_160205.json`。

### 一次参数变更的清扫只做了 1/5 + settings「测试连接」窄屏不可达（Round 83 第二十七批，2026-10-10）

> 承 R86（Round 82）的分发/UX 评估。本轮按 **先定位 → 再解决 → 后测试** 推进；
> 两条缺陷都落在**用户/维护者能直接读到**的位置，且都用**机检 + 变异**锁住。

**修复 1 · `MAX_CONCURRENT_JOBS` 3 → 5 的清扫只做了 1/5**
R85（Round 81）把默认值从 3 抬到 5，但只扫了 `README.md` 与
`tests/unit/test_upload_quota_contract.py` 的 docstring。全库仍残留 **7 处「默认 3」**，
其中两处代价最高：

  · `CLAUDE.md`（**agent 指令文件**，每次会话注入）**2 处** —— 写错的默认值会误导后续
    agent 对并发上限的判断，与 R86 修的 OCR failover 阈值**同一类**缺陷；
  · `static/upload.js`（**入包源**，随包分发）—— 注释里的错值会被下游当成「产品事实」。

另有 `core/procpool.py`、`api/jobs/listings.py` 及 3 个测试 docstring。⇒ **10 处**一次扫净
（`devlogs/_verify/r87a_concurrency_sweep.py`）。

**修复 2 · 同一次改动**蕴含**了两处叙述变成假命题**：默认 5 时空位够，
「用户选 5 份 ⇒ **第 4、5 份必然吃 409**」不再成立（`static/upload.js` +
`tests/unit/test_upload_quota_js.py`）。⇒ 改为**与默认值无关**的表述（「活跃数已达额度时，
排在空位之外的几份必然吃 409」）—— **由默认值派生的序数/条数本身就是副本**，
写完即随下次调参过期。

**修复 3 · `core/procpool.py` 的 ~2GB 归因（已被实测否证）仍在当分母用**
注释写 `+60MB 相对「3×200MB PDF 最坏 ~2GB」约 3%`，而该 ~2GB 归因早在
`docs/ADVERSARIAL_REVIEW_2026-09-29.md` §内存维度被实测否证（单 job ≈ 102MB）。
⇒ 删去该百分比，改为标注**已否证** + 指向实测出处（**不**另编一个新百分比）。

**修复 4 · settings 页「测试连接」在窄屏**完全不可达**（UI 缺陷）**
唯一入口 `#test-conn-btn` 位于 `<nav class="settings-nav hidden md:flex …">`（**桌面侧栏专属**）；
窄屏下侧栏整体 `hidden`，而 `.settings-nav-mobile` 只有 5 个 section 锚点、**没有任何检测入口**
⇒ <768px 用户无法测试 OCR/LLM 连通性。⚠️ 该缺陷对既有 e2e **不可见** —— `ui_e2e.py` 是在
**桌面视口**下点 `#test-conn-btn` 的：「元素存在」≠「元素可达」。
修法：① `templates/settings.html` 加 `md:hidden` 的 `#test-conn-btn-mobile`；
② `static/settings.js` 把内联 handler 提为具名 `runConnectionTests(btn, scrollToMessage)`，
两入口**共用同一实现**；③ 移动入口触发后把结果（页面底部 `#save-msg`）**滚动到可视区**。
⚠️ 重构**不重打逻辑**：新函数体由旧 handler **逐行 dedent 2 格生成**，并由
`devlogs/_verify/r87e_refactor_lossless.py` 机检**旧逻辑零丢失**（64 行 → 64 行，集合差为空）。

**护栏 + 变异**

  · `tests/unit/test_concurrency_default_claims.py`（**9 条**）：默认值从
    `api/jobs/__init__.py` **重算**（不写死字面量）；活文件集合内不得出现与代码不一致的
    默认值；README 表形态 / PORTABLE_README **跨行**形态各自单独钉住；反空转 +
    「已否证的序数叙述不得回流」；并立下约定 **`=N` 只留给覆盖示例**（该歧义是首跑在
    `PORTABLE_README.txt` 上**假红**后定位的）。
    变异 `devlogs/_verify/r87c_mutation.py` ⇒ **基线绿 + 7/7 CAUGHT**。
  · `tests/unit/test_settings_mobile_test_conn.py`（**5 条**）：声明 == 接线（双向）；
    入口**不止一个**；至少一个落在 `md:hidden` 内；桌面入口确实在 `hidden md:flex` 内
    （记录**为什么**需要第二个）；检测逻辑**只有一份**。
    变异 `devlogs/_verify/r87f_mutation_mobile.py` ⇒ **基线绿 + 5/5 CAUGHT**。
  · `tests/unit/test_todo_freshness.py` 新增**不变式 H**：CHANGELOG 的 `第N批` 标题必须用
    `Round <N>` 形态。**R86 的标题误用了 `R86`**（另一套编号体系），而新鲜度检查只认
    `Round NN` ⇒ **静默失效**（实际最新已是 Round 82，戳却仍是 81）。已改正标题形态，
    本轮把戳推进到 Round 83。

**0-34 复核：R86 的延后判断**正确**，且本轮找到了它的**真实机制**（仍未结）**
R86 以「入包源 +1」为由把 `electron/main.js:13` 的 `30s` 注释延后到重建时处理。
本轮曾按「两份产物本就失败 ⇒ 延长清单零边际成本」改掉它，**全量测试当场判红**：
`tests/unit/test_bundle_manifest.py::TestAsarReader::test_real_asar_matches_source_for_electron_main`
把 `app.asar` 里的 `electron/main.js` **逐字节**对比源码 ⇒ **改源码必然让它红**，直到
Electron 产物重建（实测 `1 failed, 4101 passed, 4 skipped`）。⇒ **R86 的延后是对的，
理由比它写的更硬**（不是"清单 +1"，而是"有一条单测专门盯这个文件"）。已**回退**该改动；
0-34 **保持开放**，条目里补上这条实测证据。

**实测（门禁 10/11）**：`tests_coverage` = **4102 passed, 0 failed, coverage 95.09%**
（门禁 95%，junit `devlogs/gate_junit_20261010_151443.xml`）；变异 **7/7 + 5/5 CAUGHT**。
唯一红 = `artifact_freshness`（见下）。报告 `devlogs/gate_report_20261010_151405.json`。

**⚠️ 本轮再次改动入包源**（`api/jobs/listings.py` / `core/procpool.py` /
`static/upload.js` / `templates/settings.html` / `static/settings.js`）⇒ 现有产物
**必然过期**。`artifact_freshness` 的失效面由 **3 → 8 个**文件（3 个既有 +
本轮 5 个，**以门禁实测为准**）；分发前**必须重建**（需宿主退出）。

### 分发/UX 评估 + 首启体验纠偏（Round 82 第二十六批，2026-10-10）

> 记录 → `docs/DISTRIBUTION_UX_ASSESSMENT_2026-10-10.md`（新增）。
> 本轮回答用户五问：能否分发/作基线、距分发缺口、进度总结、前端设置与首启体验、UX 改善。

**评估结论（有证据）**：作为**便携目录包 / 单机单用户桌面工具**，D-Gate 六条不变量
**无阻断项**（D1–D4 ✅；D5 = 唯一产物与自证出处成立但**产物已过期**、D6 = 残余具名登记，
均 🟡）。最新门禁 **10/11**（`devlogs/gate_report_20261010_140022.json`）—— 唯一红 =
`artifact_freshness`，**指名**本轮改动的 3 个**入包源**（见下）；其余为绿：依赖 **0** 公告 /
Electron **43.7.4** 在支持线 / **1 份完整产物**。⚠️ 本轮改了**入包源** ⇒ 现有产物**已过期**，
分发前**必须重建**（需宿主退出）。距"分发"另缺：`LICENSE` 文件、**分发形态决策**、
判定质量**无标注集**。

**修复 1 · 首启 OCR 引导（真实摩擦点）**：上传页的首启横幅此前**只看 LLM**
（`main.py::index` 的 `needs_setup`），而 PDF **无 OCR 配置必然在 Stage 1 失败** ⇒ 用户先
白传大文件（实测样本 138MB / 51 页）才拿到中途错误。修法：
`core/pipeline/ocr_support.ocr_chain_configured()`（**复用**既有 `remote_backend_configured`
/ `is_backend_available` —— 单一来源、不发网络探测）+ `main.py::index` 下发
`ocr_setup_needed` + `templates/upload.html` 第二横幅。
⚠️ **不改上传拒绝语义**（后端仍只对无 LLM 返回 400）—— 把 OCR 也做成后端前置门属
**语义变更**，会波及产物级入口矩阵 `tests/e2e_rejections.py`（34 例的 seed 与期望需重验）
⇒ **登记为待办**，不在本轮擅自改。

**修复 2 · 首启文档纠偏（用户解压后第一份会读的文档）**：`PORTABLE_README.txt` 写
「并发处理 **3** 个任务」（代码默认已是 **5**）、「缺页超过 **20%**」（代码是
`missing > max(2, int(pdf_total * 0.1))` ⇒ **10%**）、「可在设置页面调整 LLM 并发数」
（**设置页无此入口**；页级并发是 env `LLM_CONCURRENCY`、任务级是 env
`MAX_CONCURRENT_JOBS`）⇒ 三处纠正；`README.md` 配置表 `max_concurrent_jobs` **3 → 5**
并改标为 env（`MAX_CONCURRENT_JOBS`），补 `LLM_CONCURRENCY` 行。

**护栏 + 变异**：
`tests/unit/test_distribution_parity.py::test_portable_readme_concurrency_matches_code_default`
（文档数字 == **从源码重算**的默认值 —— no-op 检验，防再次静默腐坏）；
`tests/integration/test_main_routes.py::TestIndexOcrGuidance`（**行为**双向：未配置→有横幅 /
已配置→无横幅，防"一刀切恒显示"式假绿）。
变异 `devlogs/_verify/r86d_mutation.py`：**基线绿 + 4/4 CAUGHT**
（M1 文档数 5→3 / M2 谓词恒不配置 / M3 模板恒显示 / M4 路由恒隐藏）。

**修复 3 · 随包文档数字复审（用户第一份会读的文档）**：`PORTABLE_README.txt` 长期游离在
机检之外（版本戳 / 并发数有护栏，其余数字没有）。逐条实测查出 4 处与代码**相反**或**无出处**：
①只写「PDF 最大 200MB」而**漏了页数上限**（真值 `UPLOAD_LIMITS = {max_bytes: 200MB,`
`max_pages: 200, warn_pages: 80}`；config 注释明确「两者正交：体积上限挡巨文件，页数上限挡
低密度但极长的 PDF」）；②「启动超时（>30秒）」而 `electron/main.js` 的
`READY_TIMEOUT_MS = 180_000`；③「51 页…约 10-15 分钟」而本仓实测全链 **1,889,115 ms ≈ 31.5
分钟**、单一真值 `sec_per_page_total = 32.7 s/页` ⇒ **约 28 分钟**（差 2~3 倍 ⇒ 用户会在第
20 分钟以为程序卡死）；④「DeepSeek 默认 50 RPM」**全仓无出处**（grep 只命中它自己），
且「请联系技术支持下调并发」是**死指引**（无联系方式、并发是 env-only）⇒ 改为给出
`set MAX_CONCURRENT_JOBS=2 && set LLM_CONCURRENCY=2` 实例。另把 UI 实际提供的
OpenAI / 自定义补进「可添加」列表。

**护栏（+4 条，全部从源码重算）**：页数上限 == `UPLOAD_LIMITS`；启动等待上限 ==
`READY_TIMEOUT_MS/1000`（解析 `electron/main.js`）；「秒/页」== `sec_per_page_total`，
且「N 页约 M 分钟」由同一常量复算（示例页数**从文档自身解析** ⇒ 改示例也受检）；
被否证说法（`10-15 分钟` / `50 RPM` / `>30秒`）不得回流。另把 `PORTABLE_README.txt`
纳入 `test_upload_limits._iter_text_files()` 的第三方数字扫描范围 —— 它此前**不在**扫描内，
正是「50 RPM」得以存活的空档。变异 `devlogs/_verify/r86r_portable_mutation.py`：
**基线绿 + 6/6 CAUGHT**（含既有并发数护栏的回归）。
`PORTABLE_README.txt` 与 `tests/` 均**不入包** ⇒ **不新增**产物失效面。

**修复 4 · DEPLOYMENT.md 事实性断言复审（5 处）**：该文件不入包，但其中两条会被当成
事实引用 —— **数据保留**与**安全描述**。①「压缩后的样式（~14KB）」而 `static/app.css`
= **20,016 字节**（顺带实测：重跑 `npm run build:css` 后 sha256 **不变** ⇒ CSS 不过期）；
②「上传文件…（**OCR 完成后自动清理**）」**与实现相反** —— `core/` 内没有任何删除已上传
PDF 的逻辑，删除只发生在 `api/jobs/actions.py::delete_job`，且**必须保留**（复核页逐页
渲染依赖它）⇒ 这是**合规相关**的错误陈述；③「有 4 个日志文件」而实为 3 个 + 控制台；
④ `/health` 示例硬编码 `"version":"1.0.0"` ⇒ 改为引用 `main.APP_VERSION`；
⑤ 上传限额只写 200MB、漏页数上限。
护栏 `tests/unit/test_deployment_doc_claims.py`（5 条）：日志轮转表逐文件 ==
`logging_config.py`（解析 `RotatingFileHandler` 重算）；app.css 体积声明与实物同量级
（±30%）；保留策略如实 + 正向对照（`delete_job` 的 `rmtree` 真实存在）+ 禁回流旧说法；
`core/` 不得出现"删掉上传 PDF"的逻辑（同行含 `pdf_path` + 删除调用）；`# →` 示例输出
不得硬编码版本号。变异 `devlogs/_verify/r86v_deployment_mutation.py`：**基线绿 +
5/5 CAUGHT + 负控 1/1 未误伤**。

**新踩坑（已记入 `docs/PROJECT_PITFALLS.md` §六十）**：`git checkout -- <入包源>` 会按
`core.autocrlf=true` 把 LF **改写成 CRLF** ⇒ 清单（**原始字节** sha256）失配 ⇒
`artifact_freshness` 多报一个"未重建"的文件，而 `git diff` / `git status` **看不到**这次
内容变化（被规范化掩盖）。本轮实测：变异 harness 在 `core/mineru_client.py` 留下锚点后
用 `git checkout` 还原，门禁随即把该文件列为第 **4** 个失配源；按 `git show HEAD:<path>`
的**原始字节**写回（纯 LF、57,331 B）并复跑门禁（`--skip-tests`）后回到 **3** 项。
⇒ 还原入包源**不要**用 `git checkout`；且 harness 的还原基准必须在改动**之前**快照。

**测试**：全量 pytest **4085 passed / 0 failed**（4 skipped；较本批首测 4074 **+11** =
新增护栏用例）。门禁 **10/11**（`devlogs/gate_report_20261010_144342.json`）：唯一红 =
`artifact_freshness`，**指名**本轮改动的 3 个**入包源**（`core/pipeline/ocr_support.py` /
`main.py` / `templates/upload.html`）⇒ **重建即转绿**；其余 10 项（worktree_clean /
1 份完整产物 / coverage **95.09%** / 依赖 **0** 公告 / Electron 43.7.4 在支持线 …）全绿。
⚠️ 本轮改了入包源（`main.py` / `core/pipeline/ocr_support.py` / `templates/upload.html`）
⇒ 门禁 `artifact_freshness` 在**重建前必红**（**设计如此，非回归**）。

**踩坑（已记入 `docs/PROJECT_PITFALLS.md` §五十九）**：插入式改写的**锚点**是结果的
**前缀** ⇒ 用"old 还在不在"判幂等会**重复插入**（实测 `main.py` / `templates/upload.html`
各多出一整份，已回滚）；正确判据是"**插入后才会出现的 marker**"。

**未完成 / 阻塞**：本地 `main` 领先基线 `dd108b2` 的提交**尚未推送**（撰写时为 11 个，
用 `git rev-list --count dd108b2..HEAD` 自算 —— 该数**随提交增长**，故不写死），卡在
**缺 GitHub 凭据**（`could not read Username`，**非**代理 —— 7897 可达）；
产物重建需**宿主退出**。

### 判定产出**采样温度未固定** ⇒ 同输入两次跑 findings 差 +24%；并发额度默认 3 → 5；日志第三方噪声抑制名单补全（Round 81 第二十五批，2026-10-10）

> 记录 → `docs/TODO.md` 的 **0-15**（核销）与 **0-27**（新增）（属当前 backlog ⇒ 就地记 §0 表）。

**定位（0-15）**：全仓**唯一**随机源是 LLM 采样 —— 页面分析（`core/page_analyzer.py`
两处）与跨页语义检查（`core/rules/llm_checks.py` 两处）都传 `temperature=0.1`。
`temperature > 0` 即引入采样随机性，逐页累积成整份记录的 findings 条数漂移
（B6-1 实测：同输入 / 同产物 / 同后端 / 同模型，**41 vs 51**，+24%）。其余“疑似随机源”
经核查**不成立**：OCR 客户端**不发** `temperature`/`seed`（服务端固定）；`datetime.now()`
只注入“今天日期”（同日恒定）；`rule_time`/`year_vote` 的年份上界是**跨年**敏感
（设计如此），**不解释**同日 41 vs 51。

**修法**：新增单一真值 `llm.client.DETERMINISTIC_TEMPERATURE = 0.0`，四处调用点改用它。
greedy 解码（0.0）是结构化抽取 / 合规判定的标准做法：既去掉运行间方差，也让输出落在
模型最高概率路径上。**不**引入 `seed` —— 网关可能拒绝该参数 ⇒ 给调用链加一条 400 分支，
收益不确定。

**并发额度（0-27）**：`api/jobs._MAX_CONCURRENT_JOBS` 默认 **3 → 5**（支持 5 个文件并发
审核；env `MAX_CONCURRENT_JOBS` 仍可覆盖）。原注释称“3 并发 200MB PDF 可达 ~2GB”
**归因有误**（实测单 job ≈ 102MB，见 `.workbuddy-ai/memory/MEMORY-DETAIL.md` §7），已更正。

**护栏**：`tests/unit/test_llm_determinism.py`（4 条：常量 / `analyze_page` **行为**断言 /
`_llm_fallback_check` **行为**断言 / 源码扫描“判定产出模块不得再出现裸 temperature 数字”）；
`tests/unit/test_upload_quota_contract.py::TestDefaultQuotaSupportsFiveConcurrentReviews`
（默认 ≥ 5 且**兜底分支** ≥ 5）。

**变异**：`devlogs/_verify/r85_mutation.py` ⇒ **5/5 CAUGHT**（M1 页面温度回 0.1 /
M2 跨页温度回 0.1 / M3 常量改 0.1 / M4 默认回 3 / M5 兜底回 3）+ 基线绿。
⚠️ **M3 是必需项**：只把常量改成 0.1 时，两条**行为**断言仍绿（调用方与常量一起变）
⇒ 只有 `test_constant_is_zero` 能抓 —— 又一个“用例存在 ≠ 断言有效”的现场。

**未验证边界**：0.0 消除的是**客户端采样**这一已知随机源；**服务端**残余方差
（批处理 / MoE 路由 / 浮点非结合）**未测** ⇒ **不宣称“非确定性已消除”**。
本批同时改了两处**入包源**（`core/`、`llm/`）⇒ **必须重建产物**。

**日志噪声（0-28）**：`logging_config._NOISY_THIRD_PARTY_LOGGERS`（#145 的逐库降噪名单）
**漏掉了实测刷屏的两个库** ⇒ 有效级别仍是 `DEBUG`（继承 root），#145 的目标未真正达成：

- `aiosqlite` —— 历史累计 **8,116 行**（每条 SQL 一行 DEBUG），全仓第一大噪声源；
- `chardet` —— `logs/pharma.log` **当天 613 行日志的 100%**（`chardet.charsetprober`
  逐候选编码打印置信度）；它是 `requests` 的**传递依赖**，故不在直接依赖清单里。

**修法**：把两者加进名单（`setup_logging` 已按名单逐库 `setLevel(WARNING)`）。
**护栏** `tests/unit/test_logging_config.py::TestThirdPartyNoiseSuppression`（3 条：
名单完整性 / `setup_logging` **行为**断言 / 子 logger `chardet.charsetprober` 继承抑制）。
**变异** M6–M9 ⇒ CAUGHT（累计 **9/9**）。
⚠️ 该行为断言**必须先清到 NOTSET** —— `logging` 的 logger 是**全局单例**，前面用例调过
`setup_logging` 会把级别留在 WARNING，否则删除抑制循环也不会红（**变异漏检**，
M8 实测暴露）。
⚠️ 本批同时改了**入包源**（`logging_config.py`）⇒ 与上文一并**重建产物**。

**受控重放实测（⚠️ 更新上文的「未验证边界」）**：把同一 (system, user) 在本上游
（SiliconFlow / `deepseek-ai/DeepSeek-V3.2`）重放 —— page 4（n=6）
`0.0 → [2,2,3,2,2,2]`、`0.1 → [3,2,3,2,2,2]`（**两者 spread 均 1 = 50%**）；
page 2（n=3）`0.1 → [3,3,4]`、`0.0 → [3,4,3]`（均 spread 1）。⇒ **固定
`temperature=0.0` 不足以获得可复现的 findings 计数**，残余方差来自上游。
故：**单次计数依然不得当回归判据**（0-15 判据**加强**）；要数字须 N 次重复跑给区间
（新增 **0-29**）。⚠️ 样本小（n=3–6/格、2 页）⇒ 只断言"**存在**"，未给量级。
**R85 第二十五批（2026-10-10）量级实测（n=8 × 3 页 × 2 温度 = 48 次受控重放）**：同一 (system, user) 下
`temperature=0.0` 在 **3/3 页**均非复现（spread=1，rel **33.3%–50%**），单页**绝对**
摆幅上界 **= 1 条**（计数区间 2–4 ⇒ rel% 偏大是**分母小**，非大摆幅）；**类型集合亦变**
（page 2 @0.0 的 8 次里出现 **4 种**不同 type set）。**反证温度非主因**：
`temperature=0.1` 的方差**不高于** 0.0（page 36 @0.1 **8/8 恒定 = 2**）⇒ 方差源于
**上游**，客户端调温度**消不掉**。统计脚本 `devlogs/_verify/r85_temp_probe_stats.py`
（数字**全脚本算出**），原始日志 `r85_temp_probe_run2.log`。⚠️ 样本仍小（3 页 /
1 个 51 页 job）⇒ 给的是**该量级**，**不**外推为全库分布；`temperature=0.0`
是否被网关原样透传**仍未验证**。


**e2e 前置条件（0-30）**：`tests/e2e_rejections.py` 有**静默前置条件** —— 产物未配置
真实 LLM provider 时 `api/jobs/upload.py` 对**每个**上传返回 400 ⇒ 矩阵里"期望 400"
的用例**全部假绿**、阳性用例全红 ⇒ 表面 **13/34** 却**零判别力**。已加**前置 fail-fast**
（读 `GET /api/settings` 的 `llm.providers[].configured`），并做**正/负控双向验证**
（有 config ⇒ rc=0 / 34/34；无 config ⇒ rc=2 + 明确提示）。⚠️ 首版按顶层 `providers`
取值 ⇒ 恒判 False（**只有正控抓到**）—— 又一次"用例存在 ≠ 断言有效"。
**产物级实测（R85 收尾，全部在新产物 `sha256[:12]=30bac4437653` 上）**：
① 门禁 **11/11**（worktree_clean / 产物与源码**逐字节一致** / **4070 passed 0 failed** /
coverage **95.09%** / 依赖 **0** 公告 / Electron 43.7.4 在支持线）；
② 冻结产物多轮 e2e（`pdf,img,mineru,rot,robust,cancel,dual`）⇒ **ALL ROUNDS PASSED**（RC=0）；
③ **5 文件并发 + UI**（`devlogs/_verify/r85_five_concurrency.py`，走**默认**额度路径）⇒ **7/7**：
UI `/` + `/static/app.css` + `/api/jobs` 均 200（css 与磁盘**逐字节一致**）、并发 5 份全 **200**
（0.2s）且拿到 5 个 job_id、第 6 份 **409**（拒绝而非排队）、5 个 job 均离开 `pending` 进入
`ocr_running`。⚠️ 只验**准入 + 起跑**；5 路**端到端**完成时延与上游速率影响**未测**（0-27）。
⚠️ 多轮 e2e 的 `pdf/img/mineru` **逐轮明细**因 harness `stdout[-4000:]` 截断**未落盘**
（仅汇总判决）；已改为**全量落盘**（`_r83b_multiround_full.out`）。

**降噪量测口径（0-31，核查后判定「非缺陷」）**：`data/pharma.db` 里有 **2,669** 条
`超出自动判定上限` 的**逐参数** completeness（20 个 job），而 `scripts/eval_noise_reduction.py`
在旧 job 上重放报 **-24.8%** ⇒ 易被误读成「当前线上降噪幅度」。**用当前产物取证**：e2e 库
（`%TEMP%/pbc_e2e_appdata/PBC/data.db`）该形态 **0 条**，且 `llm_checks.py:109-118` 超限分支
已 `aggregate=True`（单条汇总）⇒ **旧库那批是写入期降噪之前的遗留，当前无此缺陷**。
已给该脚本加**输出级口径披露**（按 `ocr_text` 前缀判 pre/post-降噪）：旧库显示「不得读成
当前线上降噪幅度」，当前库显示「写入期已降噪、增量≈0 属预期」。⚠️ 脚本**不入包**、**不计覆盖率**。

**5 路并发端到端（0-27 缺口关闭）**：`devlogs/_verify/r85_five_e2e.py` 在**产物**上把
5 份**内容互异**的 PDF **同时**上传（`?force=1` 绕内容去重、**不绕配额**）⇒ **9/9 达成**：
5/5 准入且 5 个互异 job_id；第 6 份 **409** 且理由是**配额**（「已有 5 个任务在处理中，上限为 5」）
—— ⚠️ 首版只看状态码，被**内容去重**的 409 蒙混（**假绿**），已改为断言**理由**；
**并发峰值 = 5**（同时非终态，证明**真并发**）；5/5 到 `review`、**0 error**；
存储层按 job 隔离（findings 逐 job `[1,1,1,18,25]`，合计 46 = 端点合计 46）；
**总墙钟 715s < 各 job 之和 1260s**（并行收益 1.76×）；无残留非终态 job。
⚠️ 5 个文件为**小样本**（1–6 页）⇒ **51 页大记录 ×5** 的上游速率影响**仍未测**。
**额度槽位回收（0-27 的另一半，产物级）**：`devlogs/_verify/r85h_slot_recycle.py` ⇒ **6/6** ——
占满 5 后第 6 份 **409 配额**（同轮**负控**）；`cancel` 一个 ⇒ 该 job 进 `cancelled`，
**其余 4/4 仍在跑**时重传第 6 份即 **200** 且为**新 job_id** ⇒ 额度**动态回收**
（无需等全部跑完）。此前只钉了「占满 ⇒ 409」这**一半**，回收方向**无任何断言**。
永久护栏 `TestQuotaSlotIsReleasedWhenAJobReachesTerminal`（自带同轮负控）；
变异 `devlogs/_verify/r85i_quota_recycle_mutation.py` ⇒ **3/3**（M0 绿 / M1·M2 CAUGHT）。复跑**全量门禁** ⇒ **11/11**（4071 passed / 0 failed / coverage **95.09%**；两份产物仍**逐字节新鲜**）。

**大记录 ×5 并发（0-27 最后一个缺口关闭）**：`devlogs/_verify/r85k_bigfive_e2e.py` 把 **51 页 / 138MB** 真记录**同时**跑 5 份 ⇒ **6/6**：5×200 准入（各 8–9s）、**并发峰值 5**、**5/5 全 51 页分析完**、**0 error**、全部到 `review`，**总墙钟 2579s**（预算 3600s 内）。⚠️ **1 次 cross-page LLM 超时**（上游抖动）⇒ 产品**按设计降级并披露**（该 job 带 `completeness`「跨页 LLM 语义检查不可用…需人工复核」finding）⇒ 判据修正为「**失败必须披露**」（首版断言「永不失败」是判据错，已修；事后核验脚本 `r85k_verify_bigfive.py`）。**同输入 5 次 findings = 199/203/204/217/220**（spread **21**，rel **≈10.6%**）⇒ 在真实规模上再次印证 0-29「单次计数不可复现」。

**硬崩溃恢复（产物级，0-32）**：`devlogs/_verify/r85m_crash_recovery.py` ⇒ **5/5** ——
把服务在流水线**跑到一半**（5/5 `ocr_running`）**硬杀**（`taskkill /T /F`，不走
lifespan 收尾），重启**同一产物** ⇒ 5/5 收敛为 `error`（**4s**）；
`stuck_recovery` 审计 **11 → 16（+5）**；重启后**新上传 200** ⇒
**额度未被残留非终态 job 吃掉**（否则用户被永久锁死）。此前该恢复路径**只有单测**
（lifespan 分支还是 mock 的日志）⇒ 产物级**零覆盖**。



### 报告缓存 key 未覆盖**全部渲染输入** ⇒ 重试后 `report.md` 静默返回过期内容（Round 80 第二十四批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-26**（属当前 backlog ⇒ 就地记 §0 表）。

**定位**：`api/report.py::_generate_report_md_cached` 用模块级 `cache_key` 做报告缓存，
旧 key = `(job_id, len(findings), last_id, status_hash, len(exemptions))`。但
`_generate_markdown` 还消费 **`total_pages` / `empty_pages` / `unanalyzed_pages` /
`job` 级字段（`filename`）** —— 这些**都不在** key 里。

**风险**：这些输入能在 `findings` **完全不变**时改变 ⇒ key 不变 ⇒ 报告返回旧文本。
可达路径：job 终态含 1 页未完成分析且 0 findings，用户 `retry`（`status != 'review'`
⇒ 不清 findings、不重置 `structured_json`）⇒ Stage 2 重新分析该页成功 ⇒
`unanalyzed_pages` 1 → 0、findings 仍 0 ⇒ 报告仍声明「1 页未完成分析」
（GMP 场景下携带**过期的覆盖声明 / 页数**）。与已修的 **P1-A**（同状态二次修正后
报告仍返回旧文本）**同源**：缓存 key 未覆盖全部渲染输入。

**修法**：改为对**恰好传给 `_generate_markdown` 的输入**取 SHA-256 摘要
（`_render_input_digest`）—— 任何输入变化都使 key 失效；P1-A 的
`corrected_text`/`reviewer_note` 教训由「整体 findings 摘要」天然覆盖。

**护栏**：新增 `tests/integration/test_report_cache_coverage.py`（**3 条**，逐输入
判别力：`unanalyzed_pages` / `total_pages` / `empty_pages`）。**变异**
`devlogs/_verify/r83c_mutation.py` ⇒ **4/4 CAUGHT**（M1 回旧 key / M2 去
`unanalyzed_pages` / M3 去 `empty_pages` / M4 去 `total_pages`，逐例红在**目标断言**）。
⚠️ M4 首版只删一处 `total_pages` 实测 **MISSED** —— 因为 `job["total_pages"]` 与裸
`total_pages,` **携带同一个值**，属**变异自身无效**（非测试失效），已改为两处同删。

### `esc` 转义原语：三份副本**行为等价**上锁 + 修正误导注释（Round 79 第二十三批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-25**（属当前 backlog ⇒ 就地记 §0 表）。

**定位**：`static/*.js` 有 **38 个 `innerHTML` 赋值点**，其安全**全部**依赖 `esc()` 纪律
（`tests/unit/test_innerhtml_escaping.py` 机检）。但 `esc` 本身在**三个页面 bundle 里各有
一份独立实现**：

| 文件 | 形态 | 页面 |
|---|---|---|
| `findings-map.js` | `function esc(s)`（导出 `PbcFindingsMap.esc`） | review |
| `settings-state.js` | `const esc = (s) =>` | settings |
| `upload-jobs.js` | `function esc(s)` | upload |

而**没有任何机检**保证三份一致 —— 且 `findings-map.js` 的注释写着「esc 曾有 6 份副本，
**收敛到这里**」，读者会以为已单一真值（**实际还有 3 份**）。

**风险**：日后只给一份补转义（例如属性上下文要额外转反引号 / `=`），另两份**静默**留在
较弱版本 ⇒ 那两页的 `innerHTML` 站点重新开洞。这正是「同一语义写两处必然漂移」。

**为什么不直接合并**：三份并存是**结构性**的 —— `templates/*.html` 已核，settings 页与
upload 页**不**加载 `findings-map.js`，仓里没有公共基座脚本。合并需新增公共脚本并改 3 个
模板的 `<script>`（改**加载语义**：某页漏引则该页 `esc` 变 `undefined` ⇒ 运行期炸）。
故本批**先把「三份必须等价」机检锁住**，真单一真值登记为 **0-25 待决策**。

**修法**：

- 新增 `tests/unit/test_esc_single_source.py`（**6 条**）：① 副本集合**恰好**等于登记
  （新增/消失都红）；② 每份替换链**逐项按序**等于规范 5 对（`&` 最先）；③ 三份互相等价；
  ④ 反空转（链长 5 + 提取器非空）；⑤ 转义器产出实体 **⊆** 解码器可解实体；⑥ 解码器
  `&amp;` 必须**最后**（`&amp;lt;` 二次解码的顺序陷阱）。
- `static/findings-map.js` 注释改为**如实**陈述（3 份 / 为什么 / 指向机检与 0-25）。
- 变异 `devlogs/_verify/r83_mutation.py` ⇒ **4/4 CAUGHT**。

⚠️ **改了入包源**（`static/findings-map.js`）⇒ **必须重建产物**，否则 `artifact_freshness`
逐字节比对判红（这是门禁**正确**的行为，不是误报）。

**边界**：锁的是**文本等价**；这个原语是纯字面量替换链（无控制流、无外部依赖）⇒ 链相同
即行为相同。跨 bundle 的**真**单一真值未做（见 0-25）。

### 载荷顺序不变式：把「先剥离、后判保留名」钉住（Round 78 第二十二批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-24**（属当前 backlog ⇒ 就地记 §0 表）。

**定位**：`api/jobs/upload.py` 的保留设备名守卫（R80 加）依赖一个**顺序**：

```
safe_name0 = Path(file.filename).name      # ① 剥离
if _is_reserved_device_name(safe_name0):   # ② 判定
```

若 ② 早于 ①，`../../NUL.pdf` 的 stem 是 `../../NUL`（∉ `_WIN_RESERVED_STEMS`）⇒ **绕过** ⇒ `pdf_path = job_dir / "NUL.pdf"` 仍落到 `NUL` 设备（0 字节静默丢失、用户看到误导性的 400「过小」）。而 R80 的 24 例真值表**只喂裸名**（`NUL.pdf` / `nul` / `CON..x` …）⇒ 顺序反了它**全绿** ⇒ 这是一个**载荷但未锁**的不变式。

**先说清楚：当前实现是正确的。** 真产物实测 **9/9**（`devlogs/_verify/r82_prefixed_reserved_probe.py`：6 例带前缀设备名 400 + 3 例带前缀阴性对照 200）。本批**不是**修 bug，是**把不变式钉住** —— 免得日后一次「顺手重构」把它悄悄改坏，而单测仍然全绿。

**修法**（三层）：

- **单测**：`tests/unit/test_upload_reserved_names.py` 新增 `TestEndpointStripsBeforeChecking` —— 6 例带前缀设备名 ⇒ **400 + 点名 + 回显剥离后的名字**；2 例带前缀阴性对照 ⇒ **200**。反斜杠 / 盘符 / UNC 三例标 `skipif(sys.platform != "win32")`（只有 Windows 把它们当分隔符）。
- **产物级 e2e**：`tests/e2e_rejections.py` 的矩阵 **26 → 34 例**（+6 带前缀设备名、+2 阴性对照）⇒ **34/34**（连跑两次）。
- **变异**：`devlogs/_verify/r82_mutation.py` ⇒ **3/3 CAUGHT**（M1 判原始名 / M2 不剥离 / M3 守卫过宽）。**关键点**：三条变异下**旧用例保持绿** ⇒ 新用例是**唯一**判别力来源（这正是「载荷但未锁」的证明）。

**边界**：真值表仍是**枚举**而非穷举；Windows 上标数字变体（`COM¹`）未覆盖（无本机实测）。

### 产物级**入口拒绝矩阵**：把 6 类入口守卫从「进程内单测」补到「冻结字节」（Round 77 第二十一批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-23**（属当前 backlog ⇒ 就地记 §0 表）。

**定位**：`devlogs/_verify/smoke_packaged_server.py` 只打 `/health` —— 产物层**零**拒绝路径覆盖。R80 新加的保留设备名守卫，以及既有的扩展名白名单 / magic bytes / 尺寸下限 / 路径剥离 / 本机请求守卫，**全部**只在 `ASGITransport`（**进程内**、源码树）层被验过。冻结会改变 multipart 解析、异常文案编码、状态码传播与 `frozenset` 常量的行为，而**单测绿 ≠ 产物绿**。

**修法**：新增**已提交**驱动 `tests/e2e_rejections.py`（刻意放 `tests/` 而非 `devlogs/` —— 后者被 gitignore，放那里的证据会随磁盘消失）。起真产物（`dist/pbc-server/pbc-server.exe`，与 electron-builder 内嵌那份**同 sha256**），用真 HTTP 打 16 例矩阵：

- 保留设备名 5 例（`NUL.pdf` / `CON.pdf` / `COM1.pdf` / `LPT1.pdf` / `NUL .pdf`）⇒ **400 + 点名「保留设备名」+ 回显文件名**；
- **阴性对照 5 例**（`null.pdf` / `console.pdf` / `com10.pdf` / `com0.pdf` / `auxiliary.pdf`）⇒ **200** —— 证明守卫**没有**写成前缀匹配而误伤；
- `evil.exe`（内容是合法 PDF）⇒ 400 扩展名；`fake.pdf`（文本内容）⇒ 400 magic；`tiny.pdf`（2 字节）⇒ 400 过小；`../../evil.pdf` ⇒ **200 且回显 `evil.pdf`**（路径剥离生效、穿越不成立）；外来 `Origin` ⇒ **403**、本机 `Origin` ⇒ **200**。

**结果**：**16/16 达成**（证据 `devlogs/e2e_rejections.json`）。每个被接受的用例**立刻 cancel**（1 页件；取消发生在 OCR 阶段 ⇒ 不烧 LLM 额度）；每个用例用**内容唯一**的 PDF（否则 md5 去重返回 409，会把对照读成失败）。

**边界**：矩阵是**枚举**而非穷举 —— 只覆盖上表 6 类，**不**证明「没有其他绕过」；**未**覆盖超大文件（会真的占盘）。


**追加（同日）：敌意文件名 + 去重 / `force` —— 矩阵扩到 26 例。**

① **敌意文件名**（`../../` / `..\..\` / `C:\Windows\` / `/etc/` / UNC / `..%2f..%2f` / `....//` / 盘符相对 `C:x.pdf`）—— 内容合法即须 **200**，且回显的 `filename` **不含** `/` `\` `:`（即**落盘路径不可由客户端控制**）。实测 `%2f` **不**被解码（回显原样 `..%2f..%2f…`）⇒ **不存在「双重解码」**这条绕过。
② **去重 / `force`**：同一份内容第 2 次 ⇒ **409**；`force=1` ⇒ **200**。
③ 全组追加一条硬判据：**任何用例都不得 5xx**（用户输入问题报 5xx 一律是缺陷）。

⇒ **26/26 达成**（拒绝矩阵 15 + 敌意文件名 8 + 去重 3）。

**可重跑 —— 本轮修掉的一处自伤**：`_unique_pdf` 原先只靠 `fitz` 每次生成随机 `/ID` 来获得「内容唯一」，那是**静默**依赖 —— 换成静态 fixture 就会撞上服务端的 md5 去重（**409**），把阴性对照读成失败，而且**只有换台机器才炸**。已改为**嵌入 `uuid4` nonce**（唯一性**由构造保证**）+ 起跑**防空转自检**（两次生成必须不同）。实测**连跑两次均 26/26**、`--verify-only` 重判亦 26/26。

### 依赖漏洞快照**刷新**（Round 77 续，2026-10-09）

`docs/DEPENDENCY_AUDIT.json` 的 `generated_at` 停在 **2026-09-23**（16 天）。门禁判据是「**当前锁定** vs 快照」，快照越旧，「0 条公告」这句话就越不可信 —— 而它正是「安全」结论的载体。经代理（`HTTPS_PROXY=http://127.0.0.1:7897`）重跑 `pip-audit -r requirements.txt --format json` ⇒ **0 条公告**（43 个条目，rc=0），快照 `generated_at` 更新为当天。**唯一变化是时间戳** ⇒ 结论未变，但「0 条公告」从「16 天前的说法」变成「**今天的说法**」。生成入口与代理用法见 `scripts/audit_deps.py` 的 docstring。
### Electron **fuses 缺省即默认**（三个危险开关全开）+ 上传文件名的 **Windows 保留设备名**（Round 76 第二十批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-21**（fuses）与 **0-22**（保留设备名）（均属当前 backlog ⇒ 就地记 §0 表）。
> 教训 → `docs/PROJECT_PITFALLS.md` §四十八、§四十九。

**定位（对抗性 recon，全部实测）**：

1. **Electron fuses 停在默认值。** `package.json` 的 `build` 段**没有** `electronFuses` ⇒ `app-builder-lib` 的 `doAddElectronFuses` 直接早退，产物保留 Electron 默认融合值 —— `RunAsNode` / `EnableNodeOptionsEnvironmentVariable` / `EnableNodeCliInspectArguments` **三个都是 ON**。实测后果：`ELECTRON_RUN_AS_NODE=1 BatchSentry.exe --version` 打印 **`v24.21.0`**（**Node** 版本 —— 产物被一个环境变量降级成纯 Node：不初始化 Chromium、不加载 `app.asar`）；宿主注入的 `NODE_OPTIONS` 被产物接收，Electron 打印 `Most NODE_OPTIONs are not supported in packaged apps`。⚠️ **这条在本机是活的**：宿主（WorkBuddy）自身就以 `ELECTRON_RUN_AS_NODE=1` 运行、且注入 `NODE_OPTIONS`，两者都**继承给子进程**。
2. **上传文件名可以是 Windows 保留设备名。** `safe_name = Path(file.filename).name` 只剥**路径分隔符**，不挡**设备名**。实测：`NUL.pdf` / `CON.pdf` 的 `open(..., "wb")` **成功但写入被丢弃**（落在设备上，不产生文件）⇒ 用户看到误导性的 400「不是有效的 PDF」；`COM1.pdf` / `AUX.pdf` / `LPT1.pdf` 的 `open` **直接抛** ⇒ 误导性的 500。对照 `null.pdf` 落 1100 字节（判别力）。

**修法**：

- `package.json` 加 `build.electronFuses{runAsNode:false, enableNodeOptionsEnvironmentVariable:false, enableNodeCliInspectArguments:false}`；**重建**产物（PyInstaller → `bundle_manifest` → electron-builder → `gen_provenance.py`）。**刻意不动**另外三个 fuse（`enableEmbeddedAsarIntegrityValidation` / `onlyLoadAppFromAsar` / `grantFileProtocolExtraPrivileges`）—— 它们改**启动语义**，而本环境**无法**验证 Electron 能否启动（受限沙箱跑不起 Electron GUI）⇒ 盲改有把产物改成「起不来」的风险，登记待真实终端。
- `api/jobs/upload.py` 加 `_is_reserved_device_name` + 入口守卫：命中即 **400 + 点名原因 + 回显文件名**（**不**静默改名 —— 那会让界面显示的 filename 与用户实际选的文件不一致）。

**证据（可复核）**：

- 产物 fuse wire 逐位复读（`devlogs/_verify/r80_electron_fuses.py`）：三个目标位 **OFF**、`count` 字节保持 `9`、**第 9 位（`WasmTrapHandlers`）未被改写**。
- **行为对照**：`ELECTRON_RUN_AS_NODE=1` 下改造前打印 `v24.21.0`，改造后**以 Electron 启动**；`NODE_OPTION` 报错计数 **1 → 0**。
- 两份 `pbc-server.exe`（`dist/pbc-server` 与 `dist-electron/win-unpacked/resources/pbc-server`）sha256 一致。
- 保留设备名探针 `devlogs/_verify/r80_nul_probe.py`。

**护栏 + 变异**：

- `tests/unit/test_electron_fuses.py`（**6 条**）—— 配置↔产物绑定：读**真二进制**的 fuse wire 逐位断言，另用**合成伪二进制**做正/负对照（证明读回器非空转）。
- `tests/unit/test_upload_reserved_names.py`（**4 组**）—— 24 条真值表（12 真 + 12 假阴性对照）+ 防空转 + 端点 400 点名 + **拒绝先于写盘** + 正常名阴性对照。
- 变异 `devlogs/_verify/r80_mutation.py` ⇒ **10/10 CAUGHT**（F1–F5 + U1–U5）+ 基线绿。
- ⚠️ 变异 harness **自身**踩到两个坑（已修，见 PITFALLS §四十九）：① harness 进程**自己**缺 safe-delete 阈值 ⇒ 清临时树时被 SIGTERM、管道缓冲全丢（表现为「无输出」）；② U1（关掉守卫）后端点用例会**读回 `CON` 设备而永久阻塞** ⇒ 给 harness 加**每例硬超时**（新 `HUNG` 判决：挂死 ≠ 通过），并让端点用例**不碰** `CON` / `PRN`（语义覆盖由纯函数真值表承担）。

**边界**：fuses 的**运行时**语义只在本机受限环境验到「产物能起来 + 不再被 `ELECTRON_RUN_AS_NODE` 降级成 Node」；**GUI 层**仍不可复现（见 TODO 0-2）。另三个 fuse 未改。
**产物级验证（本轮收尾）**：重建后对**内嵌产物**（`dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`，与 `dist/pbc-server` **同 sha256**）复跑 **7 轮** e2e（`pdf,img,mineru,rot,robust,cancel,dual`；`PBC_E2E_REQUIRE_LLM=1`；模型 `deepseek-ai/DeepSeek-V3.2`）⇒ **7/7 全部达成**、`llm_failed` 全 0（`pdf 8/8`、`img 2/2`、`mineru 7/7`、`rot 5/5`、`robust 4/4（复合）`、`dual 7/7`；`cancel` 按设计豁免）。证据 `devlogs/_verify/r78_e2e_postbuild.json`。门禁在提交 `fdf44fa` 上 **11/11 PASS**（`4045 passed, 0 failed, coverage 95.09%`）。


### `innerHTML` 转义纪律机检：把「不能静态证明安全」的站点改为**显式登记 + 陈旧检测**（Round 75 第十九批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-19**（属当前 backlog ⇒ 就地记 §0 表）。
> 教训 → `docs/PROJECT_PITFALLS.md` §四十七。

**定位**：前端 R63/R65 拆成模块后 `innerHTML` 达 **46 处**（38 个赋值点），**逐个依赖 `esc()` 纪律、却没有任何机检** —— 新加一处 `el.innerHTML = untrusted` 不会被任何东西拦下。`docs/RELEASE_READINESS_PLAN.md` §SEC-6 那行还曾误记「零命中」。

**修法**：新增 `tests/unit/test_innerhtml_escaping.py`（**6 条**）。判据**三层，逐层放宽、每层都显式**：

1. **纯字面量**（空串 / 字符串字面量 / 它们的 `+` 连接）⇒ 安全（无插值）；
2. **含已知转义器**（语句跨度内出现 `esc(` / `escapeHtml(`）⇒ 安全；
3. **受审计渲染器**（RHS 是**裸调用**且函数名在白名单里）—— 护栏**另断言**白名单里的函数在 `static/` 里**确有定义**，防「白名单指向一个不存在的函数」；
4. 以上都不满足 ⇒ 必须在 `_AUDITED` **显式登记**（文件 + 语句唯一子串 + 理由），且子串必须**恰好命中 1 处**（陈旧＝0、歧义＝>1，都判红）。

**为什么不是「每个 `${...}` 都必须含 `esc`」**：那会**误伤** —— 本仓大量插值是**数字**（页码/总数）、**字面量三元**（`atFirst ? "disabled" : ""`）或**已在上一行转义**的变量。把这类判红，护栏就会被关掉；一条会被关掉的护栏等于没有护栏。

**为什么扫描要过 tokenizer**：朴素正则会被**注释**骗（`// el.innerHTML = x` 不是站点），也会**漏看**转义器 —— 模板字面量 `` `...${esc(x)}...` `` 里的 `${}` **内部是代码**，把整段反引号当字符串就看不见 `esc(`，于是**误伤**真实站点。故 `_code_mask` 跟踪 `'` / `"` / 反引号 / `//` / `/* */` 与模板内 `${}` 的嵌套。

**防空转**：站点数有**下限**（当前 38 ⇒ 下限 30）；四种分类（literal/esc/renderer/audited）各自也必须**非空** —— 否则判据/扫描塌缩。

**变异**（`devlogs/_verify/r78_innerhtml_mutation.py`）：8 正向（丢唯一转义器 / 加裸站点 / 删登记 / 登记写错 / 摘白名单 / 白名单指向空 / 关转义器探测器 / 抬站点数下限）+ 2 阴性对照（注释里的伪站点、新增纯字面量站点）⇒ **10/10 达成**；且**逐例核对红在目标断言**（不是「临时树 import 炸了」那种假 CAUGHT）。

**边界**：这是**静态纪律机检**，证明「每个站点要么显式转义、要么显式登记」，**不**证明渲染出的 HTML 无注入（46 处**未逐处**判转义**充分性**）。

### 验证 harness 的「真凭据」来源被 SUT 自身覆写：负控污染 + 断言退化（Round 74 第十八批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-17**（属当前 backlog ⇒ 就地记 §0 表）。
> 教训 → 技能 `verification-integrity` Trap 92；`docs/PROJECT_PITFALLS.md` §四十六。

**定位**：`devlogs/_verify/r77_e2e_llm_audit_live.py::_creds()` 从 `%TEMP%/pbc_e2e_appdata/PBC/config.json` 读「真凭据」—— **但那正是 driver 自己会覆写的文件**（`POST /api/settings` 落盘）。r77 的**负控**把 `BAD_KEY` 写进去后没有撤销 ⇒ 「真凭据」位被污染。两个后果都**静默**：

1. **归因错位**：任何重跑的 A 正控会读到毒 key ⇒ `401` ⇒ 被读成产品缺陷（实为 harness 缺陷）；
2. **断言退化**：`assert sf_key in (real_sf, BAD_KEY)` 在 `real_sf == BAD_KEY` 时**恒真** ⇒ 恰好放过它要抓的污染。

**定位证据**（不是猜）：掩码显示 `…0000`，与 `BAD_KEY` 后缀同 → 精确比对 `config["SILICONFLOW_API_KEY"] == BAD_KEY` ⇒ **True**（len=44）。即 r77 的**存档证据仍有效**（A 正控当时用的是真 key），只是脚本**原先不可重跑**。

**修法**：① 数据面 —— 把真 key 字节还原进 `config.json`（`count==1` 字节替换），直连探针 `GET https://api.siliconflow.cn/v1/models` ⇒ **200**；② 脚本面 —— 加**污染金丝雀**（`_creds()` 起跑即断言 key 不是负控串）+ `_snapshot_cfg`/`_restore_cfg`（**只还原 SF key 那一处**，保留 driver 期间合法的 OCR 后端切换）。

**测试**：`devlogs/_verify/r78_harness_selftest.py`（金丝雀 + 外科式还原 + 幂等）；**变异** `r78_harness_mutation.py` ⇒ **4/4 CAUGHT** + 阴性对照绿；`--verify-only` 重判 r77 存档 ⇒ 仍「全部达成」。

**同批（验证）**：**多轮 e2e**（`devlogs/_verify/r78_e2e_multiround.py`；轮次 `pdf,img,mineru,rot,robust,cancel,dual`；`PBC_E2E_REQUIRE_LLM=1`；模型 `deepseek-ai/DeepSeek-V3.2`；产物 `dist/pbc-server/pbc-server.exe`，`sha256[:12]=46a589a2201d`，与 Electron 内嵌那份**同字节**）：`pdf` ok=True `llm_ok=7/7`；`img` ok=True `llm_ok=3/3`；`mineru` ok=True `llm_ok=9/9`；`rot` ok=True `llm_ok=5/5`；`robust` ok=True `llm_ok=4/4`；`cancel` ok=True（豁免轮，无 LLM 审计键）；`dual` ok=True `llm_ok=8/8`。driver rc=0（0 = 全轮通过）。

### e2e 多轮判据误伤「复合轮」（`robust`）：判据必须认得轮次形态（Round 74 第十八批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-20**（属当前 backlog ⇒ 就地记 §0 表）。

**定位**：`devlogs/_verify/r78_e2e_multiround.py` 首跑 7 轮 ⇒ **6/7**，唯一「未达成」是判据**自身误伤**：`robust` 轮**不是**单次 `run_upload`，而是**包了** `o6`/`o1` 两个 upload 并只把 `llm_*` 留在**子字典**里 ⇒ 只看顶层就报「结果字典缺 llm_calls（判据没接上？）」。这正是脚本 docstring 预告要暴露的风险（「按设计无 LLM 调用会被误判成红」）—— 本次真的暴露了，只是形态是「**有** LLM 调用但埋在子字典」，而非「没有」。

**修法**：① 新增 `_llm_audit()` —— 优先顶层；否则**下钻一层**取子轮聚合，`kind ∈ {flat, composite, none}`；② `all_pass` 取子轮**逻辑与**（**不是**求和）：求和会让 `0/2 + 2/2` 变成 `2/4` 看着「还行」，**掩盖**某一路零成功；③ `_judge` 改判 `all_pass`。**下钻不放行**：任何一层都没有 `llm_calls` ⇒ 仍判红。

**测试**：`devlogs/_verify/r78_judge_composite_selftest.py`（**13 检查**，含 5 条负控：`o6` 零成功 / 扁平轮缺 `llm_*` / 子轮全无 `llm_*` / `cancel` 带 `llm_*` / 三态判别非空转）。**重判**（`--verify-only`，从存档日志）⇒ **7/7 全部达成**。

**重打包后复跑（对实际分发的产物）**：重打包（PyInstaller + electron-builder，HEAD `c549111`）后，对**内嵌产物** `dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe`（与 `dist/pbc-server` 那份**同 sha256** = `bf8f50cf6af4…`）再跑同 7 轮 ⇒ **7/7 全部达成**（`pdf 7/7`、`img 2/2`、`mineru 7/7`、`rot 5/5`、`robust 4/4`、`dual 7/7`；`cancel` 按设计豁免）。证据 → `devlogs/_verify/r78_e2e_postbuild.json`；门禁 **11/11 PASS**（`4002 passed, 0 failed, coverage 95.09%`）。

### `real_baselines` 契约：基线计数不得当判据（Round 73 第十七批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-15**（属当前 backlog ⇒ 就地记 §0 表；本轮只完成**结构性一半**）。

**定位（报告 §6 R-B）**：`docs/FINDING_GROUND_TRUTH.json::real_baselines` 记的
**784 条**是**历史版本 + `mineru`** 的实测，而当前版本 + `paddle` 对**同一份**文件
实测只有 **208** 条（R76）。该块**没有任何代码消费者** —— 唯一判据是 `cases`
（合成语料、规则层、逐位可复现）的 F1。但文件 `description` 自称“Finding 质量**金标**”、
块的 `note` 自称“**回归对照**” ⇒ 一个照着 784 去“修回归”的人会白干几天；
且真实 job 计数**非确定**（同输入两次跑 **41 vs 51**，+24%）⇒ 单次计数本就不能当阈值。

**改动**：

| 位置 | 改动 |
|---|---|
| `real_baselines` | 加 `criterion: false` + `criterion_note`（写明唯一判据是合成语料 F1） |
| 每个 job | 加 `provenance`（`measured_at` / `ocr_backend` / `llm_model` / `app_version` / `reproducible` / `reason`）+ `comparable_to_current` |
| 每个 job | 加 `runs` + `observed_total_range`（**区间，不是点**） |
| 新增 job | `试用批记录.pdf`（`012e351f-1ea`，`runs: 2`，**41–51**）—— 非确定的现场证据 |

**护栏**：`tests/unit/test_eval_findings.py::TestRealBaselineContract`（**6 条**）——
含 **AST 机检「判据路径（`evaluate` / `aggregate` / `score_case` / `validate_cases` /
`load_ground_truth` / `main`）不得引用 `real_baselines`」**（防门禁随机红 / 绿）
+ 防空转对照。

**变异**：`devlogs/_verify/r77_eval_baseline_mutation.py` ⇒ **7/7 CAUGHT** +
阴性对照绿（临时树 + 独立子进程，只跑 `-k TestRealBaselineContract`；**不筛掉**那条
端到端用例会因临时树缺 `core` 包而每例都红 ⇒ **假的 100%**）。

**未验证边界**：「对照类结论必须带重复次数」目前只对 `real_baselines` 机检，
**尚未**成为通用护栏（`docs/B6-1_GENERALIZATION_REPORT.md` §2 那类对照仍是人工披露）。
`docs/` 不在 `BUNDLE_SOURCES` 内 ⇒ **无需重建**。

### e2e driver 补「LLM 真被调用且成功」判据（Round 73 第十七批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **0-16**（属当前 backlog ⇒ 就地记 §0 表）。

**缺口（R76 定位）**：`e2e_run.py` 全文**零处**引用 `llm_audit` —— 而
`tests/e2e_frozen.py` 早已用 `entries[].success` 当权威判据。**终态绿不蕴含
LLM 跑通**：产品在 LLM 失败时**按设计降级**（把“LLM 调用失败”写成 finding）
并照常走到 `review` ⇒ 只断言终态的轮次会把“LLM 从未成功”记成通过
（`docs/TODO.md` B11-11 第一次翻车即此）。

**改动**（`e2e_run.py`，纯 dev driver，**不入 PyInstaller 产物**）：

| 新增 | 作用 |
|---|---|
| `fetch_llm_audit(c, job_id)` | 取产品自己的 `GET /api/jobs/{job_id}/llm_audit` 的 `entries`；**取不到返回 `None`**（判不了 ≠ 空表） |
| `llm_audit_verdict(entries, require_llm)` | → `(ok, note, detail)`；`entries is None` ⇒ **fail-closed**（与开关无关）；`require_llm` 为假只记录不判红 |
| `run_upload(..., require_llm=None)` | 在**共享 helper 内**接入 ⇒ 所有轮次自动继承；`llm_ok` 参与 `ok` 判定；结果字典带 `llm_calls/llm_ok/llm_failed/llm_note` |

开关复用**单一真值** `tests.e2e_coverage.REQUIRE_LLM_ENV`（与冻结冒烟同一开关，
**不在 driver 里写字面量**）。

**护栏**：`tests/unit/test_e2e_llm_audit_assert.py`（**14 条**）——
语义 6（含 fail-closed 反例）+ AST 结构 4（判定在共享 helper 内 / 判定真的 gate
`ok` / 主结果字典四键 / 包装轮次不得丢掉 `ok` 门）+ 单一真值 3 + 防空转 1。

**变异**：`devlogs/_verify/r76_llm_audit_mutation.py` ⇒ **8/8 CAUGHT** +
阴性对照（EOF 追加注释）**不被抓**；临时树 + 每例独立子进程，只认 `rc==1`
（`rc==2` 记 INVALID）。

**真实 job 两跑**（`devlogs/_verify/r77_e2e_llm_audit_live.py`；产物
`dist/pbc-server/pbc-server.exe`；模型 `deepseek-ai/DeepSeek-V3.2`；`PBC_E2E_REQUIRE_LLM=1`；
凭据取自 appdata 的 `PBC/config.json`，经**环境变量**传入 —— **不写命令行**）：

| 跑 | job | 凭据 | 终态 | `llm_audit` | driver 自判 |
|---|---|---|---|---|---|
| 正控 | `091249fd-040` | 真 | `review`（1007s / 6 页 / 27 findings / `paddle`） | `success=7/7（失败 0 次）` ⇒ OK | `ALL ROUNDS PASSED` |
| 负控 | `903598cd-d0f` | SF key 换成明显无效串 | `error`（100s） | `0/5` 成功，note 点名 `401 code 30014 Token is invalid.` ⇒ FAIL | `FAILED rounds: ['pdf']` |

⇒ 判据在**真实 job** 上确实会响（不只是单测里成立）。

⚠️ **判据脚本自身首版有 bug**：写成 `if neg["llm_ok"] is not False` —— JSON 解析出的
是 int `0`，而 `0 is not False` 在 Python 里是 **True** ⇒ 误报「未达成」。已改 `!= 0`，
并加 `--verify-only`（从**已保存日志重判**，不必为一次断言修正重烧 19 分钟）。

**未验证边界**：负控走的是 **401 ⇒ 硬 `error`** 路径；**「零成功 + 终态仍绿」**
（**402 欠费** ⇒ 产品按设计降级到 `review`，即 B11-11 那种假绿）**未在真实 job 上复现**
—— 402 无法伪造，该分支只在**单测 / 变异**层被覆盖。`e2e_run.py` 与 `tests/` 均不在
`BUNDLE_SOURCES` 内 ⇒ **无需重建**。

### A3 前提否证 + B6-1 泛化验证（第 2 份真实批记录）（Round 72 第十六批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **A3** 与 **B6-1**（均属 §A 及以下 ⇒ 进 §0.1 台账），
> 以及本轮**新发现 0-15**（属当前 backlog ⇒ 就地记 §0 表）。

**A3 —— 前提否证**：A3 声称「目前**只有一份**真实批记录（丝裂霉素）⇒ 泛化未经检验」，
并把它归入「**需要用户动作**」。实测**仓库根一直就有**第 2 份真实批记录
`试用批记录.pdf`（2.9 MB / **10 页**扫描件；华康药业 **康泰普利片 10mg**、
批号 **50008219**、记录号 **H5-MPD-90038-R04**、批量 960,000 片、版本 04），
且**同一份 `docs/TODO.md` 的 §B6-1 早已点名要用它** ⇒ 一件**可做**的事被误挂成「等用户」。

**B6-1 —— 泛化验证**（产物 `dist/pbc-server/pbc-server.exe`，**未重建**）：

| # | 输入 | job | 页 | findings | 耗时 | gmp_basis |
|---|---|---|---|---|---|---|
| 1 | 丝裂霉素（**同版本**基线复跑） | `f1c7efa6-af6` | 51 | **208** | 1287 s | 208/208 |
| 2 | 试用批记录（第 1 次） | `387a9abc-1e9` | 10 | 41 | 233 s | 39/41 |
| 3 | 试用批记录（第 2 次） | `012e351f-1ea` | 10 | **51** | 220 s | 49/51 |

三次均 `paddle`、`backend_mismatch=null`、`sparse_pages=0`、终态 `review`。

- **未发现过拟合的正向证据**：密度 **4.08 vs 5.10** 条/页；类型各覆盖 10+ 种；零 findings 页均为 0。
- ⚠️ **但不宣称「泛化通过」**：两份记录**都没有人工标注集** ⇒ **召回不可测**。
- 报告 → `docs/B6-1_GENERALIZATION_REPORT.md`。

**0-15（本轮新发现，未修）**：流水线**非确定** —— 第 2/3 行**同输入 / 同产物 / 同后端 /
同模型**，findings 数 **41 vs 51（+24%）**，且**类型集合也不同**
（`equipment_state` / `year_contradiction` 只在第 2 次出现）⇒ **单次计数不得当回归判据**。

**另记两条已确证的事实**（写进报告 §6/§7，不另开条目）：

- `docs/FINDING_GROUND_TRUTH.json::real_baselines` 记的 **784 条**是**历史版本 + `mineru`**
  的产物，与本版本（208 条 / `paddle`）**不可比**；`tests/unit/test_eval_findings.py`
  **只断言该块存在**、`truth is False`，**不校验数值** ⇒ 该数字会随代码漂移**静默失效**。
- 逐轮 findings 的证据保全**必须**用 SQLite `backup()` 取快照 —— 该库是 **WAL 模式**，
  `copyfile` 只拷 `data.db` 会**静默丢掉整轮 job**（实测漏掉 `试用批记录` 那一轮，
  其数据在 4.1 MB 的 `data.db-wal` 里）。另：**更正**此前"`e2e_run.py` 每轮清空 appdata"
  的结论 —— **源码否证**（`e2e_run.py:316-318` 只有 `makedirs(exist_ok=True)`，无 rmtree）。

### A3 前提否证 + B6-1 泛化验证（第 2 份真实批记录）（Round 72 第十六批，2026-10-09）

> 记录 → `docs/TODO.md` 的 **A3** 与 **B6-1**（均属 §A 及以下 ⇒ 进 §0.1 台账），
> 以及本轮**新发现 0-15**（属当前 backlog ⇒ 就地记 §0 表）。

**A3 —— 前提否证**：A3 声称「目前**只有一份**真实批记录（丝裂霉素）⇒ 泛化未经检验」，
并把它归入「**需要用户动作**」。实测**仓库根一直就有**第 2 份真实批记录
`试用批记录.pdf`（2.9 MB / **10 页**扫描件；华康药业 **康泰普利片 10mg**、
批号 **50008219**、记录号 **H5-MPD-90038-R04**、批量 960,000 片、版本 04），
且**同一份 `docs/TODO.md` 的 §B6-1 早已点名要用它** ⇒ 一件**可做**的事被误挂成「等用户」。

**B6-1 —— 泛化验证**（产物 `dist/pbc-server/pbc-server.exe`，**未重建**）：

| # | 输入 | job | 页 | findings | 耗时 | gmp_basis |
|---|---|---|---|---|---|---|
| 1 | 丝裂霉素（**同版本**基线复跑） | `f1c7efa6-af6` | 51 | **208** | 1287 s | 208/208 |
| 2 | 试用批记录（第 1 次） | `387a9abc-1e9` | 10 | 41 | 233 s | 39/41 |
| 3 | 试用批记录（第 2 次） | `012e351f-1ea` | 10 | **51** | 220 s | 49/51 |

三次均 `paddle`、`backend_mismatch=null`、`sparse_pages=0`、终态 `review`。

- **未发现过拟合的正向证据**：密度 **4.08 vs 5.10** 条/页；类型各覆盖 10+ 种；零 findings 页均为 0。
- ⚠️ **但不宣称「泛化通过」**：两份记录**都没有人工标注集** ⇒ **召回不可测**。
- 报告 → `docs/B6-1_GENERALIZATION_REPORT.md`。

**0-15（本轮新发现，未修）**：流水线**非确定** —— 第 2/3 行**同输入 / 同产物 / 同后端 /
同模型**，findings 数 **41 vs 51（+24%）**，且**类型集合也不同**
（`equipment_state` / `year_contradiction` 只在第 2 次出现）⇒ **单次计数不得当回归判据**。

**另记两条已确证的事实**（写进报告 §6/§7，不另开条目）：

- `docs/FINDING_GROUND_TRUTH.json::real_baselines` 记的 **784 条**是**历史版本 + `mineru`**
  的产物，与本版本（208 条 / `paddle`）**不可比**；`tests/unit/test_eval_findings.py`
  **只断言该块存在**、`truth is False`，**不校验数值** ⇒ 该数字会随代码漂移**静默失效**。
- 逐轮 findings 的证据保全**必须**用 SQLite `backup()` 取快照 —— 该库是 **WAL 模式**，
  `copyfile` 只拷 `data.db` 会**静默丢掉整轮 job**（实测漏掉 `试用批记录` 那一轮，
  其数据在 4.1 MB 的 `data.db-wal` 里）。另：**更正**此前"`e2e_run.py` 每轮清空 appdata"
  的结论 —— **源码否证**（`e2e_run.py:316-318` 只有 `makedirs(exist_ok=True)`，无 rmtree）。

### 第十四批的修正**只做了 1/4**：同一归因结论写在 4 处，3 处残留与实现相反（Round 71 第十五批，2026-10-08）

> 记录 → `docs/TODO.md` 的 **0-14**（本轮**新发现**；属当前 backlog ⇒ 就地记 §0 表，不进 §0.1 台账）。

**怎么发现的**：第十四批只改了 `tests/e2e_proc.llm_failure_attribution` 的**返回文案**。
复核时沿同一条结论 `grep` 全部落点，发现 **6 处残留**（含 1 处**事实错误**、1 处落在
**纪律文档**里），全部与实现**相反**：

| 位置 | 残留措辞 | 性质 |
|---|---|---|
| `tests/e2e_proc.py` 的 `VERDICT_OK` 行注释 | 「…⇒ 此时失败**才真是**产品缺陷」 | 必要/充分含糊 |
| `tests/e2e_proc.py` 的 `classify_llm_probe` 行为表 | 「…⇒ 流水线仍失败**才是**产品缺陷」 | 同上 |
| `tests/e2e_proc.py` 的 `llm_failure_attribution` docstring | 「``ok`` 分支写"产品缺陷"」 | **事实错误**（已不写） |
| `tests/e2e_frozen.py::probe_llm_credential` docstring | 「仍 error **就是**产品缺陷」 | **无条件过度断言** |
| `docs/PROJECT_PITFALLS.md` §D「修法」 | 「**就是**产品缺陷」 | **纪律文档**里写着被推翻的纪律 |
| `tests/unit/test_llm_failure_attribution.py` 模块 docstring | 「``ok`` 必须写明"是产品缺陷"」 | 同上 |

⇒ 这正是本项目自己的纪律「**同一语义写两处必然漂移**」的实例：改实现而不改复述，
复述就成了**误导源**；而 `docs/PROJECT_PITFALLS.md` 是**纪律文档**，读它会把纪律读回
已被推翻的那一版。

**修法**：

1. 六处残留一律改成「**必要条件，非充分条件**」的说法，或直接指向唯一实现；
2. 新增机检护栏 `tests/unit/test_attribution_single_source.py`：遍历**全部被跟踪的文本文件**，
   禁止被禁措辞出现；
3. 明确区分**断言 / 引述 / 数据**三类，各有各的处理：
   - **断言**（"所以这是产品缺陷"）→ 删掉，改「必要条件 / 不足以断定」；
   - **引述**（描述"当时错成什么样"）→ 保留，但插入 `**` **断开连续字面量**
     （`CHANGELOG.md` 既有约定），让它在文本上就显形为引文；
   - **数据**（`FORBIDDEN_CLAIMS` / `_PRODUCT_DEFECT_CLAIM` 这类常量）→ 保留原文 +
     **同一行**加 `attr-history` 标记；
4. 整文件豁免只给 **append-only** 的 `CHANGELOG.md`，并配一条**反腐**检查：豁免若已无必要
   （文件里再也找不到被禁措辞）就必须删 —— 豁免表只增不减，等于护栏在放水。

**验证**：

- 护栏 `tests/unit/test_attribution_single_source.py`（**6 条**）—— **首跑即抓出 2 处人工清单
  漏掉的引述行**（`docs/TODO.md` 的 0-13 行、`tests/unit/test_llm_failure_attribution.py` 的
  docstring），即"非空转"的现场证据；
- 变异 `devlogs/_verify/mutate_r71_attribution_single_source.py` ⇒ **10/10 CAUGHT**
  （3 处活文件退回旧断言 / `ok` 文案退回无条件 / 抹掉「必要条件」/ 抽掉 CHANGELOG 豁免 /
  扩展名表**部分**与**全部**塌缩 / 关掉同行豁免 / 豁免表加死条目）+ 阴性对照绿 +
  还原逐文件 sha256 一致；
- **自审发现并修掉护栏自身的 fail-open**：初版在扫描范围塌缩时会 `pytest.skip`（把"范围塌缩"
  误当"git 不可用"）⇒ 改为：`git ls-files` 为空才 skip，扩展名表匹配不到任何文件**一律 FAIL**。
  变异 **M7b** 正是为压这条分支而设。

⚠️ **未重建**：本轮只改 `tests/` 与 `docs/`，二者都不在 `BUNDLE_SOURCES` 内 ⇒
`artifact_freshness` 不受影响，无需重新打包。

### LLM 归因的 `ok` 分支**越过证据**断言「产品缺陷」—— 实测被上游 504 打脸（Round 70 第十四批，2026-10-08）

> 记录 → `docs/TODO.md` 的 **0-13**（本轮**新发现**；属当前 backlog ⇒ 就地记 §0 表，不进 §0.1 台账）。

**怎么发现的**：产物级 e2e 两次以 `status=error` 收场，报告同时打印了两行**互相矛盾**的内容 ——

- `FAIL pipeline_terminal … 归因：同一凭据（含一次**计费**最小请求）探测得 免费端点 HTTP 200 +
  计费端点 HTTP 200 ⇒ **凭据与额度均可用**，故此处失败是产品缺陷（优先查：凭据是否被发给了错误的提供方/端点…）`
- 同一行的 `error_message` 与 `llm_call_audit` 里却是：
  **`<html>…<title>504 Gateway Time-out</title>…<hr><center>alb</center>`**

**定位**：`tests/e2e_proc.llm_failure_attribution` 的 `ok` 分支把"探针两段都 200"直接读成"产品缺陷"，
但探针第二段是 `POST /chat/completions` + `max_tokens=1` —— 一个**极小请求**。真实失败发生在
`page_analysis`（~3127 prompt tokens；同一模型成功时耗时 **77s**）上，被上游 ALB 以 **504** 截断
（`llm_call_audit.error` 因此不是 JSON）。⇒ `ok` 的语义只是"**凭据有效 + 额度可用**"，
**推不出**"产品缺陷"；该推理隐含了"探针请求与真实请求等价"这一**未被声明、且已为假**的前提。

与 2026-09-30 那次（**402 欠费**被报成产品缺陷）是**同一形状**：判据本身没错，错在**结论越过了
判据能支撑的范围**。本项目历史上已两次修正同类问题（Round 54、2026-09-30）。

**修法（保留全部既有保护，只去掉超出证据的断言）**：`ok` 文案改为
① 说清证到了什么（凭据/额度可用、**排除 401/402 类环境问题**）；
② **点名盲区**（极小请求看不到长请求的网关超时，附本次实测样本）；
③ 给出可执行的下一步判据（先看 `llm_call_audit.error` 是不是 JSON）；
④ 保留**条件式**归因「…⇒ 才是产品缺陷」，且仍**不得**出现「非产品缺陷」（不倒向环境放行）。

**验证**：

- 护栏 `tests/unit/test_llm_failure_attribution.py::test_ok_text_does_not_overclaim_from_a_tiny_probe`
  （断言四要素齐全 + 旧的越界断言不出现 + 不倒向环境）；
- 变异 `devlogs/_verify/mutate_r72_ok_attribution_scope.py` ⇒ **5/5 CAUGHT**（删盲区句 / 退回无条件断言 /
  删「排除 401-402」/ 弱化「极小请求」/ 把条件式改成无条件）+ 阴性对照绿 + 还原逐文件 sha256 一致；
- **产物实测**：强制 `error` 场景下新文案原样打印（`devlogs/_verify/r72_ocr_ready_failopen_probe.py`）。

**未重建**：改动只在 `tests/`（`BUNDLE_SOURCES` 不含 `tests/`）⇒ 两份产物不受影响。

**同批附记（工具修正）**：该探针脚本首版把"修复前基线"写死为 `HEAD`，而前一批修复**已提交** ⇒
`HEAD` 已是修复后版本，脚本当场 `AssertionError`。已改为**回溯查找**含旧形态的修订
（`HEAD`、`HEAD~1`…），使脚本在"修复已提交"之后仍可复跑。

### 冻结冒烟的 OCR 就绪真值：**只看 env** ⇒ 低报 + 一次真实的 fail-open（Round 69 第十三批，2026-10-08）

> 记录 → `docs/TODO.md` 的 **0-12**（本轮**新发现**；属当前 backlog ⇒ 就地记 §0 表，不进 §0.1 台账）。

**怎么发现的（不是读代码读出来的，是跑出来的）**：跑产物级 e2e 时出现一对**自相矛盾**的输出 ——
`pipeline_terminal status=review`、`llm_audit success=3/3`、`ocr_backend_used=paddle`（**OCR 真的跑了**），
而覆盖清单却写 `[skipped] ocr_pipeline —— 流水线成功，但本轮未提供 OCR 凭据 ⇒ 无法断定 OCR 链路被覆盖`。

**定位**：`tests/e2e_frozen.py` 的 APPDATA 是**固定复用**的（`%TEMP%/pbc-e2e-frozen`，跨会话保留），
上一轮注入过的 Paddle/MinerU 凭据留在 `PBC/config.json` 里；而就绪标志是
`OCR_CONFIGURED = bool(_paddle or _mineru)` —— **只认本轮 env**。两者不一致，于是：

| 情形 | 修复前 | 后果 |
|---|---|---|
| 应用已配、流水线成功 | `ocr_pipeline = skipped`（reason 称"本轮未验"） | **低报**：实际验过却说没验 |
| 应用已配、流水线 `error` | 走"环境未配 ⇒ 预期降级"分支，**只打印 `[SKIP]`、不记 PASS/FAIL** | **fail-open**：真实产品缺陷不产生任何失败记录 |

`classify_pipeline` 的**文档契约**本就写着 `ocr_ready` 指"凭据已提供且被应用**写入**" ——
实现喂的是 env 可用性，**偏离契约**。

**修法（单一真值：应用才是"写没写进去"的权威）**：

| 文件 | 改动 |
|---|---|
| `tests/e2e_proc.py` | 新增纯函数 `ocr_ready_from_settings()` —— 从 `GET /api/settings` 的 `ocr.{paddle,mineru}.configured` 读**应用自报**；非 dict / 缺段 / 非真布尔 `True` 一律 **fail-closed** |
| `tests/e2e_frozen.py` | `OCR_CONFIGURED` 改由该函数派生；新增 `ocr_ready_self_report` 断言（打出 `configured` 与 `env_injected`）；`ocr_config` 仍是 `skipped`（该**配置步骤**本轮确实没执行，语义不变），但 reason 改准（不再声称"流水线成功路径本轮未验"） |

**验证（三层，全部实测）**：

1. **变异 7/7 CAUGHT** + 阴性对照绿 + 还原逐文件 sha256 一致（`devlogs/_verify/mutate_r72_ocr_ready_truth.py`）；
2. **成功路径前后对照**：同一环境（不注入 OCR env）下 `[skipped] ocr_pipeline` → `[covered] ocr_pipeline`
   （`ocr_backend_used=paddle`；`covered=3 skipped=1`）；
3. **fail-open 前后对照**（把上游故意弄坏以逼出 `error`）：修复前 `[SKIP] pipeline_terminal`（**不记 FAIL**）
   → 修复后 `FAIL pipeline_terminal` + `[failed ] ocr_pipeline`（`devlogs/_verify/r72_ocr_ready_failopen_probe.py`）。

**未重建**：改动只在 `tests/`（`BUNDLE_SOURCES` 不含 `tests/`）⇒ 两份产物**不受影响**，
门禁 `artifact_freshness` 保持逐字节一致。

**踩坑（写进 `MEMORY-DETAIL.md`）**：变异脚本首轮在"变异后、还原前"因宿主垫片 `unlink` 抛错退出 ⇒
真文件**带着变异留在盘上**，下一次快照又把它固化成"基线"。已加两道保险：`try/finally` 强制还原 +
快照前校验已知-good 的 sha256。（这正是本项目反复警告的"改真文件跑测试"的危险面。）

### `kb_prompt_inject` 从「装饰开关」接通 + 白名单外字段不再静默（Round 68 第十二批，2026-10-08）

> 记录 → `docs/TODO.md` 的 **#169** / **C4**（同一件事的两处登记）。

**现象（C4 的下半，`#169`）**：`kb_prompt_inject` 是个**装饰开关** ——
`config.py` 支持该键（`AppConfig` 字段 + `update_config` 分支 + `KB_PROMPT_INJECT` env），
注入代码也是**活的**（`core/page_analyzer.py` 的 `kb_prompt_inject` 判定，默认 `True`），
但 `api/settings/write.py` 的 `_STATIC_FIELDS` 白名单**不含**它 ⇒ POST 时落到
`prov_name is None` 分支被 `continue` **静默丢弃**：接口回 "配置已保存并立即生效"，
而字段**根本没落盘**；GET 不暴露、UI 也没有控件。⇒ 只能改 env / 手改 config.json。

**修法（选择「接通」而非「删除」—— 注入能力是真的，删掉是净损失）**：

| 层 | 改动 |
|---|---|
| 白名单 | `_STATIC_FIELDS` 补 `kb_prompt_inject → KB_PROMPT_INJECT` |
| 请求模型 | `SettingsUpdate` 声明 `kb_prompt_inject: Optional[bool]`（此前只靠 `extra="allow"` 兜着，无类型约束） |
| read 接口 | `GET /api/settings` 新增 `kb.prompt_inject`（前端据此回填） |
| UI | `templates/settings.html` 知识库分区加「跨页分析注入条文参考」复选框；`static/settings-ocr.js` 读 GET / 写 POST（失败**回滚**复选框） |

**顺带堵住同类缝（"不静默"半边）**：`prov_name is None` 分支由裸 `continue` 改为记入
`dropped`，随响应 `dropped` 键 + `message` 回显（与既有 `skipped` 掩码回写提示同款）。
⇒ 字段名写错的人**当场看得见**，而不是"保存成功"却什么都没变。

> ⚠️ **未采纳**审计原文的"未知字段应**报错**"。理由：`SettingsUpdate` 是 `extra="allow"`，
> 前端**加字段早于后端认识它**时，报错会**整批**保存失败（连合法字段一起丢）。回显达到了
> 同一目的（消除"以为改了"的假成功）而**无破坏性耦合**。这是**有意的偏离**，写明于此。

**护栏**（新增 **13 条**用例）：
- `tests/unit/test_settings_write_whitelist.py`（**7 条**）：核心是**类级**不变式 ——
  「`SettingsUpdate` 里**显式声明**的每个字段都必须可达（白名单 / provider 后缀）」，
  以后加字段忘改白名单会**当场红**；外加防空转、env/mem 映射、`update_config` 消费、未知字段进 `dropped`。
- `tests/unit/test_settings_js.py::TestKbInjectToggle`（**3 条**，行为级）：读 GET 回填、缺失时回落 `true`、保存失败回滚。
- `tests/integration/test_api_settings.py`（**3 条**）：GET 暴露 + **POST→GET→config.json 落盘**往返 + 未知字段回显（原 `test_unknown_field_ignored` 更名并加强）。

**变异验证**：`devlogs/_verify/mutate_r72_c4_kb_prompt_inject.py` ⇒ **7/7 CAUGHT**
（白名单摘键 / 恢复静默丢弃 / GET 不暴露 / POST 体漏字段 / `load()` 不回填 / 不回滚）
+ **负控绿** + 每个变异后**还原字节一致**（sha256 证明）。

> ⚠️ **变异当场抓出首版护栏的弱断言**：`test_reads_payload_and_posts_on_toggle` 原本用 payload `false`，
> 而假 DOM 里复选框的默认值**也是** `false` ⇒ "读了 payload" 与 "没读、恰好默认相同"**不可区分**，
> M5（摘掉 `load()` 里的 `syncKbInjectToggle()` 调用）**MISSED**。改成 payload `true`（**异于**默认）
> 后才真正咬住。这是"用例存在 ≠ 断言有效"的又一例。

**未做（明确边界）**：`llm_json_mode` / `ocr_dual_compare` 仍是 **API-only**（无 UI 控件），
未一并处理 —— 它们是**同类但独立**的条目，本批只结 `#169`/`C4`。

**入包**：`static/settings-ocr.js` 与 `templates/settings.html` **在产物内**（`api/settings/` 亦然）
⇒ 需**重建**两份产物，否则门禁 `artifact_freshness` 会红。

### 冻结冒烟的就绪失败**不可当场归因** —— 顺带定性一次门禁抖动（Round 67 第十一批，2026-10-08）

> 记录 → `docs/TODO.md` 的 **0-11**。

**现象**：本批门禁首轮 **10/11** —— `tests_coverage` 报 `3931 passed, 8 failed`，
8 条全在 `tests/integration/test_frozen_smoke.py`，且都是 **setup ERROR**
（`Failed: frozen server failed to become ready:`），**不是**断言失败。

**定位（三轮对照，产物逐字节一致）**：

| 轮次 | `test_health_ok` 用时 | 结果 |
|---|---|---|
| R69 门禁 14:32 | **4.039 s** | 通过 |
| R70 门禁 14:58 | **4.666 s** | 通过 |
| 本批首轮 15:12 | **33.157 s** | 超时（30 s 期限用尽） |

`artifact_freshness` 判定两份产物与源码**逐字节一致**；隔离复跑
`test_frozen_smoke.py` ⇒ **8 passed / 20.25 s**；整轮重跑 ⇒ **11/11、3939 passed、
coverage 95.08%**。⇒ 判为**环境抖动**（冷启动 / IO 争用），**不是回归** ——
本批只动 `tests/` + `docs/` + 本文件，未改任何入包字节。

**真缺口 = 可诊断性**：旧失败消息只打印服务端日志，而该日志**为空** ⇒
**无法区分**「进程已死」与「进程活着但没就绪」（前者是崩溃、后者是冷启动/争用，
根因完全不同）—— 8 条 error 因此**无法就地定性**，只能靠整轮重跑。

**修法**（`tests/integration/test_frozen_smoke.py`，**不改判定语义、不加重试**）：
失败消息带上 `after Ns` / `alive=` / `exit_code=`；`rc = proc.poll()` 必须在
`stop_server` **之前**取（否则拿到的是被杀之后的码）。
验证：`devlogs/_verify/verify_r71_frozen_smoke_diag.py` 把 `EXE` 临时指向**立刻退出**的
stub（选 `cmd.exe` 而**非**不存在的路径 —— 后者会被模块级 `skipif` 整文件跳过、测不到
该分支），逼出失败分支 ⇒ 实测消息 `after 5.4s (alive=False, exit_code=0)`，
且**还原字节一致**。

**未做（明确边界）**：**不**加重试（会掩盖真回归）；**不**抬高 30 s 期限 ——
抖动根因未定位，`alive` 字段正是为下一次复现准备的（届时可判"死"与"慢"）。

**不入包**：只动 `tests/` ⇒ **无需重建**；用例数未变 ⇒ 门禁重跑以确认。

### LLM 自由枚举的**前置富集**契约 —— **「2 个死键」的前提被否证**（Round 67 第十一批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十七。

**缘起（更正 #165）**：`docs/TODO.md` 的 #165 断言 `TYPE_QUERIES` 的
`batch_logic` / `low_confidence` 是「**死键** ⇒ `normalize_finding_type` 永不产出该
type ⇒ 词表永不命中」。**实测否证**：富集（`attach_gmp_basis` / `attach_kb_refs`）在
**类型归一之前**执行（`stage2.py`：442/453 → 465；`stage3.py`：132/144 → 191/203），
故 LLM 直出的**原始** type 会被这三条非规范键接住。实测 raw `batch_logic` ⇒
`gmp_basis` 非空 + `kb_refs=4`；raw `time_anomaly` 同理。

**为什么非规范键却不空**：归一**之后**才落到规范类型 —— `batch_logic`→
`batch_inconsistency`（关键词 `batch` 兜底）、`time_anomaly`→`signature_time_anomaly`
（显式同义词）、`low_confidence`→`uncategorized`（**策略使然**：`finding_quality`
模块 docstring 明写「无法归一的落到 uncategorized（而非混入 completeness，避免继续
膨胀最大的噪声桶）」）。⇒ **三层（前置富集 / 归一 / 前端标签）是自洽的**，
前端映射收录这两个键也早有正向对照。

**真缺口（定位）**：这条「**富集先于归一**」的顺序不变式**此前无人守** —— 把归一上提
会让这 3 类 finding **静默**失去依据与引用，而**所有既有用例仍全绿**（它们直接以
raw type 调富集函数）。注意**全局排序是错的**：`stage3.py` 另有一条
`attach_kb_refs(dual_dicts)` 在归一之后，但 `dual_dicts` 的 type 是**写死的规范值**
`completeness`、根本不经过归一 ⇒ 必须**按集合配对**，不能按全局行号排。

**新增护栏**（`tests/unit/test_type_sync.py::TestRawEnumEnrichmentContract`，3 条）：
① 非规范键集棘轮（断言恰为 `{batch_logic, low_confidence, time_anomaly}`）+ **行为级**
证明每条都真能产出依据/引用；② 源码级顺序不变式（AST：从循环 iter 反推「被归一的
集合」，断言该集合的富集行号 < 归一调用行号）；③ 正/负对照（倒序必须报警、正序必须放行）。

**验证**：`pytest tests/unit/test_type_sync.py` ⇒ **12 passed**（原 9）；变异
`devlogs/_verify/mutate_r71_raw_enum_enrichment.py` ⇒ **3/3 CAUGHT**（stage3 归一提早 /
stage2 归一提早 / 删 `time_anomaly` 使键集漂移）+ 负控绿 + **还原字节一致**（sha256 逐文件核对）。

**不入包**：只动 `tests/` 与 `docs/` + 本文件 ⇒ **无需重建**；用例数变了 ⇒ 门禁重跑。

### `docs/TODO.md` 交叉引用解析护栏 —— **变异当场抓出「静默跳过」**（Round 67 第十批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十六。

**缘起**：`docs/TODO.md` 的 `§N 的 M-K` 交叉引用**没有任何检查点** —— R66 实测
`#144` 的尾注引用了**不存在**的 `0-6`（悬空引用），而 `test_todo_freshness.py`
当时只锁「台账 1:1」与「无失效行号锚点」。核销 `docs/TODO.md` **0-7**。

**新增护栏**（`tests/unit/test_todo_freshness.py::TestCrossReferencesResolve`，4 条）：
解析出 **11 条** `见 <id>` 引用 / **102 个**已声明条目（表格行 `| **<id>** |` 与
复选框 `- [ ] **<id> …**` 两种声明形态）；断言每条引用都解析到**真实条目**；
`§N 的 <id>` 额外要求该条目**声明在 §N 内**（章节区间由标题派生、含后代章节，
`§0` = `[38,182)`）；并带**阴性对照**（`见 0-99` 必须被捕获）。

**变异抓出的真实缺陷（首版）**：章节号原本**只认数字**
（`§(?P<sec>\d+(?:\.\d+)*)`）⇒ `见 §B 的 X` 这类引用**一条都匹配不到、被静默跳过**。
M2 变异（把 `§0` 改成 `§B`）因此 **MISSED** —— 而 MISSED 的根因**不是断言弱**，
是**变异无效**（改出来的文本不是护栏认识的形式）。⇒ 改为**宽松捕获 + fail-closed**：
看不懂的章节号一律判红，**不静默跳过**。

**验证**：`pytest tests/unit/test_todo_freshness.py` ⇒ **12 passed**（原 8）；
变异 `devlogs/_verify/mutate_r70_todo_xref_guard.py` ⇒ **4/4 CAUGHT**
（引用侧 `见 0-99` / 章节形态侧 `§B` / 声明侧 改名 `B0-3`→`B0-33` /
章节归属侧 `§0`→`§0.1`）+ 负控绿 + 还原干净。

**核销时又被自己抓了一次**：`docs/TODO.md` 的 0-7 核销文字初稿写了
「`见 0-99`」这种**字面引用形态**来描述阴性对照 —— 那**本身就是一条悬空引用**，
被本护栏**当场判红**（`L51`）。⇒ 说明文字**不得复述被禁形态**（与
`TestNoDeadFrontendAnchors` 那条同一约定），已就地改写并在护栏 docstring 写明。

**不入包**：只动 `tests/` 与 `docs/` ⇒ **无需重建**；但用例数变了 ⇒ 门禁重跑。

### 看门狗 `analyzing` 阈值：**护栏读错了真值源**（Round 67 第九批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十五。

**缘起（对第八批的更正）**：第八批把产物内 `>930s` 静默挂起记成「产物**没有**
墙钟上界」—— 复查发现**该结论是错的**：产物**有**界，`core/watchdog.py` 的
`analyzing` 停滞阈值 = `1800 + 480×页`（封顶 10800s；e2e 那个 1 页件 **2280s**）。
e2e 的 930s 只是**冒烟预算**（`poll_timeout_for_pages(1) 630 + 300`），**故意低于**
看门狗阈值 ⇒ 慢模型在 e2e 显红而产品仍在自己预算内 —— 是**预算错配**，不是缺上界。

**真缺陷（定位）**：看门狗 `_PER_PAGE_S["analyzing"]` 与它自己的护栏
`test_watchdog.py::test_analyzing_base_covers_single_llm_call` **都读
`LLMAdapter.chat` 的签名默认值**（`180.0`）。但 `core/page_analyzer.py` 在**调用点**
把 `timeout` **覆盖**为 `_PAGE_TIMEOUT`，Round 3 已从 180 提到 **480**。
⇒ 护栏断言退化成 `180 >= 180` 的**恒真式**，而它声称守护的真实关系
`180 >= 480` **早已为假** —— `_PAGE_TIMEOUT` 提高时，没有任何东西会响。

**修法（只改真值源与数值，不动机制）**：

- `_PER_PAGE_S["analyzing"]`：`180.0` → **`480.0`**（= 调用点 `_PAGE_TIMEOUT`）；
- 护栏改**从调用点推导**：`_BASE_STALL_S["analyzing"] >= _PAGE_TIMEOUT ×
  (SDK DEFAULT_MAX_RETRIES + 1)`（`AsyncOpenAI` 未显式设 `max_retries` ⇒ 用 SDK
  默认 2 ⇒ 单次 `chat()` 静默上界 **1440s**，基准 1800s 覆盖它），
  且 `_PER_PAGE_S["analyzing"] >= _PAGE_TIMEOUT`；
- 新增**静态护栏** `TestPageAnalyzerCallSiteTimeoutSource`：调用点的 `timeout=`
  实参**必须是** `_PAGE_TIMEOUT` 而非字面量（否则真值源再次被架空），带**阳性对照**。

**验证**：`pytest tests/unit/test_watchdog.py` ⇒ **64 passed**；
变异 `devlogs/_verify/mutate_r69_watchdog_llm_budget.py` ⇒ **3/3 CAUGHT**
（M1 增量退回 180 / M2 真值源上移到 1200 / M3 调用点写死 `timeout=180`）+ 负控绿。
文档同步：`docs/RUNTIME_WATCHDOG.md` 阈值表 `+180s/页` → `+480s/页` + 事故说明。

**重建 + 复跑**：`core/watchdog.py` 在 `BUNDLE_SOURCES` 内 ⇒ 必须重建。
重建链（跳过 `build:css` —— 未碰 `templates/`/`static/`）：`PyInstaller --clean` →
`bundle_manifest --write`（`head=c008769 dirty=False`）→ `electron-builder` →
`gen_provenance.py`（`git_head=c008769`）。两份产物 `pbc-server.exe` 均
**20,487,983 字节**（同 sha256）。随后门禁 **11/11 PASS**（`3932 passed`、
coverage `95.08%`、`artifact_freshness` 两份产物**逐字节一致**）；产物级 e2e
（`frozen --model deepseek-ai/DeepSeek-V3.2 --require-llm`）⇒ **`E2E_RC=0`、
`covered=4 skipped=0 failed=0`**、`llm_audit_success success=3/3`，且
`ocr_backend_used=paddle` **正是本轮配置的后端**（无 failover）
⇒ 新增归因分支的**一致路径**首次在产物上被目击
（`devlogs/e2e_coverage_20261008-143941.json`）。

**仍未修（新记 `docs/TODO.md` 0-10）**：`chat_json` 的 fix-hint 重试链可叠加
（理论上 `3 × 1440 = 4320s` > 1 页阈值 2280s）—— 概率极低，且该情形下"恢复"
本就是期望行为，倾向**显式豁免 + 写明理由**而非抬高阈值。

### 产物级 e2e **首次全绿** + 一处「归因错向」的修复（Round 67 第八批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十四。

**背景（外部条件解封）**：`SILICONFLOW_API_KEY` 此前被上游拒（401 `30014`）/
欠费（402 `30001`）。本轮**先单独探测外部那一半**再跑 e2e：`GET /v1/models` 与
`POST /v1/chat/completions` 双双 **HTTP 200** ⇒ 凭据**可用**。于是
`tests/e2e_frozen.py` 跑出 **28/28 全绿、`E2E_RC=0`、覆盖 `covered=4 skipped=0
failed=0`** —— **`llm_pipeline` 首次被真实覆盖**（`llm_audit` 有 `success=1`
的调用，不再是「靠降级到达终态」）。`docs/TODO.md` 的 **0-8 据此核销**。

**修复（只改归因，不改判据强度）**：驱动把「OCR 真实后端 ≠ 本轮配置的后端」
与「字段根本没写」合并成同一句话 —— 前者却写成「**未记录**
`ocr_backend_used`」。2026-10-08 实测触发了前者：PaddleOCR 上游返回
`state=failed, errorMsg=系统错误-单页` ⇒ 产品**按设计** failover 到 MinerU ⇒
`ocr_backend_used='mineru'`（字段**有值**）。旧 reason 会把排查引向「字段为什么
是空的」，而真问题是「配置的后端为什么没跑成」。修法：
`classify_pipeline(..., ocr_backend_expected=)` + 驱动在不一致分支**保留真实值**；
状态仍为 `failed`。护栏 6 条，变异 `mutate_e2e_coverage.py` ⇒ **11/11**。

**新增能力**：`devlogs/_verify/run_e2e_with_config_creds.py` 支持 `--model` /
`--provider` 覆盖（**只影响本次子进程**）—— 换模型复跑不再需要改用户的
`config.json` 再改回来（那既动了用户实时配置，又留一个「忘了改回」的静默失败面）。

**记录（本批不修）**：实测 `Qwen/Qwen3.5-35B-A3B`（推理模型，输出预算大量花在
reasoning 上）在产物内挂起 **>930s 且无任何日志**；同一任务
`deepseek-ai/DeepSeek-V3.2` **142.9s** 正常返回 ⇒ **`deepseek-ai/DeepSeek-V3.2`
是本轮唯一可用的档位**。已登记 `docs/TODO.md` 0-9。

> ⚠️ **同日更正（R69 第九批）**：上文初版把原因写成「产物**没有**墙钟上界」
> —— **是错的**。产物**有**界（看门狗停滞阈值）。930s 只是 e2e 的**冒烟预算**。
> 真缺陷另在别处，已修 —— 见上方第九批条目。

### 看门狗「启动即首扫」—— 存活信号无空窗（Round 67 第七批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十三。

**定位**（`B4` / 缺陷 `#125`）：`core/watchdog.py::watchdog_loop` 的循环体是
`await sleep(interval)` → `recover_stalled_jobs()`，而 `total_scans` /
`last_scan_at` 写在该轮迭代的 `finally` ⇒ 进程启动后**头一个 interval
（默认 60s）内 `last_scan_at` 恒为 `null`**。后果有二：① 存活信号有 60s 空窗
—— 外部监控无法区分「刚启动还没扫」与「巡检已死」（两者都报 `null`）；
② 崩溃重启后，库里遗留的 `running`/`ocr_running` 行要等满 60s 才被回收。

**修复**：循环体改为**先扫后睡** —— `recover_stalled_jobs()` 移出
`if stop_event` 分支、放到**任何等待之前**；等待块整块挪到扫描**之后**。
「扫描失败也不退出循环」的契约逐字不变（仍包在
`try/except CancelledError/except Exception/finally` 里，异常只写
`last_scan_error` 并继续下一轮）。

**顺带修掉一处更隐蔽的计数缺陷**：旧写法把 `finally`（`total_scans += 1` /
`last_scan_at` 写入）放在等待**之后** ⇒ **等待期间被取消也会记一次扫描**
（`total_scans` 虚高、`last_scan_at` 被「没扫过」的轮次刷新）。等待挪到扫描
之后，取消发生在扫描之外 ⇒ 不再计数。这与主缺陷**同源**（都把「等待」与
「扫描」的次序搞反了）。

**测试**：新增 `tests/unit/test_watchdog.py::TestStartupFirstScan` 两条 ——
① **行为用例**（判官）`test_loop_scans_immediately_before_first_interval`：
`interval` 设成 5s（≫ 等待窗口 0.1s），旧实现此刻扫描 **0** 次、新实现 **≥1** 次；
② **结构钉** `test_loop_waits_only_after_the_scan`：用 **AST** 取 `watchdog_loop`
内「首扫」与「最早等待」的行号并断言 `scan < wait`（不写行号、不写子串 ⇒
重排代码不会假红），含反空转锚点检查。变异
`devlogs/_verify/mutation_r69_watchdog_firstscan.py` ⇒ **7/7**（其中 M2「假修复：
形状对但跳过首扫」只被行为用例抓住 ⇒ 结构钉**只钉形状**，该边界**有意保留**）。

**门禁**：`2c820d9` 上**先红后绿** —— 首次 `09:50` 得 **10/11**
（`artifact_freshness` 指名 `core/watchdog.py`：源码改了、产物还是旧的 ⇒
**门禁干了它该干的事**）；四步重建后 `{mid_hhmm}` 终验 **11/11**、
**3923 passed / 0 failed**、覆盖率 **95.08%**（`gate_report_20261008_100622.json`）。
本批第二个提交 `8e281cd`（只动 `tests/`，**不入包**）⇒ 收尾终验 **11/11**、
**3924 passed / 0 failed**、覆盖率 **95.08%**（`gate_report_20261008_102812.json`）。
`docs/RUNTIME_WATCHDOG.md` §8.10 由「已记录，本轮不改」改为「已修」并新增
§8.10.1（保留「当时为什么不改」的理由段，避免文档自相矛盾）。

**产物级 e2e（重建后实跑）**：`python devlogs/_verify/run_e2e_with_config_creds.py frozen`
⇒ **26 passed / 2 failed**（`devlogs/_verify/_r69_e2e_frozen2.log`）。
2 条红**同因**且**非产品缺陷**：上游 LLM 返回 **402 `code=30001`
"account balance is insufficient"** ⇒ `llm_pipeline`/`ocr_pipeline` 记 `failed`
（产品按设计把 LLM 失败**降级成 finding 并继续**）。⚠️ **与上一批不同**：
上一批是 **401 `30014` 凭据被拒**，本批是**凭据有效但账户欠费** ⇒ 条目 0-8 的
动作由「轮换凭据」更正为「充值/换有余额的 key」。

**同批补齐一处产物级覆盖缺口**：跑 e2e 时发现 `tests/e2e_frozen.py` 的
`watchdog_invariants` 用 `interval + 30` 等 `last_scan_at` —— 旧实现要到
t≈interval 才写它 ⇒ **新旧同样绿，这条断言对 B4 是瞎的**（其旁注释
「循环是先等一个 interval 再扫」在修复后更与代码矛盾）。改为 `interval * 0.8`
（严格小于 interval）并把标签从写死的 `scanned@t+60s` 改成实测
`first_scan@t+<elapsed>s`；新增静态护栏（含系数必须 **< 1** 的上界，
防 `interval * 1.2` 假绿），变异
`devlogs/_verify/mutation_r69_frozen_watchdog_budget.py` ⇒ **5/5**。
重建产物上实跑：**`first_scan@t+0.0s (interval=60s)`** ⇒ 产物级确认。

### `#145` 批记录正文不再进日志/持久化载荷 + 全量重建 + 两份构建物 e2e（Round 67 第六批，2026-10-08）

> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十二。

**Fixed**

- **`#145` 日志/载荷泄漏批记录正文**（四点全修）：
  - `llm/client.py` 新增 `raw_digest()`（`response_length=` + `sha256[:12]`，刻意保留前者
    便于既有排障 grep），**3 个日志出口 + `_raw` 载荷全部**改走它。
    ⚠️ 条目里的行号**已漂移**：真实泄漏点是 **L417 / L453 / L536**（非 361/393/477），
    且 **L536 那处原本连 `_mask_secrets` 都没有**。
  - `logging_config.py`：**逐库**把第三方（httpx/httpcore/openai/urllib3/requests/asyncio）
    降到 WARNING；**root 仍留 DEBUG** —— 这是「逐库降级」而非「抬高 root」，后者会把自家
    DEBUG 一起丢掉（那才是排障要看的）。新增 `PBC_LOG_LEVEL` 控制**文件** handler，
    非法值**不静默**（回退 DEBUG 并告警）。
  - `core/page_analyzer.py`：删掉**零消费者**的 `_parse_error_payload`（**2000 字模型正文**）
    —— 它没有任何读取方，却写进 `page_cache.structured_json`，并被
    `api/review.get_page_data` **整份回给浏览器**；同文件两处 `str(result)[:500]` 也是活的
    正文出口，一并收口。⚠️ 条目**低估了**问题面：`_raw` 有 **3 个生产者**，
    「只修最先找到的那一处 = 没修」。
  - `electron/main.js`：`backend-boot.log` 加 **2 MB 上限 + 轮转**（启动时与运行期各查一次，
    只保留 `.1` 一份；轮转失败**不阻断启动**）。
- **`tests/e2e_unpacked.py` 的 D3 在受限沙箱内假红**：同一轮里 `/health` 已应答、产品自身
  stdout 也写着拉起内嵌后端，而 D3 的子进程枚举**什么都没看到** ⇒ 只报「未找到
  pbc-server.exe 子进程」会把排查引向一个**并不存在**的拉起缺陷。抽出 `_stderr_text()`、
  新增 `_backend_absent_hint()`，D3 的 not-found 分支接上归因。
  ⚠️ **判据强度不变（仍是 FAIL）** —— 沙箱内该事实既证不实也证不伪，据此转绿或转 skip
  都会制造假绿。归因证据与 `_startup_failure_attribution` 同源，同样**不得**把
  `--disable-gpu` 说成解法（2×2 对照已否证）。

**Added**

- `tests/unit/test_log_privacy.py`（**26 条**）：摘要函数的内容无关性/确定性/可区分性、
  解析失败日志与载荷不含正文、`_raw` 生产者全部收口（AST）、第三方降噪**且**自家 DEBUG
  仍在（**配对**断言）、`PBC_LOG_LEVEL` 驱动文件 handler 级别、`backend-boot.log` 上限。
- `tests/unit/test_e2e_unpacked_guard.py` 新增 5 条：D3 必须**调用**归因函数（AST 调用名，
  注释干扰不了）+ 防空转；无 GPU FATAL 时归因**必须为空**（否则会给真实缺陷贴「环境问题」
  标签）；有 GPU FATAL 时必须点明沙箱且不得处方 `--disable-gpu`；日志缺失不得抛。
  另将 `test_attribution_reads_the_stderr_log` 改为**更强**的两段判据（抽取后原断言假红
  —— 「把纯重构当缺陷」的噪声，同 `e2e_frozen` 13b 的教训）。
- 变异验证 `devlogs/_verify/mutation_145_log_privacy.py` ⇒ **9/9，9 条基线全绿**
  （含一条**假修复**变异：把 root 抬到 WARNING 而非逐库降级 ⇒ 必须被抓）；
  `devlogs/_verify/mutation_r68_d3_hint.py` ⇒ **6/6，7 条基线全绿**。

**Build / 产物**

- `#145` 动 `core/`/`llm/`/`electron/` ⇒ 按报告 §5.2 的规矩重建两份产物
  （PyInstaller → `bundle_manifest --write/--check` → electron-builder → `gen_provenance.py`，
  四步全绿，`git_head=acd25ed`）。⚠️ 第二段（`9768ae8`，只动 `tests/`）**没有**触发重建 ——
  `tests/` 不在入包集合，`artifact_freshness` 仍绿，**不是漏跑**。

**Test / 门禁**

- 门禁终验：`overall=pass`、**11/11**、**3921 passed / 0 failed**、coverage **95.1%**
  （`devlogs/gate_report_20261008_091744.json`，树 `9768ae8`）。
  对账：`3889 → 3915` = **+26**（`test_log_privacy.py`），`3915 → 3921` = **+6**（e2e 驱动护栏）。
- **构建物 e2e（①）内嵌后端**：`tests/e2e_frozen.py` 对
  `dist-electron/win-unpacked/resources/pbc-server/pbc-server.exe` ⇒ **26 passed / 2 failed**；
  2 条红同因 —— 上游 **HTTP 401 `Token is invalid`**（两段式探测确认为**凭据失效，非产品缺陷**）。
  两份产物的 `pbc-server.exe` **sha256 完全相同** ⇒ 一次运行即两份产物的证据。
- **构建物 e2e（②）Electron 应用层**：`tests/e2e_unpacked.py` ⇒ **3 passed / 3 failed**；
  4 次复跑里 1 次 `/health` 就绪（9.44s，`version`/`git_head` 均与 PROVENANCE 一致），
  其余 3 次**在 health 之前**就崩。四次均 `exitCode=0x80000003`（Chromium GPU 进程在受限沙箱内
  起不来）⇒ **渲染层/窗口层在本环境无法取证**，须在真实终端复跑（TODO **0-2**）。
- ⚠️ **未验证边界（不得读成已通过）**：`llm_pipeline` / `ocr_pipeline` 本轮均记 `failed`；
  D3 新分支**未实机观测**（只有单元级 + 变异级证据）。

**Docs**

- `docs/TODO.md`：复核戳 Round 66 → **67**；`#145` 翻 `[x]` + 台账证据行
  （§0.1.1 16 → **17** 条、§0.1.3 10 → **9** 条、`[ ]` 70 → **69**、`[x]` 92 → **93**）；
  新增 **0-8**（轮换失效凭据后复跑）并更新 0-2 的 R68 复测证据。
- 报告新增 §十二；§5.2 表格补两行并加「第六段对账」；§九 第 3 条**去掉会腐坏的剩余条数**，
  改为指向 `docs/TODO.md` §0.1.1 台账。

### 分片路径补上配置级故障的 job 级提升（Round 66 第五批，2026-09-30）

> 与第四批同属一类问题（**GMP 假阴性**），但**只在分片路径上**。
> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §11.4。

**Fixed**

- **`#144` 分片路径漏掉配置级故障的 job 级提升**（`core/pipeline/engine.py::_run_sliced_stage1_2`）：
  `_analyze_one` 确诊配置级故障后会闸住后续页（#127 的设计：一次 401/403/400 就别再拿同一份
  坏配置去打 N 次）。闸门没问题，**问题在"闸住之后谁来记账"** —— 整份路径
  `stage2._run_stage2_analysis` 会把 `config_error` 提到 job 级 `error_message`，
  **分片路径的两处 `_analyze_one` 调用都没传 `config_error`** ⇒ 一条 0 finding 的 job
  与"记录本身没问题"在前端**不可区分**。**大文档恰恰走分片路径**，即覆盖面最大的那条路反而没修。
  5 处改动：① 声明共享 `config_error: dict = {}`；② 第一处调用点传参；③ `asyncio.gather` 之后
  **回填**被闸掉的页；④ 自愈前加 `if config_error:` 守卫；⑤ 自愈内调用点同样传参。
  ⚠️ **③ 是①②的伴生回归**：传了 `config_error` 闸门才激活，尚未尝试的页会**既不在
  `failed_pages`、也不在已分析集合**里 ⇒ 两个计数都丢。不补③就是把假阴性换成**静默丢页**。

**Added**

- `tests/unit/test_pipeline.py::TestConfigErrorVisibility::test_sliced_path_escalates_config_error`
  （**行为化**）：`ocr_slices=2` + `ocr_backend="mineru"` 走分片分支
  （`sliced_calls["n"] == 1` **防空转**），断言 `error_message` 含"配置级故障"、
  `failed_pages == [1,2,3,4]`、`calls["n"] == 1`、`heal_calls["n"] == 0`。
- `tests/unit/test_config_error_visibility.py::TestSlicedPathSharesConfigErrorContract`（5 条结构性）：
  `_analyze_one(` 计数 == 2（防空转）、`config_error=config_error,` 计数 == 2、共享字典声明、
  回填块存在、自愈守卫**结构锚**。⚠️ 守卫第一版是**空断言**（只查 `"skip self-heal" in src`），
  M3 变异证明 `if False:` 照样绿 ⇒ 改为 CRLF 归一化的多行结构锚 + 行为化双保险。
- 变异验证 `devlogs/_verify/mutation_144_sliced_config_error.py` ⇒ **5/5，两条基线都绿**
  （M1 丢传参 / M2 回填退化 / M3 守卫失效 / M4 共享字典置 None ⇒ CAUGHT；M5 阴性对照 ⇒ GREEN）。

**Build / 产物**

- `#144` 又动 `core/` ⇒ 按 `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §5.2 的规矩，上一批的门禁
  结论对本树失效：重建两份产物（PyInstaller → `bundle_manifest --write/--check` →
  electron-builder → `gen_provenance.py`，四步全绿）并重跑门禁 ⇒
  **`overall=pass`、11/11、3889 passed / 0 failed、coverage 95.09%**
  （`devlogs/gate_report_20260930_134900.json`，树 `7d53aed`）。
  对账：`3883 + 6 = 3889`，那 6 条即本批用例。
- ⚠️ 构建时工作区是脏的（docs 未提交）⇒ 清单落 `dirty=True`。docs 不在入包集合里，
  提交后**重跑了一次 `--write`** 把它转成 `False`。**`--write` 是断言、不是证据**
  （它在当下计算，构建后再跑不可能与构建矛盾）；结论只引用 `--check`
  —— 两份产物**逐字节一致**。详见报告 §11.5。

**Docs**

- `#144` 条目尾注曾写「见 §0 的 0-6」，而 **0-6 行并不存在** ⇒ **悬空引用**
  （F1/F2 同类缺陷，由本批修复自身引入）。已补上 **0-6**（job 级 `error_kind` 列），
  并登记 **0-7**（给 `§N 的 M-K` 交叉引用加解析护栏 —— 目前无此护栏）。见报告 §11.6。

### 错误文案脱敏 + 产物重建 + 门禁复绿（Round 66 第四批，2026-09-30）

> 前三批只动 docs/tests；**本批动了 `core/` 字节**，于是把「产物新鲜度」这条链也拉进了射程。
> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md` §十一。

**Fixed**

- **`#146` 错误文案未脱敏**（`core/pipeline/stage2.py`）：页级出口此前是**裸的 `str(exc)`**，
  任务级出口也只做 `[:200]` 截断、**不脱敏**。上游异常文本里常见 **API key** 与
  **带签名 query 的 URL**，两者都会经 API 回给前端（页级还会渲染进复核页）。
  新增 `_ERROR_TEXT_LIMIT = 200` 与 `_sanitize_error_text()`：
  `redact_urls(_mask_secrets(text))[:_ERROR_TEXT_LIMIT]`，**两个出口都**改走它。
  ⚠️ **两层顺序是有意的**：截断若放在最前，会把 key 切出一个**残片**（`sk-abcdefgh`
  在 200 字处腰斩成 `sk-abc`）留在输出里 —— **顺序本身就是判据**。

**Added**

- `tests/unit/test_config_error_visibility.py::TestPageLevelErrorTextIsSanitized`（4 条）：
  掩 key / 抹签名 query / **顺序判别**（`payload = "x"*193 + "sk-abcdefgh"` ⇒ 输出**不得含
  `sk-abc` 残片**）/ **两个调用点**都走 helper 的源码断言（防重构只改一处）。
- 变异验证 `devlogs/_verify/mutation_page_error_sanitize.py` ⇒ **5/5，基线绿**：
  M1 页级出口绕开 helper / M2 丢 `redact_urls` / M3 先截断再掩码 / M4 退化成纯截断
  ⇒ 全部 CAUGHT；M5 阴性对照 ⇒ GREEN。脚本用**临时副本 + 每例独立子进程**
  （进程内 `pytest.main()` 会因同名模块跨目录重复导入报 `import file mismatch`，rc=2，
  而 **rc≠0 会被误读成"被抓"** ⇒ 假的 100%）。

**Build / 产物**

- 改 `core/` ⇒ `artifact_freshness`（**逐字节**比对源码与产物副本）转红，实测只有
  **1 个文件**不符（`core/pipeline/stage2.py`）⇒ 说明无其他源码漂移。已按 `build.ps1`
  的步骤用 Bash 复刻重建：PyInstaller → `bundle_manifest.py --write` → electron-builder。
  两份产物（`dist/pbc-server` + `dist-electron/win-unpacked/resources/pbc-server`）
  **均与源码逐字节一致**；清单 `version=1.2.1 files=116 head=8d5848d dirty=False`。
- 新增 `devlogs/_verify/gen_provenance.py`：`electron-builder` 会**整目录重建**
  `win-unpacked/` ⇒ 上一轮的 `PROVENANCE.txt` 被抹掉，而 `count_complete_artifacts`
  拿它当**完整产物三件套**之一 ⇒ 缺它则 `runtime_eol` **静默 SKIP**（不影响退出码）。
  **实测对照**：补文件前 `[SKIP] runtime_eol`，补后 `[PASS] … Electron/43.7.4`。

**Changed（文档，避免误导）**

- `docs/TODO.md`：`#146` 由 `[ ]` 翻 `[x]` 并补 §0.1.1 台账行（核销尾注与台账行数
  仍**相等**：15 == 15）；从 §0.1.3 的"仍开放"抽查中移出并注明**先后顺序**
  （抽查时确为开放，第四批才结案）；统计更新为 `[ ]` 86 → **71**、`[x]` 76 → **91**。
- `docs/ADVERSARIAL_REVIEW_2026-09-30.md`：新增 §十一（本批全过程），§5.2 补第四批的
  `tree × value` 行与对账（`3879 + 4 = 3883`），§一/§七/§八/§九 同步。

**Verified**

```
$ python scripts/release_gate.py          # 工作区干净，HEAD = 8d5848d
[PASS] worktree_clean / no_build_outputs(378) / dist_variants(1) / packaging_files(5)
[PASS] artifact_freshness 2 份产物与源码逐字节一致
[PASS] rules_wired(29) / kb_corpus(441) / kb_packaging(6) / dependency_vulns(0 公告)
[PASS] runtime_eol      在支持线 [41, 42, 43] 内：dist-electron: Electron/43.7.4
[PASS] tests_coverage   3883 passed, 0 failed, coverage=95.09% (门禁 95%)

OVERALL: pass  (pass=11 fail=0 warn=0 skip=0)
报告: devlogs/gate_report_20260930_130757.json
```

⚠️ **构建链上的两条坑（都已记入 `MEMORY-DETAIL.md` §2 / §10）**：

1. **Node 层"安全删除"垫片是两级失败**：默认宿主阈值 50 < 1927 个文件 ⇒
   `SAFE_DELETE_BULK_CONFIRM_REQUIRED`；**只抬阈值仍不够** —— 删除改走回收站后
   `genie-trash` **ETIMEDOUT**。⇒ 构建类命令统一 `CODEBUDDY_SAFE_DELETE_ENABLED=0`。
2. **`PROVENANCE.txt` 缺失会静默降级**（见上），构建后必须补。

### 文档契约护栏 + 待办清单复核（Round 66，2026-09-30）

> 本轮的**发现方式**是"文档 vs 代码/事实"：三条缺陷都不是功能 bug，而是**文档与事实脱节**
> —— 它们不会让任何用例变红，只能靠人工比对发现。第四条（F4）是**测试自身的清理期缺陷**。
> 报告 → `docs/ADVERSARIAL_REVIEW_2026-09-30.md`。

**Fixed**

- `CLAUDE.md` 的 release gate 清单写成 **"9 items"**，实际 `run_all()` 跑 **11** 项 ——
  漏掉 `dependency_vulns` 与 `runtime_eol`（B11-2 加的供应链 / 运行时检查，**正是 Round 58
  查出 50 条公告与 Electron EOL 的那两项**）。已补全为 11 项，并注明必须与 `run_all()` 一致。
- `CLAUDE.md` 提到的垫片源码路径 `cli/vendor/shim/safe-delete-bulk-guard.cjs` **不在本仓库**
  （本树无 `cli/`），已注明它是宿主路径；并澄清两个**不同**旋钮：
  `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`（抬阈值、**安全网仍开**，`release_gate.py` 走的就是它）
  vs `CODEBUDDY_SAFE_DELETE_ENABLED=0`（**整个关掉**垫片）。
- `docs/TODO.md` 头部自称"唯一且最新的待办清单"**是错的**：它停在 Round 59 而 `CHANGELOG.md`
  已到 Round 65；含"自述已解除却仍是 `[ ]`"的条目（`A4`、`#162`）；且仍含 `settings.js:46`
  这类**拆分后已失效**的行号锚点。已更正头部、加**复核戳**、新增 **§0 实核 backlog**，
  并明确标注 §A 及以下为**未复核快照**。
- `tests/integration/test_main_routes.py::TestServePdf::test_pdf_non_local_host_returns_403` ——
  此前被记为"未定 flaky"（全量跑红、单跑绿）。**根因不是逻辑，是清理期**：用例自己的
  `finally: pdf_path.unlink()` 撞上宿主垫片的 fail-closed（`SAFE_DELETE_FAIL_CLOSED` /
  `SHFileOperationW 失败: 0x2`），`OSError` 逃出 `finally`，把一条**断言已通过**的用例染红。
  pytest 自清 `tmp_path` 只记 `PytestWarning` 并继续，**手写 `unlink` 没有这层容忍** —— 差别在"谁来删"。
  已改用 `tmp_path` 夹具并删掉手写 `unlink`；同时**收紧断言** `assert "non-local" in r.text`
  （host 守卫与路径越界守卫**都**返回 403，不锁理由就可能因别的原因通过）。
  ⚠️ 触发条件（为何只在全量跑的某一位次触发）**仍未完全定位**，见报告 §4.2-7。

**Added**

- `tests/unit/test_gate_inventory_doc.py`（5 条）—— 机检 `CLAUDE.md` gate 清单 ↔
  `release_gate.py::run_all()`：条数一致、`CheckResult` 名字不漏、调用点无悬空。
- `tests/unit/test_todo_freshness.py`（4 条）—— 机检 `docs/TODO.md` 复核戳不得落后于
  `CHANGELOG.md` 的最新 Round，且"未复核"免责声明不得被删。
- `devlogs/_verify/mutation_doc_contract_guards.py` —— **9/9**（7 条变异全被抓 + 2 条阴性对照保持绿）。
  ⚠️ 首版用进程内 `pytest.main()`，**基线即红**：同名测试模块跨临时目录被再次导入，触发 pytest
  `import file mismatch`、rc=2 —— 而 rc≠0 会被误读成"变异被抓"，即**假的 100%**。
  已改为**每例一个子进程** + **退出码严格分诊**（只有 rc==1 算 CAUGHT，rc==2 记 INVALID）。

**Notes**

- **发布门禁已在提交 `dd33330` 上重跑：`overall=pass`、`pass=11 fail=0 warn=0`、
  `tests_coverage` = 3875 passed / 0 failed / coverage 95.09%**（报告
  `devlogs/gate_report_20260930_121027.json`）。与修复前 `3866 passed / 2 failed` 对账：
  **+9 条**正是本轮两个新护栏（5+4），**2 条红**正是上面两条已修项。
  ⚠️ `worktree_clean` 是 11 项里的**第 1 项**、先于 pytest 跑 ⇒ 门禁结论只对**当时那份字节**有效，
  跑完再改文件即失效、须重跑。
- **待办核销批的门禁已在提交 `2787f29` 上重跑：`overall=pass`、`pass=11 fail=0 warn=0`、
  `tests_coverage` = 3879 passed / 0 failed / coverage 95.09%**
  （`devlogs/gate_report_20260930_124300.json`）。与 `dd33330` 的 3875 对账：
  **+4** 正是本批新增的 4 条用例（`TestCheckoffLedger` 2 + `TestNoDeadFrontendAnchors` 2）。
- 本地 `main` 领先 `origin/main` **9 个提交**（截至 HEAD `dd33330`）—— **尚未推送**。
  ⚠️ 该数是**快照**、每提交 +1；**取当前值请运行** `git rev-list --left-right --count origin/main...HEAD`，
  不要引用本文里的数字（"文档复述会变的代码事实"正是本轮 F1/F2 的病根）。
- Electron 应用层 e2e 在本 agent 环境**仍取不到证据**（沙箱内 Chromium GPU 崩溃，
  `exitCode=0x80000003`）；**不得读成已通过**，须在真实终端复跑。

**待办清单核销（R66 第三批，2026-09-30）** —— `docs/TODO.md` §A 及以下

> 动机：§A 及以下自称"Round 59 快照"，但其中**一半已做完却仍显示未做**。
> 不核销就按清单开工 = 重复劳动；而清单里"看起来精确"的行号锚点**指不到任何东西**。

- **核销 14 条**（`[ ]`→`[x]` + 尾注 + §0.1.1 证据行）：`[ ]` **86 → 72**、`[x]` 76 → 90。
  其中**两条 P1 安全项**影响最大：
  - `B10-1`（守卫晚于请求体解析 ⇒ 跨站可触发 CPU 型 DoS）**已修** —— `main.py:318-420`
    的**纯 ASGI 中间件**（`add_middleware` LIFO ⇒ 最后注册=最先执行）：Host/Origin 不读 body 即判
    + 流式体积上限 + 非法/负 `Content-Length` 一律拒；护栏
    `tests/unit/test_local_guard_middleware.py` ⇒ **17 passed**。
  - `B10-2`/`B11-7`（4 个依赖漏洞）**已修** —— `requirements.txt` 实测
    `python-multipart==0.0.31`、`Pillow==12.3.0`、`requests==2.33.0`、`python-dotenv==1.2.2`；
    门禁 `dependency_vulns` ⇒ **0 条公告**。
  - 另有 `B10-3`/`B11-8`（Electron 33.4.11 → **43.7.4**）、`B10-4`、`B11-9`（4 个 `dist*` → 2 个）、
    `B11-17`（`docx` 已从 `dependencies` 移除）、`B11-18`（`get_flattened_data` + 兼容护栏）、
    `#166`/`#167`（KB 键覆盖护栏**已存在**，`user_rule`/`uncategorized` 是**有意的开放桶**）、
    `#162`、`A4`、`B4-1`。
- **清掉 16 个前端行号锚点**（12 行，全在 §A）—— 拆模块后指不到任何东西。
  `devlogs/_verify/r66_strip_frontend_anchors.py`，**断言 93 个 Python 锚点未被误伤**
  （那些文件没拆、行号仍有效）。
- **新增 2 条不变式护栏**（`tests/unit/test_todo_freshness.py`）：
  E. 带核销尾注的 `[x]` 条数 **==** §0.1.1 台账行数（防"翻了勾却不写证据"）；
  F. 活清单里**不得有前端行号锚点**（含防空转的构造样例 + 过宽反例）。
- **变异验证 5/5**（`devlogs/_verify/mutation_todo_ledger_guards.py`，基线绿）：
  M1 删尾注 / M2 删台账行 / M3 塞入前端锚点 ⇒ CAUGHT；M4 加不带尾注的 `[x]` /
  M5 加 **Python** 锚点 ⇒ GREEN（不误报）。
- **抽查确认仍开放 12 条**（保留 `[ ]` 并就地记证据）：`#144`（`engine.py` 两处 `_analyze_one`
  确无 `config_error=`）｜`#145`（`root` 仍钉 DEBUG）｜`#146`（`stage2.py:64` 仍 `str(exc)[:200]`）｜
  `#147`（只清 `error_message`/`finished_at`）｜`#148`（`api/report.py` 零处 `error_message`）｜
  `#163①`（注释仍写 "29K chars"）｜`#165`（死键 `batch_logic`/`low_confidence` 仍在）｜
  `#168`（每页检索两次）｜`#169`（`_STATIC_FIELDS` 白名单不含 `kb_prompt_inject`）｜
  `B2-9`（审计写失败仍只 `warning`）｜`B3-3`（`RuleSpec.severity` 零消费点）｜
  `B4-2`（`feishu_app_secret` 仍明文）｜`B7-2`（`ruff check .` ⇒ **105**，与登记一致）。

### 对抗性审查批次 + 重打包验收（Round 62–64，2026-09-30）

> 本批次对应两份审查记录：`docs/ADVERSARIAL_REVIEW_2026-09-28.md`（FIX-1..FIX-11）
> 与 `docs/ADVERSARIAL_REVIEW_2026-09-29.md`（修复 1..9 + §八 并发专项 + §九 重打包验收）。
> 因改动跨越多日，本节按**主题**归并。

**Added**

- 前端模块化：`static/review.js`（原 ~2256 行）拆为 `review-state.js`（复核页共享状态
  **唯一宿主**）、`review-findings.js`、`review-pageinfo.js`、`review-pageview.js`、
  `review-progress.js`、`review-locate.js`、`review-suppressions.js`、`findings-map.js`、
  `pbc-fallback.js`、`sse.js`；`static/upload.js` 拆出 `upload-jobs.js`（活跃作业计数 +
  额度预检的唯一写点）。
- 后端：`core/rules/engine.py`（规则引擎从 `core/rules/__init__.py` 迁出，re-export 保留，
  `tests/unit/test_rules_engine_patchability.py` 锁 patch 锚点）。
- 测试基建：`tests/js_harness.py` —— 浏览器 IIFE 的 node 实跑 harness（假 DOM / 定时器队列 /
  `fetch` / `EventSource` / 键盘分发），供 `tests/unit/test_*_js.py` 复用。
- 新增护栏约 24 个测试文件（`test_review_*_js.py`、`test_upload_*_js.py`、
  `test_settings_js.py`、`test_sse_js.py`、`test_import_graph_contract.py`、
  `test_config_import_order_contract.py`、`test_llm_failure_attribution.py` 等）。

**Changed**

- `server.py`：把 `workers=1` 的注释从"性能取舍"改为**正确性前提** —— `db_lock`、
  `procpool`、job 配额计数、pipeline task 注册表全是**进程内全局单例**，多 worker 会让配额
  按 worker 数翻倍、CPU 池变 2×N、跨 worker 取消失效。护栏
  `test_config_import_order_contract.py::TestSingleWorkerPrerequisite`（4 条，变异 10/10）。
- `main.py`：新增 `_max_concurrent_jobs()`（**调用期**解析，避免顶层 from-import 把常量
  冻结在 import 时刻），经 `templates/upload.html` 下发 `window.__PBC__.concurrency.limit`。
- `core/pipeline/state.py`：状态分区改为单一真值。

**Fixed（真缺陷）**

- 🔴 **并发额度：第 4 个 job 被 409 拒绝时，文件已完整上传**（几百 MB / 分钟级"白传"）。
  前端改为发送前预检 `waitForQuotaSlot()` + 额度 409 有界重试（`QUOTA_RETRY_MAX=2`，
  与去重 409 **不合并**）；熔断 `quotaWaitTimedOut` 的唯一判定点收敛到 `waitForQuotaSlot`。
- 🔴 **`build.ps1` 两处**：
  ① `-Clean` 会**整删 `build/`**，而**本次运行的 start 台账**（B9-6「被杀 vs 真失败」的
     唯一判据）就在 `build/_run` 里、且在脚本第 60 行就已落盘 ⇒ 全量构建一旦被
     `TerminateProcess` 强杀，证据只剩"从未运行过"。改为 `build/` 只删 `_run` 以外内容。
  ② 台账裁剪把 `Remove-Item` 直接挂在**空管道**尾端 —— 其语义随宿主包装器而异
     （实测：真实 cmdlet 容忍空管道，受限宿主的 `Remove-Item` 包装器抛
     `missing path operand`）。改为显式收集 + 判空 + `-LiteralPath`。
  回归护栏 `tests/unit/test_build_script.py`（7 → 11 条），变异
  `devlogs/_verify/mutation_build_clean_ledger.py` **6/6 CAUGHT**。
- 🔴 **`tests/e2e_frozen.py` 两条断言陈旧（假红）**：`#127` 的消费逻辑早已迁到
  `static/upload-jobs.js` 与 `static/review-pageinfo.js`，而断言仍只查
  `upload.js` / `review.js` ⇒ 修复被误报为回归。改为位置无关的 `_served_js()` 多文件取并，
  并补"取到空内容即断言无意义"的防空转。
- 🔴 **`tests/e2e_frozen.py` LLM 失败归因把「账户欠费」报成「产品缺陷」**：归因探测原只用
  **免费**端点 `GET {base_url}/models`。该端点不消耗额度 ⇒ 只能证明"key 是真的"，
  **不能**证明"账户付得起"；账户余额耗尽时上游 402，而 `/models` 照旧 200 ⇒ 判 `ok` ⇒
  报告写"凭据有效，故此处失败是**产品缺陷**"，**同一份报告的另一行**却写着
  `402 code=30001 balance insufficient` —— 自相矛盾且把排查引向代码。
  改为**两段式**探测（免费端点 + 一次 `max_tokens=1` 的**计费**最小请求），判定提为纯函数
  `tests/e2e_proc.classify_llm_probe`（4 态：`invalid`/`billing`/`ok`/`unknown`）+ 文案唯一
  实现 `llm_failure_attribution`。护栏 `tests/unit/test_llm_failure_attribution.py`（22 条），
  变异 `devlogs/_verify/mutation_llm_attribution.py` **9/9 CAUGHT**。
  ⚠️ 覆盖状态**仍记 `failed`**（流水线确实执行了且未达成功终态），**不**因"归因是环境"改记
  `skipped` —— `skipped` 的定义是"因环境缺项**未执行**"，用在这里是事实错误。
- 🔴 **`tests/e2e_unpacked.py` 用 `DEVNULL` 吞掉 Electron 的报错**：`BatchSentry.exe` 以
  `0x80000003` 退出时只剩一个退出码，真因（stderr 里的
  `FATAL: GPU process isn't usable.`）被丢掉，排查成本极高。改为落
  `runN_{stdout,stderr}.log` + 归因函数 `_startup_failure_attribution()`（读 stderr 翻成人话）
  + 逃生口 `PBC_E2E_UNPACKED_ARGS`。护栏 `test_e2e_unpacked_guard.py`（13 → 22 条），
  变异 `devlogs/_verify/mutation_unpacked_guard.py` **5/5 CAUGHT**。
- `static/app.css` 曾过期，缺 `decoration-dotted` 与 `[offset:offset+limit]` 两个选择器
  ⇒ 已发产物里复核页"第 N 页 / 加载更多"链接渲染成**实线**下划线。重跑
  `npm run build:css` 补齐（19921 → 20016 字节，308 → 310 选择器）并重新打包。

**Tests**

- 全量：**3831 passed / 4 skipped**，覆盖率 **95.09%**。
  ⚠️ 需 `CODEBUDDY_SAFE_DELETE_ENABLED=0`：宿主注入的 safe-delete 垫片会在 pytest 清理
  临时目录时因"批量删除 578 > 阈值 50"`SystemExit(1)`，把进程打断（`SUITE_EXIT=3`）。
- 端到端（全部针对**已重打包的产物**）：
  - 内嵌后端层（`tests/e2e_frozen.py`）：**26 passed / 0 failed**，覆盖 `covered=4/4`
    （`llm_pipeline` 以 `llm_call_audit` 中 `success=1` 为权威证据；`ocr_backend_used=paddle`
    为真实后端，无 failover 掩盖）。
  - Electron 应用层（`tests/e2e_unpacked.py` ×2）：**14 passed / 0 failed / 2 skipped**
    （D1 启动 3.0s / D2 版本==PROVENANCE / D3 内嵌后端 / D4 app.asar / D5 主窗口 /
    D6 优雅关闭 `exitCode=0` / D7 checkpoint 收敛）。
- 发版门禁：`pass=10 / fail=0 / warn=0`（`worktree_clean`、`artifact_freshness`
  「2 份产物与源码逐字节一致」、`runtime_eol` Electron/43.7.4 全部转绿）。

**已知限制（不得当作已通过）**

- **上游账户余额耗尽**：`GET /models` 返回 200，但 `POST /chat/completions` 对**全部 98 个
  模型**返回 `402 code=30001 account balance is insufficient`。故本轮
  `PBC_E2E_REQUIRE_LLM=1` **正确地**判 FAIL（fail-closed）—— 这是门禁在按设计工作。
- **Electron 应用层 e2e 无法在受限沙箱内运行**：Chromium 的 GPU 进程起不来
  （`FATAL: GPU process isn't usable.`），应用以 `0x80000003` 退出、`/health` 永不就绪。
  2×2 对照证明 `--disable-gpu` 与结果**无关**（沙箱内加不加都崩、沙箱外加不加都过）
  ⇒ 须在**非沙箱**终端复跑，而不是加开关。
- **他机验证 / 判定准确性 / 精度召回** 的口径同 v1.1.9 基线，未变化（见 `DEPLOYMENT.md`）。

### `settings.js` 全量拆分 + 产物出处护栏（Round 65，2026-09-30）

> 承接 `docs/ADVERSARIAL_REVIEW_2026-09-28.md` §维度 4 的既定计划：`static/settings.js`
> 是**单个 IIFE 闭包、1861 行 / 77.6 KB（CRLF + BOM）**，一处改动的风险面覆盖整页。
> 本轮按计划全量拆为 6 个模块并重打包。**无行为变更**：渲染逻辑逐行保持，只做文件
> 组织与状态宿主收敛。

**Added**

- `static/settings-state.js`（104 行）—— settings 页共享状态的**唯一宿主**，与
  `review-state.js` 同款 `window.PbcSettings` 命名空间模式；`current` / `activeProvider` /
  `pendingAdds` / `removedProviders` 收进 `S.state`，纯工具（`log` / `display` / `esc` /
  `statusBadge` / `ocrDisplay` / `showBackendForm` / `BUILTIN` / `DISPLAY_NAMES` /
  `OCR_DISPLAY`）经 `Object.assign` 导出。
- `static/settings-llm.js`（460 行）/ `settings-ocr.js`（466）/ `settings-feishu.js`（267）/
  `settings-rules.js`（330）—— 按职责拆出。入口**保留原文件名** `static/settings.js`
  （339 行，模板/清单/测试均引用它），只做事件接线 + 底部「保存全部/测试连接」，
  末尾 `S.load()` 启动初始渲染。
- `tests/unit/test_settings_js_modules.py`（11 条）—— 三组结构护栏：**加载顺序**
  （模板引全 / 无孤儿 / 依赖序 / state 最先 / 入口最后）、**状态宿主唯一性**
  （他人不得自建同名变量、消费方真的走 `S.state.`）、**命名空间契约**
  （每模块绑定 `window.PbcSettings`、导出非空、每个 `S.<fn>(` 调用都对应真实导出）。
- `tests/e2e_frozen.py` 新增两条**产物级**检查：`settings_modules_served`（6 个模块都必须
  能从**打包产物**取到且非空）+ `settings_entry_calls_load`（入口必须真的调用
  `S.load();`，不能只查 `load()` —— 文件头注释里也有该词，那是空断言）。
- `tests/e2e_proc.py`：`classify_provenance_git_head()` 纯函数（`ok` / `missing` / `empty`）；
  `tests/unit/test_provenance_git_head.py`（19 条）配套护栏。

**Changed**

- `templates/settings.html`：脚本块按**依赖序**加载（`settings-state.js` 最先、
  `settings.js` 最后），附顺序说明与护栏指向。
- `tests/js_harness.py`：新增 `js_sources(*patterns)` —— 取「整页源码面」。**口径类断言
  （限额字面量、端点名、共享常量）不得锚在单个文件上**，否则一次拆分/改名就静默失效
  （本仓已两次踩到「锚错文件」）。pattern 打错时 `assert files` **立刻红**，不退化空断言；
  按 `utf-8-sig` 读，防 BOM 污染行首锚点。
- `tests/unit/test_settings_js.py`、`test_settings_auto_activate.py`、`test_kb_multisource.py`：
  7 处文本锚点解耦为 `js_sources("settings*.js")`；两条状态名锚点改为容忍
  `(?:S\.state\.)?` 前缀（它们是**调用点**断言，不是字面量相等）。

**Fixed（真缺陷）**

- 🔴 **测试里的「模块名 + 冒号 + 行号」指路会静默腐坏**。拆分后 `tests/` 留下 5 处
  行号锚点（`settings.js:990`、`upload.js:397`、`review.js:343` 这类形态），指向的行
  早已换了内容 —— 而注释不参与执行、`grep` 不报错、门禁也看不见。**R63 拆 `review.js`
  时同样腐坏过一批**，说明这是会复发的类别，不是一次性疏漏。
  修法：① 5 处改为**符号引用**（`upload-jobs.js` 的 `buildMetaLine` / `renderJobRow`、
  `review-pageinfo.js` 的 `updatePageLevelUI`、`review.js` 里那句
  `R.safeAutoReload = R.progress.safeAutoReload` 赋值）；② 新增护栏
  `tests/unit/test_repo_hygiene.py::TestNoLineNumberRefsIntoFrontend`（5 条）。
  · **只扫 `tests/`** —— `docs/` 里的行号是**历史审查证据**（`docs/ADVERSARIAL_AUDIT.md`
    记的是 2026-09-16 的事实），回改等于篡改证据；测试是**可执行契约**，应引用符号。
  · 排除 **URL/端口**：判定看**本行**匹配点之前有没有 `://`。首版写成"匹配点前 12 字符"
    ⇒ 实测两条 URL 用例全红（`://` 在 12 字符之外）。
  · ⚠️ 护栏**自己**也受这条约束：说明文字与夹具都不得写出字面量形态
    （夹具用 `"upload-jobs.js" + ":" + "518"` 拼接）—— 首轮实测就因注释里写了字面量
    而**自我命中 3 条红**。
  · 变异 `devlogs/_verify/mutation_line_ref_guard.py`：**7/7 CAUGHT**
    （含"只植入 URL 端口**不得**报"的阴性对照）。
- 🔴 **产物出处 `git_head` 为空时全链路静默绿灯**。`build.ps1` 用
  `try { & git rev-parse --short HEAD } catch {}` 取 HEAD，**异常被吞**；在 git 不可解析的
  宿主上写出的是 `git_head: `（**空值**）—— 而 `release_gate.count_complete_artifacts`
  只查该文件**存在**、`e2e_unpacked` 的 D2 只比对 `version`，于是「唯一产物能自证出处」
  这条写在注释里的契约**没有任何东西守着**。本轮 agent 环境的 PowerShell 宿主正好
  git 不可解析 ⇒ 直接复现。修法：新增纯函数判定 + `e2e_unpacked` 的 **D2b** 具名检查。
  ⚠️ 判据**只要求非空**，不要求等于当前 HEAD —— PROVENANCE 是**构建期**写的，而构建
  通常发生在提交**之前**（本次拆分即：先打包后 commit，产物里是父提交）⇒ 用相等判会假红。

**拆分正确性的三条改写规则（实测得出，别凭直觉）**

1. **不可变**的纯工具/常量 → 模块顶部解构（调用点零改动）；
2. **可变**状态 → 一律 `S.state.<name>` 属性访问。**解构别名会在重赋值后变陈旧**
   （实测 `current` 39 处、`activeProvider` 20、`pendingAdds` 15、`removedProviders` 7）；
3. **跨模块函数** → `S.<fn>(...)` 属性查找在**调用期**解析 ⇒ 即便存在 ocr↔feishu、
   ocr↔rules 循环依赖也成立，加载顺序只需「state 最先、入口最后」。

**Tests**

- 前端全部 26 个测试文件 **501 passed**（其中 `test_settings_js.py` 31 条）。
- 新增结构护栏 11 条通过；变异 `devlogs/_verify/mutation_settings_modules.py` **9/9 CAUGHT**。
- 产物级 e2e（针对**重打包后**的 `dist-electron/win-unpacked` 内嵌后端）：
  **26 passed / 2 failed**，两条新增 settings 检查均 OK；2 条失败是**账户欠费**
  （402 / `code=30001`），归因文案已判「**非产品缺陷**」，非本次改动引入。
- 出处护栏：单测 19 条 + 受影响子集 166 条 + `test_e2e_unpacked_guard.py` 23 条全绿；
  变异 `devlogs/_verify/mutation_provenance_git_head.py` **10/10 CAUGHT**
  （其中 M2 首轮 MISSED 是**真断言缺口**：真值护栏只断言「判定函数说不是 empty」⇒
  判定函数一宽松就跟着瞎；修法是**独立于判定函数再查一遍原始字节**）。
- 行号引用护栏：`tests/unit/test_repo_hygiene.py` **39 passed**（原 34 + 新增 5），
  受影响四文件合计 **84 passed**；变异 `mutation_line_ref_guard.py` **7/7 CAUGHT**。
- `bundle_manifest`：`version=1.2.1 files=116`（拆分前 111，+5 个新模块），
  `--check` 报「产物新鲜：清单与源码/产物逐字节一致」。

**已知限制（不得当作已通过）**

- **Electron 应用层 e2e 本轮未取得证据**：`e2e_unpacked --runs 2` 两次都在
  `D1_startup` 失败（Chromium GPU 进程起不来）。本轮**额外实测**：即使请求关闭
  沙箱隔离，结果不变 ⇒ 该层在**当前 agent 执行环境**下不可复现，须在真实终端复跑。
  归因文案已判「宿主环境问题，非产品缺陷」。**不得**把这一层读成已通过。
- 上游账户余额仍耗尽，`PBC_E2E_REQUIRE_LLM=1` 本轮**正确地**判 FAIL（fail-closed）。

### v1.2.1 — 报告查看页与优雅返回（Round 61，2026-09-24）

> **问题**：主流程复核页点"下载报告"后浏览器直接导航到裸 Markdown 端点
> （`/api/jobs/{id}/report.md`），脱离站点外壳（顶栏消失），只能靠浏览器
> 后退返回。**本轮**：下载入口改为站内"查看报告"页（SSR 渲染 + 顶栏
> "← 返回复核 / 设置 / 首页"优雅返回），`.md` API 端点保留（e2e 与下游
> 消费者契约不变）。零新依赖（便携包离线分发，不引入 markdown 库）。

**Added**

- 页面路由 `GET /jobs/{job_id}/report`（`main.py::report_page`）：未知
  job 404；非终态（仍在跑/出错/取消）303 回复核页；终态（review /
  partial_review / done）SSR 渲染报告。md 内容与 `.md` 端点共用同一缓存
  函数（`_generate_report_md_cached`，复核状态变化自动失效），不走第二套
  生成逻辑。
- `templates/report.html`：与 review 页同构顶栏（job 身份 + 状态点 +
  优雅返回按钮组），单栏阅读版式，打印样式（`@media print` 去外壳约束）。
- `core/md_render.py`：零依赖 md→HTML 渲染器（标题/两级嵌套列表/粗体/
  行内代码/hr 子集）。**安全模型**：信任生成端 `esc()` 前置转义，实体
  原样透传；超子集输入（表格/引用块等）按段落降级，永不抛错。

**Changed**

- `templates/review.html`：顶栏按钮"下载报告"→"查看报告"，链接指向
  站内页 `/jobs/{id}/report`；状态门（review/partial_review/done）不变。

**Tests**

- 单测 `tests/unit/test_md_render.py` 37 条（子集精确断言 + 实体透传
  安全模型 + 真实报告片段端到端；变异验证 CAUGHT：删粗体处理 → 5 红）。
- 集成 `tests/integration/test_report_page.py` 19 条（路由状态机 ×
  参数化 / 优雅返回按钮 / 内容渲染 / review 入口替换完整性 / `.md`
  端点契约保留；变异验证 CAUGHT：拆掉非终态重定向 → 5 红）。
- 版本一致性机检 4 条通过（APP_VERSION / package.json / package-lock
  双处）。

### 分发就绪 **W2 收尾**（2026-09-24 — 重建 + 新 key e2e + 门禁 10/11 绿）

> W2 批次重建为 **Electron 43.7.4 便携包**（475.1 MB，`dist-electron/win-unpacked`），
> 残壳收敛后磁盘上**只此一份**可分发产物（`dist/` 为打包中间产物，按设计保留）。
> 新 key 真验：LLM `deepseek-ai/DeepSeek-V3.2`（连通性 1691ms + 读回断言一致）+
> OCR paddle 真实后端；冻结版 e2e pdf/rot 双轮全绿（rot 轮 288s，p2/p3 均
> rotation@90° 恢复，gmp_basis 16/16）。门禁 **10 PASS / 1 FAIL**（唯一 FAIL =
> `worktree_clean`，本提交即修）。过程中修掉两处真缺陷：

**Fixed（真缺陷）**

- 🔴 **PROVENANCE 契约断裂 —— 标准路径产物被门禁判"残壳"**（`build.ps1`）：
  门禁 `count_complete_artifacts` 把 `win-unpacked/PROVENANCE.txt` 列为完整产物
  三件套之一，而 build.ps1 **只在备用目录**（被锁自愈时）写该文件 ⇒ 标准路径
  构建的产物**永远被判残壳**、`runtime_eol` 永远 SKIP。旧轮次未暴露只因当时
  完整产物恰好来自备用目录。**已修**：PROVENANCE **无条件**写入（内容区分
  standard/fallback 出处）。
  ⚠️ 修复过程两课：① Edit 工具会**丢 UTF-8 BOM** —— PowerShell 5.1 对无 BOM
  的中文脚本按 ANSI 解析 ⇒ 满屏语法错（提交前必须核对 BOM）；② PowerShell
  语句位的**裸 `{...}` 是 scriptblock 表达式（被输出、不执行）**——首版修复
  "看似成功实则从未写入"，必须平铺语句。
- 🟠 **门禁自测的环境耦合**（`tests/unit/test_release_gate.py`）：
  `test_child_receives_raised_threshold` 隐含假设宿主（CodeBuddy 沙箱）必有
  `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD`，在无该变量的环境（TRAE/CI）恒红。
  **已修**：前置条件由测试**自己注入**（monkeypatch），并补反向对照
  `test_child_not_injected_when_host_has_no_threshold`（宿主未设 ⇒ 不得凭空注入）。
- 文档一致性：CHANGELOG 的 Electron 升级条目误写 `43.7.5`（该版本 win32 二进制
  四家镜像全部 404，实际钉 `43.7.4`），已更正。

### 分发就绪 **W2 落地**（Round 59，2026-09-23 — 升依赖/工具链 + 残壳收敛 + 一处「恒假判据」修复）

> **W2 从"预演通过"转为"真落地"**：4 个 Python 依赖、Electron、npm 构建工具链同时升级，
> 残壳收敛到 1 份完整产物；过程中发现并修掉一个**让清理工具永远报失败**的判据缺陷。
> 教训 → `docs/PROJECT_PITFALLS.md` **§四十四**；条目 → `docs/TODO.md` 的 **B11-7 / B11-8 / B11-9 / B11-17 / B11-18**。

**Fixed（真缺陷）**

- 🔴 **清理工具的判据恒假**（`scripts/clean_dist.py::to_recycle_bin`）：
  判定用的是 `SHFileOperationW` 的返回值，而**本机该函数删除成功时也返回 `rc=2`**
  （`ERROR_FILE_NOT_FOUND`）⇒ `if rc != 0: return False` **永远报 FAIL**。
  本轮**真实踩到**：`clean_dist --apply` 打印两个残壳 `FAIL`，而磁盘上它们**已经消失**。
  ⚠️ **假失败比假成功更危险**：它把操作者/自动化推向 `rm -rf`（本项目明令禁止）。
  **已修**：判据改为**可观测事实**（删前存在 ⇒ 删后必须不存在）；
  `rc` / `fAnyOperationsAborted` 只降级为说明文字。**变异验证 4/4 CAUGHT**
  （`devlogs/_verify/mutate_clean_dist.py`：回退 rc 判据 / 恒 True / 丢中止诊断 / 不存在也报成功）。

**Changed**

- `requirements.txt`：`Pillow` 10.1.0 → **12.3.0**｜`python-multipart` 0.0.21 → **0.0.31**
  ｜`requests` → **2.33.0**｜`python-dotenv` → **1.2.2**（后两个为顺手，均零调用）。
- `electron`：**33.4.11 → 43.7.4**（33 已 EOL 17 个月；支持线 `[41,42,43]`）。
  ⚠️ 钉**精确版本** `43.7.4` 而非 `^43`：43.7.5 的 win32 二进制在 npmmirror/
  华为云/腾讯云/清华四家镜像全部 404（npm 包存在 ≠ 二进制可得，见 B11-21）。
- `electron-builder`：**25.1.8 → 26.15.3**（major）。这消掉了 npm 树里
  **16 条公告（15 high + 1 critical，`tar`）** ⇒ `npm audit` **found 0 vulnerabilities**。
  ⚠️ 说明：本项目 `dependencies` 为**空**，npm 包**全部是构建期 devDependencies**，
  **不随包分发** ⇒ 这 16 条**本就不在 D1 的判据范围内**（D1 = "**产物依赖树**里没有已知漏洞"，
  由 `pip-audit` 对 `requirements.txt` 度量）。此升级属**额外卫生收益**，不是门禁要求。
- `package.json`：删除零消费依赖 `docx`（B11-17，全仓零引用已核）。

**Added**

- `tests/unit/test_clean_dist.py`：`to_recycle_bin` 的**成对**护栏（走真实实现，
  只替换 Win32 调用）——正例 `rc=2 + 已删除 ⇒ 成功`、反例 `rc=0 + 仍在 ⇒ 失败`、
  中止诊断不在说明里丢失、路径不存在不得报"已清理"。
  ⚠️ 旧用例一律把 `to_recycle_bin` **整个 stub 掉** ⇒ 判定分支**从未被真实执行**，
  这是缺陷能潜伏至今的直接原因。
- `devlogs/_verify/mutate_clean_dist.py`：上述护栏的变异验证（4 个变异，覆盖两个方向）。

### 分发就绪 **W3**（Round 59，2026-09-23 — D4 真实成功路径真验通过）

> **D4 从"从未真验"变为"三层互证通过"。** 过程中连抓两个真缺陷，**都在验收方法上**
> （一个让假绿放行、一个让失败被误归因到产品），均已修 + 变异验证。
> 教训 → `docs/PROJECT_PITFALLS.md` **§四十三**；条目 → `docs/TODO.md` 的 **B11-11 / B11-12**。

**Fixed（真缺陷）**

- 🔴 **端到端"覆盖判据"假绿放行**（`tests/e2e_coverage.py::classify_pipeline`）：
  `llm_pipeline` 被记 `covered` **只因为终态是 `review` 且凭据被写入过** ——
  它**从不检查 LLM 是否真的成功过**。首次真跑即复现：`E2E_RC=0`、`covered=4 failed=0`，
  而直查库显示 `llm_call_audit` **3 条调用 2 条失败**（`402`），findings 描述写着
  「**LLM 调用失败: RuntimeError**」 ⇒ **终态是降级路径达成的，LLM 从未跑通**，
  但 `--require-llm` 一条门禁被它放行。这是 B9-7 自己写下的警告
  「成功终态**不蕴含** LLM 被调用过」在**流水线**这一项上从未落地。
  **已修**：判定改为**消费真实证据**（`llm_call_succeeded` / `ocr_backend_used`），
  取不到就 **fail-closed** 记 `failed`；并加**接线护栏**（AST 校验调用点必须传证据、
  证据不得由终态/状态推导）防回归。登记 **B11-19**。
- 🟠 **产物级驱动从不注入 LLM 模型** ⇒ 静默落到产品默认的**收费**档
  (`deepseek-ai/DeepSeek-V4-Pro`) ⇒ 撞 `402 balance insufficient`，
  而报告把 `pipeline_terminal FAIL` 归因成 **"产品缺陷，优先查凭据是否发给了错误的提供方"**。
  **已修**：驱动支持模型注入（复用既有 `PBC_E2E_MODEL`），并新增
  **读回生效值断言** `settings_llm_model_matches`（`GET /api/settings → providers[].model`）。

**Added**

- `tests/e2e_proc.py`：`LLM_MODEL_ENV` / `llm_model()` —— 与根 `e2e_run.resolve_llm_model`
  **共用同一变量名**（曾拟另起 `PBC_E2E_LLM_MODEL`，那会形成"同物不同名"，
  设对一个、另一个静默失效）。
- `tests/unit/test_e2e_proc_helper.py`：模型解析行为用例 +
  **跨模块一致性护栏**（AST 取 `os.environ.get(<字面量>)` 比对两个 driver）并配阳性对照。

**Verified（实测结论，多层互证，非推测）**

- **① 驱动层**：`E2E_RC=0`｜`Total: 26 passed, 0 failed`｜`covered=4 skipped=0 failed=0`。
- **② 产品权威端点**：`GET /api/jobs/{id}/llm_audit` ⇒ **`success=3/3`（失败 0 次）**。
- **③ 直查库**：job = `review` / `error_message=None`；`llm_call_audit` 3 条全 `success=1`；
  `model` 列 = `deepseek-ai/DeepSeek-V3.2`（**无静默替换**）；
  findings 来源分布 `{llm_page:1, llm_cross:1, rule:3}`。
- **④ 抽样核对语义**：LLM 正确抓出夹具超规格项（`actual=9.8 mg` vs `spec=10.0 mg`）。
- **OCR 侧**：`ocr_backend_used = mineru`（真实后端，非 failover 掩盖），
  1 页进/1 页出，`ocr_text` 与夹具内容一致。
- ⚠️ **模型档位**：`deepseek-ai/DeepSeek-V3.2`（免费档；用户指定只用它或 `Qwen/Qwen3.5-35B-A3B`）。

### 分发就绪 **W1**（Round 59，2026-09-23 — 安全/供应链/性能的机检补齐）

> 计划全文 → `docs/RELEASE_READINESS_PLAN.md`；条目 → `docs/TODO.md` 的
> **B11 分发就绪清单**（W1 段已完成记录 + 证据表）；硬约束教训 → `docs/PROJECT_PITFALLS.md` **§四十**。
> **W1 = 6 项"不碰产物、不依赖用户"的工作**，全部完成；护栏合计 **63 条用例**、
> 变异验证 **20/20 CAUGHT**（`devlogs/mutate_b11_1_control.txt` 等四份对照表）。
>
> **门禁实测（`fb29999`，11 项）**：`OVERALL: fail (pass=8 fail=3 warn=0 skip=0)`；
> **在最终提交 `e3b3b4a` 上复测，结论一致**（`devlogs/gate_w1_final_e3b3b4a.log`）。
> `tests_coverage` = **3223 passed / 0 failed / coverage 95.04%**；
> 3 条 FAIL **全部是预期且具名**的（见下方 Known Issues），**无意外的红**。

**Fixed（真缺陷，前四轮审查均未覆盖）**

- 🔴 **头部炸弹 PNG 返回 500**（`api/jobs/upload.py`）。Pillow 的
  `DecompressionBombError` 是 `Exception` 的**直接子类**，既不是 `ValueError`
  也不是 `OSError`；而图片转换路径只 `except (ValueError, OSError, TypeError)`
  ⇒ 该异常落到通用 `except Exception` ⇒ **客户端输入问题被报成 500**，
  并以 `logger.error(exc_info=True)` 记**全栈**。**78 字节**文件即可触发
  （真实像素 8×8、IHDR 声明 40000×40000）。**已修**：显式接住 ⇒ **400**；
  新增 `test_upload_header_bomb_png_rejected_not_500`。
- 🟠 **依赖公告计数虚高 2×**（`scripts/audit_deps.py`）。`pip-audit -r` 会在
  **同一个包的 `vulns` 里把同一条公告重复列出**（实测每条正好 2 份）
  ⇒ 照抄 `len(vulns)` 把 **24 条唯一公告报成 47 条**（pip-audit 自己的汇总行同此）。
  **已修**：按 `id` 去重，同时保留 `raw_entry_count` 使两个数都可见。
  ⚠️ 去重**不改变**安全结论（`minimum_safe_version` 取最大修复版本），只污染证据链。
- 🟡 **两条 magic 用例断言过弱**：只断言 `"图片"`，而"进了解码器才失败"也返回含
  "图片"的 400 ⇒ **摘掉 magic 闸门它们照样绿**（零判别力）。已改为断言 `"文件头"`，
  并由变异 M5 证明现在能红。
- 🟡 **像素上限用例只测了"比较逻辑"**：原用例把上限 `monkeypatch` 到很小、用一张小图，
  未证明"声明超大尺寸的文件在**解码前**被拒"。已补**头部炸弹 PNG**用例。

**Added（机检能力）**

- **门禁 9 → 11 项**（`scripts/release_gate.py`）：
  **第 10 项 `dependency_vulns`**（读离线快照，有公告即**具名 FAIL**）与
  **第 11 项 `runtime_eol`**（从**产物二进制**读 Electron/Chromium/Node 版本，
  与仓库内支持线表比对）。两项均 **fail-closed**（取不到数 ⇒ FAIL，不得记 PASS）；
  产物不存在时 `runtime_eol` **如实 SKIP**（不冒充 PASS）。
- `scripts/audit_deps.py`（**联网**快照生成器）+ `docs/DEPENDENCY_AUDIT.json`
  —— 门禁保持**离线**，不因断网误红，也不随外部数据漂移；快照过老/与
  `requirements.txt` 脱节都会报警。
- `docs/RUNTIME_SUPPORT.json` —— 仓库内显式维护的支持线表（**不联网抓取**）。
- `docs/CVE_REACHABILITY.md` —— **逐条 CVE 可达性表**：24 条唯一公告分为
  **6 可达 / 18 不可达**，并给出判定方法（调用面 grep + 解码面探针 + 入站闸门）。
  另含**首次** `npm audit` 结果（18 包），按**分发面**分三层：
  运行时（仅 `electron`）／构建期（16 个，含 `tar` critical）／**随包死代码**。
- `scripts/perf_baseline.py` —— 性能**相对**判据（`本次/参考 ≥ 0.8×`），
  **刻意不设绝对 SLA**（跨机/杀软会漂移 ⇒ 绝对阈值必然恒真或恒假）。

**Changed**

- **守卫前移到 ASGI 中间件 + 请求体硬上限**（B11-1，对应分发门槛 D3）。
  `main.py` 新增纯 ASGI 的 `LocalGuardMiddleware`：**不读 body** 即按 `Host`/`Origin`
  返 **403**；`Content-Length` 非法 ⇒ 400、超限 ⇒ 413；并对 body 做**流式计数截断**
  （因为**只信 `Content-Length` 会被 chunked/缺头绕过**）。
  ⚠️ Starlette `add_middleware` 是 **LIFO** ⇒ 守卫必须**最后注册**才在最外层
  （护栏含 AST 判据，防注册顺序被改回）。
  实测：畸形体 16 MB 由 **10.26 s / 422** ⇒ **毫秒级 / 403**；
  正常路径吞吐不受影响（B/C 比由 **87–127×** 降到 **0.65–0.82×**）。
- `dist_variants` 判据由"**数目录个数**"改为"**数完整产物份数**"
  （完整性 = `ELECTRON_ENTRY` + `EMBEDDED_SERVER` + `PROVENANCE.txt`），
  并对残壳出**具名清单**。原判据在"1 完整 + 2 残壳"这一**最危险状态**上静默 PASS。

**Known Issues（未修，属 W2/W3，均需外部条件）**

- 🔴 **D1/D2 仍未绿**：依赖公告 24 条（可达 6 条）与 Electron 33.4.11 过支持期。
  修复 = 升级依赖 + 升 Electron ⇒ **必然改产物 ⇒ 必须重建**，
  而重建需**用户完全退出宿主**释放 `app.asar`（会话内无解）。
- 🔴 **D4 仍未验**：LLM 成功路径需要**可用凭据**（现有凭据被上游 401 拒）。
- 🟠 **`docx` 是零消费的生产依赖**：`electron/main.js` 从不 `require('docx')`，
  但 `app.asar` 因此含 **201 个 `node_modules` 条目 ≈6 MB** 死代码
  （也是**永久供应链面**）⇒ 登记 **B11-17**，随 W2 同一次重建删除。
- ⚠️ **`artifact_freshness` 现在如实 FAIL**：`main.py` 已改但产物未重建 ——
  **这是预期中的红，不是回归**；W2 重建后转绿。
- **B11-5 的实测基线值**待重建后采集（判据已就位，只差一次真实测量）。

### 分发就绪 **W2 预演**（Round 59 末，2026-09-23 — 隔离 venv 先验「升依赖会不会坏」）

> **本轮不改产品源码、不改锁定清单**（只新增 1 个护栏文件 + `docs/` + `devlogs/`）
> ⇒ **无需重建**。目的：趁"等用户退出宿主"的空档，把 W2 里**唯一带技术未知数**的一步
> （`Pillow` 跨 **4 个大版本**）先跑一遍，把风险提前清零。
> 隔离手段：基于 Python311 建 `--system-site-packages` 的隔离 venv
> （`…\binaries\python\envs\pbc-pillow12`），**只在该 venv 内**升级 4 个依赖；
> 升级后逐项核对 **全局 Python311 未动**（10.1.0 / 0.0.21 / 1.2.1 / 2.32.4 原样）。
>
> **门禁实测（`d584faa`，11 项）**：`OVERALL: fail (pass=8 fail=3 warn=0 skip=0)`
> —— **与 W1 完全一致，无意外红**。`tests_coverage` = **3233 passed / 0 failed**
> （= W1 的 **3223** + 新护栏 **10 条**）；`no_build_outputs` 已跟踪文件 **321 → 322**。
> 3 条 FAIL 仍是那三条**预期项**（`artifact_freshness` / `dependency_vulns` / `runtime_eol`），
> 全部要等 W2 重建才会转绿。证据：`devlogs/gate_w2prep_d584faa.log`。

**Verified（实测结论，非推测）**

- ✅ **升级在源码级零破坏**：venv 内 `Pillow 12.3.0` + `python-multipart 0.0.31` +
  `python-dotenv 1.2.2` + `requests 2.33.0` ⇒ 全量 `tests/unit tests/integration`
  **3223 passed / 0 failed**，与升级前**用例数完全一致**。
- ✅ **D1 已被直接证实会转绿**：对升级后的 4 行清单跑 `pip-audit -r`
  ⇒ **`No known vulnerabilities found`（RC=0）**；且门禁给出的**最低安全版本与预演升到的完全一致**
  （pillow 12.3.0 / python-multipart 0.0.31 / python-dotenv 1.2.2 / requests 2.33.0）。

**Added（新护栏：把"未来的断裂"提前变成红灯）**

- **`tests/unit/test_pillow_api_compat.py`**（**10 条**，变异 **7/7 CAUGHT**，
  对照表 `devlogs/_verify/mutate_pillow_compat.txt`）。
  预演同时暴露：`core/pipeline/self_heal.py:184,186` 的 `Image.getdata()` 自 Pillow 12 起
  **发弃用警告**，官方给出移除版本 `Pillow 14（2027-10-15）` —— 而 **`Pillow 10.1.0` 不报这条**
  ⇒ 在当前门禁环境里它是"**恒绿**"的判据。护栏**从 `requirements.txt` 派生阈值**（不手写），
  因此 **B11-7 一落地就会立刻变红并点名行号**（变异 **M1** 即模拟此场景 ⇒ CAUGHT）。
  登记为 **B11-18**，与 B11-7 **同一提交**修（Pillow 10 **没有** `get_flattened_data`，
  提前改会让当前环境直接跑不起来 —— 源码必须与锁定清单**同批变**）。

**Known Issues（本预演新增，如实登记）**

- ⚠️ **venv 耗时（12m37s vs 基线 6m09s）不得当性能结论**：差异**未归因**，
  但"新建 venv 首次导入 + 杀软扫描"足以解释 ⇒ 换采样环境之后"变慢"**先怀疑采样环境**。
  B11-5 的吞吐判据**仍须在新鲜产物上做**。

### 分发就绪 **W3 真验**（Round 59 续，2026-09-23 — 端到端跑出来的第一个真缺陷）

> 教训 → `docs/PROJECT_PITFALLS.md` **§四十二**；条目 → `docs/TODO.md` **B11-19**。
> 本轮**只有测试台改动**（`tests/`），`BUNDLE_SOURCES` 不含 `tests/` ⇒ **不触发重建**。

**Fixed（真缺陷 —— 前六轮审查 / 门禁 11 项 / 63 条护栏全部未覆盖）**

- 🔴 **e2e 覆盖判定把"成功终态"当成"链路已验"（假绿，会放行发版）。**
  `tests/e2e_coverage.classify_pipeline` 原规则：
  `if success and ready: 记 covered`。而 `REQUIRE_LLM=1` 的硬要求只查
  `llm_pipeline` 是否 `covered` ⇒ **退出码 0**。
  **实测翻船**：终态 `review`、覆盖清单写 `[covered] llm_pipeline`，
  但直查 `llm_call_audit` ⇒ **3 条调用 2 条失败**（`402 code=30001`），
  findings 描述里写着「**LLM 调用失败: RuntimeError**」——
  **终态是降级路径达成的，LLM 链路从未跑通**。
  **已修**：`covered` 现在必须由**产品自己暴露的证据**支撑
  （`GET /api/jobs/{id}/llm_audit` 的 `success` 字段 / `ocr_backend_used`），
  且**判据值禁止由终态推导**（AST 护栏钉住调用点必须传证据）。
  修复后**用同一真实场景复跑** ⇒ `E2E_RC` 由 **0 → 1**，点名
  `llm_pipeline=failed`（**这才是"验完"的判据**，单测绿不算）。

**Verified（实测结论，非推测）**

- **OCR 链路真验通过**：`ocr_backend_used = mineru`（**真实 MinerU 后端**，非降级），
  1 页解析 13.9 s；`failed_pages=[]`。
- **LLM 链路真验：失败，归因=外部**。同一把 key：一次 `chat` 探针得 **HTTP 200**，
  首次 e2e 的 `page_analysis` **成功**（3213+3126 tokens / 72.8 s，并**消耗掉账户最后余量**），
  此后**所有调用 402**。逐模型实测（含用户点名的 `DeepSeek-V3.2` /
  `V3.1-Terminus` / `Qwen3.5-35B-A3B` 与产品默认 `DeepSeek-V4-Pro`）
  ⇒ **4/4 全部 402 `code=30001 balance insufficient`**；另 6 个小模型候选同样 402
  ⇒ **本账户不存在免费 chat 档**。**结论：D4 阻塞于账户余额（需充值），与产品无关。**
- **判据修复已变异验证 7/7 CAUGHT**（`devlogs/_verify/mutate_e2e_coverage.txt`）：
  覆盖判据侧 2 个 + 接线侧 5 个（含"证据由终态推导""调用点不传证据"）。

### 对抗性审查（Round 58，2026-09-23 — 第五轮：供应链与守卫时序）

> 报告全文 → `docs/ADVERSARIAL_AUDIT.md` **§17**；条目 → `docs/TODO.md` **B10**。
> **本轮不改产品代码**（只动 `docs/` + `devlogs/`）⇒ 按 Repo hygiene 规则
> **不进产物、无需重建**，`1.2.0` 仍是当前可分发版本（但见下方判定）。
>
> **与前四轮的差别**：前四轮审的是**产品逻辑**（规则、LLM、降级、状态机），
> 这一轮首次审**产品所依赖的东西**与**守卫的时序位置**。

**判定：本轮不建议分发。** 工程门禁 9/9 全绿（3147 passed / coverage 95.29% /
`artifact_freshness` PASS），但两条**门禁从未覆盖**的供应链风险成立：

- **B10-1（P1）本地守卫晚于请求体解析 ⇒ 跨站可触发 CPU 型 DoS。**
  `is_local_request` 写在 `api/jobs/upload.py` 的**端点函数体第一行**，而 FastAPI
  在**调用端点之前**就解析请求体。实测（跨站 `Origin` + 畸形体）：
  **1 MB 0.53 s → 16 MB 10.26 s**，且返回 **422 而非 403** ⇒ 守卫**完全没被走到**；
  负对照（同样大体 → 不存在路径）仅 0.01–0.10 s ⇒ 耗时来自服务端解析。
  单 worker ⇒ UI 无响应；看门狗盯"停滞 job"，盯不到卡在解析上的请求 ⇒ 不自愈。
  **格式正确的跨站请求仍被 403 挡住** ⇒ 已有加固没白做，缺的是把闸门前移
  （ASGI 中间件 + 体积硬上限 + 升级 `python-multipart`）。
- **B10-2（P1）依赖漏洞，且两个包在不可信输入路径上。**
  `pip-audit` 首次扫描 ⇒ **50 条公告 / 26 条 CVE**：`python-multipart 0.0.21`
  **7 条**（含 `CVE-2026-24486` 任意文件写；3 条是"畸形头部/前导数据 ⇒ DoS"，
  与 B10-1 实测形态**同源**）；`Pillow 10.1.0` **17 条**（含 `CVE-2023-50447`
  任意代码执行、`CVE-2026-59199` 堆越界写 `Image.paste`——该函数正在
  `api/jobs/upload.py:248` 被调用）。⚠️ **边界**：这是"版本落后"+"处于不可信输入路径"
  两条独立事实叠加，**不是**已证明的可利用性；`requests.extract_zipped_paths` 与
  `dotenv.set_key` **全仓零调用** ⇒ 该 2 条当前不可达。
- **B10-3（P2）运行时已过安全支持期。** 从**产物二进制**读出
  `Electron/33.4.11`、`Chrome/130.0.6723.191`、`node.js/v20.18.3`；
  Electron **33 于 2025-04-29 EOL**（现支持线 41/42/43），Chromium 稳定线已 M156
  ⇒ 落后 **26 个大版本**。对制药客户的供应商审计条款尤其要紧。

**同时复核为「正确」的加固（不推翻，见 §17.3.4）**：`contextIsolation: true` +
`nodeIntegration: false`（splash 与主窗口都设）、`setWindowOpenHandler` 仅放行 `http(s)`、
`will-navigate` 白名单、SSRF 防护**含非点分 IP 字面量**、签名 URL 脱敏、
上传扩展名+魔术字节双检、默认绑定 `127.0.0.1`、26 处端点有本地守卫；
`templates/*.html` 中 `|safe` / `innerHTML` **零命中**。

**性能面（实测，无日常问题）**：后端冷启动 **2.85 s**、完整应用 → `/health` **5.53 s**、
主窗就绪 +1.01 s；读端点中位 **2–7 ms**（p95 ≤ 26 ms）；并发 16 帖 **62 ms**；
**正常上传 54–104 MB/s**（32 MB 仅 0.31 s）vs 畸形 **1.7–2.3 MB/s**（40–60× 差异 ⇒
判为安全面而非性能面）。磁盘 384 MB；**完整应用整棵进程树 RSS 699 MB**（需写进部署要求）。

**精度与噪音（诚实结论）**：唯一的 `P/R/F1 = 1.0/1.0/1.0` 来自**合成集**
（11 case / 10 TP），**不可外推**；真实数据只有代理指标（51 页回放 442→**272**（−38.5%）、
噪声占比 75.8%→60.7%、critical 29→29，且为**离线重放**）。
⇒ `C1 无真实标注集 ⇒ 无可信 P/R` **至今成立**，**不得**上报表级精度数字。
降噪机制已成体系（LLM 四层收权 + R1/M2/R3/B1-1/spec_guard + 三档严重度），
但缺口是**闭环度量**：`finding_suppressions` 台账在真实库 **0 行**、
`eval_noise_reduction.py` 自述非端到端、`needs_arbitration`/`structure_warning` 的 KPI 未补、
R2 批号投票 **0 voted**（有判据无判别力）。

**下一版（v1.2.1）排序**（先进度见 `docs/TODO.md` B10-5）：
① 安全收口（B10-1 守卫前移 → B10-2 升级 4 依赖 → B10-3 Electron 升级 →
B10-4 门禁补第 10/11 项）；② 让精度与噪音**可度量**（**B1-3 真实标注集**是前提）；
③ 工程与性能（B2-3/B2-4、B7-2、B8、B9-2/3/4、C2、A3）。
⚠️ ①③ 会改产物 ⇒ **必须升版 + 重建**，而重建受**宿主持锁**阻塞（B9-8）
⇒ **需用户先完全退出宿主**。

**陷阱固化**：`docs/PROJECT_PITFALLS.md` 新增 **§三十九**（五条）：
A 守卫的**位置**比守卫本身更重要（handler 里=保护效果；中间件里=才保护解析器）；
B **依赖漏洞与框架生命周期是门禁盲区** + 版本要**从产物二进制读**、支持线表要写进仓库；
C 区分"性能问题"与"安全面"要靠**吞吐对照**；D **做对的加固必须一并说**（否则误导）；
E 没有真实标注集时"精确率"这个词就不该出现在汇报里（**有判据 ≠ 有判别力**）。

**未复核（诚实留白，不得当成已通过）**：S2 的逐条 CVE **可达性未验证**（未构造 PoC）；
Electron 的 Chromium CVE 清单未逐条拉取；内存为单次采样（无泄漏结论未下）；
`npm audit`（Node 侧）**未跑**；"浏览器可发送"是基于 CORS 规范 + **服务端实测**的推理，
**浏览器侧未实测**；D8 隔离盲区仍在（`bootLog` 写真实 `%APPDATA%`）。

---

## [1.2.0] — 2026-09-21

> 本版收 **Round 50–53** 四轮。**Round 50–52 的改动不进产物**（只落在 `scripts/` /
> `tests/` / `build.ps1` / 文档；已核实产物 `_internal/` 内无 `scripts/`）；
> **Round 53 有多项改的是产物内的代码**（`core/rules/parsing.py`、`rule_doc.py`、
> `rule_time.py`、`finding_aggregate.py`、`config.py`）⇒ 按 Repo hygiene 规则
> **整体升版到 1.2.0 并重建产物**。
> **当前可分发版本 = 1.2.0**（须重建产物后才成立；`artifact_freshness` 门禁在重建前
> **刻意是红的** —— 那正是它要证明的能力，见下 Round 52 段）。
>
> ⚠️ **两处流程缺口如实声明**（已登记 `docs/TODO.md` 的 **B8**，勿当已解决）：
> ① **Round 53 的 10 个提交都没有同步改本文件**（Round 43–52 是逐提交写的）
> ⇒ 本段是**事后按 `git log c751c4a^..HEAD` 逐条点名补写**，不是当轮记录；
> ② 其中 **B4-3 / B4-5 / B5-4 三个提交未回填 `docs/TODO.md`**（条目长期仍是 `[ ]`）
> ⇒ 本版一并补齐回填，并**复核代码确已落地**（`CONFIG_SOURCE_APPDATA` 在 `config.py`；
> `release_gate.count_kb_entries` 已把 `chapters` 分列且不计入；
> `PBC_E2E_MODEL` / `--model` 已在 `e2e_run.py` 生效）。
> 另：`e2e_run.py` 在**仓库根**，文档里长期写作 `tests/e2e_run.py` 是**笔误**
> （实测该文件历史上从未在 `tests/` 下存在过），本版一并更正。

### Fixed (Round 54, 2026-09-21 — 优雅关闭：从「名义上」变成「可证」)

> 起因是上一轮遗留的问题之一「当前构建物能否优雅关闭」。答案是**不能** —— 见实测。
> 本段所有数字都来自 `devlogs/_verify/probe_shutdown_semantics.py`（多信号交叉：
> 进程存活 / 端口状态 / SQLite `-wal`·`-shm` / 服务端日志 / 退出码）。

- **🔴 `/api/shutdown` 不会让服务退出**：旧产物上 `POST /api/shutdown` 返回
  `200 {"status":"shutting_down","cancelled_tasks":0}`，之后 **15s 内进程仍存活、
  `/health` 仍 200、端口仍被占用**。端点只取消任务，真正关停全靠 Electron 侧信号。
- **🔴 Windows 上「SIGTERM 收尾」是假的**：Node 的 `child.kill('SIGTERM')` 在
  Windows 上等价于 `TerminateProcess`，**不执行任何 Python 代码** ⇒ uvicorn 的
  lifespan 收尾从不运行、`close_db()` 不执行。实测强杀后
  `data.db-wal` **494 432 B** / `data.db-shm` **32 768 B** **原样残留**
  （干净关闭会把它们 checkpoint 掉）。`electron/main.js` 注释里"数据库连接正常关闭
  （避免 SQLite WAL 残留）"与实测**不符**，已随代码一并重写。
- **🔴 强杀兜底是死代码**：条件写成 `if (pythonProcess && !pythonProcess.killed)`，
  而 Node 的 `killed` 在**信号发出时**即置真（官方语义是"已成功发出信号"，与
  "进程已退出"无关）⇒ 前置 `kill()` 之后条件**恒假** ⇒ `taskkill /T /F` 从未执行。
- **🔴 复用孤儿后端永不终止**：复用路径 `pythonProcess === null`，而 Step3/4 整体包在
  `if (pythonProcess && ...)` 里 ⇒ 孤儿只被通知取消任务、从不被终止。实测该端点
  不会让服务退出 ⇒ **孤儿必存活**，占着端口与 DB 句柄。
- **修复（三处，根因优先）**：
  1. `main.py`：新增退出通道 `bind_shutdown_trigger()` / `_schedule_shutdown_exit()`，
     端点响应里带 **`exit_requested`**，让"没有通道"与"已经优雅退出"**可区分**；
     触发器**延后 0.3 s** 执行 —— 退出必须晚于响应写回，否则 uvicorn 的关停流程
     可能在响应写完前收连接（客户端拿到 RST 而不是 200）。
  2. `server.py`：不再用 `uvicorn.run(...)`（它内部自建 `Server` 且**不返回引用**，
     端点因此永远拿不到 Server），改为显式构造 `uvicorn.Config/Server` 并注入
     `should_exit` 回调 —— 走 uvicorn 自己的关停：停收新连接 → 等 in-flight 响应
     → 收尾 lifespan → `close_db()`。
  3. `electron/main.js`：重写关闭流程为「请求优雅退出 → 按**端口释放**等待 →
     超时才升级强杀」，新增 `childAlive()`（判 `exitCode`/`signalCode`）与
     `killProcessTree(targetPid)`（本实例子进程与**复用孤儿**一视同仁），并解析响应体
     的 `exit_requested`；**看门狗重启路径的同源缺陷**（`!pythonProcess.killed`）
     一并修掉，`taskkill` 全仓收敛到**一处实现**。
- **实测（源码模式，修复后）**：响应 `exit_requested: true`；进程 **1.0 s 自行退出、
  returncode 0**；服务端日志出现 `main: Shutdown complete.`（= lifespan 尾段跑过、
  `close_db()` 执行）；端口释放。对照旧产物：只能被 0.02 s 强杀、`-wal` 残留。
- **护栏**：新增 `tests/unit/test_shutdown_graceful_contract.py`（**12 条**）。其中 JS 侧
  是**静态**判据，刻意用「**取函数体 + 包围条件 + 相对位置**」而非全文找 token，并对源码做
  **注释/字符串归一化**（护栏踩过"注释里提到被禁写法就被误判"的坑；给文件加白名单会让
  整份文件失去保护，是更差的选项）。**变异验证 9/9 CAUGHT**。
  ⚠️ 两处自我纠错留档：① 自检阈值原写"代码视图 ≥ 原文 50%"，把**正确**的剥注释实现
  判红（本文件注释密度高，实测约 48%）⇒ 比率只是 sanity 下限，语义保护靠函数体存在性；
  ② 数 `taskkill` 必须用"去注释、**留字符串**"的视图（命令字面量在模板字符串里），
  否则是恒真的空断言。
- **地面真值**：全量 `pytest tests/unit tests/integration` = **3042 passed / 2 failed**，
  两条失败均为**重建前预期红灯**（产物仍 1.1.9 构建），非本次改动引入。
- **顺序纪律**：本提交**先于**重建（否则 `bundle_manifest` 会把 `git_dirty` 记成 `true`，
  产物无法自证出处）。

### Fixed / Verified (Round 54 续, 2026-09-21 — v1.2.0 重建 + 产物级 e2e + 三处判据缺陷)

- **v1.2.0 产物已重建**：`dist/pbc-server/`（PyInstaller，**107.1 MB**，冒烟
  `/health` 2 s 通）+ Electron 便携版 **381.5 MB**。因宿主（Restart Manager 具名）
  长期持有 `dist-electron/win-unpacked/resources/app.asar`，`build.ps1` 按设计
  **自愈**到 `dist-electron-out-20260921-153526/win-unpacked/`，并写了
  `PROVENANCE.txt`（`git_head=212f2fc`、`version=1.2.0`）—— **产物能自证出处**。
- **🔴 更正上一轮的"PyInstaller 构建失败"结论：那是误判。** 真因是**长构建被放后台跑，
  回合结束时被连带回收**（症状＝exit 1 **零输出** + `build/pyinstaller.log`
  **戛然而止无 Traceback** + workpath 为空 —— 与"构建崩了"**症状完全一致**）。
  改前台跑同一条命令：**113.8 s 一次通过**。已写入 PITFALLS §三十五 A。
- **优雅关闭在「要分发的产物」上被证实**（此前只在源码模式验过）：对
  `dist-electron-out-…/resources/pbc-server/pbc-server.exe` 实测 ——
  响应 `exit_requested:true`；进程 **0.75 s 自行退出、returncode 0**；
  端口释放、`/health` UNREACHABLE；日志含 `main: Shutdown complete.` 与
  `Application shutdown complete.`；`data.db-wal`(494 432 B)/`-shm`(32 768 B)
  **已 checkpoint 并消失**（`data.db` 4 096 → 323 584 B）；强杀兜底
  `terminate_result: SKIP (进程已自行退出)` —— **未被触发**。
- **产物级 e2e（冻结包）**：**22 passed / 1 failed**。唯一失败＝LLM 凭据被上游拒绝
  （裸客户端直连 `api.siliconflow.cn/v1/models` 得 `401 code=30014`）⇒ 已如实归因、
  **不冒充 PASS**（登记 B9-7）。
- **修掉三处"在旧字节上做结论"的判据缺陷**（同一根因的三个实例）：
  1. `tests/e2e_frozen.py` 的 `#127` 判据原先两半分别**恒假**（class 已搬到
     `status.js`）与**恒真**（正则在本项目真实写法 `if (["review","done"].includes(st))`
     上 0 命中）⇒ 零判别力，且把**已正确分发**的修复报成"缺标记"。改为把产物里的
     `status.js` 交给 **node 真的跑一遍**，断言**语义关系**
     （`partial_review != review`、`error != review`、`partial_review != error`、
     `review == done`）。判据实现在新模块 `tests/e2e_status_js.py`（**一处**）。
     **变异 8/8 CAUGHT**；对照列显示**旧判据对同组变异零反应**（恒红常数）。
  2. `tests/unit/test_bundle_manifest.py::test_real_asar_matches_source_for_electron_main`
     **写死** `dist-electron/…/app.asar` ⇒ 自愈目录出现后**永远红**（旧包版本永远
     追不上）。改为复用 `test_distribution_parity._newest_artifact()`。
  3. `devlogs/_verify/probe_shutdown_semantics.py` 的 `DEFAULT_EXE` **写死**标准目录
     ⇒ 会对**旧包**下关闭语义结论。改为按 mtime 取**最新**产物，并把被测字节的
     **mtime/大小**写进报告（"报告要能自证出处"）。
- **新增护栏** `tests/unit/test_e2e_status_js.py`（**9 条**：阳性对照 + 覆盖性 +
  6 条变异），保证判据自身不会退化成空断言。
- **失败归因改为可证**：`e2e_frozen.py` 不再把"已配 OCR 的 error"一律写成
  **"真实缺陷"**（实测一份失效 key 就会被这么写，把排查引向代码），改为拿
  **应用自己上报的 `base_url` + 我们交给它的凭据**做一次直连探测：确凿 `401/403`
  ⇒ 归因"上游拒绝该凭据（环境）"；探测**通过**而流水线仍 error ⇒ **就是产品缺陷**
  （这一支恰好能抓"凭据发给了错误提供方"那个历史真事故）；探测**判不了** ⇒
  按真实缺陷**fail-closed**。
- ⚠️ 当时**未解决**：`artifact_freshness` 仍**恒红** —— 被持锁的陈旧 `dist-electron/`
  被 `discover_artifacts` 当作产物。已登记 **B9-5**（**已于 Round 55 结项**，见下节）。

### Fixed / Changed (Round 55, 2026-09-23 — B9-5 结项：收敛陈旧字节 + 门禁承认第三态)

> **本段改动不进产物**（只落在 `scripts/` / `tests/` / 文档；已核实入包清单 97 个文件中
> **没有任何 `scripts/` 条目**）⇒ **v1.2.0 产物无需重建**，`artifact_freshness` 与
> 产物级 e2e 的结论对当前 HEAD 仍然成立。

- **① 收敛（治字节）**：`clean_dist.py` 新增 `--converge-locked`。整目录回收失败时，
  退到**目标粒度**回收"会被误分发的那一份" = 内嵌后端
  `win-unpacked/resources/pbc-server`（派生自既有 `EMBEDDED_SERVER` 常量，
  不新写路径规则）。逐项先做**改名探测**（不可替换就不动）、走**回收站**、
  默认 **dry-run**、残壳写 **`HUSK.md`** 具名身份与"这里少了一份产物"。
  **实测**：执行后 `discover_artifacts` 不再返回该陈旧产物 ⇒ 本项**由恒红转 PASS**；
  最新产物（`dist-electron-out-20260921-160111`）**毫发无伤**。
- **② 判据三态（治判据）**：`release_gate.check_artifact_freshness` 不再只有两态。
  陈旧 + **可替换** ⇒ `FAIL`（可行动，提示 `--converge-locked`）；
  陈旧 + **不可替换 + Restart Manager 具名** ⇒ **第三态 `WARN`**
  （具名持有者，并**显式写明**"仍可被读取并打包分发，勿据此认为可发布"——**不是 PASS**）；
  不可替换 + **取不到签名** ⇒ **仍 `FAIL`**（fail-closed）；
  **一份新鲜的都没有** ⇒ 一律 `FAIL`（锁不构成借口）。
  持有者探测复用 `clean_dist` 的**同一份**实现（`named_holders`），避免第二份 ctypes。
- **护栏**：`test_release_gate.py` + `test_clean_dist.py` 新增 9 条
  （三态各一条 + 第三态**不是 PASS** + fail-closed + 部分收敛只动白名单目标 /
  不可替换则不动 / 写 HUSK / dry-run 不动手）。合计 **120 passed**。
- **变异验证 10/10 CAUGHT**（`devlogs/_verify/mutate_b95.py`）；对照实验
  （`devlogs/mutate_b95_control.txt`）证明改动**只**改变「不可替换」那一态的判定，
  另两态判据**逐字未动** —— 差异是判据性的，不是文案。
- **新增遗留**：**B9-8**（新建的 `dist-electron-out-*/…/app.asar` **同样**会被宿主句柄
  持有 ⇒ 多份变体**无法在会话内靠脚本收敛**）；**B9-6 / B9-7** 见 `docs/TODO.md`。
- 陷阱固化到 `docs/PROJECT_PITFALLS.md` **§三十六**：
  ① 任务清单的"已完成"不是证据（本轮实测**假完成**：清单标 `[completed]` 的
  `scripts/artifact_lock.py` **根本不存在**，虽实质目的已达成）；
  ② "整目录删不掉" ≠ "无法收敛"（锁落在**文件**上，必须按目标粒度探测）；
  ③ 第三态**不能降成 PASS**；④ 加载历史版本脚本做对照时 `@dataclass` 要求模块在 `sys.modules`。

### Fixed / Added (Round 56, 2026-09-23 — B9-6 / B9-7 / B9-9 结项：让「没验到」成为一等公民)

> 三项的共同点：都属**判据/记账**问题，而非产品功能缺陷。改动只落在 `scripts/` /
> `tests/` / `build.ps1` / 文档 —— **已核实产物入包清单 97 个文件里无 `scripts/`、
> 无 `tests/`** ⇒ **本轮无需重建产物**（`1.2.0` 仍为当前可分发版本）。
>
> ⚠️ 先澄清一个易误读的前提：**v1.2.0 产物已成功重建**，不存在"无法打包"。
> 生产 `dist-electron/` 目录**被宿主进程（WorkBuddy）持锁**（锁落在 **文件**
> `win-unpacked/resources/app.asar` 上），故 `build.ps1` 按设计自愈到
> `dist-electron-out-<ts>/` 并写 `PROVENANCE.txt`。`app.asar` 是 Electron 的归档格式
> （类似 tar/zip，内含应用代码与 `electron/main.js`）—— 宿主把它当"包"打开后留下
> 未带 `FILE_SHARE_DELETE` 的句柄，于是该目录不可删、不可原地重建。
> 判据只有一个实现：`devlogs/_verify/who_holds.py`（Restart Manager 直接具名 PID）。

- **B9-9 门禁/清理工具"检查会动仓库"（改名探测）→ 改只读判据**：
  `clean_dist.py` 删除 `_rename_probe` / `_find_locked`，改为 `can_delete(path)` ——
  `CreateFileW(path, DELETE, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING)` 只请求删除权、
  **不删除**，随即 `CloseHandle`。零副作用。
  🔴 **实测撞到的关键陷阱：只读判据对目录会低估锁定** ——
  `dist-electron/win-unpacked/resources`：改名探测 `winerror=5`（拒绝）而
  只读探测 `winerror=0`（放行）。根因：**目录自身的 DELETE 权限 ≠ 其子项可删**
  （NTFS 只在真正递归删除时才检查子项句柄）⇒ 只读方案**必须全树遍历逐文件判定，
  不得目录级短路**；目录自身不可删只作**无路径的伪条目**如实上报。
  **等价性实证**：新旧两种口径在真实目标上报出的被锁集合**完全一致（0 不一致）**，
  最坏 937 文件 0.69s（`devlogs/_verify/probe_lock_equivalence.py`）。
  护栏 42 passed｜变异 **9/9 CAUGHT**（`devlogs/_verify/mutate_b99.py`）。
- **B9-6 长构建"有始无终"与"真失败"不可区分 → 运行台账**：
  `build.ps1` 起步写 `build/_run/<runId>.start.json`（`git_head` + `pid`），
  每个出口写 `<runId>.finish.json`（`rc` + 错误文本）；`scripts/build_status.py`
  读台账给六态 `none/running/interrupted/failed/completed/unreadable`。
  核心签名：**「有 start 无 finish」= 被外部终止**（`finally` 在 `TerminateProcess`
  下不执行 ⇒ "始而无终"是被杀的可靠痕迹，不是猜测）。
  ⚠️ **实现时先复现了一遍要消除的现象**：`CommandNotFoundException`（工具缺失）是
  **statement-terminating** 错误 —— 即便 `$ErrorActionPreference="Continue"` 也会
  中断语句、**绕过 `Write-Fail`**，于是"工具缺失"伪装成"被杀"。
  两层修复：内层 catch 显式 `$script:LASTEXITCODE=1` + 顶层 `trap` 兜底 `Write-Finish 1`。
  ⚠️ pid 存活检测**不得用 `os.kill(pid, 0)`**（Windows 上对任意 pid 都像成功）⇒
  改用 `OpenProcess` + `GetExitCodeProcess == STILL_ACTIVE`。
  护栏 17 passed（含**真实失败路径** E2E）｜变异 **10/10 CAUGHT**（`mutate_b96.py`）。
- **B9-7 让"未覆盖"可审计、可卡门 + 命名债**：
  新增 `tests/e2e_coverage.py`（记账口径的**唯一实现**）：`Coverage` 逐条记
  `covered / skipped / failed` + 原因 + 自解释 `meaning`，冻结包冒烟每轮落盘
  `devlogs/e2e_coverage_<ts>.json`；判定规则集中在 `classify_pipeline`（由**事实**
  推导：终态字符串 + 凭据是否齐备 + 上传是否失败），故可被单测钉住、无需起服务。
  新增硬要求 `PBC_E2E_REQUIRE_LLM`（默认关）：置 1 时 `llm_config` 与 `llm_pipeline`
  **两条**任一不是 `covered` ⇒ 整体 FAIL 并打印 `UNMET HARD REQUIREMENTS`。
  🔴 **`skipped` 绝不等于 `covered`**：缺凭据时哪怕流水线走到成功终态，LLM 链路也只记
  `skipped` —— 成功的终态**不蕴含** LLM 被调用过（Stage 2 会降级）。
  这正是"22 条断言全绿、最贵的那条链路从没跑过"不再能冒充发版通过的机制。
  ⚠️ **"默认关闭"≠"无凭据也能跑绿"**：产品自身在未配置 LLM 时**直接 400 拒绝上传**
  ⇒ 那轮冒烟本来就红；这道门拦的是"其它全绿、只差 LLM 链路"的**假成功**。
  命名债：`LLM_KEY_ENV` → `PBC_E2E_LLM_KEY`（旧名 `PBC_E2E_DEEPSEEK_KEY` 仍可读、
  打印弃用提示、**新名优先**）；同步 3 个驱动 + `DEPLOYMENT.md` + PITFALLS。
  ➕ **顺带修掉同类未爆弹**：`e2e_quick.py` / `e2e_manual.py` 把
  `{"llm_provider": "deepseek", ...}` **写死**（与 Round 33/54 记录的 401 事故同源，
  只因样例 PDF 曾是空白页而长期"绿"）⇒ 改为由 `llm_provider()` 派生，并加 **AST 护栏**
  （查 dict 字面量，不查注释文案 —— 注释里**刻意**留了反例）。
  **实测**（真实产物 `dist-electron-out-20260921-160111/…/pbc-server`）：
  开 `REQUIRE_LLM=1` ⇒ **EXIT=1** + `UNMET HARD REQUIREMENTS: llm_pipeline=failed`；
  默认口径 ⇒ 清单如实记 `covered=0 skipped=2 failed=2`。
  护栏 55 passed｜变异 **20/20 CAUGHT**（`mutate_b97.py`）。
  ⚠️ **残余（环境性，非工程缺陷）**：凭据仍失效（`401 code=30014 Token is invalid`）
  ⇒ LLM 成功路径**仍未真验过**；发版前须换有效 key 并开 `PBC_E2E_REQUIRE_LLM=1` 跑出 `covered`。
- **工具修复**：`devlogs/_verify/mutation_harness.py` 的 `expect` 现在容忍**参数化后缀**
  （`...::test_x` 可命中 `...::test_x[0]`）—— 此前会把**已抓到的变异**判成 MISSED
  （本轮实测踩到，且首次归因还怀疑错了对象）。
- 陷阱固化到 `docs/PROJECT_PITFALLS.md`：只读锁判据为何不能用于目录；
  "有始无终"= 被杀签名；`CommandNotFoundException` 会绕过 PowerShell 的失败出口；
  **变量名/请求体都不得绑定提供方**；覆盖清单 `skipped ≠ covered`。
- 🔴 **本轮出现的操作事故（已定位并固化，非产品问题）**：用
  `cat >> .workbuddy/memory/2026-09-23.md <<'EOF' …` 追加日志时，
  **本环境的 shell `>>` 丢掉了 `O_APPEND` 语义** ⇒ 新内容**从 offset 0 覆盖写且不
  truncate**，旧文件 4178 B → 5563 B，**该日日志 Round 55 正文的开头被覆盖**
  （只在末尾残留 25 行）。最小复现 `devlogs/_verify/_append_probe.txt`：
  38 B 文件追加一行后**仍为 38 B**、新行在开头、旧内容尾部残留。
  该目录被 `.gitignore:37` 忽略、从未入库 ⇒ **无备份可恢复**；已按
  `CHANGELOG`/`TODO`/`MEMORY.md` 如实**标注重建**，不冒充当轮记录。
  ⇒ PITFALLS §七 的规矩从"中文文件禁 `cat >>`"升级为
  **"任何文件都禁 shell 重定向追加"** —— 且**理由改正**（原写"CP936 乱码"，
  真实机制是无 `O_APPEND`；理由写错会让人在"这次没编码问题"时放心破例）。

### Added / Verified (Round 57, 2026-09-23 — 产物级 e2e：补上「用户双击的那一份」应用层验收)

> 起因：「对 `dist-electron-out-20260921-160111/win-unpacked/` 做多次端到端测试，并设立评价标准」。
> **结论先行**：该目录**此前从未被端到端驱动过** —— 既有 `e2e_frozen.py` 直接跑内嵌
> `pbc-server.exe`（不经 Electron），Round 55 的关闭探针同理。本轮补上应用层，并先
> 用 12 轮探针定位了一件**会让整个产物"起不来"的环境性事实**（见下第 1 条）。

- 🔴 **B9-10 根因（本轮最重要的事实）**：宿主 WorkBuddy 自身以
  **`ELECTRON_RUN_AS_NODE=1`** 运行 Electron daemon，该变量**继承给所有子进程**
  ⇒ `BatchSentry.exe` 以**纯 Node 模式**启动：不加载 `app.asar`、不起 Chromium、
  **0.2–0.7 s 静默 `rc=0` 退出**、零输出。旁证一次性对齐：`--version` 打印
  `v20.18.3`（**Node 版本**，不是 Electron 版本）、`--xxx` 报 `bad option`+`rc=9`
  （Node CLI 行为）。清除该变量后**产物立即正常**：`/health` 5.1–5.7 s 就绪、
  `version=1.2.0`、拉起内嵌 `pbc-server.exe`、渲染进程
  `--app-path=…\resources\app.asar`、主窗口
  `'BatchSentry — 批记录辅助审查'`、关窗后**端口 0.5 s 释放 / 进程 1.4–2.3 s 退出
  `exitCode=0`**、`-wal`/`-shm` 已 checkpoint 收敛。
- **新增 `tests/e2e_unpacked.py`**（应用层驱动，D1–D8 八维）：D1 启动可达、
  D2 版本与 `PROVENANCE.txt` 自证一致、D3 拉起的是**内嵌**后端、D4 渲染进程加载
  `app.asar`、D5 主窗口就绪（splash 收敛后）、D6 `WM_CLOSE` → 端口释放 + 进程
  `exitCode=0`、D7 `close_db`/checkpoint 收敛、D8 数据隔离真实性。
  **实测 5 次重复：35 passed / 0 failed / 5 skipped，零 flaky**
  （boot 5.09–5.70 s、主窗 1.01–1.02 s、端口释放恒 0.5 s）；证据
  `devlogs/e2e_unpacked_<ts>.json`、覆盖清单 `devlogs/e2e_unpacked_coverage_*.json`。
- **D8 如实报 SKIP（不冒充"隔离完全"）**：真实 `%APPDATA%\PBC\logs\backend-boot.log`
  每次都会被写 —— Electron 的 `bootLog` 走 `app.getPath('appData')`
  （`SHGetFolderPath`），**不受 `APPDATA` 环境变量影响**；隔离因此需要**两把钥匙**
  （`--user-data-dir` 给 Chromium + `APPDATA` 给 Python 后端）。
- **新增 `tests/unit/test_e2e_unpacked_guard.py`（16 条结构断言）** 把驱动里
  "每一条都是踩出来的"约束钉死：必须删 `ELECTRON_RUN_AS_NODE`、两把隔离钥匙齐备、
  关闭前等窗口收敛为 1、存活判据必须内核级、Win32 常量正确、产物解析不得写死
  `dist-electron/`、D8 有变化只能 `skip` 不得 `ok`、`_reap` 必须**先确认存活再强杀**、
  快照必须递归、记账状态只能来自 `tests.e2e_coverage`。
- **变异验证 `devlogs/_verify/mutate_b910.py`：14/14 CAUGHT**（M1–M14 逐条对应上表）。
  ⚠️ 首轮 **M12 MISSED** 并当场暴露**护栏自身的弱点**：只断言"函数里出现过
  `kernel_alive`"会被 `_reap` **结尾那次复核调用**掩盖 ⇒ 改为 **AST 顺序判据**
  （`kernel_alive` 与 `if not alive: return` 的行号都必须**早于** taskkill）；
  另一处缺口是 `dir_snapshot` 递归性**当时没有护栏**（M14 会 MISSED）⇒ 已补
  `test_dir_snapshot_is_recursive`。
- **新增 `docs/E2E_UNPACKED_ACCEPTANCE.md`**：评价标准（判据/实测方法/通过条件/
  失败归因口径逐条写清），并明确**已知盲区**（D8 bootLog、本轮未验 LLM/OCR 成功路径）。
- 陷阱固化到 `docs/PROJECT_PITFALLS.md` **§三十八**（六条）：宿主环境变量污染子进程；
  GUI 子系统 exe 无 stdout（"零输出"≠"没跑"）；隔离两把钥匙；splash 陷阱让
  "关闭后残留"成为**探针缺陷**而非产品缺陷；存活判据要内核级且**自身要做正负对照**；
  变异验证会暴露**护栏自身**的弱点（"出现过"≠"用对了"，要用顺序判据）。

### Added / Fixed (Round 53, 2026-09-21 — P2 批次④：精度/准确性 + 配置与口径 + e2e 覆盖)

- **B1-16 `_parse_time` 不认识「仅时刻 + 中文单位」与「N日H时M分」形态**（P2，精度）：
  真实语料里"看起来是时刻"的 162 个字面量中 **59 不可解析**，其中 **51 个是形态本体不被支持**
  （`09 时 00 分` / `21日07时49分` / `20 日 18 时 06 分`）⇒ 规则层 R1/R1b 与
  `llm_finding_guard` 的时序复算**同时饿死**，"判不了 ⇒ 降级"成了这些页的唯一结局。
  新增两条解析分支，并立三条**保守约束**：分钟必须两位（否则 `13时6` 与"被截断的数字"
  不可区分）、**整串锚定**（否则 `15时36分008232-2412017` 会被截出时刻而误收）、
  `N日` 与生产日期相差 **>1 天一律拒绝**（**不猜月**）。
  同时修掉一个同源缺口：精度判定正则 `_TIME_OF_DAY_RE` 不容忍空白 ⇒
  同一种形态只因空格被判成两种精度 ⇒ 区间被放宽 ⇒ **时间倒序被系统性漏报**。
  **实测**：可解析 **103 → 128**、不可解析 **59 → 34**、形态不支持 **51 → 12**
  （余 8 条按设计必须拒：体积 `13:6 m^3` / 区间 / 批号日期粘连；4 条因**生产日期本身不可用**）；
  真实 51 页回放 **总数 487 → 487、critical 38 → 38**（净中性）：
  −1 条 p17「时间格式无法解析」（修复的直接证据）、+1 条 p38 warning 经人工核原页判为
  **假阳性**（转置表污染 `time` 槽，治本在 B1-14）。变异 11/11 CAUGHT。
- **B1-15① R8「勾选为否」遇空框标记不再断言"勾选为否"**（P2，假阳性）：
  真实语料里有一档记录**内部自相矛盾** —— `selected='否'` 却配 `marker='☐'`（**空框**），
  一个"被选中"的选项不可能配空框 ⇒ 该 `selected` 不可信。新增
  `_EMPTY_BOX_GLYPHS = frozenset("☐□◻○◯")` 判据（**整串都是空框**才算矛盾；
  `√☐` 混合 / 含勾号 / 空白 / 缺失一律 **fail-open** 维持原判），命中时**降为 info**
  并如实说明"勾选标记与选项不一致 ⇒ 该选项不可信，请对照 PDF 原页人工核对"。
  **实测**：**消掉 5 条假阳性 warning**（warning **295 → 290**、info **154 → 159**，
  总数 487 与 critical 38 不变）。⚠️ 影响面**比登记时知道的大 5 倍**
  （原条目只记 p45 ×1，全语料回放实测 p9 / p14 / p24 ×2 / p45）。
  变异 6/6 CAUGHT（含"字形集过宽/过窄""缺失被当矛盾"等反向对照）。
- **B1-15 同轮：R8「勾选为否」文案改中性（不再替用户判断"否"是好是坏）**（P2，语义）：
  R8 的语义前提对**否定式措辞**的检查项不成立 —— `是否发生偏差 / 是否发生 OOS/OOT /
  是否发生变更` 答「否」**本来就是正常答案**，旧文案却说"需确认是否已启动偏差处理
  并在记录中留痕" ⇒ **方向反了**。⚠️ 修正方式**不是**加"是否发生/无异常"这类**措辞词表**
  （那正是下面 B1-12 修掉的病），而是**不猜极性**：文案只陈述事实 + 请人工确认
  （"……请对照 PDF 原页确认该答案是否为预期（否定式提问的'否'通常即正常答案）"），
  **定级不变**（对"地面是否干净"这类肯定式题面，否仍值得人看一眼）。
  **实测**：真实 51 页**定级分布完全不变**（只改文案、未动判定）。变异 7/7 CAUGHT。
- **B1-1 同根因 finding 聚合降噪**（P2，可读性/准确性）：
  单个 OCR 误读会在**每个时间点各产 1 条** finding（实测某 job 全量 442 条；
  `进料压力` 在 7 个时间点均为 `46bar` ⇒ R3 产 7 条）。⚠️ 关键定位结论：
  **不能用描述文本反推聚合键** —— 粗口径（page+type+前 60 字）会给出多数**假合并**
  （"缺少 QA 签名"与"缺少复核人签名"是**不同根因**，合并会丢信息）。
  处置：新增 `core/rules/finding_aggregate.py`（**单一构造点 + 派生键**），
  键含 page/type/kind/主语/**根因**/取值/单位/规格；**位置是变化维度，故意不进键**
  ⇒ 取值或根因不同者**不会被合并**。
- **B1-7 规则层基准年不再随运行时刻漂移 ⇒ finding 可复现**（P2，可复现性）：
  原用 `datetime.now().year` 当基准 ⇒ **同一份 PDF 在不同年份重分析会得到不同结论**。
  ⚠️ 判据方向本身是对的（记录里出现未来日期确实可疑），问题在**基准未固化**。
  改为：`record_production_year`（记录内**多数票**取生产年，平票取小年）与
  `_baseline_year`（优先级 **显式入参 > 记录年 > 当前年**，回退当前年时**在文案里注明**）。
  ⚠️ **保留"绝对未来"判据**（防静默削弱）：记录**整体**落在未来时仍须判出，
  并**如实声明**该判据依赖运行时刻。
- **B1-10 跨页批号把 OCR 变体当成不同批号**（P2，假阳性）：
  库里那条 critical 写「检测到 **5 组**不同批号」，而日志明写 **`0 voted`**
  —— 即旧投票**一次都没触发**。逐串核对后 5 组里 **4 组是同一批号** `1127011N250101`
  的 OCR 写法差异（空格分隔 / `N` 后的**角标被读成 `4`** / `N ` 被读成 `1/` / `N ` 整段丢失），
  且**已渲染原页核验**。处置：**批号身份分解 + 结构优先归并**（含日期段不同者**不合并**）。
- **B1-12 布尔/勾选类的处置档位由「措辞是否撞上词表」决定**（P2，假阴性/fail-open）：
  原实现拿文案去撞词表定档 ⇒ 同一事实因**措辞不同**得到不同处置。
  实测 p39 / p45 / p46 三页**原页真值都是「√ 是 / □ 否」（勾的是"是"）**，
  而 LLM 主张"参数值为否" ⇒ 三处**都是 LLM 读反**，本就该得到**同一种**处置。
  ⚠️ 旧记载的根因**不准**（本轮实测更正）：p45 的 fail-open 不是词表造成的，
  而是**取值器只认一种措辞**（文案里根本没有「值」字 ⇒ 取不到值 ⇒ fail-open）。
  处置：改为**类型闸门**（处置档位不再由措辞决定）。
- **B1-13 时间倒序判据缺"行级溯源" ⇒ 代价评估后决定不做**（P2，文档）：
  产出 `docs/B1-13_ROW_TRACEABILITY_ASSESSMENT.md`（只读取证，未改生产代码）。
  结论：`source_row` 方案**否决**；评估里的两处替代动作分别转入
  **B1-16**（解析器形态，性价比最高）与 **B1-14**（列级"值 → 列"绑定）。
- **B4-3 配置双轨 ⇒ "死 key 假红" 与自查盲区**（P2，可诊断性）：
  开发模式读**仓库根** `config.json`、冻结版读 `%APPDATA%/PBC/config.json`，
  两份装的**不是同一把凭据**（仓库那份已吊销，实测 `401 {"code":30014}`；
  `%APPDATA%` 那份可用）⇒ 开发模式/走仓库配置的 e2e **静默拿到死 key**，
  与"凭据真有问题"**不可区分**。处置：让配置来源**可区分、可显示**
  （`CONFIG_SOURCE_APPDATA`）。
- **B4-5 KB 条目数口径虚高 + 文档两套数字并存**（P2，口径）：
  门禁报 **477**、检索器实际 **441**，差 **36** = 4 个 JSON 的 `chapters`
  （**纯章节标题元数据**：无正文、无 `entry_id`、**检索器根本不索引**）
  ⇒ 指标失真 + 文档据此写"477 条"，**夸大规模 8%**。
  处置：`chapters` **分列且不计入**语料规模（仍透明列出章节标题条数）。
- **B5-4 生产模型路径未被 e2e 覆盖**（P2，验证补强）：
  `e2e_run.py` 原**硬编码** `Qwen/Qwen2.5-72B-Instruct`，而本机生效的生产配置是
  `deepseek-ai/DeepSeek-V3.2`（两者协议一致但**模型不同**）⇒ e2e 从未覆盖真正的生产路径。
  处置：支持 `--model` / `PBC_E2E_MODEL` 覆盖。

> **本版新登记（未修，见 `docs/TODO.md`）**：**B1-14**（`清场批号`/`产品批号` 混用同一
> `batch_no` 字段 ⇒ 需抽取层补字段出处；实测 `page_info` 只有 5 个键、**无任何出处**，
> "清场批号"字样在全库结构化 JSON **0 命中** ⇒ **规则层修不了**，验证须重新抽取）、
> **B1-15②**（`否` + `√`/`☑`，记录内部自洽、只有原图能证伪；真实语料仍存 11 条）、
> **B1-17**（p39 同页同项 finding **完全重复发射** ×2）、
> **B8**（发版收尾不变量清单）。

### Added / Fixed (Round 52, 2026-09-20 — B7-1 / B7-3 / B7-4：让「产物陈旧」无法静默通过门禁)

> ⚠️ **本批改动不进入产物**（只落在 `scripts/` / `tests/` / `build.ps1` / 文档 —— 实测
> 产物 `_internal/` 内没有 `scripts/`）⇒ 按 Repo hygiene 规则 9 **不升版号**。
> **但门禁本轮刻意是红的**：新增的 `artifact_freshness` 立刻把**既有的陈旧产物**照了出来。
> 这不是回归，正是它要证明的能力 —— 须随 v1.2.0 重建产物后转绿。

- **B7-3 新增门禁第 9 项 `artifact_freshness`（P2，护栏缺口）**
  （新 `scripts/bundle_manifest.py` + `scripts/release_gate.py`）：
  **「版本号一致」不蕴含「产物是新构建的」**。实测铁证：源码 `static/settings.js`
  77547 B（含 `auto_activated`）vs 产物 `_internal/static/settings.js` 77231 B
  （仍含**已删除**的 `firstConfigured`）—— 两侧版本号**都是 1.1.9**，而
  `release_gate.py --skip-tests` 照样 **7/7 全绿**。机制上三层叠加都漏：
  ① `pytest.ini` 的 `testpaths=tests` + `python_files=test_*.py` ⇒ `tests/e2e_*.py`
  **不在收集范围**；② 唯一依赖产物的 `test_frozen_smoke.py` 只验「能启动」；
  ③ 唯一含版本断言的 `tests/e2e_frozen.py` 恰在收集范围之外 ⇒
  **改了进产物的模块却不重建，可静默通过全部门禁**；版本号恰好一致时连「人眼扫一眼」
  这条兜底也失效。
  修法：构建收尾把**每个入包源文件**的 sha256 写进 `<artifact>/build_manifest.json`，
  门禁拿工作树与产物副本**逐字节**比对。**六条判据**：入包集合覆盖 / 版本（AST 读
  `main.py`，不导入模块）/ 工作树 vs 清单 / **产物副本 vs 清单** / asar 成员与 asar 版本 /
  exe 绑定。**SKIP 语义**：无产物 ⇒ SKIP（≠ PASS，"没有产物"不构成"产物新鲜"的结论）；
  **有产物无清单 ⇒ FAIL**（"无法证明新鲜度" ≠ "新鲜"）。
  ⚠️ **纠正两处旧记录**：**(a)** 「比对 `main.APP_VERSION` vs 产物内 `package.json`」
  **对 PyInstaller 产物不成立** —— `dist/pbc-server/` 里没有 `package.json`，
  它只在 electron 的 `app.asar` 里、且**被 electron-builder 重写**过（源 ~1.6 KB vs
  包内 255 B）⇒ 只能比 `version` 字段，**字节比对必假**。**(b)** `core/`、`api/`、
  `config.py`、`main.py` 在产物里**根本没有对应字节**（编译进 exe 内的 PYZ）⇒
  那一层只能靠「工作树 == 清单」；该**盲区已在模块 docstring 里显式声明**
  （判不了 ≠ 判据确凿）。
- **B7-4 打 tag/发版补上「产物新鲜度」前置闸**（P2，由 B7-3 派生）：
  清单生成点接进 `build.ps1` 的 **2.6 步**，时机**刻意**卡在 PyInstaller 之后
  （绑定 exe 字节）、electron-builder 之前（随 `extraResources` 进
  `resources\pbc-server\`）⇒ 任何一份产物都能**自证**出处；写完**就地自校验**
  （把"门禁才发现"提前到构建现场）。发版前置闸写进 `CLAUDE.md` 规则 11（与门禁同判据）。
- **B7-1 全量测试的沙箱删除预算 flake（P2）——机制上消除 + 修掉一个更严重的护栏漏洞**：
  ① `run_cmd` 给**子进程**注入 `CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD=100000`
  （必须**强制赋值**：宿主已显式给 50，`setdefault` 是空操作）；只抬阈值、**不清安全网**
  （删除仍走回收站）⇒ 比 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 更保守。
  ② 🔴 **顺带修掉真漏洞**：`env_only` 原先**只按 nodeid 前缀**降级 ⇒ `TestServePdf` 里
  任何**真实回归**（403 变 200）都会被降级成 WARN、门禁退出码为 0。现要求
  **前缀 + 签名**双命中（`SystemExit` **且** safe-delete 标记串，标记串取自 shim 源码
  常量），签名取不到时**不降级**（fail-closed）。
  ③ 文档固化真变量名（旧文档的 `BULK_THRESHOLD` **不是**真变量名）。

护栏：`tests/unit/test_bundle_manifest.py`（33 例：入包集合 / hash 原始字节 / AST 版本 /
asar 读取含**数据基址不变式反证** / 清单读写 / 校验器含「落后一个提交 ⇒ 红，同步 ⇒ 绿」）
与 `tests/unit/test_release_gate.py::TestArtifactFreshness` / `TestSandboxDeleteIsolation` /
`TestEnvOnlySignature`。**变异 15/15 CAUGHT**（`devlogs/_verify/mutate_b73.py`，逐字节还原）。
顺带修正 `tests/unit/test_declared_dependencies.py` 的**守卫盲区**：`_is_local` 原先不认
「与导入方**同目录**的模块」⇒ 仓库自己的 `scripts/*.py` 互导被判成"未声明的第三方依赖"，
而误报的修法是**把本地模块塞进 requirements.txt**（那才是真的污染供应链清单）。

### Fixed (Round 51, 2026-09-20 — P2 批次③：并发与配额 B2-7 / B3-2)

> ⚠️ **本批改动进入产物**（`api/jobs/`、`core/`）⇒ 工作树继续**领先于产物**，
> **不单独升版**，随 v1.2.0 统一升版重建。

- **B2-7 并发上限存在 TOCTOU（P2）**（`api/jobs/upload.py`、`api/jobs/actions.py`、
  `api/jobs/__init__.py`）：配额 `COUNT` 在函数开头、`INSERT` 在其后，中间隔着
  **写盘 + PDF 解析**（大文件可达分钟级）。实测（4 个上传同时卡在两道检查之间、
  `MAX_CONCURRENT_JOBS=1`）**4 个全部成功** ⇒ 上限形同虚设。修法：把**授权式**检查
  放进 `INSERT` 的**同一把 `db_lock`**（中间不释放锁、无 await 让出点）⇒ 对外原子；
  开头那道检查**明确降级为"尽力而为的前置快检"**（只为避免让用户白传大文件）。
  抽出 `_count_active_jobs()` 作**单一实现点**，upload（两道）与 retry 共用。
  **未采用"占位 INSERT"**：占位行需要在**每条失败路径**上删除，漏一条就留下
  `pending` 且无 task 的**幽灵任务**（永久占额度、看门狗不收敛）。写盘仍在锁外
  ⇒ 性能未退化（锁内只多一次 COUNT）。
- **B3-2 procpool 并发度 1 → 2（P2，实测标定）**（`core/procpool.py`）：实测单 worker 下
  "1 个 job"与"3 个 job"墙钟**相同**（1.40s vs 1.41s ⇒ 并发被完全串行化），而 3 并发
  job 时 1→2 worker 让墙钟 **1.41s → 0.85s（1.64x）**，每 worker ≈60MB；提到 3 的增量
  只有 1.21x 而再 +65MB ⇒ 取 **2**。另有与负载无关的理由：worker 极少时一个挂死的
  worker 会占住槽位（worker=1 时即占满 ⇒ 故障从"某个 job 卡住"扩散成"整个应用不再
  处理新任务"），取 2 使爆炸半径减半。同时**更正一处文档说谎**：原注释称"多 worker
  只会增加内存而无吞吐收益"，实测**只对单 job 成立**，已按数据更正。

护栏：`tests/integration/test_api_jobs_upload.py::TestUploadQuotaIsAtomic`（闸门**确定性**
复现 TOCTOU 窗口 + **反空断言** + 正向对照）与
`tests/unit/test_procpool.py::TestPoolConcurrencyIsCalibrated`（标定值 + **AST** 单点判据）。
**变异 4/4**（`devlogs/_verify/mutate_b27.py`、`mutate_b32.py`，逐字节还原自校验），
其中 B2-7 的红灯原因已人工核对（正是"4 个全成功"）。
另记 **B2-14**（同族未修）：`retry` 的守卫与状态迁移之间同样有 TOCTOU —— 因
`transition_status` 自取**非重入**的 `db_lock`，不能"持锁再调它"（会死锁），
需改用 `_transition_status_unlocked`，故单独处理。

### Fixed (Round 50, 2026-09-20 — P2 批次②：B2-10 前端「过渡期与口径」4 处)

> ⚠️ **本批改动进入产物**（`api/jobs/`、`static/`）⇒ 工作树继续**领先于产物**，
> **不单独升版**，随 v1.2.0 统一升版重建。

- **① `type=error` 两类帧被混为一谈（P2）**（`api/jobs/status.py` + `static/review.js`
  + `static/status.js`）：服务端本就发两类**语义相反**的 error 帧 ——「进度查询失败」
  发完 `continue`（瞬态、应重试）与「任务不存在」发完 `return`（终态）—— 而复核页只判
  `d.type === "error"` ⇒ **一次 DB 抖动被谎报成"任务已被删除"，且进度流永久断开**
  （既丢实时更新，又理由说谎）。改为**由服务端下发显式判据**：两类帧各带 `terminal`
  （`False`/`True`），前端经 `PbcStatus.sseErrorAction(d)`（`static/status.js` 新增纯函数）
  分支 —— 只有 `terminal` 关流；瞬态改为提示「进度查询异常，重试中…」并**保持长连**。
  ⚠️ **判据不得按 `message` 文案**（文案属展示层，改文案会静默改变控制流）；
  字段缺失按**瞬态** fail-safe，服务端必带该字段由集成用例锁定。
- **② 终态过渡期文案漏中文映射（P2）**：`review.js` 的 `else` 分支此前落**裸英文
  token**（`review`/`partial_review`/`done`），与同页中文徽章自相矛盾 ⇒ 改走
  `PbcStatus.statusZh(d.status)`。
- **③ error 转态期不显示原因（P3）**：新增 `showJobErrorBanner()`，按需创建/更新 SSR
  的**同一条**横幅 `#job-error-banner`（幂等；文案与 SSR 逐字一致；原因走 `textContent`）；
  终态帧用 `d.message`、普通帧用 `d.error_message` ⇒ **转态即刻可见原因**，不必等
  1.5s 自动刷新后由 SSR 给出。
- **④ 状态中文映射第 3 份副本（P3）**：删除 `review.js` 内联 `statusZh`，徽章与进度文案
  统一走共享件（此前**文字与颜色不同源**：颜色早已走 `PbcStatus.statusDotClass`）。

护栏：`tests/unit/test_status_js.py` 新增 4 类共 **16 条** —— `sseErrorAction` 行为判据
（node 实跑，可被变异打红）、错误分支「**包围条件 + 相对位置**」结构化判据
（`es.close()` 必须晚于 `terminal` 判定）、提取器的**正向对照**（防空断言）、
Python/JS 状态**键集**覆盖；集成侧锁定两类帧**必带** `terminal`。
**变异验证 9/9**（`devlogs/_verify/mutate_b210.py`，逐个逐字节还原自校验）。
过程中还修掉一个**自己写的空断言**（只查"标识符出现过"⇒ 被条件行满足），
教训固化进 `docs/PROJECT_PITFALLS.md` **§三十**。
另记 **B2-13**（本轮新发现）：SSR(Python) 与 SSE(JS) 的 job 状态**中文措辞不一致**
（`识别中` vs `OCR 解析中`、`可复核` vs `待复核` 等）—— 本轮只锁**键集**，
**未统一措辞**（会改变用户可见文案，需单独评估）。

### Fixed (Round 49, 2026-09-20 — P2 批次①：日志/理由说谎、静默失败、fail-safe)

> ⚠️ **本批改动进入 PyInstaller 产物**（`core/pipeline/`、`core/rules/`）⇒ 工作树
> 继续**领先于产物**。**不单独升版**，随 v1.2.0 统一升版重建。

- **B2-6 `measurements=` 恒为 0**（`core/pipeline/stage2.py`）：该字段**嵌在
  `steps[]` 内部**（顶层根本没有这个键），旧写法读顶层 ⇒ 逐页日志恒为 0，
  把排障引向一个**不存在**的"LLM 完全没提取到测量值"故障。改为从 steps 汇总，
  并**同时打印 `steps=` 与 `measurements=`** 两个口径（避免下次再被单一数字误导）。
- **B2-8 分片逐片日志把"跨片累计"当"本片新增"**（`core/pipeline/engine.py`）：
  片内新增 `slice_new`，日志改为 `persisted (N this slice, M total)`；`:611` 的最终
  汇总仍用累计值（那是对的，未改）。
- **B2-12 L2 溯源把系统注入的「当前年份」当幻觉数字**（`core/rules/llm_finding_guard.py`）：
  `_check_grounding` 新增 `exclude_dates`，按**年份**排除文档级自述当前日期。
  **定位结论更新（勿再按旧描述报）**：Round 46 记录的现象**已被 B1-4 ① 顺带消除** ——
  51 页真实回放实测 21 条「当前日期/当前年份」条目中 15 条被正确抑制、6 条 fail-open，
  L2 降级中命中该日期池的**残留 = 0 条**（原为 14/19）。本参数是**防御性第二道**：
  ① 一旦因凑不出两侧字面量而 fail-open，L2 的理由也不会变成假话。
- **B2-1 损坏的 `structured_json` 被当成"已分析"**（`core/pipeline/stage2.py`）：
  `json.loads` 失败后 `except: pass` 紧接着落到 `analyzed.add(page)` ⇒ 该页被**永久**
  当作已分析，retry 不再重跑且**零日志**。改为视为**未分析**（retry 可重跑）+ warning。
- **B2-2 三处诊断静默失败补日志**（`core/pipeline/stage1.py` ×2、`self_heal.py`）：
  控制流一律不变（软失败语义保持）；`self_heal` 的既往诊断读取**顺手抽成单一实现点**
  `_load_prior_diagnostics()`（原为内联 14 行双 `except: pass`，不可单测）。

**测试**：新增 **9 例**护栏（`TestLogTruthfulness` 2 + `TestSilentFailureLogging` 3 +
`TestL2GroundingExcludesSystemDates` 4）。断言直接打在**日志文本里的数字**与
**排除集是否真的生效**上，并成对给出**正向对照 / 反向控制**（既防"空断言"，也防
"把判据打哑"）。**变异验证 8/8 全被拦住**（`devlogs/_verify/mutate_b2_logs.py` 4 +
`mutate_b2_silent.py` 4；逐字节还原 + `finally` 自校验）。
受影响测试文件 **163 passed**（test_pipeline / test_llm_finding_guard / test_config_error_visibility）。

### Changed (Round 47, 2026-09-20 — B3-4：写路径单一实现点 + 全仓生产死接口清零)

> ⚠️ **本轮改动进入 PyInstaller 产物**（`core/rules/`、`core/pipeline/`、`core/kb/`、
> `core/finding_quality.py`）⇒ 工作树继续**领先于产物**。**不单独升版**，随 v1.2.0 统一升版重建。

- **B3-4 修复**：`core/rules/llm_finding_guard.py::apply_review` 原为**生产死代码**（零消费点），
  而 `stage2.py` / `stage3.py` 各自手抄了同一段「重建 findings + 物化降级 severity」逻辑，
  且**已漂移**（`apply_review` 对 description 做了 `.rstrip()`，两处调用方没有）。
  ⇒ 两处改为统一调用 `apply_review`，**重建逻辑只剩一个实现点**，漂移随之消除。
- **同型重复一并统一**（本轮全仓扫描新发现，形态均为"函数是死的、但生产侧内联了同一逻辑"）：
  - `kb.store.kb_version()` 原内联 `srcs[sid]['_version']` ⇒ 改走 `source_version(sid)`；
  - `kb.store.entries()` 无参分支原内联"各源并集" ⇒ 改走 `entries_by_source()`；
  - `finding_quality.normalize_finding_type()` 原内联 `t in CANONICAL_TYPES` ⇒ 改走 `is_canonical()`。
- **纯死接口删除**（零消费、且**无可统一的重复**）：
  - `core/rules/registry.py::rule_by_type` —— ⚠️ 与 `rule_coverage` 的 `by_type` **语义不同**
    （前者含被禁用规则、后者只含启用）⇒ **不可合并，只能删**；连同其直接单测一并移除；
  - `core/pipeline/locks.py::begin_children` —— **冗余第二构造入口**（`ChildTasks(job_id)`
    已被 `engine.py` / `stage2.py` 直接用）；并修正那条**推荐使用它**的用法注释；
  - `core/rules/year_vote.py::vote_report` —— docstring 称"可审计摘要（落日志 / audit）"
    却**从未被调用** ⇒ 它的存在本身在**误导**"跨页年份投票已可审计"。缺口另登记 **B3-5**。
- **护栏（防再复制）**：`tests/unit/test_llm_guard_single_impl.py`（6 例）——
  正向断言 stage2/stage3 必须调用 `apply_review`；反向断言 `core/pipeline/` 不得直接调
  底层引擎 `review_llm_findings`、不得出现物化 token `to_severity`；并带**正向对照**
  （token 必须确实存在于唯一实现点，否则反向断言是**空断言**）。
  断言走 AST `ast.Constant` **精确等值** —— pipeline 里 `｜` 另有合法用途（错误文案），
  文本搜索会假红。
- **变异验证**：把重建逻辑抄回 stage2 ⇒ **三条断言全红**；改掉标记 token ⇒ **正向对照红**；
  还原后全绿，且**逐字节还原自校验**通过（脚本改递归字节级，见 `PROJECT_PITFALLS.md` §二十四）。
- **验收**：全量 `tests/unit + tests/integration` = **2776 passed / 0 failed**
  （基线 2771 + 新增护栏 6 − 删除的直接单测 1）；全仓死接口扫描重跑 = **生产死接口 0**。

### Fixed (Round 48, 2026-09-20 — B4-4：provider 自动改写收权 + 界面可见)

> ⚠️ **本节改动进入 PyInstaller 产物**（`config.py`、`api/settings/`、
> `static/settings.js`）⇒ 工作树继续**领先于产物**。**不单独升版**，随 v1.2.0 统一升版重建。

- **定位（比原记录更收敛）**：auto-activate 实际有**两份实现** —— 后端
  `config.load_config()`（import 期切换 + 落盘）与前端 `settings.js.load()`。
  后端先切完 ⇒ 前端条件恒不成立 ⇒ **那条唯一带提示的路径（`autoReason`）被绕过**
  ⇒ "界面零提示"的真实机制是**第二实现点屏蔽了第一实现点的提示**。
- **单一实现点**：新增 `config._resolve_active_provider()`；**删除前端那份本地切换**
  （连带 `opts.autoReason` / `firstConfigured`），前端改为
  `showAutoActivateNotice(current.llm.auto_activated)` —— **只呈现、不决策**。
- **回退条件收紧**：仅在「用户**未显式选择**（配置里无 `LLM_PROVIDER`）
  **且** active 的 Key **字面为空** **且** 另有 provider 的 Key 字面非空」时回退；
  用户**显式选过**则**绝不改写**（只上报，由界面提示）。
- **`_is_real_key` 改精确校验**：子串 → 「精确值 + 模板前缀（**前缀后必须是非字母**）」；
  统一 `api/settings/read.py` 里**手抄的副本** `_is_real_api_key`；
  删除**零消费**的旧名 `TEST_KEY_PATTERNS`。
- **界面可见**：`GET /api/settings` 新增 `llm.auto_activated`
  （`{applied, from, to, reason}` 或 `None`），前端渲染到 provider 徽标与消息区。
  ⚠️ 必须**调用期**读 `AUTO_ACTIVATE_NOTICE`（`from config import ...` 是导入期取值，
  状态变化读不到 —— 被集成测试当场抓住）。
- **有意保留的取舍**：切换判定**不再**使用启发式 ⇒ 带**占位 key** 的 provider
  也会成为回退候选。刻意如此：占位 key **响亮失败**（401，可追溯），
  启发式误判是**无声**的。启发式保留在显示层（`configured`）。
- **自踩并当场修掉的两个同类错误**（由新护栏抓出）：`sk-ant-test` 作前缀会命中真实 key
  `sk-ant-testing-...`（**修缺陷时又犯同一类缺陷**）⇒ 改为"前缀后接非字母"；
  `sk-your-api-key` 是词模板 ⇒ 移到精确值表。
- **验收**：`test_config_internals.py` 4 个决策用例（含"持久化即失败"的反向断言）+
  `TestIsRealKeyExactMatch` 6 例；新增 `test_settings_auto_activate.py`（单一实现点 +
  界面可见性机检，**AST 判"被调用名字"** + **正向对照**）；
  `test_api_settings.py` 新增 2 例（`auto_activated` 契约两个取值都验）。
- **变异验证**：`devlogs/_replay/mutate_b44.py` **5/5 通过**（改回启发式 / 去掉显式选择闸门 /
  不记录决策事实 / 删前端渲染调用点 / `_is_real_key` 改回子串 ⇒ 各自打红对应用例），
  逐字节还原自校验通过。
- **提交前复核追加（徽标"隐形契约"）**：`#llm-provider-badge` 的**加载期写入点**原本在
  `fillOcrForm()` 内，而 `showAutoActivateNotice()` 排其后 ⇒ 徽标文案取决于**两函数的
  调用顺序**这一隐形契约（重排即静默退回纯名称，且既有断言全绿）。
  现已把加载期写入**收敛到 `showAutoActivateNotice()` 一处**（无通知时也写回纯名称）。
  ⚠️ 该护栏**首版 3 个变异漏过 2 个**：`display(activeProvider)` 被**三元表达式另一分支**
  满足（空断言）、函数开头插 `return;` 使写入成**运行期死代码**。
  判据改为**结构化**（写入前不得有 `return`；写入所在 `if` 的条件不得依赖 `info`）后
  **3/3 全拦**（`devlogs/_lint/mutate_b44b.py`）。详见 PITFALLS §二十八。

### Fixed (Round 48, 2026-09-20 — B1-4 / B1-5：日期语义收权 + 形态闸门补齐变异证明)

> ⚠️ **本轮改动进入 PyInstaller 产物**（`core/rules/llm_finding_guard.py`）⇒ 工作树继续
> **领先于产物**。**不单独升版**，随 v1.2.0 统一升版重建。
> 本轮**只动一个文件**（+ 其单测），是"V 段 P1 三条"里的第 1、2 条。

- **B1-4 ①「参照物是当前日期」不再依赖方向**：`_check_current_date_reference` **新增**，
  并从 `_check_declared_order` 中**整体移走**该语义（后者现在只管"两侧都是记录内日期
  ⇒ 方向错则降级"）。旧实现把"参照物是墙钟"与"方向反"两个**独立**判据挤在一处
  ⇒ **必然**漏掉方向恰好对的条目：实测 23 条当前日期类条目里只收掉 **1** 条，现在收掉 15 条。
  - **反向控制（不可省）**：记录内日期**晚于**自述当前日期 ⇒ 真未来日期 ⇒ 保留。
  - **不引入墙钟**：比较两侧都取**文案自身**的日期字面量（LLM 把"今天是……"写进了文案）
    ⇒ 避开 B1-7（墙钟基准导致跨年份不可复现），也不新增须与规则层同步的阈值表。
- **B1-4 ②「生产日期」基准改为跨页单源**：`_document_current_dates` 在入口处**解析一次、
  全页复用**（禁止各页各解 —— 那正是缺陷本身），`_check_production_date_premise` 据此
  否定"把当前日期当生产日期"的前提（实测 p28「与生产日期 2026-09-18 矛盾」）。
  - ⚠️ **只做否定、不做选择**：实测 51 页有 **23 种** `production_date` 取值、
    **多数值本身就是错的**（`2026-09-18` 占 7 页），且候选里 `2025-01-25`(7 页) 比
    `2025-01-20`(6 页) 还多、批号后缀表明该记录是**多子批复合体** ⇒ **本就没有唯一真值**。
    猜真值正是本缺陷要根除的失败模式；可复现且可证伪的产物只有"**哪些值被证明是当前日期**"。
  - 引用值与**本页**抽取不一致 ⇒ 降级（弱证据，`_check_production_date_mismatch`）。
- **B1-5 补齐变异证明**（**不新增判据** —— 现象 A/D/E/F 已由 B1-6/B1-11 覆盖）：
  `devlogs/_replay/mutate_b15.py` 5 个**定向**变异证明形态闸门与三态判据承重
  （摘"非数值"/"布尔"/"日期形态"闸门、三态退化、`structured` 缺失时退化 ⇒ 各自对应用例红）。
- **验收（真实数据，`devlogs/_replay/verify_b14.py`）**：落库 LLM 行 156 条 ⇒
  抑制 23 / 降级 22 / 保留 87；**TODO 表格 5 场景逐条处置 5/5 符合预期**。
- **⚠️ 验收条款被证伪为空断言**：原条款「0 条 critical 假阳性」在 **L4 无条件封顶**下
  对任何输入都成立 ⇒ **无判别力**。已把该事实写进模块 docstring，并将验收改以
  "5 场景逐条处置是否正确"为准。同类问题在 B1-5 的原变异条款上也出现过
  （「去掉闸门 ⇒ 重新变成 critical」在 L4 之后**不可能**成立）。
- **变异验证（`devlogs/_replay/mutate_b14.py`）4/4 通过**，逐字节还原自校验通过。
- **回归**：全量 `tests/unit + tests/integration` = **2785 passed / 0 failed（300.96s）**
  —— 精确对账：基线 2776 + 新增 9（`TestL3DateSemanticsB14`）= 2785；ruff 全仓 105 = 既有基线，**零新增**。
- **登记的新条目**：**B1-13**（时间倒序缺行级溯源 ⇒ p38 跨行串位只能降级；需改 prompt +
  LLM 输出 schema，P2）；B1-4 记录里另登记 3 项留白（`year_vote` 被污染基准反喂、
  无字面量的"当前年份"条目 fail-open、p32「为未来日期」措辞无参照物字面量）。

### Added (Round 44, 2026-09-20 — B1-6「收权」：判定从 LLM 收回规则层)

> ⚠️ **本节改动进入 PyInstaller 产物**（`core/rules/`、`core/pipeline/`）⇒ 工作树
> **领先于产物**。**当前可分发版本仍为 1.1.9，不含本项** —— 待 v1.2.0 清单主要项
> （B1-4 / B1-5 / B1-7 / B1-9）完成后**统一升版重建**，避免半新半旧的产物。

- **新增 `core/rules/llm_finding_guard.py`** —— LLM finding 的**收权复核器**。
  背景：LLM 同时"自己从 OCR 文本挑值"+"自己下判定"，产生五类假阳性（日期方向反 /
  列串位 / 跨字段串位 / 类型错配 / 凭空数字），根因同一个；再调提示词是治标。
  四层复核（纯函数、零 LLM 调用、零 IO）：**L1 值形态**（布尔值/日期值/非数值 ⇒
  抑制；数值但查无此值 ⇒ 降级）、**L2 溯源**（文案长数字须能在 OCR 原文定位）、
  **L3 判据重算**（复用 `parsing._parse_time_interval`/`_interval_after` 等规则层
  同源代码）、**L4 severity 封顶**（LLM 独断的 critical 一律降 warning 待人工核对）；
  另含 **L1' 推测性表述**（「无法识别」不是异常结论）。
- **接入两条落库路径**：`core/pipeline/stage2.py`（`llm_page`）与
  `core/pipeline/stage3.py`（`llm_cross`/`llm_fallback`）。抑制**必留痕** —— 复用
  `spec_guard.suppression_rows` 唯一构造点写 `finding_suppressions` 台账
  （非空 reason + evidence，可回退、可抽检）；降级理由追加到 description。
- **真实数据回放验证**（Round 42 的 51 页 real 轮隔离库）：152 条 LLM findings ⇒
  抑制 26 / 降级 42，**critical 49 → 0**；p2/p6/p17/p39 四条实测假 critical 全部处置。
  rule 层 6 条 critical **原样保留**（真阳性不误杀）。
- **护栏**：`tests/unit/test_llm_finding_guard.py` 20 用例（样本取自真实页形态）+
  `test_pipeline.py` 链路级 1 例。**变异验证 3 个方向全部变红**。

### Fixed (Round 45, 2026-09-20 — B1-9 规则层把「区间重叠」当成「倒序」并给 critical)

> ⚠️ **本节改动进入 PyInstaller 产物**（`core/rules/rule_time.py`）⇒ 工作树
> **领先于产物**。**当前可分发版本仍为 1.1.9，不含本项** —— 与 B1-6 一并待
> v1.2.0 冻结时统一升版重建。

- **`core/rules/rule_time.py:_check_time_reversal_cross_page` 判据分级**。
  根因：判据是 `curr.start < prev.end` —— 这只说明两个工序**时间区间重叠**，
  而"倒序"的语义应是 `curr.start < prev.start`（工序编号与开始时刻矛盾）。
  实测（Round 44 视觉直读 p48 原页）：`D2101/D2102 真空干燥箱清洗 03:12→03:36`
  与 `烘盘/盘罩/勺子清洗 03:34→04:12` 是**不同设备的两道并行清洗**，重叠属正常，
  却被按 `time_reversal` 给了 `critical`。
  现在：**真倒序 ⇒ critical**；**仅重叠 ⇒ warning**，文案明说"两工序时间重叠
  （若为不同设备/物料的并行操作则属正常，请核对）"。
- **新增"证据不足"封顶（`_evidence_notes`）**：所依据的时刻**为手写填写**
  （命中该 step 的 `handwritten` 标注）、或**字面量缺日期成分**（比较所用日期系由
  该页 `production_date` 推定，不是记录里写的事实）⇒ **一律封顶 warning** 并在
  文案里注明理由。与 B1-6 的 L4 同源：**不确定来源不给最高严重度**。
  同源封顶一并施加到 R1a（页内倒序）。**未新增 finding 类型**（保持
  `time_reversal`），避免牵动 `CANONICAL_TYPES`/`TYPE_QUERIES`/前端 zh_map 多处同步。
- **实测验收**：真实 51 页回放 ⇒ p48 那条 `critical → warning`（文案含"重叠"与
  "手写"说明）；全语料 R1b 仅此 1 条，无其他 finding 变化（未弱化检测）。
  **反向断言**：真倒序用例（p9/p10）**仍为 critical**。
- **护栏**：新增 `tests/unit/test_time_reversal_evidence.py`（33 例，样本取自
  Round 42 真实隔离库）。**变异验证 4 个方向全部变红**：重叠打回 critical /
  去掉 R1b 封顶 / 复现"日期与时刻粘连"bug / 去掉 R1a 封顶。
- **实现期踩坑（已固化为回归用例）**：`_time_is_handwritten` 最初先
  `lit.replace(" ", "")` 再匹配 `(?<!\d)(\d{1,2})[:时](\d{2})` ⇒
  `2025-09-25 03:36` 去空白后粘成 `2025-09-2503:36`，日期末位数字触发反向断言
  `(?<!\d)` ⇒ **整条手写封顶静默失效**（表现为"改了但没生效"）。必须在原字面量上匹配。
- **更正上一轮的一处误读**：原登记写"两处手写日期不同（15日 vs 25日）"，
  实测该页 `handwritten` 字段原值为 `25日03时12分` / `25日03时34分`，
  **两处都是 25 日**。站得住的理由是"并行重叠"与"手写体不直接判定"。

### Fixed (Round 46, 2026-09-20 — 第四轮对抗性审查：收权复核把「判不了」当成「判据确凿」)

> ⚠️ **本节改动进入 PyInstaller 产物**（`core/rules/llm_finding_guard.py`）⇒ 工作树
> **继续领先于产物**。**当前可分发版本仍为 1.1.9（不含 B1-6/B1-9/本节）** —— 待
> v1.2.0 冻结时统一升版重建。

- **审查对象 = 尚未分发的 B1-6 / B1-9**（不是已发布的 v1.1.9）。方法：51 页真实隔离库
  回放 + 直查隔离库核对"被抑制的线索有无兜底"。完整报告见
  `docs/ADVERSARIAL_AUDIT.md` **§16**。
- **【P1】`_check_declared_order` 抑制过强**：把 LLM 的"**方向词**写反"当作"**整条
  finding** 不成立"而抑制。实测 p38「签名时间 **2027**.01.17 早于 生产日期 2025年01月20日」
  抑制后，该页**再无任何条目提及「2027」这个未来日期**，规则层亦无兜底；p27 两侧相差
  **10 年**（年份误读线索），只因同页**碰巧**另有 `year_contradiction` 才没丢。
  **改为按参照物分档**：参照物含"当前日期/当前年份"⇒ 抑制（该比较本身无意义，
  B1-4 型）；两侧都是**记录内日期** ⇒ **降级**（错的只是表述，日期对仍须人工核对）。
  新增 `_CURRENT_REF_RE` —— **不引墙钟**（避开 B1-7：墙钟基准致 finding 不可复现）、
  **不引新阈值常量**（避开"两份阈值表迟早漂移"）。
- **【P1】`_recompute_time_reversal` 由 `bool` 改三态 `bool | None`**：旧版把
  "该页工序**一个都解析不出**"归入 `False` ⇒ 当成"判据确凿地无倒序"而抑制。
  实测 **8 条 L3 抑制里 6 条**该页可解析样本 = 0，其中 **p43×4 / p46×1 的文案明写
  「开始时间晚于结束时间」**（真倒序形态）。现在：有样本且确无倒序 ⇒ 抑制；
  **无样本 ⇒ `None` ⇒ 降级**（fail-open）。
- **实测（51 页回放，job `6f80145a`）**：抑制 **26 → 13**、降级 42 → 55，
  **critical 保持 0**；仍被抑制的 13 条逐条皆强证据
  （p2×2 / p6×2 / p17×4 / p24 / p39×2 / p46×2）。
  ⚠️ **代价（有意取舍）**：p38 的串位幻觉条目也转为降级 —— 该页 0/3 可解析，
  规则层**确实判不了**；按"漏检代价 > 误报代价"宁留 warning 噪声。
- **文档一致性**：删除 `review_llm_findings` docstring 里承诺但**从未实现**的
  `_guard_corroborated` 标记（文档不得承诺不存在的字段）。
- **护栏**：`tests/unit/test_llm_finding_guard.py` 20 → **23** 例（新增"p43 判不了 /
  structured 缺失 / p27 跨度 10 年 ⇒ 均只降级"；**改写 p38 期望值并在 docstring
  写明理由**）。**变异验证 3 个方向全部变红**。
- **新登记（本轮未修）**：**B1-12**（布尔/勾选类处置档位由"措辞是否撞上词表"决定）、
  **B2-12**（`_check_grounding` 把"当前年份"当幻觉 ⇒ 降级理由说谎）、
  **B3-4**（`apply_review` 死代码 + 三份重复实现已漂移）。
- **分发判定**：**本轮不建议冻结 v1.2.0** —— ① 环境阻塞：`app.asar` 被宿主
  `WorkBuddy(pid=24732)` 持有，构建会**自愈到时间戳目录**，违反"仓库根只能有一个
  `dist-electron*`"；② 质量门禁：**先修完 P1 再冻结**。

### Changed (Round 43, 2026-09-20 — 第三轮对抗性审查：清单与文档同步)

- **`docs/ADVERSARIAL_AUDIT.md` 新增 §15**：五路只读审查（前端 / 后端 / 配置与协议 /
  流式与规则 / 文档与知识库）+ **视觉直读原页交叉比对**（用模型多模态能力核 5 页，
  **坐实 4 条假 critical**）+ 产物实测。判定：**无 P0 阻塞分发，v1.1.9 可作基线**；
  新增 **P1×1 / P2×7 / P3×9**。
- **`docs/TODO.md`**：
  - 新增 **B1-6**（主路径：把"取值"与"判据"从 LLM 收回规则层）、**B1-7**（规则层用墙钟
    当基准 ⇒ finding 不可复现）、**B2-8**（分片日志把累计值当"本片新增"）、
    **B2-9**（审计写失败静默 + 看门狗自述无停滞清单）、**B2-10**（前端过渡期与口径 4 处）、
    **B2-11**（聚合流 `/live` 异常期不 yield 帧）、**B4-4**（**P1** provider 被启动逻辑
    自动改写并持久化、界面零提示）、**B4-5**（KB 条目数口径虚高 36）；
  - **B1-5 扩充 3 个新形态**：跨字段串位（p6 体积串成时间）、布尔勾选读反 + 类型错配（p39）、
    凭空数字（p6 的 `18222`）；
  - **B1-4 二次确证**（含"同一基准值被各页各解"的直接证据）；
  - **纠正 2 条过时登记**：`B4-1`（`id:` 帧其实**早已存在**，且 2s 一帧本身即保活
    ⇒ 降级 P3、范围收窄）、`#155`（`review.js:437` 已处理 `done` 事件 ⇒ 隐患仅剩 `upload.js`）；
  - **实测更正 1 个数字**：规则覆盖度 **16/21 → 17/21**（原数字偏保守）。
- ⚠️ 本轮**不改任何进入产物的代码**（只动 `docs/`、`CHANGELOG`）⇒ 按项目约定
  **不升版号**；**当前可分发版本仍为 `1.1.9`**。

### Fixed (Round 33, 2026-09-17 — 归因更正：锁的持有者是宿主进程，不是安全软件)
- **更正散落多处的错误归因**（`README.md` / `DEPLOYMENT.md` /
  `docs/ARCHITECTURE_AND_QUALITY_REVIEW.md` / `docs/ADVERSARIAL_AUDIT.md` /
  `docs/PROJECT_PITFALLS.md` / `build.ps1` / `scripts/clean_dist.py`）：
  `resources/app.asar` 无法改名/删除此前被记作"**安全软件（火绒）驱动级锁**"，
  处置写成"加入信任区/白名单" —— **实测推翻**。持有者是 **WorkBuddy 宿主进程**
  （Restart Manager 具名：pid 16220 / 14048），且**只有 `*.asar` 被占**
  （同目录 `pbc-server.exe` / `BatchSentry.exe` / `app.asar.unpacked` 全部空闲）。
  ⇒ 加杀毒白名单**无效**，必须**完全退出该进程**。
- **`scripts/clean_dist.py`：报告不再靠猜**。新增 `who_holds()` / `describe_holders()`
  （Restart Manager；**fail-soft** —— 非 Windows / dll 缺失 / 任何异常都只返回空表，
  绝不让诊断把清理工具带崩），锁定目录时**直接具名持有者进程**，并明写
  "加杀软白名单对本案**无效**"。
- **两处只有实跑工具才会暴露的输出缺陷**：长目录名（时间戳变体）挤爆列宽、与"类型"
  列粘连；持有者行按 `", "` 切分导致条目被截断（条目内部本身含 `", "`）⇒ 改为按
  **结构化行**去重。

### Added (Round 33)
- `tests/unit/test_clean_dist.py` 新增 **6** 例（20 → 26）：`who_holds` **fail-soft**（路径不存在 /
  dll 不可用）、无人持有时 `describe_holders` 返回空串、具名渲染、**报告必须具名持有者
  且否定"加白名单"建议**、**模块 docstring 必须写明实测持有者并否掉旧误判**、
  长目录名不得与类型列粘连。
- `build.ps1` 自愈生成的 `PROVENANCE.txt` 模板新增 `unblock:` 段（正确的解锁指引：
  退出持有者进程；加白名单无效）。

### Changed (Round 33)
- ⚠️ **核验 `app.asar` 一律走子进程读取**（`clean_dist.asar_version()`）。用宿主/IDE 的
  读取能力打开 `.asar` 会**留下持久句柄**（实测：新建 `.asar` 空闲 → 宿主读一次即持续
  `winerror=32`；对照读 `.txt` 仍空闲）⇒ 锁死产物目录 ⇒ 下次构建清不掉标准输出目录
  ⇒ 又自愈出时间戳目录 ⇒ **目录越堆越多**。这就是 A1 会复发的机制。

### Added
- **`tests/unit/test_e2e_backend_assert.py` 新增两条护栏**：提供方选择器
  （`PBC_E2E_LLM_PROVIDER`，默认 `siliconflow`）存在且默认值正确；**密钥字段必须由
  提供方名派生**（AST 检查字典字面量的键：不得存在写死的 `*_api_key` 键）。
- **`tests/unit/test_clean_dist.py` 新增四例**：探针改名后必须复原原名；探针目录名
  **不得**被 `discover()` 误认成真实变体；目录级探测通过时**不得**下钻（O(1) 的关键）；
  目录级被拒却找不到被占用项时**不得返回空表**（空表会被下游读成"无占用、可删除"）。
- **打包信号新增两项生成物检查**（`scripts/release_gate.py`）：
  - `no_build_outputs`（**FAIL**）—— `git ls-files` 不得包含任何生成物根
    （`dist*` / `build/` / `devlogs/` / `node_modules/` / `release-archive/`）。
    拦三种 `.gitignore` **挡不住**的情形：`git add -f`、忽略清单被误改、
    新增落点未登记。只问"git 里有没有"，磁盘堆积交给下一项。
  - `dist_variants`（**WARN**）—— 根目录 `dist-*` 变体数超阈值时提示跑
    `scripts/clean_dist.py`。刻意不判 FAIL：多轮构建/发布会话并存几个变体是正常状态。
- **`tests/unit/test_repo_hygiene.py`**（新）：四条不变式 ——
  ① 忽略清单**实测**覆盖"文档允许 agent 写入的产物落点"（问 `git check-ignore` 的
  **返回值**，不读 `.gitignore` 的文本）；② 门禁清单与忽略清单**双向同步**
  （门禁盯着的路径必须真被忽略，否则门禁会误报）；③ 检查确实被编排注册
  （防重构摘掉，而"检查消失"不会让任何用例变红）；④ 判定谓词**不过度匹配**。
  含一组**接线证明**（喂入一份含产物的已跟踪清单，必须 FAIL）。

### Fixed
- **冻结冒烟的两处"假绿"（夹具缺陷，不是产品缺陷）**：把样例从**空白单页 PDF**
  换成**每次现生成的含真实文字 PDF** 后，同一个 v1.1.7 产物立刻由"绿"变为
  `19 passed / 1 failed`（`401 … Your api key: ****ucgz is invalid`）。两处缺陷同根且**互相掩护**：
  - **空白样例** ⇒ Stage 2 无内容可分析 ⇒ `error and OCR_CONFIGURED` 分支**永不可达**
    （"LLM 凭据失效"报不出来）；同源后果是**不可重复**（空白页触发旋转自愈，
    耗时/结果被上游 Paddle 支配）。现由 PyMuPDF **每次重写**含真实文字的一页。
  - **提供方与密钥错配** —— 配置段写死 `{"llm_provider": "deepseek", …}`，而注入的是
    **硅基流动**的 key ⇒ 打到 `api.deepseek.com`。**正因为空白样例让 LLM 从未被调用，
    这个错配才长期未被发现**。现提供方由 `PBC_E2E_LLM_PROVIDER` 派生，并新增
    判别性前置断言（活动提供方必须与密钥所属一致）。
  - 修正后实测 **21/0**，且**真的走通了 LLM 路径**：`provider=siliconflow`、
    `pipeline_terminal=review`、`findings=4` / `report_md=7049 B`（空白样例时是 0 / 202 B）。
- **`.gitignore` 漏了 `release-archive/`**：`CLAUDE.md`「Repo hygiene」规则 2 明确指示
  agent 把人为留存的历史版本打成 zip 放 `release-archive/`，而忽略清单里**没有这条**
  （`git check-ignore` 实测未忽略）⇒ **规则与忽略清单不一致时 agent 会照规则做**，
  几百 MB 的 zip 随后出现在 `git status`，下一次 `git add -A` 就进去了。已补齐，
  并由上述护栏把"落点必须三处联动"锁死。
- **测试套件的无效转义警告（既有问题，定位后顺手修掉）**：全量跑会输出
  `DeprecationWarning: invalid escape sequence '\s'`，且被 pytest 归到
  `test_declared_dependencies.py` 的 5 个用例名下 —— 看起来像依赖护栏的问题。
  实际是 **`tests/unit/test_page_analyzer.py:404` 的文档字符串**里写了 `[\s>]`
  而没加 `r` 前缀；那条警告是**依赖护栏在扫描 `tests/` 时解析到该文件**才被触发的。
  改为 `r"""…"""`。定位过程中的一个副产品：该文件带 **BOM**，用 `utf-8` 读会
  `SyntaxError` 而被扫描器静默跳过（已核实依赖护栏用的是 `utf-8-sig`，**无此盲区**）。

### Changed
- **`scripts/clean_dist.py` 认锁探测：10m52s（未完成）→ 11.5s**。原先无条件**逐文件**
  "改名再改回"（本机 ≈6500 次，每次都被安全软件拦一道，**>10 分钟仍无结论**，R3
  收敛迟迟不动）。改为**两层**：① 目录级先探 —— NTFS 拒绝重命名含被占用子项的目录
  ⇒ 目录能整体改名即证明**整棵子树**无占用（**O(1)**）；② 失败才按目录**递归二分**
  下钻（子目录能整体改名就跳过其子树），只为**指名**挡路的文件。
  另：探针目录名避开 `dist` 前缀（免得被打断时被 `discover()` 误认成真实变体）；
  诊断信息补上 **`winerror`**（`errno=13` 太含糊：目录级 `5`=ACCESS_DENIED /
  文件级 `32`=ERROR_SHARING_VIOLATION），并说明"持续 vs 瞬态"的判别与处置。
- `release_gate.is_build_output` 的前缀判定修正：`head.startswith("dist-electron")`
  会把 `dist-electronica/` 这类**恰好同前缀的正文目录**误判为产物 →
  改为"等于前缀 或 前缀后接 `-`"。**由护栏自己写的反向用例当场抓出**。
- `CLAUDE.md`：
  - 补 **Round 29**（v1.1.7 / #131，上一轮未落笔）。
  - 「Repo hygiene」新增规则 **7–9**（产物落点三处联动 / 禁止 `git add -f` 生成物 /
    不升版号的判定边界），并**修正规则 6** —— 原文要求引用 `devlogs/` 里的日志，
    但产物级 e2e 的 transcript 实际写在运行目录，且 `devlogs/` 本身可再生产物；
    现明确"同机可复核 vs 跨机可会审"的区别。
  - **修正构建指令**：原写 `.\build.ps1`（"must run in real PowerShell, NOT IDE Sandbox"），
    但实测该环境下**面向 agent 的 PowerShell 通道不执行、输出恒空** —— 照文档走的 agent
    会静默失败。改为**实测可用的 Bash 三连**，并把四个前置条件（python 3.11 置顶 /
    `CODEBUDDY_SAFE_DELETE_ENABLED=0` / `ELECTRON_BUILDER_CACHE` 指向既有缓存 /
    时间戳输出目录）连同各自的失败原因一并写进命令块。
  - Conventions 新增 **Test assertions** 条：改期望值必须同时写明理由；断言"解析后的
    结构"或"实测行为"而非"序列化后的字符"；断言必须**能真的失败**。
  - 补 **Round 31**（e2e 夹具的假绿 + `clean_dist` 的假慢），并**修正 Round 30 里
    被后验证伪的结论** —— 原文写的"冻结冒烟 20/0"实为**假绿**，已就地标注并给出
    修正后的数字与证据。
  - 修正仓库卫生节的**轮次错标**：该节原标 `round-27 立 / round-29 加固`、三条新规则
    也标 `(round-29)`，而仓库卫生工作是 **Round 30**（Round 29 是 #131）—— 5 处一并改正。
- `docs/PROJECT_PITFALLS.md` **§十九 / §二十**（新增）：
  **§十九** 夹具"空"会让断言不可达并**掩护**第二个缺陷（空白样本 ⇒ 分支永不可达；
  同一空白样本又让"密钥配错了提供方"从未暴露）——判据是"**该分支确实被执行过**"，
  且修好夹具后**必须重跑**（常会顺带抖出被掩盖的缺陷）。
  **§二十** 探测类工具要探**最粗的粒度**：目录级先探（NTFS 语义）→ 失败才按目录
  递归二分下钻；探针名避开被测命名空间；错误码要报 `winerror` 并区分持续/瞬态；
  **不能用"能否打开文件"代替"能否改名/删除"**（`FILE_SHARE_*` 是分别协商的）。
- `docs/PROJECT_PITFALLS.md` **§十八**：仓库卫生五条可复用教训 ——
  先定位（表象"仓库被污染"经实测是**误判**）→ 规则必须**自闭合** →
  护栏扫**代码结构**不扫**文本** → 判定谓词**正反成对**（反向用例针对最近的边界）→
  本机 Git Bash 的 `sort`/`find`/`timeout` 会被 `C:\Windows\system32\*.exe` 抢占，
  以及 `rm "$TEMP/..."` 混合分隔符路径触发 `SAFE_DELETE_FAIL_CLOSED`。

---

## [1.1.9] — 2026-09-18

> 本版是**缺陷修复版**：Round 34–39 的对抗性审查一共确认 **11 条**「GMP 假阴性 /
> 无终态 / 误导性处置」缺陷（2 个 P0 + 9 个 P1/P2），全部**先在源码修好、再重建
> 产物**。之所以**升补丁号**而不是复用 1.1.8：1.1.8 的产物已经构建过一次（其内容
> 含这 11 条缺陷，且已被删除），同一个版本号对应两份内容不同的二进制，对一个
> GMP 工具是不可接受的追溯性问题。

### Fixed

- **#133（P0）复核页状态点是硬编码绿色。** `templates/review.html` 无条件写
  `bg-success`，与 `upload.js` 的颜色映射各持一份 ⇒ 失败页 / 差异页在复核页
  看起来是「成功」。修法：抽共享件 `static/status.js`，SSR 侧由
  `core/zh_map.py:status_dot_class` 注入同一映射，两侧逐值等价由机检锁定。
- **#134（P0）列表端点不返回 `failed_pages` / `error_message`。** SQLite 里是
  TEXT 存 JSON，接口原样透传；前端 `Array.isArray` 静默丢弃 ⇒ 冷加载时失败页数
  与原因**永不显示**，只剩一个颜色点。
- **#135（P1）复核页首屏只显示通用横幅文案。** 具体原因只由 AJAX/SSE 之后写入，
  而 `DOMContentLoaded` 不触发 ⇒ 直接打开终态任务看不到真实原因。修法：从
  `structured_json` 提取 `_error` 注入 SSR，首屏复用**同一个** `updatePageLevelUI`。
- **#136（P1）失败页显示「本页无问题」。** 空态只按 findings 数量判断，未结合
  `page_parse_error` ⇒ 分析失败的页与确实合规的页视觉相同。修法：空态文案分
  分析失败 / 无 OCR 内容 / 确实无问题 三支，判据读**当前页**标记（首屏注入的
  `ctx` 翻页后过期）。
- **#140（P1）外部取消只终止父任务，派生的页分析任务继续跑 LLM 并写库。** 孤儿
  会刷新 `touch_activity` 掩盖停滞，并与用户重试后的新一轮抢同一页。修法：新增
  `core/pipeline/locks.ChildTasks`（spawn 登记 + 级联 drain），`run_pipeline` 的
  `finally` **首要动作**即级联取消（顺序不可颠倒：子任务会写库，会把刚收敛的
  终态再改回去）。
- **#141（P1）任务注册表按 job_id 单值覆盖 ⇒ 持锁孤儿失去引用 ⇒ 重试永久挂死。**
  注册表只记得住最新的 task（等待者），看门狗每轮杀等待者、真凶仍持 per-job 锁，
  形成「重试 → 超时被杀 → 再重试」的无限循环。修法：注册表改
  `dict[str, set[Task]]`，终止时取消该 job **全部**未完成 task —— 持锁真凶收到
  取消后才释放锁，等待者随之取到锁并因 `status=cancelled` 干净退出。
- **#142（P1）`pending` 且无注册任务 = 无终态黑洞。** 两处 `launch_pipeline` 调用
  点不在 `try` 内（`api/jobs/upload.py`、`api/jobs/actions.py`），启动失败即留下
  永不收敛的任务，而启动恢复只认「早于本进程启动」的 pending ⇒ 同进程内只能等
  下次重启，期间 SSE 无限等待且界面无提示。修法：`mark_launch_failed()` 做条件
  UPDATE + 审计，两处调用点包 `try`，失败即转 `error` 并返回 500。
- **#143（P2）「配置级故障」判据是整段错误串的子串匹配 ⇒ 误早停 + 误导排障。**
  词表含裸 `"400"` / `"invalid"`，于是真实限流 `429 … Limit 40000` 命中 `"400"`、
  本地 `ValueError("invalid literal for int() …")` 命中 `"invalid"` ⇒ 都判成配置级
  ⇒ Stage 2 **整份早停**并提示「请检查 API Key」。实测 8 个场景误判 3 个。修法：
  改为**结构化优先**（`status_code`，含 `response.status_code` 回退）+ 词边界文本
  兜底（`\b400\b` 不再命中 `40000`，裸 `invalid` 换成具体短语），并明确
  「本地编程错误类型（ValueError/KeyError/…）永不算配置级」。
- **#172（P1）列表端点还差 5 个行字段。** `renderJobRow` 是冷加载唯一的行构建器，
  消费 13 个字段而 `/api/jobs` 只给 9 个（缺 `ocr_backend_used` /
  `ocr_backend_display` / `pages_analyzed` / `phase` / `self_heal_progress`）。
  其中 `ocr_backend_used` 影响最实：缺失时 OCR 标签被隐藏 ⇒ 老任务超出 SSE
  10 分钟推送窗口后**永久显示不出用过哪个 OCR 后端**（呈现为「这份记录没用过
  OCR」）。这是 #134「只修了报告点名的那两列」的后遗症。
- **#173（P1）`page_ocr_empty` 漏在 `window.__PBC__` ⇒ OCR 空页被显示成
  「本页无问题」。** 该键一直在服务端上下文里（模板据此 SSR 正确渲染），却漏在
  给 JS 的桥接对象里 ⇒ 首屏兜底把「本页无 OCR 内容」覆盖成「本页无问题」——
  正是 #136 要消灭的假阴性，属上一轮修复的残留。

### Added

- **#171 前端字段契约机检**（`tests/integration/test_frontend_field_contract.py`）：
  字段集**从 JS 函数体派生**（不手写，避免第二份真值），对 `GET /api/jobs`、
  `GET /api/jobs/{id}`、SSE 快照**三个真实数据源**断言存在性 + 类型 + 取值，并
  **跨来源比对一致性**（「同一字段在列表与详情行为不一致」正是 #132/#134 的成因）；
  `window.__PBC__` 也纳入契约。**这条护栏建成即发现并驱动修掉了 #172 / #173。**
- **#151 Anthropic 协议实测**：此前该分支零运行证据（本机只有 openai 协议
  provider）。新增 `tests/integration/test_anthropic_protocol_live.py` —— 起一个
  忠实于 Anthropic Messages API 的**本地协议桩**，让真实 adapter + client 走真实
  socket 往返，逐项验证 `x-api-key` / `anthropic-version` / 顶层 `system` /
  `content[].text` / `input_tokens` 映射与 401/429 的分级。⚠️ 这是**协议桩不是
  厂商真机**，与 `api.anthropic.com` 的实际连通性仍未验证。
- **#159 视觉定向互证原型**（`core/vision_crosscheck.py` + adapter 多模态签名）：
  `user_content: str` 扩展为 `str | list[str | ImagePart]`（协议差异由各 adapter
  翻译：OpenAI `image_url` data-URL / Anthropic `image` + `source.base64`）。
  只对**数值 / 时间**单元格与像素互证，结论三态 `agree` / `disagree` /
  `unreadable`，**不一致一律降级为「待人工核对」，不采信任一方**；
  ⚠️ 依实测硬约束，**中文姓名不由视觉裁决**（7 个模型给出的姓名全错，没有一个
  读对「眭」），只标 `must_verify`。
  真实验收：对 `test1.jpg` 温度行给出显式输出「6 格中 5 格 OCR 与视觉不一致」
  （序号 1 两侧同为 50，作为反例）。
  ⚠️ **原型未接入 pipeline**：不写审计表、不改 job 流程 —— 接入是独立一步（#160）。
- 新增 `scripts/vision_crosscheck_demo.py`（真实验收脚本，走产品代码路径）。

### Changed

- `llm/adapters`：新增 `ImagePart` / `ContentInput` / `append_text_part` /
  `content_parts`。**纯文本路径的请求体字节级不变**（不为支持多模态而把每一次
  普通调用都改成 parts 列表）。
- `core/pipeline` / `core/watchdog.py` / `api/jobs` / `main.py`：随上述并发与
  可见性修复调整（注册表集合语义、级联取消、启动失败收敛、shutdown 双层遍历）。
- 前端：状态映射收敛到 `static/status.js` 单一副本；`upload.js` 的行摘要抽成
  `buildMetaLine`（静态与实时两条路径结构上共用，杜绝漂移）。

### 测试

- 全量 unit + integration **2713 passed / 0 skipped / 0 failed**（交付态口径：
  产物已重建，`tests/unit/test_distribution_parity.py` 里 3 条依赖实物产物的用例
  由 skip 转为 PASS）。同一提交在**产物被清空时**跑得 2710 passed / 3 skipped ——
  两个数字都对，差别只在于 `dist-electron/` 是否存在（collect 数恒为 2713）。
  上轮 2674 ⇒ **+39**：11 #143 分级判据 + 8 多模态报文形态 + 10 定向互证
  + 7 Anthropic 协议桩 + 3 条 parity 用例由 skip 转 pass；逐文件 collect 计数核对过，不是估的。
- 发布门禁六项 **OVERALL: pass（pass=8 fail=0 warn=0 skip=0）**，覆盖率 **95.24%**；
  事实源取自 junit 审计副本 `devlogs/gate_junit_20260918_112408.xml`（实测
  `tests=2713 failures=0 errors=0 skipped=0`，非文本解析）。
- 每条修复**都做了变异验证**（把缺陷改回去 ⇒ 护栏当场变红），包括：
  注册表单值覆盖、移除级联取消、还原裸 launch、失败页退回「本页无问题」、
  去掉 `status_code` 结构化判据、词表退回裸状态码、姓名纳入视觉裁决、
  空回答当成一致、用量字段映射退化、纯文本被包装成 parts 列表、
  fix-hint 退回字符串拼接。
  ⚠️ 其中一次变异**揪出了护栏自身的漏洞**：结构断言原写
  `assert "json.loads" not in src`，变异改用别名 `_j.loads(...)` 即全绿通过
  ⇒ 改为走 **AST 认调用形态**。凡是「查字面量」的护栏都可能被同义改写绕过。

---

## [1.1.8] — 2026-09-17

> 本版修的是**「失败页」在两处表现不一致**（缺陷 #132）：`jobs.failed_pages` 在
> SQLite 里是 TEXT（存 JSON），JSON 接口**原样透传**，于是 `/api/jobs/{id}` 与
> SSE 快照返回的是字符串 `"[2, 1]"`；前端按 `Array.isArray()` 取用 ⇒ **静默退化
> 为 `[]`** ⇒ 失败页数与页码在**任务列表里永不显示**。而复核页（SSR，`main.py`
> 早已自行 `json.loads`）显示正常 —— 同一字段两张页面行为不一致，用户只会读成
> "这次没有失败页"。**#127 专门为"让失败页可见"加的前端渲染，因此形同虚设。**
> 由本轮对**打包产物**的端到端验收抓出（不是推断）。
>
> ⚠️ 本版**必须重建产物**：改动落在 `api/jobs/status.py` 与 `main.py`，两者都会
> 打进 PyInstaller 产物（`main.APP_VERSION` 即版本真值）⇒ 按既有约定**升版号并
> 重建**（4 处：`main.APP_VERSION` / `package.json` / `package-lock.json` 顶层与
> `packages[""]` / `PORTABLE_README.txt`）。

### Fixed
- **`failed_pages` 双重编码**（#132）：`api/jobs/status.py` 新增
  `_parse_failed_pages`（沿用同文件 `_parse_ocr_progress` / `_parse_self_heal_progress`
  / `_parse_cross_progress` 的「TEXT → 结构」范式），并接入**两个**入口
  （`GET /api/jobs/{id}` 与 SSE 快照 `_get_job_progress`）。降级语义刻意
  **不伪造 `[]`**：无失败页 → `None`（"未能给出清单"），非法值 → `None` **并记
  warning** —— 否则"数据损坏"与"记录真的无异常"在界面上不可区分。
- **解析器收敛为单一真值源**：`main.py` 复核页原先自带一份 `json.loads`
  （`except Exception: pass`），现改为复用同一函数（call-time 导入，避免与顶层
  `from api.jobs import router` 形成循环）。同一列不再有两套降级规则。

### Added
- `tests/unit/test_job_status_parsers.py` 新增 `TestParseFailedPages`（6 例）：
  断言**返回类型是 list**（不只是取值），并锁住 `None ≠ []` 的语义。
- `tests/integration/test_api_jobs_coverage.py` 新增
  `test_get_status_failed_pages_is_a_json_array` —— 断言接口**运行时返回数组**。
  此前该类只断言字段**存在**（`field in data`），而字符串 `"[2, 1]"` 同样"存在"，
  缺陷正是从这条缝里长期存活。
- `tests/e2e_frozen.py` 新增运行时断言 `failed_pages_type`：向**打包产物自己起的
  服务**取 `/api/jobs/{id}`，断言 `failed_pages` 不是字符串。这条断言跑在要分发
  的那份东西上，证明的是"修复已随产物分发"，而非"源码树里写过这句话"。

### Changed
- `test_progress_snapshot_includes_failed_pages` 的断言由
  `'"failed_pages": "[2]"' in body`（**把缺陷本身固化成契约**）改为**先解析 SSE 的
  `data:` 帧、再断言结构**。
- `test_failed_pages_are_rendered` 补上前端的类型预期
  （`Array.isArray(job.failed_pages)`）。

### Verification
- **变异测试（把新纪律先用在自己身上）**：把两个入口分别临时改回"原样透传"，
  新护栏**当场变红**且报出实得值（GET：`实得 '[2, 1]'`；SSE：`实得 ['[2]', '[2]']`），
  还原后 149 passed —— 证明这些断言**能真的失败**，而不是"两个分支都判 PASS"。
- 全量单测 + 集成、打包信号门禁、以及对 v1.1.8 产物的冻结冒烟与 #127/#131 验收
  结果见当轮记录（`docs/TODO.md` 地面真值表）。

---

## [1.1.7] — 2026-09-17

> 本版修的是**同一份响应自相矛盾**（缺陷 #131）：终态 `error` 的原因文本写
> 「0 页产出可用结果」，可同一份 `/api/jobs/{id}` 里 `pages_analyzed` 却是
> **1**（`failed_pages=[1,2]`）。根因是「已分析页数」有**两套口径** ——
> 接口按 `structured_json IS NOT NULL` 计数，而失败页**同样**会写入
> `structured_json`（带 `_parse_error` 标记）。GMP 复核者必问"到底分析了几页"。
> 由 #127 的**产物级实景验收**现场抓出（不是推断）。

### 修复

- **【中】**「已分析页数」两套口径 ⇒ 同一 payload 自相矛盾（#131）：
  权威口径 `stage2._get_analyzed_pages` 的价值是"**排除 `_parse_error`
  占位页**"——该函数当初正是为修"retry 跳过失败页"而引入的；而
  `api/jobs/status.py` 的两个入口（`GET /{job_id}` 与 SSE 的
  `_get_job_progress`）各自**复制**了一份宽口径 SQL。失败页被算作"已分析"后：
  - 终态语义矛盾：原因文本「0 页产出可用结果」vs `pages_analyzed=1`；
  - `partial_review` 文案虚高：`部分可复核 · 10/10 页`，实际 8 页才是真产出。
  修复：新增 `_count_analyzed_pages()`，**复用** `_get_analyzed_pages`（同一
  函数，而非抄同一段 SQL）—— 两处入口同口径，杜绝再次漂移。
  前端只把该字段当**进度分子**（ETA 采样 /「分析 N/M」文案），完成判定走
  `TERMINAL_STATUSES`，故不影响"是否卡住"的判断。

### 护栏

- 集成：`_parse_error` 页不计入 `pages_analyzed`，且状态端点与 SSE 必须同值。
- 单元（源码契约）：`api/jobs/status.py` 的 **`db.execute` 实参**中不得再出现
  `structured_json IS NOT NULL`（AST 只扫代码实参，不扫注释/文档 —— 文档里为
  解释口径而引用该 SQL 是正常的，扫文本会让护栏变成噪音）；
  并机检两个入口都调用同一 helper。

---

## [1.1.6] — 2026-09-17

> 本版修的是**「分析没跑成」与「记录没问题」在界面上长得一样**（缺陷 #127）：
> 模型凭据失效时每页 Stage 2 都以 401 失败，job 报 `partial_review`、
> `error_message` 为 NULL、0 条 finding，界面是**绿点 +「部分可复核」** ——
> 对 GMP 复核者而言这与「记录确实无异常」不可区分，属**假阴性**。
> 本版把配置级故障从"页级失败"提升为可判别的类型 + job 级原因，
> 并让「零页产出」落到 `error`。推导与实测见 `docs/PROJECT_PITFALLS.md` §十五。

### 修复

- **【严重】配置级 LLM 故障被降级为页面级失败 ⇒ 界面呈现"绿色成功"（#127）**：
  产物级 e2e 现场（v1.1.5 内嵌 exe）—— 模型 key 失效后 `pipeline.log` 记
  `openai.AuthenticationError: 401 ... 'Token is invalid.'`，测试文档 6 页
  **全部** Stage 2 失败；job 终态 `partial_review`、`jobs.error_message`
  **为 NULL**、`findings` **0 条**。前端 `statusDotClass("partial_review")`
  此前返回 **`bg-success`（绿点）**，失败原因只在 `error` 态显示，而
  `failed_pages`（状态接口一直在返回）**界面零处渲染**。
  根因三处、修复四条：
  1. `llm/client.py`：「非可重试」判据（401/403/400/invalid…）**只用于控制流**，
     抛出的仍是字符串化 `RuntimeError`，调用方无从判别 → 新增类型化
     `LLMConfigError(RuntimeError)`（继承以保持 `except RuntimeError` 向后兼容），
     关键词表提为**单一来源** `_NON_RETRYABLE_KEYWORDS`；
  2. `stage2.py`：配置级故障走通用 `except Exception`，只留页级 `_parse_error`
     → 新增 `except LLMConfigError`（**必须排在通用分支之前**，有 AST 护栏）
     + `_handle_page_failure` 统一出口，把**首因提升到 job 级**
     `error_message`（脱敏、并发下只写一次）；
  3. **早停**：配置级故障是确定性的，不再"补刀" —— 未完成任务取消，且
     `_analyze_one` 取到信号量后二次确认（`config_error` 非空即返回）；
     未及尝试的页仍计入 `failed_pages`（与 Stage 1 缺页同口径）；
  4. **终态**：`failed_pages` 非空且**无任何一页产出** → `error`。判据**从数据
     派生**（复用 `_get_analyzed_pages` 的"排除 `_parse_error`"口径），
     因此不依赖故障原因；有页成功时仍为 `partial_review`（不误伤真·部分场景）。
- **前端可见性（#127 的另一半）**：
  - `partial_review` **不再显示成功色** → 降为 `bg-warning`。它**定义上**
    就不是成功态（见 `stage3` 的 `final_status` 判据：有失败页或双后端差异）；
  - `partial_review` 下同样显示 `error_message`（SSE 实时更新 + 历史任务行两处）；
  - 历史任务行渲染**失败页数与页码**（此前 `failed_pages` 零处消费）；
  - 复核页页内横幅显示**真实原因**（`structured._error`），不再只有一句通用文案。

### 护栏（把"看不见"锁死）

- **`tests/unit/test_config_error_visibility.py`（新增）**：类型化异常 +
  非可重试不重试 + 瞬态错误不误判为配置级 + 关键词单一来源 + `except`
  顺序 AST 全文件扫描 + 前端源码契约（成功色分支不得含 `partial_review`／
  必须消费 `failed_pages`／两处原因显示／横幅必须有可写入的 `id`）。
- **`tests/unit/test_pipeline.py::TestConfigErrorVisibility`（新增）**：
  全页失败 ⇒ `error` + job 级原因；配置级故障下 **LLM 只被调用一次**
  （并发=1，确定性断言）+ 未尝试页计入 `failed_pages`；
  **反向护栏**：有页成功时不得被误判为 `error`。
- `tests/unit/test_config_db_pipeline_coverage.py::test_pipeline_handles_page_analysis_failure`
  的终态断言随契约更新为 `error`（原 `partial_review` 是旧规则的副产品），
  并补强"管线必须跑完不中断"的原意。

---

## [1.1.5] — 2026-09-17

> 本版修的是**上游「容量类」错误的处置**：旋转补救把上游拥塞当成永久失败，
> 一个角度都没读到，还把基础设施状况写成了内容结论（#120）；并顺势补上
> P2「停滞可见性」。完整推导、实测证据与看门狗阈值联动见
> `docs/RUNTIME_WATCHDOG.md` §8.9 / §5；踩坑存档见 `docs/PROJECT_PITFALLS.md` §十三。

### 修复

- **【严重】旋转探测把「上游拥塞」当永久失败 ⇒ 全部角度在 61s 内耗尽（#120）**：
  现场（job `234d3838-29d`，v1.1.3 产物）**五次旋转探测全部被 Paddle
  `HTTP 400 code:10010 任务提交队列已满` 拒绝**，间隔 7–15s，**61s 内耗尽全部角度尝试**；
  而同日实测的**拥塞窗口是分钟级（≥18 分钟）**。两个后果：① 旋转通道在**最需要它的
  场景**（上游降级造成空页，正是它被设计的场景）**放弃得最快**；② 落
  `rotation_probed=True`，把**上游繁忙**写成「已探测全部角度未果」的**内容结论**
  —— GMP 复核里这两件事的处置完全不同。根因：对上游错误**不做分类**，且内层 2s
  退避与外层换角度**两层相乘**（正是 ≈15s/角度的成因）。
  修法（五项咬合）：① 分类器**单一真值** `core.ocr_client.is_congestion_error()`
  （容量/拥塞类 vs 永久 400 类），并把 `mineru_client` 内联的 `transient_markers`
  删除、收敛到一处；② **轮询超时刻意不算拥塞**（它已自身封顶 630s/页；按拥塞重试会把
  单次尝试拉到 1260s，直接抬高看门狗的静默上界）；③ **退避重试同一角度**
  （阶梯 30/60/120/240/300s，**不消耗有限的角度预算**；15s 分片睡眠保取消响应；
  **每次退避前写心跳**）；④ **诊断互斥** —— `rotation_blocked="upstream_congestion"`
  （从未探测）与 `rotation_probed`（探测过、内容读不出）由结构保证不可并存，并新增
  审计 `stage1_rotation_blocked`（文案明写 "NOT probed … retrying the job later may
  recover them"）；⑤ **单页 + 整轮双预算**（单页取"一次完整尝试成本"与"实测拥塞窗口"
  的较大者，整轮 1800s），整轮耗尽时剩余目标页**一并如实标记且不再探测**（不假装试过）。
  护栏 `tests/unit/test_upstream_congestion.py`（含"心跳必须写在每次退避前""预算耗尽
  返回 blocked 而非空串""硬失败必须立即上抛""诊断不得并存"等）。
- **【中】用户取消被吞成「角度探测失败」**（与 #120 同源，本轮顺带查出）：
  `_probe_slice_text` 的 `except Exception`（**先前就存在**）会吞掉 `OCRCancelled`
  —— 它是 `RuntimeError` 子类。后果：用户取消被记成"探测失败"（又一次把操作状况写成
  内容结论），且取消后**还会再打一次上游**、继续试其余角度。已修（`except
  OCRCancelled: raise` 排在通用分支之前；`_rotation_heal` 内外两层按取消语义收尾、
  **不写任何诊断**），并上升为**全仓机检**：AST 扫 `core/api/llm/db/models`，任何 try
  里通用分支不得排在 `OCRCancelled` 之前，且要求**至少扫到 3 处**（一处都扫不到的护栏
  永远通过）。
- **【中】看门狗 OCR 期「静默上界」推导更正**：旧注把上界算作
  `3 角度 × 630s = 1890s`，**漏了每角度 2 次尝试**（真实乘积 3780s）；且自愈改为
  "每次尝试后写心跳 + 每次退避前写心跳"之后，缺口**不再跨尝试累加**。现由
  `self_heal.rotation_silence_bound_s()` 单一提供（= `max(2×630, 300)` = **1260s**）
  ⇒ `ocr_running` 阈值 4200s **无需改动**，并由 `test_watchdog.py` 机检锁定（读派生量、
  不硬编码）。**顺带抓出一处自相矛盾的配置**：单页预算手写 `1200s` < 一次完整尝试
  `1260s` ⇒ `remaining` 在第一次尝试后必 ≤ 0、**退避分支不可达**（阶梯末项成死常量）；
  已改为**派生量**。

### 新增

- **P2：停滞可见性（纯派生 —— 不写库、不改 schema、不新增 SSE 事件类型）**：
  `core/watchdog.py::stall_report()` 用已有的心跳 `jobs.last_activity_at` +
  **同一张**阈值表算出 `{idle_seconds, limit_seconds, warn, overdue}`
  （阈值 **60%** 时置 `warn`，先于看门狗的杀任务动作给用户信号），暴露在
  `GET /api/jobs/{id}` 与 SSE 快照**两个**载荷里（SSE 投影相应多选一列）；
  前端 `static/review.js` 渲染 `role="status" aria-live="polite"` 横幅、`overdue`
  升级措辞。取数失败**永不**影响状态查询（兜异常 → `None` + warning）。
  护栏 `tests/unit/test_stall_visibility.py`。
- **看门狗自述新增 `rotation_silence_bound_s`**（`GET /api/health/watchdog`）：
  把 #120 引入的"旋转补救静默上界"连同既有 `stall_limits_s` / `ocr_upstream_cap_s` /
  `cpu_task_cap_s` 一并向外部报出，使**产物级冒烟不硬编码任何常量**即可复核
  "基准阈值 ≥ 其所覆盖的上游封顶"的不变式（阈值改动时护栏自动跟随）。延迟导入
  `self_heal`（无导入环），且仍通过 `/api/health/watchdog` 的密钥护栏。

### 修复（e2e 驱动与文档 —— 原 `[Unreleased]` 内容，随本版一并落版）

- **【中】e2e 护栏只取 findings 首页 ⇒ 判据下界失真**：`/api/jobs/{id}/findings` 默认
  `limit=50`（钳制 200），driver 原先只取首页。实测 51 页真实 **314 条 / 14 类** 被打印成
  "50 findings / 7 类"，`missing` 判定基于被截断分布；更糟的是 findings 从 314 掉到 60
  依然 `ok` —— **护栏形同虚设**。改为按 offset 翻页取全量（按**实际返回条数**推进，兼容
  服务端把 limit 钳小），并新增 `findings_declared_total` 完整性自检：取回数 ≠ 端点声明
  total 即判本轮失败。护栏 20 条（`tests/unit/test_e2e_findings_paging.py`）。
- **【中】`run_rot` 前置失败吞掉旋转测量**：原 `if not res.get("ok"): return res` 使
  paddle→mineru failover 在判失败的同时把整段旋转测量一起吞掉，摘要里连 `rot_lost` 都没有，
  读者会以为"旋转没问题"。改为**观测与断言分离**（仍测量并落 `rot_measured`/`rot_paths`/
  `rot_lost`，只是不计入 `ok`）；`ok` 断言未放宽。
- **文档**：`docs/PROJECT_PITFALLS.md` 新增 §十一（旋转自愈 forensic 定案：**非代码回归**，
  是 Paddle 上游拥塞窗口的产物 —— 附同日健康/拥塞期天然对照实验，并记录一条被推翻的
  假设"字段缺失 ≠ 闸门未触发，可能是设计语义"）与 §十二（上述两条护栏教训）；
  本版又新增 §十三（#120 的完整踩坑：源码带 **BOM** 让 `ast.parse` 崩、护栏用正则把
  **文档字符串**误判为重复定义、**派生常量没通过自己的预算**三条，均由机检当场抓到）
  与 §十四（**构建侧**：Windows 无符号链接特权时 electron-builder 解包 `winCodeSign`
  必失败，且缓存根按 HOME 推算会**绕过**机器上既有的缓存 —— 附"只指缓存根即解"的做法
  与两条排查纪律；本轮 1.1.5 产物即据此重跑取得退出码 0）。

### 变更

- 版本号 1.1.4 → **1.1.5**（一致性机检 4 处：`main.APP_VERSION` / `package.json` /
  `package-lock.json`（顶层 + `packages[""]`）/ `PORTABLE_README.txt`）。
  ⚠️ `package-lock.json` 里另有 2 处 `"1.1.4"` 属**依赖自带**
  （`color-name` / `define-data-property`），不得批量替换。

---

## [1.1.4] — 2026-09-16

> 本版是 **看门狗实施后回审（第二轮回审）** 的修复版：v1.1.3 落地的 P1 看门狗
> 自身还有 5 项缺陷（三项严重）。判定依据、量化证据与护栏见
> `docs/RUNTIME_WATCHDOG.md` §8 —— 其中第 5 项是被第 2 项提炼出的**阈值不变式**
> 顺带揪出来的。

### 修复

- **【严重】看门狗收敛只改状态、不终止孤儿 pipeline task** —— 会**由它自己**
  造出一个永久无声的 `pending`：`run_pipeline` 用 per-job 锁串行同一 job，
  而 `retry` 端点 `error → pending` 后就 `launch_pipeline`；孤儿仍持锁 →
  retry 永远停在 `async with lock`（该等待点无任何上限），而 `pending` 按设计
  不被监视 → 用户照着看门狗提示点"重试"，得到的是永远不动、也没有提示的 pending。
  附带损害：孤儿继续写 `ocr_progress`/`last_activity_at`（UPDATE 不带状态条件）→
  会刷新**重试轮**的心跳，可能掩盖重试轮真正的停滞。
  修法：收敛后 `task.cancel()` + `asyncio.wait(10s)`，处置结论写进审计
  （`orphan_task=cancelled|no-task|not-exited`）；终止与审计都在 `db_lock`
  **之外**（pipeline 的 `CancelledError` 分支自己要 `transition_status` → 取同一把锁）。
- **【严重】OCR 停滞阈值（1800s）低于上游自己的封顶（`POLL_TIMEOUT_MAX = 3600s`）**
  → 会抢在上游超时之前把"上游还在正常等待"判成停滞。量化：空页自愈**逐页**写心跳，
  心跳缺口上界 = 单页补救 = 3 个候选角 × 单页探测封顶 630s ≈ 1890s + 重分析 ≈ 2100s；
  旧阈值对 1 页文档只有 1920s（**低于合法上界**）、对 4 页 2280s（余量 8.6%）。
  修法：基准改为 `POLL_TIMEOUT_MAX + 600 = 4200s`，并把"基准 ≥ 上游封顶"写成
  **不变式** —— 护栏 `TestThresholdsAboveUpstreamCaps` 从真值源推导（ocr_client /
  procpool / LLM 适配器 / 旋转候选角数），不重复字面数字。
- **【严重】`cancelling` 阈值（900s）低于它要等的封顶（CPU 重活超时 1800s）**
  —— 取消检查点只在 `run_cpu` **前后**（`stage1.py:49/54`），所以正在 Stage 0
  规范化的大文档被取消后会合法地停在 `cancelling` 直到收尾；阈值低于该封顶时
  看门狗会把"**已请求取消、正在正常收尾**"的 job 改成 `error`，而 `engine.py`
  明确禁止覆盖取消语义（"cancelled 被改成 error 会破坏取消审计链，通知也会重发"）。
  修法：基准 = `procpool.cpu_task_timeout_seconds()` + 600 = **2400s**。
  同时记录一条**固有上限**（非缺陷）：取消的响应性受限于本地 CPU 重活的封顶
  —— 进程池隔离决定了 `run_cpu` 跑到一半无法被打断。
- **【中】"被接管的 pending"不可见** —— 补一条收窄规则：`pending` 只在
  `_pipeline_tasks` 里有未完成 task 时才纳入判定（上传/重试都是"先写 `pending`、
  紧接着 `launch_pipeline`"，中间只有毫秒级窗口，把过渡态当停滞会误杀刚上传的任务），
  阈值 900s。⚠️ 同时更正判据文案：`MAX_CONCURRENT_JOBS` 达上限时是**直接 409 拒绝**
  而**不是排队**，所以运行期**根本不存在**"排队等槽位"的 pending ——
  原判据把它写成"合法排队态"与代码不符（结论不变：仍不默认监视）。
- **【中】e2e 轮次预算写死单值，两次把真实长跑判成失败**（2026-09-04 rot 620s>600s
  误杀；2026-09-16 pdf 835s>600s 判超时，该 job 随后正常进 `review`）。修法：
  预算 = 「基线 + 每页 × 页数」，斜率按各轮实测每页成本取 —— pdf 1980s@6 页、
  rot 2400s@4 页、real 7920s@51 页；页数读不出时按 51 页保守回退；
  `E2E_*_TIMEOUT` 仍可覆盖。

### 新增

- **`GET /api/health/watchdog`** —— 看门狗自身的可观测性（存活 `running` /
  `last_scan_at` / `last_scan_error`、判定口径 `stall_limits_s` / `per_page_s` /
  `ocr_upstream_cap_s`、最近一轮 `last_stalled_found` / `last_recovered`）。
  看门狗自己挂掉比 job 卡死更糟（用户会以为有兜底），故存活必须可查。
  **不并入 `/health`**：后者是 Electron 启动与 e2e harness 依赖的稳定探针契约。
- **护栏**：`tests/unit/test_watchdog.py` 39 → 55 条
  （`TestThresholdsAboveUpstreamCaps` / `TestOrphanTaskTermination` /
  `TestPendingTakeover` / `TestStatusSnapshot`）；
  `tests/unit/test_e2e_round_budget.py`（18 条，含"不得再写死默认预算"的静态检查）；
  `/api/health/watchdog` 路由用例 3 条。

### 变更

- 版本号 1.1.3 → **1.1.4**（5 处：`main.APP_VERSION` / `package.json` /
  `package-lock.json` 顶层 + `packages[""]` / `PORTABLE_README.txt`）。

### 验证（本版实测）

- **全量回归**：`2464 passed / 0 failed`（`tests/unit` + `tests/integration`，202.78s，
  0 skipped）—— 新增 55 条看门狗护栏 + 18 条预算护栏 + 3 条健康端点用例全部生效。
- **构建**：PyInstaller `dist/pbc-server/pbc-server.exe` = 20,314,177 B；
  Electron `dist-electron-out-20260916-115142/win-unpacked/BatchSentry.exe` = 188,784,128 B。
- **产物冒烟**（只有产物才暴露的断言）：
  - `/health` 的 `version` == 源码 `main.APP_VERSION`（**从源码读，不硬编码**）；
  - `/api/health/watchdog` 可达 ⇒ 证明 **lifespan 里延迟导入的 `core.watchdog`
    真的被打进包**（PyInstaller 静态分析抓不到延迟导入）；
  - **阈值不变式在产物上再复核一次**（不硬编码任何常量，直接比对端点自述的两个数）：
    `invariant ok: ocr_running=4200.0 >= ocr_upstream_cap_s=3600.0`、
    `invariant ok: cancelling=2400.0 >= cpu_task_cap_s=1800.0`。
  - ⚠️ 该冒烟脚本首版用**字符串匹配**序列化 JSON（`'"enabled": true'` vs 实际
    `"enabled":true`）→ **产物完全正确却报失败**、把构建挡在 Step 2.5。已改为
    `json.loads` 后断言结构（教训见 `docs/PROJECT_PITFALLS.md`）。
- **分发一致性** `tests/unit/test_distribution_parity.py` **13/13 通过**：
  最新构建目录完整、内嵌产物与 `dist/` **逐字节一致**、asar 版本 == 1.1.4、
  `PORTABLE_README.txt` 版本同步、`extraResources` 确实内嵌 PyInstaller 输出。
- **真实 51 页端到端**（`e2e_run.py --rounds pdf,real`，直接指向
  `win-unpacked/resources/pbc-server/pbc-server.exe`，即**要分发的那份**）
  —— 结果见本文件同日的运行记录 / `devlogs/`。

---

## [1.1.3] — 2026-09-16

> 本版是 **R1–R3 降噪 + 运行时看门狗 + 构建物重出** 的合并版：产物此前停留在
> `8cd0dc1`（v1.1.2 设定那次），落后源码 20 个提交，**分发它等于发旧行为**。

### 新增

- **运行时看门狗（`core/watchdog.py`，schema v12）**：补齐"运行期间"的兜底 ——
  此前 `recover_stuck_jobs` **只在启动时**跑一次，不重启就没有任何自动收敛，
  卡死的 job 会让 SSE 永久等待（`while True` 只认终态）。
  判据与取舍（详见 `docs/RUNTIME_WATCHDOG.md`）：
  - 新增 `jobs.last_activity_at`，由 `state.touch_activity` 写在**真实推进点**
    （状态迁移 / OCR 进度 / 自愈进度 / 跨页进度 / Stage 2 单页分析完成）。
    **心跳绑定前进，不挂定时器** —— 定时器在 stall 期间照样跳，看门狗永不触发。
  - 列**故意不给 DB 默认值**：`ALTER` 不允许用 `datetime('now','localtime')` 作默认，
    而 `CURRENT_TIMESTAMP` 是 UTC，与全库 localtime 口径冲突 → 由代码统一写入。
  - `last_activity_at IS NULL` / 时间戳不可解析 → **一律跳过**（不可判定就不判，
    绝不用 `created_at` 兜底 —— 那会误杀正常跑很久的大文档）。
  - **`pending` 不在监视范围**：运行期间它是"已建单、pipeline 尚未推进"的过渡态
    （上传/重试先写 `pending` 再立刻 `launch_pipeline`），当停滞会误杀刚上传的任务；
    崩掉的 pending 由启动恢复兜底。
    ⚠️ 本行原文写作"合法排队态（`MAX_CONCURRENT_JOBS`）"，**与代码不符** ——
    达上限时是直接 409 拒绝而非排队，运行期不存在排队态；已在 [1.1.4] 更正
    （本行保留原值以记录当时的决策）。
  - 阈值按状态分级且刻意宽松（OCR 1800s + 120s/页 封顶 3h；逐页 LLM 1800s +
    180s/页 封顶 3h；`ocr_done` 1800s；`cancelling` 900s），`PBC_WATCHDOG_SCALE`
    可整体缩放。依据是实测基准（51 页 OCR 644s、逐页 LLM 825–1209s）。
    ⚠️ 其中 OCR 基准 1800s **低于上游封顶**，已在 [1.1.4] 修正为 4200s ——
    参见该版"修复"第 2 条（本行保留原值以记录当时的决策）。
  - 开关 `PBC_WATCHDOG_ENABLED` / 周期 `PBC_WATCHDOG_INTERVAL_S`。
  - 收敛动作与启动恢复一致：条件 `UPDATE ... WHERE status = ?`（防并发改写）+ 审计
    `watchdog_stall_recovery` + 锁外飞书通知。扫描异常**绝不退出循环**（看门狗自己
    挂掉比 job 卡死更糟）。
- **`docs/RUNTIME_WATCHDOG.md`**：看门狗必要性调研（三问判据 / 超时覆盖面矩阵 /
  业界对标 / 分层方案 / 不推荐做法）。
- **`docs/VISUAL_CROSSCHECK.md`**：视觉交叉对比实测（前置条件 + 独立互证结论）。
- **`docs/ADVERSARIAL_AUDIT.md`**：9 问对抗性审查报告（逐条带 `file:line` 证据）。

### 修复

- **周期巡检引入时踩到的两个坑（均被自己新增的用例当场抓出）**：
  - `recover_stalled_jobs` 首版在 `db_lock` 内调用 `_audit_log`，而后者也取同一把
    `asyncio.Lock`（不可重入）→ **永久死锁**。修法：审计移到锁外写。
    护栏 `test_marks_error_audits_and_notifies`。
  - `_migrate_v12` 首版被贴在 `_migrate_v11` 的 `if current_version < 11:` 块内 →
    **v11 库整段跳过迁移，而 `PRAGMA user_version` 仍无条件写成 12** = 库被标成 v12
    却缺列（静默 schema 漂移，看门狗永久失明）。修法：补自己的守卫；并新增通用护栏
    `test_every_migration_version_has_its_own_guard`（每个版本号必须有同号守卫 + 同号调用）。
- **`run_cpu` 没有超时 —— 全链路唯一的"永久非终态"入口（对抗审查定位）**：
  实测超时覆盖面发现，外部调用**都有**上限（LLM 180s、MinerU 60/300s、Paddle
  轮询封顶 3600s），唯独本地 CPU 重活（Stage 0 规范化）的
  `run_in_executor` / `to_thread` 等待**没有任何上限**。后果不是"慢"，而是
  job 永久停在非终态、SSE 死等（`api/jobs/status.py` 是 `while True` 且只认终态）、
  `recover_stuck_jobs` 又只在启动时跑一次 —— 用户只能重启应用。
  更严重的是**放大效应**：进程池是 `max_workers=1`，一个挂死的 worker 会永久
  占住唯一槽位，把"某个 job 卡住"扩散成"整个应用不再处理新任务"（项目注释
  `core/pipeline/stage1.py:45-48` 已预见该风险，但此前无对应措施）。
  修法：`run_cpu` 加单任务超时（默认 1800s，`PBC_CPU_TASK_TIMEOUT_S` 可覆盖；
  定位是"永久等待兜底"而非性能限制，故留足一个数量级余量），超时后
  **回收进程池**（仅让调用方不再等待不会释放槽位）并抛 `TimeoutError` 让 job
  走正常 error 路径。线程回退路径无法强杀线程，只能止损（抛错 + 记明"线程已泄漏"）。
  护栏：`tests/unit/test_procpool.py::TestCpuTaskTimeout` /
  `::TestProcessPoolTimeoutRecycle`（含"回收后可重建"）。调研与分层方案见
  `docs/RUNTIME_WATCHDOG.md`。
- **`e2e_frozen.py` 的"假绿"（本轮端到端实测暴露）**：两处叠加导致
  "pipeline 完全跑不起来"在冒烟里也是绿的：
  - **不配置 OCR**：只 POST 了 LLM 凭据。实测 Paddle 的 `api_url` 为空时提交
    立即失败（`Invalid URL '': No scheme supplied`），pipeline 一路走到 `error`。
  - **断言过弱**：`pipeline_terminal` 用 `status in (..., "error", ...)` 收集结果后
    无条件 `ok()` —— `error` 也判 PASS（与 `pytest.importorskip` 同源的"静默成功"）。
  修法：新增 `Configure OCR` 步骤（凭据从环境 `PBC_E2E_*` 取，绝不入库）；
  `pipeline_terminal` 按**环境是否具备跑通条件**分级断言 —— 已配 OCR 却出现
  `error` 即 FAIL 并带出 `error_message`；未配凭据时如实标注为降级（`[SKIP]`），
  **不再冒充 PASS**。
- **密钥护栏"分家"收拢为一处**：密钥扫描此前同时存在于
  `tests/unit/test_e2e_proc_helper.py`（两条）与 `tests/unit/test_no_committed_secrets.py`，
  两套口径并存 —— 正是本项目明令禁止的"重复真值"。现收拢到后者，并顺带修掉三个真实缺口：
  - **范围扩到仓库根目录脚本**：原范围只有 `api/core/db/llm/scripts/tests/tools/models`，
    漏掉 `main.py` / `config.py` / `e2e_run.py` 等根目录文件 —— 而本事故（真实 key 被写死
    进 e2e 脚本）正发生在那里，`server.py` / `ui_e2e.py` 当年就因此躲过扫描。
  - **产物目录按前缀排除**：原实现罗列 `dist-electron-m8` 等具体名（历史上正是这么积出
    4 个 `dist-electron*` 的）；改为 `dist*` 前缀，构建自愈到备用目录时不会再漏。
  - **与文档同源**：`DEPLOYMENT.md` 的自查命令与护栏口径此后由
    `test_documented_scan_command_matches_this_guard` 双向绑定 —— 改一边必须改另一边。
- **防空转与前提检查**：新增 `test_secret_scan_positive_control`（防"全绿只是空转"）、
  `test_scan_scope_covers_repo_root_scripts`（防范围被无意收窄）、
  `test_config_json_is_gitignored`（"扫描排除 config.json"这一口径的**前提**）。
  顺带**消除全部豁免**：正反样本一律用**拼接**构造，故文件自身不会命中，无需白名单
  （白名单会让"往该文件里加密钥"也逃过检查）。
- **`scripts/release_gate.py` 在非 UTF-8 控制台下崩溃（CI 首次运行的实测收获）**：
  六项检查**全部通过**、报告文件也已写出，却在随后打印报告时抛
  `UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-9` ——
  本地开发机代码页是 936/65001（中文正常），而 GitHub Actions 的 `windows-latest`
  控制台是 **cp1252**。即"门禁结论正确但 CI 红"：这是只有真跑 CI 才会暴露的缺陷，
  也正说明此前"没有触发门禁的人"。修法两层：`main()` 开头把 stdout/stderr
  `reconfigure(encoding="utf-8")`（对重定向到文件同样生效），并在跑子进程时注入
  `PYTHONIOENCODING=utf-8`（pytest 自己打印中文用例名时同样会踩）。
  新增 `TestNonUtf8Console::test_report_prints_under_cp1252_console`：**在 cp1252 下**
  启动门禁子进程，断言无 `UnicodeEncodeError` 且报告头 `OVERALL` 出现在输出里。
  已做正对照 —— 绕过修复即复现同一条报错。
- **覆盖率门禁在干净检出上不达标（CI run #2 实测暴露的真实缺口）**：修掉上面的控制台
  问题后，CI 又报到 **94.89% < 95%**。根因不是代码缺陷，而是**测试的环境耦合**：
  - **一处断言写死了检出目录名**：`test_dev_mode_returns_project_root` 断言
    `path.name in ("pharma-batch-checker", "")` —— CI 检出到 `BatchSentry` 即失败
    （`assert 'BatchSentry' in (...)`）。契约本是"开发模式返回 `main.py` 所在目录"，
    改用 `__file__` 表达，与克隆目录名解耦。
  - **一批 artifact-gated 用例在干净检出上必然 skip/ephemeral**：需要 `dist/pbc-server.exe`
    （打包产物）、真实 Paddle/MinerU 产物（体积 + 数据敏感性，不入库）、`dist-electron*`。
    本地因这些产物在场而"顺手覆盖"了 **30 行**，干净检出上无人覆盖 →
    95% 门禁**只在本机能过**。行级差分（`coverage` 数据对比）精确定位到：
    `core/mineru_client.py` 29 行（P0-3 的 `_layout_page_sizes` / `_page_regions`）+
    `core/health.py` 1 行（`probe_paddle_ocr` 的连接失败分支）。
  - **修法**：按本项目既定约定（"真实产物不入库，故用等形态合成数据锁契约"）补
    **环境无关**的直接用例 —— 内存合成 MinerU zip 覆盖 `page_size` 解析的
    全部畸形分支、块级区域抽取的 bbox 校验分支、`_content_text` 的 dict-值-字符串
    分支、以及 `probe_paddle_ocr` 的 `ConnectionError` 分支。
  - **实测**：干净检出等效覆盖率 **94.97% → 95.42%**（缺失行 460 → 419）；
    与"产物在场"全量跑的行级差分 **A−B = 0 行**（缺口闭合），且新增用例**反向多覆盖
    14 行** —— 全是真实产物用例永远走不到的畸形输入分支。
  - **这类缺陷的护栏就是 CI 本身**：此前门禁只在本机跑，所以"95% 只在本机成立"
    一直没被发现。workflow 落地后第一次跑就抓到了，这正是「提交即验证」的价值。
- **CI 上"到底跑了哪些用例"不可查证（T0.5，根因在门禁的落盘位置）**：要回答"CI 与本地
  差哪些用例、为什么"，唯一依据是**逐条**用例清单。实测发现两层障碍：
  - **第一层**：`.github/workflows/ci.yml` 的上传步骤写作 `if: failure()` —— CI 成功时该
    步骤直接 `skipped`，**连 artifact 都没有**（run #3 实测：步骤结论 = `skipped`）。
  - **第二层（真正的根因）**：即便改成 `always()`，传出来的也只有**残缺的 stdout 文本
    日志**。因为门禁跑 pytest 带 `-o addopts=-q`，stdout 上**只有点和汇总、没有逐条
    nodeid**；逐条事实源是 junit XML，而它写在 `tempfile.gettempdir()`（CI 上是
    `D:\a\_temp\`）—— 随 runner 一起消失，也不在 artifact 上传列表里。
  - **后果**：一次"44 个用例没跑"的调查只能钉死 17 条 skip（三个文件），其余无法归因。
    此前文档把它写成"属预期、不是缺陷"是**未经证实**的，已更正（见
    `docs/ADVERSARIAL_AUDIT.md` §13.2）。
  - **修法**：门禁把 junit XML **另存一份到 `devlogs/gate_junit_<stamp>.xml`**（字节级
    复制，路径写进报告 `detail`），CI 的 artifact 列表补上该 glob，并保留 `always()`。
    新增 `_audit_dir()` 作为**独立注入点** —— 测试不能直接 monkeypatch `REPO_ROOT`，
    因为 `_junit_nodeid` 依赖它定位真实 `.py` 文件（替换后 nodeid 会退化成点分转斜杠）。
  - **护栏**：`tests/unit/test_release_gate.py::TestJunitAuditTrail`（4 例）—— 副本逐字节
    一致、副本仍可被门禁自己的解析器读出逐条 nodeid、junit 缺失时不造假副本，以及一条
    **跨文件契约**（`ci.yml` 必须同时含 `devlogs/gate_junit_*.xml` 与 `if: always()`；
    少了任一半，门禁写出来的副本都传不出 CI，盲区原样保留）。
- **整个测试文件在 CI 上静默消失（numpy 未声明 —— "44 个用例"的真身）**：靠上面那个
  junit 审计副本，CI 的用例矩阵首次能**逐条**算，结果发现
  `tests/unit/test_anchor_orientation_tool.py` 的**全部 27 条用例在 CI 上根本不存在**，
  而门禁六项**全绿**、覆盖率也没反映。CI 的 junit 里该文件只剩一条 `classname=""` 的条目：
  `Skipped: could not import 'numpy': No module named 'numpy'`。
  - **根因**：该文件用 `pytest.importorskip("numpy")` 测 `scripts/verify_anchor_orientation.py`，
    而 numpy 只被登记为 `_TOOL_ONLY`（"不进 CI 环境"），**从未在任何 requirements 清单里声明**
    → CI 不装 → 整个文件在**收集阶段**被跳掉。
  - **判定更正**：此前把"CI 与本地差 40+ 个用例"写成"属预期、不是缺陷"是**错了一半** ——
    18 条 artifact-gated skip 确实预期，但这 27 条是真实缺陷（`26 = 27 − 1` 条畸形条目占位）。
  - **修法三层**：① `requirements-dev.txt` 声明 `numpy==2.4.5`（本机实测通过的那一版）；
    ② 新护栏 `TestImportOrSkipIsDeclared` —— `importorskip` 的依赖必须声明，**用 AST 而非
    正则**（正则版把本文件 docstring 里的示例误报成"未声明依赖 x"，实测踩到）；③ 门禁新增
    `_container_skips()`：`classname=""` 的 skip 条目（整文件在收集阶段被跳）**直接判 FAIL**
    —— 这是唯一能看见这类失败的检查，它既不进 `failed` 也不进覆盖率。
  - **一般化教训**：`importorskip` 把"依赖缺失"伪装成"跳过"。**凡是"静默跳过"都必须配一条
    "不许静默"的检查**，否则覆盖率绿只证明"跑了的都对"，不证明"该跑的都跑了"。

### 新增

- **`docs/RUNTIME_WATCHDOG.md`**：运行时看门狗的**调研与分层决策**文档。给出
  "是否需要"的三问判据（有无无超时的等待点 / 卡死时用户看到什么 / 兜底能否自动执行）、
  完整的超时覆盖面矩阵、业界范式对标（心跳必须绑定"前进"而非计时器、看门狗不应与
  被观察对象同进程、阈值须按任务类型分级），并据此给出 **P0 已实施 / P1 待确认
  （需 schema 变更 + 状态机写入点）/ P2 可选（SSE 停滞可见性）** 的分层方案，
  以及明确**不推荐**的弱信号方案（用 `audit_log` 最后时间推断卡死 —— 粒度不足，
  省下的 schema 代价会以"误杀正常长任务"还回来）。
- **`scripts/check_leaked_keys.py`**：把 `DEPLOYMENT.md`「Secret 轮换流程」第 4 步工具化 ——
  扫**全部可达历史 blob**（而非逐提交 diff，故"加进去又删掉"的串也跑不掉）并与当前
  `config.json` 比对；输出**只给指纹**（长度 / sha256 前 12 位 / 前 6 字符），
  **退出码 2 = 历史泄露值仍是当前生效值，必须立即轮换**。
- **CI（`.github/workflows/ci.yml`）**：push / PR 到 `main` 时跑**同一个**
  `scripts/release_gate.py`。runner 选 **Windows** —— 与本产品目标平台一致：平台分支
  （`os.name`/`sys.platform`）只在 Windows 上执行，覆盖率数字才与本地门禁可比，跑在
  Linux 上会永久低于阈值。CI **不重复实现任何检查**；artifact 用 **`if: always()`**
  带上门禁报告 JSON、**junit XML** 与 pytest 日志（详见「修复」节 T0.5 —— 只在失败时
  上传会导致"CI 一绿就无据可查"）。
- **依赖声明护栏 `tests/unit/test_declared_dependencies.py`（5 项）**：源码里 import 的
  第三方包必须在清单里声明。**它当场抓到一处真实隐患**：`main.py` 顶层写着
  `from markupsafe import Markup`，而 `markupsafe` **不在任何清单里** —— 只靠
  "Jinja2 恰好会装上它"运行，Jinja2 一换实现就是启动即 ImportError。分档判定：
  本地模块（含 PEP 420 命名空间包）/ 标准库 / `_OPTIONAL`（有"缺失即降级"保护）/
  `_TOOL_ONLY`（精确到位，如 `playwright` 只允许出现在 `ui_e2e.py`），其余必须声明。
  另有两条自检：登记表不得变成摆设（每个豁免条目必须**真的**被 import）、清单必须
  精确锁定。
- **降噪量测工具 `scripts/eval_noise_reduction.py`**：把"降噪少了几条"从手感变成
  **可复现数字** —— 在真实轮次的 `findings` + `page_cache` 上（与 `stage3.py` 完全相同的
  取数口径，`flagged_pages` 由 `page_is_flagged` 复算）跑 R1 + M2 同一条链路，输出逐类
  前后对比、噪声占比、**页级证据闭合核对**（原始页必须仍"有据可查"：要么仍有条目、
  要么落在摘要页清单里）与**硬约束自检**（高价值类型与 `critical` 不得被削弱，削弱即
  退出码 1）。这是"漏检对照"的落地形式：降噪不许靠丢真命中换。
- **用例矩阵差分工具 `scripts/compare_test_matrix.py`**：把"CI 与本地到底差哪些用例"从
  猜测变成一条命令 —— 输入 CI artifact 里的 `gate_junit_*.xml` 与本地
  `pytest --collect-only -q` 的输出，输出"只被本地收集"/"只被 CI 收集"两组（按文件聚合，
  "整文件缺失"一眼可见），并单列 CI 侧 `skipped` 计数。**复用**
  `release_gate._junit_nodeid` 做 nodeid 还原（不复制第二套口径）。退出码 1 = 两侧有差异，
  便于把"差异必须被解释"接进流程。
  护栏 `tests/unit/test_compare_test_matrix.py`（18 例）。**这个工具是被自己的自检修出来的**：
  拿"本地 junit vs 本地 collect 应当完全一致"当不变量，一跑就炸出两个真实缺陷 ——
  ① 参数化 id 里的 `\uXXXX` 是**转义**不是路径分隔符，被 `replace("\\","/")` 改写成
  `/u8bbf` → 与 junit 侧对不上 → 同一批用例**同时出现在两个差集**；② 参数化 id **可以含
  空格**（`[step_no=1 operator=空-operator]`），被尾部 `[^\s]+` 整条丢弃。修后"只被 CI
  收集"= 0，本地 collect 从 2375（被吃掉 7 条）回到 2382。
- **对抗性审查报告 `docs/ADVERSARIAL_AUDIT.md`**：对"构建物可分发 / 日志与状态机 /
  流式输出 / 设置 / pipeline / 鲁棒性与泛化与抗挫折 / 知识库 / 是否引入进化机制 /
  视觉交叉对比"逐项给出**带证据**的结论，并单列一节记下本轮发现的**度量与证据问题**。

### 变更

- **依赖精确锁定**：两个清单从"下界（`>=`）"改为**精确锁定（`==`）**，锁定值 =
  门禁实测通过的那一组（Python 3.11.9 / Windows）。本产品以冻结产物分发，浮动版本
  = CI / 本地 / 发布包各跑一套依赖。已用
  `pip install --dry-run --ignore-installed -r requirements.txt -r requirements-dev.txt`
  验证该组合可**从零解析**（无冲突）。**已知缺口，如实声明**：传递依赖仍由 pip 解析，
  未做全量 hash 锁定。
- **文档口径修正（三处真实缺陷）**：① `README.md` / `CLAUDE.md` 的测试命令写着
  `--cov=.` —— 会叠加出一个**与门禁不同的、更低的**覆盖率数字（口径只在 `pytest.ini`
  与 `scripts/release_gate.py` 各一份且刻意保持一致）；② 同处 `--timeout=30` 依赖一个
  **未声明**的 `pytest-timeout`，照抄即报错；③ `README.md` 只装 `requirements.txt`，
  **照它做根本跑不了测试**（缺 pytest）。另 `CLAUDE.md` 长期停留在一个**早已过期**的
  覆盖率/用例数快照上 —— 已改为"具体数字一律以门禁报告为准"，不再写会腐烂的常量。
- **R1–R3 降噪落地（真实轮次实测 −38.5%，零信号损失）**：三项都按"先定位 → 再解决 →
  再测试"做，每项都有实证依据与机检不变式（详见 `docs/NOISE_REDUCTION_TODO.md`）。
  - **R1 消灭自指元噪声**：`handwritten`（65 条）与「整体识别置信度较低」（7 条）讲的是
    **工具看不清**而非**记录有问题**，却计入 finding 计数。新增
    `core/finding_noise.py::reduce_self_referential_noise`（与 M2 的 completeness 降噪
    **分开**：前者按文档属性聚合、后者按结构缺失 kind 细分），整份文档聚合成 1 条并携带
    **完整页清单**。实测 442 → 372（−70）。
  - **R2 跨页批号一致性改投票归一**：规则层原报 **"5 组不同批号"（19 个原始变体）**，
    逐组看却是同一批号的 OCR 写法（`^*` 符号噪声、`/` 分隔、`N`↔`1` 单字符）。归一化先
    剥离**除 `-` 外的全部非字母数字**，再由投票决定：多数覆盖 ≥60% 时把**编辑距离 ≤1**
    的组吸收为变体，**吸收数量明示**在描述里。实测 **5 组 → 2 组**；投票不决定性或距离
    >1 一律照旧全报（`B202201` vs `B202202` 仍报 critical）。
  - **R3 日期歧义交叉约束**：新增 `core/rules/year_vote.py`（纯函数、无 IO）—— 全文档
    年份页支持度 + 批号 `YYMMDD` 段作为**独立佐证**，**五道闸同时成立**才归一
    （有佐证 / **不相邻** / 单字符差异 / 多数基数 ≥3 页 / 投票决定性）。接入 **4 个发射点**
    （`_check_year_contradiction`、`_check_time_reversal_cross_page`、
    `_check_signature_time_anomaly`、`_check_signature_order`），使**一条单字符年份误读
    不再派生第二条 finding**。**不改写任何原始文本**。"相邻年"闸专门保护真实的跨年记录
    （2024/2025）。实测 `year_contradiction` 9 → 3。
  - **净效果**：442 → **272（−38.5%）**，噪声占比 75.8% → **60.7%**，
    `critical` **29 → 29**，高价值类型一条未减，`eval_findings.py` 的
    `P/R/F1 = 1.0（TP=10 FP=0 FN=0）`。
- **记下一处"证据不可复算"的问题（本项目自己的度量纪律）**：审查发现
  `docs/ARCHITECTURE_AND_QUALITY_REVIEW.md` 的**关键证据轮** `95a27d88-52b`
  （347 findings / 51 页 / 1894s）在两份现存库中**都不存在** —— 它当时跑在
  `%TEMP%/pbc_e2e_appdata` 隔离库上，事后被清理，故文档里那几个数字**今天无法复算**。
  同类结论在**仍保留**的真实轮次 `0c5cfb00-897` 上可复现，本轮所有量测均改用它。
  **纪律**：任何"决定性证据轮"必须落到**常驻库或入库产物**上，不能只活在 `%TEMP%`。
  同源发现：`core/finding_noise.py`（2026-09-11 引入，文档记"实测 −65.8%"）**从未在真实
  轮次上验证过**（库中最新真实轮次是 2026-08-26，早于该模块）—— 那个数字是离线重放
  数字，不是端到端数字。本轮首次在真实轮次上量到它（372 → 272）。

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

### 上传限额：补上页数上限 + 消除 4 处重复写死

起因：审查"超出能力边界是否给良好提示"时发现**页数完全没有上限** —— 体积
200MB 挡不住"低密度但极长"的 PDF（数字排版件可能 50KB/页，200MB 能装下数千页），
而数千页按实测 9.0 s/页 OCR 会先撞 `POLL_TIMEOUT_MAX=3600s`（100 页即封顶）→
用户等满 1 小时后才收到"轮询超时"。能在上传时说清楚的事，不该让用户在服务端
超时后才知道。

- **单一真值 `config.UPLOAD_LIMITS`**：此前同一个 200MB 被**四处各自写死**
  （后端常量、后端错误消息、`static/upload.js` 比较式、`templates/upload.html`
  提示文字），任一处调整都会静默漂移成"前端放行、后端拒绝"。现由一处派生，
  并由源码扫描护栏兜底。前端**刻意不设兜底数值** —— 兜底副本正是漂移的来源；
  注入缺失时跳过预检交由服务端判定（服务端本来就权威）。
- **硬上限 200 页**（`MAX_UPLOAD_PAGES`）：拒绝消息含**真实页数 + 上限 +
  下一步动作**（"请按批次拆分后分别上传"），对标市面成熟做法（amaise 的
  `Too many pages (max 2,000) — split the PDF`）。定标依据是**实测**：真实 51 页
  批记录 Stage1 OCR 460.5s（9.0 s/页）、Stage2 逐页 LLM 1163.7s（22.8 s/页）——
  200 页对应 OCR 预估 1800s，对既有 3600s 轮询上限留 **2× 余量**抗拥堵。
  ⚠️ 云厂商的 1,000~3,000 页**不可移植**：上限建立在"单页成本"上 —— 三大云纯文本
  抽取约 $1.50/1,000 页，本链路每页跑手写 OCR + 逐页 LLM（按调用性质属 $10~50/1,000
  页的生成式档）。可移植的量是**墙钟时间预算**而非页数。详见
  [docs/UPLOAD_LIMITS.md](docs/UPLOAD_LIMITS.md)。
- **软阈值 80 页**（`WARN_UPLOAD_PAGES`）：放行，但响应带 `page_warning`
  （预估耗时）+ `audit_log` 留痕 + 前端把跳转延迟延长到 5s（1.5s 读不完一句
  耗时提示，等于没有告知）。阈值之下一律不发声 —— 对常态文件刷告警等于没有告警。
- **页数读取失败（=0）一律放行**：部分损坏 PDF 云端 OCR 各有容错，拦掉等于把
  "可能成功"变成"必然失败"。这条既有契约由测试钉死，新增上限不得把它变严。
- **顺带发现并修掉一处隐藏的漂移**：`core/mineru_client.py` 有 MinerU 通道**厂商**
  的独立 200MB 上限。它与本产品策略数值相同纯属巧合 —— 一旦准入上限超过它，
  一份被放行的文件会在 failover 到 MinerU 时**流程中途失败**。已抽成命名常量
  `MINERU_MAX_UPLOAD_BYTES`，并加护栏机检 `UPLOAD_LIMITS["max_bytes"] <= 厂商上限`。
- **新增护栏**：`tests/unit/test_upload_limits.py`（24 项：纯函数边界语义含
  "等于上限放行/读取失败放行"、误配置收敛、单一真值派生、**防重新写死的源码扫描
  含正反对照**、策略 ≤ 厂商上限、**引用纪律**——禁止把"每页耗时"归给第三方厂商
  + 外部数字须带出处）+ `tests/integration/test_api_upload_page_limit.py`
  （5 项端点级：真实 HTTP 上传超限被拒且**不落库、不留孤儿目录**、等于上限放行、
  大文件带预估、小文件不告警、页数读失败不拦）。
- ⚠️ **未验证项**：200 页是**基于 51 页实测的线性外推**，未以 200 页文件实跑
  （分片/并发/上游队列行为可能非线性）。建议并入人工标注集工作一并验证。

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

### 降噪续补（幻觉自检误报修复）

**问题**：真实 51 页轮次中 **31/51 页（60%）** 挂"疑似幻觉"横幅。用户可感知的
"OCR 后噪声大"里，相当一部分其实是**工具自检自己的误报**，而非记录缺陷。

**定位**（真实数据 + 代码复现，非推测）：
- `_grounding_check` 的 `text` 经 `_normalize_grounding_text` **去掉了所有空白**，
  于是 `<td>0.15</td><td>0.15</td>` 被粘成 `"0.150.15"` —— 短数字分支的
  "前后非数字"边界判据**永不成立**。实证：真实第 9 页 21 处 `0.15`（压力列）
  全被判未命中；被标记的清一色是 3 位数字（`0.15/0.16/0.17`），而 4 位数字
  （`0.974`）因走 token 分支从不被标记 —— **症状与根因完全一致**。
- 第二层同类缺陷：比对**原始 token** 时，OCR 把符号/单位/前后缀粘进同一单元格
  （`-0.094 MPa`、`100.4%`、`见附表14`），token 与 LLM 输出的数值分量不同形
  → 真实存在的值被判幻觉。真实数据上这批占剩余误报的大头（真空度负值、
  含量百分比、附表引用）。

**修复**（`core/page_analyzer.py::_value_grounded`）：token 匹配提前到长度分支
**之前**（不论数字长短都先走 token），并比对 **token 内的数字核心**而非原始
token。两条不变式保住"修过头"防线：① 匹配只在**单个 token 内部**发生 ——
`12`+`50` 不得合成 `1250`；② 只允许**相等**或**小数尾零延展** —— `0.974` ↔
`0.9740` 是格式差异，而整数补零（`125` ↔ `1250`）是 **10× 量级错误**，必须拒绝
（较修复前**更严**）。

**实测效果**（真实 51 页数据重算）：**31/51（60%）→ 2/51（4%）**。剩余 2 页经
核查**都是真阳性**：p10 `1750 kg` 在 OCR 原文中出现 **0 次**（LLM 产出原文不存在
的数值）；p39 `7800L` 被 OCR 与批号粘成 `A010297-M2501047800L`，无法区分"忠实
转写"与"跨单元格拼接幻觉" → fail-closed 报出正确。即：**自检信号从 60% 噪声
变成 4% 可用信号**。

新增 8 项回归护栏（含 4 项"防修过头"的反向用例）。全量 2242 passed。

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

### 随本版发布的前序修复（`e2417cd`、`4273c1f`）

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

### 变更（调研文档入档）

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
