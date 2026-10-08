# -*- coding: utf-8 -*-
"""LLM 归因结论的**单一真值**：被推翻的旧断言不得在活文档/活代码里残留。

由来（Round 71 第十五批，2026-10-08，实测）：
Round 70 第十四批把 `tests/e2e_proc.llm_failure_attribution` 的 ``ok`` 分支从
「凭据与额度均可用 ⇒ 故此处失败是**产品缺陷**」改成**有界**表述（只陈述探测证到了
什么 ＋ 点名盲区 ＋ 给下一步判据）。**但同一结论写在 4 个地方，只改了 1 处**：

| 位置 | 改前措辞 | 第十四批后 |
|---|---|---|
| `tests/e2e_proc.py` 的 `llm_failure_attribution` 返回文案 | 故此处失败是**产品缺陷** | 已改 |
| `tests/e2e_proc.py` 的 `VERDICT_OK` 注释 / 行为表 / 该函数 docstring | 才真是**产品缺陷** ／ **才是**产品缺陷 ／ 「ok 分支写"产品缺陷"」 | **残留** |
| `tests/e2e_frozen.py::probe_llm_credential` docstring | **就是**产品缺陷 | **残留** |
| `docs/PROJECT_PITFALLS.md` §D「修法」 | **就是**产品缺陷 | **残留** |

残留者与实现**相反**，而 `docs/PROJECT_PITFALLS.md` 是**纪律文档** —— 读它会把
纪律读回已被推翻的那一版（这正是本项目「同一语义写两处必然漂移」那条纪律的实例）。

⇒ 本护栏遍历**全部被跟踪的文本文件**，禁止这些措辞出现。两条例外：

  ① **整文件豁免**（`ALLOWED`，必须写明理由）—— 用于 append-only 的历史记录；
  ② **同行标记豁免** —— 该行含 `attr-history` 时放行，用于「反面样例常量」这类
     必须**原样引用**被禁措辞的场合（Markdown 里写 `<!-- attr-history -->`）。

**引述 vs 断言**（本护栏实测逼出来的区分）：
  * **断言**（"所以这是产品缺陷"）—— 一律删掉，改成「必要条件 / 不足以断定」；
  * **引述**（描述"当时错成什么样"）—— 保留，但**插入 `**` 断开连续字面量**
    （`CHANGELOG.md` 早已这么写），让它在文本上就显形为被强调的引文；
  * **数据**（`FORBIDDEN_CLAIMS` / `_PRODUCT_DEFECT_CLAIM` 这类常量）—— 保留原文，
    在**同一行**加 `attr-history` 标记。

⚠️ 首版护栏第一次运行就抓出 2 处人工清单漏掉的引述行（`docs/TODO.md` 的 0-13 行、
`tests/unit/test_llm_failure_attribution.py` 的 docstring）—— 这是它**非空转**的现场证据。

另有一条**反腐**检查：整文件豁免若已无必要（文件里再也找不到被禁措辞），
就必须删掉 —— 豁免表只增不减，等于护栏在放水。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_RG_PATH = REPO / "scripts" / "release_gate.py"


def _load_release_gate():
    """从文件路径加载 `scripts/release_gate.py`。

    模块名与 `test_repo_hygiene.py` 的 `release_gate_hygiene` 刻意区分：
    两个测试模块若用同一个模块名注册 `sys.modules` 会互相覆盖。
    """
    spec = importlib.util.spec_from_file_location("release_gate_attr", _RG_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["release_gate_attr"] = mod
    spec.loader.exec_module(mod)
    return mod


rg = _load_release_gate()


# ── 被禁措辞：**越过证据**的无条件断言（原缺陷的措辞）────────────────────
FORBIDDEN_CLAIMS = (
    "就是产品缺陷",          # attr-history: 被禁断言（无条件断言产品缺陷）
    "才真是产品缺陷",        # attr-history: 被禁断言（必要/充分含糊）
    "故此处失败是产品缺陷",  # attr-history: 被禁断言（原缺陷的确切措辞）
)

# ── 整文件豁免登记表：每项都必须写明理由 ───────────────────────────────
ALLOWED: dict[str, str] = {
    "CHANGELOG.md": (
        "append-only 历史记录：追改会让「当时究竟发生了什么」失真。"
        "顶部 Round 70 条目已取代其结论（Round 71 又补了残留机检）。"
    ),
}

#: 同行豁免标记：该行含它即放行（用于必须原样引用被禁措辞的场合）。
MARKER = "attr-history"

_TEXT_SUFFIXES = frozenset({
    ".py", ".md", ".js", ".html", ".htm", ".css", ".json", ".txt",
    ".ps1", ".bat", ".sh", ".spec", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".sql", ".csv",
})


def tracked_all() -> list[str]:
    """`git ls-files` 的全部已跟踪路径（仓库相对，POSIX 分隔）。"""
    return [p.replace("\\", "/") for p in rg._tracked_paths()]


def scanned_files() -> list[str]:
    """被跟踪文件里**扩展名像文本**的那些（已排序）。

    只扫被跟踪文件：`devlogs/` 下有变异快照与历史探针，**故意**保留旧措辞，
    且它们本就未入库（`.gitignore`），不属于"活文档"。
    """
    return sorted(rel for rel in tracked_all()
                  if Path(rel).suffix.lower() in _TEXT_SUFFIXES)


def read_text(rel: str) -> str:
    return (REPO / rel).read_bytes().decode("utf-8", errors="replace")


def find_forbidden(text: str) -> list[str]:
    """纯函数：一段文本里命中了哪些被禁措辞。"""
    return [c for c in FORBIDDEN_CLAIMS if c in text]


def unmarked_hits(text: str) -> list[tuple[int, str]]:
    """纯函数：**未带豁免标记**的行上命中了几处（行号从 1 起）。"""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if MARKER in line:
            continue
        for claim in FORBIDDEN_CLAIMS:
            if claim in line:
                out.append((i, claim))
    return out


class TestNoStaleClaim:
    def test_no_unconditional_claim_in_live_files(self):
        if not tracked_all():
            pytest.skip("git 不可用（git ls-files 无输出）")
        files = scanned_files()
        assert files, (
            "被跟踪文件里**一个文本文件都没匹配到** —— 这不是 git 不可用，"
            "而是 `_TEXT_SUFFIXES` 塌缩（护栏会因此静默放行全部残留）"
        )

        bad: dict[str, list[tuple[int, str]]] = {}
        for rel in files:
            if rel in ALLOWED:
                continue
            hits = unmarked_hits(read_text(rel))
            if hits:
                bad[rel] = hits

        assert not bad, (
            f"活文档/活代码里仍有「越过证据」的旧归因断言：{bad}。\n"
            f"Round 70 第十四批已把 `ok` 分支改成有界表述（必要条件，非充分条件）——"
            f"这些残留与实现**相反**，且 `docs/PROJECT_PITFALLS.md` 是纪律文档，"
            f"读它会把纪律读回已被推翻的那一版。\n"
            f"改法：① 改成「必要条件 / 不足以断定」的说法；"
            f"② 若确属历史引用，登记进 `ALLOWED`（整文件）或在该行加 `{MARKER}` 标记。"
        )

    def test_scan_covers_the_attribution_modules(self):
        if not tracked_all():
            pytest.skip("git 不可用")
        files = set(scanned_files())
        for rel in ("tests/e2e_proc.py", "tests/e2e_frozen.py",
                    "docs/PROJECT_PITFALLS.md"):
            assert rel in files, (
                f"{rel} 未被扫到 —— 本护栏对归因实现**失去覆盖**（扫描范围塌缩）"
            )
        assert len(files) >= 100, (
            f"只扫到 {len(files)} 个被跟踪文本文件，扫描范围疑似塌缩"
        )

    def test_detector_is_not_vacuous(self):
        """阳性对照 + 阴性对照：判据本身必须**有判别力**。"""
        for claim in FORBIDDEN_CLAIMS:
            assert find_forbidden(f"前文 {claim} 后文") == [claim], (
                f"{claim!r} 在合成文本里未被命中 —— 匹配逻辑失效（恒真）"
            )
            assert unmarked_hits(f"前文 {claim} 后文") == [(1, claim)]
            assert unmarked_hits(f"{claim}  # {MARKER}") == [], (
                f"{MARKER} 同行标记豁免未生效"
            )
        clean = "凭据有效、额度可用；这只是**必要条件**，不足以断定产品缺陷。"
        assert find_forbidden(clean) == [], "干净文本被误判为命中"
        assert unmarked_hits(clean) == [], "干净文本被误判为命中"

    def test_allowlist_entries_are_still_needed(self):
        """豁免不能是死的：文件里若已找不到被禁措辞，豁免就该删（防豁免表变宽）。"""
        if not tracked_all():
            pytest.skip("git 不可用")
        tracked = set(tracked_all())
        dead: list[str] = []
        for rel, reason in ALLOWED.items():
            path = REPO / rel
            assert path.exists(), f"豁免登记了不存在的文件：{rel}（{reason}）"
            assert rel in tracked, f"豁免的文件未被跟踪：{rel}"
            if not find_forbidden(read_text(rel)):
                dead.append(rel)
        assert not dead, (
            f"这些整文件豁免已无必要（文件里再也找不到被禁措辞）：{dead}。"
            f"请从 `ALLOWED` 删掉 —— 豁免表只增不减，就等于护栏在放水。"
        )


class TestLiveAttributionTextIsBounded:
    """`ok` 文案本身必须是**有界**的（第十四批的判据，此处再钉一次）。"""

    def test_ok_branch_states_a_boundary_not_a_verdict(self):
        from tests.e2e_proc import VERDICT_OK, llm_failure_attribution

        t = llm_failure_attribution(
            VERDICT_OK, "免费端点 HTTP 200 + 计费端点 HTTP 200")
        assert "凭据有效" in t and "额度可用" in t, "未说清探测证到了什么"
        assert "极小请求" in t and "不足以" in t, "未点名探测盲区"
        assert "504" in t and "llm_call_audit.error" in t, "未给下一步判据"
        assert "才是产品缺陷" in t, "条件式归因被删掉了"
        assert find_forbidden(t) == [], f"ok 文案又出现无条件断言：{find_forbidden(t)}"

    def test_ok_comment_and_behaviour_table_say_necessary_condition(self):
        """`VERDICT_OK` 注释与行为表是**人读的第一入口**，不得写无条件断言。"""
        src = read_text("tests/e2e_proc.py")
        assert find_forbidden(src) == [], (
            f"tests/e2e_proc.py 里仍有被禁断言：{find_forbidden(src)}"
        )
        assert "必要条件" in src, (
            "未在 `VERDICT_OK` / 行为表附近点明「只是必要条件」——"
            "这正是第十四批残留的那个含糊点"
        )
