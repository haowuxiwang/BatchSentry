# 分层与工程质量审查（文件上传 / OCR / LLM / 规则）

> **Round 81（2026-10-10，R85 第二十五批）** · 方法：源码结构 + 依赖方向机检 +
> 真实日志（`logs/pharma.log` / `logs/pipeline.log`）实测 + 受控重放探针。
> ⚠️ 本文只写**有证据的结论**；未验证项集中在 §6。

## 1. 结论摘要

| 层 | 结论 | 关键证据 |
|---|---|---|
| **文件上传** | **良好** | 8 MB 分块流式落盘（不整读进内存）；体积上限来自 `config.UPLOAD_LIMITS`（**单一真值**，前端预检同源）；magic-bytes 白名单独立于扩展名；图片统一转 PDF（Pillow EXIF 纠正 + PyMuPDF 300 DPI）；超大图 `_MAX_IMAGE_PIXELS` 防 DoS；并发额度检查与 `INSERT` **同 `db_lock`**（防 TOCTOU） |
| **OCR** | **良好** | 双后端（PaddleOCR-VL 主 → MinerU 备）+ **统一返回契约** `[{"markdown":{"text":…},"page_count":N}]`；`job_id` 经 `ContextVar`+`JobIdFilter` 注入本模块**全部**日志；完整性判定是**证据式**（`assess_ocr_page` 不因"字符少"就否决） |
| **LLM** | **良好** | 协议适配器隔离（`base` / `openai_adapter` / `anthropic_adapter`）；`client` 只负责重试 / 退避 / JSON 解析 / 用量审计，**报文翻译归 adapter**；温度**单一真值**（本批新增） |
| **规则** | **良好** | `RULE_REGISTRY` 是规则元数据**单一来源**（id/type/severity/basis/check/开关）；`engine.py` 单独成文件并**明文 patch 兼容契约** + 护栏 |
| **分层清晰度** | **清晰、无环** | `core/` 不 import `api/`；`llm/` 不 import `core/`\|`api/`（叶子）。方向：`api → core → llm → config` |

## 2. 分层结构（机检事实）

```
api/            ← HTTP 路由（jobs / review / report / settings）
  └─ core/      ← 流水线（pipeline.stage1/2/3）+ 判定（page_analyzer / rules/*）+ 降噪
       └─ llm/  ← 协议适配（adapters）+ 重试/审计（client）  —— 叶子，不反向依赖
db/             ← 仅被 api/* 与 core/pipeline/*（+ core/watchdog.py）使用
config.py       ← 全局配置单一真值
```

- **`core/` 对 `api/` 零依赖**（`grep '^from api\.' core/` 为空）—— 依赖方向无环。
- **`llm/` 是叶子**（不 import `core/`/`api/`）⇒ 可独立测试与替换。
- ⚠️ **`core/cross_page_analyzer.py` 只是 65 行向后兼容 shim**（原 1700 行已于 2026-08
  拆进 `core/rules/`，2026-09 编排体迁到 `core/rules/engine.py`）。它**不是**"排障黑洞"；
  读跨页逻辑应看 `core/rules/`。

### 2.1 两个"良好实践"范例

- **`core/rules/registry.py`**：把每条规则收敛为一个 `RuleSpec`，`analyze_cross_page`
  由注册表驱动。docstring 明确"为什么"（此前新增规则要同时改 import / 调用 / 日志 /
  依据 / 检索词 / 中文名 / 前端映射，漏一处就"规则跑了但前端显示英文/无依据/不可检索"）。
- **`core/rules/engine.py`**：把"为什么单独成文件"（SRP）与 **patch 兼容契约**写进模块
  docstring —— 点名 7 个协作者**必须**在调用期从 `core.rules` 解析，否则模块级 from-import
  会让打桩**静默失效**（测试转而调真 LLM，不报错、只是变慢变贵）。并给出护栏文件名。

## 3. 日志情况（实测）

**格式**（`logging_config.py:145`）：

```
2026-10-09 16:15:05 [DEBUG] [req=- job=-] chardet.charsetprober: ISO-8859-8 Hebrew confidence = 0.0053…
```

每行带 `request_id` + `job_id` ⇒ 可按请求 / 任务反查全链路。**轮转**：
`pharma.log` 10 MB×5、`error.log` 5 MB×3、`pipeline.log` 10 MB×5。
`core.pipeline` / `core.ocr_client` / `core.mineru_client` / `core.page_analyzer` /
`core.rules` / `llm.client` **额外**落 `pipeline.log`（分层隔离）。

**噪声量化**（`logs/pharma.log`，17,406 行）：

| 级别 | 行数 | 占比 |
|---|---|---|
| DEBUG | 16,014 | **92.0%** |
| INFO | 1,255 | 7.2% |
| WARNING | 54 | 0.3% |
| ERROR | 6 | 0.03% |

按 logger 名计数（第三方）：`aiosqlite` **8,116**（46.6%）、`urllib3` 5,899、`httpcore` 706、
`chardet` 636、`openai` 464。

> ⚠️ **口径陷阱（已写入 PITFALLS §五十四 教训五）**：该文件跨 **7 周**（2026-08-20 →
> 10-09）。按**日期切一刀**（`2026-10-0x`）后，当天 613 行**全部**是 `chardet`
> —— `urllib3` / `aiosqlite` 的大头都在 `#145` 修复**之前**。混算会得出
> "92% 噪声、名单全无效"的**错误结论**。

**已修（本批，0-28）**：`logging_config._NOISY_THIRD_PARTY_LOGGERS`（`#145` 逐库降噪名单）
漏掉了 `aiosqlite` 与 `chardet`（实测 `getEffectiveLevel()` = DEBUG，而名单内各库 = WARNING）
⇒ 两者补入名单。护栏 `tests/unit/test_logging_config.py::TestThirdPartyNoiseSuppression`（3 条）+
变异 M6–M9 CAUGHT。

**一方信号密度**（`logs/pipeline.log`，22,839 行）：`core.pipeline` 12,282 / `llm.` 2,995 /
`core.mineru_client` 2,922 / `core.ocr_client` 1,417 / `core.rules` 170 /
`core.page_analyzer` 157。⇒ **判定层（page_analyzer / rules）在专门文件里有足够留痕**，
`pharma.log` 里看不到它们只是被第三方 DEBUG 淹没（本批已修）。

## 4. 与 `mattpocock/skills` 的对照

经 7897 拉取并研读，三份 skill 与本仓直接相关：

| skill | 用到的概念 | 在本仓的落点 |
|---|---|---|
| `engineering/codebase-design` | deep module / **seam** / deletion test /「**一个 adapter = 假 seam，两个才是真 seam**」 | `llm/adapters/` 有 `base` + **两个**真实协议适配器（openai / anthropic）⇒ 是真 seam，不是装饰 |
| `engineering/diagnosing-bugs` | Phase 1 **先建紧反馈环**；非确定 bug 的目标是**提高复现率**；官方手法「**Replay a captured trace**」 | 本批探针即"捕获提示 + 重放"，绕开 `analyze_page` 内的重试链（页 43 单次生成 >100 s 会超时） |
| `productivity/writing-for-agents` | **单一真值** / 反 sediment / context pointer / **no-op 检验** | 温度单一真值；`#145` 名单的补全；`engine.py` 把"为什么"写进 docstring |

## 5. 判定产出的可复现性（受控重放实测）

方法：用**桩 client** 跑一次真 `analyze_page` 捕获由**真代码**构造的 (system, user)，
再用**真 client** 对同一 (system, user) 以指定温度重放 R 次（`retries=1`、`audit_ctx=None`），
自己 `json.loads` 计 `findings` 条数。源：`data/pharma.db`（job `749ead79-7b8`）。

| 页 | temp=0.1 | temp=0.0 |
|---|---|---|
| 4（raw_html 1971 / prompt sys=3553 user=4440） | `[2,2,2]` spread=0（0.0%，n=3） | `[2,2,3]` spread=1（50.0%，n=3）；**复测 n=6 → `[2,2,3,2,2,2]` spread=1（50.0%）** |
| 2（raw_html 1581 / prompt sys=3553 user=4054） | `[3,3,4]` spread=1（33.3%） | `[3,4,3]` spread=1（33.3%） |

**结论**：在本上游（SiliconFlow / `deepseek-ai/DeepSeek-V3.2`），**`temperature=0.0` 与 `0.1`
都观测到运行间方差**（每格 n=3，page 4 复测 n=6 仍 spread=1）。⇒ 把温度固定为 0.0
**不足以**获得可复现的 findings 计数。

> ⚠️ **不得**据此说"修复无效"：温度是**本仓唯一可控**的随机源，把它固定为 0.0 仍是
> 正确做法（消除"客户端主动加噪"这一项，并让输出落在最高概率路径）。**但**残余方差
> 来自上游 ⇒ **单次计数依然不得当回归判据**（0-15 的判据因此**加强**，不是放松）。
> 样本量小（n=3–6/格，2 页）⇒ 只能断言"**存在**运行间方差"，**不能**给出方差量级 /
> 分布。证据：`devlogs/_verify/r85_temp_probe.py` → `_r85_probe5.out`（n=3）、
> `_r85_probe6.out`（page 4，n=6）。

## 6. e2e 驱动的前置条件（实测暴露）

`tests/e2e_rejections.py`（产物级入口拒绝矩阵，34 例）有一个**静默前置条件**：
产物必须已配置**真实 LLM provider**。未满足时 `api/jobs/upload.py` 对**每一个**上传
都返回 400（`Upload rejected: no LLM provider configured`）⇒ 矩阵里"期望 400"的用例
**全部假绿**、"期望 200"的用例全部变红 ⇒ 表面 **13/34**，实则**整轮零判别力**。

- **只有阴性对照能暴露它**：矩阵含 5 个设备名阴性对照 + 7 个敌意文件名对照 ⇒ 才把
  13/34 暴露出来。若矩阵只写"期望 400"的用例，一个"全部拒绝"的坏环境会拿满分。
- **已加前置 fail-fast**（`_llm_provider_configured()`，读 `GET /api/settings` 的
  **`llm.providers[].configured`**），未满足即 rc=2 并打印 seed 路径。
- **双向验证**：正控（有 config）⇒ rc=0 / **34/34**；负控（隐藏 config）⇒ rc=2 + 明确提示。
  ⚠️ 首版按**顶层** `providers` 取值 ⇒ 恒判 False（把"配置齐全"也拦下）——
  **只有正控能抓到**。⇒ 一个守卫，**两个方向都要验**。
- 冻结版只读 `%APPDATA%/PBC/config.json`（**不**读仓库根 `config.json`）⇒ 换机 /
  清临时目录后必须重新 seed。这是 e2e 的**环境契约**，不是产品缺陷。

## 7. 未验证边界（**不得**读成"已通过"）

1. **上游残余方差的量级未测**：上表 n=3/格、2 页，只能证明"存在"，不能给分布 /
   置信区间。要拿数字须 N 次重复跑（见 TODO 0-29）。
2. **5 个 job 并发对上游速率的实际影响未测**：本批只保证**准入**不再拒第 4/5 个。
3. **Electron GUI 层 e2e 在受限沙箱内不可复现**（历史结论，见 MEMORY §12）——
   本环境**无法取证**，须在真实终端复跑。
4. **判定质量无人工标注集** ⇒ 漏报率 / 误报率**无可信数字**（见 §8）。
5. `temperature=0.0` 是否被网关**原样透传**（还是被当作 falsy 换成默认值）**未验证**
   —— 这是上面方差的一个可能解释，但本环境无法证实。

## 8. 与「判定质量无可信数字」的关系（G3）

`docs/FINDING_GROUND_TRUTH.json` 的唯一 pass/fail 判据是**合成语料**的规则层 F1
（**零 LLM / 零 OCR**）⇒ 对"真实批记录上的漏报/误报"**给不出数字**。
`real_baselines` 块（784 条）是**历史版本 + `mineru`** 的产物，与当前不可比，
且**无任何代码消费者**（R77 已加 `criterion:false` + AST 机检禁止当判据）。

⇒ 在拿到人工标注集之前，**唯一诚实的表达方式是"重复跑给区间"**（见 0-29），
而不是给一个单次计数。**不得**用单次 findings 数充当精度/召回。
