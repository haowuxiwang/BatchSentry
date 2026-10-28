"""把历史 findings 里被污染的 description 拆回 ``guard_reason``（CR-1 / R97 配套）。

背景
----
R97 之前，`apply_review` 把护栏降级理由以 ``{正文}｜{理由}`` **追加到 description**，
于是复核页正文里出现「LLM 独断 critical，未经规则层确定性判据复核」「v4 prompt
已禁止此类表述」这类**内部语**。R97 起新写入不再污染，但**历史行仍然是脏的**。

为什么不在迁移里自动改
----------------------
"正文到哪里结束"是**业务判断**（见 `docs/PROJECT_PITFALLS.md` 六十八①：
表头文字 ≠ 业务语义，代码不得替用户推定）。迁移里猜分隔符，猜错就是**静默改写
业务记录**（GMP 不可接受）。所以做成脚本：**默认 dry-run**，只报告；`--apply`
才写，且写前打备份。

判据（宁可少拆，不可错拆）
--------------------------
只在 ``｜`` **右侧**命中护栏理由的**专属措辞**时才拆。这些短语只由
`core/rules/llm_finding_guard.py` 产生，业务文案里不会自然出现。
正文为空的行（整条只剩理由）**不自动处理** —— 那种行缺的是业务描述本身，
拆完会变成空正文，得人来定（实测只有 1 条：p22 的 `1563`）。

用法
----
    python scripts/split_guard_reason.py                 # dry-run（默认）
    python scripts/split_guard_reason.py --db <path>     # 指定库
    python scripts/split_guard_reason.py --apply         # 真写（先备份）
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 分隔符：apply_review 用的是全角竖线。
SEP = "｜"

#: 护栏理由的**专属措辞** —— 逐条对应 llm_finding_guard.py 里的模板。
#: 只认这些，避免把业务文案里自带的 ``｜`` 误当分隔符。
REASON_MARKERS = (
    "v4 prompt 已禁止此类表述",
    "未经规则层确定性判据复核",
    "在 OCR 原文中无法定位，疑似提取幻觉",
    "规则层无法复核该结论",
    "在该页结构化抽取中查无此值",
    "属手写体 OCR 变体",
    "疑似抽取抖动",
    "该比较不构成异常（依据是墙钟",
    "疑似类型错配",
    "疑似列串位",
    "超差判定不成立",
    "应为「",           # L3 方向重算：'… 应为「晚于」…'
)

#: 主键列名 —— 两张表**不一样**（`findings.id` vs `findings_archive.archive_id`）。
#: 实测踩过：统一写 `id` ⇒ `no such column: id`。
KEY_COL = {"findings": "id", "findings_archive": "archive_id"}

_MARKER_RE = re.compile("|".join(re.escape(m) for m in REASON_MARKERS))


def split_one(description: str) -> tuple[str, str] | None:
    """``"正文｜理由"`` → ``("正文", "理由")``；不适用返回 ``None``。

    - 用 ``rsplit``：理由永远是**最后**追加的；正文里若自带 ``｜`` 也切不错。
    - 右侧必须命中专属措辞，否则**不动**（宁缺勿错）。
    """
    if not description or SEP not in description:
        return None
    head, tail = description.rsplit(SEP, 1)
    if not _MARKER_RE.search(tail):
        return None
    return head.strip(), tail.strip()


def _db_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    # 与应用同一落点（config.data_dir），避免"脚本改的是另一个库"这种假成功。
    sys.path.insert(0, str(ROOT))
    from config import config  # noqa: E402  (延迟导入：只有默认路径才需要)
    return Path(config.data_dir) / "pbc.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="SQLite 路径（默认取 config.data_dir/pbc.db）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    ap.add_argument("--limit", type=int, default=10, help="样例打印条数")
    args = ap.parse_args()

    db = _db_path(args.db)
    if not db.exists():
        print(f"[ERR] 库不存在：{db}")
        return 2

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        has_col = {r["name"] for r in con.execute("PRAGMA table_info(findings)")}
        if "guard_reason" not in has_col:
            print("[ERR] 该库还没有 guard_reason 列 —— 先让应用启动一次完成 v15 迁移")
            return 2

        plan: list[tuple[str, int, str, str, str]] = []   # (table, id, head, tail, old)
        skipped_empty: list[tuple[str, int, str]] = []
        for table in ("findings", "findings_archive"):
            if not con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                continue
            rows = con.execute(
                f"SELECT {KEY_COL[table]} AS rid, description FROM {table} "
                "WHERE description LIKE ?", (f"%{SEP}%",)
            ).fetchall()
            for r in rows:
                got = split_one(r["description"])
                if not got:
                    continue
                head, tail = got
                if not head:
                    skipped_empty.append((table, r["rid"], tail))
                    continue
                plan.append((table, r["rid"], head, tail, r["description"]))

        print(f"库：{db}")
        print(f"可拆分：{len(plan)} 条 | 正文为空（需人工定）：{len(skipped_empty)} 条")
        for table, rid, head, tail, _old in plan[: args.limit]:
            print(f"  [{table}#{rid}] {head[:60]}  ←  理由：{tail[:40]}")
        if len(plan) > args.limit:
            print(f"  … 另 {len(plan) - args.limit} 条")
        for table, rid, tail in skipped_empty:
            print(f"  ⚠️ [{table}#{rid}] 整条只剩理由，未处理：{tail[:60]}")

        if not args.apply:
            print("\n（dry-run，未写库。确认无误后加 --apply）")
            return 0

        if not plan:
            print("\n没有需要拆分的行。")
            return 0

        backup = db.with_suffix(db.suffix + ".bak-split-guard-reason")
        shutil.copy2(db, backup)
        print(f"\n已备份：{backup}")

        with con:   # 单事务：要么全改，要么全不改
            for table, rid, head, tail, _old in plan:
                con.execute(
                    f"UPDATE {table} SET description = ?, guard_reason = ? "
                    f"WHERE {KEY_COL[table]} = ?",
                    (head, tail, rid),
                )
        print(f"已拆分 {len(plan)} 条。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
