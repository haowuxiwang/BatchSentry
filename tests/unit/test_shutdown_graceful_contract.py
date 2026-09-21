# -*- coding: utf-8 -*-
"""优雅退出通道的契约护栏（v1.2.0）。

背景（2026-09-21 实测，`devlogs/_verify/probe_shutdown_semantics.py`）：
`/api/shutdown` 曾**只取消任务、不让服务退出**；关停只能靠 Electron 侧 SIGTERM，
而 Windows 上 Node 的 `kill('SIGTERM')` 等价于 TerminateProcess、**不执行任何 Python
代码** ⇒ uvicorn 的 lifespan 收尾从不运行、`close_db()` 不执行、SQLite `-wal`/`-shm`
原样残留（实测：shutdown 后进程仍存活、随后被 0.02s 强杀、`-wal` 494 KB 留存）。
修复 = 端点请求 uvicorn `Server.should_exit`（由 `server.py` 注入）。

本文件盯四件事，每条都做过**变异验证**（故意破坏 ⇒ 指定用例必须变红）：

  1. **没有退出通道时必须看得出来**：未绑定触发器 ⇒ `exit_requested` 必须是
     `False`。否则"没有通道"会被读成"已经优雅退出了"（本次要修的自欺）。
  2. **有通道时必须真的被触发**。
  3. **触发必须晚于响应**：否则 uvicorn 可能在响应写完前开始收连接，客户端拿到
     的不是 200 而是 RST。
  4. **`electron/main.js` 的关闭路径**不得再用 `killed` 判存活（Node 语义是
     "信号已发出"，与"已退出"无关 ⇒ 旧实现的强杀兜底是死代码），且兜底必须
     作用到 `targetPid`（**复用孤儿**路径同样要被终止 —— 旧实现整体跳过）。

⚠️ 第 4 组是**静态判据**，用"取函数体 + 包围条件 + 相对位置"降低盲区
（PITFALLS §二十八：全文找 token 会被别处满足，"文本在"≠"运行期可达"）。
运行期语义由 `probe_shutdown_semantics.py` 与产物级 e2e 覆盖，两者互补。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = ROOT / "electron" / "main.js"
SERVER_PY = ROOT / "server.py"


# ── 夹具：退出触发器是模块级全局，测完必须还原（否则污染后续用例）──


@pytest.fixture
def trigger_scope():
    import main as m

    before = m._shutdown_trigger
    yield m
    m.bind_shutdown_trigger(before)


async def _post_shutdown(client):
    return await client.post("/api/shutdown")


def _client():
    from main import app

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
        headers={"Origin": "http://127.0.0.1:8000"},
    )


class TestShutdownExitChannel:
    """端点必须"可自证"：既不能假报退出通道，也不能假报已经优雅退出。"""

    @pytest.mark.asyncio
    async def test_exit_requested_false_when_no_channel_bound(self, test_db, trigger_scope):
        """未绑定触发器（单测/TestClient/被当库导入）⇒ 必须回报 exit_requested=False。"""
        trigger_scope.bind_shutdown_trigger(None)
        async with _client() as c:
            r = await _post_shutdown(c)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "shutting_down"
        assert data["exit_requested"] is False, (
            "没有退出通道却回报 True —— 调用方（Electron）会把"
            "『没有通道』误读成『已经优雅退出』，于是不再强杀，进程永久残留"
        )

    @pytest.mark.asyncio
    async def test_exit_requested_true_and_trigger_actually_fires(
        self, test_db, trigger_scope
    ):
        """绑定了就必须回报 True，且触发器**确实**被调用过（不是只改了个返回值）。"""
        fired: list[float] = []
        trigger_scope.bind_shutdown_trigger(lambda: fired.append(time.monotonic()))
        async with _client() as c:
            r = await _post_shutdown(c)
        assert r.json()["exit_requested"] is True
        for _ in range(60):  # 上界 3s，远大于 _SHUTDOWN_EXIT_DELAY_S
            if fired:
                break
            await asyncio.sleep(0.05)
        assert fired, (
            "绑定了退出通道却从未触发 —— 服务永远不会自行退出，"
            "只能靠 Electron 强杀（= 本次要修的那个缺陷）"
        )

    @pytest.mark.asyncio
    async def test_trigger_fires_after_the_response_is_returned(self, test_db, trigger_scope):
        """触发必须晚于响应写回。

        判据用**时间戳先后**而不是"断言此刻还没触发"：后者依赖机器调度
        （响应往返若慢过延迟就会假红），前者对调度免疫 —— 延迟本身就是为了
        保证这个顺序，断言就该断在顺序上。
        """
        fired: list[float] = []
        trigger_scope.bind_shutdown_trigger(lambda: fired.append(time.monotonic()))
        async with _client() as c:
            r = await _post_shutdown(c)
        t_response = time.monotonic()
        assert r.status_code == 200
        for _ in range(60):
            if fired:
                break
            await asyncio.sleep(0.05)
        assert fired, "触发器未被调用（无法验证顺序）"
        assert fired[0] > t_response, (
            "退出请求早于响应写回 —— uvicorn 的关停流程会在响应写完前开始收连接，"
            "客户端可能拿到 RST 而不是 200"
        )

    @pytest.mark.asyncio
    async def test_trigger_failure_is_logged_not_swallowed(
        self, test_db, trigger_scope, caplog
    ):
        """退出通道抛异常必须留证据：静默失败 = 服务永不退出且无人知道。"""
        def _boom():
            raise RuntimeError("boom-in-trigger")

        trigger_scope.bind_shutdown_trigger(_boom)
        with caplog.at_level("ERROR"):
            async with _client() as c:
                r = await _post_shutdown(c)
            assert r.status_code == 200
            for _ in range(60):
                if "exit trigger failed" in caplog.text:
                    break
                await asyncio.sleep(0.05)
        assert "exit trigger failed" in caplog.text, (
            "退出触发器异常被静默吞掉 —— 关停失败会变成无声故障"
        )


# ── electron/main.js / server.py 的静态契约 ─────────────────────────────


def _js_code_only(src: str, *, strip_strings: bool = True) -> str:
    """抹掉 JS 的**注释**（可选连字符串内容一起抹），只留可执行标记。

    为什么必须归一化：护栏是文本判据，而"注释里提到被禁写法"会被误判成代码里有
    —— 实测过（`test_no_unread_pipe_in_test_harnesses` 就被 docstring 命中）。
    给文件加白名单会让**整份文件**失去保护，是更差的选项；所以只对判据做归一化。

    `strip_strings=False` 的用途：有些判据要**数命令字面量**（例如 `taskkill`
    写在模板字符串里），抹掉字符串就数不到了 —— 两种视图各有适用场景。

    ⚠️ 这仍只是**静态**判据：能证明"文本写在代码里"，不能证明"运行期可达"
    （插一个 `return;` 就能绕过）。运行期语义由 `probe_shutdown_semantics.py`
    与产物级 e2e 覆盖 —— 两类判据互补，谁也替代不了谁。
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if strip_strings and c in "\"'`":
            quote = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')  # 留占位，避免把两侧代码粘成一个 token
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _js_function_body(src: str, name: str) -> str:
    """取出 `[async] function name(...) { ... }` 的**函数体**（大括号配对）。

    取"函数体"而不是在全文里找字符串：全文搜索会被别处的同名 token 满足
    （PITFALLS §二十八），"文本在"也**不等于**"运行期可达"。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    return ""


class TestElectronShutdownStaticContract:
    """`electron/main.js` 关闭路径的静态契约（负断言 + 正断言成对，防空断言）。"""

    @pytest.fixture(scope="class")
    def js(self) -> str:
        return MAIN_JS.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def code(self, js) -> str:
        return _js_code_only(js)

    def test_code_view_is_not_eating_the_file(self, js, code):
        """先护栏打底：归一化若把源码吃掉，下面所有断言都会**空转**。

        恒真的护栏与恒假的护栏一样零判别力（PITFALLS §二十六）。这里的判据分两层：
        - **语义层**（真正的保护）：关键函数体必须仍能被定位 —— 归一化塌缩时它们会消失；
        - **sanity 下限**：留一个字节比例下限，只用来抓"灾难性吃文件"。
          不要把它当质量指标：本文件注释密度很高，实测代码视图约为原文的 **48%**，
          所以阈值必须显著低于它，否则会拿一个**正确**的剥注释实现去报假红
          （第一版写 50%，就是这么被我自己的测试打回来的）。
        """
        assert len(code) > 0.25 * len(js), (
            f"归一化后只剩 {len(code)}/{len(js)} 字节 —— 剥注释的逻辑吃掉了正文，"
            f"后续静态断言会退化成空断言"
        )
        for name in ("gracefulShutdown", "requestBackendShutdown", "childAlive",
                     "waitBackendGone", "killProcessTree", "startWatchdog"):
            assert _js_function_body(code, name), f"归一化后找不到 {name} 的函数体"

    def test_graceful_shutdown_body_is_locatable(self, code):
        """正断言打底：函数体取不到 ⇒ 下面的断言都会空转。"""
        body = _js_function_body(code, "gracefulShutdown")
        assert body, "electron/main.js 里找不到 gracefulShutdown 的函数体（改名/删除？）"
        for token in ("requestBackendShutdown", "waitBackendGone", "killProcessTree"):
            assert token in body, f"gracefulShutdown 函数体内应包含 {token}"

    def test_liveness_is_not_judged_by_killed_flag(self, code):
        """🔴 代码里不得用 `killed` 判存活。

        Node 的 `child.kill()` 在**信号发出后立刻**把 `killed` 置真（语义是"已成功
        发出信号"），与"进程已退出"无关。旧实现据此写的兜底条件恒假 ⇒
        `taskkill /T /F` 成了**死代码**。权威判据是 `exitCode` / `signalCode`。
        """
        assert ".killed" not in code, (
            "main.js 的**代码**里又出现了 `.killed` —— 它表示『信号已发出』而不是"
            "『进程已退出』，拿它当存活判据会让强杀兜底变成死代码。"
            "请改用 childAlive()（exitCode/signalCode）"
        )
        helper = _js_function_body(code, "childAlive")
        assert helper, "找不到 childAlive —— 存活判据的单一实现点被移除了？"
        assert "exitCode" in helper and "signalCode" in helper

    def test_watchdog_kill_path_also_uses_child_alive(self, code):
        """看门狗重启路径同属关闭/终止族，必须共用同一存活判据与同一实现。"""
        body = _js_function_body(code, "startWatchdog")
        assert "childAlive(pythonProcess)" in body, (
            "startWatchdog 没有用 childAlive 判存活（旧写法 `!pythonProcess.killed` "
            "是同一处缺陷的第二份拷贝）"
        )
        assert "killProcessTree(" in body, "startWatchdog 没有复用 killProcessTree"

    def test_kill_fallback_reaches_the_reused_orphan(self, code):
        """强杀兜底必须作用到 `targetPid`，且**在等待超时之后**。

        旧实现把它包在 `if (pythonProcess && ...)` 里 ⇒ 复用孤儿路径
        （pythonProcess 为 null）整体跳过终止，孤儿 pbc-server.exe 永久残留。
        只断言"函数里有 taskkill"不够 —— 必须落在 `targetPid` 上。
        """
        body = _js_function_body(code, "gracefulShutdown")
        assert "killProcessTree(targetPid)" in body, (
            "兜底没有作用到 targetPid —— 复用孤儿后端（pythonProcess 为 null）"
            "将不会被终止"
        )
        assert body.index("waitBackendGone") < body.index("killProcessTree(targetPid)"), (
            "强杀出现在等待之前 —— 那就退化成『先杀再说』，优雅路径永远走不到"
        )

    def test_shutdown_response_is_parsed_for_exit_requested(self, code):
        """必须读响应体的 `exit_requested`：否则无法区分"后端照做了"与"后端没这能力"。"""
        body = _js_function_body(code, "requestBackendShutdown")
        assert body, "找不到 requestBackendShutdown"
        assert "exit_requested" in body, (
            "requestBackendShutdown 没有解析 exit_requested —— 旧实现 res.resume() "
            "把响应体丢掉，于是只能假定后端优雅了"
        )
        fn = _js_function_body(code, "gracefulShutdown")
        assert "exitRequested" in fn, "gracefulShutdown 没有使用解析结果做告警判据"

    def test_taskkill_has_a_single_implementation(self, js):
        """`taskkill` 全仓只留一处实现 —— 两份拷贝各自漂移正是本轮缺陷的成因。

        本条用 `strip_strings=False` 的视图：命令字面量写在模板字符串里，
        抹掉字符串就数不到了（判据要选对视图，否则是**恒真**的空断言）。
        """
        no_comments = _js_code_only(js, strip_strings=False)
        n = no_comments.count("taskkill")
        assert n == 1, (
            f"taskkill 在 main.js 代码里出现 {n} 次 —— 终止进程树必须只有 "
            f"killProcessTree 一处实现（两份拷贝是本轮缺陷的直接成因）"
        )


class TestServerEntryBindsExitTrigger:
    """`server.py` 必须把 uvicorn 的 `Server` 注入应用 —— 否则端点没有退出通路。"""

    def test_server_constructs_server_object_and_binds(self):
        src = SERVER_PY.read_text(encoding="utf-8")
        assert "uvicorn.Server(" in src, (
            "server.py 没有显式构造 uvicorn.Server —— `uvicorn.run()` 不返回引用，"
            "端点拿不到 Server，就没有『请求进程优雅退出』的通路"
        )
        assert "bind_shutdown_trigger" in src, "server.py 没有把退出触发器注入应用"
        assert "should_exit" in src, "server.py 没有设置 Server.should_exit（uvicorn 的关停契约）"
