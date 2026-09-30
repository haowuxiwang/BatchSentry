"""confirm-dialog.js 的行为护栏（此前**零**行为覆盖）。

**为什么必须测**：`confirmDialog` 是删除/归档/取消/重试等**不可逆操作**的统一
确认入口。它的安全语义写在注释里（"危险操作默认聚焦取消，回车不误确认"），
但此前没有任何用例验证 —— 一旦键盘分支被改坏，用户按一次回车就删掉批记录
及其审计日志。这类"注释承诺 vs 实际行为"的漂移只能靠**真实调用**抓住。

用 `tests/js_harness.py` 的假 DOM 真实驱动回调，断言落在**可观测决策**上
（Promise 的解析值、哪个元素被聚焦、监听器是否被清理），而非 DOM 树复刻。
"""
from __future__ import annotations

import pytest

from tests.js_harness import run_js_async

# 驱动辅助：`p` 可能在"回车落在按钮上"时**故意**不 settle（交给浏览器默认 click），
# 故用哨兵值而不是 await，避免 node 因挂起而静默零输出。
#
# ⚠️ 关于"焦点在不在按钮上"：`confirmDialog` 打开后用 `setTimeout(() => cancelBtn.focus(), 50)`
#   把焦点移到取消按钮。因此本文件里凡是**想测"焦点尚未落到按钮"兜底分支**的用例，
#   都必须在 `__flushTimers()` **之前**按键（或显式把 activeElement 设成 body），
#   否则会静默测到"焦点在按钮上"的浏览器默认分支。这类用例一律内联写法并自证前置条件。
_DRIVE = r"""
const R = window.PBC;
async function drive(opts, action) {
  const p = R.confirmDialog(opts);
  __flushTimers();               // 初始 focus 定时器（50ms）
  action();
  __flushTimers();               // settle 的 150ms 定时器
  let out = "__UNRESOLVED__";
  p.then((v) => { out = v; });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  return out;
}
async function drivePrompt(opts, action) {
  const p = R.promptDialog(opts);
  __flushTimers();
  action();
  __flushTimers();
  let out = "__UNRESOLVED__";
  p.then((v) => { out = v; });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  return out;
}
const buttons = () => __queryAll(document.body, "button");
const clickCancel = () => buttons()[0].dispatchEvent({ type: "click" });
const clickConfirm = () => { const b = buttons(); b[b.length - 1].dispatchEvent({ type: "click" }); };
"""


def _probe(body: str):
    return run_js_async(["confirm-dialog.js"], _DRIVE + "\n" + body)


class TestConfirmDialogDismissPaths:
    """所有"取消"路径都必须解析为 false —— 解析成 true 即误执行不可逆操作。"""

    def test_escape_resolves_false(self):
        assert _probe('console.log(JSON.stringify(await drive({title:"t"}, () => __keydown("Escape"))));') is False

    def test_overlay_click_resolves_false(self):
        """点遮罩空白处 = 取消（target 是 overlay 本身）。"""
        body = """
let last = null;
const p = R.confirmDialog({title:"t"});
__flushTimers();
const overlay = document.body.children[document.body.children.length - 1];
overlay.dispatchEvent({ type: "click", target: overlay });
__flushTimers();
p.then((v) => { last = v; });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify(last));
"""
        assert _probe(body) is False

    def test_cancel_button_resolves_false(self):
        assert _probe('console.log(JSON.stringify(await drive({title:"t"}, clickCancel)));') is False

    def test_confirm_button_resolves_true(self):
        assert _probe('console.log(JSON.stringify(await drive({title:"t"}, clickConfirm)));') is True


class TestEnterKeySafety:
    """回车语义是本模块**唯一**的数据安全防线（注释承诺"危险操作默认取消"）。"""

    def test_danger_enter_without_button_focus_cancels(self):
        """危险操作：焦点不在按钮时回车 ⇒ **取消**（防误触删除）。

        前置条件（focusAtPress=BODY）由用例自身断言，避免因驱动顺序变化
        而"静默测到另一个分支"——那正是这条护栏最初失效的原因。
        """
        body = """
const p = R.confirmDialog({ title: "t", danger: true });
document.activeElement = document.body;   // 焦点尚未落到按钮（真实：刚打开）
const focusAtPress = document.activeElement.tagName;
__keydown("Enter");
__flushTimers();
let out = "__UNRESOLVED__";
p.then((v) => { out = v; });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify({ out, focusAtPress }));
"""
        got = _probe(body)
        assert got["focusAtPress"] == "BODY", "前置条件失真：按键时焦点不在 body，本用例未覆盖兜底分支"
        assert got["out"] is False, "danger 对话框回车被当成确认 —— 用户会误删数据"

    def test_non_danger_enter_without_button_focus_confirms(self):
        """正向对照：非危险操作回车 ⇒ 确认（用户预期，别为安全把普通流程也挡住）。"""
        body = """
const p = R.confirmDialog({ title: "t", danger: false });
document.activeElement = document.body;
const focusAtPress = document.activeElement.tagName;
__keydown("Enter");
__flushTimers();
let out = "__UNRESOLVED__";
p.then((v) => { out = v; });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify({ out, focusAtPress }));
"""
        got = _probe(body)
        assert got["focusAtPress"] == "BODY", "前置条件失真：未覆盖兜底分支"
        assert got["out"] is True

    def test_enter_while_cancel_focused_does_not_settle(self):
        """焦点在按钮上时，回车**不得**由 keydown 处理（否则会与浏览器默认
        click 叠加，出现"按一次回车结算两次"或绕过焦点语义）。

        ⚠️ 检测手段必须是 settle() 的**同步副作用**，不能只看 Promise 值：
        settle() 先改样式、再把 resolve 包进 150ms 定时器，因此"未 flush 定时器
        时 Promise 仍是 pending"**无法**区分"没结算"和"结算了但还没 resolve"
        —— 早期版本正是这样退化成一条永远为真的空断言。
        这里改用两个同步信号：新增定时器数量 + overlay 透明度。
        """
        body = """
const p = R.confirmDialog({ title: "t", danger: true });
__flushTimers();                       // 焦点落到取消按钮
const overlay = document.body.children[document.body.children.length - 1];
const focusedBefore = document.activeElement;
__keydown("Enter");                    // 不应结算
const pendingAfterEnter = __timers.filter((t) => !t.cleared && !t._ran).length;
console.log(JSON.stringify({
  focusedIsButton: focusedBefore.tagName === "BUTTON",
  pendingAfterEnter,
  overlayOpacity: overlay.style.opacity || "",
}));
"""
        got = _probe(body)
        assert got["focusedIsButton"] is True, "初始焦点未落到按钮（下面的断言会失真）"
        assert got["pendingAfterEnter"] == 0, (
            "keydown 自行结算并排入了 resolve 定时器 —— 与浏览器默认 click 叠加"
        )
        assert got["overlayOpacity"] == "", "keydown 已开始关闭遮罩 —— settle() 被误触发"

    def test_danger_enter_with_focus_on_cancel_ends_as_cancel(self):
        """端到端闭环：danger + 焦点在取消按钮 + 回车 ⇒ 最终**取消**。

        上一条验证"keydown 放行不结算"，本条的浏览器默认路径补上后半程：
        keydown 放行后，浏览器对聚焦按钮派发 click ⇒ 必须落在取消按钮上。
        两步合起来才是"回车不会误删"的完整保证。
        """
        body = """
const p = R.confirmDialog({ title: "t", danger: true });
__flushTimers();                        // 焦点落到取消按钮
const focused = document.activeElement;
__keydown("Enter");                     // keydown 放行，交给浏览器
const pendingAfterEnter = __timers.filter((t) => !t.cleared && !t._ran).length;
focused.dispatchEvent({ type: "click" });   // 浏览器默认 Enter → click
__flushTimers();
let out = "__UNRESOLVED__";
p.then((v) => { out = v; });
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify({ pendingAfterEnter, out, focusedText: focused.textContent }));
"""
        got = _probe(body)
        assert got["pendingAfterEnter"] == 0, "keydown 阶段就已结算，焦点语义被绕过"
        assert got["focusedText"] == "取消", f"默认焦点不在取消按钮：{got['focusedText']!r}"
        assert got["out"] is False, "danger 对话框回车最终变成确认 —— 误删风险"


class TestFocusDiscipline:
    def test_initial_focus_is_the_cancel_button(self):
        """危险操作的默认焦点必须是"取消"（APG/Polaris/Material 3 一致规范）。"""
        body = """
const p = R.confirmDialog({ title: "t", danger: true, cancelText: "保留" });
__flushTimers();
const focused = document.activeElement;
console.log(JSON.stringify({ text: focused && focused.textContent }));
"""
        assert _probe(body) == {"text": "保留"}

    def test_tab_wraps_from_last_back_to_first(self):
        """焦点陷阱：Tab 在最后一个可聚焦元素上应回到第一个（不逃出对话框）。"""
        body = """
const p = R.confirmDialog({ title: "t" });
__flushTimers();
const bs = __queryAll(document.body, "button");
const first = bs[0], last = bs[bs.length - 1];
last.focus();
__keydown("Tab", { shiftKey: false });
console.log(JSON.stringify({
  firstText: first.textContent, lastText: last.textContent,
  focusedText: document.activeElement && document.activeElement.textContent,
}));
"""
        got = _probe(body)
        assert got["focusedText"] == got["firstText"], (
            f"Tab 未从末位绕回首位：焦点停在 {got['focusedText']!r}"
        )


class TestNoListenerLeak:
    def test_keydown_listener_is_removed_after_settle(self):
        """每次对话框都会 `document.addEventListener("keydown")` ——
        不清理就会随打开次数线性累积（并且已关闭的对话框仍响应 Esc）。"""
        body = """
const before = __docListenerCount("keydown");
const p = R.confirmDialog({ title: "t" });
__flushTimers();
const during = __docListenerCount("keydown");
clickCancel();
__flushTimers();
p.then(() => {});
await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
console.log(JSON.stringify({ before, during, after: __docListenerCount("keydown") }));
"""
        got = _probe(body)
        assert got["during"] == got["before"] + 1, "打开时未注册 keydown 监听（提取器失效？）"
        assert got["after"] == got["before"], (
            f"关闭后 keydown 监听未移除（{got['after']} vs 基线 {got['before']}）—— 监听器泄漏"
        )


class TestStatusBadge:
    def test_badge_text_is_rendered(self):
        body = """
const p = R.confirmDialog({ title: "t", statusBadge: { text: "分析中", dotClass: "bg-info" } });
__flushTimers();
const overlay = document.body.children[document.body.children.length - 1];
const all = __queryAll(overlay, "div").map((d) => d.textContent).join("|");
console.log(JSON.stringify({ hasBadge: all.includes("当前状态：分析中") }));
"""
        assert _probe(body) == {"hasBadge": True}


class TestPromptDialog:
    def test_confirm_returns_typed_value(self):
        body = """
const p = R.promptDialog({ title: "修正" });
__flushTimers();
const input = __queryAll(document.body, "input")[0];
input.value = "修正后的文本";
clickConfirm();
__flushTimers();
console.log(JSON.stringify(await p));
"""
        assert _probe(body) == "修正后的文本"

    def test_escape_returns_null(self):
        assert _probe('console.log(JSON.stringify(await drivePrompt({title:"t"}, () => __keydown("Escape"))));') is None

    def test_empty_confirm_returns_empty_string_not_null(self):
        """空字符串与"取消"语义**不同**：调用方据此判断是否真的提交
        （`correctFinding` 用 `if (!text) return;` 把空串当未填）。
        若这里退化成 null，未来改用 `text === null` 判断的调用方会静默提交空修正。"""
        body = """
const p = R.promptDialog({ title: "修正" });
__flushTimers();
const input = __queryAll(document.body, "input")[0];
input.value = "";
clickConfirm();
__flushTimers();
const v = await p;
console.log(JSON.stringify({ value: v, isNull: v === null }));
"""
        assert _probe(body) == {"value": "", "isNull": False}

    def test_default_value_is_prefilled(self):
        body = """
const p = R.promptDialog({ title: "t", defaultValue: "旧值" });
__flushTimers();
console.log(JSON.stringify({ v: __queryAll(document.body, "input")[0].value }));
"""
        assert _probe(body) == {"v": "旧值"}
