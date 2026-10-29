# Security Policy

> 本项目处理**药品批生产记录**，使用者对数据的合规责任高于一般开源项目。
> 请先读下面的「数据边界」，再决定是否上传真实记录。

## 支持版本

| 版本 | 支持状态 |
| --- | --- |
| 最新 release / `main` 分支构建 | ✅ 接收漏洞报告 |
| 更早的构建产物（`release-archive/` 中的历史包） | ❌ 不接收 —— 请先复现到最新构建 |

## 报告漏洞

**不要**在公开 Issue 里贴漏洞细节、真实批记录、企业名、员工姓名或任何密钥。

请使用 GitHub 的私密通道：仓库页面 **Security → Report a vulnerability**
（GitHub Security Advisory）。报告请尽量包含：

1. 版本与构建来源（`dist-electron/win-unpacked/PROVENANCE.txt` 的 `git_head` / `version`）；
2. 复现步骤与最小输入（**用合成件**，不要用真实批记录）；
3. 影响面：是"结论方向错误（把不合规判成合规）"还是"仅提示噪声"—— 前者按最高优先级处理。

**响应方式**：尽力在 7 天内确认、30 天内给出修复或缓解说明；这是开源项目的尽力而为承诺，
不构成商业 SLA。修复发布后会在 GitHub Security Advisory 中公开（unless 报告人要求延迟）。

## 数据边界（重要）

- 上传的 PDF 会被发送到**你配置的第三方服务**：OCR（PaddleOCR-VL / MinerU）与 LLM
  （SiliconFlow / DeepSeek 等 OpenAI 兼容端点）。本仓库**不含**任何模型权重，也不做本地推理兜底。
- 因此**数据出境/委托处理**的合规评估由部署方自行完成；不能出境时请改用可离线部署的后端
  （如 docling 第三对照引擎、自建 OpenAI 兼容端点），并在内网隔离环境运行。
- 仓库、Issue、PR、测试夹具中**不得**出现真实批号、企业名、员工姓名、签名图或密钥。
  已有的脱敏工具见 `scripts/scrub_pii.py`（映射表**故意**不入库）。

## 密钥

- 密钥只放在运行时配置（`%APPDATA%/PBC/config.json` 或环境变量），**不入库**。
- 一旦怀疑泄漏：先到服务商处**轮换**，再清理仓库（删除文件抹不掉 git 历史），流程见
  [`DEPLOYMENT.md`](./DEPLOYMENT.md) 的「Secret 轮换流程」。
- 本仓库启用推送防护（`.githooks/pre-push`）：默认**拒绝推送 tag**，防止把已清除的旧历史重新公开。
  启用方式：`git config core.hooksPath .githooks`。

## 已知边界（不是漏洞，但影响结论）

- 本工具**不替代人工放行**，也未按 21 CFR Part 11 / GMP 附录 11 做计算机化系统验证；
  详见 README 的「合规边界与免责声明」。
- OCR 对手写体的误读是**已知失效模式**：读错数字可能把越界读成合规（见 `docs/TODO.md` 的 0-72），
  关键参数的手写读数会被强制转人工核对，但这**不**证明其余读数都正确。
