"""上游「容量/拥塞类」错误的分类与处置（缺陷 #120，2026-09-16）。

现场证据（job 234d3838-29d，v1.1.3 产物）：
    11:36:04→11:37:05 五次旋转探测全部被 Paddle `HTTP 400 code:10010
    任务提交队列已满` 拒绝，间隔仅 7–15s（≈15s/角度），**61s 内耗尽全部
    角度尝试**；而实测拥塞窗口是**分钟级**（同日 ≥18 分钟）。
→ 旋转通道在最需要它的场景（上游降级造成空页，正是它被设计的场景）
  放弃得最快，内容永久丢失；且 rotation_probed=True 把上游繁忙写成了
  「已探测全部角度未果」的内容结论。

修复：错误分类**单一真值** + 拥塞退避重试**同一角度**（不消耗有限的角度
预算）+ 预算耗尽时落**区分性**诊断 rotation_blocked。

注：本文件里的"字面量只许出现在一处"类检查一律走 **AST**，而不是对源码
做正则 —— 正则会把**文档字符串/注释里引用该字面量**的说明文字也算作重复
定义（第一版就这么误报了 3 条）。
"""
import ast
import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ocr_client import OCRCancelled, is_congestion_error
from core.pipeline import self_heal as sh

_SRC_ROOT = Path(__file__).resolve().parents[2]

# 真实出错形态（取自现场日志与各客户端的构造方式）
_QUEUE_FULL_MSG = (
    "提交失败 HTTP 400: {\"code\":10010,\"msg\":\"任务提交队列已满\"}"
)
_POLL_TIMEOUT_MSG = "轮询超时: 630s 内未收到 OCR 结果"
_HARD_400_MSG = "提交失败 HTTP 400: invalid model name"


class _Congestion(Exception):
    """可控的拥塞类异常（消息命中分类器）。"""


class _FakeDB:
    """只服务 _rotation_heal 自身的 UPDATE/commit（进度与审计已 patch 掉）。"""

    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []
        self.commits = 0

    async def execute(self, sql, params=()):
        self.sql.append((" ".join(str(sql).split()), tuple(params)))
        return MagicMock()

    async def commit(self):
        self.commits += 1

    def diag_updates(self) -> list[dict]:
        """抽出写 ocr_diagnostics 的载荷（解析 JSON，断言**结构**而非字符）。"""
        out = []
        for sql, params in self.sql:
            if "SET ocr_diagnostics = ?" in sql and len(params) >= 2:
                raw = params[0]
                if raw:
                    out.append(json.loads(raw))
        return out


def _code_string_literals(path: Path) -> list[str]:
    """文件里**真正的字符串字面量**（排除文档字符串）。

    注释根本不是 AST 节点，故天然被排除；文档字符串显式排除 ——
    「某某标记已收敛到 X」这类说明文字不该被判为重复定义。

    读取用 ``utf-8-sig``：本仓库部分文件带 UTF-8 **BOM**
    （``api/jobs/actions.py`` 就是），普通 ``utf-8`` 解出的首字符是
    ``U+FEFF``，``ast.parse`` 直接抛 ``SyntaxError`` —— 第一版就这么
    让 5 条用例集体报错。解析失败一律**跳过该文件**（宁可漏检一个文件，
    也不能让护栏自身崩溃而掩盖真实回归）。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            ds = ast.get_docstring(node, clean=False)
            if ds is not None:
                docstrings.add(ds)
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    ]


# ─── 一、分类器是单一真值 ────────────────────────────────────────────────

class TestCongestionClassifier:
    @pytest.mark.parametrize("msg,expected", [
        (_QUEUE_FULL_MSG, True),                       # Paddle 队列满（现场原形）
        ("提交失败 HTTP 400: code 10010", True),
        ("提交失败 HTTP 429: too many requests", True),
        ("提交失败 HTTP 503: service unavailable", True),
        ("提交失败 HTTP 500: internal error", True),
        ("parsing failed, please try again later", True),   # MinerU
        ("提交失败 HTTP 400: invalid model name", False),   # 永久类
        ("找不到文件 /tmp/x.pdf", False),
        (_POLL_TIMEOUT_MSG, False),                    # 超时**刻意**不算拥塞
    ])
    def test_classification(self, msg, expected):
        assert is_congestion_error(msg) is expected

    def test_accepts_exception_object_too(self):
        """调用方两种传法都要支持（异常对象 / 已 redact 的字符串）。"""
        assert is_congestion_error(_Congestion(_QUEUE_FULL_MSG)) is True
        assert is_congestion_error(RuntimeError(_HARD_400_MSG)) is False

    def test_status_code_needs_word_boundary(self):
        """`HTTP 5xx` 不能把自造码（HTTP 5001）也算成上游 5xx。"""
        assert is_congestion_error("提交失败 HTTP 5001: 自定义错误码") is False

    def test_poll_timeout_is_deliberately_not_congestion(self):
        """超时不算拥塞：它已被自身封顶（单页 630s）。

        若按拥塞退避重试，一次 630s 等待会被乘 4 —— 看门狗的 OCR 静默
        上界随之爆掉（阈值不变式，docs/RUNTIME_WATCHDOG.md §5）。
        这是**刻意的设计决定**，不是遗漏。
        """
        from core.ocr_client import poll_timeout_for_pages

        assert poll_timeout_for_pages(1) == 630
        assert is_congestion_error(_POLL_TIMEOUT_MSG) is False

    def test_mineru_uses_the_shared_classifier_not_its_own_tuple(self):
        """MinerU 原先内联了一份 transient_markers —— 必须已收敛到真值处。

        两份词汇表迟早漂移（本项目反复踩过"同一事实写死在两处"）。
        只看**赋值形态**：文档字符串里说明"原元组已删除"不算重新定义。
        """
        src = (_SRC_ROOT / "core" / "mineru_client.py").read_text(encoding="utf-8")
        assert not re.search(r"transient_markers\s*=", src), (
            "mineru_client 又定义了自己的瞬态标记表 —— 应改用 "
            "core.ocr_client.is_congestion_error（单一真值）"
        )
        assert "is_congestion_error" in src


_MARKER_LITERALS = (
    "10010", "队列已满", "queue is full",
    "please try again later", "parsing failed",
)


class TestVocabularySingleSource:
    """标记字面量只允许出现在 core/ocr_client.py（AST 级检查）。"""

    @pytest.mark.parametrize("marker", _MARKER_LITERALS)
    def test_marker_literal_not_redeclared_elsewhere(self, marker):
        offenders = []
        for sub in ("core", "api"):
            for p in (_SRC_ROOT / sub).rglob("*.py"):
                if p.name == "ocr_client.py":
                    continue
                if any(marker in lit for lit in _code_string_literals(p)):
                    offenders.append(p.relative_to(_SRC_ROOT).as_posix())
        assert not offenders, (
            f"上游错误标记 {marker!r} 在 {offenders} 被重复写死 —— "
            f"应统一走 is_congestion_error()"
        )

    def test_classifier_is_the_only_place_holding_the_vocabulary(self):
        lits = _code_string_literals(_SRC_ROOT / "core" / "ocr_client.py")
        assert any("10010" in s for s in lits)
        assert any("队列已满" in s for s in lits)


# ─── 二、单角度探测：拥塞退避，不消耗角度预算 ─────────────────────────────

def _fast_backoff():
    """把退避阶梯与取消粒度改小，让用例秒级跑完（只改测试期常量）。"""
    return (
        patch.object(sh, "_ROTATION_CONGESTION_BACKOFF_S", (0.001,)),
        patch.object(sh, "_ROTATION_COOLDOWN_CANCEL_POLL_S", 0.001),
    )


class TestProbeAngleCongestion:
    @pytest.mark.asyncio
    async def test_congestion_retries_the_same_angle_then_succeeds(self):
        """拥塞后**重试同一角度**（旧实现在这里就换角度/放弃了）。"""
        p1, p2 = _fast_backoff()
        probe = AsyncMock(side_effect=[
            _Congestion(_QUEUE_FULL_MSG), _Congestion(_QUEUE_FULL_MSG), "x" * 150,
        ])
        with p1, p2, patch.object(sh, "_probe_slice_text", probe), \
                patch.object(sh, "touch_activity", AsyncMock()):
            md, blocked = await sh._probe_slice_angle(
                "s.pdf", "paddle", "job1",
                page_deadline=time.monotonic() + 5.0, db=object(),
                is_cancelled=AsyncMock(return_value=False),
            )
        assert blocked is False
        assert md == "x" * 150
        assert probe.await_count == 3, "拥塞必须触发重试，而不是直接放弃"

    @pytest.mark.asyncio
    async def test_heartbeat_written_before_every_backoff(self):
        """退避是**有界**的刻意等待 → 必须写心跳，否则看门狗误判停滞。"""
        p1, p2 = _fast_backoff()
        hb = AsyncMock()
        with p1, p2, patch.object(sh, "_probe_slice_text",
                                 AsyncMock(side_effect=_Congestion(_QUEUE_FULL_MSG))), \
                patch.object(sh, "touch_activity", hb):
            md, blocked = await sh._probe_slice_angle(
                "s.pdf", "paddle", "job1",
                page_deadline=time.monotonic() + 0.05, db=object(),
                is_cancelled=AsyncMock(return_value=False),
            )
        assert blocked is True and md == ""
        assert hb.await_count >= 1

    @pytest.mark.asyncio
    async def test_budget_exhaustion_returns_blocked_not_empty_string(self):
        """预算耗尽 → `blocked=True`（调用方据此区分"没读到"与"读不出"）。"""
        p1, p2 = _fast_backoff()
        with p1, p2, patch.object(sh, "_probe_slice_text",
                                 AsyncMock(side_effect=_Congestion(_QUEUE_FULL_MSG))), \
                patch.object(sh, "touch_activity", AsyncMock()):
            md, blocked = await sh._probe_slice_angle(
                "s.pdf", "paddle", "job1",
                page_deadline=time.monotonic() - 1.0,  # 预算已耗尽
                db=object(), is_cancelled=AsyncMock(return_value=False),
            )
        assert (md, blocked) == ("", True)

    @pytest.mark.asyncio
    async def test_hard_error_propagates_immediately(self):
        """永久类错误（参数错）：不重试、不上抛为 blocked —— 换角度才是正解。"""
        p1, p2 = _fast_backoff()
        probe = AsyncMock(side_effect=RuntimeError(_HARD_400_MSG))
        with p1, p2, patch.object(sh, "_probe_slice_text", probe), \
                patch.object(sh, "touch_activity", AsyncMock()):
            with pytest.raises(RuntimeError, match="invalid model"):
                await sh._probe_slice_angle(
                    "s.pdf", "paddle", "job1",
                    page_deadline=time.monotonic() + 5.0, db=object(),
                    is_cancelled=AsyncMock(return_value=False),
                )
        assert probe.await_count == 1

    @pytest.mark.asyncio
    async def test_poll_timeout_propagates_without_long_backoff(self):
        """超时不算拥塞 → 不进入分钟级退避。"""
        p1, p2 = _fast_backoff()
        probe = AsyncMock(side_effect=RuntimeError(_POLL_TIMEOUT_MSG))
        with p1, p2, patch.object(sh, "_probe_slice_text", probe), \
                patch.object(sh, "touch_activity", AsyncMock()):
            with pytest.raises(RuntimeError, match="轮询超时"):
                await sh._probe_slice_angle(
                    "s.pdf", "paddle", "job1",
                    page_deadline=time.monotonic() + 600.0, db=object(),
                    is_cancelled=AsyncMock(return_value=False),
                )
        assert probe.await_count == 1

    @pytest.mark.asyncio
    async def test_cancel_during_backoff_raises_heal_cancelled(self):
        """退避期间必须保持取消响应（300s 的 sleep 不能对取消失聪）。"""
        p1, p2 = _fast_backoff()
        with p1, p2, patch.object(sh, "_probe_slice_text",
                                 AsyncMock(side_effect=_Congestion(_QUEUE_FULL_MSG))), \
                patch.object(sh, "touch_activity", AsyncMock()):
            with pytest.raises(sh._HealCancelled):
                await sh._probe_slice_angle(
                    "s.pdf", "paddle", "job1",
                    page_deadline=time.monotonic() + 5.0, db=object(),
                    is_cancelled=AsyncMock(return_value=True),
                )

    @pytest.mark.asyncio
    async def test_probe_slice_text_no_longer_backs_off_congestion_internally(self):
        """内层不再对拥塞做 2s 小退避 —— 两层退避**相乘**正是 15s/角度的成因。"""
        probe = MagicMock(side_effect=_Congestion(_QUEUE_FULL_MSG))
        with patch("core.ocr_client.run_ocr", probe):
            with pytest.raises(_Congestion):
                await sh._probe_slice_text("s.pdf", "paddle", "job1")
        assert probe.call_count == 1, "拥塞不该在内层被重试（上层有预算化退避）"


# ─── 三、_rotation_heal：不烧角度预算 + 区分性诊断 ────────────────────────

async def _run_heal(db, targets, prior, probe_result=None, pages=None,
                    probe_side_effect=None):
    """带替身跑一轮 _rotation_heal，返回 (recovered, blocked, probe, audit)。

    替身对象**随返回值带出**：不能在 with 块外再去读模块属性（那时 patch
    已还原，读到的是原函数，断言会指向错误对象）。
    """
    probe = AsyncMock(return_value=probe_result, side_effect=probe_side_effect)
    audit = AsyncMock()
    with (
        patch.object(sh, "_prescreen_rotation_angles",
                     AsyncMock(return_value=(90, 270))),
        patch.object(sh, "_write_rotated_slice", MagicMock()),
        patch.object(sh, "_report_heal_progress", AsyncMock()),
        patch.object(sh, "_audit_log", audit),
        patch.object(sh, "_probe_slice_angle", probe),
        patch("core.pipeline._is_cancelled", AsyncMock(return_value=False)),
    ):
        recovered, blocked = await sh._rotation_heal(
            db, "job1", "fake.pdf", targets, "paddle",
            pages if pages is not None
            else {p: {"markdown": {"text": ""}} for p in targets},
            prior,
        )
    return recovered, blocked, probe, audit


class TestRotationHealBlocked:
    @pytest.mark.asyncio
    async def test_blocked_page_does_not_burn_remaining_angles(self):
        """拥塞时不再试其余角度（同样会被拒，只是白烧预算）→ 只 1 次探测。"""
        db = _FakeDB()
        prior = {5: {"source": "paddle", "text_chars": 0}}
        recovered, blocked, probe, _ = await _run_heal(db, [5], prior, ("", True))

        assert recovered == {}
        assert blocked == {5: sh.ROTATION_BLOCKED_CONGESTION}
        assert probe.await_count == 1, "预筛给了 2 个候选角度，但拥塞只应探测 1 次"

    @pytest.mark.asyncio
    async def test_blocked_diagnostic_is_distinct_from_probed(self):
        """`rotation_blocked` 与 `rotation_probed` **互斥**。"""
        db = _FakeDB()
        prior = {5: {"source": "paddle", "text_chars": 0}}
        await _run_heal(db, [5], prior, ("", True))

        diags = db.diag_updates()
        assert len(diags) == 1
        d = diags[0]
        assert d["rotation_blocked"] == sh.ROTATION_BLOCKED_CONGESTION
        assert "rotation_probed" not in d, (
            "被上游容量挡住时**不得**声称探测过 —— 那会把上游繁忙写成"
            "『此页无内容』的内容结论"
        )
        assert d["recovered"] is False

    @pytest.mark.asyncio
    async def test_blocked_page_is_audited(self):
        """GMP 可追溯：被挡住这件事必须进审计。"""
        db = _FakeDB()
        _, _, _, audit = await _run_heal(
            db, [5], {5: {"source": "paddle"}}, ("", True))
        actions = [c.args[2] for c in audit.await_args_list]
        assert "stage1_rotation_blocked" in actions
        detail = next(c.args[3] for c in audit.await_args_list
                      if c.args[2] == "stage1_rotation_blocked")
        assert "5" in detail and sh.ROTATION_BLOCKED_CONGESTION in detail
        assert "NOT probed" in detail, (
            "审计须说明「没探测」（可重试），而不是「读不出」"
        )

    @pytest.mark.asyncio
    async def test_doc_budget_exhaustion_marks_remaining_pages_blocked(self):
        """文档级预算耗尽 → 剩余目标页如实标记，且**不再探测**（不假装试过）。"""
        db = _FakeDB()
        prior = {5: {}, 6: {}}
        with patch.object(sh, "_ROTATION_DOC_BUDGET_S", 0.0):
            recovered, blocked, probe, _ = await _run_heal(
                db, [5, 6], prior, ("", True))
        assert recovered == {}
        assert blocked == {5: sh.ROTATION_BLOCKED_CONGESTION,
                           6: sh.ROTATION_BLOCKED_CONGESTION}
        assert probe.await_count == 1, "预算耗尽后不应继续探测"

    @pytest.mark.asyncio
    async def test_non_congestion_failure_still_reports_rotation_probed(self):
        """回归护栏：普通失败（角度都读不出内容）语义**不变** —— 仍落
        rotation_probed，绝不能被新逻辑顺手改成 blocked。"""
        db = _FakeDB()
        prior = {5: {"source": "paddle", "text_chars": 0}}
        recovered, blocked, _, _ = await _run_heal(db, [5], prior, ("", False))

        assert (recovered, blocked) == ({}, {})
        diags = db.diag_updates()
        assert len(diags) == 1
        assert diags[0].get("rotation_probed") is True
        assert "rotation_blocked" not in diags[0]

    @pytest.mark.asyncio
    async def test_recovered_page_write_is_unchanged(self):
        """回归护栏：正常恢复路径不受本次改动影响（写 raw_html + rotation_deg）。"""
        db = _FakeDB()
        prior = {5: {"source": "paddle", "text_chars": 0}}
        pages = {5: {"markdown": {"text": ""}}}
        recovered, blocked, _, _ = await _run_heal(
            db, [5], prior, ("横置九十度工序表 " + "甲" * 200, False), pages=pages)

        assert recovered == {5: 90}
        assert blocked == {}
        assert pages[5]["markdown"]["text"].startswith("横置九十度工序表")
        assert any("SET raw_html = ?" in sql for sql, _ in db.sql)


# ─── 四、诊断互斥 + 预算不变式 ───────────────────────────────────────────

class TestDiagMutualExclusion:
    def test_blocked_and_probed_never_coexist(self):
        raw = sh._self_heal_diag(
            {"source": "paddle"}, recovered=False,
            rotation_probed=True, rotation_blocked=sh.ROTATION_BLOCKED_CONGESTION,
        )
        d = json.loads(raw)
        assert d["rotation_blocked"] == sh.ROTATION_BLOCKED_CONGESTION
        assert "rotation_probed" not in d

    def test_blocked_is_recorded_even_without_prior_diag(self):
        """上游被挡住的**证据**不能因为"这页原本没有诊断"就丢掉。"""
        raw = sh._self_heal_diag(None, recovered=False,
                                 rotation_blocked=sh.ROTATION_BLOCKED_CONGESTION)
        assert raw is not None
        assert json.loads(raw)["rotation_blocked"] == sh.ROTATION_BLOCKED_CONGESTION

    def test_legacy_call_signature_unchanged(self):
        """老调用（只有 prior）行为不变：无 prior 仍返回 None。"""
        assert sh._self_heal_diag(None) is None
        assert sh._self_heal_diag({}) is None
        assert "rotation_probed" not in json.loads(
            sh._self_heal_diag({"source": "x"}))


class TestRotationBudgets:
    def test_page_budget_not_larger_than_doc_budget(self):
        assert sh._ROTATION_PAGE_BUDGET_S <= sh._ROTATION_DOC_BUDGET_S

    def test_page_budget_covers_the_measured_congestion_window(self):
        """单页预算必须 ≥ 实测拥塞窗口（≥18 分钟），否则等待无意义。

        现场实测：Paddle 拥塞持续 ≥18 分钟，而旧实现在 61s 内放弃。
        """
        assert sh._ROTATION_PAGE_BUDGET_S >= 18 * 60

    def test_page_budget_is_derived_and_fits_one_full_attempt(self):
        """单页预算必须是**派生量**，且 ≥ 一次完整尝试成本。

        手写 1200s（< 一次尝试 1260s）会让 `remaining` 在第一次尝试后
        必然 ≤ 0 —— 退避分支不可达、阶梯末项成死常量。2026-09-16 由
        `test_watchdog.py` 的机检当场抓到，此用例把"派生关系"钉住。
        """
        from core.ocr_client import poll_timeout_for_pages

        assert sh._ROTATION_PROBE_COST_S == float(
            sh._ROTATION_PROBE_ATTEMPTS * poll_timeout_for_pages(1))
        assert sh._ROTATION_PAGE_BUDGET_S == max(
            sh._ROTATION_PROBE_COST_S, sh._ROTATION_CONGESTION_WINDOW_S), (
            "单页预算必须取「一次尝试成本」与「实测拥塞窗口」的较大者"
        )
        assert sh._ROTATION_PAGE_BUDGET_S >= sh.rotation_silence_bound_s()
        assert sh._ROTATION_DOC_BUDGET_S >= sh._ROTATION_PAGE_BUDGET_S, (
            "文档预算低于单页预算 → 单页预算被 min() 架空（配置自相矛盾）"
        )

    def test_backoff_schedule_is_increasing_then_capped(self):
        sched = list(sh._ROTATION_CONGESTION_BACKOFF_S)
        assert sched == sorted(sched), "退避阶梯必须递增（否则等于密集重试）"
        assert sched[-1] > sched[0], "退避必须真的拉长间隔，而不是常量 sleep"

    def test_silence_bound_matches_derivation(self):
        from core.ocr_client import poll_timeout_for_pages

        expected = float(max(
            sh._ROTATION_PROBE_ATTEMPTS * poll_timeout_for_pages(1),
            sh._ROTATION_CONGESTION_BACKOFF_S[-1],
        ))
        assert sh.rotation_silence_bound_s() == expected

    def test_blocked_reason_literal_has_a_single_definition(self):
        """诊断/审计共用的取值只应有一处**定义**（赋值），否则迟早写歪。"""
        assert sh.ROTATION_BLOCKED_CONGESTION == "upstream_congestion"
        src = (_SRC_ROOT / "core" / "pipeline" / "self_heal.py").read_text(
            encoding="utf-8")
        assert len(re.findall(r'=\s*"upstream_congestion"', src)) == 1


# ─── 五、取消语义（与 #120 同源：操作状况不得写成内容结论）──────────────────

def _ocrcancelled_handler_order(path: Path) -> list[tuple[int, list]]:
    """含 `OCRCancelled` 的 try 的 except 顺序清单 → [(起始行, [类型名, ...])]。

    `<bare>` 表示裸 except。用 AST 而非正则：`except Exception:` 出现在
    注释/文档字符串里的说明不该被算作真实分支。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = []
        for h in node.handlers:
            t = h.type
            if t is None:
                names.append("<bare>")
            elif isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Attribute):
                names.append(t.attr)
            elif isinstance(t, ast.Tuple):
                names.append(tuple(e.id for e in t.elts
                                   if isinstance(e, ast.Name)))
            else:
                names.append("<expr>")
        if any(n == "OCRCancelled"
               or (isinstance(n, tuple) and "OCRCancelled" in n)
               for n in names):
            out.append((node.lineno, names))
    return out


class TestCancelIsNotAProbeFailure:
    """用户取消 ≠ 探测失败（旧实现把 OCRCancelled 记成"角度探测失败"）。

    这是 #120 的同源缺陷：把**操作状况**（上游拥塞 / 用户取消）写成
    **内容结论**（"此页读不出"）。取消那一支还有额外代价 —— 吞掉之后
    会继续试其余角度、继续打上游。
    """

    @pytest.mark.asyncio
    async def test_probe_slice_text_does_not_retry_a_cancel(self):
        """取消不是瞬态故障：不得退避 2s 重试（那会在用户已取消后再打一次上游）。"""
        probe = MagicMock(side_effect=OCRCancelled("job1 cancelled during polling"))
        with patch("core.ocr_client.run_ocr", probe):
            with pytest.raises(OCRCancelled):
                await sh._probe_slice_text("s.pdf", "paddle", "job1")
        assert probe.call_count == 1, (
            "用户取消后不得再打一次上游 —— 旧实现把它当瞬态故障退避重试"
        )

    @pytest.mark.asyncio
    async def test_rotation_heal_stops_on_cancel_without_claiming_probed(self):
        db = _FakeDB()
        prior = {5: {"source": "paddle", "text_chars": 0}}
        recovered, blocked, probe, audit = await _run_heal(
            db, [5], prior, probe_side_effect=OCRCancelled("cancelled"))

        assert (recovered, blocked) == ({}, {})
        assert probe.await_count == 1, "取消后不该继续试下一个候选角度"
        assert db.diag_updates() == [], (
            "取消不是内容结论：不得写 rotation_probed / rotation_blocked"
        )
        assert not [c for c in audit.await_args_list
                    if c.args[2] == "stage1_rotation_blocked"]


class TestCancelledPrecedesGenericHandler:
    """`OCRCancelled` 是 RuntimeError 子类 → 必须排在通用 `except Exception` 之前。

    `mineru_client` / `ocr_support` 的注释里已两次记下这个坑，本轮又在
    `self_heal._probe_slice_text` 发现同一个洞。故上升为**全仓机检**：
    新增代码若把通用分支写在前，取消会被静默吞成普通失败。
    """

    def test_no_generic_handler_shadows_ocrcancelled(self):
        offenders, found = [], 0
        for sub in ("core", "api", "llm", "db", "models"):
            for p in sorted((_SRC_ROOT / sub).rglob("*.py")):
                for lineno, names in _ocrcancelled_handler_order(p):
                    found += 1
                    ci = next(
                        i for i, n in enumerate(names)
                        if n == "OCRCancelled"
                        or (isinstance(n, tuple) and "OCRCancelled" in n)
                    )
                    shadow = [n for n in names[:ci]
                              if n in ("Exception", "BaseException", "<bare>")]
                    if shadow:
                        offenders.append(
                            f"{p.relative_to(_SRC_ROOT).as_posix()}:{lineno} "
                            f"{names}"
                        )
        assert found >= 3, (
            f"只扫到 {found} 处 OCRCancelled 捕获 —— 机检自身可能已失效"
            f"（一处都扫不到的护栏永远通过）"
        )
        assert not offenders, (
            "这些 try 把通用分支排在 OCRCancelled 之前，取消会被吞成普通失败："
            + "; ".join(offenders)
        )
