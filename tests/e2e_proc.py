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
