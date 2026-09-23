"""E2E 覆盖清单：把「真的验过」与「因环境跳过」分开记账，并让未覆盖成为一等公民。

背景（2026-09-21 B9-7 实测）：
冻结包冒烟有 22 条断言，但其中**最贵的那条链路**（Stage 2 的 LLM 分析）
依赖外部凭据。当 `.env` 里的 key 被上游拒绝时，流水线止于 ``status=error``；
脚本虽然**如实标注了归因**（连不上就是"环境问题"，不是产品缺陷，见
``tests/e2e_frozen.py`` 的直连对照探测），但——

  * "这一轮到底验到了什么、没验到什么"只散落在 stdout 里，**不可机读、不可审计**；
  * 更要紧的是：它**卡不住发版**。报告末尾照样是"22 passed"，一次关键路径
    从未运行的发版冒烟，读起来与一次真正跑通的冒烟**完全一样**。

这与本项目已被记过多次的病根同宗：``importorskip`` / ``[SKIP]`` 这类静默降级
一旦不落账，就演变成"全绿但关键路径没跑"。**降级可以，必须留痕且可被要求**。

本模块是记账口径的**唯一实现**（调用点不得自造状态字符串）：

  * :class:`Coverage` —— 逐条记录 ``covered / skipped / failed`` 及原因，可落盘；
  * :func:`classify_pipeline` —— 由**原始事实**（终态字符串、凭据是否齐备）推导
    覆盖状态，规则只此一处；
  * :func:`required_gaps` —— 计算"被显式要求、但未被真实覆盖"的条目；
  * :func:`exit_code` —— 由（断言失败数, 未满足的硬要求）共同决定退出码。

纪律（与既有规矩一致，且**每个都有对应单测**）：
  1. **``skipped`` 绝不等于 ``covered``** —— 只有真跑到成功终态才算 covered；
  2. **凭据缺失只能记 ``skipped``**，任何情况下都不得被读成"已验证"；
  3. 硬要求**默认不开启**（``PBC_E2E_REQUIRE_LLM`` 未设即按 0）；**发版/验收前**
     置 1，未覆盖即整体 FAIL。⚠️ "默认不开启"**不等于**"无凭据也能跑绿" ——
     产品自身在未配置 LLM 时会拒绝上传（`api/jobs/upload.py` 返回 400），
     那轮冒烟本来就红。这道门的价值在于拦住"其它全绿、只差 LLM 链路"的假成功。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# ── 覆盖条目 key：唯一真值，调用点必须引用这些常量 ──────────────────
#: 凭据被应用**写入**且生效于 settings 通路。
#: ⚠️ 它**不**声称凭据有效 —— 配置期不做上游校验（2026-09-21 实测：塞一把
#: 必然无效的 key，应用照样报 configured=True）。凭据有效性由
#: :data:`ENTRY_LLM_PIPELINE` 回答，两者不可互相顶替。
ENTRY_LLM_CONFIG = "llm_config"
#: 流水线真的跑到成功终态**且**该链路有权威证据（LLM 链路**已验**）。
#: ⚠️ 「成功终态」本身**不够** —— 2026-09-23 实测：余额不足时逐页分析成功、
#: 跨页 LLM 得 402，产品**按设计降级**并照常走到 ``review``。故本条目还要求
#: ``llm_call_audit`` 中存在 ``success=1`` 的调用（见
#: :func:`_judge_llm_success`）。凭据有效性由本条目回答，与
#: :data:`ENTRY_LLM_CONFIG` 不可互相顶替。
ENTRY_LLM_PIPELINE = "llm_pipeline"
ENTRY_OCR_CONFIG = "ocr_config"
ENTRY_OCR_PIPELINE = "ocr_pipeline"

# ── 状态：三态，**没有第四态**（判不了按 failed 走，fail-closed）────
STATUS_COVERED = "covered"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

# 状态说明（落盘自解释，便于事后审计）
_STATUS_MEANING = {
    STATUS_COVERED: "真实执行并到达预期结果（该路径**已验**）",
    STATUS_SKIPPED: "因环境缺项未执行（**不等于已验**；发版前须消除）",
    STATUS_FAILED: "执行了但结果不符预期",
}

_ALL_STATUSES = frozenset(_STATUS_MEANING)

# ── 硬要求开关与环境变量 ────────────────────────────────────────────
#: 置 1 时：LLM 链路（配置 + 成功终态）必须真实覆盖，否则整体 FAIL。
REQUIRE_LLM_ENV = "PBC_E2E_REQUIRE_LLM"
#: 覆盖清单落盘位置覆盖（默认 <repo>/devlogs/e2e_coverage_<本地时间>.json）。
COVERAGE_PATH_ENV = "PBC_E2E_COVERAGE_JSON"

#: 开关 → (被其约束的条目, 未满足时打印的一句话)
_REQUIREMENTS = {
    REQUIRE_LLM_ENV: (
        (ENTRY_LLM_CONFIG, ENTRY_LLM_PIPELINE),
        "LLM 链路必须被真实覆盖（发版/验收口径）",
    ),
}

#: 流水线的成功终态（与 job 状态机一致；两处以上出现即为漂移风险）
SUCCESS_TERMINALS = ("review", "partial_review")
#: 流水线的失败终态
FAILURE_TERMINALS = ("error",)
#: 非结论性终态（作业被人工/系统取消 —— 什么也没验到，但不是缺陷）
INCONCLUSIVE_TERMINALS = ("cancelled",)

_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})


def env_flag(name: str) -> bool:
    """把环境变量读成布尔（``1/true/yes/on/y``，大小写与空白不敏感）。

    刻意**不做** ``bool(os.environ.get(...))`` —— 那样 ``"0"`` 与 ``"false"``
    都会读成 True，是个"看起来能关其实关不掉"的静默陷阱。
    """
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def default_path() -> Path:
    """覆盖清单默认落盘路径（可用 :data:`COVERAGE_PATH_ENV` 覆盖）。"""
    override = os.environ.get(COVERAGE_PATH_ENV)
    if override:
        return Path(override)
    root = Path(__file__).resolve().parent.parent
    return root / "devlogs" / f"e2e_coverage_{time.strftime('%Y%m%d-%H%M%S')}.json"


class Coverage:
    """逐条覆盖记录，可落盘为 JSON。

    ``record`` **后写覆盖先写**（终局结论为准）。但有一条硬约束：
    ``covered`` 只能由 :func:`classify_pipeline` 之类的"事实推导"入口写入，
    调用方不得凭"凭据存在"直接记 covered —— 见模块 docstring 纪律 2。
    """

    def __init__(self, path=None):
        self._items: dict[str, dict] = {}
        self.path = Path(path) if path else default_path()

    # ── 写 ─────────────────────────────────────────────────────────
    def record(self, key: str, status: str, reason: str = "", detail: str = "") -> None:
        if status not in _ALL_STATUSES:
            raise ValueError(f"未知覆盖状态 {status!r}（只允许 {sorted(_ALL_STATUSES)}）")
        self._items[key] = {
            "status": status,
            "reason": reason,
            "detail": detail,
            "meaning": _STATUS_MEANING[status],
        }

    def record_many(self, mapping) -> None:
        for key, (status, reason, detail) in mapping.items():
            self.record(key, status, reason, detail)

    # ── 读 ─────────────────────────────────────────────────────────
    def status(self, key: str, default: str = "missing") -> str:
        item = self._items.get(key)
        return item["status"] if item else default

    def items(self) -> dict:
        return dict(self._items)

    def counts(self) -> dict:
        out = {s: 0 for s in _ALL_STATUSES}
        for item in self._items.values():
            out[item["status"]] += 1
        return out

    # ── 输出 ───────────────────────────────────────────────────────
    def as_dict(self) -> dict:
        return {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counts": self.counts(),
            "items": self.items(),
        }

    def write(self, path=None) -> Path:
        """落盘。父目录不存在则创建；**失败不抛**（记账不应反过来打断冒烟）。"""
        target = Path(path) if path else self.path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(self.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # noqa: BLE001 — 记账失败不得掩盖真结论
            print(f"    [WARN] 覆盖清单落盘失败（{target}）：{exc}")
        return target

    def report_lines(self) -> list[str]:
        lines = []
        for key in sorted(self._items):
            item = self._items[key]
            suffix = f" —— {item['reason']}" if item["reason"] else ""
            lines.append(f"  [{item['status']:<7}] {key}{suffix}")
        return lines


# ── 事实 → 状态：规则唯一实现 ────────────────────────────────────────


def _judge_llm_success(llm_call_succeeded: bool | None, terminal: str):
    """成功终态下判 LLM 链路是否**真的**被覆盖。

    🔴 **成功终态不蕴含 LLM 成功过**（2026-09-23 实测的真实假绿）：
    一把 key 的账户**余额耗尽**时，逐页分析先成功、跨页 LLM 得
    ``402 {'code': 30001, 'account balance is insufficient'}``；
    产品**按设计降级**（把 "LLM 调用失败" 写成 severity=info/warning 的 finding）
    并照常走到 ``review``。于是 ``terminal == review`` 被记成
    ``llm_pipeline = covered``、``PBC_E2E_REQUIRE_LLM=1`` 下退出码 **0**
    —— 一条本该拦住发版的门禁**放了行**。

    ⇒ ``covered`` 只能建立在**审计表里真有一次成功调用**之上，
    且判不了（``None``）一律 fail-closed。
    """
    if llm_call_succeeded is True:
        return (STATUS_COVERED,
                f"流水线到达成功终态（terminal={terminal}），"
                f"且 llm_call_audit 中存在 success=1 的调用", "LLM")
    if llm_call_succeeded is False:
        return (STATUS_FAILED,
                f"流水线到达成功终态（terminal={terminal}），但 llm_call_audit 中"
                f"**没有任何成功调用** ⇒ 该终态是靠**降级**达成的，LLM 链路未验",
                "LLM")
    return (STATUS_FAILED,
            f"流水线到达成功终态（terminal={terminal}），但**未取得 LLM 调用证据**"
            f"（llm_call_succeeded=None）⇒ fail-closed：判不了 ≠ 已验", "LLM")


def _judge_ocr_success(ocr_backend_used: str | None, terminal: str):
    """成功终态下判 OCR 链路是否**真的**走了真实后端。

    判据取**产品自己记录**的 ``jobs.ocr_backend_used``（不是驱动自述）：
    非空 ⇒ 确实由某个真实后端完成了解析；空/None ⇒ 无从断定
    （可能是 failover 掩盖或根本没跑 OCR）⇒ fail-closed。
    """
    backend = (ocr_backend_used or "").strip()
    if backend:
        return (STATUS_COVERED,
                f"流水线到达成功终态（terminal={terminal}），"
                f"且 ocr_backend_used={backend}（真实后端）", "OCR")
    return (STATUS_FAILED,
            f"流水线到达成功终态（terminal={terminal}），但**未记录 "
            f"ocr_backend_used** ⇒ 无法断定 OCR 走了真实后端"
            f"（可能被 failover 掩盖）⇒ fail-closed", "OCR")


def classify_pipeline(terminal: str, *, llm_ready: bool, ocr_ready: bool,
                      upload_failed: bool = False,
                      llm_call_succeeded: bool | None = None,
                      ocr_backend_used: str | None = None) -> dict:
    """由**原始事实**推导 ``llm_pipeline`` / ``ocr_pipeline`` 两条覆盖状态。

    参数是事实（终态字符串 + 凭据是否齐备 + 上传是否失败 + **两条链路各自的
    权威证据**），不是结论 —— 这样规则只有一处，且可被单测直接钉住（无需起服务）。

    行为表（``llm_ready`` / ``ocr_ready`` 指凭据已提供且被应用**写入**）：

    | terminal | 结果 |
    |---|---|
    | ``review`` / ``partial_review`` | 凭据齐备 **且该链路有权威证据** ⇒ covered；凭据齐备但**无证据** ⇒ failed；缺凭据 ⇒ skipped |
    | ``error`` | 凭据齐备的一侧记 failed；缺凭据的一侧记 skipped |
    | ``cancelled`` | 两侧均 skipped（作业被取消 ⇒ 什么也没验到，但不是缺陷）|
    | 空串（未达终态）| 两侧均 failed（超时**不得**被读成"环境跳过"）|

    ``llm_call_succeeded``（LLM 的权威证据）取自产品自己的
    ``GET /api/jobs/{id}/llm_audit``：**有 success=1 的调用**才为 ``True``。
    ``ocr_backend_used``（OCR 的权威证据）取自 ``GET /api/jobs/{id}`` 的同名字段。

    ⚠️ 两者**默认 ``None`` = 判不了 ⇒ failed**（fail-closed）。这条默认值就是
    "成功终态不蕴含链路被覆盖"的落地：忘记传证据**不会**得到绿，而不是悄悄变绿
    （2026-09-23 的真实事故见 :func:`_judge_llm_success`）。

    ``upload_failed=True``（且终态为空）表示**作业从未建立** —— 与"作业跑了但
    没跑完"同属失败，但**归因不同**，reason 必须分开写（2026-09-21 实测：
    上传被产品拒绝时，旧 reason 写成"未在预算内到达终态"，把排查引向超时）。
    """
    out: dict[str, tuple[str, str, str]] = {}
    success = terminal in SUCCESS_TERMINALS
    failure = terminal in FAILURE_TERMINALS
    inconclusive = terminal in INCONCLUSIVE_TERMINALS

    if not terminal:
        if upload_failed:
            why = "上传未成功 ⇒ 流水线从未建立（作业不存在，故什么也没验到）"
        else:
            why = f"未在预算内到达终态（terminal={terminal!r}）⇒ 按失败处理，fail-closed"
        return {
            ENTRY_LLM_PIPELINE: (STATUS_FAILED, why, ""),
            ENTRY_OCR_PIPELINE: (STATUS_FAILED, why, ""),
        }

    for entry, ready, who in (
        (ENTRY_LLM_PIPELINE, llm_ready, "LLM"),
        (ENTRY_OCR_PIPELINE, ocr_ready, "OCR"),
    ):
        if success and ready:
            out[entry] = (
                _judge_llm_success(llm_call_succeeded, terminal)
                if entry == ENTRY_LLM_PIPELINE
                else _judge_ocr_success(ocr_backend_used, terminal)
            )
        elif success and not ready:
            out[entry] = (
                STATUS_SKIPPED,
                f"流水线成功，但本轮未提供 {who} 凭据 ⇒ 无法断定 {who} 链路被覆盖",
                who,
            )
        elif failure and ready:
            out[entry] = (STATUS_FAILED, f"流水线以 error 收场（terminal={terminal}）", who)
        elif failure and not ready:
            out[entry] = (
                STATUS_SKIPPED,
                f"流水线未跑通且未提供 {who} 凭据 ⇒ 无从判定 {who} 链路",
                who,
            )
        elif inconclusive:
            out[entry] = (
                STATUS_SKIPPED,
                f"作业以 {terminal} 收场 ⇒ 未验到任何结论（也非缺陷）",
                who,
            )
        else:
            out[entry] = (STATUS_FAILED, f"未知终态 {terminal!r}", who)
    return out


def required_gaps(cov: Coverage, require_llm: bool | None = None) -> list[str]:
    """返回"被显式要求、但未被真实覆盖"的条目（空列表 = 全部满足）。

    开关默认从环境读取；**未开启的开关一律不产生 gap** —— 硬要求默认不生效，
    这样日常冒烟在弱环境仍可跑，而发版前只需加一个环境变量即可收紧。
    """
    if require_llm is None:
        require_llm = env_flag(REQUIRE_LLM_ENV)
    enabled = {REQUIRE_LLM_ENV: bool(require_llm)}

    gaps: list[str] = []
    for flag, (entries, why) in _REQUIREMENTS.items():
        if not enabled.get(flag):
            continue
        for entry in entries:
            st = cov.status(entry)
            if st != STATUS_COVERED:
                gaps.append(f"{entry}={st}（要求：{why}）")
    return gaps


def exit_code(failed_assertions: int, gaps) -> int:
    """退出码：(有断言失败) 或 (有未满足的硬要求) ⇒ 1。"""
    return 1 if (failed_assertions or list(gaps)) else 0
