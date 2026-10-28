"""e2e driver 的"实际用了哪个 OCR 后端"断言护栏。

背景（2026-09-15 实测）：Paddle 上游返回 ``10010 任务提交队列已满`` 时，
``_get_ocr_chain`` 会**自动 failover** 到 MinerU。此时终态仍是 ``review``、
findings 照常、SSE 照常 —— 一轮"用 Paddle 跑"的结论会在实际跑 MinerU 的
情况下被记成通过。唯一能揭穿它的是 ``jobs.ocr_backend_used``。

本文件锁住三件事：
1. :func:`e2e_run.backend_mismatch` 的判定语义；
2. ``run_upload`` 确实接入了该判定并把 ``ocr_backend_used`` 写进结果；
3. **AST 级联检查**：所有 ``run_upload(...)`` 调用都必须显式传
   ``expect_backend`` —— 否则以后新增轮次会悄悄退回"不校验"。
"""
import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from e2e_run import backend_mismatch  # noqa: E402

_DRIVER = _ROOT / "e2e_run.py"


# ── 判定语义 ────────────────────────────────────────────────────────


def test_no_expectation_never_flags():
    """未指定期望后端时不判错（`pdf`/`img` 等不关心后端的轮次）。"""
    assert backend_mismatch(None, "mineru") is None
    assert backend_mismatch("", "mineru") is None


def test_matching_backend_passes():
    assert backend_mismatch("paddle", "paddle") is None
    assert backend_mismatch("mineru", "mineru") is None


def test_failover_is_flagged():
    """期望 paddle 实得 mineru = failover，必须报错且说明两者。"""
    msg = backend_mismatch("paddle", "mineru")
    assert msg is not None
    assert "paddle" in msg and "mineru" in msg


def test_missing_backend_is_flagged():
    """ocr_backend_used 为空（老库/未落库）也算不一致，不得放行。"""
    assert backend_mismatch("paddle", None) is not None


# ── 接入（源码级） ───────────────────────────────────────────────────


def test_run_upload_wires_the_verdict():
    src = _DRIVER.read_text(encoding="utf-8")
    assert "def run_upload(" in src and "expect_backend" in src, (
        "run_upload 必须接受 expect_backend —— 否则无法断言实际后端"
    )
    assert re.search(r"backend_err\s*=\s*backend_mismatch\(", src), (
        "run_upload 必须调用 backend_mismatch 并把结果用于 ok 判定"
    )
    assert '"ocr_backend_used": used_backend' in src, (
        "结果字典必须带上 ocr_backend_used（报告里要能一眼看到实际后端）"
    )


def test_all_run_upload_calls_declare_expect_backend():
    """AST 联检查：每个 run_upload(...) 调用都要写死期望后端。

    只查"有没有传"而不查值，值由各轮次的语义决定（real→paddle、
    mineru→mineru）；这里防的是**新轮次忘记传**。
    """
    tree = ast.parse(_DRIVER.read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "run_upload"):
            continue
        kw = {k.arg for k in node.keywords}
        if "expect_backend" not in kw:
            missing.append(node.lineno)
    assert not missing, (
        f"e2e_run.py 以下行号的 run_upload(...) 未声明 expect_backend：{missing}\n"
        "每个轮次都必须声明它期望的 OCR 后端，否则 failover 会静默混入结论。"
    )


# ── 密钥必须能走环境变量（不得只靠命令行） ──────────────────────────


@pytest.mark.parametrize("env_name", [
    "PBC_E2E_SILICONFLOW_KEY", "PBC_E2E_PADDLE_TOKEN", "PBC_E2E_MINERU_TOKEN",
])
def test_cli_secrets_have_env_fallback(env_name):
    """密钥参数必须有环境变量回落。

    命令行参数会进 shell history、进程表（tasklist / /proc/<pid>/cmdline）
    与 CI 日志 —— 等同于泄漏。仓库已经吃过一次硬编码密钥的亏。
    """
    src = _DRIVER.read_text(encoding="utf-8")
    assert env_name in src, f"e2e_run.py 缺少 {env_name} 环境变量回落"
    # 对应参数不得再是 required=True（否则回落形同虚设）
    for flag in ("--sf-key", "--paddle-token", "--mineru-token"):
        m = re.search(rf'add_argument\(\s*"{re.escape(flag)}"\s*,([^)]*)\)', src)
        assert m, f"未找到 {flag} 的 add_argument"
        assert "required=True" not in m.group(1), (
            f"{flag} 仍是 required=True，环境变量回落不会生效"
        )


# ── LLM 密钥必须配给"它自己所属"的提供方 ─────────────────────────────
#
# 实测（2026-09-17，真实冻结产物）：`e2e_frozen.py` 曾把配置写死成
# ``{"llm_provider": "deepseek", "deepseek_api_key": key}``，而实际注入的是
# **硅基流动**的 key ⇒ 请求打到 api.deepseek.com 得
# `401 Authentication Fails, Your api key: **** is invalid`。
# 该错配长期"绿"着：样例 PDF 曾是**空白页** ⇒ Stage 2 无内容可分析 ⇒
# 从不调用 LLM ⇒ 401 从未发生（假绿）。夹具改为含真实文字后当场暴露。


def test_proc_exposes_provider_selector():
    """提供方名有环境变量回落（与密钥同等地位），默认 siliconflow。"""
    from tests.e2e_proc import (DEFAULT_LLM_PROVIDER, LLM_PROVIDER_ENV,
                                llm_provider)
    assert LLM_PROVIDER_ENV == "PBC_E2E_LLM_PROVIDER"
    assert DEFAULT_LLM_PROVIDER == "siliconflow"
    assert callable(llm_provider)


def test_smoke_derives_key_field_from_provider():
    """密钥字段名必须由提供方名派生 —— 不得写死任何一家。

    ⚠️ 只看**真正生效的结构**（AST 里的字典字面量键），不看源码文本：
    按文本搜索会命中**注释里引用同一段代码的说明文字**（本项目已因此误报过
    两次 —— Round 25 与 Round 29）。本段注释里就写着反例
    ``{"llm_provider": "deepseek", "deepseek_api_key": key}``，
    用文本匹配会当场假红。
    """
    src = (_ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")
    assert "llm_provider" in src, "冒烟必须显式设置 llm_provider"

    literal_keys, derived_keys = [], []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                    and k.value.endswith("_api_key"):
                literal_keys.append(k.value)
            elif isinstance(k, ast.JoinedStr):  # f"{...}_api_key"
                parts = "".join(
                    str(p.value) if isinstance(p, ast.Constant) else "<expr>"
                    for p in k.values
                )
                if parts.endswith("_api_key"):
                    derived_keys.append(parts)

    assert derived_keys, (
        "密钥字段必须写成 f'{provider}_api_key' 形式（由提供方派生）；"
        "写死字段名会让'A 家 key 发给 B 家端点'的错配静默成立"
    )
    assert not literal_keys, (
        f"出现写死的提供方密钥字段 {literal_keys} —— 必须改为 f'{{provider}}_api_key'"
    )
