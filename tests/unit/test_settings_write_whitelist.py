"""`POST /api/settings` 字段可达性护栏（C4 / #169）。

**为什么需要**：`api/settings/write.py::_build_env_updates` 只认三类字段 ——
`_STATIC_FIELDS` 白名单、`<provider>_<field>` 动态字段、以及若干特例
（`*_clear_key` / legacy `deepseek_*`/`siliconflow_*`）。**其余一律落到
`prov_name is None` 分支。**

该分支在 C4 修复前是裸 `continue`（**静默丢弃**）—— 于是出现
`kb_prompt_inject` 这种"`config.py` 支持、注入代码也活着、但白名单漏了"的
**装饰开关**：用户点保存，界面回 "配置已保存并立即生效"，而字段根本没落盘，
read 接口与 UI 也没有控件。

本文件钉住两条不变式：

1. **可达性**：`SettingsUpdate` 里**显式声明**的每个字段，必须真的能进
   `env_updates` / `mem_updates`。这是**类级**护栏 —— 以后往 `SettingsUpdate`
   加字段却忘了同步白名单，会当场红，而不是等用户报"开关点了没用"。
2. **不静默**：白名单外的字段必须出现在 `dropped` 里（随响应回显），
   不得无声消失。

⚠️ **范围诚实**：本护栏**只覆盖显式声明的字段**。`SettingsUpdate.model_config`
是 `{"extra": "allow"}`，所以 `llm_json_mode` / `ocr_dual_compare` 仅靠 extra
兜着（无类型约束、无声明）—— 那是另一条已记录的技术债，不在本文件范围。
"""
from __future__ import annotations

from api.settings.write import (
    _PER_PROVIDER_FIELDS,
    _STATIC_FIELDS,
    SettingsUpdate,
    _build_env_updates,
)


def _declared_fields() -> set[str]:
    return set(SettingsUpdate.model_fields)


def _reachable(name: str) -> bool:
    """该字段是否真的会被 `_build_env_updates` 消费（而非丢弃）。"""
    if name in _STATIC_FIELDS:
        return True
    return any(name.endswith("_" + f) for f in _PER_PROVIDER_FIELDS)


class TestDeclaredFieldsAreReachable:
    def test_every_declared_field_reaches_the_writer(self):
        unreachable = sorted(n for n in _declared_fields() if not _reachable(n))
        assert not unreachable, (
            "以下字段在 SettingsUpdate 里声明了，但 _build_env_updates 不认 "
            f"（会被静默丢弃 / 现记为 dropped）：{unreachable}。"
            "修法：在 _STATIC_FIELDS 补映射，或删掉该声明。"
        )

    def test_guard_is_not_vacuous(self):
        """防空转：字段集被改名/清空时，上面那条会恒真。"""
        declared = _declared_fields()
        assert "kb_prompt_inject" in declared, "护栏前提失效：该字段被改名或删除"
        assert len(declared) >= 15, (
            f"声明字段只有 {len(declared)} 个 —— 护栏可能已空转"
        )
        assert _STATIC_FIELDS, "_STATIC_FIELDS 为空 ⇒ 可达性判定恒真"
        assert "kb_prompt_inject" in _STATIC_FIELDS


class TestKbPromptInjectSwitch:
    """C4/#169：开关必须真的能落盘，而不是"装饰"。"""

    def test_false_maps_to_env_and_mem(self):
        env, mem, errors, skipped, dropped = _build_env_updates(
            SettingsUpdate(kb_prompt_inject=False)
        )
        assert errors == []
        assert env == {"KB_PROMPT_INJECT": "false"}
        assert mem == {"kb_prompt_inject": False}
        assert dropped == []

    def test_true_maps_to_env_and_mem(self):
        env, mem, errors, skipped, dropped = _build_env_updates(
            SettingsUpdate(kb_prompt_inject=True)
        )
        assert errors == []
        assert env == {"KB_PROMPT_INJECT": "true"}
        assert mem == {"kb_prompt_inject": True}

    def test_config_update_config_consumes_the_key(self):
        """白名单接通了、`update_config` 不认 ⇒ 内存不生效（仍等于装饰）。

        `config.py:981` 早已支持该键，但"支持"必须被机检钉住，不能靠人记得。
        """
        from config import config, update_config

        app = config["app"]
        saved = app.kb_prompt_inject
        try:
            update_config({"kb_prompt_inject": False})
            assert app.kb_prompt_inject is False
            update_config({"kb_prompt_inject": "true"})
            assert app.kb_prompt_inject is True
        finally:
            update_config({"kb_prompt_inject": saved})


class TestUnknownFieldsAreReportedNotSilent:
    """C4 的"不静默"半边：白名单外字段必须回显，不能无声消失。"""

    def test_unknown_field_lands_in_dropped(self):
        env, mem, errors, skipped, dropped = _build_env_updates(
            SettingsUpdate(random_unknown_field="x", deepseek_model="m")
        )
        assert errors == []
        assert dropped == ["random_unknown_field"]
        assert "RANDOM_UNKNOWN_FIELD" not in env
        # 同批的合法字段照常生效（"一个未知字段不该拖垮整次保存"）
        assert env["DEEPSEEK_MODEL"] == "m"

    def test_known_fields_never_land_in_dropped(self):
        env, mem, errors, skipped, dropped = _build_env_updates(
            SettingsUpdate(
                llm_provider="deepseek",
                ocr_backend="paddle",
                mineru_enable_formula=True,
                kb_prompt_inject=True,
                deepseek_model="m",
                glm_api_key="sk-not-a-mask-1234567890",
            )
        )
        assert dropped == [], f"已知字段被误报为未知：{dropped}"
        assert errors == []
        assert env["KB_PROMPT_INJECT"] == "true"
        assert env["GLM_API_KEY"] == "sk-not-a-mask-1234567890"
