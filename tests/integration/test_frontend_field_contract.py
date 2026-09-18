"""#171 — "前端消费的字段，必须在它实际的数据源里存在"的契约机检。

**要防的失效模式**（#132 / #133 / #134 三条缺陷的**共同成因**）：
护栏只断言 JS **源码里出现了某个标识符** —— 例如
`test_config_error_visibility.py` 的 `assert "job.failed_pages" in js` ——
却**从不验证提供该字段的端点是否真的返回了它**。于是 `listings.py` 的
SELECT 里根本没有 `failed_pages` 时，护栏照旧全绿，缺陷一路漏到用户面前：
GMP 审阅界面呈现为**绿点 + 0 条 finding + 无原因**，与"记录确实无异常"
不可区分。**空护栏比没有护栏更危险**，因为它会让人相信这里已经查过了。

本文件把契约变成**可执行的**：

1. 字段集**不手写** —— 从 `static/upload.js` 的行构建函数体里**派生**
   （扫 `job.X` / `d.X` 访问）。手写的清单本身就是第二份真值，会漂。
2. 拿三个真实数据源（`GET /api/jobs`、`GET /api/jobs/{id}`、SSE 快照）
   逐一核对**存在性 + 类型 + 取值**。
3. 断言同一字段在不同来源上**类型与取值一致** —— "同一字段在详情与列表
   行为不一致"正是 #132/#134 的成因（`_parse_failed_pages` 的注释原话）。

⇒ 前端新消费一个字段，本护栏立刻要求对应端点提供它；端点删字段，立刻红。

与既有护栏的分工（两者缺一不可）：
- `test_config_error_visibility.py`：锁"前端**有没有消费**"（源码契约）。
- 本文件：锁"数据源**有没有提供、类型对不对**"（接口契约）。
"""
import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

REPO = Path(__file__).resolve().parents[2]
UPLOAD_JS = REPO / "static" / "upload.js"
LISTINGS_PY = REPO / "api" / "jobs" / "listings.py"
REVIEW_JS = REPO / "static" / "review.js"
REVIEW_HTML = REPO / "templates" / "review.html"


# ---------------------------------------------------------------------------
# 派生：从 JS 函数体里抽出被消费的字段
# ---------------------------------------------------------------------------


def _fn_body(src: str, name: str) -> str:
    """截取 `function name(...) {` 及其配对收尾大括号之间的函数体。

    用花括号配平扫描而非缩进正则 —— 函数体里含嵌套箭头函数（
    `updateJobRowLive` 里就有），缩进启发式会截早。
    """
    m = re.search(rf"function\s+{name}\s*\([^)]*\)\s*\{{", src)
    assert m, (
        f"static/upload.js 未找到 function {name} —— 改名/移动了？"
        f"本护栏的字段派生依赖它，请同步更新"
    )
    i = m.end()
    depth = 1
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"function {name} 的花括号未配平（解析失败）"
    return src[m.start():i]


def _consumed(body: str, prefix: str) -> set[str]:
    """函数体内 `prefix.<字段>` 形式的字段访问。"""
    return set(re.findall(rf"\b{prefix}\.([a-zA-Z_][a-zA-Z0-9_]*)", body))


_JS = UPLOAD_JS.read_text(encoding="utf-8")

# 冷加载路径：列表响应直接喂给 renderJobRow（upload.js:397）
# ⇒ 这些字段必须由 `GET /api/jobs` 提供。
LIST_ROW_FIELDS: set[str] = _consumed(_fn_body(_JS, "renderJobRow"), "job") | _consumed(
    _fn_body(_JS, "buildMetaLine"), "job"
)

# 实时/终态路径：SSE 快照喂给 updateJobRowLive / buildRowFromSnapshot
# ⇒ 这些字段必须由 SSE 快照提供（`_get_job_progress`）。
# 注意：`filename` / `created_at` 刻意**不在此列** —— 快照不带它们，
# upload.js 显式从旧行的 DOM（dataset / .job-meta）继承，见
# buildRowFromSnapshot 的对象字面量。所以派生集天然排除了它们（前缀不同）。
SNAP_ROW_FIELDS: set[str] = _consumed(
    _fn_body(_JS, "buildRowFromSnapshot"), "d"
) | _consumed(_fn_body(_JS, "updateJobRowLive"), "d")

# 类型契约：字段 → 允许的 Python 类型。
# None 一律显式列入允许集（列可空 / 解析失败），**不**用"归一成空值"的方式
# 绕开 —— `None`（未能给出清单）与 `[]`（确认零失败页）语义不同（GMP 审阅下）。
FIELD_TYPES: dict[str, tuple] = {
    "id": (str,),
    "filename": (str,),
    "status": (str,),
    "total_pages": (int, type(None)),
    "created_at": (str, type(None)),
    "failed_pages": (list, type(None)),
    "error_message": (str, type(None)),
    "ocr_progress": (dict,),
    "self_heal_progress": (dict, type(None)),
    "cross_progress": (dict, type(None)),
    "pages_analyzed": (int,),
    "phase": (str,),
    "ocr_backend_used": (str, type(None)),
    "ocr_backend_display": (str, type(None)),
}


def _check_row(where: str, row: dict, fields: set[str]) -> None:
    """字段必须存在、类型必须符合契约。"""
    for f in sorted(fields):
        assert f in row, (
            f"[{where}] 前端消费的字段 `{f}` 在响应里**不存在**。\n"
            f"这正是 #134 的成因：前端按 `job.{f}` 取值，端点却不返回它 ——\n"
            f"界面上表现为该信息静默缺失（GMP 假阴性表面）。\n"
            f"实际返回的键：{sorted(row.keys())}"
        )
        allowed = FIELD_TYPES.get(f)
        assert allowed is not None, (
            f"字段 `{f}` 未登记类型契约 —— 前端新增消费时要在这里补一行"
            f"（否则本护栏无法校验它）"
        )
        assert isinstance(row[f], allowed), (
            f"[{where}] 字段 `{f}` 类型不符："
            f"期望 {tuple(t.__name__ for t in allowed)}，实际 {type(row[f]).__name__}"
            f"（值={row[f]!r}）。\n"
            f"#132 教训：SQLite 里 TEXT 列直接透传会得到『装着 JSON 的字符串』，"
            f"前端 `Array.isArray` 一判即静默丢弃。"
        )


# ---------------------------------------------------------------------------
# 提取器自身的锚点（防空转）
# ---------------------------------------------------------------------------


class TestExtractorIsNotVacuous:
    """若提取器悄悄返回空集，下面所有用例都会变成空转 —— 那才是最大的假绿。"""

    def test_derived_sets_are_non_empty(self):
        assert LIST_ROW_FIELDS, "列表行字段派生为空 —— 提取器失效"
        assert SNAP_ROW_FIELDS, "快照行字段派生为空 —— 提取器失效"

    def test_anchor_fields_present(self):
        """已知锚点：这三条缺陷涉及的字段必须都在派生集里。"""
        assert {"failed_pages", "error_message"} <= LIST_ROW_FIELDS, (
            "#134 的两列必须仍被列表行消费（否则有人把消费代码删了）"
        )
        assert {"ocr_backend_used"} <= LIST_ROW_FIELDS, (
            "OCR 后端标签（GMP 追溯）必须仍被列表行消费"
        )
        assert "failed_pages" in SNAP_ROW_FIELDS
        # 快照侧不该出现从 DOM 继承的字段 —— 出现即说明有人把继承改成了依赖快照
        assert "created_at" not in SNAP_ROW_FIELDS

    def test_list_only_fields_are_a_subset_of_known_contract(self):
        """派生集里的每个字段都必须有类型契约，防止新字段悄悄溜过校验。"""
        unknown = (LIST_ROW_FIELDS | SNAP_ROW_FIELDS) - set(FIELD_TYPES)
        assert not unknown, f"这些字段没有类型契约，请补进 FIELD_TYPES：{sorted(unknown)}"


# ---------------------------------------------------------------------------
# 真实数据源契约
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sources(test_db):
    """造两个字段最丰富的 job，返回 (client, db, 列表行, 详情, 快照)。

    ⚠️ 不要再 `await test_db` —— 它是**已 await 过**的 Connection，再 await
    会触发 aiosqlite `Connection.start()` 重入（实测 `RuntimeError:
    threads can only be started once`）。直接当连接用（同 `client_with_job`）。

    故意让 `c-active` 带上 `_parse_error` 失败页，用来锁住
    `pages_analyzed` 的口径（失败页**不算**已分析，见 #131）。
    """
    db = test_db
    import json as _json

    ocr = _json.dumps({
        "done": 2, "total": 4,
        "self_heal": {"done": 1, "total": 2},
        "cross": {"done": 1, "total": 3, "label": "规则校验"},
    })
    for row in (
        # id, filename, status, total_pages, failed_pages, error_message, ocr, backend
        ("c-active", "active.pdf", "analyzing", 4, "[3]", "第 3 页分析失败",
         ocr, "paddle"),
        ("c-review", "ok.pdf", "review", 5, None, None, None, "mineru"),
    ):
        await db.execute(
            "INSERT INTO jobs (id, filename, status, total_pages, pdf_path, "
            "created_at, failed_pages, error_message, ocr_progress, "
            "ocr_backend_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row[0], row[1], row[2], row[3], "", "2026-09-18T10:00:00",
             row[4], row[5], row[6], row[7]),
        )
    # 3 页已分析：其中第 3 页是 _parse_error 失败页 ⇒ pages_analyzed 应为 2
    await db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?,?,?,?)",
        ("c-active", 1, "<p>x</p>", '{"steps":[],"overall_confidence":"high"}'),
    )
    await db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?,?,?,?)",
        ("c-active", 2, "<p>x</p>", '{"steps":[],"overall_confidence":"high"}'),
    )
    await db.execute(
        "INSERT INTO page_cache (job_id, page, raw_html, structured_json) "
        "VALUES (?,?,?,?)",
        ("c-active", 3, "<p>x</p>",
         '{"_parse_error": true, "_error": "boom", "overall_confidence": "low"}'),
    )
    await db.commit()

    from main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost:8000"
    ) as c:
        r = await c.get("/api/jobs")
        assert r.status_code == 200, r.text
        list_rows = {j["id"]: j for j in r.json()["jobs"]}

        r = await c.get("/api/jobs/c-active")
        assert r.status_code == 200, r.text
        detail = r.json()

        from api.jobs.status import _get_job_progress
        snapshot = await _get_job_progress(db, "c-active")
        assert snapshot, "快照为空"

        yield c, db, list_rows, detail, snapshot


class TestListEndpointSuppliesRowFields:
    """`GET /api/jobs` 是**冷加载**的数据源：整行由它渲染。"""

    @pytest.mark.asyncio
    async def test_every_consumed_field_is_present_and_typed(self, sources):
        _, _, list_rows, _, _ = sources
        assert list_rows, "列表为空"
        for jid, row in list_rows.items():
            _check_row(f"GET /api/jobs [{jid}]", row, LIST_ROW_FIELDS)


class TestSnapshotSuppliesRowFields:
    """SSE 快照是**实时/终态**的数据源（终态行由它整体重建）。"""

    @pytest.mark.asyncio
    async def test_every_consumed_field_is_present_and_typed(self, sources):
        _, _, _, _, snapshot = sources
        _check_row("SSE 快照", snapshot, SNAP_ROW_FIELDS)


class TestDetailEndpointSuppliesRowFields:
    """`GET /api/jobs/{id}` 是复核页/轮询的数据源。"""

    @pytest.mark.asyncio
    async def test_every_consumed_field_is_present_and_typed(self, sources):
        _, _, _, detail, _ = sources
        # 详情端点提供的是超集：行构建器消费的字段它必须全给
        _check_row("GET /api/jobs/{id}", detail, LIST_ROW_FIELDS & SNAP_ROW_FIELDS)


class TestSourcesAgreeOnSharedFields:
    """同一字段在不同来源上必须**类型与取值一致**。

    #132 / #134 的成因原话："同一字段在详情与列表行为不一致"。
    只测"每个来源各自自洽"抓不到它 —— 必须**跨来源比对**。
    """

    @pytest.mark.asyncio
    async def test_shared_fields_have_identical_value_and_type(self, sources):
        _, _, list_rows, detail, snapshot = sources
        row = list_rows["c-active"]
        shared = (LIST_ROW_FIELDS & SNAP_ROW_FIELDS) - {"filename", "created_at"}
        for f in sorted(shared):
            # 快照不含 filename/created_at（从 DOM 继承）→ 已在上面排除
            assert f in detail, f"详情端点缺 {f}"
            assert type(row[f]) is type(detail[f]) is type(snapshot[f]), (
                f"字段 `{f}` 在三个来源上类型不一致："
                f"列表={type(row[f]).__name__}、详情={type(detail[f]).__name__}、"
                f"快照={type(snapshot[f]).__name__}"
            )
            assert row[f] == detail[f] == snapshot[f], (
                f"字段 `{f}` 取值不一致："
                f"列表={row[f]!r}、详情={detail[f]!r}、快照={snapshot[f]!r} —— "
                f"同一字段两套口径，正是 #132/#134 的成因"
            )

    @pytest.mark.asyncio
    async def test_failed_pages_is_a_real_list_everywhere(self, sources):
        """#132 定点：字符串 `"[3]"` 与列表 `[3]` 必须不再混用。"""
        _, _, list_rows, detail, snapshot = sources
        row = list_rows["c-active"]
        for where, val in (
            ("列表", row["failed_pages"]),
            ("详情", detail["failed_pages"]),
            ("快照", snapshot["failed_pages"]),
        ):
            assert isinstance(val, list), (
                f"[{where}] failed_pages 不是 list 而是 {type(val).__name__}：{val!r}"
                f" —— 前端 `Array.isArray` 会静默丢弃，失败页数直接消失"
            )
            assert val == [3], f"[{where}] failed_pages 取值错：{val!r}"

    @pytest.mark.asyncio
    async def test_pages_analyzed_excludes_failed_pages(self, sources):
        """#131 口径：失败页（`_parse_error`）**不算**已分析。

        三个来源必须同口径 —— 否则同一份记录在列表与详情上给出不同的
        "分析了几页"，GMP 审阅会当场追问。
        """
        _, _, list_rows, detail, snapshot = sources
        assert list_rows["c-active"]["pages_analyzed"] == 2, (
            "列表页 pages_analyzed 口径错（3 页落库、其中 1 页 _parse_error ⇒ 应为 2）"
        )
        assert detail["pages_analyzed"] == 2
        assert snapshot["pages_analyzed"] == 2


class TestDerivationsAreSharedNotDuplicated:
    """结构侧护栏：列表端点必须**复用**既有派生函数，不得自写第二套口径。

    行为侧用例能证明"这一刻取值一致"，但证明不了"下次改动仍一致" ——
    若有人在 listings.py 里内联一份 `json.loads(ocr_progress)`，两个来源
    就各自成为真值源，将来必然漂（本仓库最贵的一类教训）。
    """

    def _listings_tree(self):
        import ast
        src = LISTINGS_PY.read_text(encoding="utf-8")
        return ast.parse(src), src

    def test_listings_reuses_canonical_helpers(self):
        _tree, src = self._listings_tree()
        for name in (
            "_parse_failed_pages",
            "_parse_ocr_progress",
            "_parse_self_heal_progress",
            "_count_analyzed_pages",
            "_derive_phase",
            "_ocr_backend_display",
        ):
            assert name in src, (
                f"listings.py 未复用 `{name}` —— 若改为内联实现，"
                f"列表与详情就成为两套口径（#132/#134 的成因）"
            )

    def test_listings_does_not_reparse_json_itself(self):
        """任何**本地** JSON 反序列化都是"第二套口径"的开端。

        ⚠️ 这里刻意不查字面量 `"json.loads" not in src` —— 变异验证实测：
        把它写成 `_j.loads(...)`（只是换了个别名字符串）就能溜过字符串匹配，
        护栏照样全绿。改为走 AST 认**调用形态**，别名/import 方式都挡得住。
        """
        import ast
        tree, _src = self._listings_tree()
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "loads":
                offenders.append(ast.unparse(f))      # json.loads / _j.loads / …
            elif isinstance(f, ast.Name) and f.id == "loads":
                offenders.append("loads")             # from json import loads
        assert not offenders, (
            f"listings.py 出现了本地 JSON 反序列化 {offenders} —— "
            f"这些列应交给 api/jobs/status.py 的公共解析函数（单一真值源）。"
            f"本地再解析一次，列表与详情就各自成为真值源，#132/#134 会重演。"
        )


# ---------------------------------------------------------------------------
# 复核页的 SSR 桥接契约（`window.__PBC__` 也是一个"数据源"）
# ---------------------------------------------------------------------------


def _strip_jinja(src: str) -> str:
    """剥掉 Jinja 注释与表达式。

    必须先剥再配平花括号 —— `{{ job_id | tojson }}` 里的 `{{` `}}` 会被
    朴素的花括号计数当成嵌套块，导致块在第一个变量处就被截断。
    （本护栏第一版就踩了这个坑；`{{ }}` 剥掉后键名仍可正则提取。）
    """
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", "", src, flags=re.S)


def _braced_block(src: str, anchor: str) -> str:
    """`anchor` 之后第一个 `{` 起、配平到对应 `}` 的块内文本。"""
    assert anchor in src, f"未找到锚点 {anchor!r} —— 改名了？同步更新本护栏"
    start = src.index("{", src.index(anchor))
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:j]
    raise AssertionError(f"锚点 {anchor!r} 的块花括号未配平（解析失败）")


def _ssr_bridge_keys() -> set[str]:
    """`window.__PBC__ = {...}` 注入的顶层键。"""
    html = _strip_jinja(REVIEW_HTML.read_text(encoding="utf-8"))
    blk = _braced_block(html, "window.__PBC__ = ")
    return set(re.findall(r"([a-z_][a-zA-Z0-9_]*)\s*:", blk))


def _ssr_consumed_keys() -> set[str]:
    """review.js 里 `ctx.<字段>` 形式的消费点。"""
    js = REVIEW_JS.read_text(encoding="utf-8")
    return set(re.findall(r"\bctx\.([a-zA-Z_][a-zA-Z0-9_]*)", js))


class TestSsrBridgeContract:
    """`window.__PBC__` 是复核页首屏的**唯一**数据源。

    #171 的同类缺陷：`page_ocr_empty` 一直在 Jinja 上下文里
    （`main.py` 已注入、模板据此 SSR 渲染），却**漏在 JS 桥接对象里**
    ⇒ review.js 读到的永远是 `undefined`。这**不是"少个标记"**：
    `currentPageFlags.ocrEmpty` 恒为 false ⇒ 首屏兜底会把 OCR 空页的
    SSR 文案"本页无 OCR 内容"**覆盖成"本页无问题"** —— 恰好是 #136
    要消灭的 GMP 假阴性（OCR 空页被呈现为"记录无异常"）。

    ⚠️ 这类缺陷**不可能**被"抓取页面 HTML 断言文案"的用例发现：
    拿到的是 SSR 渲染结果，而覆盖发生在浏览器里 JS 执行之后。
    所以契约必须落在**数据通道**上（消费集 ⊆ 注入集），而不是文案上。
    """

    def test_bridge_extraction_is_not_vacuous(self):
        consumed, injected = _ssr_consumed_keys(), _ssr_bridge_keys()
        assert len(consumed) >= 10, f"ctx 消费集派生过少（{sorted(consumed)}）"
        assert len(injected) >= 10, f"__PBC__ 注入集派生过少（{sorted(injected)}）"
        assert "job_id" in injected and "page_parse_error" in injected, (
            "锚点字段不在注入集里 —— 提取器解析错位了"
        )

    def test_no_consumed_key_is_missing_from_the_bridge(self):
        consumed, injected = _ssr_consumed_keys(), _ssr_bridge_keys()
        missing = sorted(consumed - injected)
        assert not missing, (
            f"review.js 消费了这些 ctx 字段，但 `window.__PBC__` 没注入：{missing}\n"
            f"后果不只是取值 undefined —— 它会把依赖该标记的分支静默走错"
            f"（例如 OCR 空页被当成'本页无问题'）。\n"
            f"（注：注入集里多余的键无害，本护栏只查'消费了却没给'。）"
        )

    def test_no_dynamic_ctx_access(self):
        """`ctx[expr]` 会让上面的静态派生静默漏字段 —— 一旦出现就必须改法。"""
        js = REVIEW_JS.read_text(encoding="utf-8")
        dyn = re.findall(r"\bctx\[[^\]]*\]", js)
        assert not dyn, (
            f"review.js 出现动态 ctx 访问 {dyn} —— 本护栏只认 `ctx.字段` 形式，"
            f"动态访问会成为看不见的漏字段通道。请改为显式字段名（或同步扩展本护栏）"
        )

    def test_ocr_empty_flag_is_bridged_end_to_end(self):
        """#171 定点：OCR 空页标记必须从 Jinja 上下文一路桥到 JS。"""
        assert "page_ocr_empty" in _ssr_consumed_keys(), (
            "review.js 不再消费 page_ocr_empty —— 空页文案分支被删了？"
        )
        assert "page_ocr_empty" in _ssr_bridge_keys(), (
            "page_ocr_empty 未注入 window.__PBC__ ⇒ 首屏把'本页无 OCR 内容'"
            "覆盖成'本页无问题'（GMP 假阴性）"
        )
        assert "page_ocr_empty" in REVIEW_HTML.read_text(encoding="utf-8"), (
            "模板未收到 page_ocr_empty（main.py 的渲染上下文掉了该键？）"
        )
