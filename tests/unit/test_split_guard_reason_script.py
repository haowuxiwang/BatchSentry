"""`scripts/split_guard_reason.py` 的判据与安全默认值（CR-1 / R97 配套）。

这个脚本会**改写用户可见的业务文案**，所以它的判定必须可测、且默认必须安全：
- 认不出护栏理由 → 不动手（宁缺勿错）；
- 正文为空（整条只剩理由）→ 不自动处理（拆完变空正文，得人来定）；
- 默认 dry-run，不加 `--apply` 不落盘；
- `--apply` 前先备份。

反面用例是重点：业务文案里自带 ``｜`` 的情况**必须不被拆**。代码替用户判断
"正文到哪里结束"这件事，本身就是 `PROJECT_PITFALLS` 六十八① 明令禁止的形态。
"""
import sqlite3
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts.split_guard_reason import SEP, split_one  # noqa: E402


class TestSplitOne:
    def test_typical_polluted_description_is_split(self):
        desc = ("检查人签名日期（2015.6.03）早于检查人签名日期（2025.06.30）"
                "，存在时间倒置。" + SEP +
                "LLM 独断 critical，未经规则层确定性判据复核 ⇒ 降为 warning 待人工核对")
        head, tail = split_one(desc)
        assert head == "检查人签名日期（2015.6.03）早于检查人签名日期（2025.06.30），存在时间倒置。"
        assert tail.startswith("LLM 独断 critical")

    def test_speculative_reason_is_recognized(self):
        desc = ("步骤10中湿度记录为35%RH%RH，单位重复" + SEP +
                "文案为推测性表述（命中「可能」），不能作为异常结论"
                "（v4 prompt 已禁止此类表述）⇒ 降级为待人工核对")
        assert split_one(desc)[1].startswith("文案为推测性表述")

    def test_grounding_reason_is_recognized(self):
        desc = "批号不一致" + SEP + "文案中的数字（50008219）在 OCR 原文中无法定位，疑似提取幻觉"
        assert split_one(desc) is not None

    # ── 反面：业务文案自带 ｜ 时**不得**被拆 ──────────────────────────
    def test_business_pipe_without_guard_marker_is_untouched(self):
        desc = "工序2 记录：时间 09:07" + SEP + "12:09，请核对是否为同一工序"
        assert split_one(desc) is None

    def test_plain_description_without_separator_is_untouched(self):
        assert split_one("操作者与复核人签名姓名不一致") is None
        assert split_one("") is None
        assert split_one(None) is None

    def test_separator_at_end_without_reason_is_untouched(self):
        assert split_one("某条记录" + SEP) is None

    def test_rsplit_keeps_business_pipe_inside_head(self):
        """理由永远追加在**最后** ⇒ 用 rsplit；正文里的 ｜ 必须留在正文里。"""
        desc = ("前段" + SEP + "中段" + SEP +
                "文案中的数字（2025）在 OCR 原文中无法定位，疑似提取幻觉")
        head, _tail = split_one(desc)
        assert head == "前段" + SEP + "中段"

    def test_empty_head_is_returned_so_caller_can_skip(self):
        """整条只剩理由（实测 p22 `1563`）—— 函数如实返回空正文，由调用方拒改。"""
        head, tail = split_one(SEP + "LLM 独断 critical，未经规则层确定性判据复核")
        assert head == ""
        assert "LLM 独断" in tail


def _make_db(path: Path, rows):
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, page INTEGER, type TEXT, severity TEXT,
            description TEXT, guard_reason TEXT
        );
        CREATE TABLE findings_archive (
            archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_id INTEGER, job_id TEXT, page INTEGER, type TEXT,
            severity TEXT, description TEXT, guard_reason TEXT
        );
    """)
    con.executemany(
        "INSERT INTO findings (job_id, page, type, severity, description) "
        "VALUES ('j', 1, 'x', 'warning', ?)", [(d,) for d in rows])
    con.commit()
    con.close()


def _run(argv):
    from scripts.split_guard_reason import main
    old = sys.argv
    sys.argv = ["split_guard_reason.py", *argv]
    try:
        return main()
    finally:
        sys.argv = old


class TestSafetyDefaults:
    POLLUTED = "批号不一致" + SEP + "LLM 独断 critical，未经规则层确定性判据复核"
    LEGIT = "工序2 时间 09:07" + SEP + "12:09"

    def test_dry_run_does_not_write(self, tmp_path, capsys):
        db = tmp_path / "dry.db"
        _make_db(db, [self.POLLUTED, self.LEGIT])
        assert _run(["--db", str(db)]) == 0
        con = sqlite3.connect(str(db))
        got = [r[0] for r in con.execute("SELECT description FROM findings ORDER BY id")]
        con.close()
        assert got == [self.POLLUTED, self.LEGIT], "dry-run 不得改库"
        assert "dry-run" in capsys.readouterr().out

    def test_apply_splits_only_marked_rows_and_backs_up(self, tmp_path):
        db = tmp_path / "apply.db"
        _make_db(db, [self.POLLUTED, self.LEGIT])
        assert _run(["--db", str(db), "--apply"]) == 0

        assert db.with_suffix(db.suffix + ".bak-split-guard-reason").exists(), "写前必须备份"

        con = sqlite3.connect(str(db))
        rows = list(con.execute(
            "SELECT description, guard_reason FROM findings ORDER BY id"))
        con.close()
        assert rows[0] == ("批号不一致", "LLM 独断 critical，未经规则层确定性判据复核")
        assert rows[1] == (self.LEGIT, None), "业务文案里的 ｜ 不得被拆"

    def test_empty_head_row_is_left_alone(self, tmp_path, capsys):
        db = tmp_path / "empty.db"
        only_reason = SEP + "LLM 独断 critical，未经规则层确定性判据复核"
        _make_db(db, [only_reason])
        assert _run(["--db", str(db), "--apply"]) == 0
        con = sqlite3.connect(str(db))
        desc, gr = list(con.execute(
            "SELECT description, guard_reason FROM findings"))[0]
        con.close()
        assert desc == only_reason and gr is None, "整条只剩理由的行必须交给人"
        assert "未处理" in capsys.readouterr().out

    def test_apply_is_idempotent(self, tmp_path):
        db = tmp_path / "idem.db"
        _make_db(db, [self.POLLUTED])
        assert _run(["--db", str(db), "--apply"]) == 0
        assert _run(["--db", str(db), "--apply"]) == 0   # 第二次没有可拆的行
        con = sqlite3.connect(str(db))
        rows = list(con.execute("SELECT description, guard_reason FROM findings"))
        con.close()
        assert rows == [("批号不一致", "LLM 独断 critical，未经规则层确定性判据复核")]

    def test_refuses_db_without_guard_reason_column(self, tmp_path, capsys):
        """没跑过 v15 迁移的库：拒绝，而不是写进一个不存在的列。"""
        db = tmp_path / "old.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE findings (id INTEGER PRIMARY KEY, description TEXT)")
        con.commit()
        con.close()
        assert _run(["--db", str(db), "--apply"]) == 2
        assert "guard_reason" in capsys.readouterr().out
