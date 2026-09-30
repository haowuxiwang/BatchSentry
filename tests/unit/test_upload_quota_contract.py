"""并发额度的**前后端一致性契约**（#17/#18/#19 修复的后端半边）。

**要防的失效模式**

后端配额是 `api.jobs._MAX_CONCURRENT_JOBS`（默认 3），超限**硬拒绝 409**。
前端若不知道这个数字，多文件串行上传的第 4 份就会把整个文件传完再吃 409
（见 `static/upload.js` 的 quotaSnapshot 注释）。故服务端必须把额度**下发**
到上传页 —— 与既有的 `UPLOAD_LIMITS` 同一套路（单一真值 + 注入，见
`main.py` 的 index 路由）。

**本文件钉住三条**
  1. 上传页确实注入了 `concurrency.limit`，且等于**后端唯一真值**。
  2. 注入是**调用期解析**的 —— patch `api.jobs._MAX_CONCURRENT_JOBS` 后页面
     必须跟着变。若写成顶层 `from-import`，值会冻结在 import 时刻，
     `MAX_CONCURRENT_JOBS` env 覆盖与测试 patch 全部失效（"前端显示 3、
     后端实际 5"这种漂移正是本条要拦的）。
  3. 注入的数字与 409 文案里的数字**同源**（否则界面说 3、报错说 5）。
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from tests.conftest import *  # noqa: F401,F403  (test_client / test_db fixtures)


def _injected_limit(html: str) -> int:
    """从上传页 HTML 里取 `concurrency: { limit: N }`。

    取不到就抛 —— 否则下面的断言会退化成"没读到也算过"（空断言）。
    """
    m = re.search(r"concurrency:\s*\{\s*limit:\s*(\d+)\s*\}", html)
    assert m, "上传页里没有 concurrency.limit 注入 —— 前端拿不到额度"
    return int(m.group(1))


class TestHarnessIsNotVacuous:
    @pytest.mark.asyncio
    async def test_page_carries_the_injection_point(self, test_client):
        """`window.__PBC__` 必须存在 —— 否则本文件所有断言都无从谈起。"""
        r = await test_client.get("/")
        assert r.status_code == 200
        assert "window.__PBC__" in r.text, "注入点消失了，前端拿不到任何服务端限额"

    @pytest.mark.asyncio
    async def test_existing_limits_injection_is_still_there(self, test_client):
        """对照：既有的 `limits`（上传体积）注入不能被本次改动挤掉。"""
        r = await test_client.get("/")
        assert "limits:" in r.text
        assert "max_bytes" in r.text


class TestConcurrencyLimitIsInjected:
    @pytest.mark.asyncio
    async def test_default_limit_is_three(self, test_client):
        from api.jobs import _MAX_CONCURRENT_JOBS

        r = await test_client.get("/")
        assert _injected_limit(r.text) == _MAX_CONCURRENT_JOBS

    @pytest.mark.asyncio
    async def test_injected_limit_is_resolved_at_call_time(self, test_client):
        """**调用期解析**：patch 后端真值后，页面必须跟着变。

        写成模块级 `from api.jobs import _MAX_CONCURRENT_JOBS` 会让值冻结在
        import 时刻 ⇒ 本用例变红（这正是它存在的意义）。
        """
        with patch("api.jobs._MAX_CONCURRENT_JOBS", 7):
            r = await test_client.get("/")
        assert _injected_limit(r.text) == 7, (
            "注入的额度没跟着后端真值走 —— 很可能是顶层 from-import 冻结了值"
        )

    @pytest.mark.asyncio
    async def test_zero_or_negative_limit_is_still_injected_verbatim(self, test_client):
        """不在这里做"合法性兜底"：注入的就是真值。

        前端的契约是 `limit <= 0 ⇒ 视为未知 ⇒ 跳过预检`（fail-open），
        所以后端**照实下发**即可；在这里替前端改数会让两侧语义分叉。
        """
        with patch("api.jobs._MAX_CONCURRENT_JOBS", 0):
            r = await test_client.get("/")
        assert _injected_limit(r.text) == 0


class TestInjectedLimitAgreesWithTheRejectionMessage:
    """界面上的数字与 409 文案里的数字必须同源。"""

    @pytest.mark.asyncio
    async def test_limit_appears_in_both_the_page_and_the_409(self, test_client):
        from fastapi import HTTPException

        from api.jobs import create_job

        with patch("api.jobs._MAX_CONCURRENT_JOBS", 2):
            r = await test_client.get("/")
            assert _injected_limit(r.text) == 2

            db = await _db()
            for i in range(2):
                await db.execute(
                    "INSERT INTO jobs (id, filename, pdf_path, status) "
                    "VALUES (?, ?, ?, 'pending')",
                    (f"probe-active-{i}", f"a{i}.pdf", f"/tmp/a{i}.pdf"),
                )
            await db.commit()

            with pytest.raises(HTTPException) as ei:
                await create_job(
                    file=_pdf_upload(), force=False, request=None
                )

        assert ei.value.status_code == 409
        assert "上限为 2" in str(ei.value.detail), (
            f"409 文案里的数字与页面上的一致（2）：{ei.value.detail!r}"
        )


async def _db():
    from db.client import get_db

    return await get_db()


def _pdf_upload():
    """一份内容唯一的最小 PDF 上传对象（避开 md5 去重 409 的干扰）。"""
    import io
    import uuid

    import fitz
    from starlette.datastructures import Headers, UploadFile

    doc = fitz.open()
    doc.new_page().insert_text((50, 50), f"quota contract {uuid.uuid4()}")
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    data = buf.getvalue()
    return UploadFile(
        file=io.BytesIO(data),
        size=len(data),
        filename="contract.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )
