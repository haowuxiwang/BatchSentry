"""冷启动导入预算 —— `import main` 不得拉起 openai SDK（R95 启动性能）。

## 为什么要有这个文件

用户实测（`%APPDATA%/PBC/logs/backend-boot.log`，2026-10-22）：

    00:07:21.013 [spawn] pbc-server.exe
    00:07:35.139 [boot] server.py entering          ← 14.1s：进程创建 + 加载器 + 杀软
    00:07:37.050 [PBC] Starting server on           ←  1.9s：uvicorn
    00:07:56.437 INFO: Started server process       ← 19.4s：`import main`
    00:07:56.935 [ready] after 70 failed checks     ← 合计 35.9s

同一份产物**缓存热**时全程 **2.7s**（`devlogs/_verify/r95_startup_probe.py`）⇒
35.9s 不是逻辑缺陷，是**冷缓存下按文件计费**：每个模块文件都要读盘 + 被杀软
实时扫描。所以"启动快"= **启动期碰的模块数少**。

实测 `import openai` 独占 **1007 个模块**，而 `import main` 全链 1340 个 ——
一个 SDK 就占 75%，可全树只用到 `AsyncOpenAI` 一个符号
（`llm/adapters/openai_adapter.py`，唯一导入点）。

延迟导入后：**1340 → 665** 个导入事件（‑50%），累计 **2.91s → 1.13s**（‑61%）。

## 三条判据

1. **行为**（最硬）：子进程里 `import main` 后 `'openai' not in sys.modules`。
   必须在**子进程**里测 —— 当前进程早被别的用例/插件拉过 openai，测了也是绿的。
2. **预算**：模块总数 ≤ `_MODULE_BUDGET`。上界必须**严格小于**改前的 1340
   （否则对本次修改零判别力，见 `verification-integrity` Trap 69），也要高于
   现值 733 留出依赖增长的余量。
3. **正对照**：适配器**该**在真正建客户端时才导入 SDK，且建出来的客户端可用 ——
   否则"延迟导入"退化成"根本不导入"，第 1 条就变成空断言。
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ADAPTER = REPO / "llm" / "adapters" / "openai_adapter.py"

#: 改前 1340 / 改后 733。上界取 900：严格小于旧值（有判别力），
#: 又给新依赖留 ~167 个模块的余量（一次误加一个 SDK 就会红）。
_MODULE_BUDGET = 900


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )


class TestStartupDoesNotImportTheSdk:
    def test_import_main_leaves_openai_out_of_sys_modules(self):
        """子进程实测：启动链不得把 openai SDK 拉进来。"""
        r = _run(
            "import sys, main; "
            "print('openai' in sys.modules); "
            "print(sorted(m for m in sys.modules if m == 'openai' or m.startswith('openai.'))[:5])"
        )
        assert r.returncode == 0, f"import main 失败：{r.stderr[-2000:]}"
        first = r.stdout.splitlines()[0].strip()
        assert first == "False", (
            "`import main` 拉起了 openai SDK —— 冷启动会为此多读 1007 个模块文件"
            f"（见模块 docstring 的 35.9s / 2.7s 实测）。命中模块：{r.stdout.splitlines()[-1]}"
        )

    def test_module_budget(self):
        """模块总数预算（判别力：上界 900 < 改前 1340）。"""
        r = _run("import sys, main; print(len(sys.modules))")
        assert r.returncode == 0, f"import main 失败：{r.stderr[-2000:]}"
        n = int(r.stdout.strip())
        assert n <= _MODULE_BUDGET, (
            f"启动期导入模块 {n} 个 > 预算 {_MODULE_BUDGET}"
            f"（改前 1340 / 延迟 openai 后 733）—— 冷启动会按文件数线性变慢"
        )

    def test_budget_is_not_vacuous(self):
        """预算必须真的比"改前"小，否则对本次修改零判别力。"""
        assert 0 < _MODULE_BUDGET < 1340


class TestAdapterImportsSdkLazily:
    def test_no_module_level_openai_import_in_adapter(self):
        """AST：适配器模块级不得出现 openai 导入（函数内才是延迟）。"""
        tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:  # 只看模块级 body，不看函数体
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.split(".")[0] == "openai"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "openai":
                    offenders.append(node.module or "")
        assert offenders == [], (
            f"openai 又回到模块顶部了（{offenders}）—— 冷启动会退回 35.9s 量级"
        )

    def test_adapter_still_builds_a_working_client(self):
        """正对照：延迟导入必须**真的能导入**，客户端类型正确。"""
        from config import ProviderConfig
        from llm.adapters.openai_adapter import OpenAIAdapter

        cfg = ProviderConfig(
            name="siliconflow",
            api_key="sk-test-not-real",
            base_url="https://api.siliconflow.cn/v1",
        )
        adapter = OpenAIAdapter(cfg)
        import openai

        assert isinstance(adapter.client, openai.AsyncOpenAI)
        assert "api.siliconflow.cn" in str(adapter.client.base_url)

    def test_adapter_construction_imports_sdk_on_demand(self):
        """建了客户端之后 SDK 才进 sys.modules（证明是"延迟"而非"没有"）。"""
        r = _run(
            "import sys; "
            "from config import ProviderConfig; "
            "from llm.adapters.openai_adapter import OpenAIAdapter; "
            "print('before', 'openai' in sys.modules); "
            "OpenAIAdapter(ProviderConfig(name='x', api_key='k', base_url='')); "
            "print('after', 'openai' in sys.modules)"
        )
        assert r.returncode == 0, r.stderr[-2000:]
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        assert lines[:2] == ["before False", "after True"], lines[:2]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
