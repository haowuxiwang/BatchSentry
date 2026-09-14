"""M7 端到端验证：docling 缺失时主链不受影响（真实 uvicorn + 真实 HTTP）。

M7 的验收核心是「**缺失即降级**」——docling 是可选依赖，未安装时：
  1. 进程能正常导入/启动（无导入期硬依赖）；
  2. `_get_ocr_chain()` 不纳入 docling，主链回退默认 PaddleOCR；
  3. `/api/health/downstream` 如实报告 docling 未安装（200，不是 500）；
  4. 三引擎对比脚本优雅跳过并返回非 0（不抛栈）。

隔离：与 tests/e2e_m6_sse.py 同款 —— 临时 cwd 启动（config.json 的
DATABASE_PATH 会**覆盖**环境变量，见该脚本 docstring），模板/静态走
`__file__` 不受影响。

用法：python tests/e2e_m7_docling_absent.py
退出码：0 全过 / 1 有失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.e2e_proc import spawn_server, stop_server  # noqa: E402

PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"

RESULTS: list[tuple[str, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    RESULTS.append(("PASS", name, detail))
    print(f"  OK  {name} {detail}")


def fail(name: str, detail: str = "") -> None:
    RESULTS.append(("FAIL", name, detail))
    print(f"  XX  {name} {detail}")


def _env(tmp: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PBC_NO_FILE_LOG"] = "1"
    env["OCR_BACKEND"] = "docling"  # 故意选不可用的可选后端
    for k in ("DATABASE_PATH", "OUTPUT_DIR"):
        env.pop(k, None)
    return env


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pbc_e2e_m7_"))
    env = _env(tmp)
    print(f"[e2e-m7] tmp={tmp}  OCR_BACKEND=docling")

    # 前置：确认真实环境里 docling 不可用（否则本 e2e 前提不成立）
    probe = subprocess.run(
        [sys.executable, "-c",
         "from core import docling_client; print(docling_client.is_available())"],
        cwd=str(tmp), env=env, capture_output=True, text=True,
    )
    installed = probe.stdout.strip() == "True"
    if installed:
        print("  [skip] 本机已安装 docling —— 缺失降级场景不适用，仅验证链路构建")
    else:
        ok("precondition_docling_absent", "docling 未安装（缺失场景成立）")

    # 1) 链级降级：不启动服务，直接问内部链
    chain_code = (
        "from core.pipeline.ocr_support import _get_ocr_chain;"
        "print([n for _, n in _get_ocr_chain()])"
    )
    r = subprocess.run(
        [sys.executable, "-c", chain_code], cwd=str(tmp), env=env,
        capture_output=True, text=True,
    )
    chain_txt = r.stdout.strip()
    print(f"\n=== 链级降级（OCR_BACKEND=docling 未装）===\n  chain = {chain_txt}")
    if r.returncode != 0:
        fail("chain_import", f"子进程失败 rc={r.returncode}: {r.stderr[-200:]}")
    elif "docling" not in chain_txt and "paddle" in chain_txt:
        ok("chain_degraded", chain_txt)
    else:
        fail("chain_degraded", f"未按预期降级：{chain_txt}")

    # 2) 服务启动不受影响（无导入期硬依赖）
    # stdout 落日志文件（未排空的 PIPE 会把服务阻塞在 write，见 tests/e2e_proc.py）
    proc, _logf = spawn_server(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(tmp), env=env,
        log_path=tmp / "e2e-m7-server.log",
    )
    try:
        up = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
        if not up:
            fail("server_up", "uvicorn 未在 20s 内就绪（docling 缺失影响了启动？）")
            return 1
        ok("server_up", BASE)

        # 3) 下游健康探针如实报告（200 + docling 未安装），而非 500
        hr = requests.get(f"{BASE}/api/health/downstream", timeout=10)
        if hr.status_code != 200:
            fail("health_downstream_status", f"HTTP {hr.status_code}")
        else:
            body = hr.json()
            print(f"\n=== /api/health/downstream ===\n  {body}")
            if body.get("ocr_backend") == "docling":
                ok("health_backend_reported", "docling")
            else:
                fail("health_backend_reported", str(body.get("ocr_backend")))
            ocr = body.get("ocr") or {}
            if installed or ocr.get("ok") is False:
                ok("health_docling_honest", f"ok={ocr.get('ok')} reason={ocr.get('reason')}")
            else:
                fail("health_docling_honest", str(ocr))

        # 4) 三引擎对比脚本：docling 缺失 → 优雅跳过 + 退出码 1（不抛栈）
        pdf = tmp / "probe.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        cr = subprocess.run(
            [sys.executable, "scripts/compare_ocr_engines.py", str(pdf),
             "--engines", "docling"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        if cr.returncode == 1 and "跳过" in cr.stdout and "Traceback" not in cr.stderr:
            ok("compare_script_graceful", "exit=1 且无栈")
        else:
            fail("compare_script_graceful",
                 f"rc={cr.returncode} out={cr.stdout.strip()[-120:]}")
    finally:
        stop_server(proc, _logf, timeout=5)

    passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    print("\n" + "=" * 54)
    print(f"[e2e-m7] Total: {passed} passed, {failed} failed")
    for s, name, detail in RESULTS:
        if s == "FAIL":
            print(f"  XX {name}: {detail}")
    print("=" * 54)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
