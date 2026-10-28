"""机检：「后端所有 DB 写必须走 db_lock」的**不新增**护栏（棘轮）。

## 这个文件锁住什么、**不**锁什么

锁住的是一条棘轮：`api/ core/ db/` 下「裸 DB 写」（未包在 `async with db_lock`
内的 `execute / executemany / commit / executescript`）必须与
`tests/unit/db_write_lock_baseline.json` **逐键相等**。

⚠️ **这份基线不是「已审计安全」清单。** 它记录的 122 处（106 个键）是
`docs/ADVERSARIAL_REVIEW_2026-10-19.md` F2 登记的**已知债务**：这些写在结构上
没有被锁保护，其中绝大多数**没有**逐条论证过「为什么可以不加锁」——
现在写不出来，硬写就是编（F1/F4 一改，行号全变，理由也得重写）。

所以本护栏**不主张**：

  · 这 122 处没有并发问题（扫描器自己也只做**语法层**判定）；
  · 基线条目背后的理由充分。

它只保证一件事：**债务不能悄悄变多**。新增一处裸写 ⇒ 红 ⇒ 要么加锁，
要么把新键写进基线 —— 而写进基线这一步在 diff 里可见，也就必须给出理由。

## 为什么「减少」也判红

只拦「新增」是不够的：有人在 A 处删掉一处裸写、同时在 B 处新增一处**不同形态**
的裸写，总数不变 ⇒ 恒绿。要求逐键相等堵住了这种抵消；顺带逼着「还了债」必须
重生成基线，让债务下降在 diff 里留下痕迹 —— 否则基线永远虚高，以后涨回去
也没人发现。

## 三条失败信息分别意味着什么

  · 【新增】      新写的 DB 写没有 db_lock ⇒ 加锁，或登记进基线并说明理由
  · 【消失】      可能真修好了（好），也可能扫描器失明（坏）⇒ 先确认是前者
  · 【计数变化】  同一函数里同形态的裸写数量变了（加了一处 / 删了一处）

## 重生成基线

    python scripts/scan_unlocked_db_writes.py --write-baseline

**不要**用 `> 重定向`：Windows 上 Python 的 stdout 是文本模式，重定向会把
`\\n` 翻成 `\\r\\n`，与仓库其余 LF 文件不一致（每次重生成都变成全文件 diff）。
脚本的 `--write-baseline` 直接按字节写 LF。

重生成后，`docs/ADVERSARIAL_REVIEW_2026-10-19.md` F2 与
`docs/PLAN_2026-10-19_F1-F4.md` 里引用的「122 处 / 68 处」要同步改。
⚠️ **这一条没有机检**，只能靠 review —— 记在这里是为了不让它变成隐性假设。

## 为什么复用脚本、不重写一遍 AST 遍历

护栏若自己再走一遍 AST，两份判据必然漂移（一份改了另一份没改，护栏恒绿而
脚本仍报 121）。所以这里 `import scripts.scan_unlocked_db_writes`，
只调它的 `collect()` / `baseline_from()` / `key_of()` —— **同一份实现**。

## 反空转（本文件最容易变成「恒绿」的地方）

`collect()` 在文件解析失败时**继续**（往 stderr 打一行），那个文件的裸写就从
统计里消失了 ⇒ 比对会报「消失」，或者（若有人顺手重生成基线）变成静默的假绿。
所以：

  · `test_scanner_is_not_blind` 断言 `parse_errors == []`，并给文件数 / 写数
    设下限；
  · `TestScannerJudgment` 直接喂源码片段给 `Scanner`，证明**判定谓词**本身
    仍然分得清「加锁」与「不加锁」—— 否则「`collect()` 返回空 ⇒ 基线也重生成
    成空」这条路是走得通的，而它会让整个文件恒绿。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import scripts.scan_unlocked_db_writes as scan

BASELINE = Path(scan.BASELINE_PATH)


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _lines_by_key(hits: list[dict]) -> dict[str, list[int]]:
    """``key -> [行号...]``：只在失败信息里用（键本身刻意不含行号）。"""
    out: dict[str, list[int]] = {}
    for h in hits:
        out.setdefault(scan.key_of(h), []).append(h["line"])
    return out


def _scan_src(src: str) -> list[dict]:
    """把一段源码喂给 Scanner —— 用来单独检验判定谓词，不碰真实仓库。"""
    s = scan.Scanner("<memory>")
    s.visit(ast.parse(src))
    return s.hits


class TestScannerJudgment:
    """判定谓词本身。这些用例红 = 扫描器不再分得清锁，基线比对**全部失效**。"""

    def test_bare_write_is_reported(self):
        hits = _scan_src(
            "async def f(db):\n"
            "    await db.execute('UPDATE jobs SET status = 1')\n")
        assert [h["kind"] for h in hits] == ["write"]
        assert hits[0]["func"] == "f"
        assert hits[0]["method"] == "execute"

    def test_write_inside_db_lock_is_not_reported(self):
        hits = _scan_src(
            "async def f(db):\n"
            "    async with db_lock:\n"
            "        await db.execute('UPDATE jobs SET status = 1')\n")
        assert hits == []

    def test_qualified_db_lock_counts_as_protection(self):
        # `self.db_lock` / `locks.db_lock` 都按后缀认（_is_db_lock_with 的约定）
        for ctx in ("self.db_lock", "locks.db_lock"):
            hits = _scan_src(
                "async def f(db):\n"
                "    async with %s:\n"
                "        await db.execute('DELETE FROM findings')\n" % ctx)
            assert hits == [], ctx

    def test_other_lock_is_not_treated_as_db_lock(self):
        # 反向对照：任何名字**不**以 db_lock 结尾的锁都不算保护。
        # 没有这条，"所有 async with 都算加锁"这种退化实现也会通过上面两条。
        hits = _scan_src(
            "async def f(db):\n"
            "    async with other_lock:\n"
            "        await db.execute('DELETE FROM findings')\n")
        assert [h["kind"] for h in hits] == ["write"]

    def test_select_is_read_not_write(self):
        hits = _scan_src(
            "async def f(db):\n"
            "    await db.execute('SELECT id FROM jobs')\n")
        assert [h["kind"] for h in hits] == ["read"]

    def test_unstatable_sql_is_unknown(self):
        # SQL 在变量里 ⇒ 静态取不到首关键字 ⇒ 进 UNKNOWN，不混进写计数
        hits = _scan_src(
            "async def f(db, sql):\n"
            "    await db.execute(sql)\n")
        assert [h["kind"] for h in hits] == ["unknown"]

    def test_commit_is_write(self):
        # commit 闭合事务边界 ⇒ 一律算写（不靠 SQL 首关键字）
        hits = _scan_src(
            "async def f(db):\n"
            "    await db.commit()\n")
        assert [h["kind"] for h in hits] == ["write"]

    def test_nested_def_does_not_inherit_the_lock(self):
        """嵌套函数写在 with 体内 = **定义**在锁内，**调用**时锁早已释放。

        不重置锁作用域就会把它当成"已保护" ⇒ **静默漏报**（扫描器唯一的
        漏报方向）。本仓当前没有这种写法（修这个洞前后计数都是 121，F1 之后
        122 —— 该洞**不改变计数**），但它是"改坏了也不会有人发现"的那一类，
        所以单独钉住。
        """
        hits = _scan_src(
            "async def f(db):\n"
            "    async with db_lock:\n"
            "        async def g():\n"
            "            await db.execute('UPDATE jobs SET status = 1')\n"
            "        await g()\n")
        assert [h["kind"] for h in hits] == ["write"], (
            "嵌套 def 里的裸写被当成已保护了 —— 锁作用域没有在函数边界重置")
        assert hits[0]["func"] == "g"

    def test_key_is_line_independent(self):
        """键不含行号 —— 这是「无关编辑不必重生成基线」的全部依据。"""
        a = _scan_src("async def f(db):\n"
                      "    await db.execute('UPDATE t SET a = 1')\n")
        b = _scan_src("async def f(db):\n"
                      "\n"
                      "\n"
                      "    await db.execute('UPDATE t SET a = 1')\n")
        assert a[0]["line"] != b[0]["line"], "两段源码行号必须不同，否则本用例无效"
        assert scan.key_of(a[0]) == scan.key_of(b[0])


class TestScannerIsNotBlind:
    def test_no_parse_failures(self):
        res = scan.collect()
        assert res["parse_errors"] == [], (
            "有文件解析失败 ⇒ 它们**没进统计**，裸写计数偏低，基线比对会误报"
            "「消失」：\n  " + "\n  ".join(res["parse_errors"]))

    def test_scan_reaches_enough_of_the_tree(self):
        """下限只为防「扫描器失明」，**不是**要求债务保持这么多。

        `SCAN_DIRS` 写错、遍历被短路、全部文件解析失败 —— 这三种都会让
        `collect()` 返回接近空的结果，而「基线也重生成成空」这条路是走得通的。
        债务真降到 50 以下时，把下限调低即可（那时是好事）。
        """
        res = scan.collect()
        assert res["files"] >= 60, "只扫到 %d 个文件" % res["files"]
        assert len(res["writes"]) >= 50, "只扫到 %d 处裸写" % len(res["writes"])


class TestBaselineRatchet:
    def test_unlocked_writes_match_baseline(self):
        res = scan.collect()
        cur = scan.baseline_from(res)
        base = _load_baseline()
        lines = {
            "writes": _lines_by_key(res["writes"]),
            "unknown": _lines_by_key(res["unknown"]),
        }

        problems: list[str] = []
        for section, label in (("writes", "裸写"), ("unknown", "SQL 取不到")):
            c, b, ln = cur[section], base[section], lines[section]
            added = sorted(k for k in c if k not in b)
            gone = sorted(k for k in b if k not in c)
            changed = sorted(k for k in c if k in b and c[k] != b[k])

            if added:
                problems.append("【新增】%d 处%s —— 新写的 DB 写没有 db_lock"
                                % (len(added), label))
                for k in added:
                    problems.append("  + %s  L%s" % (
                        k, ",".join(str(x) for x in ln.get(k, []))))
            if gone:
                problems.append("【消失】%d 处%s —— 真修好了就重生成基线；"
                                "否则是扫描器失明" % (len(gone), label))
                for k in gone:
                    problems.append("  - %s" % k)
            if changed:
                problems.append("【计数变化】%d 个键%s" % (len(changed), label))
                for k in changed:
                    problems.append("  ~ %s  %d -> %d  L%s" % (
                        k, b[k], c[k], ",".join(str(x) for x in ln.get(k, []))))

        assert not problems, (
            "裸 DB 写与基线不一致（基线：%s）\n\n%s\n\n"
            "重生成：python scripts/scan_unlocked_db_writes.py --write-baseline\n"
            "若为【新增】：先问能不能加锁；不能，再登记进基线并在 diff 里写明理由。"
            % (BASELINE.name, "\n".join(problems)))


class TestBaselineShape:
    def test_baseline_is_lf_without_bom(self):
        raw = BASELINE.read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", "基线带 BOM（仓库约定：无 BOM）"
        assert b"\r\n" not in raw, "基线是 CRLF（仓库约定：LF）"

    def test_keys_are_four_part_and_counts_are_positive_ints(self):
        base = _load_baseline()
        for section in ("writes", "unknown"):
            assert section in base, section
            for k, n in base[section].items():
                # 文件|函数|方法|SQL前缀 —— 少一段就说明 key_of 变了
                assert k.count("|") == 3, k
                assert isinstance(n, int) and n >= 1, (k, n)

    def test_note_still_says_debt_not_approval(self):
        """锁住 `_note` 的语义。

        这句话是读者唯一会看到的东西。若它被改成「已审计安全」，下一个人就会
        停止质疑基线里那些条目 —— 而本护栏恰恰**没有**验证过它们安全。
        """
        note = _load_baseline()["_note"]
        assert "不是" in note, note
        assert "债务" in note, note
