"""`confirm-dialog.js` 的两项可访问性 / 交互韧性护栏（P2）。

**8. 嵌套弹窗的滚动锁**：弹窗可以叠加（设置页在"删除 provider"确认框之上又弹
"未保存"提示；删除确认框里点"前往设置"再弹一个）。旧实现每处关闭都直接
`document.body.style.overflow = ""`，于是**先关闭的那个**把锁解掉，而另一个仍在
显示 —— 用户看到弹窗还开着，背后的页面却能滚，像弹窗"飘"了。改为计数器后，
只有最后一个弹窗关闭才恢复滚动。

**9. 错误 toast 的播报及时性**：旧实现把 live region 放在**容器**上
（`role="status"` + `aria-live="polite"`），于是"删除失败"这类必须尽快播报的
消息也要排队等读屏用户停下手上的操作。改为逐条 toast 定 role：
`err → role="alert"`（隐式 assertive），其余 `role="status"`（隐式 polite）。
"""
from __future__ import annotations

from tests.js_harness import run_js_async

FILES = ["confirm-dialog.js"]

_PRE = r"""
const R = window.PBC;
const btns = () => __queryAll(document.body, "button");
const ov = () => document.body.style.overflow;
const dialogs = () => __queryAll(document.body, '[role="dialog"]');
const boxes = () => __queryAll(document.body, '[role="textbox"]');
"""


def _probe(body: str):
    return run_js_async(FILES, _PRE + "\n" + body)


# ══════════════════════════════════════════════════════════════════════
class TestHarnessIsNotVacuous:
    """先证明驱动链真的通 —— 否则下面全是空断言。"""

    def test_dialog_opens_and_locks_scroll(self):
        assert _probe("""
R.confirmDialog({ title: "A" });
console.log(JSON.stringify({
  dialogs: dialogs().length, buttons: btns().length, overflow: ov(),
}));
""") == {"dialogs": 1, "buttons": 2, "overflow": "hidden"}

    def test_closing_the_only_dialog_restores_scroll(self):
        """防空转：证明"恢复"这条路径真的可达（否则下面测的是恒 hidden）。"""
        assert _probe("""
R.confirmDialog({ title: "A" });
btns()[0].dispatchEvent({ type: "click" });
__flushTimers();
console.log(JSON.stringify({ dialogs: dialogs().length, overflow: ov() }));
""") == {"dialogs": 0, "overflow": ""}


# ══════════════════════════════════════════════════════════════════════
class TestNestedDialogsKeepTheScrollLock:
    """滚动锁必须按**叠加层数**计数，而不是布尔复位。"""

    def test_closing_the_first_of_two_keeps_the_lock(self):
        assert _probe("""
R.confirmDialog({ title: "A" });
const afterA = ov();
R.confirmDialog({ title: "B" });
const afterB = ov();
btns()[0].dispatchEvent({ type: "click" });   // 关 A（下层）
__flushTimers();
const afterCloseA = ov();                     // ← 修复点：必须仍锁定
btns()[0].dispatchEvent({ type: "click" });   // 关 B（A 的按钮已移除）
__flushTimers();
const afterCloseB = ov();
console.log(JSON.stringify({ afterA, afterB, afterCloseA, afterCloseB }));
""") == {"afterA": "hidden", "afterB": "hidden",
        "afterCloseA": "hidden", "afterCloseB": ""}

    def test_mixed_confirm_and_prompt_share_the_same_lock(self):
        """两种弹窗共用一把锁 —— 否则"确认框 + 输入框"叠加时又会提前解锁。"""
        assert _probe("""
R.confirmDialog({ title: "A" });
R.promptDialog({ title: "B" });
btns()[0].dispatchEvent({ type: "click" });   // 关确认框
__flushTimers();
const mid = ov();
btns()[0].dispatchEvent({ type: "click" });   // 关输入框
__flushTimers();
console.log(JSON.stringify({ mid: mid, end: ov(), dialogs: dialogs().length }));
""") == {"mid": "hidden", "end": "", "dialogs": 0}

    def test_three_deep_requires_three_closes(self):
        """计数不能退化成"关一次就解锁"—— 三层时前两次关闭都必须保持锁定。"""
        assert _probe("""
R.confirmDialog({ title: "A" });
R.confirmDialog({ title: "B" });
R.confirmDialog({ title: "C" });
btns()[0].dispatchEvent({ type: "click" }); __flushTimers();
const o1 = ov();
btns()[0].dispatchEvent({ type: "click" }); __flushTimers();
const o2 = ov();
btns()[0].dispatchEvent({ type: "click" }); __flushTimers();
console.log(JSON.stringify({ o1: o1, o2: o2, o3: ov() }));
""") == {"o1": "hidden", "o2": "hidden", "o3": ""}


# ══════════════════════════════════════════════════════════════════════
class TestToastLiveRegionSemantics:
    """live region 必须在**每条 toast** 上，且错误用 assertive。"""

    def test_container_is_not_itself_a_live_region(self):
        """容器若仍是 live region，逐条 role 就会被容器的 politeness 盖过。"""
        assert _probe("""
R.showToast("hi", "ok");
const box = document.getElementById("toast-container");
console.log(JSON.stringify({
  role: box.getAttribute("role"),
  live: box.getAttribute("aria-live"),
  label: box.getAttribute("aria-label"),
}));
""") == {"role": "region", "live": None, "label": "通知"}

    def test_error_toast_is_assertive_and_others_polite(self):
        assert _probe("""
R.showToast("已保存", "ok");
R.showToast("删除失败", "err");
R.showToast("提示", "info");
const box = document.getElementById("toast-container");
console.log(JSON.stringify({
  roles: box.children.map((c) => c.getAttribute("role")),
  texts: box.children.map((c) => c.textContent),
}));
""") == {
            "roles": ["status", "alert", "status"],
            "texts": ["已保存", "删除失败", "提示"],
        }

    def test_default_type_is_polite(self):
        """`type` 缺省（调用方没传）时不得误判成错误而用 assertive 打断用户。"""
        assert _probe("""
R.showToast("没传 type");
const box = document.getElementById("toast-container");
console.log(JSON.stringify({
  role: box.children[0].getAttribute("role"),
  n: box.children.length,
}));
""") == {"role": "status", "n": 1}
