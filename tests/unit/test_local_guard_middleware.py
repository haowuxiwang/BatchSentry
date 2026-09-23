# -*- coding: utf-8 -*-
"""B11-1 护栏：本地守卫的**时序位置**与请求体硬上限。

背景（第五轮对抗性审查 S1，2026-09-23 实测）
--------------------------------------------
守卫原本写在**端点函数体内**（`api/jobs/upload.py` 第一行），而 FastAPI 在
**调用端点之前**就解析请求体（端点签名要求 `UploadFile`）⇒ 守卫排在解析器之后。
实测（跨站形态）：畸形体 1 MB **0.62 s** → 16 MB **8.60 s**，且状态是 **422**
而非 403 ⇒ 守卫**一行都没执行**；负对照（同尺寸不解析）仅 7–78 ms。
修复 = 前移到 **ASGI 中间件**（`main.LocalGuardMiddleware`，注册在最外层）。

本文件盯三层，缺一不可
----------------------
  ① **单元行为**：用最小下游 app（`_echo`）测中间件判定，**不依赖产品业务代码**
     —— 这样"业务侧改动导致测试恰巧通过/失败"不会污染判据。
  ② **真的接上了**：`main.app` 的中间件栈**最外层**必须是它。单元测试全绿、
     但没挂到 app 上，等于没修。
  ③ **源码顺序**：`add_middleware(LocalGuardMiddleware, ...)` 必须是**最后一个**
     中间件注册（**AST 判据**）。Starlette 是 **LIFO —— 后加的先执行**，
     注册位置错了守卫就被压回解析之后；而**行为上极难发现**（功能仍正常，
     只是防护失效）。用 grep 找 token 在这里会退化成恒真（PITFALLS §二十六）。

⚠️ 运行期端到端证据在 `devlogs/_verify/probe_guard_middleware.py`
（修复前 B/C 耗时比值 **87–127×** ⇒ 修复后 **0.65–0.82**），与静态判据互补。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from main import _BAD_LENGTH, LocalGuardMiddleware, _declared_length

REPO = Path(__file__).resolve().parent.parent.parent
MAIN_SRC = REPO / "main.py"
GUARD = "LocalGuardMiddleware"

# 单元测试用的小上限（真实上限 200 MB，实跑不可行）
LIMIT = 4096
HOST_OK = "127.0.0.1:58765"
EVIL_ORIGIN = "https://evil.example"


# ── 测试替身 ────────────────────────────────────────────────────────────────


def _recording_downstream(*, respond: bool):
    """造一个能记录"读到多少字节"的下游。

    `respond=True` —— 读完就回 200（**正常**下游）。
    `respond=False` —— 只读、**不产生任何响应**，用来模拟真实解析器在中断时的
    行为（FastAPI 在 body 解析被掐断时**不会**写出一个成功响应）。
    这个区分是必要的：守卫只在"下游尚未产生响应"时才能给出 413；一旦下游已经
    开写，状态码就不可更改（HTTP 语义），此时正确行为是**保持下游状态码并断开**。
    """
    box = {"received": 0}

    async def app(scope, receive, send) -> None:
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            box["received"] += len(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        if respond:
            body = json.dumps({"received": box["received"]}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length",
                                     str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

    return app, box


async def _echo(scope, receive, send) -> None:
    """最小下游：统计收到的请求体字节数并回 200（不碰任何产品代码）。"""
    await _recording_downstream(respond=True)[0](scope, receive, send)


def _guard(app=None) -> LocalGuardMiddleware:
    return LocalGuardMiddleware(app or _echo, max_body_bytes=LIMIT)


def _client(app, base_url: str = "http://127.0.0.1:58765") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url=base_url)


async def _call_raw(*, headers: list[tuple[bytes, bytes]],
                    chunks: list[bytes],
                    app=None) -> tuple[int | None, list[dict]]:
    """直接按 ASGI 调中间件 —— 用于 httpx 表达不出的形态。

    典型：**负的 `Content-Length`**（httpx 会自己算，不让设）、**重复头**、
    以及需要精确控制分块的 chunked 流。
    """
    sent: list[dict] = []
    pending = list(chunks)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/jobs",
        "raw_path": b"/api/jobs", "query_string": b"",
        "root_path": "", "headers": headers,
        "server": ("127.0.0.1", 58765), "client": ("127.0.0.1", 51234),
    }

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body,
                    "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await _guard(app)(scope, receive, send)
    status = next((m["status"] for m in sent
                   if m["type"] == "http.response.start"), None)
    return status, sent


def _hdr(*pairs: tuple[str, str]) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode(), v.encode()) for k, v in pairs]


# ── ① 单元行为：守卫 ────────────────────────────────────────────────────────


async def test_cross_site_host_is_rejected():
    """Host 不是本机 ⇒ 403（不读 body 即可判定）。"""
    async with _client(_guard(), base_url="http://evil.test") as c:
        r = await c.post("/api/jobs", content=b"x" * 16)
    assert r.status_code == 403


async def test_cross_site_origin_is_rejected():
    """Host 合法（浏览器必然如此）+ 跨站 Origin ⇒ 403。"""
    async with _client(_guard()) as c:
        r = await c.post("/api/jobs", content=b"x" * 16,
                         headers={"Origin": EVIL_ORIGIN})
    assert r.status_code == 403


async def test_malformed_body_with_cross_site_origin_is_403_not_422():
    """★ 核心回归判据：**畸形体** + 跨站 Origin ⇒ 403，而**不是** 422。

    422 意味着"请求体校验失败" ⇒ 说明解析器已经跑过、守卫没被执行。
    这正是 S1 的原症状（修复前实测 1→16 MB 得 618→8603 ms 的 422）。
    """
    async with _client(_guard()) as c:
        r = await c.post(
            "/api/jobs",
            content=b'--B\r\nContent-Disposition: form-data; name="file"\r\nX: '
                    + b"A" * 8192,          # 无终结符 ⇒ 畸形
            headers={"Origin": EVIL_ORIGIN,
                     "Content-Type": "multipart/form-data; boundary=B"},
        )
    assert r.status_code == 403, f"畸形跨站请求必须 403，实测 {r.status_code}"


async def test_local_request_passes_through():
    """同源小体 ⇒ 放行，且下游**收到完整字节数**。"""
    payload = b"z" * 512
    async with _client(_guard()) as c:
        r = await c.post("/api/jobs", content=payload)
    assert r.status_code == 200
    assert json.loads(r.content)["received"] == len(payload)


async def test_local_origin_is_allowed():
    """Origin 为本机 ⇒ 放行（别把合法页面误伤）。"""
    async with _client(_guard()) as c:
        r = await c.post("/api/jobs", content=b"z" * 16,
                         headers={"Origin": "http://127.0.0.1:58765"})
    assert r.status_code == 200


# ── ① 单元行为：请求体上限 ──────────────────────────────────────────────────


async def test_declared_length_over_limit_is_413():
    """声明值即超限 ⇒ 413，**一个 body 字节都不必读**。"""
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("content-length", str(LIMIT + 1))),
        chunks=[],
    )
    assert status == 413


async def test_declared_length_exactly_at_limit_is_allowed():
    """边界：恰好等于上限 **不得** 被拒（否则是"差一"缺陷）。"""
    async with _client(_guard()) as c:
        r = await c.post("/api/jobs", content=b"z" * LIMIT)
    assert r.status_code == 200


async def test_negative_content_length_is_rejected():
    """负 `Content-Length` ⇒ 400（对应 `CVE-2026-53540` 的"全量缓冲"形态）。"""
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("content-length", "-1")),
        chunks=[b"x" * 16],
    )
    assert status == 400


async def test_duplicate_content_length_is_rejected():
    """重复 `Content-Length` ⇒ 400（请求走私形态）。"""
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("content-length", "10"),
                     ("content-length", "20")),
        chunks=[b"x" * 16],
    )
    assert status == 400


async def test_non_numeric_content_length_is_rejected():
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("content-length", "abc")),
        chunks=[b"x" * 16],
    )
    assert status == 400


async def test_chunked_over_limit_stops_reading():
    """★ 核心判据：chunked（无 `Content-Length`）超限 ⇒ **下游提前停止读取** + 413。

    这是"只信 `Content-Length` 会被绕过"的那条路：没有声明值 ⇒ 只能在读的过程中
    掐断。判据不是"状态码好看"，而是**下游实际读到的字节数**必须显著小于发送量
    （否则就是"无限缓冲"，DoS 依旧成立）。

    用 `respond=False` 的下游：真实解析器在被掐断时**不会写出成功响应**
    （实测产物侧得到 400 "error parsing the body"），守卫因此能给出确定的 413。
    """
    app, box = _recording_downstream(respond=False)
    chunk = b"x" * 1024
    total_sent = len(chunk) * 8          # 8 KB，上限 4 KB
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("transfer-encoding", "chunked")),
        chunks=[chunk] * 8, app=app,
    )
    assert box["received"] < total_sent, (
        f"下游读到了全部 {box['received']} 字节（上限 {LIMIT}）⇒ 流式掐断没生效")
    assert box["received"] <= LIMIT + len(chunk), (
        f"下游读到 {box['received']}，超出「上限 + 一个块」的容许范围")
    assert status == 413, f"下游未产生响应时应由守卫给出 413，实测 {status}"


async def test_chunked_over_limit_does_not_override_downstream_response():
    """下游**已产生响应**时，守卫不得改写它的状态码。

    固定这条行为，防止有人"为了让 413 出现"去覆盖已发出的响应 —— 那会造出
    状态码与 body 不符的畸形响应，比"不返回 413"更糟。
    此时正确行为 = 保持下游状态码 + 断开连接。
    """
    app, box = _recording_downstream(respond=True)
    chunk = b"x" * 1024
    status, _ = await _call_raw(
        headers=_hdr(("host", HOST_OK), ("transfer-encoding", "chunked")),
        chunks=[chunk] * 8, app=app,
    )
    assert box["received"] < len(chunk) * 8, "流式掐断没生效"
    assert status == 200, f"守卫不应改写下游已产生的状态码，实测 {status}"


async def test_declared_length_parser_helper():
    """`_declared_length` 的取值域：None / int / 哨兵（负数、非数字、重复）。"""
    assert _declared_length({"headers": []}) is None
    assert _declared_length({"headers": [(b"content-length", b"123")]}) == 123
    assert _declared_length(
        {"headers": [(b"content-length", b"-1")]}) is _BAD_LENGTH
    assert _declared_length(
        {"headers": [(b"content-length", b"abc")]}) is _BAD_LENGTH
    assert _declared_length(
        {"headers": [(b"content-length", b"1"),
                     (b"content-length", b"2")]}) is _BAD_LENGTH
    # 头名大小写不敏感由 ASGI 规范保证（运行时恒小写），但别把无关头误判
    assert _declared_length({"headers": [(b"x-other", b"9")]}) is None


# ── ② 真的接上了：端到端 + 运行期栈位置 ────────────────────────────────────


async def test_product_app_rejects_cross_site_malformed_before_parsing():
    """★ 端到端：**产品 app** 上，畸形跨站请求必须 403（而非 422/400）。

    与上面的单元判据**互补、缺一不可**：
      · 单元判据（用 `_guard()`）证"中间件逻辑对"；
      · 本条证"它真的接在产品上，且位置在 body 解析之前"。
    只做单元判据会留下缺口 —— 变异 M1（把 `app.add_middleware` 整行删掉）
    只会打红本条，单元判据全绿（实测踩过）。
    """
    from main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:58765",
    ) as c:
        r = await c.post(
            "/api/jobs",
            content=b'--B\r\nContent-Disposition: form-data; name="file"\r\nX: '
                    + b"A" * 4096,          # 无终结符 ⇒ 畸形
            headers={"Origin": EVIL_ORIGIN,
                     "Content-Type": "multipart/form-data; boundary=B"},
        )
    assert r.status_code == 403, (
        f"产品 app 上畸形跨站请求必须 403，实测 {r.status_code}"
        "（422/400 意味着请求体已被解析 ⇒ 守卫位置又退回解析之后）")


def test_guard_is_outermost_in_app_stack():
    """`main.app` 的中间件栈**最外层**必须是守卫。

    Starlette 的 `user_middleware[0]` 就是最外层（`add_middleware` 用 `insert(0, ...)`）
    ⇒ 它**最先执行**，因此排在请求体解析之前。单元测试全绿但没挂上去 = 没修。
    """
    from main import app

    stack = app.user_middleware
    assert stack, "中间件栈为空 ⇒ 守卫没注册"
    assert stack[0].cls.__name__ == GUARD, (
        "最外层中间件不是守卫，而是 "
        f"{stack[0].cls.__name__} ⇒ 守卫可能又被压在 body 解析之后")
    assert stack[0].kwargs.get("max_body_bytes", 0) > 0, "上限未注入"


# ── ③ 源码顺序：AST 判据 ────────────────────────────────────────────────────


def _middleware_registrations(src: str) -> list[tuple[int, str]]:
    """收集源码里全部中间件注册的 (行号, 名字)，按行号排序。

    两种形态都要覆盖：
      · `app.add_middleware(Cls, ...)`
      · `@app.middleware("http")` 装饰的函数（`BaseHTTPMiddleware`）
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_middleware" and node.args):
            arg = node.args[0]
            name = getattr(arg, "id", None) or getattr(arg, "attr", None) or "?"
            found.append((node.lineno, name))
        elif isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "middleware"):
                    found.append((dec.lineno, f"@middleware:{node.name}"))
    return sorted(found)


def test_guard_is_registered_last_in_source():
    """★ AST 判据：守卫必须是**最后一个**注册的中间件（LIFO ⇒ 最后注册最先执行）。

    为什么不用文本判据：`LocalGuardMiddleware` 这个名字在文件里出现多次
    （定义、注释、注册），"找得到"与"位置对"是两件事 ⇒ 文本判据会退化成恒真。
    """
    regs = _middleware_registrations(MAIN_SRC.read_text(encoding="utf-8-sig"))
    guard_lines = [ln for ln, name in regs if name == GUARD]
    assert len(guard_lines) == 1, (
        f"守卫必须**恰好注册一次**，实测 {len(guard_lines)} 次：{regs}")
    assert guard_lines[0] == regs[-1][0], (
        "守卫不是最后一个注册的中间件 ⇒ 它会被压在后面的中间件之下、"
        f"从而回到 body 解析之后。注册顺序：{regs}")


def test_ast_judgement_has_teeth():
    """自检：AST 判据本身要能抓到"顺序调换"（否则判据是空护栏）。"""
    broken = (
        "app.add_middleware(LocalGuardMiddleware, max_body_bytes=1)\n"
        "app.add_middleware(GZipMiddleware, minimum_size=1024)\n"
    )
    regs = _middleware_registrations(broken)
    guard_lines = [ln for ln, name in regs if name == GUARD]
    assert guard_lines and guard_lines[0] != regs[-1][0], (
        "自检失败：把守卫放到 GZip 之前，判据竟然没察觉")
