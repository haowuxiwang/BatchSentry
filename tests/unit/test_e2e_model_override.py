"""B5-4：e2e driver 的「被测模型可覆盖」与「读回断言」护栏。

背景：driver 曾把 `siliconflow_model` **硬编码**为 `Qwen/Qwen2.5-72B-Instruct`，
而本机生效的生产配置是 `deepseek-ai/DeepSeek-V3.2`。两者都走 OpenAI adapter、
协议一致，故"链路可用"结论有效 —— 但**被测模型 ≠ 生产模型**，
模型特有的输出形态差异（字段命名/数值格式化/日期书写/对 response_format 的
遵守度）在 Qwen 上通过、在 DeepSeek 上未必。

本文件锁两件事：
  ① 模型**可**经 `--model` / `PBC_E2E_MODEL` 覆盖，且优先级明确；
  ② 配置下发后**必须读回服务端生效值**再断言 —— 只断言"我 POST 了什么"没有判别力。
"""
from __future__ import annotations

import ast
from pathlib import Path

import e2e_run

_SRC_PATH = Path(__file__).resolve().parents[2] / "e2e_run.py"
_SRC = _SRC_PATH.read_text(encoding="utf-8")


# ── ① 解析点：优先级显式传参 > 环境变量 > 默认基线 ────────────────────────────


class TestModelResolution:
    def test_default_when_nothing_given(self, monkeypatch):
        """不传参且无环境变量 ⇒ 默认值，且默认值**就是**历史基线（改基线会改可比性）。"""
        monkeypatch.delenv("PBC_E2E_MODEL", raising=False)
        assert e2e_run.resolve_llm_model(None) == e2e_run.DEFAULT_SILICONFLOW_MODEL
        assert e2e_run.DEFAULT_SILICONFLOW_MODEL == "Qwen/Qwen2.5-72B-Instruct"

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("PBC_E2E_MODEL", "deepseek-ai/DeepSeek-V3.2")
        assert e2e_run.resolve_llm_model(None) == "deepseek-ai/DeepSeek-V3.2"

    def test_explicit_beats_env(self, monkeypatch):
        """显式传参优先于环境变量（否则"命令行指定"会被环境静默推翻）。"""
        monkeypatch.setenv("PBC_E2E_MODEL", "env-model")
        assert e2e_run.resolve_llm_model("cli-model") == "cli-model"

    def test_blank_explicit_falls_through(self, monkeypatch):
        """空串算"没传" —— argparse 的 default 表达不了这一点，故收敛到函数里。"""
        monkeypatch.setenv("PBC_E2E_MODEL", "env-model")
        assert e2e_run.resolve_llm_model("") == "env-model"


# ── ② 读回断言：比对的是「生效值」，不是「POST 的值」 ──────────────────────────


class TestModelMismatch:
    def test_equal_means_no_mismatch(self):
        assert e2e_run.model_mismatch("m", "m") is None

    def test_different_means_mismatch_with_both_values(self):
        why = e2e_run.model_mismatch("requested-m", "effective-m")
        assert why is not None
        # 两个值都要在说明里 —— 否则排查时不知道差在哪
        assert "requested-m" in why and "effective-m" in why

    def test_none_effective_is_mismatch(self):
        """拿不到生效值 ⇒ **算不一致**（fail-closed）。

        反例形态：GET 返回缺字段 ⇒ ``None``。若把 None 当"一致"，
        整个读回断言就退化成恒真 —— 那正是"判不了当成判据确凿"（§二十三）。
        """
        assert e2e_run.model_mismatch("m", None) is not None

    def test_empty_effective_is_mismatch(self):
        assert e2e_run.model_mismatch("m", "") is not None


# ── ③ 消费点守卫（§二十八）：查**调用表达式**，不查裸 token ────────────────────


def _configure_form_dict(tree: ast.Module) -> dict:
    """取出 configure 表单那个 dict 字面量的键值（以 `siliconflow_model` 为锚）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "siliconflow_model" in keys and "llm_provider" in keys:
                return node
    raise AssertionError("未找到 configure 表单 dict（含 siliconflow_model）—— 提取器失效")


class TestDriverWiresModelThrough:
    def test_form_model_comes_from_a_variable_not_a_literal(self):
        """表单里的 `siliconflow_model` **必须**来自变量（`args.model`）而非字面量。

        ⚠️ 判据落在**消费点**（这个 dict 条目的 value）上：只 grep "PBC_E2E_MODEL"
        或只 grep "args.model" 都会被**别处**的出现满足（§二十八 盲区一）。
        """
        form = _configure_form_dict(ast.parse(_SRC))
        for k, v in zip(form.keys, form.values):
            if isinstance(k, ast.Constant) and k.value == "siliconflow_model":
                assert isinstance(v, ast.Attribute) and v.attr == "model", (
                    "siliconflow_model 的值不是 args.model —— 硬编码回归了"
                )
                return
        raise AssertionError("表单缺少 siliconflow_model")  # pragma: no cover

    def test_extractor_actually_finds_the_form(self):
        """**正向对照**：提取器在真实源码上必须命中，否则上面那条是空断言。"""
        assert _configure_form_dict(ast.parse(_SRC)) is not None

    def test_readback_comparison_is_wired(self):
        """必须存在 `model_mismatch(args.model, eff_model)` 这样的**读回比对**调用。

        断言的是调用表达式（函数名 + 两个实参），不是"文件里出现过 model_mismatch"
        —— 后者只要有一行无关引用就满足。
        """
        calls = [
            n for n in ast.walk(ast.parse(_SRC))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "model_mismatch"
        ]
        assert len(calls) == 1, f"model_mismatch 调用点应恰为 1 处，实际 {len(calls)}"
        args = calls[0].args
        assert len(args) == 2
        assert isinstance(args[0], ast.Attribute) and args[0].attr == "model"
        assert isinstance(args[1], ast.Name), "第二个实参应是读回的生效值变量"

    def test_effective_model_is_read_back_from_the_api(self):
        """生效值必须来自 `GET /api/settings` —— 不能拿 `form` 里的值自证。"""
        calls = [
            n for n in ast.walk(ast.parse(_SRC))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
        ]
        urls = [
            ast.unparse(c.args[0]) for c in calls if c.args
        ]
        assert any("api/settings" in u for u in urls), (
            f"未见 GET /api/settings 的读回调用，实际 GET 目标：{urls[:8]}"
        )
