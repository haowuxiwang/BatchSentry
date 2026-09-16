# -*- coding: utf-8 -*-
"""e2e driver 的 findings 取回完整性护栏。

背景（2026-09-16 实测）：``/api/jobs/{id}/findings`` 默认 ``limit=50``
（``api/review.py``，钳制上限 200）。driver 原先只取首页，于是：

- 51 页真实文档 **314** 条 / **14** 类，被打印成 "50 findings types={7 类}"；
- ``missing`` 判定基于被截断的分布 → 把**统计截断**误报成产品缺陷；
- 反向更危险：findings 从 314 掉到 60 依然 ``ok``，**护栏形同虚设**。

本文件锁住四件事：
1. :func:`e2e_run.findings_of` 必须翻页取全量（且按**实际返回条数**推进
   offset，兼容服务端把 limit 钳得更小）；
2. :func:`e2e_run.findings_total_of` 读端点声明的 ``total``；取不到时返回
   ``None``（不假定"首页就是全部"）；
3. ``run_upload`` 把"取回不完整"折进 ``ok``（护栏的护栏）；
4. AST 检查：取回请求必须同时带 ``limit`` 与 ``offset`` —— 防止有人退回单页。
"""
import ast
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import e2e_run  # noqa: E402
from e2e_run import findings_of, findings_total_of  # noqa: E402

_DRIVER = _ROOT / "e2e_run.py"


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """模拟分页端点；``server_cap`` 用来模拟服务端把 limit 钳小。"""

    def __init__(self, total, *, server_cap=200, legacy_list=False,
                 fail_at_offset=None, no_total=False):
        self.total = total
        self.server_cap = server_cap
        self.legacy_list = legacy_list
        self.fail_at_offset = fail_at_offset
        self.no_total = no_total
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        offset = params.get("offset", 0)
        self.calls.append(dict(params))
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            return _Resp({}, status_code=500)
        want = min(params.get("limit", 50), self.server_cap)
        n = max(0, min(want, self.total - offset))
        batch = [{"id": offset + i, "type": f"t{(offset + i) % 3}"} for i in range(n)]
        if self.legacy_list:
            return _Resp(batch)
        payload = {"findings": batch, "count": len(batch), "page": None,
                   "limit": want, "offset": offset}
        if not self.no_total:
            payload["total"] = self.total
        return _Resp(payload)


# ── 翻页取全量 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("total,server_cap", [(0, 200), (1, 200), (50, 200),
                                              (200, 200), (314, 200), (314, 50)])
def test_finds_every_finding(total, server_cap):
    """无论服务端把 limit 钳到多少，都必须取满 total 条。"""
    c = _FakeClient(total, server_cap=server_cap)
    got = findings_of(c, "job-1")
    assert len(got) == total
    assert [f["id"] for f in got] == list(range(total))  # 无重复、无缺页


def test_advances_by_actual_batch_not_requested_limit():
    """服务端钳小 limit 时，offset 必须按**实际返回条数**推进。

    若按请求的 200 推进，会在 50 条的批次上跳页 → 静默漏 150 条。
    """
    c = _FakeClient(314, server_cap=50)
    got = findings_of(c, "job-1")
    assert len(got) == 314
    assert [p["offset"] for p in c.calls] == [0, 50, 100, 150, 200, 250, 300]


def test_stops_after_last_page():
    """不得多打无意义的空页请求（total 已知时提前收敛）。"""
    c = _FakeClient(200, server_cap=200)
    findings_of(c, "job-1")
    assert len(c.calls) == 1  # 一页刚好 200 条且 offset>=total → 停


def test_empties_are_not_infinite():
    c = _FakeClient(0)
    assert findings_of(c, "job-1") == []
    assert len(c.calls) == 1


# ── 韧性：不因局部失败而丢干净结果，也不无限翻页 ────────────────────


def test_partial_http_failure_returns_what_it_got():
    c = _FakeClient(314, fail_at_offset=200)
    got = findings_of(c, "job-1")
    assert len(got) == 200  # 第一批成功


def test_first_request_failure_returns_empty():
    c = _FakeClient(314, fail_at_offset=0)
    assert findings_of(c, "job-1") == []


def test_stops_at_page_cap_when_total_missing():
    """端点不给 total 时必须靠页数上限收敛（否则死循环）。"""
    c = _FakeClient(10 ** 7, no_total=True)
    got = findings_of(c, "job-1")
    assert len(c.calls) == e2e_run._FINDINGS_MAX_PAGES
    assert len(got) == e2e_run._FINDINGS_MAX_PAGES * e2e_run._FINDINGS_PAGE_LIMIT


def test_legacy_list_shape_still_supported():
    """无分页的旧形态（裸 list）不得报错。"""
    c = _FakeClient(7, legacy_list=True)
    assert len(findings_of(c, "job-1")) == 7


# ── total 读取 ──────────────────────────────────────────────────────


def test_total_is_read_from_payload():
    assert findings_total_of(_FakeClient(314), "j") == 314


def test_total_absent_is_none_not_the_page_size():
    """端点没声明 total → 返回 None（**不得**假定首页条数就是总数）。"""
    assert findings_total_of(_FakeClient(314, no_total=True), "j") is None


def test_total_absent_on_legacy_list():
    assert findings_total_of(_FakeClient(314, legacy_list=True), "j") is None


def test_total_none_on_http_error():
    assert findings_total_of(_FakeClient(314, fail_at_offset=0), "j") is None


# ── "护栏的护栏"：AST 级联检查 ──────────────────────────────────────


def _driver_tree():
    return ast.parse(_DRIVER.read_text(encoding="utf-8"))


def _call_nodes(name):
    out = []
    for node in ast.walk(_driver_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == name:
            out.append(node)
    return out


def _method_call_nodes(method):
    """匹配 ``obj.method(...)`` 形态（``func`` 是 Attribute 而非 Name）。"""
    out = []
    for node in ast.walk(_driver_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == method:
            out.append(node)
    return out


def test_findings_request_passes_limit_and_offset():
    """取回请求必须同时带 limit 与 offset —— 否则退回单页（本缺陷原形）。"""
    paged = []
    for node in _method_call_nodes("get"):
        for kw in node.keywords:
            if kw.arg == "params" and isinstance(kw.value, ast.Dict):
                keys = {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
                if {"limit", "offset"} <= keys:
                    paged.append(node)
    assert paged, "findings_of 的 GET 未同时传 limit+offset（会退回只取首页）"


def test_run_upload_folds_truncation_into_ok():
    """取回不完整必须让本轮判失败，且把声明总数写进结果。"""
    src = _DRIVER.read_text(encoding="utf-8")
    assert "findings_declared_total" in src, "结果里必须留声明总数（可事后对账）"
    assert "truncated" in src
    # ok 表达式必须引用 truncated
    for node in ast.walk(_driver_tree()):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "ok" in targets:
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if "truncated" in names:
                    return
    pytest.fail("run_upload 的 ok 未把 truncated 计入（截断会静默通过）")


def test_findings_of_is_called_in_run_upload():
    """保证护栏挂在真实调用路径上，而不是写了没人用。"""
    assert _call_nodes("findings_of"), "driver 里没有任何 findings_of 调用点"


# ── "观测不得被前置失败吞掉" ────────────────────────────────────────


def _func_node(name):
    for node in ast.walk(_driver_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"e2e_run.py 里找不到 {name}()")


def test_run_rot_measures_even_when_precondition_failed():
    """``res.ok`` 为假时不得短路掉旋转测量。

    2026-09-16 实测：paddle→mineru failover 让 ``run_upload`` 判失败，原先的
    ``if not res.get("ok"): return res`` 于是把整段旋转测量一起吞掉 —— 摘要里
    连 ``rot_lost`` 都没有，读者会以为"旋转没问题"。观测与断言必须分离。
    """
    fn = _func_node("run_rot")
    src_seg = ast.get_source_segment(_DRIVER.read_text(encoding="utf-8"), fn)
    assert "rot_measured" in src_seg, "run_rot 必须显式记录'旋转是否被测过'"
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            consts = {n.value for n in ast.walk(node.test)
                      if isinstance(n, ast.Constant)}
            if "ok" in consts and any(isinstance(s, ast.Return) for s in node.body):
                pytest.fail("run_rot 又用 res.ok 短路了（会吞掉旋转测量）")


def test_rot_ok_still_gated_by_rotation_result():
    """分离观测≠放宽断言：ok 仍须同时要求没丢内容、对照页可见、审计可追溯。"""
    fn = _func_node("run_rot")
    src_seg = ast.get_source_segment(_DRIVER.read_text(encoding="utf-8"), fn)
    assert "and not lost" in src_seg and "p1_ok" in src_seg, \
        "run_rot 的 ok 断言被削弱了（丢内容/对照页必须仍然判失败）"
