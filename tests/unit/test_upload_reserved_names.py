"""上传文件名的 Windows 保留设备名守卫（R80 第二十批 → TODO 0-22）。

## 为什么需要

`safe_name = Path(file.filename or "").name` 能挡**路径分隔符**，**挡不住** Windows
**保留设备名**。实测（Python 3.11 / Windows，见 `devlogs/_verify/r80_nul_probe.py`）：

| 上传文件名 | 实际发生 | 用户看到 |
|---|---|---|
| `NUL.pdf` / `CON.pdf` | `open(..., "wb")` **成功**但写入被丢弃（落在设备上，不产生文件）⇒ 回读 header 为空 | 400「文件不是有效的 PDF（缺少 %PDF- 文件头）」（**误导**） |
| `COM1.pdf` / `AUX.pdf` | `open(..., "wb")` 直接抛 `FileNotFoundError` | 500「上传失败（磁盘写入错误）」（**误导**） |

两者都不是真实原因 —— 用户按提示无从下手（正确做法是**改名**）。本护栏把入口改成
**点名拒绝**（400 + 说明），与仓内既有风格一致（多页 TIFF / 加密 PDF 都是明确拒绝
而不是静默处理：「GMP 场景宁缺勿滥」）。

⚠️ 这是**健壮性**问题、**不是**路径穿越：`Path(...).name` 已把分隔符剥掉，
`job_id` 是服务端生成的 uuid ⇒ 落盘路径**不可**由客户端控制。本护栏**不**声称
发现了穿越漏洞。

跑法：`pytest tests/unit/test_upload_reserved_names.py`
"""
from __future__ import annotations

import sys
from io import BytesIO

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.jobs.upload import _is_reserved_device_name

#: (文件名, 是否保留设备名)
_CASES = (
    # ── 保留设备名 ──────────────────────────────────────────────
    ("NUL.pdf", True),
    ("nul", True),
    ("CON.pdf", True),
    ("PRN.pdf", True),
    ("AUX.pdf", True),
    ("COM1.pdf", True),
    ("COM9.pdf", True),
    ("LPT1.pdf", True),
    ("LPT9.pdf", True),
    ("NUL .txt", True),      # 尾部空格被 Windows 忽略
    ("CON..x", True),        # 第一个点之前就是 CON
    ("nul.PDF", True),       # 大小写不敏感
    # ── **不**是设备名（阴性对照：防止实现写成"前缀匹配"而误伤）────
    ("null.pdf", False),
    ("console.pdf", False),
    ("com0.pdf", False),     # Windows 只保留 COM1–9
    ("com10.pdf", False),
    ("lpt0.pdf", False),
    ("printer.pdf", False),
    ("auxiliary.pdf", False),
    ("批记录.pdf", False),
    ("normal.pdf", False),
    ("", False),
    (".", False),
    ("..", False),
)


@pytest.mark.parametrize("name,expected", _CASES)
def test_reserved_device_name_truth_table(name: str, expected: bool):
    assert _is_reserved_device_name(name) is expected, (
        f"_is_reserved_device_name({name!r}) 应为 {expected}"
    )


def test_detector_is_not_vacuous():
    """阳性 + 阴性成对出现 ⇒ 判据有判别力（恒 True/恒 False 都过不了这条）。"""
    assert _is_reserved_device_name("NUL.pdf") is True
    assert _is_reserved_device_name("null.pdf") is False
    assert _is_reserved_device_name("COM1.pdf") is True
    assert _is_reserved_device_name("COM10.pdf") is False


@pytest_asyncio.fixture
async def client(test_db):
    """ASGI 客户端（依赖 `test_db` 隔离库 + mock provider key）。"""
    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        yield c


class TestEndpointRejectsReservedNames:
    """入口守卫：必须 400 **且点名原因**（而不是落到误导性的 400/500）。"""

    @pytest.mark.asyncio
    #: ⚠️ **刻意不含 `CON.pdf` / `PRN.pdf`**：这两个是**控制台/打印机**设备，
    #: 守卫被绕过后 `open(..., "wb")` 会成功，但 `create_job` 紧接着在**事件
    #: 循环线程内同步**回读 `header = f.read(12)` —— 读 `CON` **永不返回**
    #: （实测 `timeout 12` → rc=124）。同步调用 ⇒ 测试层 `wait_for` 也救不回
    #: 来，只会把变异验证从「变红」变成「挂死」。这两个名字的**语义覆盖**由
    #: 纯函数真值表（`test_reserved_device_name_truth_table`）承担（无 I/O）。
    @pytest.mark.parametrize("name", ["NUL.pdf", "COM1.pdf", "AUX.pdf", "LPT1.pdf"])
    async def test_reserved_name_gets_400_naming_the_reason(self, client, name):
        files = {"file": (name, BytesIO(b"%PDF-1.4\n%fake\n"), "application/pdf")}
        r = await client.post("/api/jobs", files=files)
        assert r.status_code == 400, (
            f"{name} 应被 400 拒绝，实际 {r.status_code}：{r.text[:200]}"
        )
        body = r.text
        assert "保留设备名" in body, f"400 未点名原因（{name}）：{body[:200]}"
        assert name in body, f"400 未回显违规文件名（{name}）：{body[:200]}"

    @pytest.mark.asyncio
    async def test_rejection_happens_before_any_disk_write(self, client, test_db):
        """拒绝必须发生在**建目录/写盘之前** —— 否则设备名已经把写入引到设备上了。

        `test_db` 把 `output_dir` 指向一个**尚不存在**的临时目录；入口的
        磁盘余量预检（`_ensure_disk_headroom`）会 `mkdir(parents=True)`。若守卫晚于它，
        目录就会被建出来 ⇒ 用"目录是否出现"当**写入前置**的判据。
        """
        import os

        import config as _config
        out_dir = _config.config["app"].output_dir
        assert not os.path.isdir(out_dir), "前置条件：output_dir 起初不应存在"

        files = {"file": ("NUL.pdf", BytesIO(b"%PDF-1.4\n%fake\n"), "application/pdf")}
        r = await client.post("/api/jobs", files=files)
        assert r.status_code == 400
        assert not os.path.isdir(out_dir), (
            f"被拒的上传仍建出了 output_dir（{out_dir}）⇒ 守卫晚于磁盘动作"
        )

    @pytest.mark.asyncio
    async def test_normal_name_still_accepted(self, client, test_db):
        """阴性对照：正常文件名**不得**被这条守卫误伤。"""
        from unittest.mock import patch

        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "ok")
        buf = doc.tobytes()
        doc.close()

        with patch("api.jobs.launch_pipeline"):
            files = {"file": ("批记录-正常.pdf", BytesIO(buf), "application/pdf")}
            r = await client.post("/api/jobs", files=files)
        assert r.status_code == 200, f"正常文件名被误伤：{r.status_code} {r.text[:200]}"
        assert r.json()["filename"] == "批记录-正常.pdf"


class TestEndpointStripsBeforeChecking:
    """顺序不变式：**先** `Path(...).name` 剥离，**后**判保留设备名。

    `create_job` 里这两步的顺序是**载荷**的：若把判定提到剥离之前，
    `../../NUL.pdf` 的 stem 是 `../../NUL`（∉ 集合）⇒ **绕过守卫** ⇒ 仍写到 `NUL`
    设备（0 字节静默丢失，用户看到误导性的 400）。上面的 24 例真值表只喂**裸名**，
    顺序反了它**全绿** ⇒ 需要**带前缀**的端点用例把顺序钉住。

    实测当前行为（真产物）：`devlogs/_verify/r82_prefixed_reserved_probe.py` ⇒ 9/9。
    """

    #: 跨平台可移植（`/` 在两个平台都是分隔符）
    _PORTABLE = (
        ("../../NUL.pdf", "NUL.pdf"),
        ("/tmp/COM1.pdf", "COM1.pdf"),
        ("a/b/c/../AUX.pdf", "AUX.pdf"),
    )
    #: 反斜杠 / 盘符 / UNC —— 只有 Windows 把它们当分隔符
    _WIN_ONLY = (
        ("..\\..\\NUL.pdf", "NUL.pdf"),
        ("C:\\dir\\NUL.pdf", "NUL.pdf"),
        ("\\\\srv\\s\\LPT1.pdf", "LPT1.pdf"),
    )

    @staticmethod
    def _assert_rejected(r, name: str, echo: str) -> None:
        assert r.status_code == 400, (
            f"{name} 应被 400 拒绝（剥离后是保留设备名），实际 {r.status_code}：{r.text[:200]}"
        )
        assert "保留设备名" in r.text, f"400 未点名原因（{name}）：{r.text[:200]}"
        assert echo in r.text, (
            f"400 未回显**剥离后**的文件名 {echo!r}（{name}）：{r.text[:200]}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,echo", _PORTABLE)
    async def test_prefixed_reserved_name_is_still_rejected(self, client, name, echo):
        """带路径前缀 ⇒ 剥离后是保留设备名 ⇒ 仍须 400 + 点名 + 回显**剥离后**的名字。"""
        files = {"file": (name, BytesIO(b"%PDF-1.4\n%fake\n"), "application/pdf")}
        self._assert_rejected(await client.post("/api/jobs", files=files), name, echo)

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="反斜杠 / 盘符 / UNC 仅在 Windows 上是路径分隔符",
    )
    @pytest.mark.parametrize("name,echo", _WIN_ONLY)
    async def test_windows_style_prefixed_reserved_name_is_still_rejected(
        self, client, name, echo
    ):
        files = {"file": (name, BytesIO(b"%PDF-1.4\n%fake\n"), "application/pdf")}
        self._assert_rejected(await client.post("/api/jobs", files=files), name, echo)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,echo", [
        ("../../null.pdf", "null.pdf"),
        ("/tmp/com10.pdf", "com10.pdf"),
    ])
    async def test_prefixed_non_device_name_is_accepted(
        self, client, test_db, name, echo
    ):
        """阴性对照：带前缀但**不是**设备名 ⇒ 必须 200。

        否则"凡带斜杠就拒"也能过上一条 ⇒ 那条断言没有判别力。
        """
        from unittest.mock import patch

        import fitz
        doc = fitz.open()
        doc.new_page().insert_text((50, 50), "ok")
        buf = doc.tobytes()
        doc.close()

        with patch("api.jobs.launch_pipeline"):
            files = {"file": (name, BytesIO(buf), "application/pdf")}
            r = await client.post("/api/jobs", files=files)
        assert r.status_code == 200, (
            f"带前缀的普通名被误伤：{r.status_code} {r.text[:200]}"
        )
        assert r.json()["filename"] == echo
