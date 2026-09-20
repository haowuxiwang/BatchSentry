"""B4-4 —— provider 自动改写的**单一实现点**与**界面可见性**机检。

要防的失效模式（B4-4 实证）：
  1. **两份实现**：后端 `config.load_config()` 在 import 期就切并落盘，
     前端 `settings.js` 又自己切一次（用"占位判定"结果当触发条件）。
     后端先切完 ⇒ 前端条件恒不成立 ⇒ **那条唯一有提示的路径被绕过**
     ⇒ 用户看到的是"provider 被静默换掉"；
  2. **决策依据是启发式**（`_is_real_key` 子串匹配）⇒ 真实 key 含
     `placeholder` 子串时在**生产环境换模型**（GMP：模型不可追溯）。

⇒ 契约：**决策只在后端一处**，前端只**呈现**后端回传的事实。

护栏写法说明（防"空断言"）：
- 反向断言（"前端不得再有本地切换"）**必须**配**正向对照** ——
  断言 `setActiveProvider`（用户显式切换）**仍然存在**。
  否则把整个文件清空也能让反向断言通过，护栏就没有判别力。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SETTINGS_JS = REPO / "static" / "settings.js"
READ_PY = REPO / "api" / "settings" / "read.py"
CONFIG_PY = REPO / "config.py"

#: 后端"决策事实"的唯一载体（config 模块级全局）。
_NOTICE_SYMBOL = "AUTO_ACTIVATE_NOTICE"
#: 判定函数（唯一实现点）。
_JUDGE_SYMBOL = "_resolve_active_provider"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _func_source(name: str, path: Path = CONFIG_PY) -> str:
    """取某个顶层函数的源码片段（含其 docstring）。"""
    src = _read(path)
    start = src.index(f"def {name}(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def _called_names(src: str, func_name: str) -> set[str]:
    """AST 提取某函数体里**被调用**的名字（不查字面量 —— 注释/docstring 不算）。"""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {
                n.func.id if isinstance(n.func, ast.Name) else
                (n.func.attr if isinstance(n.func, ast.Attribute) else "")
                for n in ast.walk(node) if isinstance(n, ast.Call)
            }
    raise AssertionError(f"{func_name} 未找到（护栏前提不成立）")


def test_backend_owns_the_decision():
    """决策 + 判定函数必须在 `config.py` 里，且 `read.py` 只**读**不判。"""
    cfg, read = _read(CONFIG_PY), _read(READ_PY)
    assert f"def {_JUDGE_SYMBOL}(" in cfg, "判定函数的唯一实现点必须在 config.py"
    assert _NOTICE_SYMBOL in cfg
    assert _NOTICE_SYMBOL in read, "settings API 必须把决策事实回传给前端"
    # read.py 不得自己判定"该不该切"——它只呈现
    assert f"def {_JUDGE_SYMBOL}(" not in read


def test_switch_does_not_use_placeholder_heuristic():
    """🔴 **核心判据**：切换判定里**不得调用** `_is_real_key`。

    `_is_real_key` 是启发式（占位词），把它当切换依据 = 真实 key 被误判时
    静默换模型。切换只允许依据"Key 字面为空/非空"。

    判据走 **AST 的"被调用名字"**，而不是文本搜索 —— 否则函数 docstring 里
    为了解释"为什么不用它"而**提到**这个标识符就会假红（首版实测踩到）。
    """
    calls = _called_names(_func_source(_JUDGE_SYMBOL), _JUDGE_SYMBOL)
    assert "_is_real_key" not in calls, (
        f"provider 自动改写不得依赖占位启发式（B4-4 ①）；实际调用={sorted(calls)}"
    )
    # 前提校验：AST 确实解析出了东西（防"函数改名 ⇒ 集合恒空 ⇒ 空断言"）
    assert calls, "AST 未解析到任何调用 ⇒ 判据可能已失效"


def test_requires_no_explicit_choice():
    """必须能区分"用户显式选过" —— 否则会覆盖用户的显式选择（B4-4 验收 ①）。"""
    body = _func_source(_JUDGE_SYMBOL)
    assert "explicit" in body
    assert 'os.getenv("LLM_PROVIDER")' in body


def test_frontend_consumes_notice_and_has_no_second_switch():
    js = _read(SETTINGS_JS)

    # ── 正向对照：用户显式切换必须**仍然存在** ────────────────────────
    assert "setActiveProvider" in js, (
        "正向对照失败：用户显式切换功能不应被本改动移除"
    )
    # ── 正向：前端确实消费了后端字段 ─────────────────────────────────
    assert "auto_activated" in js, "前端必须消费后端回传的 auto_activated"
    assert "showAutoActivateNotice" in js, "必须存在渲染该事实的函数"
    # ⚠️ 只断言"函数存在"是**空断言**（定义了不调用照样过）⇒ 必须钉**调用点**。
    assert "showAutoActivateNotice(current.llm.auto_activated)" in js, (
        "必须在 load() 里实际调用渲染（定义了不调用 = 提示永远不显示）"
    )
    # ── 反向：不得再有第二份本地自动切换实现 ─────────────────────────
    assert "firstConfigured" not in js, (
        "前端不得再自己找'第一个可用 provider'并切换（第二实现点）"
    )
    # 只查**用法**（属性读取 / 对象字面量键），不查注释里提到该名字
    assert "opts.autoReason" not in js, "不得再消费 autoReason 选项"
    assert "autoReason:" not in js, "不得再传入 autoReason 选项"


def test_frontend_notice_is_display_only():
    """渲染函数不得再发起切换请求（只显示，不决策）。"""
    js = _read(SETTINGS_JS)
    start = js.index("function showAutoActivateNotice(")
    end = js.index("\n  }", start)
    body = js[start:end]
    assert "setActiveProvider" not in body, "提示函数不得触发切换"
    assert "fetch(" not in body, "提示函数不得发起网络请求"


def _js_func_body(js: str, signature: str) -> str:
    """取 JS 函数体：从签名到第一个 2 空格缩进的收尾花括号。

    （函数体内嵌套块的收尾必然是 4+ 空格缩进，不会先命中；
      若该前提被打破，下面的"前提校验"会显式报错而不是静默返回空串。）
    """
    start = js.index(signature)
    end = js.index("\n  }", start)
    body = js[start:end]
    assert body, f"{signature} 取到空函数体 ⇒ 本护栏前提已失效"
    return body


def test_badge_has_single_writer_per_flow():
    """🔴 加载期「provider 徽标」只允许一个写入点。

    失效模式（本轮实测踩到）：徽标写入原本放在 `fillOcrForm()` 里，而
    `showAutoActivateNotice()` 排在其后 ⇒ 徽标文案取决于**两个函数的调用顺序**
    这一隐形契约 —— 一旦重排，`（已自动从 X 切换）` 会被静默覆盖回纯名称，
    而所有既有断言仍然全绿（典型的"空断言"陷阱）。

    ⇒ 钉死：加载期写入点收敛到 `showAutoActivateNotice` 内，`fillOcrForm` 内不得有。
    """
    js = _read(SETTINGS_JS)
    badge = 'getElementById("llm-provider-badge")'

    # ── 正向：提示函数必须自己渲染徽标，且覆盖"无通知"的常态分支 ──────
    notice = _js_func_body(js, "function showAutoActivateNotice(")
    assert badge in notice, "加载期徽标必须由 showAutoActivateNotice 写入"
    assert "display(activeProvider)" in notice, (
        "无自动改写时也要写回纯名称 —— 否则徽标无人渲染（信息丢失）"
    )

    # ── 反向：OCR 表单填充不得再写徽标（这是唯一残留的第二写入点）─────
    filler = _js_func_body(js, "function fillOcrForm(")
    assert badge not in filler, (
        "fillOcrForm 不得写徽标：会让徽标文案依赖两函数的调用顺序（隐形契约）"
    )

    # ── 正向对照：用户显式切换的乐观更新**仍应**保留 ──────────────────
    # 否则"要求 fillOcrForm 不写徽标"这条反向断言可以靠删光写入点轻松通过。
    switcher = _js_func_body(js, "async function setActiveProvider(")
    assert badge in switcher, (
        "用户显式切换的乐观更新不应被移除（反向断言的正向对照）"
    )


def test_badge_write_is_unconditional_and_reachable():
    """徽标写入必须「在函数体开头可达」且「不以有通知为条件」。

    本护栏由**两个真实漏过的变异**倒逼出来（首版全是文本断言，两个都放过）：
      M2  写入条件由 `if (badgeEl)` 改成 `if (badgeEl && info && ...)` ⇒
          没有自动改写时徽标**不再被渲染**；而"函数体含 `display(activeProvider)`"
          这种断言仍会被三元表达式的另一分支满足 —— 典型**空断言**。
      M3  在函数开头插 `return;` ⇒ 整段写入成为**运行期死代码**，
          纯文本断言完全看不出来。

    ⇒ 改为**结构化**判据：看写入语句**之前**有什么（提前返回？），
      以及它所在 `if` 的**条件表达式**里有没有 `info`。
    """
    js = _read(SETTINGS_JS)
    notice = _js_func_body(js, "function showAutoActivateNotice(")

    write_at = notice.find("badgeEl.textContent")
    assert write_at != -1, "前提校验失败：未找到徽标写入语句（判据可能已失效）"
    head = notice[:write_at]

    # ── M3：写入之前不得有 return（提前返回 ⇒ 死代码）─────────────────
    assert "return" not in head, (
        "徽标写入之前出现 return ⇒ 写入成为运行期死代码（页面上永不执行）"
    )

    # ── M2：写入所在 if 的条件不得依赖 info ──────────────────────────
    open_at = head.rindex("if (")
    cond = head[open_at + 4 : head.index(")", open_at)]
    assert cond.strip(), "前提校验失败：未解析出写入所在 if 的条件表达式"
    assert "info" not in cond, (
        f"徽标写入不得以「有通知」为条件（实际条件={cond!r}）——"
        " 无自动改写时徽标必须照样渲染，否则信息丢失"
    )
