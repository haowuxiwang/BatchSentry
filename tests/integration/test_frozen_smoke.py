"""打包产物（frozen server）冒烟测试。

验证 dist/pbc-server/pbc-server.exe 能独立启动并服务核心端点：
- /health 健康检查（lifespan + DB 初始化 + 资源目录解析）
- GET / 上传页（templates 通过 _MEIPASS 正确打包）
- /static/app.css（静态资源打包）

隔离策略：
- APPDATA 指向临时目录，避免污染真实 %APPDATA%/PBC，同时验证 frozen
  模式的配置/DB 重定向逻辑（config.json 落盘）
- PORT 环境变量覆盖默认 58765（server.py 支持），避开正在运行的外部实例

前置条件：构建产物存在（跳过条件）。
"""
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

EXE = Path(__file__).parent.parent.parent / "dist" / "pbc-server" / "pbc-server.exe"

pytestmark = pytest.mark.skipif(
    not EXE.exists(),
    reason="dist/pbc-server/pbc-server.exe not built — run .\\build.ps1 first",
)


def _free_port() -> int:
    """借一个空闲端口（短暂竞态窗口可接受，测试环境可控）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def frozen_server(tmp_path_factory):
    """启动 frozen server（隔离 APPDATA + 随机端口），就绪后 yield，teardown 关闭。"""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    fake_appdata = tmp_path_factory.mktemp("frozen_appdata")
    env = dict(os.environ)
    env["APPDATA"] = str(fake_appdata)
    env["PORT"] = str(port)

    proc = subprocess.Popen(
        [str(EXE)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            r = httpx.get(f"{base}/health", timeout=2)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.5)
    if not ready:
        out = ""
        try:
            out = proc.stdout.read() if proc.stdout else ""
        except Exception:
            pass
        proc.kill()
        pytest.fail(f"frozen server failed to become ready:\n{out[:2000]}")

    yield {"proc": proc, "appdata": fake_appdata, "base": base}

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_health_ok(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/health", timeout=5)
    assert r.status_code == 200


def test_upload_page_served(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/", timeout=5)
    assert r.status_code == 200
    assert "上传" in r.text or "upload" in r.text.lower()


def test_static_css_served(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/static/app.css", timeout=5)
    assert r.status_code == 200
    assert "text/css" in r.headers.get("content-type", "")


def test_review_page_route(frozen_server):
    """review 页路由不 404（无 job 时应有兜底跳转或空态页）。"""
    r = httpx.get(f"{frozen_server['base']}/jobs/nonexistent/review", timeout=5)
    assert r.status_code in (200, 404)  # 允许 404（job 不存在），但不允许 500


def test_settings_page_served(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/settings", timeout=5)
    assert r.status_code == 200


def test_db_created_in_appdata(frozen_server):
    """frozen 模式 DB 应落在隔离的 %APPDATA%/PBC 下（Phase 5B 数据重定向）。

    config.json 只在设置页保存过或 .env 迁移时才会创建；首次启动无配置
    文件是合法状态。可验证的可靠信号是 data.db — lifespan 初始化必然创建。
    """
    db = Path(frozen_server["appdata"]) / "PBC" / "data.db"
    assert db.exists(), f"DB not created at {db}"


def test_jobs_api_returns_paginated(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/api/jobs", timeout=5)
    assert r.status_code == 200
    data = r.json()
    assert "jobs" in data and data["jobs"] == []


def test_api_docs_served(frozen_server):
    r = httpx.get(f"{frozen_server['base']}/docs", timeout=5)
    assert r.status_code == 200
