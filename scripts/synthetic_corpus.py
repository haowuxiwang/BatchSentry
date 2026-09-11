"""合成批记录语料生成器（M2/T2.2）—— 缺陷"答案键"的输入侧。

为什么需要它
------------
真实 51 页文档无法逐条人工标注 700+ findings，且含业务原文（不便入库/共享）。
本模块用**确定性构造**的合成页结构化数据（planted defects）充当评测输入：
每个 case 精确知道埋了哪些缺陷，配合 `docs/FINDING_GROUND_TRUTH.json` 的
标签即可算出可复现的 precision / recall / F1（借鉴
`regulated-multiagent-reference` 的 answer-key 方法）。

输入形态与 `core.rules._normalize_pages` 期望一致（`page_cache.structured_json`
的解析结果），且**不依赖 OCR/LLM** —— 规则层是纯函数，故评测零成本、可离线、
逐位可复现。

设计约定
--------
- 每个 case 的 `case_id` 必须与金标文件中的键一致（由 `scripts/eval_findings.py`
  交叉校验，缺一即报错）。
- 页面只含规则层关心的字段；不写真实企业名/批号（合成值）。
"""
from __future__ import annotations


def _page(n: int, steps: list[dict] | None = None,
          page_info: dict | None = None,
          extra: dict | None = None) -> dict:
    """构造一页结构化数据（结构与 page_cache.structured_json 一致）。"""
    data: dict = {
        "page_info": {"page_number": n, **(page_info or {})},
        "steps": steps or [],
        "findings": [],
    }
    if extra:
        data.update(extra)
    return {"page": n, "data": data}


def _step(no: int, op: str, operator: str = "操作员甲", reviewer: str = "复核员乙",
          start: str = "", end: str = "", **extra) -> dict:
    s = {"no": no, "operation": op, "operator": operator, "reviewer": reviewer}
    if start:
        s["start_time"] = start
    if end:
        s["end_time"] = end
    s.update(extra)
    return s


# 一个"干净"工序：编号连续、时间有序、操作/复核齐全、无越界参数。
_CLEAN_STEP = _step(1, "物料称量", start="2025-01-20 08:00", end="2025-01-20 08:30")


def build_cases() -> dict[str, list[dict]]:
    """返回 {case_id: page_structures}（每页结构与 page_cache 解析结果一致）。"""
    cases: dict[str, list[dict]] = {}

    # 1) 干净页 —— 精度护栏：不得报出任何高价值缺陷（types 见金标 forbid）。
    cases["clean_page"] = [_page(1, [
        _step(1, "物料称量", start="2025-01-20 08:00", end="2025-01-20 08:30"),
        _step(2, "溶解配制", start="2025-01-20 08:30", end="2025-01-20 09:10"),
    ])]

    # 2) 参数越界 —— pH 3.0 不在 6.0-8.0。
    cases["param_out_of_spec"] = [_page(1, [
        _step(1, "溶解配制", start="2025-01-20 08:30", end="2025-01-20 09:10",
              parameters=[{"name": "pH", "value": "3.0",
                           "spec_range": "6.0-8.0", "value_source": "handwritten"}]),
    ])]

    # 3) 页内时间倒序 —— 开始晚于结束。
    cases["time_reversal_in_page"] = [_page(1, [
        _step(1, "溶解配制", start="2025-01-20 19:09", end="2025-01-20 10:15"),
    ])]

    # 4) 跨页批号不一致 —— 两页主批号不同。
    #    注意：批号 **不能** 只差 "-后缀"（如 SYN-B001/SYN-B002）—— R7 会把
    #    "-" 后的工序页号视作同一批号核心（SYN）而正确归并。此处必须让核心不同，
    #    才是真正的"跨页批号不一致"。
    cases["batch_inconsistency"] = [
        _page(1, [_step(1, "物料称量", start="2025-01-20 08:00", end="2025-01-20 08:30")],
              page_info={"batch_no": "SYNQ001"}),
        _page(2, [_step(2, "溶解配制", start="2025-01-20 08:30", end="2025-01-20 09:10")],
              page_info={"batch_no": "SYNW002"}),
    ]

    # 5) 可疑未来日期 —— 2099 年。
    cases["suspicious_future_date"] = [_page(1, [
        _step(1, "物料称量", start="2099-01-01 08:00", end="2099-01-01 09:00"),
    ])]

    # 6) 内容不完整 —— 有操作描述但缺执行时间（completeness/warning）。
    cases["missing_exec_time"] = [_page(1, [
        _step(1, "物料称量"),  # 无 start/end
    ])]

    # 7) 自检自核（操作人 == 复核人）—— **当前规则未覆盖（known_gap）**。
    #    金标标记 known_gap=true：不计入 P/R，仅作可见 TODO；M4 的 R12 落地后
    #    翻转为 expect，即成为 R12 的验收测试。
    cases["self_review_operator_eq_reviewer"] = [_page(1, [
        _step(1, "溶解配制", operator="操作员甲", reviewer="操作员甲",
              start="2025-01-20 08:30", end="2025-01-20 09:10"),
    ])]

    return cases


if __name__ == "__main__":  # 人工抽查：打印各 case 的页数/工序数
    import json
    for cid, pages in build_cases().items():
        print(f"{cid}: {len(pages)} page(s), "
              f"{sum(len(p['data']['steps']) for p in pages)} step(s)")
    print(json.dumps(sorted(build_cases()), ensure_ascii=False))
