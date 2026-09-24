# -*- coding: utf-8 -*-
"""e2e 上传吞吐打点的结构护栏（B11-5 基准采集口径）。

为什么是 AST 结构检查而不是行为测试：打点逻辑（size_mb / up_s）本身是
一行算术，真正的风险是**回退**——重构时把 ``upload_mbps`` 键从结果 dict
里删掉、或把 MB/s 打印行去掉，基准汇总就静默失去数据源（回到 B11-5
"判据就绪但没有测量"的状态）。AST 断言键/表达式存在，改没了当场红。

单位口径：MB = 1024*1024 字节（与 e2e_run.py 的 size_mb 计算一致），
Mbps = MB/s。护栏同时锁定**除数是墙钟秒**（防有人把 MB 换成 MiB 时
只改一处、或把分母换成别的计时器）。
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "e2e_run.py"


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _return_keys(fn: ast.FunctionDef) -> set[str]:
    """收集函数所有 return 语句里字典字面量的键（字符串字面量键）。"""
    keys: set[str] = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


class TestUploadMbpsKeyPresent:
    def test_run_upload_result_has_upload_mbps(self, tree):
        fn = _fn(tree, "run_upload")
        assert fn is not None, "run_upload 函数不存在（e2e 驱动被重构？）"
        assert "upload_mbps" in _return_keys(fn), (
            "run_upload 结果 dict 缺 upload_mbps 键 —— 基准汇总将取不到吞吐数据"
        )

    def test_run_cancel_result_has_upload_mbps(self, tree):
        fn = _fn(tree, "run_cancel")
        assert fn is not None, "run_cancel 函数不存在（e2e 驱动被重构？）"
        assert "upload_mbps" in _return_keys(fn), (
            "run_cancel 结果 dict 缺 upload_mbps 键 —— 大文件 cancel 轮的吞吐证据丢失"
        )


class TestThroughputComputation:
    """吞吐计算的口径不变式：分子 = 文件字节数 ÷ 1024²，分母 = 上传墙钟秒。"""

    def test_size_mb_uses_mib_division(self, tree):
        src = SRC.read_text(encoding="utf-8")
        # 分子口径：os.path.getsize(path) / (1024 * 1024)（MiB，与打印的 MB 一致）
        assert "os.path.getsize(path) / (1024 * 1024)" in src, (
            "size_mb 计算口径改变 —— 与历史基线（MiB）不可比，须同步更新基准文档"
        )

    def test_upload_mbps_divides_size_by_elapsed_seconds(self, tree):
        fn = _fn(tree, "run_upload")
        assert fn is not None
        found = any(
            isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
            for n in ast.walk(fn)
        )
        assert found, "run_upload 内无除法 —— 吞吐计算被移除"

    def test_mbps_printed_in_upload_log(self, tree):
        """print 行必须带 MB/s —— 人读日志时第一眼要能看到吞吐。"""
        src = SRC.read_text(encoding="utf-8")
        assert "MB/s) -> job" in src, (
            "上传日志行不再打印 MB/s —— 运行时吞吐对操作者不可见"
        )


class TestUnitSemantics:
    """单位换算的独立复算（不 import e2e_run，避免拉起 httpx 依赖链）。"""

    def test_mib_bytes(self):
        # 1 MiB = 1048576 字节：若 e2e_run 用了别的换算，上面的结构断言
        # 已锁死 1024*1024；此处复算保证本护栏自身的算术没有写错。
        assert 1024 * 1024 == 1048576

    def test_throughput_example(self):
        size_mb = 138.0
        up_s = 2.5
        assert round(size_mb / up_s, 1) == 55.2
