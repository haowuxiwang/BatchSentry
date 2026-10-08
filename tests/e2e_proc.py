"""E2E / 冒烟测试的服务子进程启动助手（单一来源）。

背景（2026-09-14 M8 实测定位）：
以 ``stdout=subprocess.PIPE`` 启动 long-running 服务进程却**从不排空**，是一类
静默死锁 —— 服务端日志写满 OS 管道缓冲（Windows 约 64KB）后，子进程阻塞在
``write()``；若是 asyncio 服务（uvicorn），阻塞发生在事件循环线程 → 事件循环
停摆 → 后续所有 HTTP 请求 read timeout。

症状特征：**卡点漂移**（第一次卡在请求 7，第二次卡在请求 8…），因为触发点
只取决于累计日志量，与具体请求无关 —— 极易被误判为产品缺陷。

对照实验（``devlogs/diag_pipe_block.py``，同一 APPDATA、同一请求序列）：
    stdout=DEVNULL → 10 passed / 0 failed
    stdout=PIPE    → 8 passed / 8 failed（稳定复现）

因此约定：long-running 服务进程的 stdout/stderr 一律落**日志文件**
（既保留诊断能力，又不会阻塞）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 仓库根目录（本文件位于 <root>/tests/）。**不要把产物路径写成绝对盘符** ——
# 那会让 e2e 绑定到某台机器的目录结构，且无法指向真正要分发的那份副本。
REPO_ROOT = Path(__file__).resolve().parent.parent

# 产物路径可用环境变量覆盖：既能测 PyInstaller 的直接产物（默认），
# 也能测 Electron 打包后**内嵌**的那份副本（dist-electron*/win-unpacked/
# resources/pbc-server），后者才是用户实际运行的东西。
EXE_ENV = "PBC_E2E_EXE"
DEFAULT_EXE = REPO_ROOT / "dist" / "pbc-server" / "pbc-server.exe"

# LLM 密钥**绝不入库**：曾经把真实 key 硬编码在 3 个 e2e 脚本里，随提交进入
# 历史且已推送（删掉文件也删不掉历史，只能轮换）。改用环境变量注入；
# 未提供时不阻断（LLM 步骤降级），但会明确提示。
#
# ⚠️ 变量名**不得绑定某一提供方**（B9-7 命名债，2026-09-21 实测）：
# 语义是 provider-agnostic 的（配 :data:`LLM_PROVIDER_ENV` 使用），旧名却叫
# DEEPSEEK —— 本轮实际往里塞的是**硅基流动**的 key，等于把"名字骗人"固化进
# 发布流程。改名为 PBC_E2E_LLM_KEY；**旧名保留兼容**（先读新名，再回退旧名并
# 提示弃用），避免已写好的发版脚本一夜之间静默失效（静默失效 = LLM 步骤降级
# 而冒烟照绿，正是本项目反复踩的坑）。
LLM_KEY_ENV = "PBC_E2E_LLM_KEY"
LLM_KEY_ENV_LEGACY = "PBC_E2E_DEEPSEEK_KEY"

# LLM 提供方：冒烟必须把密钥发给**与该密钥匹配**的那一家。
# 反例（2026-09-17 实测）：`e2e_frozen.py` 曾写死
# ``{"llm_provider": "deepseek", "deepseek_api_key": key}``，而注入的却是
# **硅基流动**的 key ⇒ 请求打到 ``api.deepseek.com`` 得 401。此前长期"绿"
# 是因为样例 PDF 是**空白页**：Stage 2 无内容可分析 ⇒ 从不调用 LLM ⇒ 401
# 从未发生（假绿；夹具改为含真实文字后当场暴露）。
# 默认 siliconflow 沿用本项目"LLM 凭据即硅基流动 key"的既有约定；
# 运行方可用 :data:`LLM_PROVIDER_ENV` 显式覆盖成自己真实在用的提供方。
LLM_PROVIDER_ENV = "PBC_E2E_LLM_PROVIDER"
DEFAULT_LLM_PROVIDER = "siliconflow"

# LLM 模型：与提供方**配对**注入（写进设置时拼成 `<provider>_model`）。
# ⚠️ 必须可配，否则端到端会静默沿用产品默认值 —— 而默认值未必是运行方的可用档位。
# 实测（2026-09-23）：产品默认 `deepseek-ai/DeepSeek-V4-Pro` 是**收费**模型，
# 免费档账号用它必得 `402 code=30001 balance insufficient`，流水线随即降级到
# `error`；而**同一把 key 的小请求探针仍返回 200**（费用按请求规模预授权）
# ⇒ 极易被误读成"产品 LLM 链路坏了"。空值 = 沿用产品默认（此时失败才真在产品侧）。
#
# 🔴 变量名**必须**与仓库根 `e2e_run.py`（其 ``resolve_llm_model`` 是模型解析的
# 唯一权威点）一致 —— 曾差点在此另起 `PBC_E2E_LLM_MODEL`，那就是"同物不同名"：
# 运行方设对一个、另一个静默落到默认档，正是上面那条 402 误归因的翻版。
# 一致性由 `tests/unit/test_e2e_proc_helper.py::test_model_env_name_matches_root_driver`
# 机检。
LLM_MODEL_ENV = "PBC_E2E_MODEL"


def llm_provider(default: str = DEFAULT_LLM_PROVIDER) -> str:
    """返回 e2e 用的 LLM 提供方名（小写），由 ``PBC_E2E_LLM_PROVIDER`` 覆盖。

    与 :func:`llm_key` 配对使用：``{llm_provider()}_api_key`` 才是该密钥
    应当归属的字段名。**不要**在调用点再写死提供方名。
    """
    return (os.environ.get(LLM_PROVIDER_ENV) or default).strip().lower()


def llm_model(default: str = "") -> str:
    """返回 e2e 要用的 LLM 模型名（由 ``PBC_E2E_MODEL`` 覆盖）。

    与 :func:`llm_provider` 配对使用：写进设置时字段名是
    ``f"{llm_provider()}_model"``。**不要**在调用点再写死提供方名。

    与根目录 ``e2e_run.resolve_llm_model`` **共用同一个环境变量**（只是本函数
    不设历史默认基线，故默认返回空串）—— 两个 driver 必须能被同一份环境配置驱动。

    返回空串表示**沿用产品默认值** —— 这是有意义的档位（验的正是"发出去的那份
    默认值"），所以空串不是错误，调用方不得因此判失败；但一旦非空，就必须校验
    它**确实生效**（否则验的是别的模型，结论无意义）。
    """
    return (os.environ.get(LLM_MODEL_ENV) or default).strip()


def resolve_exe(default=None) -> str:
    """解析被测 pbc-server 可执行文件路径。

    优先级：``PBC_E2E_EXE`` 环境变量 > ``default`` > :data:`DEFAULT_EXE`。
    文件不存在时抛 :class:`FileNotFoundError`（让"测错了目标"立刻暴露，
    而不是静默跑成 0 断言）。
    """
    exe = os.environ.get(EXE_ENV) or str(default or DEFAULT_EXE)
    if not Path(exe).is_file():
        raise FileNotFoundError(
            f"pbc-server 可执行文件不存在: {exe}\n"
            f"  先构建，或用 {EXE_ENV}=<路径> 指向要测的产物副本。"
        )
    return exe


def llm_key(default: str = "") -> str:
    """返回 e2e 用的 LLM 密钥。

    取值顺序：``PBC_E2E_LLM_KEY``（当前名）> ``PBC_E2E_DEEPSEEK_KEY``（旧名，
    仍兼容但会打印弃用提示）> ``default``。

    未设置时返回空串 —— 调用方据此把 LLM 步骤**如实降级**（并记入覆盖清单），
    而不是让整条流水线假装"已配置"。

    ⚠️ 旧名兼容是**过渡**而非长期特性：它存在的唯一理由是防止已写好的发版命令
    静默失效。读到旧名时提示一次，便于尽快完成迁移。
    """
    new = os.environ.get(LLM_KEY_ENV)
    if new:
        return new
    legacy = os.environ.get(LLM_KEY_ENV_LEGACY)
    if legacy:
        print(
            f"[WARN] 环境变量 {LLM_KEY_ENV_LEGACY} 已弃用，请改用 {LLM_KEY_ENV}"
            f"（两者等价；旧名将在一个发布周期后移除）",
            file=sys.stderr,
        )
        return legacy
    return default


def llm_key_env_display() -> str:
    """给人类看的变量名说明（驱动脚本的提示文案统一用它，避免三处各写一份）。"""
    return f"{LLM_KEY_ENV}（兼容旧名 {LLM_KEY_ENV_LEGACY}）"


# ── LLM 失败归因：把「凭据无效」与「账户欠费」分开 ─────────────────────
#
# 由来（2026-09-30 实测，一次**真实的误归因**）：
#   `tests/e2e_frozen.py::probe_llm_credential` 原本只用 **免费** 端点
#   `GET {base_url}/models` 做"直连正向对照"。该端点不消耗额度，所以它只能
#   回答"这把 key 是不是真的"，**回答不了**"这个账户还付得起钱吗"。
#   于是上游返回 `402 code=30001 account balance is insufficient` 时：
#     · `/models` 照旧 200 ⇒ 判 `ok` ⇒ 报告写"**凭据有效**，故此处失败是
#       **产品缺陷**（优先查：凭据是否被发给了错误的提供方/端点）"；
#     · 而**同一份报告的另一行**写着 `首条错误：402 ... balance insufficient`
#       —— 自相矛盾，且把排查方向引向代码，真因却是账户余额。
#   实测证据（`devlogs/_verify/probe_llm_balance_vs_credential.py`）：
#   `GET /models` → 200；`POST /chat/completions`（max_tokens=1）→ 402；
#   换 3 个模型（DeepSeek-V3.2 / V4-Flash / Qwen3.8-27B）**全部 402**
#   ⇒ 与"产品挑了个贵模型"无关，是账户层面付不起。
#   ⚠️ 本文件的 `LLM_MODEL_ENV` 注释里**早就记过这个坑**（2026-09-23：免费档
#   账号用收费模型必得 402，而"同一把 key 的小请求探针仍返回 200"），但当时只
#   落在"选档位"的建议上、没落到探针实现上 —— 所以它又发生了一次。
#
# 纪律：**判不了 ≠ 通过**。除 `ok` 外的任何 verdict 都不得被读成"产品没问题"，
# 也不得被读成"产品有问题"；调用方须按 fail-closed 处理。

VERDICT_INVALID = "invalid"   # 凭据确凿无效（401/403）⇒ 环境问题
VERDICT_BILLING = "billing"   # 凭据有效但账户欠费（402）⇒ 环境问题
VERDICT_OK = "ok"             # 凭据与额度都可用 ⇒ 此时失败才真是产品缺陷
VERDICT_UNKNOWN = "unknown"   # 判不了 ⇒ fail-closed，按产品缺陷处理


def classify_llm_probe(free_status, metered_status):
    """由两次探测的 HTTP 状态推导归因。**纯函数**（不做 I/O），可直接单测。

    参数是**事实**（两个 HTTP 状态码；``None`` 表示该项未取得/未尝试），返回
    :data:`VERDICT_INVALID` / :data:`VERDICT_BILLING` / :data:`VERDICT_OK` /
    :data:`VERDICT_UNKNOWN` 之一。

    ``free_status``    —— ``GET /models``（**免费**，不消耗额度）。
    ``metered_status`` —— ``POST /chat/completions``（``max_tokens=1``，**计费**），
    用**应用自己上报的那个模型**发一次最小请求。**只有它能把 401 与 402 分开。**

    | free | metered | 结论 |
    |---|---|---|
    | 401/403 | 任意 | invalid —— 上游确凿拒绝该凭据 |
    | 200 | 402 | **billing** —— 凭据是真的，但账户付不起 |
    | 200 | 200 | ok —— 两者都可用 ⇒ 流水线仍失败**才是**产品缺陷 |
    | 200 | 401/403 | invalid |
    | 200 | None/其它 | unknown（**不得**据"免费端点 200"就判 ok）|
    | 其它 | 任意 | unknown |
    """
    if free_status in (401, 403) or metered_status in (401, 403):
        return VERDICT_INVALID
    if free_status == 200:
        if metered_status == 402:
            return VERDICT_BILLING
        if metered_status == 200:
            return VERDICT_OK
        return VERDICT_UNKNOWN
    return VERDICT_UNKNOWN


def llm_failure_attribution(verdict, detail):
    """把 verdict 翻成一句**给人看的归因**（文案的唯一实现）。

    ⚠️ 措辞是**承重**的：``ok`` 分支写"产品缺陷"，``billing`` / ``invalid``
    分支必须写"环境问题、非产品缺陷"。曾有一版把**欠费**报成产品缺陷，把排查
    引向代码（见本节顶部由来）。回归护栏：
    ``tests/unit/test_llm_failure_attribution.py``。
    """
    if verdict == VERDICT_INVALID:
        return (f"归因：同一 base_url/凭据直连探测得 {detail} ⇒ "
                "**上游确凿拒绝该凭据**（环境问题，轮换密钥后复跑）；"
                "非产品缺陷，但本轮 LLM 链路因此未被覆盖。")
    if verdict == VERDICT_BILLING:
        return (f"归因：同一凭据计费探测得 {detail} ⇒ **凭据有效但账户欠费**"
                "（上游 402 / code=30001，环境问题，充值后复跑）；"
                "**非产品缺陷** —— 产品按设计把 LLM 调用失败降级成 finding 并继续。")
    if verdict == VERDICT_OK:
        return (f"归因：同一凭据（含一次**计费**最小请求）探测得 {detail} ⇒ "
                "**凭据与额度均可用**，故此处失败是产品缺陷（优先查：凭据是否被"
                "发给了错误的提供方/端点 —— 本项目发生过完全相同的 401 事故）。")
    return (f"归因：凭据探测无法判定（{detail}）⇒ 按真实缺陷处理"
            "（fail-closed：判不了 ≠ 通过）。")


# ── OCR 就绪真值：以**应用自报**为准，不以"本轮是否注入 env"为准 ──────────
# 由来（2026-10-08 实测）：`tests/e2e_frozen.py` 的 APPDATA 是**固定复用**的
# （`%TEMP%/pbc-e2e-frozen`，跨会话保留）⇒ 上一轮注入过的 Paddle/MinerU 凭据会
# 留在 `PBC/config.json`。本轮**没有**提供任何 `PBC_E2E_*` OCR 凭据，流水线却
# **真的**跑完了 OCR（terminal=review、ocr_backend_used=paddle）。而就绪标志当时
# 只由 env 推导 ⇒ 覆盖清单把**实际跑过**的 OCR 记成 skipped（低报）；更危险的是
# 反向：`terminal == "error"` 且 env 无凭据时会走"环境未配 ⇒ 预期降级"分支
# **只打印、不记 FAIL** ⇒ 真实产品缺陷被吞成退出码 0（fail-open）。
# `classify_pipeline` 的文档契约本就写着 `ocr_ready` 指"凭据已提供且被应用
# **写入**" —— 应用才是"写没写进去"的权威。故就绪真值改从
# `GET /api/settings` 的 `ocr.{paddle,mineru}.configured` 取。
# 回归护栏：`tests/unit/test_e2e_proc_helper.py::TestOcrReadyFromSettings`。


def ocr_ready_from_settings(settings) -> bool:
    """从 `GET /api/settings` 的响应判定 OCR 是否**已就绪**（应用自报）。**纯函数**。

    只看 ``ocr.paddle.configured`` / ``ocr.mineru.configured`` 两个布尔 —— 它们是
    应用侧对"凭据是不是真值（非掩码占位）"的判定（``api/settings/read.py`` 的
    ``_is_real_api_key``），与本轮测试**有没有**注入 env 无关。

    fail-closed：响应不是 dict、缺 ``ocr`` 段、两个标志都不是 ``True`` ⇒ 返回
    ``False``（"判不了"按未就绪处理，与 :func:`classify_pipeline` 的 fail-closed 同源）。
    """
    if not isinstance(settings, dict):
        return False
    ocr = settings.get("ocr")
    if not isinstance(ocr, dict):
        return False
    for name in ("paddle", "mineru"):
        prov = ocr.get(name)
        if isinstance(prov, dict) and prov.get("configured") is True:
            return True
    return False


# ── 产物出处（PROVENANCE.txt 的 `git_head`）──────────────────────────────────
# 由来（2026-09-30 实测）：`build.ps1` 用 `try { & git rev-parse --short HEAD }`
# 取 HEAD，**异常被吞**。在一台 git 不在 PATH 的宿主上执行时，写出来的
# `PROVENANCE.txt` 是 `git_head: `（**空值**）—— 而全文没有任何地方校验它。
# 于是产物**声称**能自证出处，实际证不了；`release_gate.count_complete_artifacts`
# 只查文件**存在**，`e2e_unpacked` 的 D2 只比对 `version` ⇒ 空 git_head 一路绿灯。
# 判据：字段缺失（老产物）⇒ SKIP（不判定，非缺陷）；字段在但**为空** ⇒ FAIL。
PROV_OK = "ok"           # git_head 有值 ⇒ 产物能自证由哪次提交构建
PROV_MISSING = "missing"  # 整个字段都没有（早于本契约的产物）⇒ 不判定
PROV_EMPTY = "empty"     # 字段在但值为空 ⇒ 无法自证出处（build.ps1 静默失败）


def classify_provenance_git_head(text):
    """判定 `PROVENANCE.txt` 的 `git_head:` 是否**真的承载了出处**。**纯函数**。

    返回 ``(status, detail)``，``status`` 为 :data:`PROV_OK` /
    :data:`PROV_MISSING` / :data:`PROV_EMPTY` 之一；``detail`` 为取值或说明。

    ⚠️ 这里**不**校验"git_head == 当前 HEAD"：`PROVENANCE.txt` 是**构建期**写的，
    而构建通常发生在"提交之前"（本次拆分就是：先打包、后 commit）⇒ 两者**本来
    就会不同**。用相等做判据会制造假红。能自证"由某个真实提交构建"即可，
    所以只判"非空"。回归护栏：``tests/unit/test_provenance_git_head.py``。
    """
    value = None
    for ln in (text or "").splitlines():
        if ln.lower().startswith("git_head:"):
            value = ln.split(":", 1)[1].strip()
            break
    if value is None:
        return PROV_MISSING, "PROVENANCE.txt 无 git_head 字段（早于本契约的产物，不判定）"
    if not value:
        return PROV_EMPTY, (
            "git_head 字段存在但**为空** ⇒ 产物无法自证由哪次提交构建"
            "（`build.ps1` 的 `& git rev-parse` 被 try/catch 吞掉时会这样；"
            "确认构建宿主的 PATH 上有 git，然后重新打包）")
    return PROV_OK, value


def spawn_server(cmd, *, log_path, cwd=None, env=None, extra=None):
    """启动服务子进程，stdout/stderr 追加写入 ``log_path``。

    返回 ``(proc, logfile)``；调用方结束时应调用 :func:`stop_server`
    （终止进程并关闭日志句柄）。
    """
    lp = Path(log_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(lp, "ab")
    kwargs = dict(cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT)
    if extra:
        kwargs.update(extra)
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        logf.close()
        raise
    return proc, logf


def tail_log(log_path, max_chars: int = 2000) -> str:
    """读取日志尾部（失败诊断用，绝不抛异常）。"""
    try:
        data = Path(log_path).read_bytes()
    except OSError:
        return ""
    return data[-max_chars:].decode("utf-8", errors="replace")


def stop_server(proc, logf=None, timeout: float = 10) -> None:
    """终止服务进程并关闭日志句柄（幂等，绝不抛异常）。"""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if logf is not None:
        try:
            logf.close()
        except Exception:
            pass
