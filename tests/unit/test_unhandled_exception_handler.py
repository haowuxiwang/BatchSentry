# -*- coding: utf-8 -*-
"""全局未捕获异常处理器护栏（R78 第十八批，对抗性审查）。

由来：安全/鲁棒性审查发现源码**没有** `add_exception_handler`（grep 为空）⇒
未捕获异常交给 Starlette 的 `ServerErrorMiddleware`，返回**纯文本**
"Internal Server Error"：非 JSON ⇒ 前端 `r.json()` **再抛**一次，真实错误在
客户端彻底丢失；且没有集中的服务端钩子。

修法：`main.unhandled_exception_handler` —— 统一成与 `HTTPException` **同形**的
`{"detail": ...}`，**只**回显请求号（`str(exc)` 可能含路径 / 上游 URL / 密钥片段，
**不可信**），完整栈只进服务端日志。

本文件两条判据：
  ① 处理器**确实注册**在 `Exception` 上（否则等于没修）；
  ② 响应体**不含**任何异常文本（防"把 exc 回显出去"的顺手写法）。
"""
import asyncio
import json
import sys
from pathlib import Path

from starlette.requests import Request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import main as app_main  # noqa: E402
from logging_config import request_id_var  # noqa: E402


def _req(path: str = "/api/x", method: str = "GET") -> Request:
    return Request({
        "type": "http", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": [],
        "scheme": "http", "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 1),
    })


def test_handler_is_registered_for_bare_exception():
    """只注册 HTTPException 不算修 —— 未捕获异常走的是 `Exception` 这一档。"""
    assert Exception in app_main.app.exception_handlers, (
        "Exception 档没有处理器 ⇒ 未捕获异常仍返回纯文本 500（前端 r.json() 会再抛）"
    )


def test_response_is_json_500_in_the_http_exception_shape():
    rid = "rid-r78-0001"
    token = request_id_var.set(rid)
    try:
        resp = asyncio.run(app_main.unhandled_exception_handler(
            _req(), RuntimeError("boom")))
    finally:
        request_id_var.reset(token)
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    body = json.loads(resp.body.decode("utf-8"))
    assert "detail" in body, "形状必须与 HTTPException 一致（前端只解 detail）"
    assert rid in body["detail"], "请求号必须回显，否则用户报障无从对日志"


def test_response_never_leaks_exception_text():
    """`str(exc)` 可能含路径 / 上游 URL / 密钥片段 —— 一律不得回显。"""
    secrets = ("sk-abc123", "C:\\Users\\someone\\config.json",
               "https://api.siliconflow.cn/v1", "RuntimeError")
    exc = RuntimeError(" | ".join(secrets))
    token = request_id_var.set("rid-r78-0002")
    try:
        resp = asyncio.run(app_main.unhandled_exception_handler(_req(), exc))
    finally:
        request_id_var.reset(token)
    text = resp.body.decode("utf-8")
    for s in secrets:
        assert s not in text, f"响应泄漏了内部细节：{s!r}"
