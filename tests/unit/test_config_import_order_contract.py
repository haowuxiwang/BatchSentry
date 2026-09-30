"""`import config` 的**顺序不变式**护栏（`server.py` 注释约束的落地）。

背景（对抗性审查 · 遗留清单第 3 条）
--------------------------------------
`config.py` 有两个模块级调用点，它们让 `import config` 具备副作用：

  · line 258  `_load_json_config()`   —— 读 config.json → 写 os.environ
  · line 951  `config = load_config()` —— 可能经 `_resolve_active_provider`
                                          → `_persist_env_to_config` 写盘

实测（`devlogs/_verify/probe_config_import_io.py`）：

  | 场景 | 导入期写盘 | 改 os.environ |
  |---|---|---|
  | A 干净 | 否 | 否 |
  | B 有旧 .env | **创建 config.json** | 是 |
  | C 需自动切换 provider | **重写 config.json** | 是 |

"导入期写盘"是**被测试锁定的设计契约**（`test_config_internals.py:282` 明确断言
`load_config()` 调用 `_persist_env_to_config`），因此本轮**不改**它。

但它带来一条硬约束：**`PORT` 必须在 `import config` 之前进入 os.environ**。
`AppConfig.port` 是导入期快照（`_env_int("PORT", _env_int("APP_PORT", 8000))`），
而 `main.py` 的 CORS 白名单在导入期由 `config["app"].port` 生成。顺序错了就是
**监听端口与白名单不一致** ⇒ 浏览器访问设置页的 POST/PUT 被 CORS preflight 拦掉
（`server.py:32-37` 描述的正是这个故障）。

这条约束此前**只写在注释里、零测试**。本文件把它变成护栏：
  ① 行为化：证明"顺序真的有牙"（before → 59999；after → 8000）；
  ② 源码顺序：`server.py` 里 setdefault(PORT) 必须**先于** `from config import config`；
  ③ 派生关系：`main.py` 的 CORS 白名单必须**派生自** `config["app"].port`，
     而不是硬编码端口字面量；
  ④ 单 worker 前提（报告 §3.6）：`uvicorn.Config(workers=1)` 是**正确性前提**
     而非性能取舍 —— 见下方 `TestSingleWorkerPrerequisite` 的说明。

⚠️ 源码类断言一律**锚定调用点**（`from config import config` 语句本身），
不锚标识符 —— `server.py:32-37` 的注释里就含 "import config" 字样，
锚错即假绿。同理 ④ 锚的是 `uvicorn.Config(...)` 的实参文本，不是裸的 "workers"。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVER_PY = REPO / "server.py"
MAIN_PY = REPO / "main.py"

# 子进程载荷：通过 env 传参，避免 shell/引号转义问题（本项目踩过多次）
_PAYLOAD = r'''
import json, os, sys
repo = os.environ["PBC_TEST_REPO"]
sandbox = os.environ["PBC_TEST_SANDBOX"]
sys.path.insert(0, repo)
os.chdir(sandbox)

order = os.environ["PBC_TEST_ORDER"]
if order == "before":
    os.environ["PORT"] = os.environ["PBC_TEST_PORT"]

import config as C

if order == "after":
    os.environ["PORT"] = os.environ["PBC_TEST_PORT"]

out = {
    "config_port": C.config["app"].port,
    "env_port": os.environ["PORT"],
    "import_main_ok": False,
}
if os.environ.get("PBC_TEST_IMPORT_MAIN") == "1":
    import main
    out["cors_origins"] = main._cors_origins()
    out["import_main_ok"] = True
print("__R__" + json.dumps(out))
'''


def _run_probe(tmp_path, order: str, port: str = "59999",
               import_main: bool = False) -> dict:
    """在**独立子进程 + 隔离 CWD/APPDATA** 里跑一次导入，返回载荷结果。

    独立子进程是必须的：`config` 一旦导入就进 `sys.modules` 缓存，
    同进程内无法再观察"导入顺序"的差别。
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        PBC_TEST_REPO=str(REPO),
        PBC_TEST_SANDBOX=str(sandbox),
        PBC_TEST_ORDER=order,
        PBC_TEST_PORT=port,
        PBC_TEST_IMPORT_MAIN="1" if import_main else "0",
        APPDATA=str(tmp_path / "appdata"),   # 冻结模式落点，双保险隔离
    )
    for key in ("LLM_PROVIDER", "LLM_PROVIDERS", "GLM_API_KEY",
                "DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY"):
        env.pop(key, None)

    proc = subprocess.run([sys.executable, "-c", _PAYLOAD], capture_output=True,
                          text=True, env=env, cwd=str(sandbox), timeout=180)
    for line in proc.stdout.splitlines():
        if line.startswith("__R__"):
            return json.loads(line[len("__R__"):])
    raise AssertionError(
        "探针未产出结果 —— 载荷本身坏了（这不是被测行为的问题）\n"
        f"rc={proc.returncode}\nstdout tail={proc.stdout[-800:]}\n"
        f"stderr tail={proc.stderr[-1500:]}"
    )


def _read_source(path: Path) -> str:
    """读源码并归一化换行（工作区是 CRLF，多行锚点不归一化会静默失配）。"""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


# ══════════════════════════════════════════════════════════════
# 0. 防空转：探针与提取器本身必须有效
# ══════════════════════════════════════════════════════════════
class TestHarnessIsNotVacuous:
    def test_probe_returns_expected_keys(self, tmp_path):
        """探针能真的跑起来并回传结果 —— 否则下面全是空断言。"""
        r = _run_probe(tmp_path, "before")
        assert set(r) >= {"config_port", "env_port"}
        assert isinstance(r["config_port"], int)

    def test_probe_observes_env_port(self, tmp_path):
        """探针确实把 PORT 注入了环境 —— 否则"顺序"根本无从谈起。"""
        r = _run_probe(tmp_path, "before")
        assert r["env_port"] == "59999"

    def test_server_source_is_readable_and_has_the_import(self):
        """`server.py` 里确实存在那条 import 语句（锚点有效性）。"""
        src = _read_source(SERVER_PY)
        assert re.search(r"^\s*from config import config\b", src, re.M), \
            "锚点失配：server.py 里找不到 `from config import config` 语句"

    def test_cors_function_is_extractable(self):
        """`_cors_origins` 能被提取出来 —— 否则源码断言是空断言。"""
        body = _cors_origins_body()
        assert body.strip(), "_cors_origins 提取为空"


# ══════════════════════════════════════════════════════════════
# 1. 行为化：顺序真的有牙（config["app"].port 在导入期冻结）
# ══════════════════════════════════════════════════════════════
class TestPortIsFrozenAtImport:
    def test_port_before_import_is_picked_up(self, tmp_path):
        """正确顺序：PORT 先入 env ⇒ config 读到它。"""
        r = _run_probe(tmp_path, "before", port="59999")
        assert r["config_port"] == 59999

    def test_port_after_import_is_ignored(self, tmp_path):
        """错误顺序：import 之后再设 PORT ⇒ config 仍持默认 8000。

        这条**就是**那条注释约束的证据：顺序错了，端口快照就错了。
        """
        r = _run_probe(tmp_path, "after", port="59999")
        assert r["env_port"] == "59999", "env 确实被改了"
        assert r["config_port"] == 8000, (
            "config 端口应在导入期冻结 —— 若此处变成 59999，"
            "说明 port 改成了调用期读取，本文件的顺序护栏需重新评估"
        )

    def test_cors_follows_the_frozen_port_not_the_env(self, tmp_path):
        """CORS 白名单跟着**冻结值**走 —— 顺序错了白名单就错。

        这是端到端的故障复现：监听 59999 但白名单是 8000。
        """
        before = _run_probe(tmp_path / "b", "before", port="59999",
                            import_main=True)
        after = _run_probe(tmp_path / "a", "after", port="59999",
                           import_main=True)
        assert before["import_main_ok"], "main 未成功导入，CORS 断言无效"
        assert before["cors_origins"] == ["http://127.0.0.1:59999",
                                          "http://localhost:59999"]
        assert after["cors_origins"] == ["http://127.0.0.1:8000",
                                         "http://localhost:8000"], (
            "顺序错时应复现故障：白名单停在 8000，与监听端口 59999 不一致"
        )


# ══════════════════════════════════════════════════════════════
# 2. 源码顺序：server.py 必须先同步 PORT，再导入 config
# ══════════════════════════════════════════════════════════════
class TestServerSyncsPortBeforeImportingConfig:
    def test_setdefault_port_precedes_config_import(self):
        src = _read_source(SERVER_PY)
        m_call = re.search(r'os\.environ\.setdefault\(\s*"PORT"', src)
        m_import = re.search(r"^\s*from config import config\b", src, re.M)
        assert m_call, "server.py 里找不到 os.environ.setdefault(\"PORT\", ...)"
        assert m_import, "server.py 里找不到 from config import config"
        assert m_call.start() < m_import.start(), (
            "顺序不变式被破坏：`os.environ.setdefault(\"PORT\", ...)` 必须出现在 "
            "`from config import config` **之前**。否则 config 按默认 8000 快照端口，"
            "而 uvicorn 监听 58765 ⇒ CORS 白名单与监听端口不一致，"
            "浏览器访问设置页的 POST/PUT 会被 preflight 拦截。"
        )

    def test_port_value_comes_from_env_not_a_literal(self):
        """server.py 的端口取自 PORT env（默认 58765）—— 与 config 同源。"""
        src = _read_source(SERVER_PY)
        assert re.search(r'os\.getenv\(\s*"PORT"\s*,\s*"58765"\s*\)', src), (
            "server.py 的端口应读 PORT env（默认 58765），"
            "否则与 config.py 的端口派生逻辑脱节"
        )


# ══════════════════════════════════════════════════════════════
# 3. 派生关系：CORS 白名单由 config["app"].port 生成，不硬编码
# ══════════════════════════════════════════════════════════════
def _cors_origins_body() -> str:
    """提取 `main.py::_cors_origins` 的函数体（到下一个顶层 def/装饰器为止）。"""
    src = _read_source(MAIN_PY)
    m = re.search(r"^def _cors_origins\(.*?\n(.*?)(?=\n(?:def |@|app\.)|\Z)",
                  src, re.M | re.S)
    return m.group(1) if m else ""


class TestCorsDerivesFromConfigPort:
    def test_body_reads_config_port(self):
        body = _cors_origins_body()
        assert 'config["app"].port' in body, (
            "_cors_origins 必须从 config[\"app\"].port 派生端口；"
            "硬编码端口会在换端口时与监听端口脱节"
        )

    def test_body_has_no_hardcoded_port_literal(self):
        body = _cors_origins_body()
        for literal in ("8000", "58765"):
            assert literal not in body, (
                f"_cors_origins 里出现了硬编码端口 {literal} —— "
                "端口必须由 config[\"app\"].port 单一来源派生"
            )

    def test_middleware_uses_the_derived_origins(self):
        src = _read_source(MAIN_PY)
        assert "allow_origins=_cors_origins()" in src, (
            "CORSMiddleware 必须消费 _cors_origins() 的返回值；"
            "若改成字面量列表，端口派生关系即失效"
        )


# ══════════════════════════════════════════════════════════════
# 4. 单 worker 前提：uvicorn 必须 workers=1（报告 §3.6 的隐性耦合点）
# ══════════════════════════════════════════════════════════════
def _uvicorn_config_args() -> str:
    """提取 `uvicorn.Config(...)` 的实参文本（括号配平扫描，不靠正则贪心）。"""
    src = _read_source(SERVER_PY)
    i = src.find("uvicorn.Config(")
    if i == -1:
        return ""
    start = i + len("uvicorn.Config(")
    depth = 0
    for k in range(start - 1, len(src)):
        ch = src[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[start:k]
    return ""


class TestSingleWorkerPrerequisite:
    """`workers=1` 不是性能取舍，是**正确性前提**。

    本进程的并发模型全部建立在「单进程内全局单例」上：

      · `core/pipeline/locks.py` 的 `db_lock`（进程内全局单锁）
      · `core/procpool.py` 的 `_pool` / `_POOL_MAX_WORKERS = 2`（进程内单池）
      · job 配额计数、pipeline task 注册表都在进程内存里

    多 worker ⇒ 每个进程各有一把锁、各有一个池 ⇒ 配额**按 worker 数翻倍**
    （"与 INSERT 同一把 `db_lock` 内的权威检查"不再权威）、CPU 池上限变 2×N、
    且跨 worker 的取消与级联取消全部失效。

    这条前提此前**只写在 `server.py` 的注释里**（本轮补的），故在此加护栏。
    ⚠️ 源码断言一律锚定 `uvicorn.Config(...)` 的**实参文本**，不锚裸标识符 ——
    `server.py` 的注释里就出现了 "workers" 字样，锚错即假绿。
    """

    def test_uvicorn_config_args_are_extractable(self):
        """防空转：实参文本必须真的抽到，否则下面两条是空断言。"""
        args = _uvicorn_config_args()
        assert args.strip(), "抽不到 uvicorn.Config(...) 的实参 —— 提取器坏了或调用形状变了"
        assert "workers" in args, "抽到的实参里没有 workers —— 锚点失配"

    def test_uvicorn_config_pins_exactly_one_worker(self):
        args = _uvicorn_config_args()
        assert re.search(r"^\s*workers\s*=\s*1\s*,\s*$", args, re.M), (
            "`uvicorn.Config` 必须显式 `workers=1`。多 worker 会让 `db_lock` 与"
            "进程内单例池各进程一份 ⇒ 并发配额按 worker 数翻倍、跨 worker 取消失效。"
            "要改这个数，必须先换掉「单连接 + 进程内单例」模型。"
        )

    def test_no_other_worker_count_appears_in_server(self):
        """整个 `server.py` 里 `workers=` 只许出现 1 这个值。"""
        src = _read_source(SERVER_PY)
        found = re.findall(r"workers\s*=\s*(\d+)", src)
        assert found == ["1"], f"server.py 里出现了别的 worker 数：{found}"

    def test_the_singletons_it_protects_are_module_level(self):
        """把注释里的论据变成**可验证的**：那两个单例确实是模块级定义。

        若将来有人把它们改成"每 job 一份"或搬进函数，本用例会变红 ——
        提醒"单 worker 前提"的论据已经变了（而不是静默失效）。
        """
        locks = _read_source(REPO / "core" / "pipeline" / "locks.py")
        assert re.search(r"^db_lock\s*=\s*asyncio\.Lock\(\)", locks, re.M), (
            "core/pipeline/locks.py 的 db_lock 不再是模块级 asyncio.Lock —— "
            "单 worker 前提的论据变了，请重新评估本类"
        )
        proc = _read_source(REPO / "core" / "procpool.py")
        assert re.search(r"^_POOL_MAX_WORKERS\s*=\s*2\b", proc, re.M), (
            "core/procpool.py 的 _POOL_MAX_WORKERS 不再是模块级常量 2"
        )
        assert re.search(r"^_pool\s*:\s*ProcessPoolExecutor\s*\|\s*None\s*=", proc, re.M), (
            "core/procpool.py 的 _pool 不再是模块级单例"
        )
